#!/usr/bin/env python
"""Reprocessa TODOS os lotes no DB rodando `_pipeline_lote` em cada um.

Pula a fase de coleta de listagem (61 cidades × scroll), que é demorada e
estressa Auto Avaliar quando já temos os lotes cadastrados. Use depois de
DELETE AvaliacaoLote + LaudoCache pra forçar re-avaliação completa.

Uso:
    PYTHONPATH=. python scripts/reprocessar_lotes_do_db.py --empresa carros_uberlandia
    PYTHONPATH=. python scripts/reprocessar_lotes_do_db.py --empresa carros_uberlandia --max 5  # smoke
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from sqlmodel import Session, select

load_dotenv()
console = Console()
app = typer.Typer(add_completion=False)


# Circuit-breaker: depois de N extrações com confidence<0.6, o lote sai do retry
# diário. Sintoma alvo: lote com body_text vazio ou PDF inacessível fica em
# perma-loop, queimando 2-3 chamadas LLM/dia indefinidamente (~50-100/dia
# acumulado entre todos os lotes presos). Audit `--strict` continua reportando
# como `cache_confianca_baixa` — operador inspeciona manualmente e zera o
# contador (ou deleta a row de LaudoCache) pra forçar retry.
MAX_TENTATIVAS_EXTRACAO = 3


def _filtrar_laudo_pendente(
    lotes,
    laudos_confidence: dict,
    pdf_dir: Optional[Path] = None,
    laudos_tentativas: Optional[dict] = None,
):
    """Filtra `lotes` mantendo só os que têm laudo pendente.

    "Pendente" = qualquer uma:
      1. Sem entrada em LaudoCache
      2. LaudoCache.confidence < 0.6 (fallback `_laudo_sem_pdf`)
      3. PDF ausente em `pdf_dir/<lote_id>.pdf` (ou <5KB)
      4. `raw_json.detalhe.laudo_pdf_url` ausente ou inválido — caso introduzido
         em 2026-05-10 (DD3): `_upsert_lote` antes zerava `detalhe` em todo
         re-scrape da listagem; com PDF + cache OK no DB, short-circuit pulava
         o pipeline e a URL nunca voltava. Coluna "Ver laudo" da planilha
         sumia. Bug raiz corrigido (preservação em `_upsert_lote`), mas o
         filtro precisa cobrir essa dimensão também — paridade total com
         `verificar_laudo_completo`.

    Mas com circuit-breaker: lote com `confidence<0.6 AND tentativas>=MAX`
    sai da fila (extração já falhou N vezes — não é o extrator, é o input).
    Lotes pendentes por OUTROS motivos (PDF ausente, URL inválida) com cache
    forte NÃO são afetados — o retry pra eles não gasta LLM, só re-baixa PDF.

    Extraído pra fora do CLI pra ficar coberto por teste unitário (sem
    Playwright, sem `_run`). O `pdf_dir=None` resolve em runtime via
    `PDF_DIR_DEFAULT` — mesma fonte do `verificar_laudo_completo`.
    """
    from carros_sa.tools.laudo_audit import PDF_DIR_DEFAULT
    from carros_sa.scraping.parsers import is_laudo_pdf_url
    if pdf_dir is None:
        pdf_dir = PDF_DIR_DEFAULT
    laudos_tentativas = laudos_tentativas or {}

    def _pdf_ausente(lote_id: str) -> bool:
        p = pdf_dir / f"{lote_id}.pdf"
        return not (p.exists() and p.stat().st_size > 5_000)

    def _url_invalida(lote) -> bool:
        det = (lote.raw_json or {}).get("detalhe") if isinstance(lote.raw_json, dict) else None
        url = det.get("laudo_pdf_url") if isinstance(det, dict) else None
        return not is_laudo_pdf_url(url)

    def _esgotou_tentativas(lote_id: str) -> bool:
        return (laudos_tentativas.get(lote_id) or 0) >= MAX_TENTATIVAS_EXTRACAO

    return [
        l for l in lotes
        if (
            laudos_confidence.get(l.id) is None
            or (laudos_confidence.get(l.id) or 0) < 0.6
            or _pdf_ausente(l.id)
            or _url_invalida(l)
        )
        # Circuit-breaker SÓ pula quando o motivo de pendência é cache fraco —
        # se cache é forte mas PDF/URL sumiu, o retry não chama LLM, então
        # MAX_TENTATIVAS não se aplica.
        and not (
            (laudos_confidence.get(l.id) or 0) < 0.6
            and _esgotou_tentativas(l.id)
        )
    ]


@app.command()
def main(
    empresa: str = typer.Option("carros_uberlandia", help="ID da empresa"),
    max_lotes: Optional[int] = typer.Option(None, "--max", help="Limita a N lotes (smoke test)"),
    headless: bool = typer.Option(True, help="False = browser visível"),
    sem_sheets: bool = typer.Option(False, help="Pular exportação final"),
    somente_sem_avaliacao: bool = typer.Option(
        False, "--somente-sem-avaliacao",
        help="Filtra só lotes ainda não avaliados pra esta empresa (útil pra re-tentar erros).",
    ),
    somente_ativos: bool = typer.Option(
        False, "--somente-ativos",
        help="Filtra só lotes com leilão ainda aberto (fim_em no futuro). Evita gastar tempo/LLM em lotes já encerrados.",
    ),
    somente_laudo_pendente: bool = typer.Option(
        False, "--somente-laudo-pendente",
        help=(
            "Filtra só lotes cujo laudo está incompleto: LaudoCache vazio OU "
            "fallback (confidence<0.6) OU PDF ausente em data/laudos_pdfs/. "
            "Ideal pra rodar após a triagem diária e tentar destravar lotes em "
            "que o scraper não achou o PDF — e pra recuperar PDFs sumidos "
            "entre runs CI quando state/db não restaurou tudo."
        ),
    ),
) -> None:
    """Reprocessa lotes já ingeridos sem re-scraping da listagem."""
    asyncio.run(_run(empresa, max_lotes, headless, sem_sheets, somente_sem_avaliacao,
                     somente_ativos, somente_laudo_pendente))


async def _run(
    empresa_id: str,
    max_lotes: Optional[int],
    headless: bool,
    sem_sheets: bool,
    somente_sem_avaliacao: bool = False,
    somente_ativos: bool = False,
    somente_laudo_pendente: bool = False,
) -> None:
    from playwright.async_api import async_playwright

    from datetime import datetime

    from carros_sa.agents.vision_clients import build_default_client
    from carros_sa.agents.text_llm_clients import build_default_text_client
    from carros_sa.db import get_session, init_db
    from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
    from carros_sa.orquestrador import _pipeline_lote
    from carros_sa.scraping.scraper_autoavaliar import garantir_autenticado
    from carros_sa.tenancy import carregar_empresa

    email = os.environ.get("AUTOAVALIAR_EMAIL")
    password = os.environ.get("AUTOAVALIAR_PASSWORD")
    if not email or not password:
        console.print("[red]AUTOAVALIAR_EMAIL/PASSWORD faltando no .env[/red]")
        raise typer.Exit(1)

    init_db()
    empresa = carregar_empresa(empresa_id)
    vision_client = build_default_client()
    console.print(f"[cyan]Vision:[/cyan] {type(vision_client).__name__}")
    # text_llm_client habilita a camada 4 do `extrair_laudo` (DD4 — LLM textual
    # vendor-agnostic) e o fallback de URL de laudo no scraper de detalhe.
    # Sem isso, o retry diário rodava `_pipeline_lote` SEM camada 4: lotes de
    # vendor fora do Auto Avaliar (Zachi, Procemax, Terceira Visão, etc) que
    # entram pelo retry ficavam presos em confidence=0.0 indefinidamente,
    # mesmo após DD4 ter aterrissado em main — paridade entre triagem inicial
    # (que passa) e retry (que não passava). Tentativa N+1 incrementava e o
    # circuit-breaker (II) os congelava após 3 ciclos, sem chance da camada 4
    # rodar uma única vez sobre esses PDFs. Diagnóstico em 2026-05-22: 6/64
    # lotes ativos presos exatamente por este caminho.
    try:
        text_llm_client = build_default_text_client()
        console.print(f"[cyan]Text LLM:[/cyan] {type(text_llm_client).__name__}")
    except RuntimeError:
        text_llm_client = None
        console.print("[yellow]Text LLM: desabilitado — camada 4 do extrator não dispara[/yellow]")

    tmp_dir = Path(tempfile.mkdtemp(prefix="carros_sa_reproc_"))

    n_ok = n_erro = n_descartado = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()
        await garantir_autenticado(page, email, password)
        console.print("[green]✓ Sessão ativa[/green]")

        with get_session() as session:
            query = select(Lote)
            lotes = session.exec(query).all()
            if somente_sem_avaliacao:
                ja = {
                    r.lote_id for r in session.exec(
                        select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa.empresa_id)
                    ).all()
                }
                lotes = [l for l in lotes if l.id not in ja]
                console.print(f"[cyan]Filtro ativo: só lotes sem avaliação → {len(lotes)} candidatos[/cyan]")
            if somente_ativos:
                agora = datetime.now()
                lotes = [l for l in lotes if l.fim_em is not None and l.fim_em > agora]
                console.print(f"[cyan]Filtro ativo: só leilões abertos → {len(lotes)} candidatos[/cyan]")
            if somente_laudo_pendente:
                # Carrega só (lote_id, confidence, tentativas) — tupla, não entity.
                # Se selecionarmos LaudoCache inteiro, os objetos vão pra identity
                # map; depois `session.commit()` no meio de `_pipeline_lote` expira
                # eles, e `session.get(LaudoCache, lote_id)` no `_upsert_laudo_cache`
                # retorna None pra rows expirados → tenta INSERT → UNIQUE viola.
                # (sintoma observado 2026-04-18 após processar ~105/139 lotes).
                laudos_full = {
                    row[0]: (row[1], row[2])
                    for row in session.exec(
                        select(
                            LaudoCache.lote_id,
                            LaudoCache.confidence,
                            LaudoCache.tentativas_extracao,
                        )
                    ).all()
                }
                laudos_confidence = {k: v[0] for k, v in laudos_full.items()}
                laudos_tentativas = {k: v[1] for k, v in laudos_full.items()}
                lotes = _filtrar_laudo_pendente(
                    lotes, laudos_confidence, laudos_tentativas=laudos_tentativas,
                )
                console.print(
                    f"[cyan]Filtro ativo: só laudo pendente (cache<0.6 OU PDF ausente OU URL inválida, "
                    f"max {MAX_TENTATIVAS_EXTRACAO} tentativas) → {len(lotes)} candidatos[/cyan]"
                )
            if max_lotes:
                lotes = lotes[:max_lotes]
            console.print(f"[cyan]Reprocessando {len(lotes)} lotes…[/cyan]\n")

            with Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Reprocessando", total=len(lotes))
                for lote in lotes:
                    try:
                        res = await _pipeline_lote(
                            lote, page, vision_client, empresa, session, tmp_dir,
                            text_llm_client=text_llm_client,
                        )
                        if res.erro:
                            n_erro += 1
                        elif res.motivo_descarte:
                            n_descartado += 1
                        else:
                            n_ok += 1
                    except Exception as exc:
                        console.print(f"  [red]exceção em {lote.id}: {exc}[/red]")
                        n_erro += 1
                    progress.advance(task)

        await browser.close()

    console.print(
        f"\n[bold green]✓ {n_ok} avaliados  [/bold green]"
        f"[yellow]{n_descartado} descartados  [/yellow]"
        f"[red]{n_erro} erros[/red]"
    )

    if sem_sheets:
        return

    sheet_id = os.environ.get("GOOGLE_SHEETS_ID")
    creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")
    if sheet_id and creds:
        from carros_sa.tools.sheets import SheetsExporter
        with get_session() as s:
            exporter = SheetsExporter(sheet_id, creds)
            n = exporter.exportar(empresa.empresa_id, s)
            console.print(f"[green]✓ {n} lotes exportados → Sheet[/green]")


if __name__ == "__main__":
    app()
