#!/usr/bin/env python
"""Sobe PDFs locais (data/laudos_pdfs/) pro Google Drive e persiste o webViewLink.

Backfill de URL permanente pra lotes que tinham PDF baixado mas não tinham
`laudo_drive_url` em `raw_json["detalhe"]`. Idempotente:
  - lotes já com Drive URL persistida → skip imediato (sem chamada de API)
  - lotes com PDF local mas sem URL → upload (uploader internamente busca por
    nome no Drive antes de criar duplicata)

Pré-requisitos no .env:
  GOOGLE_DRIVE_FOLDER_ID         — ID da pasta no Drive (extraído da URL)
  GOOGLE_SERVICE_ACCOUNT_PATH    — JSON da service account (mesma do Sheets)

Uso:
  PYTHONPATH=. python scripts/sync_laudos_drive.py
  PYTHONPATH=. python scripts/sync_laudos_drive.py --empresa carros_uberlandia
  PYTHONPATH=. python scripts/sync_laudos_drive.py --dry-run
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from sqlmodel import select

from carros_sa.db import get_session, init_db
from carros_sa.models import AvaliacaoLote, Lote
from carros_sa.tools.drive_uploader import build_default_uploader
from carros_sa.tools.laudo_audit import PDF_DIR_DEFAULT

console = Console()
app = typer.Typer(add_completion=False)


@app.command()
def main(
    empresa: Optional[str] = typer.Option(
        None,
        help="Filtra pelos lotes avaliados de uma empresa específica. Default: TODOS.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Imprime ações sem subir."),
    pdf_dir: Path = typer.Option(
        PDF_DIR_DEFAULT,
        help=f"Diretório de PDFs persistidos (default: {PDF_DIR_DEFAULT}).",
    ),
) -> None:
    load_dotenv()
    uploader = build_default_uploader()
    if uploader is None and not dry_run:
        console.print(
            "[red]Erro:[/red] GOOGLE_DRIVE_FOLDER_ID e GOOGLE_SERVICE_ACCOUNT_PATH "
            "precisam estar setados no .env. Use --dry-run pra ver o que seria feito."
        )
        raise typer.Exit(1)

    init_db()
    subiram = pulados_ja_no_drive = pulados_sem_pdf = falhas = 0
    with get_session() as session:
        # Filtra por empresa quando solicitado — espelha o filtro do exporter.
        if empresa:
            avs = session.exec(
                select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa)
            ).all()
            lote_ids = {av.lote_id for av in avs}
            lotes = [session.get(Lote, lid) for lid in lote_ids]
            lotes = [l for l in lotes if l is not None]
        else:
            lotes = session.exec(select(Lote)).all()

        for lote in lotes:
            detalhe = (lote.raw_json or {}).get("detalhe") or {}

            if detalhe.get("laudo_drive_url"):
                pulados_ja_no_drive += 1
                continue

            pdf_path = pdf_dir / f"{lote.id}.pdf"
            if not pdf_path.exists() or pdf_path.stat().st_size < 5_000:
                pulados_sem_pdf += 1
                continue

            if dry_run:
                console.print(f"[dim]would upload[/dim] {pdf_path.name}")
                subiram += 1
                continue

            try:
                web_link = uploader.upload_pdf(pdf_path, file_name=f"{lote.id}.pdf")
            except Exception as exc:
                console.print(f"[red]falha[/red] {lote.id}: {exc}")
                falhas += 1
                continue

            raw_atual = dict(lote.raw_json or {})
            det_atual = dict(raw_atual.get("detalhe") or {})
            det_atual["laudo_drive_url"] = web_link
            raw_atual["detalhe"] = det_atual
            lote.raw_json = raw_atual
            session.add(lote)
            session.commit()
            subiram += 1
            console.print(f"[green]✓[/green] {lote.id} → {web_link}")

    console.print(
        f"\n[bold]Resumo:[/bold] subiram={subiram} "
        f"já_no_drive={pulados_ja_no_drive} "
        f"sem_pdf={pulados_sem_pdf} "
        f"falhas={falhas}"
    )


if __name__ == "__main__":
    app()
