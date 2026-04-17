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

SITUACOES_VALIDAS = {"✓ Viável", "✗ Caro demais", "⚠ Encerrado"}
SEVERIDADES_VALIDAS = {"nenhuma", "leve", "média", "media", "grave", "estrutural", "—", None}
MOTOR_VALIDOS = {"Sim", "NÃO", "—", True, False, None}
POPULARIDADE_VALIDA = {"blockbuster", "popular", "normal", "nicho", "iliquido", "—", None}


# Validator retorna None se ok; string com motivo se suspeito.
Validator = Callable[[Any, Dict[str, Any]], Optional[str]]


def _situacao(row: Dict[str, Any]) -> str:
    if row["encerrado"]:
        return "⚠ Encerrado"
    return "✓ Viável" if row["viavel"] else "✗ Caro demais"


CHECKS: Dict[str, Validator] = {
    "Rank": lambda v, r: None if isinstance(v, int) and v > 0 else "Rank deve ser int positivo",
    "Situação": lambda v, r: (
        None if v in SITUACOES_VALIDAS
        else f"Situação '{v}' fora do domínio {SITUACOES_VALIDAS}"
    ),
    "Lote ID": lambda v, r: "Lote ID vazio" if not v else None,
    "Modelo": lambda v, r: (
        "Modelo string vazia" if not v or not str(v).strip()
        else "Modelo sem marca e sem nome (só ano) — scraper falhou em capturar campos"
        if not r.get("marca") or not r.get("modelo_raw")
        else None
    ),
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
        else None
    ),
    "FIPE (R$)": lambda v, r: (
        "FIPE zerado mas preco_giro existe — AvaliadorMercado falhou em popular FIPE?"
        if v == 0 and r.get("preco_giro_fipe")
        else None
    ),
    "Webmotors Mediana (R$)": lambda v, r: None,  # None é legítimo (scraper offline)
    "Preço Giro FIPE (R$)": lambda v, r: (
        "Preço Giro FIPE não-positivo" if v is not None and v <= 0 else None
    ),
    "Preço Giro Auto Avaliar (R$)": lambda v, r: (
        "Preço Giro AA não-positivo" if v is not None and v <= 0 else None
    ),
    "FIPE % (lance min)": lambda v, r: None,  # string formatada do DOM
    "ROI se pagar o máximo (%)": lambda v, r: (
        "ROI >500% improvável — revisar preco_giro vs capital_total"
        if v is not None and v > 500
        else (
            "ROI <-80% num lote 'Viável' é contraditório (se prejuízo é tão grande, status deveria ser 'Caro demais')"
            if v is not None and v < -80 and r["situacao"] == "✓ Viável"
            else None
        )
    ),
    "Dias até venda (est.)": lambda v, r: (
        "Dias até venda deve ser >=1; valor <=0 indica bug no CalibracaoGiro"
        if v is not None and v <= 0
        else None
    ),
    "ROI anualizado (%)": lambda v, r: (
        "ROI anualizado >1000% sugere dias_giro=1 (floor deveria ser 30d)"
        if v is not None and v > 1000
        else None
    ),
    "Fator Risco": lambda v, r: (
        "Fator Risco fora de [0.5, 1.5] — bounds típicos do precificador"
        if v is not None and not (0.5 <= v <= 1.5)
        else None
    ),
    "Popularidade": lambda v, r: (
        None if v in POPULARIDADE_VALIDA
        else f"Popularidade '{v}' fora do enum BucketPopularidade"
    ),
    "Severidade Laudo": lambda v, r: (
        None if v in SEVERIDADES_VALIDAS
        else f"Severidade '{v}' fora do domínio do ExtratorLaudo"
    ),
    "Motor OK": lambda v, r: (
        None if v in MOTOR_VALIDOS else f"Motor OK '{v}' inesperado (esperado Sim/NÃO/—)"
    ),
    "Reforma Estimada (R$)": lambda v, r: (
        "Reforma estimada negativa" if v is not None and v < 0 else None
    ),
    "Frete (R$)": lambda v, r: (
        "Frete negativo" if v is not None and v < 0 else None
    ),
    "Justificativa": lambda v, r: (
        "Justificativa string vazia — precificador deveria sempre escrever o racional"
        if not v else None
    ),
    "URL": lambda v, r: None,  # pode ser "—" ou fórmula HYPERLINK; ambos aceitáveis
    "Coletado em": lambda v, r: (
        "Coletado em vazio — Lote.scraped_at não foi populado"
        if not v or v == "—"
        else None
    ),
}


