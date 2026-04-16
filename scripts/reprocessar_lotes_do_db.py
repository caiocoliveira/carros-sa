#!/usr/bin/env python
"""Reprocessa TODOS os lotes no DB rodando `_pipeline_lote` em cada um.

Pula a fase de coleta de listagem (61 cidades × scroll), que é demorada e
estressa Auto Avaliar quando já temos os lotes cadastrados. Use depois de
DELETE AvaliacaoLote + LaudoCache pra forçar re-avaliação completa.

Uso:
    PYTHONPATH=. python scripts/reprocessar_lotes_do_db.py --empresa carros_uberlandia
    PYTHONPATH=. python scripts/reprocessar_lotes_do_db.py --empresa carros_uberlandia --max 5  # smoke
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from sqlmodel import Session, select

load_dotenv()
console = Console()
app = typer.Typer(add_completion=False)


@app.command()
def main(
    empresa: str = typer.Option("carros_uberlandia", help="ID da empresa"),
    max_lotes: Optional[int] = typer.Option(None, "--max", help="Limita a N lotes (smoke test)"),
    headless: bool = typer.Option(True, help="False = browser visível"),
    sem_sheets: bool = typer.Option(False, help="Pular exportação final"),
    somente_sem_avaliacao: bool = typer.Option(
        False, "--somente-sem-avaliacao",
        help="Filtra só lotes ainda não avaliados pra esta empresa (útil pra re-tentar erros).",
    ),
) -> None:
    """Reprocessa lotes já ingeridos sem re-scraping da listagem."""
    asyncio.run(_run(empresa, max_lotes, headless, sem_sheets, somente_sem_avaliacao))


async def _run(
    empresa_id: str,
    max_lotes: Optional[int],
    headless: bool,
    sem_sheets: bool,
    somente_sem_avaliacao: bool = False,
) -> None:
    from playwright.async_api import async_playwright

    from carros_sa.agents.vision_clients import build_default_client
    from carros_sa.db import get_session, init_db
    from carros_sa.models import AvaliacaoLote, Lote
    from carros_sa.orquestrador import _pipeline_lote
    from carros_sa.scraping.scraper_autoavaliar import garantir_autenticado
    from carros_sa.tenancy import carregar_empresa

    email = os.environ.get("AUTOAVALIAR_EMAIL")
    password = os.environ.get("AUTOAVALIAR_PASSWORD")
    if not email or not password:
        console.print("[red]AUTOAVALIAR_EMAIL/PASSWORD faltando no .env[/red]")
        raise typer.Exit(1)

    init_db()
    empresa = carregar_empresa(empresa_id)
    vision_client = build_default_client()
    console.print(f"[cyan]Vision:[/cyan] {type(vision_client).__name__}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="carros_sa_reproc_"))

    n_ok = n_erro = n_descartado = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()
        await garantir_autenticado(page, email, password)
        console.print("[green]✓ Sessão ativa[/green]")

        with get_session() as session:
            query = select(Lote)
            lotes = session.exec(query).all()
            if somente_sem_avaliacao:
                ja = {
                    r.lote_id for r in session.exec(
                        select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa.empresa_id)
                    ).all()
                }
                lotes = [l for l in lotes if l.id not in ja]
                console.print(f"[cyan]Filtro ativo: só lotes sem avaliação → {len(lotes)} candidatos[/cyan]")
            if max_lotes:
                lotes = lotes[:max_lotes]
            console.print(f"[cyan]Reprocessando {len(lotes)} lotes…[/cyan]\n")

            with Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Reprocessando", total=len(lotes))
                for lote in lotes:
                    try:
                        res = await _pipeline_lote(lote, page, vision_client, empresa, session, tmp_dir)
                        if res.erro:
                            n_erro += 1
                        elif res.motivo_descarte:
                            n_descartado += 1
                        else:
                            n_ok += 1
                    except Exception as exc:
                        console.print(f"  [red]exceção em {lote.id}: {exc}[/red]")
                        n_erro += 1
                    progress.advance(task)

        await browser.close()

    console.print(
        f"\n[bold green]✓ {n_ok} avaliados  [/bold green]"
        f"[yellow]{n_descartado} descartados  [/yellow]"
        f"[red]{n_erro} erros[/red]"
    )

    if sem_sheets:
        return

    sheet_id = os.environ.get("GOOGLE_SHEETS_ID")
    creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")
    if sheet_id and creds:
        from carros_sa.tools.sheets import SheetsExporter
        with get_session() as s:
            exporter = SheetsExporter(sheet_id, creds)
            n = exporter.exportar(empresa.empresa_id, s)
            console.print(f"[green]✓ {n} lotes exportados → Sheet[/green]")


if __name__ == "__main__":
    app()
