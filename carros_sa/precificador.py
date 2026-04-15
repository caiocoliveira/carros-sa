"""Precificador — Python puro, sem LLM.

Fórmula (ver plano):
    preco_giro       = min(FIPE * 0.95, webmotors_p25)
    margem_min       = margem_base * fator_risco * fator_liquidez
    preco_alvo_lance = preco_giro - reforma - taxas - frete - custo_op - margem_min * preco_giro

Fator_risco e fator_liquidez são derivados do laudo + sinal de mercado; bounds
vêm da config da empresa (empresas mais exigentes usam bounds mais altos).
"""

from __future__ import annotations

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
    # 1. Preço de venda âncora: mínimo entre FIPE descontado e Webmotors p25
    preco_giro = min(int(mercado.fipe * 0.95), mercado.webmotors_p25)

    # 2. Fatores
    fator_risco = calcular_fator_risco(laudo, empresa.fator_risco_bounds)
    fator_liquidez = calcular_fator_liquidez(mercado, empresa.fator_liquidez_bounds)

    # 3. Margem — respeita o piso absoluto da empresa
    margem_calculada = empresa.margem.base * fator_risco * fator_liquidez
    margem_aplicada = max(margem_calculada, empresa.margem.minima_absoluta)

    # 4. Custos fixos do deal
    taxas_leilao_base = int(lote.lance_atual * empresa.taxa_leilao_pct) if lote.lance_atual else 0
    frete_incluso = frete.frete_estimado
    custo_op = empresa.custo_op_fixo
    margem_reais = int(preco_giro * margem_aplicada)

    # 5. Preço-alvo de lance
    preco_alvo = (
        preco_giro
        - reforma.custo_total
        - taxas_leilao_base
        - frete_incluso
        - custo_op
        - margem_reais
    )

    # 6. Preço máximo absoluto (margem mínima)
    margem_reais_min = int(preco_giro * empresa.margem.minima_absoluta)
    preco_max = (
        preco_giro
        - reforma.custo_total
        - taxas_leilao_base
        - frete_incluso
        - custo_op
        - margem_reais_min
    )

    # 7. Score ROI — retorno % esperado sobre o capital imobilizado no preço-alvo
    #    (útil pra ordenar entre lotes de preços muito diferentes)
    capital_total = max(preco_alvo + reforma.custo_total + frete_incluso + taxas_leilao_base + custo_op, 1)
    retorno = preco_giro - capital_total
    score_roi = retorno / capital_total

    justificativa = (
        f"preco_giro=R${preco_giro} (FIPE={mercado.fipe}, WM_p25={mercado.webmotors_p25}) "
        f"reforma=R${reforma.custo_total} frete=R${frete_incluso} "
        f"taxas=R${taxas_leilao_base} op=R${custo_op} "
        f"margem={margem_aplicada:.1%} (risco={fator_risco:.2f}, liq={fator_liquidez:.2f})"
    )

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
        taxas_leilao=taxas_leilao_base,
        preco_giro=preco_giro,
        justificativa=justificativa,
    )
