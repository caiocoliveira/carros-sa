"""Diagnóstico — por que cada lote ativo (ainda) não tem laudo analisado.

Tira foto do que está pendente AGORA: percorre todos os lotes ativos
(`fim_em > now`, badge não-encerrado) e, pra cada um que apareceria como
"⚠ LAUDO NÃO ANALISADO" na planilha, classifica a causa raiz e aponta a
ação concreta que destrava.

Causas detectadas (`MotivoPendencia`):

  - SEM_DETALHE_RASPADO — orquestrador nem chegou a visitar a página do lote.
    Scrape inicial pode ter dropado o lote por timeout/erro genérico.
    Ação: `make triagem` ou `reprocessar_lotes_do_db --somente-sem-avaliacao`.

  - SCRAPER_NAO_ACHOU_PDF_URL — detalhe foi raspado mas o seletor não pegou
    o link do PDF (modal lazy não abriu, layout diferente, "SEM LAUDO").
    Ação: `reprocessar_lotes_do_db --somente-laudo-pendente` (re-tenta o modal).

  - URL_DECOY_PERSISTIDO — `laudo_pdf_url` no DB ainda passa como decoy
    (regressão do `is_laudo_pdf_url()` ou lote raspado antes do gate).
    Ação: `make limpar-decoys` (zera URL + derruba LaudoCache → próximo retry refaz).

  - FALLBACK_SEM_PDF — LaudoCache existe com `confidence < 0.6`, sintoma do
    `_laudo_sem_pdf` (URL ok mas download deu HTTP 429 OU PDF baixado
    rejeitado por `_pdf_eh_laudo_valido`). Cada retry re-tenta automaticamente;
    se persiste em vários ciclos, há rate-limit OU o PDF é decoy não-detectado.
    Ação: `reprocessar_lotes_do_db --somente-laudo-pendente`. Se não cair em
    1-2 ciclos, conferir manualmente o link do anúncio.

A função pública `auditar_laudos_pendentes(session)` retorna `List[LaudoPendente]`.
A formatação humana fica em `formatar_relatorio()` — usada pelo hook do audit
e pelo `make audit`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import List, Optional

from sqlmodel import Session, select

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.scraping.parsers import is_laudo_pdf_url


class MotivoPendencia(str, Enum):
    SEM_DETALHE_RASPADO = "sem_detalhe_raspado"
    SCRAPER_NAO_ACHOU_PDF_URL = "scraper_nao_achou_pdf_url"
    URL_DECOY_PERSISTIDO = "url_decoy_persistido"
    FALLBACK_SEM_PDF = "fallback_sem_pdf"


# Ação recomendada por motivo (texto pronto pro relatório).
ACAO_RECOMENDADA = {
    MotivoPendencia.SEM_DETALHE_RASPADO:
        "rode `make triagem` ou `reprocessar_lotes_do_db --somente-sem-avaliacao`",
    MotivoPendencia.SCRAPER_NAO_ACHOU_PDF_URL:
        "rode `reprocessar_lotes_do_db --somente-laudo-pendente` (re-tenta o modal)",
    MotivoPendencia.URL_DECOY_PERSISTIDO:
        "rode `make limpar-decoys` (zera URL decoy + derruba LaudoCache)",
    MotivoPendencia.FALLBACK_SEM_PDF:
        "rode `reprocessar_lotes_do_db --somente-laudo-pendente`; "
        "se persistir 2+ ciclos, conferir o anúncio manualmente",
}


@dataclass(frozen=True)
class LaudoPendente:
    """Um lote ativo que aparece como ⚠ LAUDO NÃO ANALISADO + diagnóstico."""

    lote_id: str
    modelo: str               # "Marca Modelo Ano"
    cidade: str               # "Cidade/UF" ou "—"
    url: str
    motivo: MotivoPendencia
    detalhe: str = ""         # contexto extra (URL decoy, confidence, etc.)

    @property
    def acao(self) -> str:
        return ACAO_RECOMENDADA[self.motivo]


def _ativo(lote: Lote, agora: datetime) -> bool:
    """Lote ativo = aparece na planilha (mesma regra do SheetsExporter)."""
    if lote.fim_em is None:
        return False
    if lote.fim_em < agora:
        return False
    detalhe = (lote.raw_json or {}).get("detalhe") or {}
    if detalhe.get("encerrado"):
        return False
    return True


def _classificar(lote: Lote, laudo: Optional[LaudoCache]) -> Optional[tuple[MotivoPendencia, str]]:
    """Retorna (motivo, detalhe) se o lote está pendente; None se está OK.

    Ordem importa: cobrimos a causa raiz mais específica primeiro pra dar a
    instrução acionável correta. Ex.: lote com URL-decoy + LaudoCache fallback
    é classificado como URL_DECOY_PERSISTIDO (acão: limpar-decoys), não como
    FALLBACK_SEM_PDF (que assumiria URL ok).
    """
    raw = lote.raw_json or {}
    detalhe = raw.get("detalhe") if isinstance(raw, dict) else None

    # Caso 1: detalhe nunca raspado — pipeline não chegou aqui.
    if not isinstance(detalhe, dict):
        return MotivoPendencia.SEM_DETALHE_RASPADO, "raw_json sem chave 'detalhe'"

    pdf_url = detalhe.get("laudo_pdf_url")

    # Caso 2: URL persistida não passa no gate is_laudo_pdf_url → decoy
    # (verificado ANTES do None pq decoy é problema mais específico/acionável).
    if pdf_url and not is_laudo_pdf_url(pdf_url):
        return (
            MotivoPendencia.URL_DECOY_PERSISTIDO,
            f"laudo_pdf_url={pdf_url[:80]}…" if len(pdf_url) > 80 else f"laudo_pdf_url={pdf_url}",
        )

    # Caso 3: laudo já está OK — não está pendente, retorna None.
    if laudo is not None and (laudo.confidence or 0) >= 0.6:
        return None

    # Caso 4: scraper raspou detalhe mas não achou URL de laudo.
    if not pdf_url:
        status = detalhe.get("status_laudo") or "—"
        return (
            MotivoPendencia.SCRAPER_NAO_ACHOU_PDF_URL,
            f"status_laudo={status}",
        )

    # Caso 5: URL ok no DB, LaudoCache existe mas com confidence baixa →
    # download falhou ou PDF foi rejeitado por `_pdf_eh_laudo_valido`.
    if laudo is not None:
        return (
            MotivoPendencia.FALLBACK_SEM_PDF,
            f"confidence={laudo.confidence:.2f} (fallback `_laudo_sem_pdf`)",
        )

    # Caso 6: URL ok mas LaudoCache totalmente ausente → orquestrador
    # raspou detalhe e parou. Mesma ação que fallback (retry --somente-laudo-pendente).
    return (
        MotivoPendencia.FALLBACK_SEM_PDF,
        "LaudoCache ausente (pipeline interrompido após scrape do detalhe)",
    )


def auditar_laudos_pendentes(session: Session, *, empresa_id: Optional[str] = None) -> List[LaudoPendente]:
    """Lista lotes ativos sem laudo analisado, com motivo classificado.

    Quando `empresa_id` é passado, restringe a lotes que TÊM AvaliacaoLote
    daquela empresa (= aparecem na planilha dela). Sem `empresa_id`, varre
    todos os lotes ativos do DB — útil pra hygiene check geral.
    """
    agora = datetime.now()

    if empresa_id is not None:
        # select(LaudoCache.lote_id) retorna scalars (str), não tuplas — depende
        # da versão do SQLAlchemy/SQLModel. Materializa direto pra evitar
        # surpresa entre `row[0]` e `row` puro.
        avaliados_ids = set(session.exec(
            select(AvaliacaoLote.lote_id).where(AvaliacaoLote.empresa_id == empresa_id)
        ).all())
        lotes = [
            l for l in session.exec(select(Lote)).all()
            if l.id in avaliados_ids and _ativo(l, agora)
        ]
    else:
        lotes = [l for l in session.exec(select(Lote)).all() if _ativo(l, agora)]

    pendentes: List[LaudoPendente] = []
    for lote in lotes:
        laudo = session.get(LaudoCache, lote.id)
        resultado = _classificar(lote, laudo)
        if resultado is None:
            continue
        motivo, detalhe_str = resultado
        cidade = (
            f"{lote.origem_cidade}/{lote.origem_uf}"
            if lote.origem_cidade and lote.origem_uf
            else "—"
        )
        pendentes.append(LaudoPendente(
            lote_id=lote.id,
            modelo=f"{lote.marca} {lote.modelo} {lote.ano}".strip(),
            cidade=cidade,
            url=lote.url,
            motivo=motivo,
            detalhe=detalhe_str,
        ))
    return pendentes


def formatar_relatorio(pendentes: List[LaudoPendente]) -> List[str]:
    """Formata lista de pendentes em linhas prontas pra impressão (uma por motivo).

    Agrupa por motivo + ação: cada motivo vira UMA linha agregada com contagem
    e amostra de até 3 lote_ids. Sem pendência → lista vazia.
    """
    if not pendentes:
        return []

    por_motivo: dict[MotivoPendencia, list[LaudoPendente]] = {}
    for p in pendentes:
        por_motivo.setdefault(p.motivo, []).append(p)

    saida: List[str] = []
    for motivo, items in sorted(por_motivo.items(), key=lambda kv: -len(kv[1])):
        amostra = ", ".join(p.lote_id for p in items[:3])
        if len(items) > 3:
            amostra += f", +{len(items) - 3}"
        suffix = "" if len(items) == 1 else "s"
        saida.append(
            f"⚠ Laudo pendente ({motivo.value}): {len(items)} lote{suffix} — "
            f"{ACAO_RECOMENDADA[motivo]} (ex: {amostra})"
        )
    return saida
