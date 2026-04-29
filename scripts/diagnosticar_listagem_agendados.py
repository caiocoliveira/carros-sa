#!/usr/bin/env python
"""Diagnóstico: a listagem do Auto Avaliar expõe leilões agendados (não iniciados)?

Contexto: feedback do usuário (2026-04-23) — planilha só mostra leilões de hoje.
Fix imediato (PR #16) removeu o filtro de horizonte do scraper e deixou coleta
full-pipeline. Mas se a listagem default NÃO expõe lotes "Agendados"/"Em breve",
o fix sozinho não resolve. Esse script inspeciona a listagem ao vivo e reporta:

  1. total_resultados declarado no site × cards visíveis
  2. Cards com countdown timer (`HH:MM:SS` ou `N dias, HH:MM:SS`) × cards sem
     (possíveis agendados com formato "Início em DD/MM" ou similar)
  3. UI de filtros de status presentes no DOM (tabs/botões com texto tipo
     "agendado", "em andamento", "em breve", "futuro", "próximo")
  4. innerText das primeiras 3 linhas dos cards SEM timer — pra mapear o
     formato que o parser deve aprender

Não escreve no DB. Não probe URLs alternativas agressivamente.

Uso:
    PYTHONPATH=. python scripts/diagnosticar_listagem_agendados.py
    PYTHONPATH=. python scripts/diagnosticar_listagem_agendados.py --cidade uberlandia --uf mg
"""

from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import quote

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()
console = Console()
app = typer.Typer(add_completion=False)


_COLETA_DIAGNOSTICO_JS = """
() => {
    // Conta total_resultados declarado no header ("149 resultados" etc.)
    const bodyText = document.body.innerText || '';
    const m = bodyText.match(/(\\d{1,4})\\s+(resultados?|leil[ãa]es|lotes?|ve[íi]culos?)/i);
    const totalDeclarado = m ? parseInt(m[1], 10) : null;

    // Cards de lote (mesma seleção do _EXTRACT_CARDS_JS)
    const anchors = Array.from(document.querySelectorAll('a[href*="/avaliacoes/"]'));
    const vistosHref = new Set();
    const cards = [];
    for (const el of anchors) {
        const href = el.href || '';
        if (!href.match(/\\/avaliacoes\\/[^\\/]+\\/\\d+\\//) || vistosHref.has(href)) continue;
        vistosHref.add(href);
        // Container ancestral com info suficiente
        let container = el;
        for (let i = 0; i < 6; i++) {
            if (!container.parentElement) break;
            container = container.parentElement;
            const t = container.innerText || '';
            if (t.match(/\\d{1,3}\\.\\d{3},\\d{2}/) && t.match(/\\/[A-Z]{2}/)) break;
        }
        const lines = (container.innerText || '').split('\\n').map(l => l.trim()).filter(Boolean);
        cards.push({ href, lines });
    }

    // Filtros / tabs de status no DOM
    const palavrasDeStatus = ['agendad', 'em andamento', 'em breve', 'futur', 'próxim', 'proxim', 'iniciad', 'inicia em'];
    const filtrosVisiveis = [];
    const candidatos = Array.from(document.querySelectorAll('button, a, [role="tab"], [role="button"], label, option'));
    for (const el of candidatos) {
        const txt = (el.textContent || '').trim().toLowerCase();
        if (!txt || txt.length > 80) continue;
        if (palavrasDeStatus.some(p => txt.includes(p))) {
            filtrosVisiveis.push({
                tag: el.tagName,
                texto: el.textContent.trim(),
                href: el.getAttribute('href') || null,
            });
        }
    }

    return { totalDeclarado, cards, filtrosVisiveis };
}
"""


# Timer "ativo" do listing: HH:MM:SS[:cs] ou N dia[s][,] HH:MM:SS[:cs]
_TIMER_ATIVO_RE = re.compile(
    r"^(?:\d+\s*dias?[,\s]+\s*)?\d{1,3}:\d{2}:\d{2}(?::\d{2})?$"
)


