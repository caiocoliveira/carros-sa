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


# Migrations adicionadas pelo Bloco C (dias_giro_estimado em avaliacao_lote).
# Registrar aqui novas colunas opcionais quando surgirem — substituir por Alembic
# quando o projeto sair de PoC.
_MIGRACOES_ADD_COLUMN = [
    ("avaliacao_lote", "dias_giro_estimado", "INTEGER"),
]


def _aplicar_migracoes_leves(engine) -> None:
    """Adiciona colunas novas em tabelas existentes se ainda não estiverem lá."""
    from sqlalchemy import text

    with engine.connect() as conn:
        for tabela, coluna, tipo in _MIGRACOES_ADD_COLUMN:
            existentes = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({tabela})")).all()
            }
            if coluna not in existentes:
                conn.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}"))
                conn.commit()


def get_session(db_path: Path | str | None = None) -> Session:
    return Session(get_engine(db_path))
