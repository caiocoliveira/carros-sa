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
    "Lance Máximo (R$)": lambda v, r: (
        "Lance Máximo não-positivo num lote 'Viável' — precificador deveria ter produzido teto > 0"
        if r["situacao"] == "✓ Viável" and (v is None or v <= 0)
        else (
            # Sanidade de cabeça: por construção `preco_max ≤ preco_giro × (1−margem_min)`
            # < FIPE, tirando casos extremos (km do lote MUITO abaixo da mediana de
            # mercado → f_km próximo do teto 1.15). Lance Máximo > FIPE × 1.05 é
            # red flag — indica dado desalinhado (FIPE errada, mediana inflada,
            # f_km saturado num caso onde não devia).
            f"Lance Máximo R$ {v} > FIPE × 1.05 (FIPE={r.get('fipe')}) — checar âncora de revenda"
            if v is not None and r.get("fipe") and v > int(r["fipe"] * 1.05)
            else None
        )
    ),
    # FIPE pode ser '—' em registros pré-workstream K (campo nullable). Quando
    # presente, deve ser inteiro positivo — valor zero ou negativo indica falha
    # de scraping/cache do client FIPE.
    "FIPE (R$)": lambda v, r: (
        "FIPE não-positivo — provável falha do client FIPE ou cache stale"
        if isinstance(v, (int, float)) and v <= 0
        else None
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
        "Reforma negativa" if v is not None and v < 0 else None
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
    "ROI anualizado (%)": lambda r: r["roi_anualizado"],
    "Lucro/mês (R$)": lambda r: r.get("lucro_mes", "—"),
    "Reforma (R$)": lambda r: r["reforma_estimada"],
    "Tese": lambda r: r.get("tese", "—"),
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

        # ROI anualizado = score_roi (caso médio no preço-alvo) anualizado.
        # Igual ao SheetsExporter: ROI no máximo era quase-constante por empresa
        # (≈margem_min/(1-margem_min)), virava tautologia inútil pra ranking.
        roi_anual = roi_anualizado(av.score_roi, av.dias_giro_estimado) * 100

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
            "preco_max": av.preco_max,
            "preco_alvo": av.preco_alvo,
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
        })

    # Espelha SheetsExporter.exportar: filtra encerrados, depois ordena viáveis
    # primeiro, desempate por folga de lance.
    rows = [r for r in rows if not r["encerrado"]]
    rows.sort(key=lambda r: (
        0 if r["viavel"] else 1,
        -(r["preco_max"] - r["lance_atual"]),
    ))
    for idx, r in enumerate(rows, start=1):
        r["rank"] = idx
        r["situacao"] = _situacao(r)
    return rows


# Invariantes que CRUZAM colunas — não cabem em CHECKS porque não pertencem
# a uma coluna individual. Rodam independente de COLUMN_EXTRACTORS / HEADER,
# então não quebram a paridade test. Validator devolve `(label, motivo, valor_exemplo)`
# ou None quando ok. Label não precisa estar em HEADER (vira chave de agregação).
CrossValidator = Callable[[Dict[str, Any]], Optional[Tuple[str, str, Any]]]


def _check_preco_giro_acima_fipe(row: Dict[str, Any]) -> Optional[Tuple[str, str, Any]]:
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
        return (
            "Preço-Giro vs FIPE",
            f"preco_giro {ratio_pct}% da FIPE — f_km saturando ou mediana inflada (esperado <110%)",
            pg,
        )
    return None


def _check_preco_alvo_gt_preco_max(row: Dict[str, Any]) -> Optional[Tuple[str, str, Any]]:
    """preco_alvo > preco_max viola a invariante do precificador.

    Por construção, margem_aplicada >= margem_minima_absoluta, então o
    desconto sobre preco_giro pra calcular preco_alvo é ≥ que pra preco_max.
    preco_alvo > preco_max indicaria bug no precificador (ex.: regressão
    em margem.minima_absoluta) ou registro corrompido.
    """
    av_alvo = row.get("preco_alvo")
    av_max = row.get("preco_max")
    if av_alvo is not None and av_max is not None and av_alvo > av_max:
        return (
            "Preço-Alvo vs Máximo",
            "preco_alvo > preco_max — bug no precificador (margem mínima maior que margem aplicada?)",
            av_alvo,
        )
    return None


CROSS_CHECKS: List[CrossValidator] = [
    _check_preco_giro_acima_fipe,
    _check_preco_alvo_gt_preco_max,
]


def audit(engine, sample_size: int = 20) -> List[str]:
    """Retorna lista de violações. Vazia = tudo ok.

    Cada violação é uma string pronta pra impressão no formato:
        "⚠ <Coluna>: N linha(s) — <motivo> (ex: lote <id> valor=<v>)"
    """
    with Session(engine) as session:
        rows = _build_rows(session, sample_size)

    if not rows:
        return []

    # Agrega por (coluna, motivo): N linhas + primeiro exemplo.
    # Dois motivos diferentes na mesma coluna viram DUAS entradas agrupadas
    # separadamente — menos compacto mas mais acionável.
    agregador: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        for coluna in HEADER:
            extractor = COLUMN_EXTRACTORS.get(coluna)
            validator = CHECKS.get(coluna)
            if extractor is None or validator is None:
                continue
            valor = extractor(row)
            motivo = validator(valor, row)
            if motivo is None:
                continue
            chave = (coluna, motivo)
            if chave not in agregador:
                agregador[chave] = {
                    "count": 1,
                    "exemplo_lote": row["lote_id"],
                    "exemplo_valor": valor,
                }
            else:
                agregador[chave]["count"] += 1

        # Invariantes que cruzam colunas — não respeitam o loop por HEADER.
        for cross in CROSS_CHECKS:
            resultado = cross(row)
            if resultado is None:
                continue
            label, motivo, valor = resultado
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
