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
    """Mesmo lote, mas simulando que o leilão é em Uberlândia (frete 0).

    Após calibração (Bloco A): margem.base 0.25, custo_op_fixo 2523 (decomposto),
    taxa_leilao_pct 0.0156 (HH-4: calibrado em N=1 Fusion R$867/R$55.500).
    """
    empresa = carregar_empresa("carros_uberlandia")
    lote_local = gol_2015_lote.model_copy(update={
        "origem_cidade": "Uberlândia", "origem_uf": "MG",
    })
    frete = _frete("Uberlândia", "MG", "Uberlândia", "MG", 0, 0)

    av = precificar(lote_local, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma, frete, empresa)

    # Checks (refactor FIPE-only 2026-05-08):
    # preco_giro = FIPE × f_km × 0.95 = 28000 × 1.0 × 0.95 = 26_600
    # webmotors_mediana=25000 da fixture vai pro display, não pro cálculo.
    assert av.preco_giro == 26_600
    assert av.frete_incluso == 0
    assert av.reforma_estimada == 3_500

    # HH-4: taxa percentual de 1.56% sobre o lance (N=1 Fusion, recalibrar c/ mais dados)
    assert av.taxas_leilao == 275   # int(preco_max * 0.0156) = int(17641 * 0.0156)
    assert empresa.taxa_leilao_fixa == 0
    assert empresa.taxa_leilao_pct == 0.0156

    # Custo op decomposto soma R$ 2.523 (380+450+1500+120+73)
    assert empresa.custo_op_fixo == 2_523

    # Margem: fator_risco ≈ 1.295, fator_liquidez ≈ 1.431
    # margem_aplicada ≈ 0.25 * 1.295 * 1.431 ≈ 0.4632
    # margem_reais = int(26600 * 0.4632) = 12321
    # bruto_alvo = 26600 - 3500 - 0 - 2523 - 12321 = 8256
    # preco_alvo = 8256 / 1.0156 = 8126 (taxa menor q 999 → preco_alvo sobe vs modelo antigo)
    assert 7_800 < av.preco_alvo < 8_400
    assert av.preco_max > av.preco_alvo           # margem mínima é menos restritiva


# =============================================================================
# Gol 2015 — Goiânia (frete R$ 1400)
# =============================================================================

def test_gol_2015_em_goiania_com_frete(
    gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma
):
    """Frete R$ 1400 + transferência interestadual R$ 580 derrubam preco_alvo (taxa pct 1.56%)."""
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Goiânia", "GO", "Uberlândia", "MG", 400, 1_400)

    av = precificar(gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma, frete, empresa)

    # Lote vem de GO (origem_uf="GO") no pátio MG → custo_op_para_lote soma
    # transferencia_interestadual=580 sobre o agregado de 2523. Workstream HH (2026-05-11).
    # preco_giro = FIPE × 0.95 = 26600 (refactor FIPE-only)
    # HH-4: taxa 1.56% (pct, N=1 Fusion). bruto_alvo = 26600 - 3500 - 1400 - 3103 - 12321 = 6276
    # preco_alvo = 6276 / 1.0156 = 6176 (taxa <999 em lotes baratos → preco_alvo sobe)
    assert av.frete_incluso == 1_400
    assert 5_800 < av.preco_alvo < 6_500

    # preco_max = (26600 - 3500 - 1400 - 3103 - 2660) / 1.0156 = 15692
    # Lance atual R$ 15k está DENTRO do preco_max agora (15000 < 15692).
    # HH-4 inverte o resultado vs taxa fixa: pct=1.56% sobre preco_max=15692
    # é R$245, muito menos que os R$999 fixos que tornavam o lote inviável.
    # Com taxa menor, preco_max sobe e o lance de R$15k volta a caber.
    assert gol_2015_lote.lance_atual < av.preco_max


# =============================================================================
# Multi-tenancy: mesmo lote, 2 empresas, margens/pátios diferentes
# =============================================================================

