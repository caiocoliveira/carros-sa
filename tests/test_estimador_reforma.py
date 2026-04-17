"""Gold tests do EstimadorReforma com o laudo real do Fiesta 21854782.

Usa a fixture de visão já gravada (tests/fixtures/21854782_visual_gemini.json)
para reconstruir o LaudoEstruturado offline e validar:

  1. 2 colunas reparadas (B esq. + C esq.) → 2 itens "coluna" GRAVE
  2. severidade_geral ESTRUTURAL → adicional fixo de chassi
  3. custo_total elevado (>5k) com range coerente (±25%)
  4. Multi-tenancy: SP cobra mais que MG na mesma reforma
"""

from pathlib import Path

import pytest

from carros_sa.agents.estimador_reforma import (
    _familia_de,
    carregar_tabela,
    estimar,
)
from carros_sa.agents.extrator_laudo import extrair_laudo
from carros_sa.models import (
    Avaria,
    CategoriaVeiculo,
    LaudoEstruturado,
    SeveridadeAvaria,
    StatusDocumentacao,
)
from carros_sa.tenancy import carregar_empresa

PDF = Path(__file__).resolve().parent.parent / "data" / "laudos_amostra" / "21854782_fiesta.pdf"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "21854782_visual_gemini.json"


class FixedResponseClient:
    def __init__(self, fixture_path: Path):
        import json
        self._response = json.loads(fixture_path.read_text())
        self.custo_estimado_usd = 0.0

    def classify(self, image_png_bytes: bytes, prompt: str) -> dict:
        return self._response


# =============================================================================
# Família casa por prefixo, prioridade para o mais específico
# =============================================================================

def test_familia_match_basico():
    assert _familia_de("coluna_b_esquerda") == "coluna"
    assert _familia_de("longarina_dianteira_direita") == "longarina"
    assert _familia_de("porta_dianteira_esquerda") == "porta"
    assert _familia_de("paralama_traseiro_direito") == "paralama"
    assert _familia_de("teto_veiculo") == "teto"
    assert _familia_de("painel_frontal") == "painel"


def test_familia_capo_tampa_antes_de_tampa():
    """capo_tampa_motor não pode cair em "tampa" (preço diferente)."""
    assert _familia_de("capo_tampa_motor") == "capo_tampa"
    assert _familia_de("tampa_traseira") == "tampa"


def test_familia_desconhecida_vai_pra_default():
    assert _familia_de("rodopio_sintetico") == "default"


# =============================================================================
# Gold test — Fiesta REPROVADO ESTRUTURAL
# =============================================================================

@pytest.mark.skipif(not PDF.exists() or not FIXTURE.exists(), reason="PDF ou fixture ausente")
def test_estimar_fiesta_reprovado_estrutural_uberlandia():
    laudo = extrair_laudo(PDF, FixedResponseClient(FIXTURE))
    empresa = carregar_empresa("carros_uberlandia")

    custo = estimar(laudo, empresa)

    # 2 colunas GRAVE + 1 adicional estrutural = 3 itens
    assert len(custo.itens) == 3
    descricoes = [i.descricao for i in custo.itens]
    assert any("coluna_b_esquerda" in d for d in descricoes)
    assert any("coluna_c_esquerda" in d for d in descricoes)
    assert any("adicional estrutural" in d for d in descricoes)

    # Cada coluna GRAVE = 3500 (Uberlândia), adicional estrutural = 3000
    # Total = 3500 + 3500 + 3000 = 10000
    assert custo.custo_total == 10000

    # Range ±25%
    assert custo.range_min == 7500
    assert custo.range_max == 12500

    # Custo "elevado" — premissa do critério de aceite no ROADMAP
    assert custo.custo_total > 5000


@pytest.mark.skipif(not PDF.exists() or not FIXTURE.exists(), reason="PDF ou fixture ausente")
def test_estimar_fiesta_multi_empresa_sp_mais_caro_que_mg():
    """Mesmo laudo, empresas diferentes, custos coerentes (SP > MG)."""
    laudo = extrair_laudo(PDF, FixedResponseClient(FIXTURE))
    mg = carregar_empresa("carros_uberlandia")
    sp = carregar_empresa("empresa_fake_sp")

    custo_mg = estimar(laudo, mg)
    custo_sp = estimar(laudo, sp)

    assert custo_sp.custo_total > custo_mg.custo_total
    # SP: 2 × 4400 (coluna grave) + 4000 (adicional estrutural) = 12800
    assert custo_sp.custo_total == 12800


