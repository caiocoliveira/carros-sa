"""Testes do subcomando `carros-sa registrar-compra`.

Cobre os casos do plano HH-3:
  1. Caminho com todas as flags → CSV + DB corretos
  2. Caminho interativo (prompt para campo obrigatório ausente)
  3. Idempotência — re-rodar mesmo (marca, modelo, ano, valor) atualiza ao invés de duplicar
  4. Formato decomposto: buckets separados persistidos corretamente
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from typer.testing import CliRunner

import carros_sa.db as db_module
from carros_sa.cli import app
from carros_sa.models import Arrematado, Lote

runner = CliRunner()


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def db_tmp(tmp_path, monkeypatch):
    """DB SQLite temporário + patch do DEFAULT_DB_PATH."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", db_path)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return db_path


@pytest.fixture
def csv_tmp(tmp_path) -> Path:
    """Caminho para CSV temporário (vazio — ainda não existe)."""
    return tmp_path / "test_arrematado.csv"


@pytest.fixture
def csv_com_uma_linha(csv_tmp) -> Path:
    """CSV com 1 linha pre-existente (legacy, 10 colunas) + header 16 colunas."""
    header = (
        "marca,modelo,ano,km,valor_compra,data_compra,custos_extras,"
        "valor_venda,data_venda,taxa_leilao_real,frete_real,transferencia_real,"
        "higienizacao_real,outros_extras_real,gastos_reforma_real,observacoes\n"
    )
    linha = "Honda,Civic EXL,2018,60000,42000,2026-01-10,3500,,,,,,,,,pre-existente\n"
    csv_tmp.write_text(header + linha, encoding="utf-8")
    return csv_tmp


# =============================================================================
# 1. Caminho com todas as flags
# =============================================================================

def test_todas_as_flags_cria_csv_e_db(db_tmp, csv_tmp):
    """Todas as flags fornecidas → CSV criado + Lote+Arrematado no DB."""
    result = runner.invoke(app, [
        "registrar-compra",
        "--empresa", "carros_uberlandia",
        "--marca", "Ford",
        "--modelo", "Fusion Titanium 2.0 AWD",
        "--ano", "2014",
        "--valor", "55500",
        "--km", "95000",
        "--data", "2026-05-11",
        "--taxa", "867",
        "--frete", "1200",
        "--transf", "580",
        "--higi", "550",
        "--reforma", "1200",
        "--obs", "Compra Auto Arremate SP",
        "--csv", str(csv_tmp),
    ])

    assert result.exit_code == 0, result.output
    assert "criado" in result.output or "atualizado" in result.output
    assert "✓ CSV" in result.output
    assert "✓ DB sincronizado" in result.output

    # CSV escrito com os buckets corretos
    with csv_tmp.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    r = rows[0]
    assert r["marca"] == "Ford"
    assert r["modelo"] == "Fusion Titanium 2.0 AWD"
    assert r["ano"] == "2014"
    assert r["valor_compra"] == "55500"
    assert r["taxa_leilao_real"] == "867"
    assert r["frete_real"] == "1200"
    assert r["transferencia_real"] == "580"
    assert r["higienizacao_real"] == "550"
    assert r["gastos_reforma_real"] == "1200"
    assert r["custos_extras"] == ""  # decomposto não usa campo legacy
    assert r["observacoes"] == "Compra Auto Arremate SP"

    # DB: Lote sintético + Arrematado persistidos
    engine = create_engine(f"sqlite:///{db_tmp}", connect_args={"check_same_thread": False})
    with Session(engine) as s:
        lotes = s.exec(select(Lote).where(Lote.leilao == "historico_offline")).all()
        assert len(lotes) == 1
        assert lotes[0].marca == "Ford"
        assert lotes[0].lance_atual == 55500

        arr = s.exec(select(Arrematado)).all()
        assert len(arr) == 1
        assert arr[0].gastos_reforma_real == 1200  # só reforma, não soma total


# =============================================================================
# 2. Caminho interativo (prompt para valor ausente)
# =============================================================================

def test_interativo_prompta_valor_ausente(db_tmp, csv_tmp):
    """--valor omitido → Typer prompta; resposta via stdin é usada."""
    result = runner.invoke(
        app,
        [
            "registrar-compra",
            "--empresa", "carros_uberlandia",
            "--marca", "VW",
            "--modelo", "Gol 1.0",
            "--ano", "2015",
            # --valor deliberadamente omitido → prompt
            "--csv", str(csv_tmp),
        ],
        input="30000\n",  # resposta ao prompt do --valor
    )

    assert result.exit_code == 0, result.output
    assert "Valor de compra" in result.output  # prompt apareceu
    assert "30000" in result.output or "R$ 30,000" in result.output or "R$ 30000" in result.output

    with csv_tmp.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["valor_compra"] == "30000"


# =============================================================================
# 3. Idempotência — re-rodar atualiza, não duplica
# =============================================================================