def test_multi_empresa_mesmo_lote_rankings_divergem(
    gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma
):
    """Mesmo Gol em Goiânia: empresa_fake_sp é mais exigente que Uberlândia.

    A semântica "mais exigente" se manifesta no `preco_max` (teto absoluto),
    NÃO necessariamente no `preco_alvo`: com o cap de 0.50 em margem_aplicada,
    empresas com bounds altos (SP sai 0.625 sem cap → 0.50 com cap) saturam e
    deixam de diferenciar pelo eixo da margem calculada. O eixo confiável é o
    `minima_absoluta` (Uber 0.10 vs SP 0.18) que entra no `preco_max`.

    - Margem base 30% (vs 25% de Uberlândia)
    - minima_absoluta 0.18 (vs 0.10) → preco_max mais baixo em SP
    - Pátio em SP está mais longe de Goiânia que Uberlândia (frete maior)
    - taxa_leilao_pct 8% (vs 1.56% de Uberlândia após HH-4)
    - bounds de risco/liquidez mais largos (operação mais exigente)

    Usa laudo/mercado SUAVIZADOS (severidade leve, mercado normal) pra que
    nenhuma das empresas atinja o cap de margem (`_MARGEM_TETO=0.50`). Sem isso,
    SP saturaria em 50% pelos bounds mais largos (2.2 × 2.0) e o teste só
    estaria comparando configs idênticas no teto.
    """
    # Laudo "ótimo" + mercado raso pra que SP (bounds altos) não sature em 50%.
    # Base SP = 0.30 → fator_risco × fator_liquidez precisam ficar < 1.67.
    laudo_brando = gol_2015_laudo.model_copy(update={
        "severidade_geral": SeveridadeAvaria.NENHUMA,
        "avarias": [],
        "documentacao": StatusDocumentacao.OK,
        "motor_ok": True,
        "confidence": 1.0,
    })
    mercado_brando = gol_2015_mercado.model_copy(update={
        "n_anuncios_competidores": 5,
        "dias_giro_estimado": 30,
    })

    empresa_uber = carregar_empresa("carros_uberlandia")
    empresa_sp = carregar_empresa("empresa_fake_sp")

    # Frete de Goiânia pra Uberlândia (~400km) é mais barato que pra SP (~900km)
    frete_uber = _frete("Goiânia", "GO", "Uberlândia", "MG", 400, 1_400)
    frete_sp = _frete("Goiânia", "GO", "São Paulo", "SP", 900, 2_100)

    av_uber = precificar(gol_2015_lote, laudo_brando, mercado_brando, gol_2015_reforma, frete_uber, empresa_uber)
    av_sp = precificar(gol_2015_lote, laudo_brando, mercado_brando, gol_2015_reforma, frete_sp, empresa_sp)

    # Sanidade: nenhuma das duas atingiu o cap (senão a comparação vira ruído)
    assert av_uber.margem_aplicada < 0.50
    assert av_sp.margem_aplicada < 0.50

    # SP é mais exigente: margem base 0.30 vs 0.25 → margem aplicada maior
    # (com brando, fatores são similares e a base é o que diferencia).
    assert av_sp.margem_aplicada > av_uber.margem_aplicada
    # SP oferece MENOS pelo mesmo lote no preco_max: minima_absoluta 0.18 vs 0.10
    # + frete maior + taxa percentual sobre lance, todos puxam o teto pra baixo.
    assert av_sp.preco_max < av_uber.preco_max
    # Nota: preco_alvo deixou de ser eixo monotônico de "exigência" após o workstream HH
    # (2026-05-11) — empresa_fake_sp usa YAML legacy sem `custos_operacionais` decomposto,
    # então não aplica transferencia_interestadual (custo_op_para_lote devolve fixo).
    # Em Uberlândia (decomposto + transferencia=580), lote vindo de GO ganha R$ 580
    # extra no custo_op → preco_alvo cai. Resultado: Uber preco_alvo (~9.9k) fica
    # ABAIXO do SP (~10k) mesmo com Uber sendo "menos exigente" pelos demais eixos.
    # `preco_max` e `margem_aplicada` continuam refletindo o ranking de exigência
    # corretamente porque a transferência é diluída no teto (mais lance permitido =
    # mesma margem mínima absoluta). Mantemos assertivas sobre esses dois eixos.
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


def test_tabela_frete_zero_km_e_gratuito():
    """Lote na mesma cidade do pátio → frete R$ 0 pra qualquer categoria.

    O comprador busca o carro pessoalmente; sem logística contratada.
    """
    empresa = carregar_empresa("carros_uberlandia")
    for categoria in (CategoriaVeiculo.HATCH, CategoriaVeiculo.SUV, CategoriaVeiculo.PICAPE):
        assert empresa.frete_para(0, categoria) == 0


# =============================================================================
# Integração FIPE + Tabela Auto Avaliar
# =============================================================================

def test_sem_auto_avaliar_mantem_comportamento_fipe_only(
    gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma,
):
    """Backward compat: mercado sem auto_avaliar_ref usa só FIPE (preco_giro_aa=None)."""
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Goiânia", "GO", empresa.patio.cidade, empresa.patio.uf, 420, 1_400)
    av = precificar(gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma, frete, empresa)
    assert av.preco_giro_fipe == av.preco_giro    # consolidado = FIPE quando AA ausente
    assert av.preco_giro_aa is None


