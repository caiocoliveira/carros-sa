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
    FaixaIdade,
    _categoria_de_modelo,
    calibrar_dias_giro,
    faixa_de_idade,
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
    # 10% em 90 dias = ~40,56% ao ano (90d > floor 60d, sem clamp)
    assert roi_anualizado(0.10, 90) == pytest.approx(0.10 * 365 / 90)
    # 5% em 120 dias = ~15,21% ao ano
    assert roi_anualizado(0.05, 120) == pytest.approx(0.05 * 365 / 120)


def test_roi_anualizado_floor_60_dias():
    """Lotes com dias_giro otimista (<60d) capam no floor de 60d.

    Antes o floor era 30d, mas defaults categóricos chegam a 25d (HATCH NOVO)
    e ROI saturava em 500-600% (irreal — benchmark operacional ~60-75%/ano).
    Floor 60d comprime a anualização sintética sem zerar o ranking.
    """
    # 10% em "1 dia" capeado a 60 dias = 60.83% ao ano (não 3650%)
    assert roi_anualizado(0.10, 1) == pytest.approx(0.10 * 365 / 60)
    # 10% em 30d capeado a 60 dias = 60.83% ao ano (era 121.67% com floor antigo)
    assert roi_anualizado(0.10, 30) == pytest.approx(0.10 * 365 / 60)
    assert roi_anualizado(0.10, 0) == pytest.approx(0.10 * 365 / 60)
    # 60d EXATO = sem clamp (borda)
    assert roi_anualizado(0.10, 60) == pytest.approx(0.10 * 365 / 60)
    # 61d > floor → sem clamp
    assert roi_anualizado(0.10, 61) == pytest.approx(0.10 * 365 / 61)


def test_roi_anualizado_dias_none_usa_fallback_90():
    """Avaliações sem dias_giro_estimado caem em 90 dias (4x ao ano)."""
    assert roi_anualizado(0.10, None) == pytest.approx(0.10 * 365 / 90)


def test_roi_anualizado_score_zero_e_zero():
    assert roi_anualizado(0.0, 30) == 0.0


# =============================================================================
# FaixaIdade — classificação + cascata de fallback
# =============================================================================

def test_faixa_de_idade_thresholds():
    # Ano referência 2026
    assert faixa_de_idade(2026, 2026) == FaixaIdade.NOVO     # idade 0
    assert faixa_de_idade(2023, 2026) == FaixaIdade.NOVO     # idade 3 (borda)
    assert faixa_de_idade(2022, 2026) == FaixaIdade.MEDIO    # idade 4 (borda)
    assert faixa_de_idade(2019, 2026) == FaixaIdade.MEDIO    # idade 7 (borda)
    assert faixa_de_idade(2018, 2026) == FaixaIdade.VELHO    # idade 8 (borda)
    assert faixa_de_idade(2010, 2026) == FaixaIdade.VELHO    # idade 16
    # Veículo do futuro não quebra
    assert faixa_de_idade(2030, 2026) == FaixaIdade.NOVO


