"""Helpers para o ciclo de reconciliação de laudos.

Extraído do `scripts/reprocessar_lotes_do_db.py` pra ficar testável sem
Playwright. Centraliza a regra de "lote precisa de retry" — antes ela vivia
inline no script e cada chamador podia divergir.

Convenção de "pendente" (alinhada com `tools/laudo_audit.py` workstream U):

  - Lote ATIVO (`fim_em` futuro). Lote encerrado é descartado do export
    independentemente, então não precisamos gastar Playwright neles.
  - LaudoCache ausente OU confidence < 0.6. Tudo abaixo de 0.6 é fallback
    `_laudo_sem_pdf` ou textual com nada concreto, e a planilha sinaliza
    como ⚠ LAUDO NÃO CAPTURADO.

Uso típico (loop de reconciliação):

    for tentativa in range(1, max_tentativas + 1):
        pendentes = selecionar_pendentes(session, empresa_id=...)
        if not pendentes:
            break
        for lote in pendentes:
            await _pipeline_lote(lote, ...)
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlmodel import Session, select

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote


def selecionar_pendentes(
    session: Session,
    *,
    empresa_id: str,
    somente_sem_avaliacao: bool = False,
    somente_ativos: bool = False,
    somente_laudo_pendente: bool = False,
    max_lotes: Optional[int] = None,
) -> List[Lote]:
    """Aplica os 3 filtros opcionais e retorna lotes em ordem do banco.

    A re-consulta a cada chamada é importante: dentro de um loop de retry,
    lotes que tiveram sucesso na iteração anterior já não aparecem como
    pendentes na próxima — o filtro `somente_laudo_pendente` re-lê o
    `LaudoCache` atualizado.

    `LaudoCache` é carregado como tupla `(lote_id, confidence)` e não como
    entity pra evitar poluir o identity map da Session — quando o
    `_pipeline_lote` faz commit no meio do processamento, entities no
    identity map expiram e `session.get(LaudoCache, id)` posterior
    retorna None mesmo pra rows que existem (sintoma observado 2026-04-18
    após processar ~105/139 lotes: UNIQUE constraint).
    """
    lotes = list(session.exec(select(Lote)).all())

    if somente_sem_avaliacao:
        ja_avaliados = {
            row.lote_id
            for row in session.exec(
                select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa_id)
            ).all()
        }
        lotes = [l for l in lotes if l.id not in ja_avaliados]

    if somente_ativos:
        agora = datetime.now()
        lotes = [l for l in lotes if l.fim_em is not None and l.fim_em > agora]

    if somente_laudo_pendente:
        laudos = {
            row[0]: row[1]
            for row in session.exec(
                select(LaudoCache.lote_id, LaudoCache.confidence)
            ).all()
        }
        lotes = [
            l for l in lotes
            if laudos.get(l.id) is None or (laudos.get(l.id) or 0) < 0.6
        ]

    if max_lotes is not None:
        lotes = lotes[:max_lotes]

    return lotes
