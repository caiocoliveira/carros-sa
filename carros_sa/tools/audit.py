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
from carros_sa.tools.sheets import HEADER, _calcular_roi_no_maximo

SITUACOES_VALIDAS = {"✓ Viável", "✗ Caro demais"}

# Tolerâncias para checagens cross-column.
# Referência empírica: em fallback (sem Webmotors real), preco_giro_fipe = FIPE*0.97*f_km,
# com f_km ∈ [0.75, 1.15] → razão vs FIPE ∈ [0.73, 1.12]. Com Webmotors real ativo,
# mediana pode divergir mais de FIPE (varejo vs tabela). Faixa >30% abaixo ou >30%
# acima da FIPE é sinal de dado bagunçado (FIPE errada, similares estragados, etc.).
_GIRO_VS_FIPE_MIN = 0.60
_GIRO_VS_FIPE_MAX = 1.30
# Lance máximo não deve ultrapassar FIPE por muito — o preço de venda esperado
# TEM QUE cobrir margem + custos, então preco_max < preco_giro < ~FIPE.
# Tolerância 5% pra absorver ruído em lotes muito baratos (pouca sensibilidade).
_LANCE_MAX_VS_FIPE_MAX = 1.05


# Validator retorna None se ok; string com motivo se suspeito.
Validator = Callable[[Any, Dict[str, Any]], Optional[str]]


def _situacao(row: Dict[str, Any]) -> str:
    return "✓ Viável" if row["viavel"] else "✗ Caro demais"


def _checar_lance_maximo(v: Any, r: Dict[str, Any]) -> Optional[str]:
    """Sanidade do teto de lance — viabilidade + razão vs FIPE.

    Cobre:
      (1) lote 'Viável' com teto não-positivo (bug no precificador)
      (2) teto muito acima de FIPE (preco_giro inflado ou FIPE subestimada —
          operador vai ancorar decisão em valor irreal)
    """
    if r["situacao"] == "✓ Viável" and (v is None or v <= 0):
        return "Lance Máximo não-positivo num lote 'Viável' — precificador deveria ter produzido teto > 0"
    fipe = r.get("fipe")
    if v is not None and v > 0 and fipe and v > fipe * _LANCE_MAX_VS_FIPE_MAX:
        razao = v / fipe
        return (
            f"Lance Máximo {razao:.2f}× FIPE — teto acima da FIPE (>{_LANCE_MAX_VS_FIPE_MAX:.2f}) "
            f"quando margem deveria garantir teto < preço de venda"
        )
    return None


def _checar_giro_vs_fipe(row: Dict[str, Any]) -> List[str]:
    """Checagens sistêmicas que não mapeiam 1:1 pra coluna da planilha.

    Returns lista de (motivo) — vazia se tudo ok. Cada motivo gera uma entrada
    agregada sob o rótulo '<interno>' pra sinalizar que é invariante de dado
    persistido, não de coluna exibida.
    """
    alertas: List[str] = []
    fipe = row.get("fipe") or 0
    giro_fipe = row.get("preco_giro_fipe") or 0
    giro_aa = row.get("preco_giro_aa")

    if fipe > 0 and giro_fipe > 0:
        razao = giro_fipe / fipe
        if razao > _GIRO_VS_FIPE_MAX or razao < _GIRO_VS_FIPE_MIN:
            alertas.append(
                f"preco_giro_fipe {razao:.2f}× FIPE (fora da faixa "
                f"[{_GIRO_VS_FIPE_MIN:.2f}, {_GIRO_VS_FIPE_MAX:.2f}]) — "
                f"webmotors_mediana ou FIPE com dado estranho"
            )

    if fipe > 0 and giro_aa is not None and giro_aa > 0:
        if giro_aa > fipe * _GIRO_VS_FIPE_MAX:
            alertas.append(
                f"preco_giro_aa ({giro_aa}) >> FIPE ({fipe}) — Tabela Auto Avaliar "
                f"é atacado, deveria ficar abaixo da FIPE"
            )

    return alertas


