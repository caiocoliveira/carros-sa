#!/usr/bin/env python
"""Exporta triagem de lotes do SQLite para uma aba no Google Sheets.

Uso:
    PYTHONPATH=. .venv/bin/python scripts/exportar_sheets.py --empresa uberlandia_mg
    make sheets EMPRESA=uberlandia_mg

Setup one-time (ver carros_sa/tools/sheets.py para instruções completas):
    1. Criar Service Account no Google Cloud e baixar JSON para fora do repo
    2. Criar uma Google Sheet e copiar o ID da URL
    3. Compartilhar a Sheet com o e-mail do service account (Editor)
    4. Setar no .env: GOOGLE_SHEETS_ID e GOOGLE_SERVICE_ACCOUNT_PATH
"""

import os
import sys

import typer
from dotenv import load_dotenv

load_dotenv()

app = typer.Typer(add_completion=False)


@app.command()
def exportar(
    empresa: str = typer.Option(..., help="ID da empresa (ex: uberlandia_mg)"),
    sheet_id: str = typer.Option(
        None, help="ID do Google Sheets (override do GOOGLE_SHEETS_ID no .env)"
    ),
    credentials: str = typer.Option(
        None,
        help="Caminho pro JSON da service account (override do GOOGLE_SERVICE_ACCOUNT_PATH no .env)",
    ),
) -> None:
    """Exporta as avaliações da empresa para uma aba no Google Sheets."""
    # Resolve credenciais
    sheet_id = sheet_id or os.environ.get("GOOGLE_SHEETS_ID")
    credentials = credentials or os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")

    if not sheet_id:
        typer.echo(
            "Erro: defina GOOGLE_SHEETS_ID no .env ou passe --sheet-id",
            err=True,
        )
        raise typer.Exit(1)

    if not credentials:
        typer.echo(
            "Erro: defina GOOGLE_SERVICE_ACCOUNT_PATH no .env ou passe --credentials",
            err=True,
        )
        raise typer.Exit(1)

    if not os.path.exists(credentials):
        typer.echo(f"Erro: arquivo de credenciais não encontrado: {credentials}", err=True)
        raise typer.Exit(1)

    from carros_sa.db import get_session, init_db
    from carros_sa.tools.sheets import SheetsExporter

    init_db()

    exporter = SheetsExporter(spreadsheet_id=sheet_id, credentials_path=credentials)

    with get_session() as session:
        try:
            n = exporter.exportar(empresa_id=empresa, session=session)
        except Exception as exc:
            typer.echo(f"Erro ao exportar para o Sheets: {exc}", err=True)
            raise typer.Exit(1)

    if n == 0:
        typer.echo(
            f"Aviso: nenhuma avaliação encontrada para empresa '{empresa}'. "
            "Rode o Orquestrador antes de exportar.",
            err=True,
        )
    else:
        typer.echo(f"✓ {n} lotes exportados → aba \"{empresa}\"")

    typer.echo(f"  Sheet: {exporter.sheet_url}")


if __name__ == "__main__":
    app()
