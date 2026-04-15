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
    """Cria todas as tabelas. Idempotente."""
    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)


def get_session(db_path: Path | str | None = None) -> Session:
    return Session(get_engine(db_path))
