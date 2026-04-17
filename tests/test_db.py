"""Smoke tests pro bootstrap idempotente de `carros_sa.db`."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text
from sqlmodel import Session

from carros_sa.db import _aplicar_migracoes_leves, get_engine, init_db


def _colunas(db_path, tabela: str) -> set:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({tabela})").fetchall()
    finally:
        conn.close()
    return {r[1] for r in rows}


def _tabelas(db_path) -> set:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def test_init_db_cria_schema_completo(tmp_path):
    db = tmp_path / "novo.db"
    init_db(db)

    tabelas = _tabelas(db)
    assert "lote" in tabelas
    assert "avaliacao_lote" in tabelas
    assert "preco_referencia_aa" in tabelas  # Workstream K

    lote_cols = _colunas(db, "lote")
    assert "preco_referencia_aa" in lote_cols
    assert "fipe_pct_lance_minimo" in lote_cols

    aval_cols = _colunas(db, "avaliacao_lote")
    assert {"preco_giro_fipe", "preco_giro_aa", "fipe", "webmotors_mediana",
            "dias_giro_estimado"} <= aval_cols


def test_init_db_idempotente(tmp_path):
    db = tmp_path / "idemp.db"
    init_db(db)
    cols_antes = _colunas(db, "avaliacao_lote")
    init_db(db)
    cols_depois = _colunas(db, "avaliacao_lote")
    assert cols_antes == cols_depois


def test_migracao_backfill_preco_giro_fipe(tmp_path):
    """Simula DB legado (pré-K) com `preco_giro` mas sem `preco_giro_fipe`
    e valida que a migração faz backfill."""
    db = tmp_path / "legado.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE avaliacao_lote (
            id INTEGER PRIMARY KEY,
            preco_giro INTEGER NOT NULL
        );
        INSERT INTO avaliacao_lote (id, preco_giro) VALUES (1, 30000), (2, 45000);
    """)
    conn.commit()
    conn.close()

    engine = get_engine(db)
    _aplicar_migracoes_leves(engine)

    cols = _colunas(db, "avaliacao_lote")
    assert "preco_giro_fipe" in cols

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, preco_giro_fipe FROM avaliacao_lote ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows == [(1, 30000), (2, 45000)]