def test_auto_avaliar_ref_nao_afeta_preco_giro_fipe_only(
    gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma,
):
    """Refactor FIPE-only (2026-05-08): auto_avaliar_ref e webmotors_mediana
    NÃO entram mais no cálculo do preco_giro — viraram referência informativa.
    Preserva o sinal em SinalMercado pra display, mas preco_giro_aa fica None.
    """
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Goiânia", "GO", empresa.patio.cidade, empresa.patio.uf, 420, 1_400)

    # Mesmo lote com 3 valores diferentes de auto_avaliar_ref → preco_giro idêntico
    sem_aa = gol_2015_mercado
    aa_baixo = gol_2015_mercado.model_copy(update={"auto_avaliar_ref": 22_000})
    aa_alto = gol_2015_mercado.model_copy(update={"auto_avaliar_ref": 30_000})

    av_sem = precificar(gol_2015_lote, gol_2015_laudo, sem_aa, gol_2015_reforma, frete, empresa)
    av_baixo = precificar(gol_2015_lote, gol_2015_laudo, aa_baixo, gol_2015_reforma, frete, empresa)
    av_alto = precificar(gol_2015_lote, gol_2015_laudo, aa_alto, gol_2015_reforma, frete, empresa)

    # FIPE × f_km × 0.95 = 28000 × 1.0 × 0.95 = 26_600 (idêntico nos 3 cenários)
    assert av_sem.preco_giro == 26_600
    assert av_baixo.preco_giro == 26_600
    assert av_alto.preco_giro == 26_600
    # preco_giro_aa sempre None (campo de referência, não usado no cálculo)
    assert av_sem.preco_giro_aa is None
    assert av_baixo.preco_giro_aa is None
    assert av_alto.preco_giro_aa is None
    # webmotors_mediana segue persistido pra display
    assert av_sem.webmotors_mediana == 25_000


def test_auto_avaliar_ref_zero_rejeitado_pela_validacao():
    """SinalMercado garante que auto_avaliar_ref se presente é positivo."""
    with pytest.raises(Exception):
        SinalMercado(
            fipe=28_000,
            webmotors_mediana=25_000,
            webmotors_p25=24_000,
            n_anuncios_competidores=80,
            dias_giro_estimado=25,
            auto_avaliar_ref=0,
        )


# =============================================================================
# Bloco A — Calibração com dados reais do operador (Polo Track 2024)
# =============================================================================

def test_taxa_leilao_fixa_auto_avaliar_polo_track_real():
    """Gold do Polo Track 2024 real comprado pelo Caio em 2025-11.

    Dados do acerto de contas:
      - Compra: R$ 52.200 (Auto Avaliar, Uberlândia, mesma cidade do pátio)
      - FIPE: R$ 69.400 / vendido por R$ 69.400 (FIPE cheia)
      - Custos não-veículo: R$ 6.235 (incluso R$ 1.500 não-recorrente de chave)
      - Recorrente real: R$ 4.735 (despachante 380 + cegonha 1200 + marketing 1513
        + higienização 450 + laudo 120 + combustível 73 + AA 999)
      - Reforma: zero (Polo novo, sem avaria)

    O sistema deve sugerir um preço-alvo coerente com a realidade — a R$ 52.200
    pago, sobra ~R$ 17.200 de margem bruta = 24,8% sobre venda, dentro do alvo
    calibrado de 25%.
    """
    empresa = carregar_empresa("carros_uberlandia")

    # Mesma cidade → frete 0 (comprador busca pessoalmente, conforme tenancy)
    lote_polo = LoteRaw(
        lote_id="AA-POLO-2024",
        leilao="auto_arremate",
        url="https://autoarremate.com.br/lotes/polo",
        marca="VW",
        modelo="Polo Track",
        ano=2024,
        km=80_000,
        lance_atual=50_000,
        origem_cidade="Uberlândia",
        origem_uf="MG",
    )
    laudo_polo = LaudoEstruturado(
        avarias=[],
        severidade_geral=SeveridadeAvaria.NENHUMA,
        motor_ok=True,
        documentacao=StatusDocumentacao.OK,
        categoria_veiculo=CategoriaVeiculo.HATCH,
        confidence=0.95,
    )
    mercado_polo = SinalMercado(
        fipe=69_400,
        webmotors_mediana=68_000,
        webmotors_p25=66_000,
        n_anuncios_competidores=20,
        dias_giro_estimado=30,
    )
    reforma_zero = CustoReforma(itens=[], custo_total=0, range_min=0, range_max=0)
    frete_zero = _frete("Uberlândia", "MG", "Uberlândia", "MG", 0, 0)

    av = precificar(lote_polo, laudo_polo, mercado_polo, reforma_zero, frete_zero, empresa)

    # HH-4: taxa 1.56% sobre preco_max (N=1 Fusion; era R$999 fixo antes)
    # int(55941 * 0.0156) = 872
    assert av.taxas_leilao == 872
    # Preço de giro: FIPE × f_km × 0.95 = 69400 × 1.0 × 0.95 = 65_930 (refactor
    # FIPE-only de 2026-05-08; antes era webmotors_mediana=68_000).
    assert av.preco_giro == 65_930
    # Custos op = R$ 2.523 (decomposto em config)
    assert empresa.custo_op_fixo == 2_523
    # Preço-alvo deve estar abaixo de R$ 52.200 (o que o Caio pagou) — sistema
    # exige margem maior que a operação real, então alvo conservador < pago real
    assert av.preco_alvo < 52_200
    # Mas razoável (não absurdo) — pelo menos 30% do preco_giro
    assert av.preco_alvo > 20_000


