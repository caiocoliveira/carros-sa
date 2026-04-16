"""Ingestão: lê um JSON de listagem coletado via Chrome MCP e persiste no SQLite.

Uso:
    .venv/bin/python scripts/ingest_listagem.py data/scrapes/<arquivo>.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table
from sqlmodel import select

from carros_sa.db import get_session, init_db
from carros_sa.models import Lote
from carros_sa.scraping.parsers import parse_card_lines

console = Console()


def _parse_iso_utc(s: str) -> datetime:
    """'2026-04-14T22:00:00Z' → datetime naive UTC."""
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def main(path: str) -> None:
    data = json.loads(Path(path).read_text())
    lotes_raw = data["lotes_amostra"]
    # coletado_em é a referência histórica pro timer do card: reingerir sem
    # ele calcula fim_em relativo a "agora" e os lotes viram sempre futuros.
    agora = _parse_iso_utc(data["coletado_em"]) if "coletado_em" in data else None
    init_db()

    parsed = []
    falhas = []
    for entry in lotes_raw:
        try:
            lote = parse_card_lines(entry["lines"], entry["loteId"], entry["href"], agora=agora)
            parsed.append(lote)
        except Exception as e:  # pragma: no cover — log estratégico
            falhas.append({"loteId": entry["loteId"], "erro": str(e)})

    # Persistência (upsert manual — SQLite)
    with get_session() as session:
        for lote in parsed:
            existente = session.get(Lote, lote.lote_id)
            row = Lote(
                id=lote.lote_id,
                leilao=lote.leilao,
                url=lote.url,
                marca=lote.marca,
                modelo=lote.modelo,
                ano=lote.ano,
                km=lote.km,
                lance_atual=lote.lance_atual,
                fim_em=lote.fim_em,
                origem_cidade=lote.origem_cidade,
                origem_uf=lote.origem_uf,
                raw_json=lote.model_dump(mode="json"),
            )
            if existente:
                for k, v in row.model_dump(exclude={"id"}).items():
                    setattr(existente, k, v)
            else:
                session.add(row)
        session.commit()

    # Output: tabela do que foi ingerido
    tbl = Table(title=f"Lotes parseados ({len(parsed)}/{len(lotes_raw)} OK)")
    tbl.add_column("ID"); tbl.add_column("Cidade/UF"); tbl.add_column("Marca/Modelo")
    tbl.add_column("Ano", justify="right"); tbl.add_column("KM", justify="right")
    tbl.add_column("Lance", justify="right"); tbl.add_column("Fim em")
    for l in parsed:
        tbl.add_row(
            l.lote_id,
            f"{l.origem_cidade}/{l.origem_uf}",
            f"{l.marca} {l.modelo[:40]}",
            str(l.ano),
            f"{l.km:,}" if l.km else "-",
            f"R$ {l.lance_atual:,}",
            l.fim_em.strftime("%d/%m %H:%M") if l.fim_em else "-",
        )
    console.print(tbl)

    if falhas:
        console.print(f"[red]{len(falhas)} falha(s):[/red] {falhas}")

    # Confirma via SELECT
    with get_session() as session:
        total = len(session.exec(select(Lote)).all())
    console.print(f"[green]Total em `lote` no SQLite: {total}[/green]")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/scrapes/2026-04-14_uberlandia_listagem.json")
