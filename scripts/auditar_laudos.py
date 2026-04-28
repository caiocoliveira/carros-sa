#!/usr/bin/env python
"""CLI da auditoria de laudos.

Roda `auditar_laudos.auditar(session, empresa_id)`, imprime relatório no
stderr (sai do caminho do `tee` em logs) e sai com código != 0 se houver
qualquer lote ativo com laudo incompleto.

Uso:
    PYTHONPATH=. python scripts/auditar_laudos.py --empresa carros_uberlandia
    PYTHONPATH=. python scripts/auditar_laudos.py --empresa carros_uberlandia --auto-heal
    PYTHONPATH=. python scripts/auditar_laudos.py --empresa carros_uberlandia --formato=json

Quando `--auto-heal` é passado, o script tenta re-extrair laudos de PDFs
locais válidos (offline — sem rede). Casos que precisam re-scrape (URL
ausente, PDF não baixado) seguem para o cron de retry.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

import typer
from dotenv import load_dotenv

from carros_sa.db import get_session, init_db
from carros_sa.tools.auditor_laudos import (
    MotivoLaudoFaltante,
    auditar,
    auto_heal_local,
    render_relatorio,
)

load_dotenv()
app = typer.Typer(add_completion=False)


@app.command()
def main(
    empresa: str = typer.Option("carros_uberlandia", help="ID da empresa"),
    auto_heal: bool = typer.Option(
        False, "--auto-heal",
        help="Re-extrai laudo de PDFs locais válidos onde confidence < 0.6.",
    ),
    formato: str = typer.Option(
        "texto", "--formato", help="texto | json",
    ),
    fail_se_problemas: bool = typer.Option(
        True, "--fail-se-problemas/--no-fail",
        help="Sai com código 1 quando houver lote ativo sem laudo completo.",
    ),
) -> None:
    init_db()

    with get_session() as session:
        resultado = auditar(session, empresa)
        heal_resumo: Optional[dict] = None
        if auto_heal:
            heal = auto_heal_local(session, resultado)
            heal_resumo = {
                "re_extraidos": heal.re_extraidos,
                "falhas": heal.falhas,
            }
            # Re-roda auditoria pra refletir o estado pós-heal.
            resultado = auditar(session, empresa)

    if formato == "json":
        payload = {
            "empresa_id": resultado.empresa_id,
            "total_lotes_ativos": resultado.total_lotes_ativos,
            "lotes_ok": resultado.lotes_ok,
            "total_problemas": resultado.total_problemas,
            "por_motivo": {
                m.value: [s.lote_id for s in resultado.por_motivo.get(m, [])]
                for m in MotivoLaudoFaltante
                if resultado.por_motivo.get(m)
            },
            "heal": heal_resumo,
        }
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        # Texto vai pra stderr — não polui pipes/redirects do consumidor
        # que esperam só o JSON estruturado, mas continua visível no terminal/log.
        print(render_relatorio(resultado), file=sys.stderr)
        if heal_resumo is not None:
            print(
                f"\n  Auto-heal: {len(heal_resumo['re_extraidos'])} re-extraído(s), "
                f"{len(heal_resumo['falhas'])} falha(s)",
                file=sys.stderr,
            )

    if fail_se_problemas and resultado.total_problemas > 0:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
