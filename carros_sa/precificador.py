"""Precificador — Python puro, sem LLM.

Fórmula efetiva (espelha o código abaixo). Duas margens distintas — não confundir:
    f_km            = fator_km(km_lote, webmotors_km_mediana)            # ∈ [0.75, 1.15]
    preco_giro_fipe = FIPE × f_km × 0.95                                 # âncora única
    preco_giro      = preco_giro_fipe                                    # FIPE-only desde 2026-05-08

    # Margem CALIBRADA por risco/liquidez — entra no preco_alvo. Capada em 50%
    # (_MARGEM_TETO) e respeitando piso minima_absoluta da empresa.
    margem_calibrada = clamp(margem_base × fator_risco × fator_liquidez, minima_absoluta, 0.50)

    # preco_alvo usa margem CALIBRADA (caso médio — "se entrarmos bem, ganhamos isso")
    preco_alvo_lance = (preco_giro − reforma − frete − custo_op − margem_calibrada×preco_giro − taxa_fixa)
                       / (1 + taxa_pct)

    # preco_max usa margem MÍNIMA ABSOLUTA da empresa — teto inegociável (acima disso
    # nem a margem mínima é respeitada). Sempre preco_max > preco_alvo por construção.
    preco_max_lance  = (preco_giro − reforma − frete − custo_op − minima_absoluta×preco_giro − taxa_fixa)
                       / (1 + taxa_pct)

Fator_risco e fator_liquidez são derivados do laudo + sinal de mercado; bounds
vêm da config da empresa (empresas mais exigentes usam bounds mais altos).

Sobre a âncora única (FIPE × 0.95):
- FIPE é tabulada com base em revenda de seminovos em concessionária. Loja
  pequena vende com leve desconto vs FIPE em condições normais — daí o ajuste
  conservador de 0.95.
- `webmotors_mediana` continua exibido na planilha como REFERÊNCIA informativa
  (col "Mediana mercado"), mas NÃO entra no cálculo. Operador vê FIPE e mediana
  lado a lado e contextualiza a decisão da máquina. Desde workstream G (LIVE
  2026-05-12), a mediana vem do cache `anuncio_webmotors` (Webmotors live,
  populado pelo cron `carros-sa webmotors-coletar`, TTL 24h); sem amostra fresh
  o display mostra "—" e o sistema persiste `FIPE` como placeholder neutro.
- Histórico: até 2026-05-08 a âncora era `webmotors_mediana × f_km` com 3 caps
  em série tentando consertar similares poluídos do Auto Avaliar (Tiggo 7 vs
  Tiggo 2, Airtrek vs Outlander, Ka descontinuado). Workstream G descontinuou
  AA como fonte de mediana e migrou pro Webmotors live. Reativar mediana no
  precificador (workstream G.3 — ponderação `FIPE × β + mediana × (1-β)`)
  bloqueia em ≥1 semana de cron acumulando amostra estável; por enquanto
  segue FIPE-only.
- `webmotors_p25` continua em `SinalMercado` mas não é consumido (legado).

Invariantes esperados (validados pelo audit):
- `preco_alvo ≤ preco_max` (margem_calibrada ≥ minima_absoluta sempre — o `max(...)`
  no clamp garante). Equivalente: usar minima_absoluta no teto e a calibrada no alvo
  → teto sempre acima do alvo. Audit `_check_preco_alvo_gt_preco_max` guarda.
- `preco_giro_fipe / FIPE ≤ 1.15 × 0.95 = 1.0925` (máximo do f_km × ajuste revenda) →
  `preco_max < FIPE` mesmo com custos zero e margem mínima. Inviável Lance Máximo >
  FIPE no design FIPE-only. Audit `_check_preco_giro_acima_fipe` (threshold 1.13) +
  `_check_lance_maximo_acima_fipe` (preco_max > FIPE × 1.05) guardam regressão.
"""

from __future__ import annotations