def test_taxa_leilao_pct_e_fixa_combinadas(
    gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma
):
    """Empresa com 2% pct + R$ 200 fixos (leilão hipotético misto).

    Verifica que a fórmula soma ambos: taxas = preco_max*0.02 + 200.
    """
    from carros_sa.tenancy import EmpresaConfig, MargemConfig, PatioConfig

    empresa_mista = EmpresaConfig(
        empresa_id="teste_misto",
        nome="Teste Taxa Mista",
        patio=PatioConfig(cidade="Uberlândia", uf="MG"),
        margem=MargemConfig(base=0.20, minima_absoluta=0.10),
        tabela_frete={"0-300": {"hatch": 800, "sedan": 800, "suv": 1200,
                                "utilitario": 1500, "picape": 1500, "outro": 1200}},
        categorias_aceitas=["hatch", "sedan", "suv", "picape"],
        taxa_leilao_pct=0.02,
        taxa_leilao_fixa=200,
        custo_op_fixo=500,
    )
    frete = _frete("Uberlândia", "MG", "Uberlândia", "MG", 0, 0)

    av = precificar(gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma, frete, empresa_mista)

    # taxas = preco_max * 0.02 + 200
    assert av.taxas_leilao == int(av.preco_max * 0.02) + 200


def test_custos_op_decompostos_somam_total():
    """CustosOperacionais.total = soma de todos os componentes."""
    from carros_sa.tenancy import CustosOperacionais

    c = CustosOperacionais(
        despachante=380,
        higienizacao=450,
        marketing_medio=1500,
        laudo_cautelar=120,
        combustivel=73,
        outros=100,
    )
    assert c.total == 2_623

    # Vazio = zero
    assert CustosOperacionais().total == 0


def test_yaml_antigo_so_custo_op_fixo_funciona():
    """empresa_fake_sp.yaml ainda usa formato legado (sem custos_operacionais).

    Garante retrocompat: int agregado funciona sem o decomposto novo.
    """
    empresa_sp = carregar_empresa("empresa_fake_sp")
    # SP YAML não tem `custos_operacionais` — fica None
    assert empresa_sp.custos_operacionais is None
    # custo_op_fixo lido direto do int legado
    assert empresa_sp.custo_op_fixo == 650
    # taxa_leilao_fixa default 0 (não tem no YAML legado)
    assert empresa_sp.taxa_leilao_fixa == 0
    assert empresa_sp.taxa_leilao_pct == 0.08


# =============================================================================
# Reforma racional — propaga do CustoReforma pra Avaliacao
# =============================================================================

def test_reforma_racional_montado_a_partir_dos_itens(
    gol_2015_lote, gol_2015_laudo, gol_2015_mercado
):
    """Precificador deve preencher avaliacao.reforma_racional com sumário dos itens."""
    reforma = CustoReforma(
        itens=[
            ItemReforma(descricao="Funilaria porta dianteira", custo=2_000),
            ItemReforma(descricao="Pintura + polimento", custo=1_500),
        ],
        custo_total=3_500, range_min=2_800, range_max=4_500,
    )
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Uberlândia", "MG", "Uberlândia", "MG", 0, 0)

    av = precificar(gol_2015_lote, gol_2015_laudo, gol_2015_mercado, reforma, frete, empresa)

    assert av.reforma_racional is not None
    assert "Funilaria porta dianteira" in av.reforma_racional
    assert "Pintura" in av.reforma_racional
    assert "2000" in av.reforma_racional or "2.000" in av.reforma_racional
    assert "1500" in av.reforma_racional or "1.500" in av.reforma_racional


def test_reforma_racional_usa_justificativa_do_llm_quando_disponivel(
    gol_2015_lote, gol_2015_laudo, gol_2015_mercado
):
    """Quando CustoReforma vem com racional (do LLM), Avaliacao herda esse texto."""
    reforma = CustoReforma(
        itens=[ItemReforma(descricao="Reparo genérico", custo=4_000)],
        custo_total=4_000, range_min=3_000, range_max=5_000,
        racional="Gol 2014 com motor suspeito — retífica parcial VW AP, mão-de-obra local.",
    )
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Uberlândia", "MG", "Uberlândia", "MG", 0, 0)

    av = precificar(gol_2015_lote, gol_2015_laudo, gol_2015_mercado, reforma, frete, empresa)

    assert av.reforma_racional == (
        "Gol 2014 com motor suspeito — retífica parcial VW AP, mão-de-obra local."
    )


