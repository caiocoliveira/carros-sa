"""Testes do CLI unificado (carros-sa).

Sem rede, sem Playwright, sem LLM. Usa DB SQLite temporário por teste.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine
from typer.testing import CliRunner

import carros_sa.db as db_module
from carros_sa.cli import app
from carros_sa.models import AvaliacaoLote, Lote

runner = CliRunner()


@pytest.fixture
def db_tmp(tmp_path, monkeypatch):
    """DB SQLite em arquivo temporário, isolado por teste."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", db_path)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return db_path


def _seed_avaliacoes(db_path: Path, empresa: str = "carros_uberlandia", n: int = 3, prefix: str = "LOT"):
    """Insere N lotes + avaliações com ROI decrescente. Usa prefix para evitar colisão de IDs entre empresas."""
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    from sqlmodel import Session

    with Session(engine) as session:
        for i in range(n):
            lote = Lote(
                id=f"{prefix}{i:03d}",
                leilao="Uberlândia",
                url=f"https://example/lot/{i}",
                marca="Fiat",
                modelo=f"Uno {i}",
                ano=2015 + i,
                km=50000 + i * 1000,
                lance_atual=20000 + i * 1000,
                fim_em=datetime(2026, 4, 20, 14, 0),
                origem_cidade="Uberlândia",
                origem_uf="MG",
                raw_json={},
            )
            av = AvaliacaoLote(
                empresa_id=empresa,
                lote_id=lote.id,
                preco_alvo=25000 + i * 500,
                preco_max=27000,
                score_roi=0.30 - i * 0.05,  # ROI decrescente: 0.30, 0.25, 0.20
                fator_risco=1.0,
                fator_liquidez=1.0,
                margem_aplicada=0.20,
                frete_incluso=500,
                reforma_estimada=1000,
                taxas_leilao=500,
                preco_giro=26000,
                preco_giro_fipe=26000,
                preco_giro_aa=None,
                justificativa="teste",
                criado_em=datetime.utcnow(),
            )
            session.add(lote)
            session.add(av)
        session.commit()


# ---------------------------------------------------------------------------
# Ajuda geral — `carros-sa --help` lista os subcomandos principais
# ---------------------------------------------------------------------------

def test_help_lista_subcomandos():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for sub in ("triagem", "top", "ingest", "extrair-laudo", "sheets", "empresas"):
        assert sub in result.stdout


# ---------------------------------------------------------------------------
# top — caso feliz + caso vazio
# ---------------------------------------------------------------------------

def test_top_sem_dados_emite_aviso(db_tmp):
    result = runner.invoke(app, ["top", "--empresa", "carros_uberlandia"])
    assert result.exit_code == 0
    assert "Nenhuma avaliação" in result.stdout


def test_top_ranqueia_por_roi_desc(db_tmp):
    _seed_avaliacoes(db_tmp, empresa="carros_uberlandia", n=3)
    result = runner.invoke(app, ["top", "--empresa", "carros_uberlandia", "--n", "10"])
    assert result.exit_code == 0
    # Primeiro lote (LOT000) tem ROI 30%, segundo 25%, terceiro 20% — ordem descendente
    idx_lot0 = result.stdout.index("LOT000")
    idx_lot1 = result.stdout.index("LOT001")
    idx_lot2 = result.stdout.index("LOT002")
    assert idx_lot0 < idx_lot1 < idx_lot2


def test_top_respeita_limite_n(db_tmp):
    _seed_avaliacoes(db_tmp, empresa="carros_uberlandia", n=3)
    result = runner.invoke(app, ["top", "--empresa", "carros_uberlandia", "--n", "2"])
    assert result.exit_code == 0
    assert "LOT000" in result.stdout
    assert "LOT001" in result.stdout
    assert "LOT002" not in result.stdout


def test_top_filtra_por_empresa(db_tmp):
    _seed_avaliacoes(db_tmp, empresa="carros_uberlandia", n=2, prefix="UBE")
    _seed_avaliacoes(db_tmp, empresa="outra_empresa", n=2, prefix="OUT")
    result = runner.invoke(app, ["top", "--empresa", "outra_empresa"])
    assert result.exit_code == 0
    assert "outra_empresa" in result.stdout
    assert "OUT000" in result.stdout
    assert "UBE000" not in result.stdout


# ---------------------------------------------------------------------------
# empresas — lista configs do diretório
# ---------------------------------------------------------------------------

def test_empresas_lista_configs():
    result = runner.invoke(app, ["empresas"])
    assert result.exit_code == 0
    assert "carros_uberlandia" in result.stdout
    # sanity: não deve ter erro de atributo em nenhuma linha
    assert "erro" not in result.stdout.lower()


# ---------------------------------------------------------------------------
# triagem — falha cedo sem credenciais
# ---------------------------------------------------------------------------

def test_triagem_sem_credenciais_falha(monkeypatch):
    monkeypatch.delenv("AUTOAVALIAR_EMAIL", raising=False)
    monkeypatch.delenv("AUTOAVALIAR_PASSWORD", raising=False)
    result = runner.invoke(app, ["triagem", "--empresa", "carros_uberlandia"])
    assert result.exit_code == 1
    assert "AUTOAVALIAR_EMAIL" in result.stdout


# ---------------------------------------------------------------------------
# sheets — valida env vars antes de falar com gspread
# ---------------------------------------------------------------------------

def test_sheets_sem_credenciais_falha(monkeypatch):
    monkeypatch.delenv("GOOGLE_SHEETS_ID", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_PATH", raising=False)
    result = runner.invoke(app, ["sheets", "--empresa", "carros_uberlandia"])
    assert result.exit_code == 1
    assert "GOOGLE_SHEETS_ID" in result.stdout


def test_sheets_credencial_inexistente_falha(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_SHEETS_ID", "abc123")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_PATH", str(tmp_path / "fake.json"))
    result = runner.invoke(app, ["sheets", "--empresa", "carros_uberlandia"])
    assert result.exit_code == 1
    assert "não encontrado" in result.stdout


# ---------------------------------------------------------------------------
# ingest — arquivo inexistente
# ---------------------------------------------------------------------------

def test_ingest_arquivo_inexistente_falha(db_tmp, tmp_path):
    result = runner.invoke(app, ["ingest", str(tmp_path / "nao_existe.json")])
    assert result.exit_code == 1
    assert "não encontrado" in result.stdout


def test_ingest_lote_real_persiste(db_tmp):
    """Ingest com o JSON real de Uberlândia (10 lotes)."""
    fixture = Path("data/scrapes/2026-04-14_uberlandia_listagem.json")
    if not fixture.exists():
        pytest.skip("fixture de listagem não disponível")
    result = runner.invoke(app, ["ingest", str(fixture)])
    assert result.exit_code == 0
    assert "10" in result.stdout  # 10 lotes parseados
