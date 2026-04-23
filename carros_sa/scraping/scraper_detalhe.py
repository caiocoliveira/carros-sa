"""Processa a página de detalhe de um lote: parse_detalhe + early_exit + PDF.

Separação intencional:
  - **fetch** do innerText e do PDF é feito FORA daqui (Chrome MCP / Playwright /
    cache em disco). O motivo é que Auto Avaliar é JS-pesado; chamar via httpx
    pelado retorna SPA shell. A função abaixo é pura: recebe `body_text` e
    `laudo_pdf_url` já coletados.
  - **persistência** acontece aqui: enriquece `lote.raw_json["detalhe"]` com as
    flags, baixa o PDF apenas se `DetalheFlags.early_exit is None` (respeitando
    o critério do ROADMAP — economia obrigatória).

O downloader de PDF é injetável pra facilitar teste sem rede.
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlmodel import Session

from carros_sa.models import Lote, PrecoReferenciaAA
from carros_sa.scraping.parsers import DetalheFlags, parse_detalhe

PdfDownloader = Callable[[str, Path], None]

def _default_pdf_downloader(url: str, destino: Path) -> None:
    """Baixa um PDF via stdlib. Substituível em testes."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - URL vem do site oficial
        destino.write_bytes(resp.read())

@dataclass
class ResultadoProcessamento:
    lote_id: str
    early_exit: str | None          # None = passou, str = motivo do descarte
    pdf_baixado: bool
    pdf_path: Path | None
    flags: DetalheFlags

    @property
    def passou(self) -> bool:
        return self.early_exit is None

def _flags_to_dict(flags: DetalheFlags) -> dict:
    """Versão JSON-serializável dos campos do DetalheFlags pra raw_json."""
    return {
        "specs": flags.specs,
        "status_laudo": flags.status_laudo,
        "status_documento": flags.status_documento,
        "prazo_transferencia_dias": flags.prazo_transferencia_dias,
        "prazo_liberacao_dias": flags.prazo_liberacao_dias,
        "itens_reprovados": flags.itens_reprovados,
        "opcionais": flags.opcionais,
        "ipva_pago": flags.ipva_pago,
        "observacoes_anunciante": flags.observacoes_anunciante,
        "laudo_pdf_url": flags.laudo_pdf_url,
        "similares_precos": flags.similares_precos,
        "preco_referencia_aa": flags.preco_referencia_aa,
        "fipe_pct_lance_minimo": flags.fipe_pct_lance_minimo,
        "reprovado_estrutural": flags.reprovado_estrutural,
        "laudo_aprovado": flags.laudo_aprovado,
        "encerrado": flags.encerrado,
        "early_exit": flags.early_exit,
    }

def processar_detalhe(
    *,
    lote_id: str,
    body_text: str,
    laudo_pdf_url: str | None,
    session: Session,
    pdf_dir: Path,
    downloader: PdfDownloader | None = None,
) -> ResultadoProcessamento:
    """Aplica parse_detalhe, persiste flags e baixa PDF se o lote sobreviver ao early-exit.

    Side-effects:
      - Atualiza `Lote.raw_json["detalhe"]` no SQLite (commit feito aqui).
      - Se `early_exit is None` e há `laudo_pdf_url`, baixa o PDF pra `pdf_dir/<lote_id>.pdf`.

    Não chama LLM. Não toca em `LaudoCache` (isso é responsabilidade do
    ExtratorLaudo, workstream separado).
    """
    flags = parse_detalhe(body_text, laudo_pdf_url=laudo_pdf_url)

    lote = session.get(Lote, lote_id)
    if lote is None:
        raise ValueError(f"Lote {lote_id} não existe no SQLite — ingerir listagem antes.")

    raw = dict(lote.raw_json or {})
    raw["detalhe"] = _flags_to_dict(flags)
    lote.raw_json = raw

    # Promove referências de preço da Tabela Auto Avaliar pra colunas first-class.
    # Histórico vai pra preco_referencia_aa (tabela separada) pra futura consulta
    # por modelo em lotes que não trouxerem o dado embutido.
    if flags.preco_referencia_aa is not None:
        lote.preco_referencia_aa = flags.preco_referencia_aa
        session.add(PrecoReferenciaAA(
            marca=lote.marca,
            modelo=lote.modelo,
            ano=lote.ano,
            preco=flags.preco_referencia_aa,
            fipe_pct_lance_minimo=flags.fipe_pct_lance_minimo,
            origem_lote_id=lote_id,
        ))
    if flags.fipe_pct_lance_minimo is not None:
        lote.fipe_pct_lance_minimo = flags.fipe_pct_lance_minimo

    pdf_baixado = False
    pdf_path: Path | None = None
    if flags.early_exit is None and laudo_pdf_url:
        pdf_path = pdf_dir / f"{lote_id}.pdf"
        dl = downloader or _default_pdf_downloader
        dl(laudo_pdf_url, pdf_path)
        pdf_baixado = True
        raw["detalhe"]["pdf_path_local"] = str(pdf_path)
        lote.raw_json = raw  # reatribui pra SQLAlchemy detectar mudança

    session.add(lote)
    session.commit()

    return ResultadoProcessamento(
        lote_id=lote_id,
        early_exit=flags.early_exit,
        pdf_baixado=pdf_baixado,
        pdf_path=pdf_path,
        flags=flags,
    )