def test_reforma_racional_sem_itens_e_sem_llm_fica_none(
    gol_2015_lote, gol_2015_laudo, gol_2015_mercado
):
    """Reforma vazia (nenhum item, sem racional LLM) → reforma_racional=None (mostra '—' na planilha)."""
    reforma = CustoReforma(itens=[], custo_total=0, range_min=0, range_max=0)
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Uberlândia", "MG", "Uberlândia", "MG", 0, 0)

    av = precificar(gol_2015_lote, gol_2015_laudo, gol_2015_mercado, reforma, frete, empresa)

    assert av.reforma_racional is None


# =============================================================================
# Ajuste por km do lote vs km mediana do mercado (ajuste_km.py)
# =============================================================================

def test_ajuste_km_lote_com_km_alta_reduz_preco_alvo(
    gol_2015_lote, gol_2015_laudo, gol_2015_reforma
):
    """Mesmo Gol, mesmo mercado, mas km do lote >> km mediana: preço-alvo cai.

    Sem webmotors_km_mediana → f_km=1.0, preco_giro cheio.
    Com webmotors_km_mediana=50k e lote.km=150k → f_km ≈ 0.70 → clamp 0.75 →
    preco_giro cai de FIPE×0.95 pra FIPE×0.95×0.75 → preço-alvo várias milhares menor.
    """
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Uberlândia", "MG", "Uberlândia", "MG", 0, 0)

    # Baseline: sem webmotors_km_mediana → f_km=1.0
    mercado_sem_km = SinalMercado(
        fipe=28_000, webmotors_mediana=25_000, webmotors_p25=24_000,
        n_anuncios_competidores=80, dias_giro_estimado=25,
    )
    lote_km_alta = gol_2015_lote.model_copy(update={"km": 150_000})
    av_baseline = precificar(
        lote_km_alta, gol_2015_laudo, mercado_sem_km, gol_2015_reforma, frete, empresa,
    )

    # Com km mediana de mercado (50k) muito abaixo da km do lote (150k) → f_km clamp 0.75
    mercado_com_km = mercado_sem_km.model_copy(update={"webmotors_km_mediana": 50_000})
    av_km_alta = precificar(
        lote_km_alta, gol_2015_laudo, mercado_com_km, gol_2015_reforma, frete, empresa,
    )

    # preco_giro aplicado com f_km=0.75 → FIPE × 0.95 × 0.75 = 28000 × 0.7125 = 19_950
    # (refactor FIPE-only 2026-05-08; antes: 25000 × 0.75 = 18_750)
    assert av_km_alta.preco_giro == 19_950
    # preço-alvo cai proporcionalmente (sem sair negativo)
    assert av_km_alta.preco_alvo < av_baseline.preco_alvo
    # justificativa exibe f_km quando aplicado
    assert "f_km=0.75" in av_km_alta.justificativa
    assert "km=150000" in av_km_alta.justificativa
    # Baseline não deve mencionar f_km (foi no-op 1.0)
    assert "f_km=" not in av_baseline.justificativa


def test_ajuste_km_lote_com_km_baixa_eleva_preco_alvo(
    gol_2015_lote, gol_2015_laudo, gol_2015_reforma
):
    """Lote com km abaixo da mediana → f_km > 1 → preço-alvo sobe."""
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Uberlândia", "MG", "Uberlândia", "MG", 0, 0)

    mercado_sem_km = SinalMercado(
        fipe=28_000, webmotors_mediana=25_000, webmotors_p25=24_000,
        n_anuncios_competidores=80, dias_giro_estimado=25,
    )
    # km=40k, mediana=80k → delta=+0.5 → fator=1.15 (cap)
    lote_km_baixa = gol_2015_lote.model_copy(update={"km": 40_000})
    mercado_com_km = mercado_sem_km.model_copy(update={"webmotors_km_mediana": 80_000})

    av_sem_ajuste = precificar(
        lote_km_baixa, gol_2015_laudo, mercado_sem_km, gol_2015_reforma, frete, empresa,
    )
    av_com_ajuste = precificar(
        lote_km_baixa, gol_2015_laudo, mercado_com_km, gol_2015_reforma, frete, empresa,
    )

    # preco_giro: FIPE × 0.95 × 1.15 = 28000 × 1.0925 = 30_590
    # (refactor FIPE-only 2026-05-08; antes: 25000 × 1.15 = 28_750)
    assert av_com_ajuste.preco_giro == 30_590
    # preço-alvo sobe com f_km > 1
    assert av_com_ajuste.preco_alvo > av_sem_ajuste.preco_alvo
    assert "f_km=1.15" in av_com_ajuste.justificativa


# =============================================================================
# Cap em margem_aplicada (≤ 0.50) — evita score_roi inflado em lotes péssimos
# =============================================================================

