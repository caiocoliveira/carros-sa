#!/usr/bin/env python
"""Auditoria de laudos — todo lote ativo precisa ter laudo OK ou motivo claro.

Roda no fim de toda triagem (chamado em `triagem_diaria.py`) e também pode ser
executado manualmente. Para cada lote ATIVO no DB (fim_em > now), classifica
o estado do laudo em:

    OK                     — PDF baixado E LaudoCache.confidence ≥ 0.6
    sem_laudo_declarado    — Auto Avaliar marcou "SEM LAUDO"; estado final
    url_nao_capturada      — modal lazy do detalhe não revelou URL → retry resolve
    download_falhou        — URL existia mas baixar_pdf deu 429/timeout → retry
    pdf_invalido           — baixou mas é decoy (ex: Transparência Salarial)
    extracao_falhou        — PDF ok mas LaudoCache veio com confidence < 0.6
    legado                 — lote antes do guardrail (raw_json sem motivo)

Saída:
  - Tabela rich com a contagem por estado
  - Lista até 5 exemplos de cada estado pendente (acionáveis)
  - Exit code != 0 quando há lotes em estado pendente (não 'sem_laudo_declarado'
    nem 'OK'). Cron deve falhar pra alertar.

Uso:
    PYTHONPATH=. python scripts/auditar_laudos.py
    PYTHONPATH=. python scripts/auditar_laudos.py --empresa carros_uberlandia
    PYTHONPATH=. python scripts/auditar_laudos.py --so-resumo  # só agregado, sem exemplos
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()
console = Console()
app = typer.Typer(add_completion=False)


# Estados que NÃO devem disparar exit-code não-zero — ou estão OK ou são finais.
_ESTADOS_NAO_ACIONAVEIS = {"OK", "sem_laudo_declarado"}


def classificar_lote(lote, laudo_cache) -> str:
    """Retorna a classificação de estado do laudo de um Lote."""
    detalhe = (lote.raw_json or {}).get("detalhe") or {}
    motivo = detalhe.get("motivo_sem_laudo")
    pdf_path = detalhe.get("pdf_path_local")

    # Lotes ingeridos antes do guardrail não têm motivo gravado. Distingue dos
    # legítimos OK olhando se LaudoCache existe com confiança boa — quando sim
    # tratamos como OK, senão é "legado" (precisa reprocessar pra ganhar motivo).
    if motivo is None and pdf_path is None:
        if laudo_cache and (laudo_cache.confidence or 0) >= 0.6:
            return "OK"
        return "legado"

    if motivo is not None:
        return motivo

    # pdf_path != None mas extração saiu fraca — força reclassificação
    if laudo_cache is None or (laudo_cache.confidence or 0) < 0.6:
        return "extracao_falhou"
    return "OK"


@app.command()
def main(
    empresa: Optional[str] = typer.Option(
        None,
        help="Filtra por empresa (avaliações). Quando omitido, audita todos os lotes ativos.",
    ),
    so_resumo: bool = typer.Option(False, "--so-resumo", help="Não imprime exemplos."),
    incluir_inativos: bool = typer.Option(
        False,
        "--incluir-inativos",
        help="Inclui lotes com fim_em < now (default: só ativos).",
    ),
) -> None:
    """Audita estado dos laudos. Exit code != 0 quando há pendências acionáveis."""
    from sqlmodel import Session, select

    from carros_sa.db import get_engine
    from carros_sa.models import AvaliacaoLote, LaudoCache, Lote

    agora = datetime.now()
    contagem: dict = {}
    exemplos: dict = {}

    with Session(get_engine()) as session:
        query = select(Lote)
        if not incluir_inativos:
            query = query.where(Lote.fim_em > agora)
        if empresa:
            # Filtra por lotes que tenham AvaliacaoLote da empresa — espelha
            # o que vai pra planilha daquela empresa.
            ids_empresa = session.exec(
                select(AvaliacaoLote.lote_id).where(AvaliacaoLote.empresa_id == empresa)
            ).all()
            if not ids_empresa:
                console.print(
                    f"[yellow]Nenhuma avaliação encontrada para empresa '{empresa}'. "
                    "Verifique o ID ou rode triagem antes.[/yellow]"
                )
                raise typer.Exit(0)
            query = query.where(Lote.id.in_(ids_empresa))  # type: ignore[attr-defined]

        lotes = session.exec(query).all()
        total = len(lotes)
        for lote in lotes:
            laudo = session.get(LaudoCache, lote.id)
            estado = classificar_lote(lote, laudo)
            contagem[estado] = contagem.get(estado, 0) + 1
            exemplos.setdefault(estado, []).append(
                (lote.id, f"{lote.marca} {lote.modelo} {lote.ano}".strip(), lote.url)
            )

    if total == 0:
        console.print("[yellow]Nenhum lote ativo no DB — nada para auditar.[/yellow]")
        return

    # Tabela resumo
    tbl = Table(title=f"Auditoria de laudos — {total} lote(s) ativo(s)")
    tbl.add_column("Estado")
    tbl.add_column("Lotes", justify="right")
    tbl.add_column("% do total", justify="right")
    # Ordena por gravidade: OK e sem_laudo_declarado primeiro (não acionáveis), pendentes depois.
    ordem = ["OK", "sem_laudo_declarado", "url_nao_capturada", "download_falhou",
             "pdf_invalido", "extracao_falhou", "legado"]
    estados_presentes = [e for e in ordem if e in contagem] + [
        e for e in contagem if e not in ordem
    ]
    for estado in estados_presentes:
        n = contagem[estado]
        pct = n * 100 / total
        cor = "green" if estado in _ESTADOS_NAO_ACIONAVEIS else "yellow"
        tbl.add_row(f"[{cor}]{estado}[/{cor}]", str(n), f"{pct:.1f}%")
    console.print(tbl)

    # Exemplos por estado pendente
    pendentes = sum(n for e, n in contagem.items() if e not in _ESTADOS_NAO_ACIONAVEIS)
    if pendentes == 0:
        console.print("\n[green]✓ Sem pendências — todos os lotes ativos têm "
                      "laudo OK ou motivo final.[/green]")
        return

    if not so_resumo:
        console.print(f"\n[bold yellow]{pendentes} lote(s) em estado pendente:[/bold yellow]")
        for estado in estados_presentes:
            if estado in _ESTADOS_NAO_ACIONAVEIS:
                continue
            console.print(f"\n[yellow]· {estado}[/yellow]")
            for lote_id, modelo, url in exemplos[estado][:5]:
                console.print(f"    {lote_id} — {modelo[:40]} — {url}")
            if len(exemplos[estado]) > 5:
                console.print(f"    [dim]... +{len(exemplos[estado]) - 5} outros[/dim]")

    console.print(
        f"\n[bold red]✗ Auditoria FALHOU — {pendentes} lote(s) precisam de retry. "
        "Rode `make triagem` ou `python scripts/reprocessar_laudos.py` pra resolver.[/bold red]"
    )
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
