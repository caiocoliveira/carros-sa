#!/usr/bin/env python
"""Zera `LaudoCache.tentativas_extracao` em lotes com cache fraco.

Uso típico:
    PYTHONPATH=. python scripts/laudo_reset_tentativas.py             # dry-run
    PYTHONPATH=. python scripts/laudo_reset_tentativas.py --apply     # aplica
    PYTHONPATH=. python scripts/laudo_reset_tentativas.py --lote-id 22298499 --apply
    PYTHONPATH=. python scripts/laudo_reset_tentativas.py --max-conf 0.6 --apply

Quando rodar:
  - Depois de melhorar uma camada do extrator (ex.: DD4 entrou em main mas o
    retry não a estava chamando — circuit-breaker pode ter congelado lotes
    que agora extrairiam OK).
  - Quando `auditar_laudos --strict` mostra lotes em `cache_confianca_baixa`
    com PDF presente e URL OK, indicando que a falha foi no extrator (não no
    input). Operador re-roda extrator manualmente após resetar.
  - **Não chamar em loop**: o circuit-breaker existe pra parar de queimar LLM
    em lote com input ruim (PDF corrompido, body_text vazio). Resetar sempre
    anula a defesa.

Operação é idempotente: lotes onde `tentativas_extracao` já é 0 não mudam.
"""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from sqlmodel import select

from carros_sa.db import get_session, init_db
from carros_sa.models import LaudoCache

console = Console()
app = typer.Typer(add_completion=False)


@app.command()
def main(
    apply: bool = typer.Option(False, "--apply", help="Aplica o reset; sem isso só lista (dry-run)."),
    lote_id: Optional[str] = typer.Option(
        None, "--lote-id",
        help="Reseta um lote específico (ignora o filtro de confidence).",
    ),
    max_conf: float = typer.Option(
        0.6, "--max-conf",
        help="Só lotes com confidence < N entram no reset (default 0.6 = paridade com LAUDO_CONFIDENCE_MIN).",
    ),
) -> None:
    init_db()
    with get_session() as session:
        if lote_id:
            laudo = session.get(LaudoCache, lote_id)
            if laudo is None:
                console.print(f"[red]Lote {lote_id} não tem LaudoCache.[/red]")
                raise typer.Exit(1)
            candidatos = [laudo]
        else:
            candidatos = session.exec(
                select(LaudoCache).where(LaudoCache.confidence < max_conf)
            ).all()

        # Linhas que ainda vão mudar (tentativas_extracao > 0).
        alvos = [l for l in candidatos if (l.tentativas_extracao or 0) > 0]

        if not alvos:
            console.print(
                f"[green]Nenhum lote precisa de reset[/green] "
                f"(candidatos com confidence<{max_conf}: {len(candidatos)}, "
                f"todos já com tentativas=0)."
            )
            return

        tbl = Table(title=f"{'Aplicado' if apply else 'Dry-run'} — reset de tentativas_extracao")
        tbl.add_column("Lote")
        tbl.add_column("Confidence", justify="right")
        tbl.add_column("Tentativas (antes)", justify="right")
        for l in alvos:
            tbl.add_row(l.lote_id, f"{l.confidence:.2f}", str(l.tentativas_extracao))
        console.print(tbl)

        if not apply:
            console.print(
                f"\n[yellow]Dry-run.[/yellow] {len(alvos)} lote(s) seriam resetados. "
                "Use [code]--apply[/code] pra escrever no DB."
            )
            return

        for l in alvos:
            l.tentativas_extracao = 0
            session.add(l)
        session.commit()
        console.print(f"[green]✓ {len(alvos)} lote(s) zerados.[/green]")


if __name__ == "__main__":
    app()