def test_margem_aplicada_capada_em_50pct(gol_2015_lote, gol_2015_reforma):
    """Lote estrutural + mercado ilíquido satura `fator_risco × fator_liquidez`
    nos bounds (2.0 × 1.8 = 3.6 em Uberlândia, base=0.25 → margem teórica 0.90).
    O cap em 50% impede que `score_roi` (= margem/(1-margem) com custos zerados)
    explore artificialmente, fazendo lote péssimo aparecer com Lucro/mês alto.
    Antes do cap: margem ~68%, score_roi ~1.09. Depois: margem 50%, score_roi ≤ 1.
    """
    laudo_pessimo = LaudoEstruturado(
        avarias=[Avaria(parte="coluna_b", severidade=SeveridadeAvaria.ESTRUTURAL)],
        severidade_geral=SeveridadeAvaria.ESTRUTURAL,
        motor_ok=False,
        documentacao=StatusDocumentacao.PENDENCIA_GRAVE,
        categoria_veiculo=CategoriaVeiculo.HATCH,
        confidence=0.5,
    )
    mercado_pessimo = SinalMercado(
        fipe=28_000, webmotors_mediana=25_000, webmotors_p25=22_000,
        n_anuncios_competidores=200, dias_giro_estimado=180,
    )
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Goiânia", "GO", "Uberlândia", "MG", 400, 1_400)

    av = precificar(gol_2015_lote, laudo_pessimo, mercado_pessimo, gol_2015_reforma, frete, empresa)

    # Cap dura é exatamente 50% — fatores saturados deveriam dar margem teórica
    # 0.25 × 2.0 × 1.8 = 0.90, mas o cap segura em 0.50.
    assert av.margem_aplicada == pytest.approx(0.50, abs=1e-6)
    # Score_roi não passa de ~1.0 (fórmula de identidade: capital_alvo encolhe
    # com a margem, então tem teto natural quando margem→0.5).
    assert av.score_roi <= 1.05


def test_margem_aplicada_brando_nao_atinge_cap(gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma):
    """Lote calibrado normal NÃO deve bater no cap — só lotes com fatores saturados.
    Este é o regime típico (margem 25-45%); o cap é o "freio de emergência".
    """
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Goiânia", "GO", "Uberlândia", "MG", 400, 1_400)
    av = precificar(gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma, frete, empresa)
    assert av.margem_aplicada < 0.50  # bem abaixo do teto


# =============================================================================
# Refactor FIPE-only (2026-05-08): preco_giro = FIPE × f_km × 0.95
# =============================================================================
# Antes existiam 3 caps em série (n<5 no avaliador, 1.20×FIPE no precificador,
# 1.05×FIPE no audit) tentando consertar similares poluídos do Auto Avaliar
# (Tiggo 7 vs Tiggo 2, Airtrek vs Outlander, Ka descontinuado vs seminovos
# europeus). Removidos os 2 primeiros — fórmula nova torna `preco_max > FIPE`
# matematicamente inviável (max teórico = FIPE × 1.15 × 0.95 × 0.90 ≈ 0.98×FIPE).
# `webmotors_mediana` continua persistido pra display.

def test_preco_giro_fipe_independe_de_mediana_inflada(gol_2015_lote, gol_2015_laudo, gol_2015_reforma):
    """Mediana de mercado inflada (similares poluídos) não impacta preco_giro
    desde o refactor FIPE-only. Antes: gerava lance > FIPE em adversarial.
    """
    # Cenário adversarial: mediana 168% FIPE (similares de outro modelo entrando),
    # f_km=1.15 (km do lote MUITO abaixo da mediana).
    mercado_inflado = SinalMercado(
        fipe=50_000,
        webmotors_mediana=84_000,  # 168% FIPE — antes do refactor: viraria preco_giro
        webmotors_p25=70_000,
        n_anuncios_competidores=10,
        dias_giro_estimado=60,
        webmotors_km_mediana=100_000,  # vs km_lote=95k → f_km≈1.015
    )
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Goiânia", "GO", "Uberlândia", "MG", 400, 1_400)

    av = precificar(gol_2015_lote, gol_2015_laudo, mercado_inflado, gol_2015_reforma, frete, empresa)

    # FIPE-only: preco_giro_fipe = FIPE × f_km × 0.95, NÃO depende da mediana.
    # 50000 × ~1.015 × 0.95 = ~48_212 (mediana 84k é IGNORADA no cálculo)
    assert av.preco_giro_fipe < int(50_000 * 1.10), (
        f"preco_giro_fipe {av.preco_giro_fipe} acima do esperado pra FIPE-only"
    )
    # Sanidade: preco_max consequente fica BEM abaixo da FIPE (não dispara
    # `Lance Máximo > FIPE × 1.05` no audit).
    assert av.preco_max < int(50_000 * 1.05)
    # Mediana fica persistida pra display (referência informativa)
    assert av.webmotors_mediana == 84_000


