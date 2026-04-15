"""Testes do parser e estatísticas do Webmotors.

Gold test usa a fixture REAL coletada via Chrome MCP em 2026-04-14:
  data/scrapes/2026-04-14_webmotors_fiesta.json  (26 cards, Ford Fiesta)

Nenhum teste bate em rede — _fetch_playwright está bloqueado por design.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from carros_sa.tools.webmotors import (
    AnuncioWM,
    EstatisticasWM,
    _percentil,
    estatisticas,
    parse_card,
    parse_resultados,
)

# =============================================================================
# Fixture real
# =============================================================================

FIXTURE_PATH = Path(__file__).parent.parent / "data/scrapes/2026-04-14_webmotors_fiesta.json"


@pytest.fixture(scope="module")
def cards_reais():
    data = json.loads(FIXTURE_PATH.read_text())
    return data["cards"]


@pytest.fixture(scope="module")
def anuncios_reais(cards_reais):
    return parse_resultados(cards_reais)


# =============================================================================
# parse_card — casos individuais
# =============================================================================

def test_parse_card_basico():
    texto = [
        "1/8",
        "FORD FIESTA",
        "1.6 Se Hatch 16v Flex 4p Manual",
        "2012/2013",
        "145.000 Km",
        "Santo André (SP)",
        "R$ 39.200",
        "Ver parcelas",
    ]
    a = parse_card(texto, "67470583")
    assert a is not None
    assert a.marca == "Ford"
    assert a.modelo == "Fiesta"
    assert a.versao == "1.6 Se Hatch 16v Flex 4p Manual"
    assert a.ano_fab == 2012
    assert a.ano_mod == 2013
    assert a.km == 145_000
    assert a.cidade == "Santo André"
    assert a.uf == "SP"
    assert a.preco == 39_200
    assert a.abaixo_da_fipe is False
    assert a.oferta_destaque is False


def test_parse_card_com_badges():
    """Card com carousel + 2 badges na frente."""
    texto = [
        "1/17",
        "OFERTA DESTAQUE",
        "ABAIXO DA FIPE",
        "FORD FIESTA",
        "1.0 Ecoboost Titanium Plus Hatch 12v Gasolina 4p Powershift",
        "2016/2017",
        "100.000 Km",
        "Brasília (DF)",
        "R$ 53.000",
        "Ver parcelas",
    ]
    a = parse_card(texto, "67210102")
    assert a is not None
    assert a.abaixo_da_fipe is True
    assert a.oferta_destaque is True
    assert a.preco == 53_000
    assert a.ano_fab == 2016
    assert a.ano_mod == 2017


def test_parse_card_sem_carousel():
    """Sem linha de carousel (alguns cards não têm)."""
    texto = [
        "FORD FIESTA",
        "1.5 Se Hatch 16v Flex 4p Manual",
        "2014/2015",
        "49.590 Km",
        "Jaboatão dos Guararapes (PE)",
        "R$ 33.500",
        "Ver parcelas",
    ]
    a = parse_card(texto, "67188344")
    assert a is not None
    assert a.preco == 33_500
    assert a.uf == "PE"


def test_parse_card_sem_preco_retorna_none():
    texto = ["FORD FIESTA", "1.6 Se", "2013/2014", "50.000 Km", "SP (SP)"]
    assert parse_card(texto, "x") is None


def test_parse_card_sem_ano_retorna_none():
    texto = ["FORD FIESTA", "1.6 Se", "50.000 Km", "São Paulo (SP)", "R$ 39.000"]
    assert parse_card(texto, "x") is None


# =============================================================================
# parse_resultados — gold sobre fixture real (26 cards)
# =============================================================================

def test_parse_resultados_recupera_todos_os_26_cards(anuncios_reais):
    """Todos os 26 cards devem parsear sem None."""
    assert len(anuncios_reais) == 26


def test_parse_resultados_precos_positivos(anuncios_reais):
    assert all(a.preco > 0 for a in anuncios_reais)


def test_parse_resultados_anos_validos(anuncios_reais):
    for a in anuncios_reais:
        assert 2000 <= a.ano_fab <= 2030, f"ano_fab inválido: {a}"
        assert a.ano_mod >= a.ano_fab, f"ano_mod < ano_fab: {a}"


def test_parse_resultados_kms_razoaveis(anuncios_reais):
    """Km deve ser 0 < km < 500k para carros usados."""
    assert all(0 < a.km < 500_000 for a in anuncios_reais)


# =============================================================================
# estatisticas() — gold test: Fiesta 2013 contra dados reais
# =============================================================================

def test_estatisticas_fiesta_2013_com_fixture_real(anuncios_reais):
    """
    Gold: Ford Fiesta 2013 usando a fixture coletada ao vivo.
    Cards com ano_fab=2013 ou ano_mod=2013:
      31936, 39200, 42900, 39900, 37900, 35900, 33500, 28500, 44880, 33990, 34999
    Ordenado: 28500 31936 33500 33990 34999 35900 37900 39200 39900 42900 44880
    n=11, mediana=35900, p25≈33745
    """
    est = estatisticas("Ford", "Fiesta", 2013, anuncios=anuncios_reais)

    assert est.n_anuncios == 11
    assert est.mediana == 35_900
    # p25 interpolado entre 33500 e 33990: ~33745
    assert est.p25 == 33_745
    assert est.p25 < est.mediana


def test_estatisticas_retorna_zeros_quando_sem_anuncios_do_ano():
    anuncios = [
        AnuncioWM("1", "Ford", "Fiesta", "1.6", 2010, 2011, 80_000, "SP", "SP", 30_000),
    ]
    est = estatisticas("Ford", "Fiesta", 2013, anuncios=anuncios)
    assert est.n_anuncios == 0
    assert est.mediana == 0
    assert est.p25 == 0


def test_estatisticas_match_case_insensitivo():
    anuncios = [
        AnuncioWM("1", "Ford", "Fiesta", "1.6", 2013, 2014, 100_000, "SP", "SP", 38_000),
        AnuncioWM("2", "Ford", "Fiesta", "1.0", 2013, 2014, 120_000, "RJ", "RJ", 30_000),
    ]
    est = estatisticas("FORD", "FIESTA", 2013, anuncios=anuncios)
    assert est.n_anuncios == 2


def test_estatisticas_com_fetch_injetavel():
    """fetch= permite substituir Playwright por qualquer callable (mocks, CSV)."""
    def fetch_fake(marca: str, modelo: str):
        return [
            AnuncioWM("1", marca.title(), modelo.title(), "1.6", 2013, 2014, 100_000, "SP", "SP", 40_000),
            AnuncioWM("2", marca.title(), modelo.title(), "1.0", 2012, 2013, 120_000, "RJ", "RJ", 32_000),
        ]

    est = estatisticas("Ford", "Fiesta", 2013, fetch=fetch_fake)
    assert est.n_anuncios == 2
    assert est.mediana == 36_000  # median([32000, 40000])


def test_estatisticas_sem_fetch_levanta_not_implemented():
    """Sem anuncios= nem fetch=, chama _fetch_playwright que levanta NotImplementedError."""
    with pytest.raises(NotImplementedError, match="Coleta ao vivo"):
        estatisticas("Ford", "Fiesta", 2013)


# =============================================================================
# _percentil
# =============================================================================

def test_percentil_lista_vazia():
    assert _percentil([], 25) == 0


def test_percentil_um_elemento():
    assert _percentil([42], 25) == 42


def test_percentil_interpolacao():
    vals = [10, 20, 30, 40, 50]
    assert _percentil(vals, 25) == 20
    assert _percentil(vals, 50) == 30
    assert _percentil(vals, 75) == 40
