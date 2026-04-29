#!/usr/bin/env python
"""Limpa URLs-decoy persistidas em `lote.raw_json["detalhe"]["laudo_pdf_url"]`.

Contexto: até abril/2026 o seletor JS `_EXTRACT_PDF_URL_JS` do scraper pegava o
link do rodapé institucional ("Relatório de Transparência Salarial" em
`storage.googleapis.com/app/uploads/.../Relatorio-de-Transparencia-...pdf`)
como se fosse o PDF do laudo do lote. O gate `is_laudo_pdf_url()` em
`scraping/parsers.py` agora rejeita esses decoys em tempo de scraping, mas
~74 lotes no DB ainda carregam o decoy em `raw_json.detalhe.laudo_pdf_url`
porque foram raspados antes do aperto.

Esses lotes envenenam o retry: o orquestrador vê a URL, acha que é laudo,
baixa, o `_pdf_eh_laudo_valido()` rejeita o PDF, e o lote cai em
`_laudo_sem_pdf` com confidence=0.5. Resultado: mostra "LAUDO NÃO CAPTURADO"
pro usuário até alguém re-raspar o detalhe.

O que este script faz:
  1. Varre todos os lotes no SQLite.
  2. Para cada um, olha `raw_json.detalhe.laudo_pdf_url`. Se a URL não passa
     no gate `is_laudo_pdf_url()`, considera decoy.
  3. **Remove a URL** do raw_json (seta `None`).
  4. **Derruba o LaudoCache** do lote (delete) pra garantir que o retry do
     pipeline vai re-executar o fluxo completo — incluindo re-scrape do
     detalhe com o seletor corrigido.
  5. Retorna contagem de lotes tocados.

Importável como biblioteca (`limpar_decoys(session) -> ResultadoLimpeza`)
pra uso em testes de regressão — o teste de DB hygiene chama em modo
`dry_run=True` e falha se achar algum decoy.

Uso CLI:
    PYTHONPATH=. python scripts/limpar_decoys_laudo.py             # limpa
    PYTHONPATH=. python scripts/limpar_decoys_laudo.py --dry-run   # só reporta
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import typer
from rich.console import Console
from sqlmodel import Session, select

from carros_sa.db import get_session, init_db
from carros_sa.models import LaudoCache, Lote
from carros_sa.scraping.parsers import is_laudo_pdf_url

console = Console()
app = typer.Typer(add_completion=False)


@dataclass
class ResultadoLimpeza:
    total_lotes: int = 0
    decoys_encontrados: int = 0
    decoys_limpos: int = 0
    laudos_derrubados: int = 0
    lotes_afetados: List[str] = field(default_factory=list)


def _extrair_url(raw_json: Optional[dict]) -> Optional[str]:
    """Extrai `detalhe.laudo_pdf_url` do raw_json, tolerante a raw_json None/dict vazio."""
    if not isinstance(raw_json, dict):
        return None
    det = raw_json.get("detalhe")
    if not isinstance(det, dict):
        return None
    url = det.get("laudo_pdf_url")
    return url if isinstance(url, str) else None


def limpar_decoys(session: Session, *, dry_run: bool = False) -> ResultadoLimpeza:
    """Varre o DB e limpa decoys. Seguro pra rodar idempotentemente.

    Em `dry_run=True` não persiste nada — útil pro teste de hygiene.
    """
    result = ResultadoLimpeza()

    lotes = session.exec(select(Lote)).all()
    result.total_lotes = len(lotes)

    for lote in lotes:
        url = _extrair_url(lote.raw_json)
        if not url:
            continue
        # `is_laudo_pdf_url()` é a fonte de verdade — qualquer URL que não
        # passa ali é decoy pra efeitos deste script.
        if is_laudo_pdf_url(url):
            continue

        result.decoys_encontrados += 1
        result.lotes_afetados.append(lote.id)

        if dry_run:
            continue

        # Limpa o campo — não apaga o dict `detalhe` inteiro pra preservar
        # status_laudo, specs, observacoes_anunciante etc, que ainda são
        # úteis pro `_laudo_sem_pdf` enquanto o retry não roda.
        raw = dict(lote.raw_json or {})
        det = dict(raw.get("detalhe") or {})
        det["laudo_pdf_url"] = None
        raw["detalhe"] = det
        lote.raw_json = raw
        session.add(lote)
        result.decoys_limpos += 1

        # Derruba o LaudoCache — sem isso, o retry `--somente-laudo-pendente`
        # ainda vê confidence<0.6 e tenta, mas sem invalidar o cache o
        # orquestrador pode pular em outros paths. Deletar força o fluxo
        # completo no próximo retry.
        laudo = session.get(LaudoCache, lote.id)
        if laudo is not None:
            session.delete(laudo)
            result.laudos_derrubados += 1

    if not dry_run:
        session.commit()

    return result


@app.command()
def main(
    dry_run: bool = typer.Option(False, "--dry-run", help="Só reporta, não persiste."),
) -> None:
    """Limpa decoys do DB. Idempotente — rodar N vezes é equivalente a rodar 1."""
    init_db()
    with get_session() as session:
        res = limpar_decoys(session, dry_run=dry_run)

    modo = "[yellow]DRY-RUN[/yellow]" if dry_run else "[green]persistido[/green]"
    console.print(f"\n[bold]Limpeza de decoys ({modo}):[/bold]")
    console.print(f"  Total de lotes no DB: {res.total_lotes}")
    console.print(f"  Decoys encontrados:   {res.decoys_encontrados}")
    if not dry_run:
        console.print(f"  Decoys limpos:        {res.decoys_limpos}")
        console.print(f"  LaudoCache derrubado: {res.laudos_derrubados}")
    if res.lotes_afetados:
        console.print(f"\n  Amostra de lotes afetados: {res.lotes_afetados[:10]}")

    if dry_run and res.decoys_encontrados > 0:
        # Sai com código != 0 pra facilitar uso em CI/test/pre-hook.
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
