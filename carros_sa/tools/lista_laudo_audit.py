"""Auditoria do invariante 'todo carro na planilha tem laudo baixado + revisado + linkado'.

Cruza os 3 sinais que precisam coexistir pra um lote estar 'completo' do ponto
de vista do operador:

  1. **Baixado**  — arquivo PDF persistente em `data/laudos_pdfs/<lote_id>.pdf`.
                    Sem isso, qualquer reprocessamento offline (incluindo
                    re-extração com vision_client diferente) re-baixa do
                    Auto Avaliar — e expõe o pipeline a HTTP 429.

  2. **Revisado** — `LaudoCache` com `confidence >= 0.6`. Confidence baixa
                    sinaliza fallback `_laudo_sem_pdf` (sem PDF / vision falhou)
                    e o operador vê 'LAUDO NÃO ANALISADO' na planilha — mesmo
                    cenário de "lance no escuro" que o usuário pediu pra evitar.

  3. **Linkado**  — `raw_json.detalhe.laudo_pdf_url` presente E passa em
                    `is_laudo_pdf_url()`. Sem isso a célula 'Laudo' no Sheets
                    fica '—' e o operador não consegue conferir o PDF antes
                    de dar lance.

Por que esse módulo é separado do `tools/audit.py` (auditor de colunas):
  - audit.py valida invariantes determinísticas POR COLUNA da planilha;
    não cruza com filesystem (PDF baixado) nem com URL (decoy).
  - este módulo valida o INVARIANTE TRANSVERSAL "todos os 3 sinais coexistem"
    e é a fonte de verdade pro guard de DB real (`tests/test_lista_laudo_guard.py`).

A ordem de causa raiz importa: URL_AUSENTE bloqueia tudo abaixo, então
reportamos só a primeira causa por lote (não inflamos o output com sintomas
derivados). Operador trabalha de cima pra baixo: resolveu URL, roda retry,
e os outros gaps somem em cascata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional

from sqlmodel import Session, select

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.scraping.parsers import is_laudo_pdf_url

# Mesmo path usado pelo orquestrador em `_pdf_persistente_path`. Mantido como
# constante separada porque importar do orquestrador puxaria Playwright e
# circulares no test path. Se o orquestrador mudar de pasta, sincronizar aqui
# (test do guard pega o desalinhamento).
_PDF_STORAGE_DIR = Path(__file__).resolve().parents[2] / "data" / "laudos_pdfs"

# Threshold de "laudo realmente revisado" — espelha o filtro do SheetsExporter
# (`laudo_analisado = confidence >= 0.6`). Mudou lá → muda aqui.
_CONFIDENCE_MIN_REVISADO = 0.6


class CausaGap(str, Enum):
    """Por que esse lote não está 'completo' na planilha.

    Ordenadas por causa raiz: resolver URL_AUSENTE/URL_DECOY destrava o
    download, que destrava a revisão. Reportamos só a primeira encontrada.
    """

    URL_AUSENTE = "url_ausente"
    URL_DECOY = "url_decoy"
    PDF_NAO_BAIXADO = "pdf_nao_baixado"
    LAUDO_NAO_REVISADO = "laudo_nao_revisado"


@dataclass
class LaudoGap:
    lote_id: str
    modelo: str
    causa: CausaGap
    detalhe: str = ""


@dataclass
class ResultadoAuditoria:
    empresa_id: str
    total_na_planilha: int = 0
    completos: int = 0
    gaps: List[LaudoGap] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.gaps is None:
            self.gaps = []

    @property
    def total_gaps(self) -> int:
        return len(self.gaps)

    def gaps_por_causa(self) -> dict:
        agg: dict = {}
        for g in self.gaps:
            agg[g.causa.value] = agg.get(g.causa.value, 0) + 1
        return agg


def _identificar_gap(lote: Lote, laudo: Optional[LaudoCache]) -> Optional[LaudoGap]:
    """Retorna o primeiro gap encontrado pra um lote, em ordem de causa raiz.

    None significa lote completo (3 invariantes satisfeitos).
    """
    detalhe_raw = (lote.raw_json or {}).get("detalhe") or {}
    laudo_url = detalhe_raw.get("laudo_pdf_url")

    modelo = f"{lote.marca} {lote.modelo} {lote.ano}".strip()

    # 1. Sem URL nada funciona: scraper não conseguiu extrair link do modal.
    #    Causa típica: modal lazy-load que não renderizou no tempo do JS,
    #    ou status "SEM LAUDO" no anúncio (lote sem inspeção do AA).
    if not laudo_url:
        return LaudoGap(
            lote_id=lote.id, modelo=modelo, causa=CausaGap.URL_AUSENTE,
            detalhe="raw_json.detalhe.laudo_pdf_url=None — scraper não achou link do PDF no DOM",
        )

    # 2. URL presente mas envenenada (rodapé institucional, listagem genérica).
    #    `limpar_decoys` remove esses, mas se um padrão novo escapar pelo
    #    scraper, esse audit pega antes de virar problema operacional.
    if not is_laudo_pdf_url(laudo_url):
        url_curto = laudo_url[:100] + "…" if len(laudo_url) > 100 else laudo_url
        return LaudoGap(
            lote_id=lote.id, modelo=modelo, causa=CausaGap.URL_DECOY,
            detalhe=f"URL não passa em is_laudo_pdf_url(): {url_curto}",
        )

    # 3. PDF não persistido localmente. Pode ter sido baixado em um run e
    #    apagado depois (limpeza manual de tmp), ou rejeitado por
    #    `_pdf_eh_laudo_valido` no orquestrador (URL parece laudo mas
    #    conteúdo do PDF não bate). Sem PDF não dá pra reprocessar offline.
    pdf_path = _PDF_STORAGE_DIR / f"{lote.id}.pdf"
    if not pdf_path.exists():
        return LaudoGap(
            lote_id=lote.id, modelo=modelo, causa=CausaGap.PDF_NAO_BAIXADO,
            detalhe=f"{pdf_path.relative_to(_PDF_STORAGE_DIR.parents[1])} ausente — re-baixar via retry",
        )

    # 4. PDF existe mas LaudoCache ausente OU veio de fallback (confidence<0.6).
    #    Vision client falhou + textual sem avarias (raro, mas observado em
    #    abril/2026 quando Gemini ficou em 503 e ANTHROPIC_API_KEY não estava
    #    setado no .env).
    if laudo is None:
        return LaudoGap(
            lote_id=lote.id, modelo=modelo, causa=CausaGap.LAUDO_NAO_REVISADO,
            detalhe="LaudoCache ausente — extrair_laudo nunca rodou pra este lote",
        )
    conf = laudo.confidence or 0
    if conf < _CONFIDENCE_MIN_REVISADO:
        return LaudoGap(
            lote_id=lote.id, modelo=modelo, causa=CausaGap.LAUDO_NAO_REVISADO,
            detalhe=f"LaudoCache.confidence={conf:.2f} < {_CONFIDENCE_MIN_REVISADO} (fallback _laudo_sem_pdf)",
        )

    return None


def auditar_lista_laudos(
    session: Session,
    empresa_id: str,
    *,
    agora: Optional[datetime] = None,
) -> ResultadoAuditoria:
    """Audita o invariante laudo-completo pra todo lote QUE APARECERIA na planilha.

    Espelha os filtros do `SheetsExporter._query` + `exportar`:
      - só `AvaliacaoLote` da empresa
      - lote precisa existir
      - `lote.fim_em` precisa estar definido (sem countdown = removido pelo export)
      - lote não pode estar encerrado (timer vencido OU badge ARREMATADO)

    Retorna um `ResultadoAuditoria` com contagem total + lista de gaps.
    Lote sem gap NÃO entra na lista — `total_na_planilha - total_gaps = completos`.
    """
    agora = agora or datetime.now()
    avaliacoes = session.exec(
        select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa_id)
    ).all()

    result = ResultadoAuditoria(empresa_id=empresa_id)

    for av in avaliacoes:
        lote = session.get(Lote, av.lote_id)
        if lote is None:
            continue
        if lote.fim_em is None:
            # Espelha SheetsExporter._query — lote sem countdown não entra na planilha.
            continue

        # Encerrado: badge persistido OU timer vencido. Espelha SheetsExporter.exportar.
        detalhe_raw = (lote.raw_json or {}).get("detalhe") or {}
        if bool(detalhe_raw.get("encerrado")):
            continue
        if lote.fim_em < agora:
            continue

        result.total_na_planilha += 1
        laudo = session.get(LaudoCache, av.lote_id)
        gap = _identificar_gap(lote, laudo)
        if gap is None:
            result.completos += 1
        else:
            result.gaps.append(gap)

    return result
