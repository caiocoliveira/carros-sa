"""Migração idempotente: LaudoCache.tentativas_extracao.

Adiciona a coluna com default 0 nos registros existentes. SQLite só
suporta ADD COLUMN — `default 0` na cláusula DDL preenche os rows
antigos sem precisar UPDATE explícito.

Roda no início do cron diário (antes de `triagem_diaria.py`). Idempotente:
se a coluna já existe, sai zero sem mexer no DB.

Uso:
    PYTHONPATH=. python scripts/migrar_tentativas_extracao.py
    PYTHONPATH=. python scripts/migrar_tentativas_extracao.py --db /caminho/alternativo.db
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("carros_sa.db")


def _tem_coluna(cur: sqlite3.Cursor, tabela: str, coluna: str) -> bool:
    cur.execute(f"PRAGMA table_info({tabela})")
    return any(r[1] == coluna for r in cur.fetchall())


def migrar(db_path: Path = DB_PATH) -> dict:
    if not db_path.exists():
        # Sem DB ainda — primeira run do cron. `init_db` cria a tabela com a
        # coluna nova diretamente. Migração é no-op.
        return {"status": "no-op-sem-db", "coluna_adicionada": False}

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    if _tem_coluna(cur, "laudo", "tentativas_extracao"):
        conn.close()
        return {"status": "ja-migrado", "coluna_adicionada": False}

    cur.execute("ALTER TABLE laudo ADD COLUMN tentativas_extracao INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()
    return {"status": "migrado", "coluna_adicionada": True}


if __name__ == "__main__":
    args = sys.argv[1:]
    db = DB_PATH
    if "--db" in args:
        idx = args.index("--db")
        db = Path(args[idx + 1])
    resultado = migrar(db)
    print(resultado)
