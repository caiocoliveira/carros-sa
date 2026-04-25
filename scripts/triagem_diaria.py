#!/usr/bin/env python
"""Pipeline diário: scraping → avaliação → Google Sheets.

Uso:
    PYTHONPATH=. .venv/bin/python scripts/triagem_diaria.py --empresa carros_uberlandia
    make triagem
    make triagem-debug   # abre browser visível

Variáveis de ambiente necessárias (.env):
    AUTOAVALIAR_EMAIL          — e-mail de login
    AUTOAVALIAR_PASSWORD       — senha (NUNCA cole no chat)
    GEMINI_API_KEY             — chave da API Gemini (vision)
    GOOGLE_SHEETS_ID           — ID da Sheet de output
    GOOGLE_SERVICE_ACCOUNT_PATH — caminho do JSON da service account
"""

from __future__ import annotations

import asyncio
import os
import sys

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()
console = Console()
app = typer.Typer(add_completion=False)


@app.command()
def main(
    empresa: str = typer.Option("carros_uberlandia", help="ID da empresa"),
    horizonte_dias: int = typer.Option(7, help="Lotes com encerramento nos próximos N dias"),
    headless: bool = typer.Option(True, help="False = abre browser visível (debug)"),
    sem_sheets: bool = typer.Option(False, help="Pular exportação para Sheets"),
) -> None:
    """Roda pipeline completo: scraping → avaliação → Google Sheets."""
    asyncio.run(_run(empresa, horizonte_dias, headless, sem_sheets))


async def _run(empresa_id: str, horizonte_dias: int, headless: bool, sem_sheets: bool) -> None:
    # --- Validação de ambiente ---
    email = os.environ.get("AUTOAVALIAR_EMAIL")
    password = os.environ.get("AUTOAVALIAR_PASSWORD")
    if not email or not password:
        console.print("[red]Erro: AUTOAVALIAR_EMAIL e AUTOAVALIAR_PASSWORD devem estar no .env[/red]")
        raise typer.Exit(1)

    # --- Imports pesados aqui para não atrasar --help ---
    from playwright.async_api import async_playwright

    from carros_sa.agents.vision_clients import build_default_client
    from carros_sa.agents.text_llm_clients import build_default_text_client
    from carros_sa.db import get_session, init_db
    from carros_sa.orquestrador import orquestrar
    from carros_sa.scraping.scraper_autoavaliar import garantir_autenticado

    init_db()

    vision_client = build_default_client()
    console.print(f"[cyan]Vision provider:[/cyan] {type(vision_client).__name__}")

    try:
        text_llm_client = build_default_text_client()
        console.print(f"[cyan]Reforma LLM:[/cyan] {type(text_llm_client).__name__}")
    except RuntimeError:
        text_llm_client = None
        console.print("[yellow]Reforma LLM: desabilitado → usando tabela determinística[/yellow]")

    # --- Playwright ---
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        # --- Login / restaurar sessão ---
        console.print(f"\n[bold]Autenticando no Auto Avaliar...[/bold]")
        try:
            await garantir_autenticado(page, email, password)
            console.print("[green]✓ Sessão ativa[/green]")
        except Exception as exc:
            console.print(f"[red]Erro de autenticação: {exc}[/red]")
            await browser.close()
            raise typer.Exit(1)

        # --- Orquestrador ---
        console.print(f"\n[bold]Coletando leilões ({empresa_id}, horizonte {horizonte_dias} dias)...[/bold]")
        with get_session() as session:
            result = await orquestrar(
                empresa_id=empresa_id,
                session=session,
                page=page,
                vision_client=vision_client,
                horizonte_dias=horizonte_dias,
                text_llm_client=text_llm_client,
            )

        await browser.close()

    # --- Output ---
    console.print(f"\n[green]✓ {result.n_coletados} lotes coletados "
                  f"({result.n_novos} novos)[/green]")
    console.print(f"[green]✓ {result.n_avaliados} avaliados | "
                  f"{result.n_descartados} descartados | "
                  f"{result.n_erros} erros[/green]")

    if result.lotes:
        tbl = Table(title="Resultados")
        tbl.add_column("Lote ID")
        tbl.add_column("Modelo")
        tbl.add_column("Status")
        tbl.add_column("Preço-Alvo", justify="right")
        tbl.add_column("ROI%", justify="right")
        for r in sorted(result.lotes, key=lambda x: (x.roi_pct or 0), reverse=True):
            if r.erro:
                status = f"[red]ERRO: {r.erro[:40]}[/red]"
            elif r.motivo_descarte:
                status = f"[yellow]descartado: {r.motivo_descarte}[/yellow]"
            elif r.preco_alvo:
                status = "[green]avaliado[/green]"
            else:
                status = "[dim]já avaliado[/dim]"
            tbl.add_row(
                r.lote_id,
                r.modelo[:35],
                status,
                f"R$ {r.preco_alvo:,}" if r.preco_alvo else "—",
                f"{r.roi_pct:.1f}%" if r.roi_pct is not None else "—",
            )
        console.print(tbl)

    # --- Google Sheets ---
    if sem_sheets:
        console.print("\n[dim]--sem-sheets: exportação pulada[/dim]")
        return

    sheet_id = os.environ.get("GOOGLE_SHEETS_ID")
    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")
    if not sheet_id or not creds_path:
        console.print("\n[yellow]Aviso: GOOGLE_SHEETS_ID ou GOOGLE_SERVICE_ACCOUNT_PATH "
                      "não configurados — exportação para Sheets pulada.[/yellow]")
        return

    from carros_sa.db import get_session
    from carros_sa.tools.sheets import SheetsExporter

    console.print("\n[bold]Exportando para Google Sheets...[/bold]")
    try:
        exporter = SheetsExporter(spreadsheet_id=sheet_id, credentials_path=creds_path)
        with get_session() as session:
            n = exporter.exportar(empresa_id=empresa_id, session=session)
        console.print(f"[green]✓ {n} lotes exportados[/green]")
        console.print(f"  Sheet: {exporter.sheet_url}")
    except Exception as exc:
        console.print(f"[red]Erro ao exportar para Sheets: {exc}[/red]")

    # Auditoria de laudos pós-export — chama o classificador como módulo (não
    # subprocess) pra herdar o init_db e a sessão. Resumo no console; exit-code
    # da função não propaga aqui, mas a contagem de pendentes apareça pro cron.
    _auditar_laudos_pos_run(empresa_id)


