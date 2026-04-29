"""Testes do ajuste_km — fator multiplicativo sobre a âncora de venda."""

from __future__ import annotations

import pytest

from carros_sa.ajuste_km import fator_km


def test_km_igual_mediana_fator_neutro():
    assert fator_km(80_000, 80_000) == pytest.approx(1.0)


def test_km_metade_da_mediana_aumenta_ate_o_cap():
    # km=40k, mediana=80k → delta = +0.5 → fator = 1 + 0.5*0.30 = 1.15 (exatamente no cap)
    assert fator_km(40_000, 80_000) == pytest.approx(1.15)


def test_km_dobro_da_mediana_derruba_ate_o_piso():
    # km=160k, mediana=80k → delta = -1.0 → fator nominal 0.70 → clamp em 0.75
    assert fator_km(160_000, 80_000) == pytest.approx(0.75)


def test_km_20pct_acima_desconto_moderado():
    # km=96k, mediana=80k → delta = -0.2 → fator = 1 - 0.06 = 0.94
    assert fator_km(96_000, 80_000) == pytest.approx(0.94, abs=0.001)


def test_km_20pct_abaixo_bonus_moderado():
    # km=64k, mediana=80k → delta = +0.2 → fator = 1 + 0.06 = 1.06
    assert fator_km(64_000, 80_000) == pytest.approx(1.06, abs=0.001)


def test_km_lote_ausente_retorna_neutro():
    assert fator_km(None, 80_000) == 1.0


def test_km_mercado_ausente_retorna_neutro():
    assert fator_km(80_000, None) == 1.0


def test_km_lote_zero_retorna_neutro():
    """km=0 é um sinal de 'desconhecido' (comum no scraper); tratamos como faltando."""
    assert fator_km(0, 80_000) == 1.0


def test_km_mercado_zero_retorna_neutro():
    """Mediana do mercado == 0 significa sem amostra — não penalizar."""
    assert fator_km(80_000, 0) == 1.0
