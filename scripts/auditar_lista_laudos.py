#!/usr/bin/env python
"""Audita o invariante 'todo carro na planilha tem laudo baixado + revisado + linkado'.

Cruza `AvaliacaoLote` × `LaudoCache` × arquivo PDF persistente × URL do laudo,
agrupa as violações por causa-raiz e imprime um relatório acionável. Saída
exit-code != 0 quando há gaps — útil pra pipeline pós-cron e pra falhar
deploys quando o estado da planilha não bate com o invariante.

Uso:
    PYTHONPATH=. python scripts/auditar_lista_laudos.py
    PYTHONPATH=. python scripts/auditar_lista_laudos.py --empresa carros_uberlandia
    PYTHONPATH=. python scripts/auditar_lista_laudos.py --max-listar 30

Para auto-corrigir gaps cabíveis (decoy + retry de laudo pendente), o cron já
encadeia isso: `limpar_decoys` → `reprocessar_lotes_do_db --somente-laudo-pendente`.
Este script é o termômetro DEPOIS desses dois — o que sobrar aqui é gap real
que precisa de intervenção (ex.: scraper não acha modal do PDF).
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from carros_sa.db import get_session, init_db
from carros_sa.tools.lista_laudo_audit import (
    CausaGap,
    auditar_lista_laudos,
)

console = Console()
app = typer.Typer(add_completion=False)


_DICAS_POR_CAUSA = {
    CausaGap.URL_AUSENTE: (
        "Scraper não capturou laudo_pdf_url no DOM. Reproduzir manualmente: "
        "abrir lote no browser → ver se o modal 'LAUDO' carrega. Se sim, ajustar "
        "_EXTRACT_PDF_URL_JS pra esperar o lazy-load. Se não, o lote é 'SEM LAUDO' "
        "no AA e o gap é estrutural."
    ),
    CausaGap.URL_DECOY: (
        "URL persistida não passa em `is_laudo_pdf_url()`. Rodar `make limpar-decoys` "
        "(no cron) corrige automaticamente — se continua aparecendo, há padrão NOVO "
        "de decoy a adicionar na blocklist de scraping/parsers.py."
    ),
    CausaGap.PDF_NAO_BAIXADO: (
        "URL válida, mas data/laudos_pdfs/<lote>.pdf não existe. Pode ter sido "
        "rejeitado por `_pdf_eh_laudo_valido` (conteúdo não bate com 'laudo'). "
        "Rodar retry: `python scripts/reprocessar_lotes_do_db.py --somente-ativos "
        "--somente-laudo-pendente`."
    ),
    CausaGap.LAUDO_NAO_REVISADO: (
        "PDF baixado mas LaudoCache vazio/baixa-confidence. Vision client falhou "
        "(Gemini 503?) ou textual sem avarias. Garantir ANTHROPIC_API_KEY no .env "
        "pra ativar fallback Haiku, depois rodar retry."
    ),
}


@app.command()
def main(
    empresa: str = typer.Option("carros_uberlandia", help="ID da empresa"),
    max_listar: int = typer.Option(
        20, "--max-listar",
        help="Máximo de lotes pra listar individualmente (resto agregado por causa).",
    ),
    quiet: bool = typer.Option(
        False, "--quiet",
        help="Suprime saída quando tudo ok (silêncio = sucesso). Cron-friendly.",
    ),
) -> None:
    """Audita laudos da planilha; sai com código != 0 se há gaps."""
    init_db()
    with get_session() as session:
        result = auditar_lista_laudos(session, empresa)

    if result.total_na_planilha == 0:
        if not quiet:
            console.print(
                f"[yellow]Empresa '{empresa}' não tem AvaliacaoLote ativa — nada a auditar.[/yellow]"
            )
        return

    if result.total_gaps == 0:
        if not quiet:
            console.print(
                f"[bold green]✓ Lista limpa[/bold green] — "
                f"{result.completos}/{result.total_na_planilha} lotes da planilha "
                f"têm laudo baixado + revisado + linkado."
            )
        return

    # Header sumário
    console.print(
        f"\n[bold red]⚠ {result.total_gaps} gap(s) na planilha[/bold red] "
        f"({result.completos}/{result.total_na_planilha} completos)\n"
    )

    # Agrupa por causa pra dar o "big picture" antes do detalhe.
    por_causa = result.gaps_por_causa()
    tab = Table(title="Gaps por causa-raiz", show_header=True, header_style="bold cyan")
    tab.add_column("Causa")
    tab.add_column("N", justify="right")
    tab.add_column("Resolução")
    for causa_str, n in sorted(por_causa.items(), key=lambda kv: -kv[1]):
        causa = CausaGap(causa_str)
        tab.add_row(causa.value, str(n), _DICAS_POR_CAUSA[causa])
    console.print(tab)

    # Lista os primeiros N gaps individualmente. Resto fica implícito no agregado
    # (operador não precisa de 130 linhas, só dos primeiros pra debug).
    if max_listar > 0:
        console.print(f"\n[bold]Detalhe dos primeiros {min(max_listar, result.total_gaps)} lotes:[/bold]")
        for gap in result.gaps[:max_listar]:
            console.print(
                f"  [yellow]{gap.causa.value}[/yellow] · {gap.lote_id} "
                f"({gap.modelo}) — {gap.detalhe}"
            )
        if result.total_gaps > max_listar:
            console.print(f"  … e mais {result.total_gaps - max_listar} lote(s).")

    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
