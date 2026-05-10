#!/usr/bin/env python
"""Diagnóstico de cobertura de reforma — invocado quando o audit dispara
'⚠ Cobertura de reforma'.

Read-only: não toca DB, não chama LLM. Só inspeciona o estado atual do
SQLite e devolve um relatório textual identificando a causa provável
(LLM em fallback, batch incompleto, severidade enviesada) + sugere o
comando exato pra retry direcionado.

Uso:
    PYTHONPATH=. .venv/bin/python scripts/diagnose_cobertura.py
    make diagnose-cobertura

Sai com exit code 0 sempre — script é diagnóstico, não gate.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional, TextIO

from sqlmodel import Session, select

from carros_sa.db import DEFAULT_DB_PATH, get_engine
from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.laudo_audit import (
    PDF_DIR_DEFAULT,
    verificar_laudo_completo,
)


def _gemini_healthcheck() -> tuple[bool, str]:
    """Ping leve no Gemini sem chamar a API real (custaria créditos).

    Estratégia: verifica se a env var está setada e parece válida (>20 chars).
    Não tenta autenticar — só sinaliza "config presente vs ausente". Operador
    decide se vale a pena tentar de fato (rodar uma triagem custaria moeda).
    """
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return False, "GEMINI_API_KEY não setada"
    if len(key) < 20:
        return False, f"GEMINI_API_KEY muito curta ({len(key)} chars) — provavelmente inválida"
    return True, "GEMINI_API_KEY presente"


def diagnosticar(session: Session, out: TextIO = sys.stdout) -> None:
    """Imprime relatório de cobertura no `out`."""
    agora = datetime.now()
    lotes_ativos = session.exec(
        select(Lote).where(Lote.fim_em > agora)
    ).all()
    total = len(lotes_ativos)

    avaliacoes_por_lote = {
        av.lote_id: av
        for av in session.exec(select(AvaliacaoLote)).all()
    }

    com_avaliacao = 0
    sem_avaliacao = 0
    com_laudo_sem_reforma: list[tuple[str, str]] = []
    sem_laudo: list[tuple[str, str]] = []
    motivos_sem_laudo: Counter = Counter()
    com_laudo_total = 0

    for lote in lotes_ativos:
        av = avaliacoes_por_lote.get(lote.id)
        if av is None:
            sem_avaliacao += 1
            continue
        com_avaliacao += 1

        laudo: Optional[LaudoCache] = session.get(LaudoCache, lote.id)
        status = verificar_laudo_completo(lote, laudo, pdf_dir=PDF_DIR_DEFAULT)
        modelo_str = f"{lote.marca or '?'} {lote.modelo or '?'} {lote.ano or '?'}"

        if status.laudo_cache_ok:
            com_laudo_total += 1
            if (av.reforma_estimada or 0) == 0:
                com_laudo_sem_reforma.append((lote.id, modelo_str))
        else:
            sem_laudo.append((lote.id, modelo_str))
            motivos_sem_laudo[status.motivo or "desconhecido"] += 1

    print(f"Diagnóstico de cobertura de reforma — {agora.strftime('%d/%m/%Y %H:%M')}", file=out)
    print("=" * 70, file=out)
    print(f"Universo: {total} lotes ativos (fim_em > now())", file=out)
    print(f"  com avaliação: {com_avaliacao}", file=out)
    print(f"  sem avaliação: {sem_avaliacao}", file=out)
    print(f"  com laudo válido: {com_laudo_total}", file=out)
    print(f"  com laudo + reforma=0: {len(com_laudo_sem_reforma)}  ← suspeito", file=out)
    print(f"  sem laudo válido: {len(sem_laudo)}", file=out)
    print(file=out)

    if motivos_sem_laudo:
        print("Distribuição de motivos (lotes sem laudo válido):", file=out)
        for motivo, n in motivos_sem_laudo.most_common():
            print(f"  {motivo}: {n}", file=out)
        print(file=out)

    if com_laudo_sem_reforma[:5]:
        print("Exemplos (laudo válido + reforma=0):", file=out)
        for lote_id, modelo in com_laudo_sem_reforma[:5]:
            print(f"  {lote_id} — {modelo}", file=out)
        print(file=out)

    gemini_ok, gemini_msg = _gemini_healthcheck()
    print(f"Gemini healthcheck: {'OK' if gemini_ok else 'FALHA'} — {gemini_msg}", file=out)
    print(file=out)

    print("Sugestão:", file=out)
    if not gemini_ok:
        print(
            "  → Sem GEMINI_API_KEY válida; setar env var ou trocar VISION_PROVIDER=anthropic "
            "e re-rodar 'make triagem'.",
            file=out,
        )
    elif sem_laudo:
        print(
            f"  → {len(sem_laudo)} lotes sem laudo válido — re-disparar laudos pendentes "
            f"(motivo dominante: {motivos_sem_laudo.most_common(1)[0][0] if motivos_sem_laudo else '?'}). "
            f"Rodar 'make limpar-decoys' + 'make triagem' pra forçar nova passagem do extrator.",
            file=out,
        )
    elif com_laudo_sem_reforma:
        print(
            f"  → {len(com_laudo_sem_reforma)} lotes com laudo válido produziram reforma=0 — "
            f"possível bug no EstimadorReformaLLM (severidade enviesada, prompt regredido, "
            f"itens vazios). Inspecionar logs do estimador nos lotes acima e considerar "
            f"'make limpar-decoys && make triagem' pra reprocessar.",
            file=out,
        )
    else:
        print("  → Sem causa óbvia detectada; abrir 1 lote suspeito manualmente e checar laudo + estimativa.", file=out)
    print("=" * 70, file=out)


def main() -> int:
    db_path = Path(DEFAULT_DB_PATH)
    if not db_path.exists():
        print(f"DB não encontrado em {db_path} — nada a diagnosticar.", file=sys.stderr)
        return 0
    engine = get_engine(db_path)
    with Session(engine) as session:
        diagnosticar(session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
