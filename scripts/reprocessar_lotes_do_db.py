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
    somente_ativos: bool = typer.Option(
        False, "--somente-ativos",
        help="Filtra só lotes com leilão ainda aberto (fim_em no futuro). Evita gastar tempo/LLM em lotes já encerrados.",
    ),
    somente_laudo_pendente: bool = typer.Option(
        False, "--somente-laudo-pendente",
        help=(
            "Filtra só lotes cujo LaudoCache está vazio OU veio de fallback "
            "(confidence<0.6 = `_laudo_sem_pdf`). Ideal pra rodar após a triagem "
            "diária e tentar destravar lotes em que o scraper não achou o PDF."
        ),
    ),
    max_tentativas: int = typer.Option(
        1, "--max-tentativas",
        help=(
            "Número máximo de iterações do loop de reconciliação. Cada iteração "
            "re-consulta os pendentes (`somente_laudo_pendente` shrinka conforme "
            "lotes ganham confidence>=0.6) e tenta destravar de novo. Com "
            "fornecedor instável (modal lazy AA, Gemini 503 transitivo, 429) "
            "uma 2ª/3ª passada captura o que a 1ª perdeu, sem custo de re-login. "
            "Loop encerra cedo quando não há mais pendentes."
        ),
    ),
) -> None:
    """Reprocessa lotes já ingeridos sem re-scraping da listagem."""
    asyncio.run(_run(empresa, max_lotes, headless, sem_sheets, somente_sem_avaliacao,
                     somente_ativos, somente_laudo_pendente, max_tentativas))


async def _run(
    empresa_id: str,
    max_lotes: Optional[int],
    headless: bool,
    sem_sheets: bool,
    somente_sem_avaliacao: bool = False,
    somente_ativos: bool = False,
    somente_laudo_pendente: bool = False,
    max_tentativas: int = 1,
) -> None:
    from playwright.async_api import async_playwright

    from carros_sa.agents.vision_clients import build_default_client
    from carros_sa.db import get_session, init_db
    from carros_sa.orquestrador import _pipeline_lote
    from carros_sa.scraping.scraper_autoavaliar import garantir_autenticado
    from carros_sa.tenancy import carregar_empresa
    from carros_sa.tools.laudo_reconciliacao import selecionar_pendentes

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
            for tentativa in range(1, max_tentativas + 1):
                lotes = selecionar_pendentes(
                    session,
                    empresa_id=empresa.empresa_id,
                    somente_sem_avaliacao=somente_sem_avaliacao,
                    somente_ativos=somente_ativos,
                    somente_laudo_pendente=somente_laudo_pendente,
                    max_lotes=max_lotes,
                )

                if not lotes:
                    if tentativa > 1:
                        console.print(
                            f"[green]Iteração {tentativa}: sem pendentes → encerrando "
                            f"loop antes de max_tentativas={max_tentativas}[/green]"
                        )
                    break

                if max_tentativas > 1:
                    console.print(
                        f"\n[bold cyan]Iteração {tentativa}/{max_tentativas} — "
                        f"{len(lotes)} candidatos[/bold cyan]"
                    )
                else:
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
