"""Auditoria de colunas — cruza valores recentes com o propósito declarado no Glossário.

Invariantes por coluna (CHECKS) expressam em linguagem imperativa os limites
"de sanidade" de cada campo da planilha. Quando uma linha recente viola, o
resultado reportado é:

    "⚠ <coluna>: N linha(s) — <motivo> (ex: lote <id> valor=<v>)"

A função `audit(engine, sample_size=20)` lê as últimas N avaliações
(JOIN Lote LEFT JOIN LaudoCache), monta um dict enriched com situação/viável/
encerrado/ROI anualizado e aplica os validators. Silencioso quando tudo ok.

Mantida como módulo (não script) pra ser importável nos testes sem path munging.
A CLI fica em `scripts/audit_columns.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlmodel import Session, select

from carros_sa.agents.calibracao_giro import roi_anualizado
from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.sheets import HEADER, _lucro_absoluto_no_alvo

SITUACOES_VALIDAS = {"✓ Viável", "✗ Caro demais"}

# Severidades >= MEDIA exigem reforma > 0. NENHUMA/LEVE/desconhecido podem ter R$ 0.
_SEVERIDADES_QUE_EXIGEM_REFORMA = {"media", "grave", "estrutural"}

# Limite acima do qual `preco_giro_fipe` (= webmotors_mediana × f_km) começa a
# ficar suspeito. Por construção pode chegar a ~1.115×FIPE no fallback
# `webmotors_mediana = FIPE × 0.97` somado a `f_km = 1.15` (km do lote MUITO
# abaixo da mediana). Acima de 1.10 = combinação otimista das duas premissas
# rodando ao mesmo tempo — sinal pra checar a entrada de Webmotors live ou km
# do mercado. Não é bug matemático, é dado fraco.
_PRECO_GIRO_FIPE_RATIO_MAX = 1.10


# Validator retorna None se ok; string com motivo se suspeito.
Validator = Callable[[Any, Dict[str, Any]], Optional[str]]


def _situacao(row: Dict[str, Any]) -> str:
    return "✓ Viável" if row["viavel"] else "✗ Caro demais"


CHECKS: Dict[str, Validator] = {
    "Rank": lambda v, r: None if isinstance(v, int) and v > 0 else "Rank deve ser int positivo",
    "Situação": lambda v, r: (
        None if v in SITUACOES_VALIDAS
        else f"Situação '{v}' fora do domínio {SITUACOES_VALIDAS}"
    ),
    "Marca": lambda v, r: (
        "Marca string vazia — scraper não capturou fabricante do card"
        if not v or not str(v).strip()
        else None
    ),
    "Modelo": lambda v, r: (
        "Modelo string vazia — scraper não capturou nome do modelo"
        if not v or not str(v).strip()
        else None
    ),
    "Ano": lambda v, r: (
        "Ano fora de [1980, ano_atual+1] — provavelmente erro de parsing do card"
        if v is None or not isinstance(v, int) or v < 1980 or v > datetime.now().year + 1
        else None
    ),
    "Cidade": lambda v, r: None,  # "—" é legítimo (lote sem origem_cidade declarada)
    "Fim do Leilão": lambda v, r: None,  # pode ser "—" para lotes showroom
    "KM": lambda v, r: (
        "KM absurdo (>800k ou <0)" if v is not None and (v > 800_000 or v < 0) else None
    ),
    "Lance Atual (R$)": lambda v, r: (
        "Lance atual negativo" if v is not None and v < 0 else None
    ),
    # Sanidade de coluna individual: lote 'Viável' tem que ter teto positivo. Os
    # cross-checks (zona apertada, preco_max > FIPE, preco_alvo > preco_max) vivem
    # em `ALL_CHECKS` como funções independentes — assim múltiplos sintomas na
    # MESMA linha emergem juntos (antes o if/elif encadeado escondia red flags
    # atrás de yellow flags: zona apertada coexistindo com preco_max > FIPE
    # mostrava só o 1º).
    "Lance Máximo (R$)": lambda v, r: (
        "Lance Máximo não-positivo num lote 'Viável' — precificador deveria ter produzido teto > 0"
        if r["situacao"] == "✓ Viável" and (v is None or v <= 0)
        else None
    ),
    # FIPE pode ser '—' em registros pré-workstream K (campo nullable). Quando
    # presente, deve ser inteiro positivo — valor zero ou negativo indica falha
    # de scraping/cache do client FIPE.
    #
    # Cross-field: `preco_giro_fipe` é a âncora de revenda (webmotors_mediana × f_km).
    # Por construção fica próximo de FIPE — quando AA "Talvez se interesse" não
    # tem dados, fallback é FIPE × 0.97; quando tem, é mediana de similares (que
    # historicamente bate em ±15% da FIPE). Divergência grande sinaliza:
    #  - mediana de similares poluída (regex pegou R$ de outras seções, ver
    #    `_extrai_precos_similares` em parsers.py)
    #  - FIPE da consulta cacheada está stale ou veio de marca/modelo errado
    #  - f_km saturado (lote km muito acima/abaixo da mediana real do mercado)
    # Threshold ±25% é largo de propósito: f_km contribui no máximo ±15%,
    # então gap >25% praticamente exige outra fonte de erro.
    "FIPE (R$)": lambda v, r: (
        "FIPE não-positivo — provável falha do client FIPE ou cache stale"
        if isinstance(v, (int, float)) and v <= 0
        else (
            # Sinaliza quando preco_giro_fipe (âncora de revenda usada no
            # precificador) está muito acima da FIPE pura. Combinação típica
            # que dispara: webmotors_mediana=FIPE×0.97 (fallback sem live data)
            # + f_km saturado a 1.15 (km do lote << km mediana). Não é bug
            # matemático, é dado fraco — duas premissas otimistas combinadas.
            f"preco_giro_fipe R$ {r.get('preco_giro_fipe')} > FIPE × {_PRECO_GIRO_FIPE_RATIO_MAX:.2f} (FIPE={v}) — checar Webmotors live e km mediana"
            if (
                isinstance(v, (int, float)) and v > 0
                and r.get("preco_giro_fipe") is not None
                and r["preco_giro_fipe"] > int(v * _PRECO_GIRO_FIPE_RATIO_MAX)
            )
            else None
        )
    ),
    # Threshold de 500% calibrado contra benchmark real do operador (Reinaldo:
    # 21 carros, ~60-75% anual; Polo Track: 21% em 7m). ROI > 500% num negócio
    # de leilão de carros é matematicamente possível mas operacionalmente irreal
    # — quando aparece, geralmente significa `dias_giro_estimado` otimista
    # (default 25d HATCH NOVO sem calibração via Arrematado), score_roi
    # inflado por margem×fator alto, ou floor não aplicado. Sinaliza como suspeito.
    "ROI anualizado (%)": lambda v, r: (
        "ROI anualizado >500% — provável dias_giro otimista ou margem×fator inflado (benchmark operacional ~60-75%/ano)"
        if isinstance(v, (int, float)) and v > 500
        else (
            "ROI anualizado negativo — score_roi negativo (custos > preco_giro) deveria ter sido descartado"
            if isinstance(v, (int, float)) and v < 0
            else None
        )
    ),
    "Lucro/mês (R$)": lambda v, r: (
        "Lucro/mês negativo — verificar score_roi ou preco_alvo"
        if isinstance(v, (int, float)) and v < 0
        else None
    ),
    "Reforma (R$)": lambda v, r: (
        "Reforma negativa"
        if v is not None and v < 0
        else (
            # Cross-field: severidade média/grave/estrutural com reforma R$ 0 é
            # contradição. Deterministico sempre soma adicional pra grave/estrutural;
            # LLM ocasionalmente devolve 0. Indica que a estimativa não levou
            # em conta o laudo — operador veria "✓ Viável, reforma 0" em lote
            # estrutural, falso conforto.
            f"Reforma R$ 0 com severidade '{r.get('severidade')}' "
            f"(esperado >0 quando severidade ≥ média)"
            if v == 0 and str(r.get("severidade") or "").lower() in _SEVERIDADES_QUE_EXIGEM_REFORMA
            else None
        )
    ),
    # Coluna informativa. Valores esperados: texto com emoji prefixo (🟢/🟡/🔴)
    # ou "—" quando config/tese.yaml não carrega ou laudo pendente. String vazia
    # seria bug (calcular_tese deveria sempre produzir algo).
    "Tese": lambda v, r: (
        "Tese string vazia — calcular_tese ou fallback '—' não produziu valor"
        if v is None or (isinstance(v, str) and not v.strip())
        else None
    ),
    "Anúncio": lambda v, r: None,  # pode ser "—" ou fórmula HYPERLINK; ambos aceitáveis
    "Laudo": lambda v, r: None,    # "—" (sem URL ou decoy filtrado) ou HYPERLINK — ambos aceitáveis
}


# Extrai o valor de cada coluna a partir do dict interno enriquecido.
#
# IMPORTANTE: ROI anualizado e Lucro/mês são suprimidos ("—") em lotes
# inviáveis pra ESPELHAR o que `SheetsExporter._write_sheet` exibe (linhas
# 422-429): comprar pelo preço-alvo é cenário fantasioso quando lance_atual
# já passou do nosso teto. Sem essa paridade, o audit reportava "ROI
# anualizado negativo" pra lotes inviáveis (capital_ef > preco_giro →
# score_efetivo < 0) que o operador NUNCA vê na planilha — alarme falso
# que confundia debug. Padrão geral: audit deve espelhar TODAS as
# supressões de display, não só `fim_em is None`.
COLUMN_EXTRACTORS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "Rank": lambda r: r["rank"],
    "Situação": lambda r: r["situacao"],
    "Marca": lambda r: r["marca"],
    "Modelo": lambda r: r["modelo_raw"],
    "Ano": lambda r: r["ano"],
    "Cidade": lambda r: r["cidade"],
    "Fim do Leilão": lambda r: r["fim_em"],
    "KM": lambda r: r["km"],
    "Lance Atual (R$)": lambda r: r["lance_atual"],
    "Lance Máximo (R$)": lambda r: r["preco_max"],
    "FIPE (R$)": lambda r: r["fipe"] if r["fipe"] is not None else "—",
    "ROI anualizado (%)": lambda r: r["roi_anualizado"] if r["viavel"] else "—",
    "Lucro/mês (R$)": lambda r: r.get("lucro_mes", "—") if r["viavel"] else "—",
    "Reforma (R$)": lambda r: r["reforma_estimada"],
    "Tese": lambda r: r.get("tese", "—") if r["viavel"] else "—",
    "Anúncio": lambda r: r["url"],
    "Laudo": lambda r: r.get("laudo_url") or "—",
}


def _build_rows(session: Session, sample_size: int) -> List[Dict[str, Any]]:
    """Últimas N avaliações com JOIN Lote + LEFT JOIN LaudoCache, ordenadas e ranqueadas.

    Espelha `SheetsExporter._query` — lotes sem `fim_em` (sumiram do leilão) e
    encerrados (timer vencido / badge ARREMATADO) são filtrados; ativos viáveis
    vêm primeiro, desempate por folga de lance. Rank auditado coincide com o
    que o operador vê na planilha. Sem essa paridade, audit reportava violações
    em lotes invisíveis na UI — alarme falso que confunde o operador.
    """
    avaliacoes = session.exec(
        select(AvaliacaoLote).order_by(AvaliacaoLote.criado_em.desc()).limit(sample_size)
    ).all()

    agora = datetime.now()
    rows: List[Dict[str, Any]] = []
    for av in avaliacoes:
        lote = session.get(Lote, av.lote_id)
        if lote is None:
            continue
        # Paridade com SheetsExporter._query: lote sem fim_em é sumido do leilão
        # ativo do AA — não entra na planilha, então auditá-lo só gera ruído.
        if lote.fim_em is None:
            continue
        laudo: Optional[LaudoCache] = session.get(LaudoCache, av.lote_id)

        viavel = av.preco_max > (lote.lance_atual or 0)

        detalhe_raw = (lote.raw_json or {}).get("detalhe") or {}
        encerrado_por_badge = bool(detalhe_raw.get("encerrado"))
        encerrado_por_timer = lote.fim_em is not None and lote.fim_em < agora
        encerrado = encerrado_por_badge or encerrado_por_timer

        # ROI anualizado HONESTO: usa `score_roi_efetivo` que considera entrada
        # por max(lance_atual, preco_alvo). Bate com o que o SheetsExporter
        # exibe na planilha — auditoria não deve "ver" um ROI diferente do
        # operador. Quando lance_atual ≤ preco_alvo, equivale a `score_roi`
        # original (entrada pelo alvo é factível).
        from carros_sa.tools.sheets import _score_roi_efetivo
        score_efetivo = _score_roi_efetivo(av, lote.lance_atual)
        roi_anual = roi_anualizado(score_efetivo, av.dias_giro_estimado) * 100

        # Popularidade (bucket relativo) — pode falhar se popularidade.py quebrar;
        # auditoria não deve morrer por isso, cai pro "—" que é valor aceito.
        popularidade = "—"
        try:
            from carros_sa.agents.calibracao_giro import _categoria_de_modelo
            from carros_sa.tools.popularidade import bucket_modelo
            cat = _categoria_de_modelo(lote.modelo)
            popularidade = bucket_modelo(lote.marca, lote.modelo, cat, ano=lote.ano).value
        except Exception:
            pass

        fim_em_str = "—"
        if lote.fim_em is not None:
            try:
                fim_em_str = lote.fim_em.strftime("%d/%m/%Y %H:%M")
            except Exception:
                fim_em_str = str(lote.fim_em)

        scraped_at_str = "—"
        if lote.scraped_at is not None:
            try:
                scraped_at_str = lote.scraped_at.strftime("%d/%m/%Y %H:%M")
            except Exception:
                scraped_at_str = str(lote.scraped_at)

        loja_raw = (lote.raw_json or {}).get("loja") if isinstance(lote.raw_json, dict) else None
        rows.append({
            "lote_id": av.lote_id,
            "modelo": f"{lote.marca} {lote.modelo}".strip(),
            "ano": lote.ano,
            "cidade": lote.origem_cidade or "—",
            "loja": loja_raw or "—",
            "marca": lote.marca,
            "modelo_raw": lote.modelo,
            "fim_em": fim_em_str,
            "km": lote.km,
            "lance_atual": lote.lance_atual or 0,
            "preco_alvo": av.preco_alvo,
            "preco_max": av.preco_max,
            "fipe": av.fipe,
            "preco_giro_fipe": av.preco_giro_fipe,
            "preco_giro_aa": av.preco_giro_aa,
            "fipe_pct_lance_minimo": lote.fipe_pct_lance_minimo,
            "score_roi": av.score_roi,
            "dias_giro": av.dias_giro_estimado,
            "roi_anualizado": round(roi_anual, 1),
            "fator_risco": round(av.fator_risco, 3),
            "popularidade": popularidade,
            "severidade": laudo.severidade_geral if laudo else "—",
            "motor_ok": ("Sim" if laudo.motor_ok else "NÃO") if laudo else "—",
            "reforma_estimada": av.reforma_estimada,
            "frete": av.frete_incluso,
            "justificativa": av.justificativa,
            "url": lote.url,
            "scraped_at": scraped_at_str,
            "preco_giro": av.preco_giro,
            "viavel": viavel,
            "encerrado": encerrado,
            "margem_aplicada": av.margem_aplicada,
        })

    # Espelha SheetsExporter.exportar: filtra encerrados, depois ordena viáveis
    # primeiro, desempate por ROI anualizado desc (mesma métrica do CLI top).
    rows = [r for r in rows if not r["encerrado"]]
    rows.sort(key=lambda r: (
        0 if r["viavel"] else 1,
        -(r["roi_anualizado"] or 0),
    ))
    for idx, r in enumerate(rows, start=1):
        r["rank"] = idx
        r["situacao"] = _situacao(r)
    return rows


# --------------------------------------------------------------------------
# Modelo unificado de checks
# --------------------------------------------------------------------------
#
# Toda check é uma função `(row) -> List[(label, motivo, valor_exemplo)]`. 0+
# violações por linha — a maioria devolve `[]` no caminho feliz e `[(...)]` em
# violação. `_check_columns` é a exceção que pode devolver várias (uma por
# coluna do HEADER que falhou).
#
# Antes desta unificação coexistiam 3 modelos: `CHECKS` (Dict, por coluna),
# `CROSS_CHECKS` (List[Optional[Tuple]], cruza colunas), `_DERIVED_CHECKS`
# (List[Optional[Tuple]], inspeciona campos fora do HEADER). Cada um com
# loop próprio em `audit()`. Convergência em um único contrato simplifica
# leitura sem perder a ergonomia de `CHECKS` (mantida intacta como Dict por
# coluna — é só a EXECUÇÃO que ficou unificada).

CheckResult = Tuple[str, str, Any]  # (label, motivo, valor_exemplo)
CheckFn = Callable[[Dict[str, Any]], List[CheckResult]]


def _check_columns(row: Dict[str, Any]) -> List[CheckResult]:
    """Roda os validators do `CHECKS` (por coluna) sobre um row.

    Mantém o pareamento histórico com `COLUMN_EXTRACTORS` + `HEADER` (paridade
    obrigatória do workstream Q). Cada coluna pode acionar 0 ou 1 motivo —
    múltiplas colunas podem falhar na mesma linha, então a saída é uma lista.
    """
    out: List[CheckResult] = []
    for coluna in HEADER:
        extractor = COLUMN_EXTRACTORS.get(coluna)
        validator = CHECKS.get(coluna)
        if extractor is None or validator is None:
            continue
        valor = extractor(row)
        motivo = validator(valor, row)
        if motivo is not None:
            out.append((coluna, motivo, valor))
    return out


def _check_preco_giro_acima_fipe(row: Dict[str, Any]) -> List[CheckResult]:
    """preco_giro > FIPE × 1.10 sinaliza f_km saturando ou mediana inflada.

    Por construção, preco_giro = webmotors_mediana × f_km. webmotors_mediana
    pode ser FIPE × 0.97 (fallback) ou similares Auto Avaliar. f_km tem cap
    1.15 (lote com km muito baixa). No pior caso teórico: 0.97 × 1.15 =
    1.1155 — chega na borda mas não passa muito. Quando passa, geralmente
    a mediana já vem inflada (similares premium dominando) ou f_km está
    saturado num caso onde o lote real não é tão raro.
    """
    pg = row.get("preco_giro")
    fipe = row.get("fipe")
    if pg and fipe and pg > fipe * 1.10:
        ratio_pct = round((pg / fipe) * 100, 1)
        return [(
            "Preço-Giro vs FIPE",
            f"preco_giro {ratio_pct}% da FIPE — f_km saturando ou mediana inflada (esperado <110%)",
            pg,
        )]
    return []


def _check_preco_alvo_gt_preco_max(row: Dict[str, Any]) -> List[CheckResult]:
    """preco_alvo > preco_max viola a invariante do precificador.

    Por construção, margem_aplicada >= margem_minima_absoluta, então o
    desconto sobre preco_giro pra calcular preco_alvo é ≥ que pra preco_max.
    preco_alvo > preco_max indicaria bug no precificador (ex.: regressão
    em margem.minima_absoluta) ou registro corrompido.
    """
    av_alvo = row.get("preco_alvo")
    av_max = row.get("preco_max")
    if av_alvo is not None and av_max is not None and av_alvo > av_max:
        return [(
            "Preço-Alvo vs Máximo",
            "preco_alvo > preco_max — bug no precificador (margem mínima maior que margem aplicada?)",
            av_alvo,
        )]
    return []


def _check_zona_apertada(row: Dict[str, Any]) -> List[CheckResult]:
    """`lance_atual > preco_alvo` mas ainda `≤ preco_max` — entrada acima da
    margem calibrada. ROI/Lucro/mês exibidos usam `_score_roi_efetivo` (já
    descontado), mas o aviso lembra o operador de que a folga é só pra margem
    mínima absoluta — não pra a margem-alvo.
    """
    preco_max = row.get("preco_max")
    preco_alvo = row.get("preco_alvo")
    lance_atual = row.get("lance_atual")
    if (
        preco_max is None or preco_alvo is None or lance_atual is None
        or preco_max <= 0
    ):
        return []
    if lance_atual > preco_alvo and lance_atual <= preco_max:
        return [(
            "Lance Máximo (R$)",
            f"Zona apertada: lance_atual R$ {lance_atual} > preco_alvo R$ {preco_alvo} "
            f"(Lance Máximo R$ {preco_max}) — ROI realista < ROI alvo",
            preco_max,
        )]
    return []


def _check_lance_maximo_acima_fipe(row: Dict[str, Any]) -> List[CheckResult]:
    """`Lance Máximo > FIPE × 1.05` é red flag econômico.

    Por construção `preco_max ≤ preco_giro × (1 − margem.minima_absoluta)` —
    chega na FIPE só com f_km saturado a 1.15 + margem mínima MUITO baixa
    (≤10%). Quando passa, sinaliza dado quebrado: FIPE stale, mediana de
    similares poluída ou cap defensivo do precificador (1.20×FIPE) batendo
    com mediana inflada legítima (Webmotors n≥5 sem cap em avaliador). É
    independente da zona apertada — pode coexistir e ambos devem aparecer.
    """
    preco_max = row.get("preco_max")
    fipe = row.get("fipe")
    if not preco_max or not fipe or preco_max <= 0:
        return []
    if preco_max > int(fipe * 1.05):
        return [(
            "Lance Máximo (R$)",
            f"Lance Máximo R$ {preco_max} > FIPE × 1.05 (FIPE={fipe}) — checar âncora de revenda",
            preco_max,
        )]
    return []


def _check_reforma_pesada(row: Dict[str, Any]) -> List[CheckResult]:
    """Reforma > 30% do preco_giro num lote 'Viável' = lote economicamente
    questionável.

    Tecnicamente o precificador já desconta a reforma do preco_max e a margem
    aplicada continua respeitada — então o lote passa como 'Viável'. Mas
    operacionalmente, gastar R$10k+ em reforma pra revender um carro de R$30k
    significa: (a) capital empatado por mais tempo, (b) risco de surpresa na
    oficina (estimativa subestima o real), (c) revenda mais difícil porque
    histórico de avaria assusta comprador. Dispara só pra lote viável — em
    inviáveis o display já suprime tudo, audit acompanha.
    """
    if not row.get("viavel"):
        return []
    reforma = row.get("reforma_estimada")
    preco_giro = row.get("preco_giro")
    if not reforma or not preco_giro or preco_giro <= 0:
        return []
    pct = reforma / preco_giro
    if pct > 0.30:
        return [(
            "Reforma (R$)",
            f"Reforma R$ {reforma} é {pct:.0%} do preco_giro R$ {preco_giro} — "
            "lote economicamente questionável (capital de reforma alto vs. revenda)",
            reforma,
        )]
    return []


def _check_margem_no_teto(row: Dict[str, Any]) -> List[CheckResult]:
    """Margem efetiva ≥ 49% sinaliza fatores risco × liquidez perto do teto.

    Margem aplicada é capada em 50% no precificador (`_MARGEM_TETO`) — quando
    bate no cap, normalmente é laudo estrutural + mercado ilíquido. O lote
    acaba sendo descartado por viabilidade, mas vale flagar pra o operador
    conferir se a calibração de fatores faz sentido pra essa amostra.
    """
    margem = row.get("margem_aplicada")
    if margem is None or margem < 0.49:
        return []
    return [(
        "Precificador / margem",
        f"margem aplicada {margem:.1%} no teto (cap=50%) — fatores no limite, "
        "lote provavelmente péssimo. Conferir laudo + mercado.",
        margem,
    )]


# Registry único — `audit()` itera por todas as checks com a mesma assinatura.
# Ordem importa só pra ergonomia da leitura do output (mas a agregação é por
# (label, motivo), então duplicatas naturalmente colapsam).
ALL_CHECKS: List[CheckFn] = [
    _check_columns,
    _check_preco_giro_acima_fipe,
    _check_preco_alvo_gt_preco_max,
    _check_zona_apertada,
    _check_lance_maximo_acima_fipe,
    _check_reforma_pesada,
    _check_margem_no_teto,
]


def audit(engine, sample_size: int = 20) -> List[str]:
    """Retorna lista de violações. Vazia = tudo ok.

    Cada violação é uma string pronta pra impressão no formato:
        "⚠ <Coluna>: N linha(s) — <motivo> (ex: lote <id> valor=<v>)"

    Toda check em `ALL_CHECKS` segue a mesma assinatura `(row) -> List[(label,
    motivo, valor)]` — o aggregator é um único loop. (label, motivo) é a chave
    de agregação: 2 motivos distintos na mesma coluna viram 2 entradas
    separadas (menos compacto, mais acionável).
    """
    with Session(engine) as session:
        rows = _build_rows(session, sample_size)

    if not rows:
        return []

    agregador: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        for check_fn in ALL_CHECKS:
            for label, motivo, valor in check_fn(row):
                chave = (label, motivo)
                if chave not in agregador:
                    agregador[chave] = {
                        "count": 1,
                        "exemplo_lote": row["lote_id"],
                        "exemplo_valor": valor,
                    }
                else:
                    agregador[chave]["count"] += 1

    saida: List[str] = []
    for (coluna, motivo), info in agregador.items():
        suffix = "" if info["count"] == 1 else "s"
        saida.append(
            f"⚠ {coluna}: {info['count']} linha{suffix} — {motivo} "
            f"(ex: lote {info['exemplo_lote']} valor={info['exemplo_valor']!r})"
        )
    return saida
