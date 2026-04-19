#!/usr/bin/env python
"""Audita lotes ATIVOS sem laudo baixado e classifica o motivo.

Motivação: histórico repetido de lotes silenciosamente sem laudo (ex.:
21862502 Gol, 21865772 Cruze na triagem de Uberlândia 2026-04-14 — ambos
com `laudo_pdf_url=null` no cache porque o coletor não abriu o modal
"LAUDO DO VEÍCULO"). Sem auditoria, o operador só descobre ao tentar
clicar em "Ver laudo" na planilha e encontrar "—". Este script lista
preventivamente todos os gaps, agrupados por motivo, com exit code != 0
quando existem gaps recuperáveis (pra servir de gate em cron / CI).

Uso:
    PYTHONPATH=. python scripts/audit_laudos_faltantes.py --empresa carros_uberlandia
    PYTHONPATH=. python scripts/audit_laudos_faltantes.py --empresa carros_uberlandia --fix-hint
    PYTHONPATH=. python scripts/audit_laudos_faltantes.py --strict    # exit 1 se houver QUALQUER gap (até 'sem_laudo')

Exit codes:
    0  → zero lotes com gap recuperável (tudo OK pra exportar planilha)
    1  → existe ≥1 gap recuperável (url_nao_capturada, body_vazio, download_falhou)
         → ação: rodar `make triagem EMPRESA=<id>` pra re-scrape
    2  → `--strict` ligado E existem gaps de qualquer tipo (inclusive sem_laudo)
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from sqlmodel import select

console = Console()
app = typer.Typer(add_completion=False)

# Motivos que indicam bug nosso (dá pra consertar com re-scrape). Os demais
# ("sem_laudo") são ausência legítima declarada pelo anunciante — só viram erro
# quando --strict.
MOTIVOS_RECUPERAVEIS = {"url_nao_capturada", "body_vazio", "download_falhou"}


@app.command()
def main(
    empresa: Optional[str] = typer.Option(
        None, help="ID da empresa (filtra lotes avaliados por essa empresa)."
    ),
    strict: bool = typer.Option(
        False, help="Exit 2 quando existe QUALQUER gap — inclusive 'sem_laudo'."
    ),
    fix_hint: bool = typer.Option(
        False, help="Imprime comandos para resolver cada motivo detectado."
    ),
    incluir_encerrados: bool = typer.Option(
        False,
        help="Inclui lotes com fim_em no passado. Default: só lotes ativos "
        "(fim_em futuro), que são os que importam para a planilha.",
    ),
) -> None:
    """Lista lotes ativos sem laudo baixado + motivo estruturado."""
    from carros_sa.db import get_session, init_db
    from carros_sa.models import AvaliacaoLote, Lote
    from carros_sa.scraping.parsers import (
        DetalheFlags,
        detectar_motivo_laudo_ausente,
    )

    init_db()

    agora = datetime.now()

    with get_session() as session:
        query = select(Lote)
        if not incluir_encerrados:
            query = query.where(Lote.fim_em > agora)
        lotes = session.exec(query).all()

        # Se filtrou empresa, cruza com AvaliacaoLote pra pegar só lotes
        # que a empresa estaria vendo na planilha.
        if empresa:
            ids_empresa = set(
                session.exec(
                    select(AvaliacaoLote.lote_id).where(AvaliacaoLote.empresa_id == empresa)
                ).all()
            )
            lotes = [l for l in lotes if l.id in ids_empresa]

        gaps: list[dict] = []
        for lote in lotes:
            detalhe = (lote.raw_json or {}).get("detalhe") or {}
            laudo_url = detalhe.get("laudo_pdf_url")
            if laudo_url:
                continue  # tem URL → fora do escopo desta auditoria

            # Se o detalhe nem foi coletado ainda, classifica como body_vazio —
            # operador precisa rodar triagem; sem isso o lote é invisível pra
            # auditoria (ficava escondido atrás do "sem raw_json.detalhe").
            motivo = detalhe.get("laudo_missing_reason")
            if motivo is None and not detalhe:
                motivo = DetalheFlags.MISSING_BODY_VAZIO
            # Retrocompat: lotes ingeridos antes do campo `laudo_missing_reason`
            # existir, mas com `body_text` cacheado → classifica on-the-fly.
            if motivo is None:
                body_text = detalhe.get("body_text") or ""
                motivo = detectar_motivo_laudo_ausente(body_text)

            gaps.append({
                "lote_id": lote.id,
                "modelo": f"{lote.marca} {lote.modelo}"[:40],
                "ano": lote.ano,
                "cidade": lote.origem_cidade or "—",
                "motivo": motivo or "desconhecido",
                "url": lote.url,
            })

    # Relatório
    if not gaps:
        console.print("[green]✓ Nenhum lote ativo com laudo faltando.[/green]")
        raise typer.Exit(0)

    tbl = Table(title=f"{len(gaps)} lote(s) ativo(s) sem PDF de laudo")
    tbl.add_column("Lote")
    tbl.add_column("Modelo")
    tbl.add_column("Ano", justify="right")
    tbl.add_column("Cidade")
    tbl.add_column("Motivo", style="yellow")
    for g in gaps:
        tbl.add_row(
            g["lote_id"], g["modelo"], str(g["ano"]),
            g["cidade"], g["motivo"],
        )
    console.print(tbl)

    # Agrupa por motivo pra resumo
    por_motivo: dict[str, int] = {}
    for g in gaps:
        por_motivo[g["motivo"]] = por_motivo.get(g["motivo"], 0) + 1
    console.print("\n[bold]Resumo por motivo:[/bold]")
    for motivo, n in sorted(por_motivo.items(), key=lambda kv: -kv[1]):
        tag = "[red]" if motivo in MOTIVOS_RECUPERAVEIS else "[dim]"
        console.print(f"  {tag}{motivo}[/]: {n}")

    if fix_hint:
        console.print("\n[bold]Como resolver:[/bold]")
        for motivo in sorted(por_motivo):
            console.print(f"  [cyan]{motivo}[/cyan]: {_hint_por_motivo(motivo)}")

    recuperaveis = sum(por_motivo.get(m, 0) for m in MOTIVOS_RECUPERAVEIS)
    if recuperaveis > 0:
        console.print(
            f"\n[red]{recuperaveis} gap(s) recuperável(is)[/red] — rodar `make triagem` re-captura."
        )
        raise typer.Exit(1)
    if strict:
        console.print(f"\n[yellow]--strict: {len(gaps)} gap(s) no total[/yellow]")
        raise typer.Exit(2)
    console.print(
        "\n[green]Todos os gaps são 'sem_laudo' declarado pelo anunciante — ausência real, sem ação.[/green]"
    )


def _hint_por_motivo(motivo: str) -> str:
    hints = {
        "url_nao_capturada": (
            "botão 'LAUDO DO VEÍCULO' aparece no DOM mas scraper não extraiu URL. "
            "Modal lazy não abriu. Rodar `make triagem EMPRESA=<id>` — scraper ao "
            "vivo tenta click + re-extração 3x. Em últim caso, `make triagem-debug` "
            "com browser visível pra diagnosticar seletor quebrado."
        ),
        "body_vazio": (
            "innerText do detalhe nunca foi coletado. Rodar `make triagem "
            "EMPRESA=<id>` pra coletar o detalhe pela primeira vez."
        ),
        "download_falhou": (
            "URL capturada mas download do PDF falhou (provavelmente HTTP 429 "
            "do Auto Avaliar). Rodar `make triagem` de novo — o baixar_pdf tem "
            "retry com backoff 15s/30s/60s."
        ),
        "sem_laudo": (
            "anunciante declarou SEM LAUDO no próprio anúncio. NÃO é bug — ausência real. "
            "Se quiser esconder esses lotes, aumente severidade esperada na config da empresa."
        ),
    }
    return hints.get(motivo, "motivo desconhecido — investigar manualmente o lote no Auto Avaliar.")


if __name__ == "__main__":
    app()