@app.command()
def diagnosticar(
    cidade: str = typer.Option("uberlandia", help="cidade do raio pra inspecionar"),
    uf: str = typer.Option("mg", help="UF da cidade"),
    headless: bool = typer.Option(True, help="False = abre browser visível"),
) -> None:
    """Inspeciona a listagem do Auto Avaliar pra uma cidade e reporta sinais
    sobre leilões agendados (não iniciados)."""
    asyncio.run(_run(cidade, uf, headless))


async def _run(cidade: str, uf: str, headless: bool) -> None:
    from playwright.async_api import async_playwright
    from carros_sa.scraping.scraper_autoavaliar import (
        LISTAGEM_URL,
        garantir_autenticado,
    )

    email = os.environ.get("AUTOAVALIAR_EMAIL")
    password = os.environ.get("AUTOAVALIAR_PASSWORD")
    if not email or not password:
        console.print("[red]AUTOAVALIAR_EMAIL/PASSWORD não setados no .env[/red]")
        raise typer.Exit(1)

    url = (
        f"{LISTAGEM_URL}?location={uf.lower()}&cities={quote(cidade.lower())}"
        f"&report=yes&order=recforyou"
    )

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

        console.print("[cyan]Autenticando...[/cyan]")
        await garantir_autenticado(page, email, password)

        console.print(f"[cyan]GET {url}[/cyan]")
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

        diag = await page.evaluate(_COLETA_DIAGNOSTICO_JS)
        await browser.close()

    total_declarado = diag.get("totalDeclarado")
    cards = diag.get("cards") or []
    filtros = diag.get("filtrosVisiveis") or []

    # Classifica cards
    com_timer = []
    sem_timer = []
    for c in cards:
        lines = c.get("lines") or []
        tem = any(_TIMER_ATIVO_RE.match(l) for l in lines)
        (com_timer if tem else sem_timer).append(c)

    console.print()
    console.print(f"[bold]Total declarado no DOM:[/bold] {total_declarado}")
    console.print(f"[bold]Cards visíveis:[/bold] {len(cards)} "
                  f"([green]{len(com_timer)} com countdown[/green], "
                  f"[yellow]{len(sem_timer)} sem[/yellow])")
    console.print()

    if filtros:
        console.print("[bold green]Filtros/tabs de status detectados no DOM:[/bold green]")
        t = Table(show_header=True)
        t.add_column("Tag")
        t.add_column("Texto")
        t.add_column("Href")
        for f in filtros[:20]:
            t.add_row(f.get("tag") or "?", f.get("texto") or "", f.get("href") or "—")
        console.print(t)
        console.print()
    else:
        console.print("[dim]Nenhum elemento com texto de 'agendado/em breve/futuro' encontrado no DOM.[/dim]")
        console.print()

    if sem_timer:
        console.print(f"[bold yellow]Amostra de cards SEM countdown (primeiros 5):[/bold yellow]")
        console.print("[dim](se forem lotes agendados, o formato dessas linhas é o que o parser precisa aprender)[/dim]")
        for i, c in enumerate(sem_timer[:5]):
            console.print(f"\n  [cyan]Card {i + 1}[/cyan] · {c.get('href', '')[:80]}")
            for ln in (c.get("lines") or [])[:12]:
                console.print(f"    {ln!r}")
    else:
        console.print("[dim]Todos os cards visíveis têm countdown — nenhum candidato a 'agendado' nessa URL.[/dim]")

    # Conclusão sugerida
    console.print()
    console.print("[bold]Próxima ação sugerida:[/bold]")
    if filtros:
        console.print("  → UI tem filtros de status; descobrir URL/param que dá pra combinar")
        console.print("    (ex.: clicar no filtro e copiar URL resultante).")
    elif sem_timer:
        console.print("  → Tem cards sem countdown na listagem; mapear o formato das linhas")
        console.print("    e estender `parsers._timer_para_fim_em` ou adicionar `inicia_em` no LoteRaw.")
    elif total_declarado and total_declarado > len(cards):
        console.print("  → Site declara mais lotes do que renderiza em p=1 — conferir paginação"
                      " em ?p=N (hoje o scraper já itera até 50 páginas).")
    else:
        console.print("  → Listagem já expõe só leilões ativos; leilões agendados provavelmente"
                      " ficam em URL/rota separada. Pedir pro usuário enviar URL que ele"
                      " usa no browser pra ver os futuros.")


if __name__ == "__main__":
    app()
