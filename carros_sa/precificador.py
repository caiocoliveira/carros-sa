"""Precificador — Python puro, sem LLM.

Fórmula (ver plano):
    preco_giro_fipe  = min(FIPE * 0.95, webmotors_p25)
    preco_giro_aa    = min(auto_avaliar_ref, webmotors_p25)  # só se auto_avaliar_ref
    preco_giro       = min(preco_giro_fipe, preco_giro_aa)   # consolidado, mais conservador
    margem_min       = margem_base * fator_risco * fator_liquidez
    preco_alvo_lance = preco_giro - reforma - taxas - frete - custo_op - margem_min * preco_giro

Fator_risco e fator_liquidez são derivados do laudo + sinal de mercado; bounds
vêm da config da empresa (empresas mais exigentes usam bounds mais altos).

Sobre as duas âncoras:
- FIPE é sempre disponível (API pública). Ajustamos por 5% pq FIPE é varejo.
- Tabela Auto Avaliar só está disponível quando o lote (ou um lote histórico do
  mesmo modelo) trouxe a "ULTIMA AVALIAÇÃO" embutida. Reflete atacado real e
  costuma ser mais baixo que FIPE — daí usarmos o menor dos dois como preço
  de giro consolidado.
"""

from __future__ import annotations

from carros_sa.ajuste_km import fator_km
from carros_sa.models import (
    Avaliacao,
    CustoLogistico,
    CustoReforma,
    LaudoEstruturado,
    LoteRaw,
    SeveridadeAvaria,
    SinalMercado,
    StatusDocumentacao,
)
from carros_sa.tenancy import EmpresaConfig

# =============================================================================
# Derivação de fatores
# =============================================================================

def calcular_fator_risco(laudo: LaudoEstruturado, bounds: tuple[float, float]) -> float:
    """Quanto mais incerto ou grave o laudo, maior o fator (exige mais margem)."""
    lo, hi = bounds
    # Base começa no meio-termo se confidence ~1 e severidade leve
    severidade_pesos = {
        SeveridadeAvaria.NENHUMA: 0.0,
        SeveridadeAvaria.LEVE: 0.1,
        SeveridadeAvaria.MEDIA: 0.25,
        SeveridadeAvaria.GRAVE: 0.55,
        SeveridadeAvaria.ESTRUTURAL: 1.0,
    }
    doc_pesos = {
        StatusDocumentacao.OK: 0.0,
        StatusDocumentacao.PENDENCIA_LEVE: 0.1,
        StatusDocumentacao.PENDENCIA_GRAVE: 0.4,
        StatusDocumentacao.DESCONHECIDO: 0.2,
    }
    motor_penalidade = 0.0 if laudo.motor_ok else 0.3
    confidence_penalidade = (1.0 - laudo.confidence) * 0.3

    peso = (
        severidade_pesos[laudo.severidade_geral]
        + doc_pesos[laudo.documentacao]
        + motor_penalidade
        + confidence_penalidade
    )
    peso = min(peso, 1.0)  # satura em 1
    return lo + (hi - lo) * peso

def calcular_fator_liquidez(mercado: SinalMercado, bounds: tuple[float, float]) -> float:
    """Pouca competição + giro rápido = fator baixo (margem menor já basta)."""
    lo, hi = bounds
    n = mercado.n_anuncios_competidores
    giro = mercado.dias_giro_estimado

    # Normaliza: 0 anúncios = liquidez ótima (0); 100+ = saturado (1)
    peso_competicao = min(n / 100.0, 1.0)
    # 0 dias = ótimo; 90+ dias = péssimo
    peso_giro = min(giro / 90.0, 1.0)

    peso = (peso_competicao + peso_giro) / 2.0
    return lo + (hi - lo) * peso

# =============================================================================
# Precificador principal
# =============================================================================

