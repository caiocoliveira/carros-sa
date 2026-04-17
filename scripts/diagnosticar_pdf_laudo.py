#!/usr/bin/env python
"""Diagnóstico: valida que o seletor JS do PDF está pegando laudos de carro reais.

Pega N lotes do DB, re-abre cada URL com Playwright, extrai pdf_url via
`_EXTRACT_PDF_URL_JS` (versão atual) e tenta baixar + validar.

Uso:
    PYTHONPATH=. python scripts/diagnosticar_pdf_laudo.py --n 3

Sem flags destrutivos: não toca em LaudoCache nem AvaliacaoLote. Só baixa PDFs
pra data/laudos_pdfs/ e relata o resultado numa tabela rich.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from sqlmodel import Session, select

load_dotenv()
console = Console()
app = typer.Typer(add_completion=False)


async def _diagnosticar(n: int) -> None:
    from carros_sa.db import get_session
    from carros_sa.models import Lote
    from carros_sa.orquestrador import _pdf_eh_laudo_valido, _pdf_persistente_path
    from carros_sa.scraping.scraper_autoavaliar import (
        _EXTRACT_PDF_URL_JS,
        baixar_pdf,
        coletar_detalhe,
        garantir_autenticado,
    )
    from playwright.async_api import async_playwright

    from datetime import datetime

    with get_session() as session:
        # Só lotes com leilão ainda ativo — os antigos dão 404/"Oh!..."
        lotes = session.exec(
            select(Lote).where(Lote.fim_em > datetime.now()).limit(n)
        ).all()
        lotes_info = [(l.id, l.url, f"{l.marca} {l.modelo} {l.ano}") for l in lotes]

    console.print(f"[cyan]Diagnosticando {len(lotes_info)} lotes...[/cyan]\n")

    resultados = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Tenta restaurar cookies de sessão ou logar
        email = os.environ.get("AUTOAVALIAR_EMAIL", "")
        password = os.environ.get("AUTOAVALIAR_PASSWORD", "")
        try:
            await garantir_autenticado(page, email, password)
        except Exception as e:
            console.print(f"[yellow]garantir_autenticado falhou ({e}) — seguindo sem login[/yellow]")

        for lote_id, url, modelo in lotes_info:
            try:
                body_text, pdf_url = await coletar_detalhe(page, url)
            except Exception as e:
                resultados.append((lote_id, modelo, None, False, f"coletar_detalhe: {e}"))
                continue

            # Diagnóstico — onde a página parou?
            url_atual = page.url
            title = await page.title()
            console.print(f"  [dim]{lote_id}: {url_atual[:80]} | título: {title[:40]}[/dim]")

            if not pdf_url:
                # Debug: dumpa TODOS os PDFs encontrados na página pra investigar
                todos_pdfs = await page.evaluate("""
                    () => {
                        const out = new Set();
                        document.querySelectorAll('a[href], iframe[src], embed[src], object[data]').forEach(el => {
                            const h = el.href || el.src || el.data || '';
                            if (h.toLowerCase().includes('.pdf')) out.add(h);
                        });
                        // Também procura no HTML cru
                        const html = document.documentElement.outerHTML;
                        const re = /https?:\\/\\/[^\\s"'<>]+?\\.pdf/gi;
                        for (const m of (html.match(re) || [])) out.add(m);
                        return Array.from(out);
                    }
                """)
                resultados.append((lote_id, modelo, None, False, f"pdf_url=None | todos PDFs: {todos_pdfs[:3]}"))
                continue

            pdf_dest = _pdf_persistente_path(lote_id)
            try:
                cookies = await context.cookies()
                await baixar_pdf(pdf_url, pdf_dest, cookies)
            except Exception as e:
                resultados.append((lote_id, modelo, pdf_url, False, f"baixar: {e}"))
                continue

            valido = _pdf_eh_laudo_valido(pdf_dest)
            nota = "laudo ok" if valido else "FALSO POSITIVO (denylist)"
            resultados.append((lote_id, modelo, pdf_url, valido, nota))

        await browser.close()

    # Relatório
    tbl = Table(title="Diagnóstico PDF laudo")
    tbl.add_column("Lote")
    tbl.add_column("Modelo")
    tbl.add_column("URL", overflow="fold", max_width=50)
    tbl.add_column("Válido?")
    tbl.add_column("Nota")
    for lote_id, modelo, url, valido, nota in resultados:
        cor = "green" if valido else "red"
        tbl.add_row(
            lote_id,
            modelo,
            (url or "—")[:60],
            f"[{cor}]{'✓' if valido else '✗'}[/{cor}]",
            nota,
        )
    console.print(tbl)

    ok = sum(1 for r in resultados if r[3])
    console.print(f"\n[bold]{ok}/{len(resultados)} lotes com PDF laudo válido baixado.[/bold]")


@app.command()
def main(n: int = typer.Option(3, help="Quantidade de lotes a diagnosticar")) -> None:
    asyncio.run(_diagnosticar(n))


if __name__ == "__main__":
    app()
