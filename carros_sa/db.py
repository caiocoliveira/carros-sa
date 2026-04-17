"""Engine SQLite + bootstrap de schema.

PoC usa SQLite local; migração explícita é `create_all` (idempotente).
Quando passar de 3 empresas ou 100k linhas, trocar por Postgres + Alembic.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

# Import pra registrar os models no metadata do SQLModel.
from carros_sa import models  # noqa: F401

DEFAULT_DB_PATH = Path(os.getenv("CARROS_SA_DB", "carros_sa.db"))


def get_engine(db_path: Path | str | None = None):
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    url = f"sqlite:///{path}"
    return create_engine(url, echo=False, connect_args={"check_same_thread": False})


def init_db(db_path: Path | str | None = None) -> None:
    """Cria todas as tabelas. Idempotente.

    Também aplica migrações leves de adição de coluna (SQLite create_all não
    altera tabelas existentes, então campos novos em SQLModels precisam de
    ALTER manual). Idempotente — só ALTERa quando a coluna não existe.
    """
    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)
    _aplicar_migracoes_leves(engine)


# Registrar aqui novas colunas opcionais quando surgirem — substituir por Alembic
# quando o projeto sair de PoC.
#
# Cobre:
#   - Bloco C: dias_giro_estimado
#   - Workstream K: preco_referencia_aa + fipe_pct_lance_minimo em lote,
#     preco_giro_fipe/_aa + fipe + webmotors_mediana em avaliacao_lote.
#     (Tabela preco_referencia_aa é criada por SQLModel.metadata.create_all.)
_MIGRACOES_ADD_COLUMN = [
    ("avaliacao_lote", "dias_giro_estimado", "INTEGER"),
    ("lote", "preco_referencia_aa", "INTEGER"),
    ("lote", "fipe_pct_lance_minimo", "INTEGER"),
    ("avaliacao_lote", "preco_giro_fipe", "INTEGER"),
    ("avaliacao_lote", "preco_giro_aa", "INTEGER"),
    ("avaliacao_lote", "fipe", "INTEGER"),
    ("avaliacao_lote", "webmotors_mediana", "INTEGER"),
]


def _aplicar_migracoes_leves(engine) -> None:
    """Adiciona colunas novas em tabelas existentes se ainda não estiverem lá.

    Backfill especial: `avaliacao_lote.preco_giro_fipe` herda de `preco_giro`
    (comportamento FIPE-only pré-workstream-K) quando a coluna é criada pela
    primeira vez.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        for tabela, coluna, tipo in _MIGRACOES_ADD_COLUMN:
            existentes = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({tabela})")).all()
            }
            if not existentes:
                continue  # tabela não existe (DB legado parcial); create_all a criará completa
            if coluna not in existentes:
                conn.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}"))
                if tabela == "avaliacao_lote" and coluna == "preco_giro_fipe":
                    if "preco_giro" in existentes:
                        conn.execute(text(
                            "UPDATE avaliacao_lote SET preco_giro_fipe = preco_giro "
                            "WHERE preco_giro_fipe IS NULL"
                        ))
        conn.commit()


def get_session(db_path: Path | str | None = None) -> Session:
    return Session(get_engine(db_path))