def precificar(
    lote: LoteRaw,
    laudo: LaudoEstruturado,
    mercado: SinalMercado,
    reforma: CustoReforma,
    frete: CustoLogistico,
    empresa: EmpresaConfig,
) -> Avaliacao:
    """Calcula Avaliacao para o lote dado a config da empresa.

    Não decide se lance atual é aceitável — só retorna o target. Orquestrador
    compara `lote.lance_atual` vs `avaliacao.preco_alvo` pra descartar.
    """
    # 1. Preço de venda âncora — usamos a mediana de mercado como estimativa de
    #    revenda (usuário confirmou que vende próximo da FIPE; mediana fallback=97%).
    #    Quando Webmotors (workstream B) chegar, webmotors_mediana terá dado real.
    #    Auto Avaliar ref: preço de referência da própria plataforma quando disponível.
    #    fator_km calibra a âncora pela km do lote vs km mediana do mercado:
    #    lote com km acima da mediana → fator < 1 → preço-alvo cai.
    f_km = fator_km(lote.km, mercado.webmotors_km_mediana)
    preco_giro_fipe = int(round(mercado.webmotors_mediana * f_km))
    preco_giro_aa: int | None = None
    if mercado.auto_avaliar_ref is not None:
        preco_giro_aa = int(round(
            min(mercado.auto_avaliar_ref, mercado.webmotors_mediana) * f_km
        ))
    # Consolidado = o mais conservador entre os dois (mais baixo preço de giro
    # => preço-alvo de lance também mais baixo => decisão mais cautelosa).
    preco_giro = min(preco_giro_fipe, preco_giro_aa) if preco_giro_aa is not None else preco_giro_fipe

    # 2. Fatores
    fator_risco = calcular_fator_risco(laudo, empresa.fator_risco_bounds)
    fator_liquidez = calcular_fator_liquidez(mercado, empresa.fator_liquidez_bounds)

    # 3. Margem — respeita o piso absoluto da empresa
    margem_calculada = empresa.margem.base * fator_risco * fator_liquidez
    margem_aplicada = max(margem_calculada, empresa.margem.minima_absoluta)

    # 4. Custos fixos (exceto taxas, calculadas abaixo sobre o lance vencedor)
    frete_incluso = frete.frete_estimado
    custo_op = empresa.custo_op_fixo
    taxa_leilao_pct = empresa.taxa_leilao_pct
    taxa_leilao_fixa = empresa.taxa_leilao_fixa

    # 5. Preço máximo (lance máximo aceitável) — solução algébrica.
    #    A taxa do leilão pode ter 2 componentes:
    #      - `taxa_pct`  cobrada como % do lance vencedor (nosso bid)
    #      - `taxa_fixa` R$ fixo independente do lance (ex.: R$ 999 do Auto Avaliar)
    #
    #    Equação:  bid + bid*pct + fixa = preco_giro - reforma - frete - custo_op - margem
    #              bid * (1 + pct) = bruto - fixa
    #              bid = (bruto - fixa) / (1 + pct)
    margem_reais_min = int(preco_giro * empresa.margem.minima_absoluta)
    bruto_max = preco_giro - reforma.custo_total - frete_incluso - custo_op - margem_reais_min
    preco_max = int(max(bruto_max - taxa_leilao_fixa, 0) / (1 + taxa_leilao_pct))
    taxas_leilao_max = int(preco_max * taxa_leilao_pct) + taxa_leilao_fixa

    # 6. Preço-alvo de lance (margem calculada = mais exigente que a mínima)
    margem_reais = int(preco_giro * margem_aplicada)
    bruto_alvo = preco_giro - reforma.custo_total - frete_incluso - custo_op - margem_reais
    preco_alvo = int(max(bruto_alvo - taxa_leilao_fixa, 0) / (1 + taxa_leilao_pct))

    # 7. Score ROI — retorno % esperado se ganharmos pelo preço-ALVO (margem
    #    calibrada por risco/liquidez). Usamos o alvo, NÃO o teto, por dois motivos:
    #      a) ROI baseado no `preco_max` é sempre idêntico entre lotes — cai em
    #         `margem_min / (1 - margem_min)` por construção (o teto é definido
    #         pra deixar exatamente a margem mínima). Vira tautologia, não sinal.
    #      b) O alvo é o "caso esperado" da empresa — quanto pretendemos ganhar
    #         se comprarmos bem. Laudo pior / mercado saturado → fator_risco
    #         ou fator_liquidez maior → margem_aplicada maior → score_roi maior.
    #         Ranking fica de fato informativo.
    taxas_leilao_alvo = int(preco_alvo * taxa_leilao_pct) + taxa_leilao_fixa
    capital_alvo = max(
        preco_alvo + reforma.custo_total + frete_incluso + taxas_leilao_alvo + custo_op,
        1,
    )
    retorno_alvo = preco_giro - capital_alvo
    score_roi = retorno_alvo / capital_alvo

    aa_txt = f" AA_ref={mercado.auto_avaliar_ref}" if mercado.auto_avaliar_ref else ""
    if taxa_leilao_fixa and taxa_leilao_pct:
        taxa_desc = f"{taxa_leilao_pct:.0%}+R${taxa_leilao_fixa}"
    elif taxa_leilao_fixa:
        taxa_desc = f"R${taxa_leilao_fixa} fixo"
    else:
        taxa_desc = f"{taxa_leilao_pct:.0%} do lance max"
    # f_km só aparece na justificativa quando houve ajuste de fato (dados disponíveis).
    km_txt = ""
    if f_km != 1.0:
        km_txt = (
            f" f_km={f_km:.2f} (km={lote.km}, "
            f"km_mercado={mercado.webmotors_km_mediana})"
        )
    justificativa = (
        f"preco_giro=R${preco_giro} (fipe={preco_giro_fipe}"
        f"{f', aa={preco_giro_aa}' if preco_giro_aa is not None else ''}){km_txt} "
        f"(FIPE={mercado.fipe}{aa_txt}, WM_p25={mercado.webmotors_p25}) "
        f"reforma=R${reforma.custo_total} frete=R${frete_incluso} "
        f"taxas≈R${taxas_leilao_max} ({taxa_desc}) op=R${custo_op} "
        f"margem={margem_aplicada:.1%} (risco={fator_risco:.2f}, liq={fator_liquidez:.2f})"
    )

    # Racional da reforma: prioriza a justificativa do LLM; fallback é sumário
    # dos itens (determinístico não tem narrativa). None quando reforma vazia.
    if reforma.racional:
        reforma_racional = reforma.racional
    elif reforma.itens:
        reforma_racional = " · ".join(
            f"{it.descricao} (R${it.custo})" for it in reforma.itens
        )
    else:
        reforma_racional = None

    return Avaliacao(
        lote_id=lote.lote_id,
        empresa_id=empresa.empresa_id,
        preco_alvo=max(preco_alvo, 0),
        preco_max=max(preco_max, 0),
        score_roi=score_roi,
        fator_risco=fator_risco,
        fator_liquidez=fator_liquidez,
        margem_aplicada=margem_aplicada,
        frete_incluso=frete_incluso,
        reforma_estimada=reforma.custo_total,
        taxas_leilao=taxas_leilao_max,
        preco_giro=preco_giro,
        preco_giro_fipe=preco_giro_fipe,
        preco_giro_aa=preco_giro_aa,
        fipe=mercado.fipe,
        webmotors_mediana=mercado.webmotors_mediana,
        dias_giro_estimado=mercado.dias_giro_estimado,
        justificativa=justificativa,
        reforma_racional=reforma_racional,
    )