def _auditar_laudos_pos_run(empresa_id: str) -> None:
    """Imprime resumo de estados de laudo dos lotes ativos da empresa.

    Não falha o triagem_diaria — só sinaliza pendências pro operador. Auditoria
    "que falha" fica em `scripts/auditar_laudos.py` (cron pode chamar separado
    e usar exit-code pra alertar).
    """
    from sqlmodel import Session, select

    from carros_sa.db import get_engine
    from carros_sa.models import AvaliacaoLote, LaudoCache, Lote

    from datetime import datetime as _dt
    from scripts.auditar_laudos import classificar_lote, _ESTADOS_NAO_ACIONAVEIS

    agora = _dt.now()
    contagem: dict = {}
    with Session(get_engine()) as session:
        ids_empresa = session.exec(
            select(AvaliacaoLote.lote_id).where(AvaliacaoLote.empresa_id == empresa_id)
        ).all()
        if not ids_empresa:
            return
        lotes = session.exec(
            select(Lote)
            .where(Lote.fim_em > agora)
            .where(Lote.id.in_(ids_empresa))  # type: ignore[attr-defined]
        ).all()
        for lote in lotes:
            laudo = session.get(LaudoCache, lote.id)
            estado = classificar_lote(lote, laudo)
            contagem[estado] = contagem.get(estado, 0) + 1

    if not contagem:
        return
    pendentes = sum(n for e, n in contagem.items() if e not in _ESTADOS_NAO_ACIONAVEIS)
    ok = contagem.get("OK", 0)
    sem_laudo = contagem.get("sem_laudo_declarado", 0)
    console.print(
        f"\n[bold]Auditoria de laudos:[/bold] "
        f"[green]{ok} OK[/green] · "
        f"{sem_laudo} SEM LAUDO declarado · "
        f"[yellow]{pendentes} pendente(s)[/yellow]"
    )
    if pendentes > 0:
        detalhes = ", ".join(
            f"{e}={n}" for e, n in contagem.items() if e not in _ESTADOS_NAO_ACIONAVEIS
        )
        console.print(
            f"  [yellow]Rode `python scripts/auditar_laudos.py --empresa {empresa_id}` "
            f"para investigar[/yellow] ({detalhes})"
        )


if __name__ == "__main__":
    app()
