"""Gold tests do calibrador de dias_giro a partir do histórico Arrematado.

Cobre:
  - Categoria com ≥3 vendas → usa média observada
  - Categoria com <3 vendas → cai pro prior hardcoded (fallback)
  - Inferência de categoria por nome de modelo (alinhada com orquestrador)
  - roi_anualizado: aritmética + floor de 30 dias
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.agents.calibracao_giro import (
    _categoria_de_modelo,
    calibrar_dias_giro,
    invalidar_cache,
    roi_anualizado,
)
from carros_sa.models import Arrematado, CategoriaVeiculo, Empresa, Lote


@pytest.fixture
def session_isolada() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    invalidar_cache()  # evita vazamento entre testes


def _criar_lote(
    session: Session,
    lote_id: str,
    marca: str,
    modelo: str,
    ano: int,
    valor: int,
) -> None:
    session.add(Lote(
        id=lote_id, leilao="historico_offline", url="",
        marca=marca, modelo=modelo, ano=ano, lance_atual=valor,
        raw_json={},
    ))


def _criar_arrematado(
    session: Session,
    empresa_id: str,
    lote_id: str,
    valor: int,
    data_compra: datetime,
    data_venda: datetime,
) -> None:
    session.add(Arrematado(
        empresa_id=empresa_id,
        lote_id=lote_id,
        preco_real=valor,
        data=data_compra,
        vendido_por=valor + 5000,
        vendido_em=data_venda,
    ))


# =============================================================================
# Inferência de categoria
# =============================================================================

def test_categoria_de_modelo_pickup():
    assert _categoria_de_modelo("Strada Adventure") == CategoriaVeiculo.PICAPE
    assert _categoria_de_modelo("Hilux SR") == CategoriaVeiculo.PICAPE
    assert _categoria_de_modelo("Toro Endurance 1.8 Flex Aut") == CategoriaVeiculo.PICAPE


def test_categoria_de_modelo_suv():
    assert _categoria_de_modelo("Renegade Sport") == CategoriaVeiculo.SUV
    assert _categoria_de_modelo("Tracker LTZ 1.0 Turbo") == CategoriaVeiculo.SUV
    assert _categoria_de_modelo("Pajero Full 3.2 Diesel") == CategoriaVeiculo.SUV


def test_categoria_de_modelo_hatch_e_sedan():
    assert _categoria_de_modelo("Polo Track 1.0") == CategoriaVeiculo.HATCH
    assert _categoria_de_modelo("Onix Joy 1.0") == CategoriaVeiculo.HATCH
    assert _categoria_de_modelo("Voyage 1.6") == CategoriaVeiculo.SEDAN
    assert _categoria_de_modelo("Cruze Sedan") == CategoriaVeiculo.SEDAN


def test_categoria_de_modelo_desconhecido_cai_em_outro():
    assert _categoria_de_modelo("MarcaInventada XYZ") == CategoriaVeiculo.OUTRO


# =============================================================================
# Calibração com histórico
# =============================================================================

def test_calibracao_usa_arrematado_quando_3_ou_mais_vendas(session_isolada):
    """≥3 sedans vendidos com ~28 dias → função retorna 28 (não o prior 30)."""
    session_isolada.add(Empresa(id="emp1", nome="x", config_yaml_path="x"))
    # 3 sedans (Voyage e Logan caem em SEDAN), todos com 28 dias entre compra e venda
    for i, (modelo, dias) in enumerate([
        ("Voyage 1.6", 28),
        ("Logan Life", 30),
        ("Cruze Sedan", 26),
    ]):
        lote_id = f"hist{i}"
        _criar_lote(session_isolada, lote_id, "VW", modelo, 2018, 30000)
        compra = datetime(2026, 1, 1)
        venda = datetime(2026, 1, 1 + dias)
        _criar_arrematado(session_isolada, "emp1", lote_id, 30000, compra, venda)
    session_isolada.commit()
    invalidar_cache()

    # Prior fallback é 30 (sedan default). Esperamos calibração = (28+30+26)/3 = 28
    dias_calib = calibrar_dias_giro(
        "emp1", CategoriaVeiculo.SEDAN, session_isolada, fallback=999,
    )
    assert dias_calib == 28


def test_calibracao_fallback_quando_menos_de_3_vendas(session_isolada):
    """Categoria com <3 vendas usa o prior hardcoded passado."""
    session_isolada.add(Empresa(id="emp1", nome="x", config_yaml_path="x"))
    # Só 1 SUV vendido — abaixo do mínimo
    _criar_lote(session_isolada, "h1", "Jeep", "Renegade Sport", 2021, 80000)
    _criar_arrematado(
        session_isolada, "emp1", "h1", 80000,
        datetime(2026, 1, 1), datetime(2026, 4, 1),
    )
    session_isolada.commit()
    invalidar_cache()

    dias_calib = calibrar_dias_giro(
        "emp1", CategoriaVeiculo.SUV, session_isolada, fallback=42,
    )
    assert dias_calib == 42  # fallback, não a única amostra


def test_calibracao_ignora_arrematados_sem_data_venda(session_isolada):
    """Linhas 'no pátio' (sem vendido_em) não contam na média."""
    session_isolada.add(Empresa(id="emp1", nome="x", config_yaml_path="x"))
    # 2 vendidas + 5 no pátio — só as 2 vendidas contam, abaixo do mínimo de 3
    for i, dias in enumerate([20, 30]):
        lote_id = f"v{i}"
        _criar_lote(session_isolada, lote_id, "VW", "Polo Track", 2024, 50000)
        _criar_arrematado(
            session_isolada, "emp1", lote_id, 50000,
            datetime(2026, 1, 1), datetime(2026, 1, 1 + dias),
        )
    for i in range(5):
        lote_id = f"p{i}"
        _criar_lote(session_isolada, lote_id, "VW", "Polo Track", 2024, 50000)
        # Arrematado sem vendido_em
        session_isolada.add(Arrematado(
            empresa_id="emp1", lote_id=lote_id, preco_real=50000,
            data=datetime(2026, 1, 1),
        ))
    session_isolada.commit()
    invalidar_cache()

    # Cai pro fallback (só 2 vendidos, precisa de 3)
    dias_calib = calibrar_dias_giro(
        "emp1", CategoriaVeiculo.HATCH, session_isolada, fallback=25,
    )
    assert dias_calib == 25


def test_calibracao_filtra_por_empresa(session_isolada):
    """Calibração de empresa A não vaza pra empresa B."""
    session_isolada.add(Empresa(id="empA", nome="A", config_yaml_path="x"))
    session_isolada.add(Empresa(id="empB", nome="B", config_yaml_path="x"))
    # 3 vendas só pra empresa A
    for i in range(3):
        lote_id = f"a{i}"
        _criar_lote(session_isolada, lote_id, "VW", "Voyage 1.6", 2018, 30000)
        _criar_arrematado(
            session_isolada, "empA", lote_id, 30000,
            datetime(2026, 1, 1), datetime(2026, 2, 20),  # 50 dias
        )
    session_isolada.commit()
    invalidar_cache()

    # empA tem dado calibrado
    assert calibrar_dias_giro("empA", CategoriaVeiculo.SEDAN, session_isolada, fallback=99) == 50
    # empB não — cai no fallback
    assert calibrar_dias_giro("empB", CategoriaVeiculo.SEDAN, session_isolada, fallback=99) == 99


# =============================================================================
# ROI anualizado
# =============================================================================

def test_roi_anualizado_aritmetica():
    # 10% em 30 dias = ~121,67% ao ano
    assert roi_anualizado(0.10, 30) == pytest.approx(0.10 * 365 / 30)
    # 5% em 90 dias = ~20,28% ao ano
    assert roi_anualizado(0.05, 90) == pytest.approx(0.05 * 365 / 90)


def test_roi_anualizado_floor_30_dias():
    """Lotes com dias_giro absurdo baixo (3, 1) capam em 30 pra evitar inflar."""
    # 10% em "1 dia" capeado a 30 dias = 121,67% ao ano (não 3650%)
    assert roi_anualizado(0.10, 1) == pytest.approx(0.10 * 365 / 30)
    assert roi_anualizado(0.10, 0) == pytest.approx(0.10 * 365 / 30)


def test_roi_anualizado_dias_none_usa_fallback_90():
    """Avaliações sem dias_giro_estimado caem em 90 dias (4x ao ano)."""
    assert roi_anualizado(0.10, None) == pytest.approx(0.10 * 365 / 90)


def test_roi_anualizado_score_zero_e_zero():
    assert roi_anualizado(0.0, 30) == 0.0
