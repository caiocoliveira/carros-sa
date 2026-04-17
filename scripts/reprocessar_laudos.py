#!/usr/bin/env python
"""Reprocessa LaudoCache + AvaliacaoLote dos lotes existentes no SQLite.

Útil pra re-rodar laudo + avaliação com:
  - Pipeline atualizado (ex.: após workstream L que consertou o extrator textual)
  - Schema expandido (campos fipe, webmotors_mediana, preco_giro_fipe/aa, etc)

Não re-raspe a listagem. Usa PDFs já baixados em `data/laudos_amostra/` +
informação do `lote.raw_json` (incluindo `.detalhe` quando existir). Lotes sem
PDF disponível ganham `_laudo_sem_pdf` ou `_laudo_de_textual` dependendo do que
o parser textual conseguir aproveitar.

Uso:
    PYTHONPATH=. python scripts/reprocessar_laudos.py --empresa carros_uberlandia
    PYTHONPATH=. python scripts/reprocessar_laudos.py --empresa carros_uberlandia --lote 21854782  # só um
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from sqlmodel import Session, select

load_dotenv()
console = Console()
app = typer.Typer(add_completion=False)

PDF_DIR = Path("data/laudos_amostra")
# data/laudos_pdfs/ é onde o orquestrador persiste PDFs frescos baixados do
# Auto Avaliar. data/laudos_amostra/ é o bucket de fixtures / gold tests. O
# script procura nos DOIS pra cobrir os dois fluxos.
PDF_DIR_PERSISTIDO = Path("data/laudos_pdfs")


@app.command()
def main(
    empresa: str = typer.Option("carros_uberlandia", help="ID da empresa"),
    lote: Optional[str] = typer.Option(None, help="Reprocessar apenas este lote_id"),
    dry_run: bool = typer.Option(False, help="Só mostra o que seria feito, sem persistir"),
) -> None:
    """Reprocessa LaudoCache + AvaliacaoLote dos lotes já ingeridos."""
    from carros_sa.agents.avaliador_mercado import avaliar as avaliar_mercado
    from carros_sa.agents.estimador_reforma import estimar as estimar_reforma
    from carros_sa.agents.estimador_reforma_llm import estimar_llm as estimar_reforma_llm
    from carros_sa.agents.extrator_laudo import extrair_laudo, parse_laudo_textual
    from carros_sa.agents.text_llm_clients import build_default_text_client
    from carros_sa.agents.vision_clients import build_default_client
    from carros_sa.db import get_session, init_db
    from carros_sa.models import AvaliacaoLote, LaudoCache, Lote, LoteRaw
    from carros_sa.orquestrador import (
        _calcular_frete,
        _laudo_de_textual,
        _laudo_sem_pdf,
        _upsert_avaliacao,
        _upsert_laudo_cache,
    )
    from carros_sa.precificador import precificar
    from carros_sa.scraping.parsers import DetalheFlags
    from carros_sa.tenancy import carregar_empresa

    init_db()
    empresa_cfg = carregar_empresa(empresa)

    try:
        vision_client = build_default_client()
        console.print(f"[cyan]Vision client:[/cyan] {type(vision_client).__name__}")
    except Exception as e:
        console.print(f"[yellow]Vision indisponível ({e}) — usando fallback textual puro[/yellow]")
        vision_client = None

    try:
        text_llm_client = build_default_text_client()
        console.print(f"[cyan]Reforma LLM:[/cyan] {type(text_llm_client).__name__}")
    except Exception:
        text_llm_client = None
        console.print("[yellow]Reforma LLM desabilitado → usando tabela determinística[/yellow]")

    with get_session() as session:
        query = select(Lote)
        if lote:
            query = query.where(Lote.id == lote)
        lotes = session.exec(query).all()

        resultados = []
        for l in lotes:
            resultado = _reprocessar_um(
                l, empresa_cfg, vision_client, session, dry_run,
                parse_laudo_textual=parse_laudo_textual,
                extrair_laudo=extrair_laudo,
                laudo_de_textual=_laudo_de_textual,
                laudo_sem_pdf=_laudo_sem_pdf,
                avaliar_mercado=avaliar_mercado,
                estimar_reforma=estimar_reforma,
                estimar_reforma_llm=estimar_reforma_llm,
                text_llm_client=text_llm_client,
                calcular_frete=_calcular_frete,
                precificar_fn=precificar,
                upsert_laudo=_upsert_laudo_cache,
                upsert_aval=_upsert_avaliacao,
            )
            resultados.append(resultado)

        if not dry_run:
            session.commit()

    # Tabela resumo
    tbl = Table(title=f"Reprocessamento ({'DRY RUN' if dry_run else 'persistido'})")
    tbl.add_column("Lote")
    tbl.add_column("Modelo")
    tbl.add_column("Laudo")
    tbl.add_column("Avarias", justify="right")
    tbl.add_column("Severidade")
    tbl.add_column("Reforma (R$)", justify="right")
    tbl.add_column("Conf.", justify="right")
    for r in resultados:
        status = f"[yellow]{r['status']}[/yellow]" if r["status"] != "ok" else "[green]ok[/green]"
        tbl.add_row(
            r["lote_id"],
            r["modelo"][:30],
            status,
            str(r.get("n_avarias", "—")),
            r.get("severidade", "—"),
            f"R$ {r.get('reforma', 0):,}" if "reforma" in r else "—",
            f"{r.get('confidence', 0):.2f}" if "confidence" in r else "—",
        )
    console.print(tbl)

    ok = sum(1 for r in resultados if r["status"] == "ok")
    console.print(f"\n[bold]{ok}/{len(resultados)} lotes reprocessados com sucesso.[/bold]")


def _reprocessar_um(
    lote,
    empresa_cfg,
    vision_client,
    session,
    dry_run,
    *,
    parse_laudo_textual,
    extrair_laudo,
    laudo_de_textual,
    laudo_sem_pdf,
    avaliar_mercado,
    estimar_reforma,
    estimar_reforma_llm,
    text_llm_client,
    calcular_frete,
    precificar_fn,
    upsert_laudo,
    upsert_aval,
) -> dict:
    """Reprocessa um único lote: laudo → mercado → reforma → frete → precificador."""
    from carros_sa.scraping.parsers import DetalheFlags
    from carros_sa.models import LoteRaw

    modelo = f"{lote.marca} {lote.modelo} {lote.ano}"
    resultado: dict = {"lote_id": lote.id, "modelo": modelo, "status": "—"}

    # 1. Laudo: tenta PDF → visão → textual → sem PDF
    # Procura em data/laudos_pdfs/ (scraper de produção) primeiro; fallback em
    # data/laudos_amostra/ (fixtures de teste).
    pdf_path = PDF_DIR_PERSISTIDO / f"{lote.id}.pdf"
    if not pdf_path.exists():
        pdf_path = PDF_DIR / f"{lote.id}.pdf"
    if not pdf_path.exists():
        # Tenta variações de nome comuns em ambos os diretórios
        for base in (PDF_DIR_PERSISTIDO, PDF_DIR):
            for candidato in base.glob(f"{lote.id}*.pdf"):
                pdf_path = candidato
                break
            if pdf_path.exists():
                break

    try:
        if pdf_path.exists() and vision_client:
            try:
                laudo_est = extrair_laudo(pdf_path, vision_client)
            except Exception:
                # Vision falhou → fallback textual
                txt = parse_laudo_textual(pdf_path)
                laudo_est = laudo_de_textual(txt)
        elif pdf_path.exists():
            txt = parse_laudo_textual(pdf_path)
            laudo_est = laudo_de_textual(txt)
        else:
            # Sem PDF — usa flags do raw_json.detalhe se disponível
            flags = None
            det = (lote.raw_json or {}).get("detalhe")
            if det:
                flags = DetalheFlags(
                    specs=det.get("specs", {}),
                    status_laudo=det.get("status_laudo"),
                    status_documento=det.get("status_documento"),
                )
            laudo_est = laudo_sem_pdf(flags)
    except Exception as e:
        resultado["status"] = f"erro_laudo: {e}"
        return resultado

    # 2. Persist laudo cache
    if not dry_run:
        upsert_laudo(lote.id, laudo_est, session)

    # 3. Mercado + reforma + frete + precificador
    try:
        lote_raw = LoteRaw(
            lote_id=lote.id,
            leilao=lote.leilao,
            url=lote.url,
            marca=lote.marca,
            modelo=lote.modelo,
            ano=lote.ano,
            km=lote.km,
            lance_atual=lote.lance_atual or 0,
            origem_cidade=lote.origem_cidade,
            origem_uf=lote.origem_uf,
            preco_referencia_aa=getattr(lote, "preco_referencia_aa", None),
            fipe_pct_lance_minimo=getattr(lote, "fipe_pct_lance_minimo", None),
        )
        mercado = avaliar_mercado(
            marca=lote.marca, modelo=lote.modelo, ano=lote.ano,
            similares_precos=None, categoria=laudo_est.categoria_veiculo,
            session=session,
        )
        if text_llm_client is not None:
            observacoes = ""
            try:
                from pathlib import Path as _P
                pdf_local = _P("data/laudos_amostra") / f"{lote.id}.pdf"
                if pdf_local.exists():
                    observacoes = parse_laudo_textual(pdf_local).observacoes or ""
            except Exception:
                observacoes = ""
            reforma = estimar_reforma_llm(
                laudo=laudo_est,
                lote_info={
                    "marca": lote.marca, "modelo": lote.modelo, "ano": lote.ano,
                    "km": lote.km, "lance_atual": lote.lance_atual,
                },
                empresa=empresa_cfg,
                llm_client=text_llm_client,
                observacoes_pdf=observacoes,
            )
        else:
            reforma = estimar_reforma(laudo_est, empresa_cfg)
        frete = calcular_frete(lote, empresa_cfg)
        avaliacao = precificar_fn(lote_raw, laudo_est, mercado, reforma, frete, empresa_cfg)
        if not dry_run:
            upsert_aval(avaliacao, empresa_cfg.empresa_id, session)
    except Exception as e:
        resultado["status"] = f"erro_precificacao: {e}"
        return resultado

    resultado.update({
        "status": "ok",
        "n_avarias": len(laudo_est.avarias),
        "severidade": laudo_est.severidade_geral.value,
        "confidence": laudo_est.confidence,
        "reforma": reforma.custo_total,
    })
    return resultado


if __name__ == "__main__":
    app()
