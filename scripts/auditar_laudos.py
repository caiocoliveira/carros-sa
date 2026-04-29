#!/usr/bin/env python
"""Audita laudos dos lotes ativos de uma empresa.

Para cada lote ativo (com avaliação E `fim_em` no futuro E não-encerrado),
verifica as 3 condições de "laudo completo":

  1. PDF persistido em `data/laudos_pdfs/<lote_id>.pdf` (>5KB).
  2. `LaudoCache.confidence >= 0.6` (laudo veio de PDF, não de fallback).
  3. `raw_json.detalhe.laudo_pdf_url` passa em `is_laudo_pdf_url`.

Imprime relatório curto + lista os lotes incompletos com motivo. Sai com
código != 0 quando há incompletos — útil pro cron diário travar e o
operador olhar o log.

Uso:
    PYTHONPATH=. python scripts/auditar_laudos.py
    PYTHONPATH=. python scripts/auditar_laudos.py --empresa carros_uberlandia
    PYTHONPATH=. python scripts/auditar_laudos.py --strict     # exit 1 se incompletos
    PYTHONPATH=. python scripts/auditar_laudos.py --incluir-encerrados
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from carros_sa.db import get_session, init_db
from carros_sa.tools.laudo_audit import auditar

console = Console()
app = typer.Typer(add_completion=False)


@app.command()
def main(
    empresa: str = typer.Option("carros_uberlandia", help="ID da empresa"),
    strict: bool = typer.Option(
        False, "--strict",
        help="Sai com código 1 quando há lotes incompletos (default: 0).",
    ),
    incluir_encerrados: bool = typer.Option(
        False, "--incluir-encerrados",
        help="Audita TODOS os lotes avaliados, não só os ativos.",
    ),
    max_linhas: int = typer.Option(
        30, help="Máximo de lotes incompletos a imprimir."
    ),
) -> None:
    init_db()
    with get_session() as session:
        rel = auditar(session, empresa, apenas_ativos=not incluir_encerrados)

    pct = (rel.completos / rel.total * 100) if rel.total else 0.0
    console.print(
        f"\n[bold]Laudos de {empresa}[/bold] "
        f"({'todos avaliados' if incluir_encerrados else 'só ativos'}):"
    )
    console.print(f"  Total auditado:      {rel.total}")
    console.print(
        f"  Completos:           {rel.completos} "
        f"({pct:.1f}%)"
    )
    console.print(f"  [yellow]Incompletos:         {len(rel.incompletos)}[/yellow]")
    if rel.incompletos:
        console.print(f"    sem PDF baixado:   {rel.sem_pdf}")
        console.print(f"    cache conf<0.6:    {rel.cache_baixa_conf}")
        console.print(f"    URL inválida:      {rel.url_invalida}")

        tbl = Table(title="Lotes incompletos")
        tbl.add_column("Lote")
        tbl.add_column("Modelo")
        tbl.add_column("PDF", justify="center")
        tbl.add_column("Cache", justify="center")
        tbl.add_column("URL", justify="center")
        tbl.add_column("Motivo")
        for s in rel.incompletos[:max_linhas]:
            def chk(b: bool) -> str:
                return "[green]✓[/green]" if b else "[red]✗[/red]"
            tbl.add_row(
                s.lote_id,
                s.modelo[:32],
                chk(s.pdf_local),
                chk(s.laudo_cache_ok),
                chk(s.url_persistida_ok),
                s.motivo or "—",
            )
        console.print(tbl)

        if len(rel.incompletos) > max_linhas:
            console.print(f"  [dim]… +{len(rel.incompletos) - max_linhas} lotes truncados[/dim]")

        console.print(
            "\n[bold cyan]Como destravar:[/bold cyan]\n"
            "  1. [yellow]URL inválida[/yellow] → "
            "[code]make limpar-decoys[/code] (zera URLs decoy + derruba LaudoCache stale)\n"
            "  2. [yellow]PDF ausente / cache baixo[/yellow] → "
            "[code]PYTHONPATH=. .venv/bin/python scripts/reprocessar_lotes_do_db.py "
            "--empresa " + empresa + " --somente-ativos --somente-laudo-pendente[/code]\n"
            "  3. Ambos já rodam diariamente em [code]bash scripts/setup_cron.sh[/code]."
        )

    if strict and rel.incompletos:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
