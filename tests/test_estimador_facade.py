"""Facade estimar(laudo, empresa, llm_client=...) despacha pro ramo certo.

Antes havia duas APIs paralelas: `estimar()` determinístico em um módulo e
`estimar_llm()` em outro — o orquestrador fazia if/else. Agora é callsite único.
"""

from __future__ import annotations

from carros_sa.agents.estimador_reforma import estimar, estimar_deterministico
from carros_sa.models import (
    Avaria,
    CategoriaVeiculo,
    LaudoEstruturado,
    SeveridadeAvaria,
    StatusDocumentacao,
)
from carros_sa.tenancy import carregar_empresa


def _laudo_limpo() -> LaudoEstruturado:
    return LaudoEstruturado(
        avarias=[],
        severidade_geral=SeveridadeAvaria.NENHUMA,
        motor_ok=True,
        documentacao=StatusDocumentacao.OK,
        categoria_veiculo=CategoriaVeiculo.HATCH,
        confidence=0.9,
    )


def _laudo_paralama_leve() -> LaudoEstruturado:
    return LaudoEstruturado(
        avarias=[Avaria(parte="paralama_dianteiro_esquerdo", severidade=SeveridadeAvaria.LEVE)],
        severidade_geral=SeveridadeAvaria.LEVE,
        motor_ok=True,
        documentacao=StatusDocumentacao.OK,
        categoria_veiculo=CategoriaVeiculo.HATCH,
        confidence=0.9,
    )


def test_sem_llm_client_vai_pro_deterministico():
    """Facade com llm_client=None devolve o mesmo que estimar_deterministico."""
    empresa = carregar_empresa("carros_uberlandia")
    laudo = _laudo_paralama_leve()

    via_facade = estimar(laudo, empresa)
    via_direto = estimar_deterministico(laudo, empresa)

    assert via_facade.custo_total == via_direto.custo_total
    assert [i.descricao for i in via_facade.itens] == [i.descricao for i in via_direto.itens]


def test_laudo_limpo_respeita_piso_de_imprevistos():
    """Piso de imprevistos aplicado via facade (vem do config agora)."""
    from carros_sa.config import get_settings

    custo = estimar(_laudo_limpo(), carregar_empresa("carros_uberlandia"))
    assert custo.custo_total == get_settings().reforma_reserva_imprevistos_brl


class _FakeClientReturnsJSON:
    """TextLLMClient stub — devolve JSON ditado sem chamar LLM real."""
    custo_estimado_usd = 0.0

    def __init__(self, payload: dict):
        self._payload = payload

    def generate_json(self, prompt: str) -> dict:  # noqa: D401
        return self._payload


def test_com_llm_client_usa_itens_do_llm():
    empresa = carregar_empresa("carros_uberlandia")
    client = _FakeClientReturnsJSON({
        "itens": [{"descricao": "coluna B esq. soldada + repintura", "custo": 3800}],
        "custo_total": 3800,
        "range_min": 3000,
        "range_max": 4500,
        "confidence": 0.8,
        "justificativa": "peça estrutural com repintura parcial",
    })

    custo = estimar(
        _laudo_paralama_leve(),
        empresa,
        llm_client=client,
        lote_info={"marca": "Fiesta", "modelo": "Fiesta 1.6", "ano": 2013, "km": 150000, "lance_atual": 12000},
    )

    assert custo.custo_total == 3800
    assert any("coluna" in i.descricao.lower() for i in custo.itens)
    assert custo.racional is not None


def test_com_llm_client_falhando_cai_no_deterministico():
    """LLM estourando → facade ainda devolve custo válido via ramo determinístico."""

    class _BrokenClient:
        custo_estimado_usd = 0.0

        def generate_json(self, prompt: str) -> dict:
            raise RuntimeError("LLM fora do ar")

    empresa = carregar_empresa("carros_uberlandia")
    laudo = _laudo_paralama_leve()

    via_facade = estimar(
        laudo, empresa,
        llm_client=_BrokenClient(),
        lote_info={"marca": "Ford", "modelo": "Fiesta", "ano": 2013},
    )
    via_direto = estimar_deterministico(laudo, empresa)

    assert via_facade.custo_total == via_direto.custo_total