def test_idempotencia_atualiza_sem_duplicar(db_tmp, csv_tmp):
    """Mesma (marca, modelo, ano, valor) rodada 2x → 1 linha no CSV, 1 no DB, ação='atualizado'."""
    base_args = [
        "registrar-compra",
        "--empresa", "carros_uberlandia",
        "--marca", "Fiat",
        "--modelo", "Uno 1.0",
        "--ano", "2016",
        "--valor", "18000",
        "--csv", str(csv_tmp),
    ]

    r1 = runner.invoke(app, base_args)
    assert r1.exit_code == 0, r1.output
    assert "criado" in r1.output

    # Segunda execução com mesma chave mas obs diferente
    r2 = runner.invoke(app, base_args + ["--obs", "obs atualizada"])
    assert r2.exit_code == 0, r2.output
    assert "atualizado" in r2.output

    # CSV continua com 1 linha
    with csv_tmp.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["observacoes"] == "obs atualizada"  # atualizado

    # DB continua com 1 Lote + 1 Arrematado
    engine = create_engine(f"sqlite:///{db_tmp}", connect_args={"check_same_thread": False})
    with Session(engine) as s:
        lotes = s.exec(select(Lote).where(Lote.leilao == "historico_offline")).all()
        assert len(lotes) == 1
        arr = s.exec(select(Arrematado)).all()
        assert len(arr) == 1


# =============================================================================
# 4. Formato decomposto — apenas gastos_reforma_real vai para Arrematado
# =============================================================================

def test_decomposto_isola_reforma_no_arrematado(db_tmp, csv_tmp):
    """Com taxa+frete+reforma separados, Arrematado.gastos_reforma_real recebe só reforma."""
    result = runner.invoke(app, [
        "registrar-compra",
        "--empresa", "carros_uberlandia",
        "--marca", "Chevrolet",
        "--modelo", "Onix Plus 1.0",
        "--ano", "2022",
        "--valor", "72000",
        "--taxa", "1100",
        "--frete", "800",
        "--reforma", "2500",
        "--csv", str(csv_tmp),
    ])
    assert result.exit_code == 0, result.output

    engine = create_engine(f"sqlite:///{db_tmp}", connect_args={"check_same_thread": False})
    with Session(engine) as s:
        arr = s.exec(select(Arrematado)).first()
        assert arr is not None
        assert arr.gastos_reforma_real == 2500  # não 1100+800+2500=4400


# =============================================================================
# 5. CSV pre-existente — nova linha adicionada sem apagar existentes
# =============================================================================

def test_nova_linha_preserva_existentes(db_tmp, csv_com_uma_linha):
    """Append de linha nova não apaga linha existente do CSV."""
    result = runner.invoke(app, [
        "registrar-compra",
        "--empresa", "carros_uberlandia",
        "--marca", "Toyota",
        "--modelo", "Corolla XEi",
        "--ano", "2019",
        "--valor", "88000",
        "--csv", str(csv_com_uma_linha),
    ])
    assert result.exit_code == 0, result.output

    with csv_com_uma_linha.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    marcas = {r["marca"] for r in rows}
    assert "Honda" in marcas  # linha pré-existente preservada
    assert "Toyota" in marcas  # nova linha adicionada


# =============================================================================
# 6. Validações de input
# =============================================================================

def test_ano_invalido_falha(db_tmp, csv_tmp):
    result = runner.invoke(app, [
        "registrar-compra",
        "--empresa", "carros_uberlandia",
        "--marca", "Ford", "--modelo", "Ka", "--ano", "1950",
        "--valor", "10000",
        "--csv", str(csv_tmp),
    ])
    assert result.exit_code == 1
    assert "ano" in result.output.lower() or "1950" in result.output


def test_valor_zero_falha(db_tmp, csv_tmp):
    result = runner.invoke(app, [
        "registrar-compra",
        "--empresa", "carros_uberlandia",
        "--marca", "Ford", "--modelo", "Ka", "--ano", "2020",
        "--valor", "0",
        "--csv", str(csv_tmp),
    ])
    assert result.exit_code == 1
    assert "valor" in result.output.lower()


def test_data_invalida_falha(db_tmp, csv_tmp):
    result = runner.invoke(app, [
        "registrar-compra",
        "--empresa", "carros_uberlandia",
        "--marca", "Ford", "--modelo", "Ka", "--ano", "2020",
        "--valor", "20000",
        "--data", "nao-e-uma-data",
        "--csv", str(csv_tmp),
    ])
    assert result.exit_code == 1
    assert "data" in result.output.lower()


def test_empresa_inexistente_falha_com_mensagem_util(db_tmp, csv_tmp):
    """Empresa sem YAML → mensagem legível + exit 1 (não stacktrace crua)."""
    result = runner.invoke(app, [
        "registrar-compra",
        "--empresa", "empresa_que_nao_existe",
        "--marca", "Ford", "--modelo", "Ka", "--ano", "2020",
        "--valor", "20000",
        "--csv", str(csv_tmp),
    ])
    assert result.exit_code == 1
    # Mensagem orienta o operador — sem traceback cru
    assert "empresa_que_nao_existe" in result.output
    assert "carros-sa empresas" in result.output


# =============================================================================
# 9. --help lista registrar-compra
# =============================================================================

def test_help_inclui_registrar_compra():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "registrar-compra" in result.output