from typing import Optional

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
    # 1. Preço de venda âncora — FIPE como única fonte primária, ajustada por km
    #    do lote vs km mediana de mercado. FIPE × 0.95 reflete o desconto típico
    #    da revenda em loja pequena (a tabela é calibrada com concessionária).
    #
    #    `webmotors_mediana` continua persistido em `Avaliacao` como campo de
    #    REFERÊNCIA exibido na planilha (col "Mediana mercado") — operador
    #    compara FIPE × mediana lado a lado, sistema decide com FIPE. Desde
    #    workstream G (LIVE 2026-05-12), a mediana vem do Webmotors live via
    #    cache populado pelo cron noturno; sem amostra real, o display mostra
    #    "—" (paridade sheets/audit) e o sistema persiste `FIPE` como placeholder
    #    neutro. Workstream G.3 (reativar mediana no precificador via
    #    `FIPE × β + mediana × (1-β)`) bloqueia em ≥1 semana de cron acumulando
    #    amostra estável.
    #
    #    Histórico (pré-2026-05-08): a âncora era `webmotors_mediana × f_km` com
    #    cap n<5 no avaliador (1.20×FIPE) + cap final no precificador (1.20×FIPE).
    #    A "mediana" vinha de `similares_precos` extraídos do Auto Avaliar —
    #    frequentemente poluída por outliers categóricos (Tiggo 7 entre Tiggo 2,
    #    Airtrek entre Outlander, Ka descontinuado vs seminovos europeus) e
    #    elevava o lance máximo acima da FIPE. Os 3 caps em série mascaravam a
    #    fonte do ruído sem removê-la. Workstream G removeu AA como fonte e
    #    migrou pro Webmotors live; FIPE-only mata categoricamente Lance Máximo
    #    > FIPE no design atual.
    f_km = fator_km(lote.km, mercado.webmotors_km_mediana)
    _AJUSTE_FIPE_REVENDA = 0.95
    preco_giro_fipe = int(round(mercado.fipe * f_km * _AJUSTE_FIPE_REVENDA))
    preco_giro_aa: Optional[int] = None  # campo de referência apenas
    preco_giro = preco_giro_fipe

    # 2. Fatores
    fator_risco = calcular_fator_risco(laudo, empresa.fator_risco_bounds)
    fator_liquidez = calcular_fator_liquidez(mercado, empresa.fator_liquidez_bounds)

    # 3. Margem — respeita o piso absoluto da empresa e um teto duro.
    #    Cap em 0.50: quando fator_risco × fator_liquidez saturam (laudo estrutural
    #    + mercado ilíquido), `base × 2.0 × 1.8 = 0.90` em Uberlândia. Margem
    #    teórica acima de 50% é sintoma de fatores no teto, não de "oportunidade
    #    real" — o lote será descartado de qualquer forma (lance > preco_max), mas
    #    deixar a margem subir desproporcionalmente infla `score_roi` e faz lotes
    #    péssimos aparecerem com Lucro/mês alto na planilha. Cap mata o ruído sem
    #    afetar lotes calibrados (margem típica fica em 25-45%).
    _MARGEM_TETO = 0.50
    margem_calculada = empresa.margem.base * fator_risco * fator_liquidez
    margem_aplicada = max(
        min(margem_calculada, _MARGEM_TETO),
        empresa.margem.minima_absoluta,
    )

    # 4. Custos fixos (exceto taxas, calculadas abaixo sobre o lance vencedor)
    #    `custo_op_para_lote` soma o adicional de transferência interestadual
    #    quando origem.uf != patio.uf (taxa DETRAN mudança de estado). Pra lotes
    #    locais (mesma UF) e configs sem `custos_operacionais` decompostos,
    #    devolve `custo_op_fixo` puro — compat total com YAML legacy.
    frete_incluso = frete.frete_estimado
    custo_op = empresa.custo_op_para_lote(lote)
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
        f"preco_giro=R${preco_giro} (FIPE×0.95×f_km){km_txt} "
        f"(FIPE={mercado.fipe}{aa_txt}, WM_med={mercado.webmotors_mediana} ref) "
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