def test_faixa_idade_aplica_sub_bucket_quando_tem_dados(session_isolada):
    """Quando faixa NOVO tem ≥3 amostras, calibração usa APENAS elas (não todas)."""
    session_isolada.add(Empresa(id="emp1", nome="x", config_yaml_path="x"))
    # 3 hatches NOVOS (2024, 3 anos idade=2) com 20 dias
    for i, modelo in enumerate(["Polo 1.0", "HB20 1.0", "Mobi 1.0"]):
        lote_id = f"novo{i}"
        _criar_lote(session_isolada, lote_id, "VW", modelo, 2024, 50000)
        _criar_arrematado(session_isolada, "emp1", lote_id, 50000,
                          datetime(2026, 1, 1), datetime(2026, 1, 21))  # 20d
    # 3 hatches VELHOS (2012, idade 14) com 200 dias
    for i, modelo in enumerate(["Gol 1.0", "Ka 1.0", "Palio 1.0"]):
        lote_id = f"velho{i}"
        _criar_lote(session_isolada, lote_id, "VW", modelo, 2012, 20000)
        _criar_arrematado(session_isolada, "emp1", lote_id, 20000,
                          datetime(2025, 6, 1), datetime(2025, 12, 18))  # 200d
    session_isolada.commit()
    invalidar_cache()

    # Calibração NOVO: só usa os 3 novos → média 20
    d_novo = calibrar_dias_giro("emp1", CategoriaVeiculo.HATCH, session_isolada,
                                 fallback=99, faixa_idade=FaixaIdade.NOVO, ano_referencia=2026)
    assert d_novo == 20

    # Calibração VELHO: só usa os 3 velhos → média 200
    d_velho = calibrar_dias_giro("emp1", CategoriaVeiculo.HATCH, session_isolada,
                                  fallback=99, faixa_idade=FaixaIdade.VELHO, ano_referencia=2026)
    assert d_velho == 200

    # Sem faixa (comportamento legado): média de todos = (20*3 + 200*3)/6 = 110
    d_agg = calibrar_dias_giro("emp1", CategoriaVeiculo.HATCH, session_isolada, fallback=99)
    assert d_agg == 110


def test_faixa_insuficiente_cai_pro_agregado(session_isolada):
    """Se faixa NOVO só tem 1 amostra (< 3), cai pro agregado categórico."""
    session_isolada.add(Empresa(id="emp1", nome="x", config_yaml_path="x"))
    # 1 NOVO (idade 1) com 10d — não bate mínimo de 3
    _criar_lote(session_isolada, "n0", "VW", "Polo 1.0", 2025, 50000)
    _criar_arrematado(session_isolada, "emp1", "n0", 50000,
                      datetime(2026, 1, 1), datetime(2026, 1, 11))  # 10d
    # 3 MEDIOS (idade 5) com 100d
    for i in range(3):
        lote_id = f"m{i}"
        _criar_lote(session_isolada, lote_id, "VW", "Gol 1.0", 2021, 30000)
        _criar_arrematado(session_isolada, "emp1", lote_id, 30000,
                          datetime(2025, 10, 1), datetime(2026, 1, 9))  # 100d
    session_isolada.commit()
    invalidar_cache()

    # Faixa NOVO tem 1 amostra → cai pro agregado
    # Agregado: 4 amostras (1 NOVO + 3 MEDIOS). Valor exato depende de
    # arredondamento de datas; usamos faixa pra ser robusto.
    d = calibrar_dias_giro("emp1", CategoriaVeiculo.HATCH, session_isolada,
                           fallback=99, faixa_idade=FaixaIdade.NOVO, ano_referencia=2026)
    assert 70 <= d <= 85          # agregado bate nessa faixa
    assert d != 99                # não caiu no fallback


def test_agregado_insuficiente_cai_pro_fallback(session_isolada):
    """Se nem a faixa nem o agregado tem ≥3, cai pro prior hardcoded."""
    session_isolada.add(Empresa(id="emp1", nome="x", config_yaml_path="x"))
    # Só 2 hatch VELHO, zero NOVO/MEDIO
    for i in range(2):
        lote_id = f"v{i}"
        _criar_lote(session_isolada, lote_id, "VW", "Gol 1.0", 2014, 20000)
        _criar_arrematado(session_isolada, "emp1", lote_id, 20000,
                          datetime(2025, 6, 1), datetime(2025, 10, 9))
    session_isolada.commit()
    invalidar_cache()

    # Pede NOVO: faixa tem 0 → agregado tem 2 → insuficiente → fallback
    d = calibrar_dias_giro("emp1", CategoriaVeiculo.HATCH, session_isolada,
                           fallback=42, faixa_idade=FaixaIdade.NOVO, ano_referencia=2026)
    assert d == 42