# Extrai o valor de cada coluna a partir do dict interno enriquecido.
# Chaves do dict interno: lote_id, modelo, fim_em, km, lance_atual, preco_max,
# fipe, webmotors_mediana, preco_giro_fipe, preco_giro_aa, fipe_pct_lance_minimo,
# roi_pct, dias_giro, roi_anualizado, fator_risco, severidade, motor_ok,
# reforma_estimada, frete, justificativa, url, scraped_at, rank, situacao,
# encerrado, viavel, preco_giro.
COLUMN_EXTRACTORS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "Rank": lambda r: r["rank"],
    "Situação": lambda r: r["situacao"],
    "Lote ID": lambda r: r["lote_id"],
    "Modelo": lambda r: r["modelo"],
    "Fim do Leilão": lambda r: r["fim_em"],
    "KM": lambda r: r["km"],
    "Lance Atual (R$)": lambda r: r["lance_atual"],
    "Lance Máximo (R$)": lambda r: r["preco_max"],
    "FIPE (R$)": lambda r: r["fipe"],
    "Webmotors Mediana (R$)": lambda r: r["webmotors_mediana"],
    "Preço Giro FIPE (R$)": lambda r: r["preco_giro_fipe"],
    "Preço Giro Auto Avaliar (R$)": lambda r: r["preco_giro_aa"],
    "FIPE % (lance min)": lambda r: r["fipe_pct_lance_minimo"],
    "ROI se pagar o máximo (%)": lambda r: r["roi_pct"],
    "Dias até venda (est.)": lambda r: r["dias_giro"],
    "ROI anualizado (%)": lambda r: r["roi_anualizado"],
    "Fator Risco": lambda r: r["fator_risco"],
    "Popularidade": lambda r: r.get("popularidade", "—"),
    "Severidade Laudo": lambda r: r["severidade"],
    "Motor OK": lambda r: r["motor_ok"],
    "Reforma Estimada (R$)": lambda r: r["reforma_estimada"],
    "Frete (R$)": lambda r: r["frete"],
    "Justificativa": lambda r: r["justificativa"],
    "URL": lambda r: r["url"],
    "Coletado em": lambda r: r["scraped_at"],
}


def _build_rows(session: Session, sample_size: int) -> List[Dict[str, Any]]:
    """Últimas N avaliações com JOIN Lote + LEFT JOIN LaudoCache, ordenadas e ranqueadas.

    Espelha `SheetsExporter._query` de forma simplificada — o critério de ordenação
    é o mesmo (encerrados últimos, viáveis primeiro, maior folga depois) pra que
    o Rank auditado seja o mesmo Rank que apareceria na planilha.
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

        rows.append({
            "lote_id": av.lote_id,
            "modelo": f"{lote.marca} {lote.modelo} {lote.ano}".strip(),
            "marca": lote.marca,
            "modelo_raw": lote.modelo,
            "fim_em": fim_em_str,
            "km": lote.km,
            "lance_atual": lote.lance_atual or 0,
            "preco_max": av.preco_max,
            "fipe": av.fipe,
            "webmotors_mediana": av.webmotors_mediana,
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

    # Ordenação espelha SheetsExporter.exportar — pra o Rank auditado coincidir
    # com o que o operador veria.
    rows.sort(key=lambda r: (
        1 if r["encerrado"] else 0,
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

    saida: List[str] = []
    for (coluna, motivo), info in agregador.items():
        suffix = "" if info["count"] == 1 else "s"
        saida.append(
            f"⚠ {coluna}: {info['count']} linha{suffix} — {motivo} "
            f"(ex: lote {info['exemplo_lote']} valor={info['exemplo_valor']!r})"
        )
    return saida
