"""Itera os Lotes do SQLite e processa cada detalhe disponível em cache.

Cache esperado: `data/detalhes/<lote_id>.json` com schema:
    {
      "lote_id": str,
      "url": str,
      "laudo_pdf_url": Optional[str],
      "body_text": str
    }

Pra coletar o cache (one-off, manual via Chrome MCP): navegue até a URL do
lote, copie `document.body.innerText`, e salve no JSON acima.

Saída: tabela de status por lote + resumo passou vs. early_exit.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from sqlmodel import select

from carros_sa.db import get_session, init_db
from carros_sa.models import Lote
from carros_sa.scraping.scraper_detalhe import processar_detalhe

console = Console()

DETALHES_DIR = Path("data/detalhes")
PDF_DIR = Path("data/laudos_amostra")


def main() -> None:
    init_db()

    with get_session() as session:
        lotes = session.exec(select(Lote)).all()

        if not lotes:
            console.print("[yellow]Nenhum lote no SQLite. Rodar `make ingest` primeiro.[/yellow]")
            sys.exit(1)

        tbl = Table(title=f"Processamento de detalhes ({len(lotes)} lotes em SQLite)")
        tbl.add_column("Lote ID")
        tbl.add_column("Marca/Modelo")
        tbl.add_column("Status", justify="left")
        tbl.add_column("PDF", justify="center")

        passou = 0
        descartado = 0
        sem_cache = 0

        for lote in lotes:
            cache_path = DETALHES_DIR / f"{lote.id}.json"
            if not cache_path.exists():
                tbl.add_row(lote.id, f"{lote.marca} {lote.modelo[:30]}",
                            "[dim]sem_cache[/dim]", "-")
                sem_cache += 1
                continue

            cache = json.loads(cache_path.read_text())
            resultado = processar_detalhe(
                lote_id=lote.id,
                body_text=cache["body_text"],
                laudo_pdf_url=cache.get("laudo_pdf_url"),
                session=session,
                pdf_dir=PDF_DIR,
            )

            if resultado.passou:
                status = "[green]passou[/green]"
                passou += 1
            else:
                status = f"[red]early_exit:[/red] {resultado.early_exit}"
                descartado += 1

            pdf_marca = "✓" if resultado.pdf_baixado else "-"
            tbl.add_row(lote.id, f"{lote.marca} {lote.modelo[:30]}", status, pdf_marca)

        console.print(tbl)
        console.print()
        console.print(f"[green]Passaram:[/green] {passou}   "
                      f"[red]Descartados:[/red] {descartado}   "
                      f"[dim]Sem cache:[/dim] {sem_cache}")


if __name__ == "__main__":
    main()