def test_preco_giro_fipe_eh_fipe_vezes_fkm_vezes_095(
    gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma,
):
    """Sanidade da fórmula nova: preco_giro_fipe = FIPE × f_km × 0.95.
    """
    empresa = carregar_empresa("carros_uberlandia")
    frete = _frete("Goiânia", "GO", "Uberlândia", "MG", 400, 1_400)
    # FIPE=28k da fixture, sem webmotors_km_mediana → f_km=1.0
    # preco_giro_fipe = 28000 × 1.0 × 0.95 = 26_600
    av = precificar(gol_2015_lote, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma, frete, empresa)
    assert av.preco_giro_fipe == 26_600
    # Não depende mais da webmotors_mediana (que é 25_000 da fixture)
    assert av.preco_giro_fipe != av.webmotors_mediana


# 4 carros reais que o operador reportou no screenshot de 2026-05-08 com
# `Lance Máximo > FIPE`. Todos disparavam adversarial pré-refactor (mediana
# saturada 1.20×FIPE + custos baixos Uberlândia + reforma=0). Guard test
# trava regressão se alguém reintroduzir a mediana no cálculo.
@pytest.mark.parametrize(
    "label, fipe",
    [
        ("Mitsubishi Airtrek 2008", 32_994),
        ("Chery Tiggo 2.0 2015", 41_634),
        ("Ford Ka 1.0 2020", 48_199),
        ("Fiat Argo 1.0 2019", 50_570),
    ],
)
def test_lance_maximo_nunca_excede_fipe_em_uberlandia_sem_dano(
    label, fipe, gol_2015_lote, gol_2015_reforma,
):
    """Caso real do screenshot de 2026-05-08: 4 carros com `Lance Máximo > FIPE`.

    Cenário ADVERSARIAL: lote em Uberlândia (frete=0, mesma cidade pátio),
    sem dano (reforma=0), mediana de mercado inflada (similares poluídos
    do AA). Pré-refactor (mediana × f_km com cap 1.20×FIPE) podia produzir
    Lance Máximo até 101% FIPE. Pós-refactor (FIPE × f_km × 0.95) o teto
    teórico cai pra ~98% FIPE mesmo no pior caso (f_km=1.15, custos zero).
    """
    empresa = carregar_empresa("carros_uberlandia")
    laudo = LaudoEstruturado(
        avarias=[],
        severidade_geral=SeveridadeAvaria.NENHUMA,
        motor_ok=True,
        documentacao=StatusDocumentacao.OK,
        categoria_veiculo=CategoriaVeiculo.HATCH,
        confidence=0.95,
    )
    # Mediana 130% FIPE (cenário adversarial: similares poluídos), f_km=1.15
    # (km do lote << km mediana). Pré-refactor: preco_giro = 1.30×1.15×FIPE = 1.495×FIPE
    # → preco_max ~1.07×FIPE (acima da FIPE). Pós-refactor: ignora a mediana.
    mercado = SinalMercado(
        fipe=fipe,
        webmotors_mediana=int(fipe * 1.30),
        webmotors_p25=int(fipe * 1.10),
        n_anuncios_competidores=10,
        dias_giro_estimado=60,
        webmotors_km_mediana=100_000,
    )
    lote_local = gol_2015_lote.model_copy(update={
        "origem_cidade": "Uberlândia", "origem_uf": "MG", "km": 10_000,
    })
    reforma_zero = CustoReforma(itens=[], custo_total=0, range_min=0, range_max=0)
    frete_zero = _frete("Uberlândia", "MG", "Uberlândia", "MG", 0, 0)

    av = precificar(lote_local, laudo, mercado, reforma_zero, frete_zero, empresa)

    # Invariante pós-refactor FIPE-only: preco_max < FIPE em qualquer cenário.
    # 1.05×FIPE é o threshold do audit; aqui exigimos margem confortável.
    assert av.preco_max < int(fipe * 1.00), (
        f"{label}: preco_max R${av.preco_max} >= FIPE R${fipe} — refactor FIPE-only deve bloquear"
    )


# =============================================================================
# Transferência interestadual — workstream HH (2026-05-11)
# =============================================================================
# Calibração derivada da compra real Fusion Titanium 2.0 GTDI 14/14 AWD: lote
# arrematado em São Paulo, pátio em Uberlândia/MG. DETRAN-MG cobrou R$ 580 de
# segundo emplacamento (mudança de estado) — custo não modelado antes deste
# workstream. Aplicado por-lote em `EmpresaConfig.custo_op_para_lote` quando
# `lote.origem_uf != patio.uf`.

