"""Auditoria de laudos — garante que TODO carro na planilha tem laudo nos 3 eixos.

Eixos auditados (exatamente o que o usuário enxerga na planilha):

  1. **PDF baixado**     — arquivo existe em `data/laudos_pdfs/<lote_id>.pdf`
                           E passa em `_pdf_eh_laudo_valido` (não é decoy).
  2. **Revisado**        — `LaudoCache` existe E `confidence >= 0.6`
                           (=`laudo_analisado=True` no exporter).
  3. **Link na planilha** — `raw_json["detalhe"]["laudo_pdf_url"]` existe E
                            passa em `is_laudo_pdf_url()` (vira HYPERLINK
                            "Ver laudo" em vez de "—").

Para cada lote ativo (com `AvaliacaoLote` + `fim_em > now()`) classifica em
um motivo único — o pior eixo manda — e agrega.

Motivos classificados (ordem de prioridade):

  - `OK`                       — todos os 3 eixos verdes (estado desejado)
  - `URL_AUSENTE_NO_DB`        — scraper nunca achou URL (modal lazy, JS-pesado)
  - `URL_FILTRADA_DECOY`       — URL persistida mas é decoy (rodar limpar-decoys)
  - `PDF_NAO_BAIXADO`          — URL ok mas arquivo local não existe (download falhou,
                                  geralmente HTTP 429 rate-limit)
  - `PDF_LOCAL_INVALIDO`       — arquivo existe mas não é laudo de carro (corrompido/parcial)
  - `EXTRACAO_BAIXA_CONFIANCA` — PDF ok, LaudoCache existe, mas confidence < 0.6
                                  (visão LLM falhou e fallback `_laudo_sem_pdf` rodou)
  - `LAUDO_CACHE_AUSENTE`      — PDF ok mas pipeline nunca rodou no lote
                                  (só ingestão de listagem, sem _pipeline_lote)

Auto-heal (`auto_heal_local`): para a categoria EXTRACAO_BAIXA_CONFIANCA quando
o PDF local É VÁLIDO, re-roda `extrair_laudo` sem pedir nada à rede. Cobre o
caso clássico "Gemini 503 caiu no fallback no run inicial; agora voltou ao ar
ou tem cliente Anthropic disponível".

Importável como biblioteca (testes) e via CLI (`scripts/auditar_laudos.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from sqlmodel import Session, select

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.scraping.parsers import is_laudo_pdf_url


# Diretório dos PDFs persistidos. Espelha `_PDF_STORAGE_DIR` do orquestrador
# (não importamos de lá pra evitar pegar Playwright/asyncio só pra checar path).
_PDF_DIR = Path(__file__).resolve().parents[2] / "data" / "laudos_pdfs"

CONFIDENCE_MIN = 0.6


class MotivoLaudoFaltante(str, Enum):
    OK = "ok"
    URL_AUSENTE_NO_DB = "url_ausente_no_db"
    URL_FILTRADA_DECOY = "url_filtrada_decoy"
    PDF_NAO_BAIXADO = "pdf_nao_baixado"
    PDF_LOCAL_INVALIDO = "pdf_local_invalido"
    EXTRACAO_BAIXA_CONFIANCA = "extracao_baixa_confianca"
    LAUDO_CACHE_AUSENTE = "laudo_cache_ausente"


@dataclass
class StatusLote:
    """Status detalhado de 1 lote nos 3 eixos."""
    lote_id: str
    modelo: str
    pdf_local_existe: bool
    pdf_local_valido: bool
    laudo_cache_existe: bool
    laudo_confidence_ok: bool
    url_no_db: Optional[str]
    url_passa_filtro: bool
    motivo: MotivoLaudoFaltante

    @property
    def ok(self) -> bool:
        return self.motivo is MotivoLaudoFaltante.OK


@dataclass
class ResultadoAuditoria:
    empresa_id: str
    total_lotes_ativos: int = 0
    lotes_ok: int = 0
    por_motivo: Dict[MotivoLaudoFaltante, List[StatusLote]] = field(default_factory=dict)

    @property
    def total_problemas(self) -> int:
        return sum(len(v) for k, v in self.por_motivo.items() if k is not MotivoLaudoFaltante.OK)


def _pdf_path(lote_id: str) -> Path:
    return _PDF_DIR / f"{lote_id}.pdf"


def _pdf_eh_laudo_valido(pdf_path: Path) -> bool:
    """Mesma heurística do `orquestrador._pdf_eh_laudo_valido` — duplicada aqui
    pra não importar Playwright via cadeia do orquestrador. Se evoluir lá,
    sincronizar aqui (teste de paridade abaixo segura)."""
    if not pdf_path.exists() or pdf_path.stat().st_size < 5_000:
        return False
    try:
        import fitz  # PyMuPDF
        with fitz.open(str(pdf_path)) as pdf:
            if pdf.page_count == 0:
                return False
            txt = (pdf[0].get_text() or "").upper()
    except Exception:
        return False
    positivos = ("LAUDO", "INSPEÇÃO", "INSPECAO", "AVALIAÇÃO", "AVALIACAO",
                 "VEÍCULO", "VEICULO", "CHASSI", "PLACA")
    if not any(m in txt for m in positivos):
        return False
    negativos = ("TRANSPARÊNCIA SALARIAL", "TRANSPARENCIA SALARIAL",
                 "IGUALDADE SALARIAL", "RELATÓRIO DE TRANSPARÊNCIA")
    if any(m in txt for m in negativos):
        return False
    return True


def _classificar(
    pdf_local_existe: bool,
    pdf_local_valido: bool,
    laudo_cache_existe: bool,
    laudo_confidence_ok: bool,
    url_no_db: Optional[str],
    url_passa_filtro: bool,
) -> MotivoLaudoFaltante:
    """Retorna o motivo "raiz" — o eixo mais a montante quebrado.

    Ordem importa: se o link não existe NEM em DB nem como decoy, sequer
    podemos baixar PDF — esse é o problema raiz. Se URL é decoy, rodar
    `limpar-decoys` é a ação. Se URL ok mas PDF não baixou, é HTTP/rate-limit.
    Se PDF baixou mas não é laudo, validador rejeitou. Se tudo ok mas
    confidence baixa, visão falhou na extração.
    """
    if not url_no_db:
        return MotivoLaudoFaltante.URL_AUSENTE_NO_DB
    if not url_passa_filtro:
        return MotivoLaudoFaltante.URL_FILTRADA_DECOY
    if not pdf_local_existe:
        return MotivoLaudoFaltante.PDF_NAO_BAIXADO
    if not pdf_local_valido:
        return MotivoLaudoFaltante.PDF_LOCAL_INVALIDO
    if not laudo_cache_existe:
        return MotivoLaudoFaltante.LAUDO_CACHE_AUSENTE
    if not laudo_confidence_ok:
        return MotivoLaudoFaltante.EXTRACAO_BAIXA_CONFIANCA
    return MotivoLaudoFaltante.OK


def auditar(session: Session, empresa_id: str) -> ResultadoAuditoria:
    """Auditoria completa dos 3 eixos pra cada lote ativo da empresa.

    "Ativo" = mesmo critério do exporter (`fim_em > now()` — espelha o filtro
    duro de `_query`/`_write_sheet`). Lotes encerrados não entram porque o
    operador não pode mais dar lance neles.
    """
    resultado = ResultadoAuditoria(empresa_id=empresa_id)
    agora = datetime.now()

    avaliacoes = session.exec(
        select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa_id)
    ).all()

    for av in avaliacoes:
        lote = session.get(Lote, av.lote_id)
        if lote is None or lote.fim_em is None or lote.fim_em <= agora:
            continue

        resultado.total_lotes_ativos += 1

        # Eixo 1 — PDF
        pdf_path = _pdf_path(lote.id)
        pdf_local_existe = pdf_path.exists()
        pdf_local_valido = _pdf_eh_laudo_valido(pdf_path) if pdf_local_existe else False

        # Eixo 2 — Revisão
        laudo = session.get(LaudoCache, lote.id)
        laudo_cache_existe = laudo is not None
        laudo_confidence_ok = bool(laudo and (laudo.confidence or 0) >= CONFIDENCE_MIN)

        # Eixo 3 — Link na planilha
        det = (lote.raw_json or {}).get("detalhe") or {}
        url_raw = det.get("laudo_pdf_url")
        url_no_db = url_raw if isinstance(url_raw, str) and url_raw else None
        url_passa_filtro = is_laudo_pdf_url(url_no_db)

        motivo = _classificar(
            pdf_local_existe=pdf_local_existe,
            pdf_local_valido=pdf_local_valido,
            laudo_cache_existe=laudo_cache_existe,
            laudo_confidence_ok=laudo_confidence_ok,
            url_no_db=url_no_db,
            url_passa_filtro=url_passa_filtro,
        )

        status = StatusLote(
            lote_id=lote.id,
            modelo=f"{lote.marca} {lote.modelo} {lote.ano}",
            pdf_local_existe=pdf_local_existe,
            pdf_local_valido=pdf_local_valido,
            laudo_cache_existe=laudo_cache_existe,
            laudo_confidence_ok=laudo_confidence_ok,
            url_no_db=url_no_db,
            url_passa_filtro=url_passa_filtro,
            motivo=motivo,
        )

        if motivo is MotivoLaudoFaltante.OK:
            resultado.lotes_ok += 1
        resultado.por_motivo.setdefault(motivo, []).append(status)

    return resultado


# ---------------------------------------------------------------------------
# Auto-heal — só remediação OFFLINE (re-extração de PDFs locais)
# ---------------------------------------------------------------------------

@dataclass
class ResultadoHeal:
    re_extraidos: List[str] = field(default_factory=list)
    falhas: List[str] = field(default_factory=list)


def auto_heal_local(
    session: Session,
    resultado: ResultadoAuditoria,
    vision_client=None,
) -> ResultadoHeal:
    """Re-extrai laudo de PDFs locais válidos onde a confidence está baixa.

    Cobre o cenário: PDF baixado em run anterior ficou bom no disco, mas a
    chamada de visão na hora caiu (ex.: Gemini 503), gravando LaudoCache com
    confidence 0.5/0.6. Agora com cliente vision novo (ou Anthropic Haiku
    como fallback) podemos re-extrair sem pedir 1 byte à rede.

    Não tenta nada que dependa de orquestrador/Playwright — esses cenários
    (`URL_AUSENTE_NO_DB`, `PDF_NAO_BAIXADO`) precisam de re-scrape e ficam pro
    cron de retry.

    Quando `vision_client=None`, usa o build_default_client. Se nem isso
    existir (sem creds), pula sem erro.
    """
    from carros_sa.agents.extrator_laudo import extrair_laudo
    from carros_sa.orquestrador import _upsert_laudo_cache

    heal = ResultadoHeal()

    candidatos = list(resultado.por_motivo.get(MotivoLaudoFaltante.EXTRACAO_BAIXA_CONFIANCA, []))
    candidatos += list(resultado.por_motivo.get(MotivoLaudoFaltante.LAUDO_CACHE_AUSENTE, []))
    if not candidatos:
        return heal

    if vision_client is None:
        try:
            from carros_sa.agents.vision_clients import build_default_client
            vision_client = build_default_client()
        except Exception:
            return heal  # sem cliente, sem heal

    for status in candidatos:
        if not status.pdf_local_valido:
            continue
        pdf_path = _pdf_path(status.lote_id)
        try:
            laudo = extrair_laudo(pdf_path, vision_client)
        except Exception as exc:
            heal.falhas.append(f"{status.lote_id}: {type(exc).__name__}: {exc}")
            continue

        if laudo.confidence < CONFIDENCE_MIN:
            # Re-extração também não convergiu — vai precisar do retry com nova
            # chamada de visão num cliente diferente. Não mascara como sucesso.
            heal.falhas.append(f"{status.lote_id}: confidence ainda {laudo.confidence:.2f}")
            continue

        _upsert_laudo_cache(status.lote_id, laudo, session)
        heal.re_extraidos.append(status.lote_id)

    if heal.re_extraidos:
        session.commit()

    return heal


# ---------------------------------------------------------------------------
# Render textual (CLI + testes)
# ---------------------------------------------------------------------------

_DESCRICOES = {
    MotivoLaudoFaltante.OK: "tudo ok — PDF baixado, revisado e link válido",
    MotivoLaudoFaltante.URL_AUSENTE_NO_DB: (
        "scraper não persistiu URL do PDF (DOM lazy, modal não abriu). "
        "Re-rodar triagem ou retry com `--somente-laudo-pendente`."
    ),
    MotivoLaudoFaltante.URL_FILTRADA_DECOY: (
        "URL no DB é decoy — rodar `make limpar-decoys` e depois retry."
    ),
    MotivoLaudoFaltante.PDF_NAO_BAIXADO: (
        "URL ok mas PDF local ausente (download falhou, provavelmente HTTP 429). "
        "Re-rodar retry com sleep maior."
    ),
    MotivoLaudoFaltante.PDF_LOCAL_INVALIDO: (
        "PDF local existe mas não é laudo de carro (decoy escapou validador). "
        "Apagar arquivo + rodar retry."
    ),
    MotivoLaudoFaltante.LAUDO_CACHE_AUSENTE: (
        "PDF ok, mas pipeline nunca chegou a extrair — provavelmente lote ingerido "
        "só pela listagem. Auto-heal cobre se vision_client disponível."
    ),
    MotivoLaudoFaltante.EXTRACAO_BAIXA_CONFIANCA: (
        "PDF ok, LaudoCache existe mas confidence < 0.6 (visão LLM falhou no run). "
        "Auto-heal re-extrai do PDF local."
    ),
}


def render_relatorio(resultado: ResultadoAuditoria, max_exemplos: int = 5) -> str:
    """Texto humano pronto pra stderr/log do cron."""
    linhas: List[str] = []
    linhas.append(
        f"Auditoria de laudos — empresa={resultado.empresa_id}  "
        f"ativos={resultado.total_lotes_ativos}  "
        f"ok={resultado.lotes_ok}  problemas={resultado.total_problemas}"
    )
    if resultado.total_problemas == 0:
        linhas.append("  ✓ todos os lotes ativos têm laudo baixado, revisado e link válido")
        return "\n".join(linhas)

    for motivo in MotivoLaudoFaltante:
        if motivo is MotivoLaudoFaltante.OK:
            continue
        items = resultado.por_motivo.get(motivo, [])
        if not items:
            continue
        linhas.append(f"\n  ⚠ {motivo.value}: {len(items)} lote(s)")
        linhas.append(f"     → {_DESCRICOES[motivo]}")
        amostra = items[:max_exemplos]
        for s in amostra:
            linhas.append(f"     · {s.lote_id}  {s.modelo}")
        if len(items) > max_exemplos:
            linhas.append(f"     · … (+{len(items) - max_exemplos} outros)")
    return "\n".join(linhas)
