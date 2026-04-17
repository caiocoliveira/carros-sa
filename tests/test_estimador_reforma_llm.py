"""Gold + unit tests do EstimadorReformaLLM.

Ideia: substituir a tabela determinística (família × severidade) por um LLM
que lê o laudo estruturado + observações + carro + região e devolve itens
com custos específicos. Quando o LLM falha (timeout, JSON malformado), cai
no estimador determinístico — então o pipeline nunca fica sem resposta.

Gold Fiesta 21854782: 2 colunas estruturais reparadas → LLM deve retornar
custo >= R$ 8k (mesmo patamar do determinístico de R$ 10k, mas com itens
mais detalhados). O teste de diferenciação valida o ponto central do
workstream: mesma severidade em carros diferentes não pode dar o mesmo
número quando o mercado de peças e mão-de-obra é distinto.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import pytest

from carros_sa.agents.estimador_reforma_llm import (
    TextLLMClient,
    estimar_llm,
)
from carros_sa.models import (
    Avaria,
    CategoriaVeiculo,
    CustoReforma,
    LaudoEstruturado,
    SeveridadeAvaria,
    StatusDocumentacao,
)
from carros_sa.tenancy import carregar_empresa

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
FIESTA_LLM_FIXTURE = FIXTURE_DIR / "21854782_reforma_llm.json"


class FakeTextLLMClient(TextLLMClient):
    """Client determinístico pra testes — retorna respostas pré-gravadas em ordem."""

    def __init__(self, responses: List[dict], raise_on_call: Optional[Exception] = None):
        self._responses = list(responses)
        self._raise = raise_on_call
        self.prompts_vistos: List[str] = []

    def generate_json(self, prompt: str) -> dict:
        self.prompts_vistos.append(prompt)
        if self._raise:
            raise self._raise
        if not self._responses:
            raise RuntimeError("FakeTextLLMClient sem respostas restantes")
        return self._responses.pop(0)

    @property
    def custo_estimado_usd(self) -> float:
        return 0.0


def _laudo_fiesta_estrutural() -> LaudoEstruturado:
    return LaudoEstruturado(
        avarias=[
            Avaria(parte="coluna_b_esquerda", severidade=SeveridadeAvaria.GRAVE,
                   descricao="reparado/soldado"),
            Avaria(parte="coluna_c_esquerda", severidade=SeveridadeAvaria.GRAVE,
                   descricao="reparado/soldado"),
        ],
        severidade_geral=SeveridadeAvaria.ESTRUTURAL,
        motor_ok=False,
        documentacao=StatusDocumentacao.OK,
        categoria_veiculo=CategoriaVeiculo.HATCH,
        confidence=0.9,
    )


def _laudo_motor_nao_original_isolado() -> LaudoEstruturado:
    """Laudo típico onde só o motor não é original (não há dano estrutural).

    Hoje, o estimador determinístico aplica R$ 4.000 fixo pra QUALQUER carro.
    O ponto do LLM é diferenciar Range Rover (peças caras) de Gol (peças
    baratas) — mesma situação no laudo, custos muito diferentes no mundo real.
    """
    return LaudoEstruturado(
        avarias=[],
        severidade_geral=SeveridadeAvaria.NENHUMA,
        motor_ok=False,
        documentacao=StatusDocumentacao.OK,
        categoria_veiculo=CategoriaVeiculo.HATCH,
        confidence=0.7,
    )


# =============================================================================
# Gold test — Fiesta 21854782
# =============================================================================

def test_fiesta_estrutural_llm_retorna_custo_alto_com_itens_detalhados():
    """Laudo estrutural real → LLM devolve 3+ itens e custo total >= R$ 8k."""
    laudo = _laudo_fiesta_estrutural()
    empresa = carregar_empresa("carros_uberlandia")
    fake = FakeTextLLMClient([json.loads(FIESTA_LLM_FIXTURE.read_text())])

    custo = estimar_llm(
        laudo=laudo,
        lote_info={
            "marca": "FORD", "modelo": "Fiesta Hatch 1.6", "ano": 2013,
            "km": 180_000, "lance_atual": 12_000,
        },
        empresa=empresa,
        llm_client=fake,
        observacoes_pdf="VEÍCULO POSSUI REPARO NAS COLUNAS B E C DO LADO ESQUERDO",
    )

    assert isinstance(custo, CustoReforma)
    assert len(custo.itens) >= 3
    assert custo.custo_total >= 8_000
    # LLM recebeu no prompt a severidade ESTRUTURAL e o carro
    prompt = fake.prompts_vistos[0]
    assert "estrutural" in prompt.lower()
    assert "fiesta" in prompt.lower()


def test_fiesta_range_respeita_incerteza_min_menor_que_max():
    laudo = _laudo_fiesta_estrutural()
    empresa = carregar_empresa("carros_uberlandia")
    fake = FakeTextLLMClient([json.loads(FIESTA_LLM_FIXTURE.read_text())])

    custo = estimar_llm(
        laudo=laudo,
        lote_info={"marca": "FORD", "modelo": "Fiesta", "ano": 2013},
        empresa=empresa,
        llm_client=fake,
    )

    assert custo.range_min <= custo.custo_total <= custo.range_max


# =============================================================================
# Diferenciação por carro — o ponto central do workstream
# =============================================================================

def test_motor_nao_original_diferencia_gol_de_range_rover():
    """Mesmo laudo (motor não original, sem estrutural), carros com mercado
    de peças muito diferentes → LLM devolve custos distintos.

    Garante que a flag 'motor_ok=False' não é mais um fallback cego de R$4k —
    o contexto do carro (marca/modelo/ano) entra no prompt e diferencia.
    """
    laudo = _laudo_motor_nao_original_isolado()
    empresa = carregar_empresa("carros_uberlandia")

    gol_response = {
        "itens": [{"descricao": "Inspeção motor + retífica parcial Gol 1.0", "custo": 1800}],
        "custo_total": 1800, "range_min": 1400, "range_max": 2400,
        "confidence": 0.7, "justificativa": "Motor VW AP de baixa complexidade.",
    }
    evoque_response = {
        "itens": [
            {"descricao": "Diagnóstico motor Evoque 2.0 turbo", "custo": 2500},
            {"descricao": "Retífica ou substituição parcial (peças importadas)", "custo": 9500},
        ],
        "custo_total": 12_000, "range_min": 9_000, "range_max": 18_000,
        "confidence": 0.65, "justificativa": "Motor 2.0 JLR com peças caras e mão-de-obra especializada.",
    }

    fake = FakeTextLLMClient([gol_response, evoque_response])

    gol = estimar_llm(
        laudo=laudo,
        lote_info={"marca": "VOLKSWAGEN", "modelo": "Gol 1.0", "ano": 2014},
        empresa=empresa, llm_client=fake,
    )
    evoque = estimar_llm(
        laudo=laudo,
        lote_info={"marca": "LAND ROVER", "modelo": "Range Rover Evoque", "ano": 2018},
        empresa=empresa, llm_client=fake,
    )

    assert gol.custo_total < evoque.custo_total
    assert evoque.custo_total >= 3 * gol.custo_total, (
        "Carros de mercado completamente diferente devem ter custos multiplicativamente distintos, "
        "não um flat R$4k igual pros dois"
    )


# =============================================================================
# Robustez: LLM falha ou devolve resposta inválida → fallback determinístico
# =============================================================================

def test_llm_raise_cai_no_estimador_deterministico():
    """Gemini/Haiku fora do ar → pipeline continua com tabela YAML."""
    laudo = _laudo_fiesta_estrutural()
    empresa = carregar_empresa("carros_uberlandia")
    fake = FakeTextLLMClient([], raise_on_call=RuntimeError("API down"))

    custo = estimar_llm(
        laudo=laudo,
        lote_info={"marca": "FORD", "modelo": "Fiesta", "ano": 2013},
        empresa=empresa, llm_client=fake,
    )

    # Determinístico pra Fiesta: 2 coluna grave (3500 cada) + 3000 estrutural = 10_000
    # Aceita variação se a tabela mudar — o ponto é que RETORNOU algo válido.
    assert custo.custo_total > 0
    assert len(custo.itens) > 0


def test_llm_json_malformado_cai_no_deterministico():
    """LLM responde JSON faltando campos obrigatórios → fallback transparente."""
    laudo = _laudo_fiesta_estrutural()
    empresa = carregar_empresa("carros_uberlandia")
    fake = FakeTextLLMClient([{"itens": "não é uma lista"}])  # shape errado

    custo = estimar_llm(
        laudo=laudo,
        lote_info={"marca": "FORD", "modelo": "Fiesta", "ano": 2013},
        empresa=empresa, llm_client=fake,
    )

    assert custo.custo_total > 0
    assert len(custo.itens) > 0


def test_llm_custo_total_inconsistente_usa_soma_dos_itens():
    """Se LLM manda custo_total != sum(itens), usa a soma (mais confiável)."""
    laudo = _laudo_fiesta_estrutural()
    empresa = carregar_empresa("carros_uberlandia")
    fake = FakeTextLLMClient([{
        "itens": [
            {"descricao": "A", "custo": 1000},
            {"descricao": "B", "custo": 2000},
        ],
        "custo_total": 99_999,  # mentira
        "range_min": 2400, "range_max": 3600,
        "confidence": 0.7,
    }])

    custo = estimar_llm(
        laudo=laudo,
        lote_info={"marca": "FORD", "modelo": "Fiesta", "ano": 2013},
        empresa=empresa, llm_client=fake,
    )

    assert custo.custo_total == 3000  # 1000 + 2000


def test_llm_sem_range_deriva_do_custo_total():
    """Resposta mínima (só itens) → range = custo_total ± 25%."""
    laudo = _laudo_fiesta_estrutural()
    empresa = carregar_empresa("carros_uberlandia")
    fake = FakeTextLLMClient([{
        "itens": [{"descricao": "reparo único", "custo": 4000}],
        "custo_total": 4000,
        "confidence": 0.7,
    }])

    custo = estimar_llm(
        laudo=laudo,
        lote_info={"marca": "FORD", "modelo": "Fiesta", "ano": 2013},
        empresa=empresa, llm_client=fake,
    )

    assert custo.custo_total == 4000
    assert custo.range_min == 3000  # 4000 * 0.75
    assert custo.range_max == 5000  # 4000 * 1.25


# =============================================================================
# Prompt shape — garante que o LLM recebe os campos mínimos
# =============================================================================

def test_custoreforma_preserva_justificativa_do_llm_como_racional():
    """Quando LLM devolve 'justificativa', ela sobrevive em CustoReforma.racional
    pra ser propagada até o Sheets (coluna 'Racional Reforma')."""
    laudo = _laudo_fiesta_estrutural()
    empresa = carregar_empresa("carros_uberlandia")
    fake = FakeTextLLMClient([{
        "itens": [{"descricao": "Solda coluna B", "custo": 3800}],
        "custo_total": 3800, "range_min": 3000, "range_max": 4700,
        "confidence": 0.85,
        "justificativa": "Fiesta 2013 com coluna estrutural reparada — retrabalho de solda.",
    }])

    custo = estimar_llm(
        laudo=laudo,
        lote_info={"marca": "FORD", "modelo": "Fiesta", "ano": 2013},
        empresa=empresa, llm_client=fake,
    )

    assert custo.racional == (
        "Fiesta 2013 com coluna estrutural reparada — retrabalho de solda."
    )


def test_fallback_deterministico_nao_tem_racional():
    """LLM falhou → cai no determinístico → racional fica None
    (precificador então monta a partir dos itens)."""
    laudo = _laudo_fiesta_estrutural()
    empresa = carregar_empresa("carros_uberlandia")
    fake = FakeTextLLMClient([], raise_on_call=RuntimeError("API down"))

    custo = estimar_llm(
        laudo=laudo,
        lote_info={"marca": "FORD", "modelo": "Fiesta", "ano": 2013},
        empresa=empresa, llm_client=fake,
    )

    assert custo.racional is None
    assert custo.custo_total > 0  # determinístico respondeu


def test_prompt_inclui_carro_severidade_regiao_e_avarias():
    laudo = _laudo_fiesta_estrutural()
    empresa = carregar_empresa("carros_uberlandia")
    fake = FakeTextLLMClient([json.loads(FIESTA_LLM_FIXTURE.read_text())])

    estimar_llm(
        laudo=laudo,
        lote_info={"marca": "FORD", "modelo": "Fiesta Hatch", "ano": 2013, "km": 180_000},
        empresa=empresa, llm_client=fake,
        observacoes_pdf="OBSERVAÇÃO_TESTE_UNICA_12345",
    )

    prompt = fake.prompts_vistos[0].lower()
    assert "fiesta" in prompt
    assert "2013" in prompt
    assert "uberlândia" in prompt or "mg" in prompt  # região
    assert "estrutural" in prompt  # severidade_geral
    assert "coluna_b_esquerda" in prompt  # avaria concreta
    assert "observação_teste_unica_12345" in prompt  # texto das observações
