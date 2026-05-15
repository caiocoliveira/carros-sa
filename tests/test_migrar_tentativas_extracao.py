"""Tests pra `scripts/migrar_tentativas_extracao.py` — ADD COLUMN
idempotente em SQLite."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Script não está no path como módulo
_scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from migrar_tentativas_extracao import migrar  # noqa: E402


def _criar_db_pre_migracao(db: Path):
    """Replica o schema antigo da tabela laudo (sem tentativas_extracao)."""
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE laudo (
            lote_id TEXT PRIMARY KEY,
            avarias_json JSON,
            severidade_geral TEXT,
            motor_ok INTEGER,
            documentacao TEXT,
            categoria_veiculo TEXT,
            confidence REAL,
            modelo_llm TEXT,
            custo_usd REAL DEFAULT 0.0,
            extraido_em TEXT
        )
        """
    )
    cur.execute(
        "INSERT INTO laudo (lote_id, avarias_json, severidade_geral, motor_ok, "
        "documentacao, categoria_veiculo, confidence, modelo_llm) VALUES "
        "(?, '[]', 'nenhuma', 1, 'ok', 'outro', ?, 'gemini-flash')",
        ("L_LEGADO", 0.3),
    )
    conn.commit()
    conn.close()


def test_migracao_adiciona_coluna_em_db_existente(tmp_path):
    db = tmp_path / "carros_sa.db"
    _criar_db_pre_migracao(db)

    resultado = migrar(db)

    assert resultado["coluna_adicionada"] is True
    assert resultado["status"] == "migrado"

    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(laudo)")
    colunas = {r[1]: (r[2], r[4]) for r in cur.fetchall()}  # nome → (tipo, default)
    assert "tentativas_extracao" in colunas
    assert colunas["tentativas_extracao"][0] == "INTEGER"

    # Row pré-existente recebe default 0
    cur.execute("SELECT tentativas_extracao FROM laudo WHERE lote_id = 'L_LEGADO'")
    assert cur.fetchone()[0] == 0
    conn.close()


def test_migracao_idempotente_em_db_ja_migrado(tmp_path):
    db = tmp_path / "carros_sa.db"
    _criar_db_pre_migracao(db)
    migrar(db)  # 1ª passagem

    resultado = migrar(db)  # 2ª passagem
    assert resultado["coluna_adicionada"] is False
    assert resultado["status"] == "ja-migrado"


def test_migracao_sem_db_e_noop(tmp_path):
    """Primeira run do cron pode não ter DB ainda — init_db cria com schema
    novo. Migração não falha, retorna no-op."""
    db = tmp_path / "nao_existe.db"
    resultado = migrar(db)
    assert resultado["status"] == "no-op-sem-db"
    assert resultado["coluna_adicionada"] is False
    assert not db.exists()
