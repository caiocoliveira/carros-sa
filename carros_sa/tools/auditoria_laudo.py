"""Auditoria de completude de laudo — garante que todo lote na planilha tem laudo OK.

A planilha só é útil pro operador se cada linha traz as 3 coisas que ele usa
pra decidir o lance:

1. **link na planilha**  → `raw_json.detalhe.laudo_pdf_url` passa em
   `is_laudo_pdf_url()` (não é decoy, aponta pra host conhecido de laudo).
2. **PDF baixado**       → `data/laudos_pdfs/<lote_id>.pdf` existe e passa no
   validador de conteúdo `_pdf_eh_laudo_valido` (tem marcadores positivos de
   laudo de carro, não é decoy institucional).
3. **laudo revisado**    → `LaudoCache.confidence >= 0.6` (camada de visão ou
   textual extraiu avarias de verdade; 0.5 = fallback `_laudo_sem_pdf`, 0.55 =
   sinal estrutural do HTML sem PDF, 0.6+ = laudo real lido).

Quando qualquer um falta, o lote fica "zumbi": aparece na planilha como
"⚠ LAUDO NÃO ANALISADO" (numéricos suprimidos) e o operador precisa abrir o
anúncio manualmente pra conferir. Historicamente o ciclo do cron
(triagem → limpar_decoys → retry) resolve a maior parte, mas sem um audit
final NÃO existe invariante que proteja: lotes travados por rate-limit ou
por modal lazy que nunca abre acumulam silenciosamente.

Este módulo é a última linha de defesa:

- `auditar_completude(session, pdf_dir)` classifica cada lote ativo em um
  `StatusCompletude` e agrega por razão.
- `reextrair_pendentes_com_pdf_local(...)` é o auto-fix barato: quando o PDF
  local está OK mas o LaudoCache veio do fallback sem visão (conf<0.6),
  re-roda o extrator direto no arquivo em disco — sem Playwright, sem rede.
  Consumido pelo CLI `scripts/auditar_laudos.py --fix` e pelo ciclo do cron.
- `test_auditoria_laudo.py` usa esses helpers no modo hygiene: se o DB real
  tem zumbi, o `make test` falha.

Complementa os dois módulos adjacentes:
- `scripts/limpar_decoys_laudo.py` — remove URL-decoy de `raw_json`.
- `scripts/reprocessar_lotes_do_db.py --somente-laudo-pendente` — retry com
  Playwright (re-scrape do detalhe). Caro, roda só 2×/dia no cron.

Este audit roda sem rede — é o passo que se deve rodar TAMBÉM depois do
cron pra certificar que "zumbi remanescente = 0".
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

# Default espelha `orquestrador._PDF_STORAGE_DIR` — mantido como constante
# local pra não criar import circular com orquestrador.
_DEFAULT_PDF_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "laudos_pdfs"

# Mesmo limiar usado em `sheets.py` pra decidir "laudo analisado vs pendente".
_CONFIDENCE_MINIMA = 0.6


class StatusCompletude(str, Enum):
    """Classificação do estado do laudo de um lote ativo.

    Ordem importa pro relatório (OK primeiro, LAUDO_PENDENTE é o mais
    acionável, SEM_LINK é o menos acionável localmente).
    """

    OK = "ok"
    # Laudo já foi revisado com confidence >= 0.6 E tem link válido na planilha
    # E PDF baixado localmente. Nada a fazer.

    LAUDO_PENDENTE = "laudo_pendente"
    # Tem PDF local válido, mas LaudoCache ausente ou com confidence<0.6.
    # Auto-fixável offline — só re-rodar extrator no PDF que já está em disco.

    SEM_PDF_LOCAL = "sem_pdf_local"
    # Tem URL válida em raw_json mas o arquivo `data/laudos_pdfs/<id>.pdf` não
    # existe (download falhou com 429 ou não rodou pós-decoy-cleanup). Fixável
    # com download direto usando cookies salvos do AutoAvaliar.

    SEM_LINK = "sem_link"
    # raw_json.detalhe.laudo_pdf_url ausente OU é decoy (não passa em
    # is_laudo_pdf_url). Modal lazy nunca abriu ou lote realmente não tem
    # laudo. Requer re-scraping (cro ou retry manual) — sem rede não corrige.


@dataclass
class DiagnosticoLote:
    """Estado de completude de um único lote."""

    lote_id: str
    modelo: str
    status: StatusCompletude
    razao: str
    url_laudo: Optional[str] = None
    pdf_local: Optional[Path] = None
    confidence: Optional[float] = None


@dataclass
class ResultadoAuditoria:
    """Resultado agregado da auditoria."""

    total_ativos: int = 0
    # Contagem por status — evita recontar iterando `diagnosticos`.
    contagens: Dict[StatusCompletude, int] = field(default_factory=dict)
    diagnosticos: List[DiagnosticoLote] = field(default_factory=list)

    @property
    def total_zumbis(self) -> int:
        return sum(
            n for status, n in self.contagens.items()
            if status != StatusCompletude.OK
        )

    @property
    def total_ok(self) -> int:
        return self.contagens.get(StatusCompletude.OK, 0)


# ---------------------------------------------------------------------------
# Classificador
# ---------------------------------------------------------------------------

def classificar_lote(
    lote: Lote,
    laudo: Optional[LaudoCache],
    pdf_dir: Path,
) -> DiagnosticoLote:
    """Classifica o estado de completude de um lote.

    Regra de decisão (em ordem — a primeira que bater define o status):

    1. URL em raw_json.detalhe.laudo_pdf_url NÃO passa em `is_laudo_pdf_url`
       → SEM_LINK (scraper precisa achar link válido primeiro, sem isso
       não dá pra baixar PDF nem confirmar link na planilha).

    2. PDF não existe no diretório (ou tem <5KB — decoy tinha ~200KB sempre,
       mas laudos legítimos têm 300KB+; <5KB é quase certo arquivo corrompido)
       → SEM_PDF_LOCAL.

    3. LaudoCache ausente ou confidence<0.6 (independente de ter PDF)
       → LAUDO_PENDENTE (auto-fixável se PDF local OK).

    4. Caso base: tudo alinhado → OK.

    Retorna `DiagnosticoLote` com campos auxiliares (url, pdf_path, confidence)
    pra o auto-fix saber o que tentar.
    """
    modelo = f"{lote.marca} {lote.modelo}".strip() or lote.id
    detalhe = (lote.raw_json or {}).get("detalhe") or {}
    url_raw = detalhe.get("laudo_pdf_url")
    url_valida = is_laudo_pdf_url(url_raw)
    pdf_path = pdf_dir / f"{lote.id}.pdf"
    pdf_existe = pdf_path.exists() and pdf_path.stat().st_size >= 5_000
    conf = (laudo.confidence if laudo else None) or 0.0

    # 1. Sem URL válida → sem link na planilha.
    if not url_valida:
        razao = (
            "raw_json.detalhe.laudo_pdf_url ausente — scraper não achou link do PDF"
            if not url_raw
            else "raw_json.detalhe.laudo_pdf_url é decoy (não passa em is_laudo_pdf_url)"
        )
        return DiagnosticoLote(
            lote_id=lote.id, modelo=modelo,
            status=StatusCompletude.SEM_LINK, razao=razao,
            url_laudo=url_raw, pdf_local=None, confidence=conf,
        )

    # 2. URL OK mas arquivo local ausente/truncado.
    if not pdf_existe:
        razao = (
            f"URL válida em raw_json mas PDF não existe em {pdf_path}"
            if not pdf_path.exists()
            else f"PDF local existe mas tem {pdf_path.stat().st_size}B (<5KB — download corrompido?)"
        )
        return DiagnosticoLote(
            lote_id=lote.id, modelo=modelo,
            status=StatusCompletude.SEM_PDF_LOCAL, razao=razao,
            url_laudo=url_raw, pdf_local=pdf_path, confidence=conf,
        )

    # 3. PDF OK mas laudo não revisado.
    if laudo is None or conf < _CONFIDENCE_MINIMA:
        razao = (
            "LaudoCache ausente — extrator nunca rodou neste lote"
            if laudo is None
            else f"LaudoCache.confidence={conf:.2f} < {_CONFIDENCE_MINIMA} "
                 "(fallback `_laudo_sem_pdf` / `_laudo_de_textual`)"
        )
        return DiagnosticoLote(
            lote_id=lote.id, modelo=modelo,
            status=StatusCompletude.LAUDO_PENDENTE, razao=razao,
            url_laudo=url_raw, pdf_local=pdf_path, confidence=conf,
        )

    # 4. Tudo OK.
    return DiagnosticoLote(
        lote_id=lote.id, modelo=modelo,
        status=StatusCompletude.OK, razao="laudo completo",
        url_laudo=url_raw, pdf_local=pdf_path, confidence=conf,
    )


# ---------------------------------------------------------------------------
# Auditoria
# ---------------------------------------------------------------------------

def auditar_completude(
    session: Session,
    pdf_dir: Optional[Path] = None,
    empresa_id: Optional[str] = None,
    agora: Optional[datetime] = None,
) -> ResultadoAuditoria:
    """Audita completude de laudo pros lotes ATIVOS que aparecem na planilha.

    "Ativo" = `fim_em > agora` E `raw_json.detalhe.encerrado != True`. Espelha
    o filtro de `SheetsExporter._query` pra auditar exatamente o que o
    operador vê.

    "Na planilha" = tem AvaliacaoLote (independente de empresa — ou filtrado
    por `empresa_id` se fornecido). Lotes ativos SEM AvaliacaoLote são
    ignorados: são os que caíram em `early_exit` (reprovado_estrutural,
    leilao_encerrado, fipe_indisponivel) e não vão pra planilha por desígnio.

    Args:
        session: sessão SQLModel aberta.
        pdf_dir: diretório onde PDFs são persistidos. Default:
            `<repo>/data/laudos_pdfs`.
        empresa_id: se passado, audita só lotes avaliados por esta empresa.
            Sem ele, audita toda empresa (caso multi-tenant).
        agora: override do timestamp (usado em testes determinísticos).

    Returns:
        ResultadoAuditoria com contagens por status e lista de diagnósticos.
    """
    pdf_dir = pdf_dir or _DEFAULT_PDF_DIR
    agora = agora or datetime.now()

    # Lotes que POSSIVELMENTE entram na planilha = têm AvaliacaoLote.
    query_aval = select(AvaliacaoLote)
    if empresa_id is not None:
        query_aval = query_aval.where(AvaliacaoLote.empresa_id == empresa_id)
    lote_ids_avaliados = {av.lote_id for av in session.exec(query_aval).all()}

    if not lote_ids_avaliados:
        return ResultadoAuditoria()

    result = ResultadoAuditoria()

    for lote_id in sorted(lote_ids_avaliados):
        lote = session.get(Lote, lote_id)
        if lote is None:
            continue

        # Filtro de "ativo" — mesmo critério do exportador da planilha.
        if lote.fim_em is None or lote.fim_em <= agora:
            continue
        detalhe = (lote.raw_json or {}).get("detalhe") or {}
        if detalhe.get("encerrado"):
            continue

        laudo = session.get(LaudoCache, lote_id)
        diag = classificar_lote(lote, laudo, pdf_dir)
        result.diagnosticos.append(diag)
        result.total_ativos += 1
        result.contagens[diag.status] = result.contagens.get(diag.status, 0) + 1

    return result


# ---------------------------------------------------------------------------
# Auto-fix barato: re-extração offline quando PDF local está OK
# ---------------------------------------------------------------------------

@dataclass
class ResultadoReextracao:
    """Resumo do auto-fix."""

    tentativas: int = 0
    sucessos: int = 0
    falhas: List[str] = field(default_factory=list)   # "lote_id: motivo"


def reextrair_pendentes_com_pdf_local(
    session: Session,
    pdf_dir: Optional[Path] = None,
    empresa_id: Optional[str] = None,
    *,
    vision_client=None,
    agora: Optional[datetime] = None,
    dry_run: bool = False,
) -> ResultadoReextracao:
    """Auto-fix: re-roda o extrator de laudo nos lotes com PDF local mas laudo<0.6.

    Estratégia de extração (espelha o pipeline do orquestrador):
    1. Se tem vision_client: tenta `extrair_laudo(pdf, vision)` (visão + textual).
    2. Fallback: só textual via `_laudo_de_textual(parse_laudo_textual(pdf))`.
    3. Persiste o `LaudoCache` com o resultado (upsert), invalidando o placeholder
       antigo com conf<0.6.

    Não mexe em `AvaliacaoLote` — re-precificar requer contexto do scraper
    (flags do detalhe, similares_precos). Quando o laudo muda, o ROI pode
    ficar stale até o próximo `reprocessar_lotes_do_db.py`. Esse módulo não
    resolve o ROI; ele só garante que `confidence >= 0.6`, tirando o zumbi
    da planilha. `AvaliacaoLote` é refeito no próximo ciclo do cron.

    Args:
        session: sessão aberta (será comitada se não dry_run).
        pdf_dir: onde procurar PDFs. Default `data/laudos_pdfs/`.
        empresa_id: escopo do audit pra achar lotes pendentes.
        vision_client: instância do VisionClient (Gemini, Anthropic, Ollama).
            None → só textual.
        agora: override pra testes.
        dry_run: não persiste; só conta o que re-extrairia.

    Returns:
        ResultadoReextracao com contagens.
    """
    auditoria = auditar_completude(
        session, pdf_dir=pdf_dir, empresa_id=empresa_id, agora=agora,
    )
    pendentes = [
        d for d in auditoria.diagnosticos
        if d.status == StatusCompletude.LAUDO_PENDENTE and d.pdf_local
    ]

    resultado = ResultadoReextracao(tentativas=len(pendentes))
    if not pendentes:
        return resultado

    # Imports tardios pra não puxar PyMuPDF/LLM nos testes que não precisam.
    from carros_sa.agents.extrator_laudo import (
        extrair_laudo,
        parse_laudo_textual,
    )
    from carros_sa.orquestrador import _laudo_de_textual, _upsert_laudo_cache

    for diag in pendentes:
        pdf_path = diag.pdf_local
        if pdf_path is None or not pdf_path.exists():
            resultado.falhas.append(f"{diag.lote_id}: pdf_local sumiu entre audit e fix")
            continue

        try:
            if vision_client is not None:
                try:
                    laudo_est = extrair_laudo(pdf_path, vision_client)
                except Exception:
                    # Visão falhou → textual puro.
                    txt = parse_laudo_textual(pdf_path)
                    laudo_est = _laudo_de_textual(txt)
            else:
                txt = parse_laudo_textual(pdf_path)
                laudo_est = _laudo_de_textual(txt)
        except Exception as exc:
            resultado.falhas.append(f"{diag.lote_id}: {type(exc).__name__}: {exc}")
            continue

        # Só promove se confidence DE FATO subiu pra >=0.6 — senão o
        # placeholder antigo continua tão bom quanto o novo, não há ganho
        # em persistir e a planilha permanece "pendente".
        if (laudo_est.confidence or 0) < _CONFIDENCE_MINIMA:
            resultado.falhas.append(
                f"{diag.lote_id}: extração rodou mas confidence={laudo_est.confidence:.2f} "
                "ainda abaixo do limite — laudo difícil de parsear"
            )
            continue

        if not dry_run:
            _upsert_laudo_cache(diag.lote_id, laudo_est, session)
        resultado.sucessos += 1

    if not dry_run and resultado.sucessos > 0:
        session.commit()

    return resultado
