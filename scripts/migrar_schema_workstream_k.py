"""Migração idempotente do schema pro workstream K.

Roda uma vez após o merge de K. SQLite não suporta ALTER COLUMN, só ADD COLUMN,
então a estratégia é: adicionar as colunas novas como nullable, backfillar
`preco_giro_fipe` com o valor de `preco_giro` existente (compatibilidade com
o cálculo FIPE-only anterior), e criar a tabela `preco_referencia_aa`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("carros_sa.db")


def _tem_coluna(cur: sqlite3.Cursor, tabela: str, coluna: str) -> bool:
    cur.execute(f"PRAGMA table_info({tabela})")
    return any(r[1] == coluna for r in cur.fetchall())


def _tem_tabela(cur: sqlite3.Cursor, nome: str) -> bool:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (nome,))
    return cur.fetchone() is not None


def migrar(db_path: Path = DB_PATH) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"Banco não encontrado em {db_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    mudancas = {"lote": [], "avaliacao_lote": [], "preco_referencia_aa": []}

    # Lote: 2 colunas novas
    if not _tem_coluna(cur, "lote", "preco_referencia_aa"):
        cur.execute("ALTER TABLE lote ADD COLUMN preco_referencia_aa INTEGER")
        mudancas["lote"].append("+preco_referencia_aa")
    if not _tem_coluna(cur, "lote", "fipe_pct_lance_minimo"):
        cur.execute("ALTER TABLE lote ADD COLUMN fipe_pct_lance_minimo INTEGER")
        mudancas["lote"].append("+fipe_pct_lance_minimo")

    # AvaliacaoLote: 4 colunas novas + backfill
    if not _tem_coluna(cur, "avaliacao_lote", "preco_giro_fipe"):
        cur.execute("ALTER TABLE avaliacao_lote ADD COLUMN preco_giro_fipe INTEGER")
        cur.execute("UPDATE avaliacao_lote SET preco_giro_fipe = preco_giro")
        mudancas["avaliacao_lote"].append("+preco_giro_fipe (backfilled=preco_giro)")
    if not _tem_coluna(cur, "avaliacao_lote", "preco_giro_aa"):
        cur.execute("ALTER TABLE avaliacao_lote ADD COLUMN preco_giro_aa INTEGER")
        mudancas["avaliacao_lote"].append("+preco_giro_aa (null pros existentes)")
    if not _tem_coluna(cur, "avaliacao_lote", "fipe"):
        cur.execute("ALTER TABLE avaliacao_lote ADD COLUMN fipe INTEGER")
        mudancas["avaliacao_lote"].append("+fipe (null pros existentes)")
    if not _tem_coluna(cur, "avaliacao_lote", "webmotors_mediana"):
        cur.execute("ALTER TABLE avaliacao_lote ADD COLUMN webmotors_mediana INTEGER")
        mudancas["avaliacao_lote"].append("+webmotors_mediana (null pros existentes)")

    # Nova tabela PrecoReferenciaAA
    if not _tem_tabela(cur, "preco_referencia_aa"):
        cur.execute("""
            CREATE TABLE preco_referencia_aa (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marca VARCHAR NOT NULL,
                modelo VARCHAR NOT NULL,
                ano INTEGER NOT NULL,
                versao VARCHAR,
                preco INTEGER NOT NULL,
                fipe_pct_lance_minimo INTEGER,
                origem_lote_id VARCHAR,
                coletado_em DATETIME NOT NULL
            )
        """)
        cur.execute("CREATE INDEX ix_preco_referencia_aa_marca ON preco_referencia_aa(marca)")
        cur.execute("CREATE INDEX ix_preco_referencia_aa_modelo ON preco_referencia_aa(modelo)")
        cur.execute("CREATE INDEX ix_preco_referencia_aa_ano ON preco_referencia_aa(ano)")
        cur.execute("CREATE INDEX ix_preco_referencia_aa_coletado_em ON preco_referencia_aa(coletado_em)")
        mudancas["preco_referencia_aa"].append("created")

    conn.commit()
    conn.close()
    return mudancas


if __name__ == "__main__":
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else DB_PATH
    resultado = migrar(db)
    total = sum(len(v) for v in resultado.values())
    if total == 0:
        print(f"Nada a fazer: schema de {db} já está na versão K.")
    else:
        print(f"Migração de {db}:")
        for tabela, ops in resultado.items():
            if ops:
                print(f"  {tabela}: {', '.join(ops)}")
