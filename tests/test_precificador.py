"""Gold tests do Precificador.

Cobre o exemplo do plano (Gol 2015) em 2 cenários: Goiânia (frete R$1400)
e Uberlândia (frete R$0), e roda o MESMO lote em 2 empresas distintas
pra validar isolamento de tenancy (rankings divergem na direção esperada).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from carros_sa.models import (
    Avaria,
    CategoriaVeiculo,
    CustoLogistico,
    CustoReforma,
    ItemReforma,
    LaudoEstruturado,
    LoteRaw,
    SeveridadeAvaria,
    SinalMercado,
    StatusDocumentacao,
)
from carros_sa.precificador import (
    calcular_fator_liquidez,
    calcular_fator_risco,
    precificar,
)
from carros_sa.tenancy import carregar_empresa


# =============================================================================
# Fixtures — Gol 2015 base
# =============================================================================

@pytest.fixture
def gol_2015_lote() -> LoteRaw:
    return LoteRaw(
        lote_id="AA-12345",
        leilao="auto_arremate",
        url="https://autoarremate.com.br/lotes/12345",
        marca="Volkswagen",
        modelo="Gol",
        ano=2015,
        km=95_000,
        lance_atual=15_000,
        fim_em=datetime(2026, 5, 1, 14, 0),
        fotos_urls=[],
        laudo_texto="Gol 2015, lataria com avarias médias na porta dianteira, motor OK.",
        origem_cidade="Goiânia",
        origem_uf="GO",
    )


@pytest.fixture
def gol_2015_laudo() -> LaudoEstruturado:
    return LaudoEstruturado(
        avarias=[
            Avaria(parte="porta_dianteira_direita", severidade=SeveridadeAvaria.MEDIA, descricao="amassado"),
        ],
        severidade_geral=SeveridadeAvaria.MEDIA,
        motor_ok=True,
        documentacao=StatusDocumentacao.OK,
        categoria_veiculo=CategoriaVeiculo.HATCH,
        confidence=0.85,
    )


@pytest.fixture
def gol_2015_mercado() -> SinalMercado:
    return SinalMercado(
        fipe=28_000,
        webmotors_mediana=25_000,
        webmotors_p25=24_000,
        n_anuncios_competidores=80,
        dias_giro_estimado=25,
    )


@pytest.fixture
def gol_2015_reforma() -> CustoReforma:
    return CustoReforma(
        itens=[ItemReforma(descricao="Funilaria + pintura porta", custo=3_500)],
        custo_total=3_500,
        range_min=2_800,
        range_max=4_500,
    )


def _frete(origem_cidade: str, origem_uf: str, destino_cidade: str, destino_uf: str, km: int, valor: int) -> CustoLogistico:
    return CustoLogistico(
        origem_cidade=origem_cidade,
        origem_uf=origem_uf,
        destino_cidade=destino_cidade,
        destino_uf=destino_uf,
        distancia_km=km,
        categoria_veiculo=CategoriaVeiculo.HATCH,
        frete_estimado=valor,
        fonte_cotacao="tabela_empresa",
    )


# =============================================================================
# Fatores
# =============================================================================

def test_fator_risco_laudo_otimo_fica_baixo(gol_2015_laudo):
    # Versão "perfeita": sem avaria, doc OK, motor OK, confidence 1.0
    laudo = gol_2015_laudo.model_copy(update={
        "severidade_geral": SeveridadeAvaria.NENHUMA,
        "avarias": [],
        "confidence": 1.0,
    })
    f = calcular_fator_risco(laudo, (1.0, 2.0))
    assert f == pytest.approx(1.0, abs=0.01)


def test_fator_risco_laudo_estrutural_fica_alto(gol_2015_laudo):
    laudo = gol_2015_laudo.model_copy(update={
        "severidade_geral": SeveridadeAvaria.ESTRUTURAL,
        "motor_ok": False,
        "documentacao": StatusDocumentacao.PENDENCIA_GRAVE,
        "confidence": 0.5,
    })
    f = calcular_fator_risco(laudo, (1.0, 2.0))
    assert f == pytest.approx(2.0, abs=0.01)  # satura no topo


def test_fator_liquidez_alta_concorrencia_giro_lento(gol_2015_mercado):
    mercado = gol_2015_mercado.model_copy(update={
        "n_anuncios_competidores": 150,
        "dias_giro_estimado": 120,
    })
    f = calcular_fator_liquidez(mercado, (1.0, 1.8))
    assert f == pytest.approx(1.8, abs=0.01)


def test_fator_liquidez_mercado_vazio_e_rapido(gol_2015_mercado):
    mercado = gol_2015_mercado.model_copy(update={
        "n_anuncios_competidores": 0,
        "dias_giro_estimado": 0,
    })
    f = calcular_fator_liquidez(mercado, (1.0, 1.8))
    assert f == pytest.approx(1.0, abs=0.01)


# =============================================================================
# Gol 2015 — Uberlândia (pátio local, frete = 0)
# =============================================================================

def test_gol_2015_em_uberlandia_sem_frete(
    gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma
):
    """Mesmo lote, mas simulando que o leilão é em Uberlândia (frete 0)."""
    empresa = carregar_empresa("carros_uberlandia")
    lote_local = gol_2015_lote.model_copy(update={
        "origem_cidade": "Uberlândia", "origem_uf": "MG",
    })
    frete = _frete("Uberlândia", "MG", "Uberlândia", "MG", 0, 0)

    av = precificar(lote_local, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma, frete, empresa)

    # Checks:
    assert av.preco_giro == 24_000                # min(28000*0.95=26600, 24000) = 24000
    assert av.frete_incluso == 0
    assert av.reforma_estimada == 3_500

    # Taxas agora calculadas sobre o lance máximo (nosso bid), não sobre lance_atual
    # taxas_leilao = preco_max * 8%
    assert av.taxas_leilao == int(av.preco_max * 0.08)

    # Margem: fator_risco ≈ 1.0 + (2.0-1.0) * (0.25 + 0 + 0 + 0.3*0.15) = 1.0 + 0.295 = 1.295
    # fator_liquidez ≈ 1.0 + (1.8-1.0) * ((80/100 + 25/90)/2) = 1.0 + 0.8 * 0.539 ≈ 1.431
    # margem ≈ 0.18 * 1.295 * 1.431 ≈ 0.3336
    # bruto_alvo = 24000 - 3500 - 0 - 500 - 8006 = 11994
    # preco_alvo = 11994 / 1.08 ≈ 11106
    assert 10_500 < av.preco_alvo < 12_500
    assert av.preco_max > av.preco_alvo           # margem mínima é menos restritiva


# =============================================================================
# Gol 2015 — Goiânia (frete R$ 1400)
# =============================================================================

def test_gol_2015_em_goiania_com_frete(
    gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma
):
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Goiânia", "GO", "Uberlândia", "MG", 400, 1_400)

    av = precificar(gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma, frete, empresa)

    # Comparado com Uberlândia, preco_alvo deve cair em ~R$ 1400/1.08 ≈ R$1296 (o frete)
    assert av.frete_incluso == 1_400
    assert 9_000 < av.preco_alvo < 12_000

    # Lance atual (R$ 15k) está bem acima do preco_max → lote caro demais
    assert gol_2015_lote.lance_atual > av.preco_max


# =============================================================================
# Multi-tenancy: mesmo lote, 2 empresas, margens/pátios diferentes
# =============================================================================

def test_multi_empresa_mesmo_lote_rankings_divergem(
    gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma
):
    """Mesmo Gol em Goiânia deve ter preco_alvo MAIS BAIXO pra empresa_fake_sp:
    - Margem base 22% (vs 18% de Uberlândia) → exige mais desconto
    - Pátio em SP está mais longe de Goiânia que Uberlândia (frete maior)
    - custo_op_fixo mais alto
    """
    empresa_uber = carregar_empresa("carros_uberlandia")
    empresa_sp = carregar_empresa("empresa_fake_sp")

    # Frete de Goiânia pra Uberlândia (~400km) é mais barato que pra SP (~900km)
    frete_uber = _frete("Goiânia", "GO", "Uberlândia", "MG", 400, 1_400)
    frete_sp = _frete("Goiânia", "GO", "São Paulo", "SP", 900, 2_100)

    av_uber = precificar(gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma, frete_uber, empresa_uber)
    av_sp = precificar(gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma, frete_sp, empresa_sp)

    # SP deve oferecer MENOS pelo mesmo lote
    assert av_sp.preco_alvo < av_uber.preco_alvo
    # E a margem aplicada deve ser maior em SP
    assert av_sp.margem_aplicada > av_uber.margem_aplicada
    # Consistência do empresa_id
    assert av_uber.empresa_id == "carros_uberlandia"
    assert av_sp.empresa_id == "empresa_fake_sp"


# =============================================================================
# Tabela de frete (tenancy.py)
# =============================================================================

def test_tabela_frete_lookup_por_faixa():
    empresa = carregar_empresa("carros_uberlandia")
    # 150km → faixa 0-300
    assert empresa.frete_para(150, CategoriaVeiculo.HATCH) == 800
    # 500km → faixa 300-600
    assert empresa.frete_para(500, CategoriaVeiculo.HATCH) == 1_400
    # 800km → faixa 600-1000
    assert empresa.frete_para(800, CategoriaVeiculo.SUV) == 2_800


def test_tabela_frete_extrapola_alem_da_maior_faixa():
    empresa = carregar_empresa("carros_uberlandia")
    # 2500km → além da maior faixa (1000-2000); aplica 30% extra em cima
    maior = empresa.frete_para(1_500, CategoriaVeiculo.HATCH)  # 3000 na maior faixa
    extrapolado = empresa.frete_para(2_500, CategoriaVeiculo.HATCH)
    assert extrapolado > maior
    assert extrapolado == int(maior * 1.3)
