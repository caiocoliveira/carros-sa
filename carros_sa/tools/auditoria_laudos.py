"""Auditoria de laudos — classifica o estado do laudo de cada lote ativo.

Pergunta que este módulo responde: **"o operador, olhando a planilha, pode
clicar em 'Ver laudo' E confiar nos números do lote?"**. Se não, o módulo
aponta a causa específica entre 5 modos de falha observados em produção:

  - `SEM_DETALHE`     — página de detalhe nunca foi raspada (só a listagem)
  - `SEM_URL`         — detalhe raspado mas scraper não achou `laudo_pdf_url`
  - `URL_DECOY`       — URL presente mas reprovada por `is_laudo_pdf_url`
                        (ex.: Relatório de Transparência Salarial)
  - `PDF_AUSENTE`     — URL válida mas `data/laudos_pdfs/<lote>.pdf` sumiu
  - `EXTRACAO_FALHOU` — PDF no disco mas `LaudoCache` tem confidence < 0.6

Um lote só é `OK` quando TODOS passam: detalhe raspado, URL válida, PDF baixado
e laudo extraído com confidence ≥ 0.6. O motivo específico vira sufixo da
coluna "Situação" na planilha — a string mantém o prefixo histórico
"LAUDO NÃO ANALISADO" pra compatibilidade com automações que filtravam por ele.

Usado em três pontos:
  1. `scripts/auditar_laudos.py` — CLI que reporta e retorna exit != 0 se há gap.
  2. `carros_sa/tools/sheets.py` — célula "Situação" mostra o motivo específico.
  3. `tests/test_auditoria_laudos.py` — cada modo coberto + hygiene do DB real.
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


# Limite de confiança que conta como "laudo revisado de verdade". Abaixo disso
# é fallback sem PDF (0.5) ou textual magro — operador não pode dar lance.
CONFIDENCE_MIN_OK = 0.6

# Diretório onde o orquestrador persiste PDFs baixados (espelha o default em
# `carros_sa.orquestrador._PDF_STORAGE_DIR`). Trabalhamos com Path absoluto
# pra o auditor rodar de qualquer cwd.
_PDF_DIR_DEFAULT = (
    Path(__file__).resolve().parent.parent.parent / "data" / "laudos_pdfs"
)


class StatusLaudo(str, Enum):
    OK = "ok"
    SEM_DETALHE = "sem_detalhe"
    SEM_URL = "sem_url"
    URL_DECOY = "url_decoy"
    PDF_AUSENTE = "pdf_ausente"
    EXTRACAO_FALHOU = "extracao_falhou"


# Sufixos humanos usados na coluna "Situação" da planilha. O prefixo
# "LAUDO NÃO ANALISADO" é preservado porque filtros e automações do operador
# já dependem dele.
_SUFIXO_SITUACAO = {
    StatusLaudo.SEM_DETALHE: "detalhe não raspado",
    StatusLaudo.SEM_URL: "URL não encontrada",
    StatusLaudo.URL_DECOY: "URL decoy",
    StatusLaudo.PDF_AUSENTE: "PDF ausente",
    StatusLaudo.EXTRACAO_FALHOU: "extração falhou",
}


def classificar_status(
    lote: Lote,
    laudo: Optional[LaudoCache],
    *,
    pdf_dir: Path = _PDF_DIR_DEFAULT,
) -> StatusLaudo:
    """Classifica um lote em relação à revisibilidade do laudo na planilha.

    Caminho feliz tem precedência: se `LaudoCache.confidence >= 0.6`, o laudo
    foi extraído de verdade — o operador pode confiar nos números. Só caímos
    no diagnóstico quando não há cache confiável, e aí reportamos a primeira
    etapa do pipeline que falhou (detalhe → URL → decoy → PDF no disco).
    """
    if laudo is not None and (laudo.confidence or 0) >= CONFIDENCE_MIN_OK:
        return StatusLaudo.OK

    raw = lote.raw_json or {}
    detalhe = raw.get("detalhe")
    if not isinstance(detalhe, dict):
        return StatusLaudo.SEM_DETALHE

    url = detalhe.get("laudo_pdf_url")
    if not url:
        return StatusLaudo.SEM_URL
    if not is_laudo_pdf_url(url):
        return StatusLaudo.URL_DECOY

    pdf_path = pdf_dir / f"{lote.id}.pdf"
    if not pdf_path.exists():
        return StatusLaudo.PDF_AUSENTE

    return StatusLaudo.EXTRACAO_FALHOU


def situacao_label(status: StatusLaudo) -> Optional[str]:
    """String para a coluna 'Situação' quando há gap; None se `OK`.

    Mantém o prefixo histórico "LAUDO NÃO ANALISADO" pra não quebrar filtros
    visuais/automações que usuários já criaram em cima da planilha antiga.
    """
    if status == StatusLaudo.OK:
        return None
    return f"⚠ LAUDO NÃO ANALISADO · {_SUFIXO_SITUACAO[status]}"


@dataclass
class ResultadoAuditoria:
    """Snapshot da auditoria para uma empresa — contagens + IDs por status."""

    empresa_id: str
    total: int = 0
    por_status: Dict[StatusLaudo, int] = field(default_factory=dict)
    lotes_por_status: Dict[StatusLaudo, List[str]] = field(default_factory=dict)

    @property
    def ok(self) -> int:
        return self.por_status.get(StatusLaudo.OK, 0)

    @property
    def gaps(self) -> int:
        return self.total - self.ok

    def registrar(self, lote_id: str, status: StatusLaudo) -> None:
        self.total += 1
        self.por_status[status] = self.por_status.get(status, 0) + 1
        self.lotes_por_status.setdefault(status, []).append(lote_id)


def auditar_empresa(
    empresa_id: str,
    session: Session,
    *,
    pdf_dir: Path = _PDF_DIR_DEFAULT,
    agora: Optional[datetime] = None,
) -> ResultadoAuditoria:
    """Audita os lotes ATIVOS de uma empresa — mesmo universo da planilha.

    Mesmo filtro do SheetsExporter (`AvaliacaoLote` por empresa + `fim_em` no
    futuro). Lotes encerrados ou sem data de leilão não entram porque também
    não aparecem no Sheet — o operador não precisa agir.
    """
    agora = agora or datetime.now()
    res = ResultadoAuditoria(empresa_id=empresa_id)

    avaliacoes = session.exec(
        select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa_id)
    ).all()

    for av in avaliacoes:
        lote = session.get(Lote, av.lote_id)
        if lote is None or lote.fim_em is None or lote.fim_em < agora:
            continue
        laudo = session.get(LaudoCache, lote.id)
        status = classificar_status(lote, laudo, pdf_dir=pdf_dir)
        res.registrar(lote.id, status)

    return res
