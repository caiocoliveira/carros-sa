"""Testes do extrator textual de avarias — analisa o bloco 'Observações' do
laudo cautelar (texto livre escrito pelo inspetor) em busca de menções a
reparo/substituição de peças estruturais.

Existe pra que, quando a camada visual (Gemini) falhe por overload 503, o
pipeline ainda consiga gerar `avarias` e `severidade_geral` a partir do PDF.
"""

from __future__ import annotations

import pytest

from carros_sa.agents.extrator_laudo import (
    extrair_avarias_textuais,
    parse_laudo_textual,
)
from carros_sa.models import SeveridadeAvaria


# =============================================================================
# Fiesta 21854782 — gold test (texto real do PDF)
# =============================================================================

@pytest.fixture
def laudo_fiesta(pdf_fiesta_real):
    return parse_laudo_textual(pdf_fiesta_real)


def test_parse_laudo_fiesta_encontra_observacao_do_reparo(laudo_fiesta):
    """O Fiesta real tem 'VEÍCULO POSSUI REPARO DE FUNILARIA NAS COLUNAS B e C DO LADO ESQUERDO'."""
    obs = (laudo_fiesta.observacoes or "").upper()
    assert "REPARO" in obs and "COLUNA" in obs


def test_extrair_avarias_fiesta_pega_coluna_b_e_c(laudo_fiesta):
    avarias = extrair_avarias_textuais(laudo_fiesta.observacoes)
    partes = {a.parte for a in avarias}
    assert "coluna_b_esquerda" in partes
    assert "coluna_c_esquerda" in partes
    # Ambas devem ser classificadas como graves (coluna reparada é sinal estrutural)
    assert all(a.severidade in {SeveridadeAvaria.GRAVE, SeveridadeAvaria.ESTRUTURAL} for a in avarias)


# =============================================================================
# Casos sintéticos — regex robusta
# =============================================================================

def test_vazio_retorna_lista_vazia():
    assert extrair_avarias_textuais("") == []
    assert extrair_avarias_textuais(None) == []


def test_longarina_reparada_marca_estrutural():
    obs = "REPARO NA LONGARINA DIANTEIRA DIREITA."
    av = extrair_avarias_textuais(obs)
    assert len(av) == 1
    assert av[0].parte == "longarina_dianteira_direita"
    assert av[0].severidade == SeveridadeAvaria.GRAVE


def test_porta_lateral_marca_media():
    obs = "REPINTURA DA PORTA DIANTEIRA ESQUERDA E PARALAMA TRASEIRO DIREITO."
    av = extrair_avarias_textuais(obs)
    partes = {a.parte for a in av}
    # Porta e paralama são chapa externa — severidade média.
    # Sufixo de posição/lado segue convenção feminina ("traseira/esquerda") —
    # o EstimadorReforma casa por prefixo de família, gênero é irrelevante.
    assert "porta_dianteira_esquerda" in partes
    assert "paralama_traseira_direita" in partes
    for a in av:
        assert a.severidade == SeveridadeAvaria.MEDIA


def test_capo_tampa_motor_reconhecidos():
    obs = "CAPÔ ORIGINAL. TAMPA DO MOTOR SUBSTITUÍDA."
    av = extrair_avarias_textuais(obs)
    # "Capô original" não conta como avaria; "tampa do motor substituída" sim.
    partes = {a.parte for a in av}
    assert "tampa_motor" in partes or "capo_tampa_motor" in partes


def test_observacao_sem_reparo_retorna_vazio():
    """Observações sem qualquer verbo de reparo — zero avarias."""
    obs = (
        "Veículo em bom estado geral. Pneus originais. "
        "Lacres intactos. Documentação regular."
    )
    assert extrair_avarias_textuais(obs) == []


def test_multiplos_reparos_retorna_multiplas_avarias():
    obs = (
        "VEÍCULO APRESENTA REPARO NA COLUNA B DIREITA E NA LONGARINA TRASEIRA ESQUERDA. "
        "PORTA DIANTEIRA DIREITA REPINTADA."
    )
    av = extrair_avarias_textuais(obs)
    partes = {a.parte for a in av}
    assert "coluna_b_direita" in partes
    assert "longarina_traseira_esquerda" in partes
    assert "porta_dianteira_direita" in partes
    assert len(partes) >= 3
