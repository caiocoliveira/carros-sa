#!/usr/bin/env python
"""Audita a completude de laudo dos lotes ativos na planilha.

Verifica as 3 dimensões exigidas pra uma linha da planilha ser útil ao
operador:

    1. link na planilha (raw_json.detalhe.laudo_pdf_url é laudo válido)
    2. PDF baixado em data/laudos_pdfs/<lote_id>.pdf
    3. LaudoCache revisado (confidence >= 0.6)

Uso:
    PYTHONPATH=. python scripts/auditar_laudos.py                   # só reporta
    PYTHONPATH=. python scripts/auditar_laudos.py --fix             # tenta re-extrair laudos com PDF local
    PYTHONPATH=. python scripts/auditar_laudos.py --empresa carros_uberlandia

Exit codes:
    0  → tudo ok (zero zumbi)
    1  → algum zumbi remanescente (integra com `cron`/CI)

Complementa, não substitui, o ciclo principal do cron (triagem +
limpar_decoys + retry). Ideal rodar DEPOIS do cron pra pegar lotes que
escaparam do retry por rate-limit crônico ou modal lazy que não abriu.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from carros_sa.db import get_session, init_db
from carros_sa.tools.auditoria_laudo import (
    StatusCompletude,
    auditar_completude,
    reextrair_pendentes_com_pdf_local,
)

load_dotenv()
console = Console()
app = typer.Typer(add_completion=False)


_EMOJI = {
    StatusCompletude.OK: "[green]✓[/green]",
    StatusCompletude.LAUDO_PENDENTE: "[yellow]◐[/yellow]",
    StatusCompletude.SEM_PDF_LOCAL: "[yellow]⇩[/yellow]",
    StatusCompletude.SEM_LINK: "[red]✗[/red]",
}


def _imprimir_resumo(result, detalhes: bool = False) -> None:
    """Imprime resumo por status + opcionalmente os lotes afetados."""
    console.print(f"\n[bold]Lotes ativos na planilha:[/bold] {result.total_ativos}")

    tbl = Table(show_header=True)
    tbl.add_column("Status")
    tbl.add_column("Quantidade", justify="right")
    for status in StatusCompletude:
        n = result.contagens.get(status, 0)
        tbl.add_row(f"{_EMOJI[status]} {status.value}", str(n))
    console.print(tbl)

    if result.total_zumbis == 0:
        console.print("\n[bold green]✓ Zero zumbis — planilha consistente.[/bold green]")
        return

    console.print(
        f"\n[bold yellow]{result.total_zumbis} lote(s) zumbi — planilha incompleta.[/bold yellow]"
    )

    if not detalhes:
        return

    tbl2 = Table(title="Lotes zumbi (detalhe)")
    tbl2.add_column("Lote")
    tbl2.add_column("Modelo")
    tbl2.add_column("Status")
    tbl2.add_column("Razão", overflow="fold", max_width=60)
    for diag in result.diagnosticos:
        if diag.status == StatusCompletude.OK:
            continue
        tbl2.add_row(
            diag.lote_id,
            diag.modelo[:30],
            f"{_EMOJI[diag.status]} {diag.status.value}",
            diag.razao,
        )
    console.print(tbl2)


def _imprimir_plano_de_fix(result) -> None:
    """Mostra o que o operador deve fazer pra cada categoria de zumbi."""
    sem_link = result.contagens.get(StatusCompletude.SEM_LINK, 0)
    sem_pdf = result.contagens.get(StatusCompletude.SEM_PDF_LOCAL, 0)
    pendente = result.contagens.get(StatusCompletude.LAUDO_PENDENTE, 0)

    if pendente:
        console.print(
            f"\n[yellow]◐ {pendente} com PDF local mas laudo pendente →[/yellow] "
            "rode `python scripts/auditar_laudos.py --fix` (offline, sem rede)"
        )
    if sem_pdf:
        console.print(
            f"[yellow]⇩ {sem_pdf} com URL mas sem PDF local →[/yellow] "
            "rode `python scripts/reprocessar_lotes_do_db.py "
            "--somente-ativos --somente-laudo-pendente` (requer Playwright + cookies)"
        )
    if sem_link:
        console.print(
            f"[red]✗ {sem_link} sem URL do PDF →[/red] "
            "modal lazy não abriu ou lote sem laudo no AutoAvaliar. "
            "Próxima triagem completa (`make triagem`) re-tenta."
        )


@app.command()
def main(
    empresa: Optional[str] = typer.Option(
        None, help="Filtra auditoria por empresa_id (default: todas)"
    ),
    fix: bool = typer.Option(
        False, "--fix",
        help="Auto-fixa lotes com PDF local re-rodando o extrator de laudo",
    ),
    detalhes: bool = typer.Option(
        False, "--detalhes/--sem-detalhes",
        help="Lista cada lote zumbi individualmente (senão só contagem por status)",
    ),
) -> None:
    """Audita completude de laudo. Exit 1 se houver zumbi."""
    init_db()

    with get_session() as session:
        result = auditar_completude(session, empresa_id=empresa)

    _imprimir_resumo(result, detalhes=detalhes)
    _imprimir_plano_de_fix(result)

    if fix:
        console.print("\n[bold cyan]--fix: tentando re-extrair laudos com PDF local...[/bold cyan]")
        vision_client = None
        try:
            from carros_sa.agents.vision_clients import build_default_client
            vision_client = build_default_client()
            console.print(f"[cyan]Vision:[/cyan] {type(vision_client).__name__}")
        except Exception as exc:
            console.print(f"[yellow]Vision indisponível ({exc}) — só textual[/yellow]")

        with get_session() as session:
            fix_result = reextrair_pendentes_com_pdf_local(
                session, empresa_id=empresa, vision_client=vision_client,
            )

        console.print(
            f"[green]✓ {fix_result.sucessos}/{fix_result.tentativas} "
            f"laudos promovidos pra confidence≥0.6[/green]"
        )
        for motivo in fix_result.falhas[:10]:
            console.print(f"  [red]✗ {motivo}[/red]")
        if len(fix_result.falhas) > 10:
            console.print(f"  [dim]... +{len(fix_result.falhas) - 10} falhas[/dim]")

        # Re-audita pra exit code refletir estado pós-fix.
        with get_session() as session:
            result = auditar_completude(session, empresa_id=empresa)
        console.print("\n[bold]Estado pós-fix:[/bold]")
        _imprimir_resumo(result, detalhes=False)

    sys.exit(1 if result.total_zumbis > 0 else 0)


if __name__ == "__main__":
    app()
