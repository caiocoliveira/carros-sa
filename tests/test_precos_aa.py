"""Testes do extrator de preços Auto Avaliar (ULTIMA AVALIAÇÃO + FIPE %).

Fixture real: fragmento do DOM da página de detalhe do lote 21893414 (VW Voyage 2019),
capturado autenticado em 2026-04-16 com conta de lojista. Ver
`tests/fixtures/21893414_voyage_precos_aa.html`.
"""

from pathlib import Path

import pytest

from carros_sa.scraping.parsers import extrair_precos_aa


FIXTURE = Path(__file__).parent / "fixtures" / "21893414_voyage_precos_aa.html"


def _html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# =============================================================================
# Gold test com fixture real
# =============================================================================

def test_extrai_preco_ultima_avaliacao_auto_avaliar():
    precos = extrair_precos_aa(_html())
    assert precos.preco_referencia_aa == 31000


def test_extrai_fipe_pct_do_lance_minimo():
    precos = extrair_precos_aa(_html())
    assert precos.fipe_pct_lance_minimo == 34


# =============================================================================
# Robustez: variações que não devem quebrar
# =============================================================================

def test_html_sem_preco_referencia_retorna_none():
    html = '<div class="vehicle-avaliacao"><div class="tag-percent-value">28<small>%</small></div></div>'
    precos = extrair_precos_aa(html)
    assert precos.preco_referencia_aa is None
    assert precos.fipe_pct_lance_minimo == 28


def test_html_sem_fipe_pct_retorna_none():
    html = (
        '<div class="col1 vehicle-min-price">'
        '<span class="currency bind-price">45.500,00</span></div>'
    )
    precos = extrair_precos_aa(html)
    assert precos.preco_referencia_aa == 45500
    assert precos.fipe_pct_lance_minimo is None


def test_html_vazio_retorna_ambos_none():
    precos = extrair_precos_aa("")
    assert precos.preco_referencia_aa is None
    assert precos.fipe_pct_lance_minimo is None


def test_fipe_pct_tres_digitos_limitado():
    # FIPE pode teoricamente passar de 100% (carro raro); validamos parse correto até 999
    html = '<div class="tag-percent-value">112<small>%</small></div>'
    precos = extrair_precos_aa(html)
    assert precos.fipe_pct_lance_minimo == 112


def test_preco_com_virgula_decimal_sem_centavos():
    # Alguns cards vêm sem casa decimal completa
    html = '<span class="currency bind-price">28.500</span>'
    precos = extrair_precos_aa(html)
    assert precos.preco_referencia_aa == 28500


# =============================================================================
# Fallback via innerText (quando HTML bruto não tá disponível, só o text)
# =============================================================================

def test_extrai_de_innertext_plano():
    """Na ausência de tags HTML, o fallback via regex de texto ainda acha os 2 campos."""
    text = (
        "VOLKSWAGEN VOYAGE\n"
        "34%\n"
        "Veículo de Repasse\n"
        "ULTIMA AVALIAÇÃO\n"
        "31.000,00 / VOYAGE\n"
        "R$\n"
        "min. 31.200,00\n"
    )
    precos = extrair_precos_aa(text)
    assert precos.preco_referencia_aa == 31000
    assert precos.fipe_pct_lance_minimo == 34
