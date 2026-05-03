#!/usr/bin/env python
"""Sobe PDFs de laudo já validados pro Google Drive e popula `laudo_drive_url`.

Quando ativar o Drive (workstream U+) num DB que já tem PDFs persistidos em
`data/laudos_pdfs/`, os lotes legados ficam no estado "PDF salvo (link
expirado)" porque o `_pipeline_lote` só sobe pro Drive durante a triagem ao
vivo. Este script atravessa esses PDFs uma única vez, sobe pra pasta do
Drive e atualiza `raw_json.detalhe.laudo_drive_url` — instantaneamente, todo
lote completo passa a mostrar HYPERLINK permanente na planilha.

Idempotente: usa `LaudoDriveClient.upload`, que procura `<lote>.pdf` na
pasta antes de subir — chamadas repetidas são no-op.

Filtra pra só subir lotes com `LaudoCache.confidence >= 0.6` (laudo de
verdade) — não desperdiça quota subindo PDFs duvidosos. Lotes ATIVOS têm
prioridade (sobe primeiro).

Uso:
    PYTHONPATH=. python scripts/backfill_laudos_drive.py
    PYTHONPATH=. python scripts/backfill_laudos_drive.py --empresa carros_uberlandia
    PYTHONPATH=. python scripts/backfill_laudos_drive.py --dry-run
    PYTHONPATH=. python scripts/backfill_laudos_drive.py --limite 50

Vars necessárias (mesmas da triagem):
    GOOGLE_DRIVE_LAUDOS_FOLDER_ID
    GOOGLE_SERVICE_ACCOUNT_PATH
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from sqlmodel import Session, select

from carros_sa.db import get_session, init_db
from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.laudo_audit import PDF_DIR_DEFAULT
from carros_sa.tools.laudo_drive import (
    DRIVE_FILE_ID_KEY,
    DRIVE_URL_KEY,
    LaudoDriveClient,
    build_default_drive_client,
)

console = Console()
app = typer.Typer(add_completion=False)


def _persistir_drive_link(
    lote: Lote, file_id: str, web_view_link: str, session: Session,
) -> None:
    """Mesmo helper do orquestrador, repetido aqui pra evitar import circular."""
    raw = dict(lote.raw_json or {})
    det = dict(raw.get("detalhe") or {})
    det[DRIVE_FILE_ID_KEY] = file_id
    det[DRIVE_URL_KEY] = web_view_link
    raw["detalhe"] = det
    lote.raw_json = raw
    session.add(lote)


def backfill(
    session: Session,
    drive_client: LaudoDriveClient,
    *,
    empresa_id: Optional[str] = None,
    pdf_dir: Path = PDF_DIR_DEFAULT,
    apenas_ativos_primeiro: bool = True,
    limite: Optional[int] = None,
    dry_run: bool = False,
) -> dict:
    """Itera lotes elegíveis e sobe PDFs ausentes do Drive.

    Lote elegível = tem PDF local + LaudoCache forte + ainda não tem Drive URL.
    Quando `empresa_id` é setado, filtra pra lotes avaliados por aquela empresa
    (evita gastar quota com lotes que não vão pra planilha de ninguém).
    """
    # Indexa LaudoCache pra evitar N+1.
    laudos = {l.lote_id: l for l in session.exec(select(LaudoCache)).all()}

    if empresa_id:
        ids_avaliados = {
            r.lote_id for r in session.exec(
                select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa_id)
            ).all()
        }
        lotes = [l for l in session.exec(select(Lote)).all() if l.id in ids_avaliados]
    else:
        lotes = list(session.exec(select(Lote)).all())

    candidatos = []
    agora = datetime.now()
    for lote in lotes:
        # Filtro 1: PDF local existe e é grande o bastante (mesmos critérios
        # do laudo_audit, copiados pra não importar circular).
        pdf_path = pdf_dir / f"{lote.id}.pdf"
        if not pdf_path.exists() or pdf_path.stat().st_size < 5_000:
            continue
        # Filtro 2: LaudoCache forte (não desperdiça quota com fallback _laudo_sem_pdf).
        laudo = laudos.get(lote.id)
        if laudo is None or (laudo.confidence or 0) < 0.6:
            continue
        # Filtro 3: ainda não tem Drive URL.
        det = (lote.raw_json or {}).get("detalhe") or {}
        if det.get(DRIVE_URL_KEY):
            continue
        ativo = lote.fim_em is not None and lote.fim_em > agora
        candidatos.append((lote, pdf_path, ativo))

    # Sobe ativos primeiro pra "dar visibilidade rápida" na planilha viva.
    if apenas_ativos_primeiro:
        candidatos.sort(key=lambda t: 0 if t[2] else 1)
    if limite is not None:
        candidatos = candidatos[:limite]

    res = {"candidatos": len(candidatos), "subidos": 0, "ja_existia": 0, "erros": 0}

    for lote, pdf_path, _ativo in candidatos:
        if dry_run:
            console.print(f"  [dim]would upload[/dim] {lote.id}.pdf "
                          f"({pdf_path.stat().st_size//1024} KB)")
            continue
        try:
            up = drive_client.upload(lote.id, pdf_path)
            _persistir_drive_link(lote, up.file_id, up.web_view_link, session)
            session.commit()
            if up.criado_agora:
                res["subidos"] += 1
                console.print(f"  [green]✓[/green] {lote.id} → {up.web_view_link}")
            else:
                res["ja_existia"] += 1
                console.print(
                    f"  [cyan]~[/cyan] {lote.id} já existia no Drive, link gravado"
                )
        except Exception as exc:
            res["erros"] += 1
            session.rollback()
            console.print(
                f"  [red]✗[/red] {lote.id}: {type(exc).__name__}: {exc}"
            )

    return res


@app.command()
def main(
    empresa: Optional[str] = typer.Option(
        None, help="Filtra por empresa (sobe só PDFs de lotes avaliados pra essa empresa)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    limite: Optional[int] = typer.Option(
        None, help="Sobe no máximo N PDFs (útil pra teste / quota)"
    ),
) -> None:
    drive_client = build_default_drive_client()
    if drive_client is None:
        console.print(
            "[red]GOOGLE_DRIVE_LAUDOS_FOLDER_ID + GOOGLE_SERVICE_ACCOUNT_PATH "
            "precisam estar setados no .env.[/red]"
        )
        raise typer.Exit(1)

    init_db()
    with get_session() as session:
        res = backfill(
            session,
            drive_client,
            empresa_id=empresa,
            limite=limite,
            dry_run=dry_run,
        )

    console.print(
        f"\n[bold]Backfill {'(dry-run) ' if dry_run else ''}terminado:[/bold]"
    )
    console.print(f"  Candidatos:   {res['candidatos']}")
    console.print(f"  Subidos:      {res['subidos']}")
    console.print(f"  Já existiam:  {res['ja_existia']}")
    if res["erros"]:
        console.print(f"  [red]Erros:        {res['erros']}[/red]")


if __name__ == "__main__":
    app()