class TestTransferenciaInterestadual:
    """Cobertura do custo extra de DETRAN quando origem.uf != patio.uf."""

    def test_total_recorrente_nao_inclui_transferencia(self):
        """`CustosOperacionais.total` continua somando só itens recorrentes.

        Transferência é condicional ao lote; entra via `custo_op_para_lote`,
        não no agregado da empresa. Sem essa separação, o `custo_op_fixo`
        passaria a contar o extra DETRAN como recorrente — superestimando
        capital em 100% dos lotes locais (origem.uf == patio.uf).
        """
        from carros_sa.tenancy import CustosOperacionais

        c = CustosOperacionais(
            despachante=380, higienizacao=450, marketing_medio=1500,
            laudo_cautelar=120, combustivel=73,
            transferencia_interestadual=580,
        )
        assert c.total == 2_523  # 380+450+1500+120+73, SEM os 580

    def test_custo_op_para_lote_acrescenta_em_origem_diferente_uf(self, gol_2015_lote):
        """Lote vindo de GO no pátio MG (Uberlândia) ganha R$ 580 extra.

        Cenário concreto: replica a compra Fusion SP→MG (mesma estrutura).
        gol_2015_lote tem origem_uf="GO", patio é Uberlândia/MG.
        """
        empresa = carregar_empresa("carros_uberlandia")
        custo = empresa.custo_op_para_lote(gol_2015_lote)
        # custo_op_fixo = 2_523 (recorrentes) + 580 (transferência) = 3_103
        assert custo == 2_523 + 580 == 3_103

    def test_custo_op_para_lote_nao_acrescenta_em_mesma_uf(self, gol_2015_lote):
        """Lote MG no pátio MG não soma transferência — pátio local."""
        empresa = carregar_empresa("carros_uberlandia")
        lote_mg = gol_2015_lote.model_copy(update={"origem_uf": "MG"})
        assert empresa.custo_op_para_lote(lote_mg) == 2_523

    def test_custo_op_para_lote_origem_uf_none_devolve_fixo(self, gol_2015_lote):
        """Lote com origem_uf=None (cadastro incompleto) não é penalizado.

        Defensivo: melhor sub-estimar do que cobrar transferência fantasma
        em lote mal-cadastrado. Operador vê o lote passar pelo modelo
        normal e decide manualmente se vai transferir.
        """
        empresa = carregar_empresa("carros_uberlandia")
        lote_sem_uf = gol_2015_lote.model_copy(update={"origem_uf": None})
        assert empresa.custo_op_para_lote(lote_sem_uf) == 2_523

        lote_uf_vazio = gol_2015_lote.model_copy(update={"origem_uf": ""})
        assert empresa.custo_op_para_lote(lote_uf_vazio) == 2_523

    def test_yaml_legado_sem_custos_operacionais_funciona(self, gol_2015_lote):
        """empresa_fake_sp usa formato antigo (`custo_op_fixo` int puro).

        Sem `custos_operacionais` decomposto, `custo_op_para_lote` devolve
        sempre o int legado — independente da origem do lote. Retrocompat
        total: configs antigas não exigem migração pra continuar rodando.
        """
        empresa_sp = carregar_empresa("empresa_fake_sp")
        assert empresa_sp.custos_operacionais is None
        # Mesmo lote com origem fora de SP — sem campo decomposto, retorna fixo
        assert empresa_sp.custo_op_para_lote(gol_2015_lote) == empresa_sp.custo_op_fixo

    def test_precificador_aplica_transferencia_no_capital_alvo(
        self, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma
    ):
        """End-to-end: 2 lotes idênticos exceto pela UF de origem.

        Lote MG (local) vs lote SP (interestadual). Capital do segundo é
        R$ 580 maior → preço-alvo do segundo é R$ 580 menor (espaço pra
        margem encolhe). Garante que o custo extra é propagado pela
        equação do precificador, não só pelo método de tenancy.
        """
        empresa = carregar_empresa("carros_uberlandia")
        frete = _frete("Uberlândia", "MG", "Uberlândia", "MG", 0, 0)

        lote_local = LoteRaw(
            lote_id="AA-MG-1", leilao="auto_arremate",
            url="https://autoarremate.com.br/lotes/MG-1",
            marca="Volkswagen", modelo="Gol", ano=2015, km=95_000,
            lance_atual=15_000,
            fim_em=datetime(2026, 5, 1, 14, 0),
            fotos_urls=[], laudo_texto="",
            origem_cidade="Uberlândia", origem_uf="MG",
        )
        lote_interestadual = lote_local.model_copy(update={
            "lote_id": "AA-SP-1", "origem_cidade": "São Paulo", "origem_uf": "SP",
        })

        av_local = precificar(lote_local, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma, frete, empresa)
        av_inter = precificar(lote_interestadual, gol_2015_laudo, gol_2015_mercado, gol_2015_reforma, frete, empresa)

        # Tudo idêntico exceto custo_op (interestadual +580 → bruto -580).
        # HH-4: taxa_pct=0.0156 → divisão por 1.0156, então diferença
        # no lance = 580 / 1.0156 ≈ 571 (não mais 580 como na taxa fixa).
        diff_alvo = av_local.preco_alvo - av_inter.preco_alvo
        diff_max  = av_local.preco_max  - av_inter.preco_max
        assert 560 < diff_alvo < 580, (
            f"local R${av_local.preco_alvo} vs interestadual R${av_inter.preco_alvo} — "
            f"esperava ≈571 (580/1.0156), obtido {diff_alvo}"
        )
        assert 560 < diff_max < 580, (
            f"esperava ≈571 no preco_max, obtido {diff_max}"
        )