CHECKS: Dict[str, Validator] = {
    "Rank": lambda v, r: None if isinstance(v, int) and v > 0 else "Rank deve ser int positivo",
    "Situação": lambda v, r: (
        None if v in SITUACOES_VALIDAS
        else f"Situação '{v}' fora do domínio {SITUACOES_VALIDAS}"
    ),
    "Modelo": lambda v, r: (
        "Modelo string vazia" if not v or not str(v).strip()
        else "Modelo sem marca e sem nome — scraper falhou em capturar campos"
        if not r.get("marca") or not r.get("modelo_raw")
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
    "Lance Máximo (R$)": lambda v, r: _checar_lance_maximo(v, r),
    "ROI anualizado (%)": lambda v, r: (
        "ROI anualizado >1000% sugere dias_giro=1 (floor deveria ser 30d)"
        if v is not None and v > 1000
        else None
    ),
    "Lucro/mês (R$)": lambda v, r: (
        "Lucro/mês negativo — verificar score_roi ou preco_alvo"
        if isinstance(v, (int, float)) and v < 0
        else None
    ),
    "Reforma (R$)": lambda v, r: (
        "Reforma negativa" if v is not None and v < 0 else None
    ),
    "Anúncio": lambda v, r: None,  # pode ser "—" ou fórmula HYPERLINK; ambos aceitáveis
    "Laudo": lambda v, r: None,    # "—" (sem URL ou decoy filtrado) ou HYPERLINK — ambos aceitáveis
}


# Extrai o valor de cada coluna a partir do dict interno enriquecido.
COLUMN_EXTRACTORS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "Rank": lambda r: r["rank"],
    "Situação": lambda r: r["situacao"],
    "Modelo": lambda r: r["modelo"],
    "Ano": lambda r: r["ano"],
    "Cidade": lambda r: r["cidade"],
    "Fim do Leilão": lambda r: r["fim_em"],
    "KM": lambda r: r["km"],
    "Lance Atual (R$)": lambda r: r["lance_atual"],
    "Lance Máximo (R$)": lambda r: r["preco_max"],
    "ROI anualizado (%)": lambda r: r["roi_anualizado"],
    "Lucro/mês (R$)": lambda r: r.get("lucro_mes", "—"),
    "Reforma (R$)": lambda r: r["reforma_estimada"],
    "Anúncio": lambda r: r["url"],
    "Laudo": lambda r: r.get("laudo_url") or "—",
}


def _build_rows(session: Session, sample_size: int) -> List[Dict[str, Any]]:
    """Últimas N avaliações com JOIN Lote + LEFT JOIN LaudoCache, ordenadas e ranqueadas.

    Espelha `SheetsExporter._query` — encerrados são filtrados, ativos viáveis
    vêm primeiro, desempate por folga de lance. Rank auditado coincide com o
    que o operador vê na planilha.
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
        laudo: Optional[LaudoCache] = session.get(LaudoCache, av.lote_id)

        viavel = av.preco_max > (lote.lance_atual or 0)

        detalhe_raw = (lote.raw_json or {}).get("detalhe") or {}
        encerrado_por_badge = bool(detalhe_raw.get("encerrado"))
        encerrado_por_timer = lote.fim_em is not None and lote.fim_em < agora
        encerrado = encerrado_por_badge or encerrado_por_timer

        roi_max = _calcular_roi_no_maximo(av)
        roi_anual = roi_anualizado(roi_max / 100.0, av.dias_giro_estimado) * 100

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
            "fipe": av.fipe,
            "preco_giro_fipe": av.preco_giro_fipe,
            "preco_giro_aa": av.preco_giro_aa,
            "fipe_pct_lance_minimo": lote.fipe_pct_lance_minimo,
            "roi_pct": roi_max,
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

        # Checagens cross-column que não ligam a uma coluna da planilha: rótulo
        # '<interno>' sinaliza invariante de dado persistido.
        for motivo in _checar_giro_vs_fipe(row):
            chave = ("<interno>", motivo)
            if chave not in agregador:
                agregador[chave] = {
                    "count": 1,
                    "exemplo_lote": row["lote_id"],
                    "exemplo_valor": f"fipe={row.get('fipe')}, giro_fipe={row.get('preco_giro_fipe')}, giro_aa={row.get('preco_giro_aa')}",
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