# =============================================================================
# Casos sintéticos — sem dependência de PDF
# =============================================================================

def _laudo_sintetico(
    avarias: list,
    severidade: SeveridadeAvaria = SeveridadeAvaria.NENHUMA,
    motor_ok: bool = True,
) -> LaudoEstruturado:
    return LaudoEstruturado(
        avarias=avarias,
        severidade_geral=severidade,
        motor_ok=motor_ok,
        documentacao=StatusDocumentacao.OK,
        categoria_veiculo=CategoriaVeiculo.HATCH,
        confidence=0.9,
    )


def test_laudo_limpo_aplica_piso_de_imprevistos():
    """Laudo sem avarias ganha apenas o piso de reserva (R$ 1.000) — nenhum
    item de peça, nenhum adicional de motor/estrutural. Premissa operacional:
    qualquer carro consome uns R$ 1k de ajustes menores no pátio mesmo quando
    o laudo vem limpo."""
    from carros_sa.agents.estimador_reforma_llm import MINIMO_RESERVA_IMPREVISTOS
    laudo = _laudo_sintetico([])
    custo = estimar(laudo, carregar_empresa("carros_uberlandia"))
    assert custo.custo_total == MINIMO_RESERVA_IMPREVISTOS
    assert len(custo.itens) == 1
    assert "reserva" in custo.itens[0].descricao.lower()


def test_motor_nao_ok_sem_estrutural_adiciona_pendencia_mecanica():
    laudo = _laudo_sintetico(
        [Avaria(parte="paralama_dianteiro_esquerdo", severidade=SeveridadeAvaria.LEVE)],
        severidade=SeveridadeAvaria.LEVE,
        motor_ok=False,
    )
    custo = estimar(laudo, carregar_empresa("carros_uberlandia"))
    descricoes = [i.descricao for i in custo.itens]
    assert any("motor não conforme" in d for d in descricoes)
    # paralama LEVE = 600 (peça + pintura/acabamento), adicional_motor = 4000
    assert custo.custo_total == 4600


def test_motor_nao_ok_com_estrutural_nao_dobra_adicional():
    """Quando severidade é ESTRUTURAL, motor_ok já é False por design — só
    aplicamos o adicional estrutural, nunca o de motor isolado."""
    laudo = _laudo_sintetico(
        [Avaria(parte="longarina_dianteira_esquerda", severidade=SeveridadeAvaria.GRAVE)],
        severidade=SeveridadeAvaria.ESTRUTURAL,
        motor_ok=False,
    )
    custo = estimar(laudo, carregar_empresa("carros_uberlandia"))
    descricoes = [i.descricao for i in custo.itens]
    assert any("adicional estrutural" in d for d in descricoes)
    assert not any("motor não conforme" in d for d in descricoes)
    # longarina GRAVE = 5000 + adicional_estrutural = 3000
    assert custo.custo_total == 8000


def test_avaria_nenhuma_e_ignorada():
    """Avaria com severidade NENHUMA não vira item; só o piso de imprevistos fica."""
    from carros_sa.agents.estimador_reforma_llm import MINIMO_RESERVA_IMPREVISTOS
    laudo = _laudo_sintetico(
        [Avaria(parte="porta_dianteira_esquerda", severidade=SeveridadeAvaria.NENHUMA)],
    )
    custo = estimar(laudo, carregar_empresa("carros_uberlandia"))
    # Só o item de reserva — avaria NENHUMA foi ignorada
    assert custo.custo_total == MINIMO_RESERVA_IMPREVISTOS
    assert all("reserva" in i.descricao.lower() for i in custo.itens)


def test_carregar_tabela_cache_e_isolado_por_empresa():
    mg = carregar_tabela("carros_uberlandia")
    sp = carregar_tabela("empresa_fake_sp")
    assert mg["empresa_id"] == "carros_uberlandia"
    assert sp["empresa_id"] == "empresa_fake_sp"
    assert sp["pecas"]["coluna"]["grave"] > mg["pecas"]["coluna"]["grave"]
