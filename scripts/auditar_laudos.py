#!/usr/bin/env python
"""Audita lotes que deveriam ter laudo baixado, revisado e linkado na planilha.

Reporta três classes de defeito conhecidas:

  1. **`laudo_aprovado_sem_url`** — `status_laudo` no DOM diz "Laudo Aprovado"
     (ou variantes), mas o scraper não conseguiu extrair `laudo_pdf_url`.
     Causa típica: a JS de modal abre via botão "Acessar" sem a palavra
     "laudo" no texto e a heurística antiga de click pulava esse alvo.
     Efeito na planilha: célula "Laudo" vira "—" e a linha fica marcada
     "⚠ LAUDO NÃO ANALISADO" (LaudoCache cai pra confidence=0.5).

  2. **`url_invalida_no_db`** — `laudo_pdf_url` está preenchida mas não passa
     em `is_laudo_pdf_url()` (família decoy do Relatório de Transparência ou
     URLs novas que apareceram). Cobre a regressão dos decoys históricos.
     Tem script dedicado (`limpar_decoys_laudo.py`) que faz a limpeza.

  3. **`laudo_cache_baixa_confianca`** — LaudoCache existe mas confidence
     < 0.6 (placeholder de `_laudo_sem_pdf` ou textual fraco). O usuário
     vê "⚠ LAUDO NÃO ANALISADO" — esses lotes precisam reprocessar com
     PDF disponível.

Uso CLI:
    PYTHONPATH=. python scripts/auditar_laudos.py             # reporta tudo
    PYTHONPATH=. python scripts/auditar_laudos.py --strict    # exit != 0 se achar defeito

Importável (pra testes de hygiene e pra integração no pipeline):
    from scripts.auditar_laudos import auditar
    res = auditar(session)
    if res.tem_defeito: ...
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table
from sqlmodel import Session, select

from carros_sa.db import get_session, init_db
from carros_sa.models import LaudoCache, Lote
from carros_sa.scraping.parsers import is_laudo_pdf_url

console = Console()
app = typer.Typer(add_completion=False)


# Status de laudo que indicam que EXISTE PDF de laudo no Auto Avaliar — pra
# esses, `laudo_pdf_url` ausente é defeito do scraper, não do anunciante.
# "Laudo aprovado", "Laudo aprovado com apontamento", "Laudo Aprovado", etc.
# Excluímos "Laudo não aprovado" (lote já cai em early_exit).
def _status_indica_laudo_existente(status: Optional[str]) -> bool:
    if not status:
        return False
    low = status.lower().strip()
    if "não aprovad" in low or "nao aprovad" in low:
        return False
    if "sem laudo" in low:
        return False
    return "aprovad" in low


@dataclass
class DefeitoLaudo:
    lote_id: str
    motivo: str            # 'laudo_aprovado_sem_url' | 'url_invalida_no_db' | 'laudo_cache_baixa_confianca'
    detalhe: str           # mensagem humana explicando o caso
    url_atual: Optional[str] = None
    status_laudo: Optional[str] = None
    confidence: Optional[float] = None


@dataclass
class ResultadoAuditoria:
    total_lotes: int = 0
    defeitos: List[DefeitoLaudo] = field(default_factory=list)

    @property
    def tem_defeito(self) -> bool:
        return len(self.defeitos) > 0

    def por_motivo(self, motivo: str) -> List[DefeitoLaudo]:
        return [d for d in self.defeitos if d.motivo == motivo]


def _extrair_detalhe(raw_json) -> dict:
    if not isinstance(raw_json, dict):
        return {}
    det = raw_json.get("detalhe")
    return det if isinstance(det, dict) else {}


def auditar(session: Session) -> ResultadoAuditoria:
    """Varre todos os lotes e identifica lacunas de laudo. Idempotente, read-only.

    Não persiste nada — só reporta. O retry/limpeza fica em scripts dedicados
    (`limpar_decoys_laudo.py` pra URLs envenenadas; orquestrador na próxima
    triagem pra LaudoCache com confidence < 0.6).
    """
    result = ResultadoAuditoria()
    lotes = session.exec(select(Lote)).all()
    result.total_lotes = len(lotes)

    for lote in lotes:
        det = _extrair_detalhe(lote.raw_json)
        # Sem detalhe raspado ainda → não é defeito (lote só foi ingerido).
        if not det:
            continue

        url = det.get("laudo_pdf_url")
        status = det.get("status_laudo")

        # Defeito 1: status diz que tem laudo, mas URL ausente.
        if _status_indica_laudo_existente(status) and not url:
            result.defeitos.append(DefeitoLaudo(
                lote_id=lote.id,
                motivo="laudo_aprovado_sem_url",
                detalhe=(
                    f"DOM diz '{status}' mas laudo_pdf_url=None — scraper não "
                    "abriu o modal (botão 'Acessar' sem texto 'laudo'). "
                    "Re-rodar triagem pega o lote no próximo ciclo via "
                    "short-circuit de confidence < 0.6."
                ),
                status_laudo=status,
            ))

        # Defeito 2: URL existe mas não passa no gate (decoy).
        if url and not is_laudo_pdf_url(url):
            result.defeitos.append(DefeitoLaudo(
                lote_id=lote.id,
                motivo="url_invalida_no_db",
                detalhe=(
                    "URL persistida não é de laudo de carro — provável decoy "
                    "(Relatório de Transparência, listagem). Rode "
                    "`make limpar-decoys` pra zerar."
                ),
                url_atual=url,
                status_laudo=status,
            ))

        # Defeito 3: LaudoCache fraco (sem PDF analisado de verdade) MAS o
        # DOM dizia que há laudo disponível. Sem o segundo gate, lotes que
        # legitimamente não têm laudo ("SEM LAUDO" do anunciante) também
        # apareceriam, o que polui o report.
        cache = session.get(LaudoCache, lote.id)
        if cache and (cache.confidence or 0) < 0.6 and _status_indica_laudo_existente(status):
            # Evita duplicar o mesmo lote já reportado em (1)/(2).
            ja_reportado = any(d.lote_id == lote.id for d in result.defeitos)
            if not ja_reportado:
                result.defeitos.append(DefeitoLaudo(
                    lote_id=lote.id,
                    motivo="laudo_cache_baixa_confianca",
                    detalhe=(
                        f"LaudoCache.confidence={cache.confidence:.2f} (< 0.6) — "
                        "PDF não foi extraído de verdade. Pipeline reprocessa "
                        "automaticamente na próxima triagem."
                    ),
                    url_atual=url,
                    status_laudo=status,
                    confidence=cache.confidence,
                ))

    return result


@app.command()
def main(
    strict: bool = typer.Option(
        False, "--strict",
        help="Sai com código != 0 se encontrar qualquer defeito (CI/cron-friendly).",
    ),
) -> None:
    """Roda a auditoria contra o SQLite default e imprime tabela de defeitos."""
    init_db()
    with get_session() as session:
        res = auditar(session)

    console.print(f"\n[bold]Auditoria de laudos:[/bold] {res.total_lotes} lotes inspecionados")

    if not res.tem_defeito:
        console.print("[green]✓ Nenhum defeito encontrado — todos os lotes têm laudo OK.[/green]")
        return

    tbl = Table(title=f"{len(res.defeitos)} defeito(s) encontrado(s)")
    tbl.add_column("Lote")
    tbl.add_column("Motivo")
    tbl.add_column("status_laudo")
    tbl.add_column("Confidence", justify="right")
    tbl.add_column("Detalhe")
    for d in res.defeitos:
        tbl.add_row(
            d.lote_id,
            d.motivo,
            d.status_laudo or "—",
            f"{d.confidence:.2f}" if d.confidence is not None else "—",
            d.detalhe[:80] + ("…" if len(d.detalhe) > 80 else ""),
        )
    console.print(tbl)

    # Resumo por motivo
    for motivo in ("laudo_aprovado_sem_url", "url_invalida_no_db", "laudo_cache_baixa_confianca"):
        n = len(res.por_motivo(motivo))
        if n:
            console.print(f"  • {motivo}: [yellow]{n}[/yellow]")

    if strict:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
