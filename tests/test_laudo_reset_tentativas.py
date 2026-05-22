"""Tests pra `scripts/laudo_reset_tentativas.py` — zera o contador de
tentativas_extracao em lotes com cache fraco, destravando o circuit-breaker.

Caso de uso primário (2026-05-22): após o fix de paridade text_llm_client no
retry, lotes que ficaram presos em `tentativas_extracao>=MAX` precisam ser
manualmente destravados pra próxima passagem do cron rodar DD4. Sem este
helper, operador rodaria SQL cru contra `state/db` em prod — II-FU4 do
ROADMAP.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine
from typer.testing import CliRunner

from carros_sa.models import LaudoCache

_scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))


def _engine_mem():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _laudo(lote_id: str, confidence: float, tentativas: int) -> LaudoCache:
    return LaudoCache(
        lote_id=lote_id,
        avarias_json=[],
        severidade_geral="nenhuma",
        motor_ok=True,
        documentacao="ok",
        categoria_veiculo="outro",
        confidence=confidence,
        modelo_llm="gemini-flash",
        custo_usd=0.0,
        extraido_em=datetime.utcnow(),
        tentativas_extracao=tentativas,
    )


@pytest.fixture
def engine_e_app(monkeypatch):
    """Engine in-memory + app do reset reusando get_session patched."""
    engine = _engine_mem()

    # O script chama `init_db()` + `get_session()`. Patcheamos ambos pra
    # apontar pro engine in-memory do teste.
    import carros_sa.db as db_mod

    monkeypatch.setattr(db_mod, "engine", engine, raising=False)
    monkeypatch.setattr(db_mod, "init_db", lambda: None)

    from contextlib import contextmanager

    @contextmanager
    def fake_session():
        with Session(engine) as s:
            yield s

    monkeypatch.setattr(db_mod, "get_session", fake_session)

    # Import depois do patch — script pega o get_session já patched.
    import importlib

    import laudo_reset_tentativas as mod

    importlib.reload(mod)
    return engine, mod.app


def test_dry_run_nao_modifica_db(engine_e_app):
    engine, app = engine_e_app
    with Session(engine) as s:
        s.add(_laudo("L_STUCK", confidence=0.0, tentativas=3))
        s.commit()

    runner = CliRunner()
    res = runner.invoke(app, [])  # default: dry-run
    assert res.exit_code == 0, res.output
    assert "Dry-run" in res.output or "dry-run" in res.output.lower()

    with Session(engine) as s:
        assert s.get(LaudoCache, "L_STUCK").tentativas_extracao == 3


def test_apply_zera_tentativas_de_lotes_com_cache_fraco(engine_e_app):
    engine, app = engine_e_app
    with Session(engine) as s:
        s.add(_laudo("L_STUCK_A", confidence=0.0, tentativas=3))
        s.add(_laudo("L_STUCK_B", confidence=0.55, tentativas=2))
        s.add(_laudo("L_FORTE", confidence=0.9, tentativas=0))  # forte, fica
        s.commit()

    runner = CliRunner()
    res = runner.invoke(app, ["--apply"])
    assert res.exit_code == 0, res.output
    assert "2 lote(s) zerados" in res.output

    with Session(engine) as s:
        assert s.get(LaudoCache, "L_STUCK_A").tentativas_extracao == 0
        assert s.get(LaudoCache, "L_STUCK_B").tentativas_extracao == 0
        # Forte permanece intocado (tentativas já era 0).
        assert s.get(LaudoCache, "L_FORTE").tentativas_extracao == 0


def test_idempotente_segunda_passagem_nada_a_fazer(engine_e_app):
    engine, app = engine_e_app
    with Session(engine) as s:
        s.add(_laudo("L_STUCK", confidence=0.0, tentativas=3))
        s.commit()

    runner = CliRunner()
    runner.invoke(app, ["--apply"])  # 1ª
    res = runner.invoke(app, ["--apply"])  # 2ª — nada a fazer
    assert res.exit_code == 0
    assert "Nenhum lote precisa de reset" in res.output


def test_apply_nao_toca_em_lote_com_cache_forte(engine_e_app):
    """Cache forte significa extração bem-sucedida — o operador deve zerar
    SÓ rows onde a extração falhou e queremos dar nova chance."""
    engine, app = engine_e_app
    with Session(engine) as s:
        # Hipotético: laudo forte com tentativas>0 (não deveria existir, mas
        # blindar — `_upsert_laudo_cache` zera em extração forte, mas migração
        # legacy pode deixar inconsistente).
        s.add(_laudo("L_FORTE_COM_TENTATIVAS", confidence=0.95, tentativas=5))
        s.commit()

    runner = CliRunner()
    res = runner.invoke(app, ["--apply"])
    assert res.exit_code == 0
    # Cache forte (>=0.6) NÃO entra no filtro default — fica intocado.
    with Session(engine) as s:
        assert (
            s.get(LaudoCache, "L_FORTE_COM_TENTATIVAS").tentativas_extracao == 5
        )


def test_lote_id_especifico_ignora_filtro_de_confidence(engine_e_app):
    """`--lote-id X` força reset mesmo em cache forte — operador escolheu."""
    engine, app = engine_e_app
    with Session(engine) as s:
        s.add(_laudo("L_FORTE_X", confidence=0.95, tentativas=2))
        s.commit()

    runner = CliRunner()
    res = runner.invoke(app, ["--lote-id", "L_FORTE_X", "--apply"])
    assert res.exit_code == 0
    assert "1 lote(s) zerados" in res.output
    with Session(engine) as s:
        assert s.get(LaudoCache, "L_FORTE_X").tentativas_extracao == 0


def test_lote_id_inexistente_falha_com_exit_1(engine_e_app):
    engine, app = engine_e_app
    runner = CliRunner()
    res = runner.invoke(app, ["--lote-id", "NAO_EXISTE", "--apply"])
    assert res.exit_code == 1
    assert "não tem LaudoCache" in res.output
