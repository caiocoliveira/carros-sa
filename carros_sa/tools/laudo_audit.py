"""Auditoria de completude de laudo por lote ativo.

Critério "laudo completo" — TODAS as 3 condições simultaneamente:

  1. **PDF persistido em disco** — `<pdf_dir>/<lote_id>.pdf` existe e tem
     tamanho mínimo (>5KB descarta arquivos vazios/corrompidos). É o que
     `_pdf_persistente_path` do orquestrador escreve durante a triagem.

  2. **LaudoCache extraído** — linha em `LaudoCache` com `confidence >= 0.6`,
     i.e. veio de PDF real (visão Gemini ou textual com avarias) e não do
     fallback `_laudo_sem_pdf` (confidence 0.5/0.55).

  3. **URL clicável** — `raw_json.detalhe.laudo_pdf_url` passa em
     `is_laudo_pdf_url` — i.e. é uma URL de laudo de fato (não decoy nem
     None). É o que o exporter renderiza como `=HYPERLINK("...", "Ver laudo")`.

Sem essa fonte única, cada camada do pipeline (`is_laudo_pdf_url` no scraper,
`_pdf_eh_laudo_valido` no orquestrador, `limpar_decoys` no cron) cuida de UM
sintoma e ninguém audita o resultado integrado. Esse módulo fecha o laço.

Uso típico:
    from carros_sa.tools.laudo_audit import auditar
    rel = auditar(session, "carros_uberlandia")
    if rel.incompletos:
        for s in rel.incompletos:
            print(s.lote_id, s.motivo)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from sqlmodel import Session, select

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.scraping.parsers import is_laudo_pdf_url

# Diretório onde o orquestrador persiste PDFs baixados em produção. Espelha
# `_PDF_STORAGE_DIR` do orquestrador. Mantido fora de tmp_dir pra permitir
# auditoria/reprocessamento offline.
PDF_DIR_DEFAULT = Path(__file__).resolve().parent.parent.parent / "data" / "laudos_pdfs"

# Tamanho mínimo plausível pra um PDF de laudo (heurística). Auto Avaliar gera
# laudos de 200KB-2MB; arquivos <5KB são quase sempre erro de download (HTML
# de erro salvo, redirect interceptado).
_PDF_MIN_BYTES = 5_000


@dataclass
class StatusLaudo:
    """Status individual de um lote — 3 booleanos + motivo agregado."""
    lote_id: str
    modelo: str
    pdf_local: bool
    laudo_cache_ok: bool
    url_persistida_ok: bool
    motivo: Optional[str] = None  # None se completo

    @property
    def completo(self) -> bool:
        return self.pdf_local and self.laudo_cache_ok and self.url_persistida_ok


@dataclass
class RelatorioLaudos:
    total: int = 0
    completos: int = 0
    incompletos: List[StatusLaudo] = field(default_factory=list)
    sem_pdf: int = 0
    cache_baixa_conf: int = 0
    url_invalida: int = 0


def _motivo(s: StatusLaudo) -> Optional[str]:
    falhas: List[str] = []
    if not s.pdf_local:
        falhas.append("pdf_ausente")
    if not s.laudo_cache_ok:
        falhas.append("cache_confianca_baixa")
    if not s.url_persistida_ok:
        falhas.append("url_invalida_ou_ausente")
    return ", ".join(falhas) if falhas else None


def verificar_laudo_completo(
    lote: Lote,
    laudo: Optional[LaudoCache],
    pdf_dir: Path = PDF_DIR_DEFAULT,
) -> StatusLaudo:
    """Status individual: 3 condições + motivo agregado quando incompleto."""
    pdf_path = pdf_dir / f"{lote.id}.pdf"
    pdf_ok = pdf_path.exists() and pdf_path.stat().st_size > _PDF_MIN_BYTES

    laudo_ok = laudo is not None and (laudo.confidence or 0) >= 0.6

    detalhe = (lote.raw_json or {}).get("detalhe") if isinstance(lote.raw_json, dict) else None
    url = (detalhe or {}).get("laudo_pdf_url") if isinstance(detalhe, dict) else None
    drive_url = (detalhe or {}).get("laudo_drive_url") if isinstance(detalhe, dict) else None
    # Aceita qualquer URL renderizável: Drive permanente OU storage pré-assinado.
    # O Drive é o estado-alvo (sobrevive entre runs); o storage é fallback de
    # curta duração. Audit considera URL ok se qualquer um dos dois existir e
    # for válido — espelha a precedência do exporter.
    url_ok = bool(drive_url) or is_laudo_pdf_url(url)

    s = StatusLaudo(
        lote_id=lote.id,
        modelo=f"{lote.marca or '?'} {lote.modelo or '?'} {lote.ano or '?'}".strip(),
        pdf_local=pdf_ok,
        laudo_cache_ok=laudo_ok,
        url_persistida_ok=url_ok,
    )
    s.motivo = _motivo(s)
    return s


def auditar(
    session: Session,
    empresa_id: str,
    *,
    pdf_dir: Path = PDF_DIR_DEFAULT,
    apenas_ativos: bool = True,
) -> RelatorioLaudos:
    """Audita lotes que apareceriam na planilha de uma empresa.

    Por default só conta lotes ATIVOS — espelha o filtro do `SheetsExporter`:
    avaliados, com `fim_em` no futuro, sem badge `encerrado`. Lote que já saiu
    da tela não precisa de laudo "perfeito" — ninguém vai dar lance.
    """
    avaliacoes = session.exec(
        select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa_id)
    ).all()

    relatorio = RelatorioLaudos()
    agora = datetime.now()

    for av in avaliacoes:
        lote = session.get(Lote, av.lote_id)
        if lote is None:
            continue
        if apenas_ativos:
            if lote.fim_em is None or lote.fim_em < agora:
                continue
            detalhe_raw = (lote.raw_json or {}).get("detalhe") or {}
            if isinstance(detalhe_raw, dict) and detalhe_raw.get("encerrado"):
                continue

        laudo = session.get(LaudoCache, lote.id)
        s = verificar_laudo_completo(lote, laudo, pdf_dir)
        relatorio.total += 1
        if s.completo:
            relatorio.completos += 1
        else:
            relatorio.incompletos.append(s)
            if not s.pdf_local:
                relatorio.sem_pdf += 1
            if not s.laudo_cache_ok:
                relatorio.cache_baixa_conf += 1
            if not s.url_persistida_ok:
                relatorio.url_invalida += 1

    return relatorio
