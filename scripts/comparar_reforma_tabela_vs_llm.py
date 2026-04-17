#!/usr/bin/env python
"""Compara estimativa de reforma: tabela YAML (determinística) vs LLM.

Para cada lote com laudo no DB roda os dois estimadores lado a lado:

  1. `estimar(laudo, empresa)` — determinístico, tabela YAML (família × severidade)
  2. `llm_client.generate_json(prompt)` + `_parse_resposta` — LLM direto

Imprime tabela comparativa + detalhe item-a-item pra auditoria humana. Tirei
a chamada por `estimador_reforma_llm.estimar_llm()` porque ela cai em silêncio
pro determinístico em qualquer erro — o que arruína a comparação. Aqui a gente
QUER saber quando o LLM falhou.

Útil pra:
  - Validar se a tabela YAML está calibrada (divergência baixa = boa)
  - Auditar justificativas do LLM (cada item vem com "peça X R$, MO Y horas")
  - Detectar alucinação do LLM (custos absurdos ou itens inexistentes)

Uso:
    PYTHONPATH=. python scripts/comparar_reforma_tabela_vs_llm.py --empresa carros_uberlandia
    ... --lote 21854782 --lote 21893528       # só alguns lotes
    ... --min-avarias 1                       # só lotes com >=N avarias
    ... --min-confidence 0.9                  # só laudos confiáveis
    ... --limit 10                            # teto de lotes
"""

from __future__ import annotations

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


@app.command()
def main(
    empresa: str = typer.Option("carros_uberlandia", help="ID da empresa"),
    lotes: List[str] = typer.Option(None, "--lote", help="Filtrar por lote_id (repetível)"),
    min_avarias: int = typer.Option(0, help="Só lotes com >=N avarias extraídas"),
    min_confidence: float = typer.Option(0.0, help="Só laudos com confidence >= X"),
    limit: int = typer.Option(20, help="Máximo de lotes a processar"),
) -> None:
    from carros_sa.agents.estimador_reforma import estimar as estimar_tabela
    from carros_sa.agents.estimador_reforma_llm import _build_prompt, _parse_resposta
    from carros_sa.agents.extrator_laudo import parse_laudo_textual
    from carros_sa.agents.text_llm_clients import build_default_text_client
    from carros_sa.db import get_session
    from carros_sa.models import (
        Avaria, CategoriaVeiculo, LaudoCache, LaudoEstruturado, Lote,
        SeveridadeAvaria, StatusDocumentacao,
    )
    from carros_sa.tenancy import carregar_empresa

    empresa_cfg = carregar_empresa(empresa)

    try:
        llm_client = build_default_text_client()
        console.print(f"[cyan]LLM:[/cyan] {type(llm_client).__name__}")
    except Exception as e:
        console.print(f"[red]LLM indisponível ({e}) — impossível comparar.[/red]")
        raise typer.Exit(1)

    # Procura PDF pra tentar enriquecer observacoes_pdf (mesma estratégia do
    # reprocessar_laudos: tenta data/laudos_pdfs/ primeiro, depois data/laudos_amostra/).
    PDF_DIRS = [Path("data/laudos_pdfs"), Path("data/laudos_amostra")]

    def _pdf_path(lote_id: str) -> Optional[Path]:
        for base in PDF_DIRS:
            candidato = base / f"{lote_id}.pdf"
            if candidato.exists():
                return candidato
            # fallback glob pra nomes tipo "21854782_fiesta.pdf"
            for m in base.glob(f"{lote_id}*.pdf"):
                return m
        return None

    with get_session() as session:
        query = select(Lote, LaudoCache).join(LaudoCache, LaudoCache.lote_id == Lote.id)
        if lotes:
            query = query.where(Lote.id.in_(lotes))
        pares = session.exec(query).all()

    linhas = []
    for lote, lc in pares:
        if lc.confidence < min_confidence:
            continue
        avarias = [
            Avaria(
                parte=a["parte"],
                severidade=SeveridadeAvaria(a["severidade"]),
                descricao=a.get("descricao", ""),
            )
            for a in (lc.avarias_json or [])
        ]
        if len(avarias) < min_avarias:
            continue

        laudo = LaudoEstruturado(
            avarias=avarias,
            severidade_geral=SeveridadeAvaria(lc.severidade_geral),
            motor_ok=bool(lc.motor_ok),
            documentacao=StatusDocumentacao(lc.documentacao),
            categoria_veiculo=CategoriaVeiculo(lc.categoria_veiculo),
            confidence=lc.confidence,
        )

        # Observações do inspetor enriquecem o prompt quando temos o PDF
        observacoes = ""
        pdf = _pdf_path(lote.id)
        if pdf is not None:
            try:
                observacoes = parse_laudo_textual(pdf).observacoes or ""
            except Exception:
                observacoes = ""

        lote_info = {
            "marca": lote.marca, "modelo": lote.modelo, "ano": lote.ano,
            "km": lote.km, "lance_atual": lote.lance_atual,
        }

        # 1. Determinístico
        custo_tab = estimar_tabela(laudo, empresa_cfg)

        # 2. LLM — invocação direta pra distinguir sucesso real de fallback
        prompt = _build_prompt(laudo, lote_info, empresa_cfg, observacoes)
        veiculo = f"{lote.marca} {lote.modelo} {lote.ano}"
        console.print(f"[dim]→ chamando LLM p/ {lote.id} ({veiculo})...[/dim]")
        try:
            raw = llm_client.generate_json(prompt)
            custo_llm = _parse_resposta(raw)
            llm_ok = True
            llm_erro = ""
        except Exception as e:
            llm_ok = False
            llm_erro = f"{type(e).__name__}: {e}"
            custo_llm = None
            console.print(f"[yellow]  LLM falhou: {llm_erro[:100]}[/yellow]")

        linhas.append({
            "lote_id": lote.id,
            "veiculo": veiculo[:35],
            "n_avarias": len(avarias),
            "severidade": lc.severidade_geral,
            "motor_ok": lc.motor_ok,
            "tem_observacoes": bool(observacoes.strip()),
            "tabela_total": custo_tab.custo_total,
            "tabela_itens": [f"R$ {i.custo}: {i.descricao}" for i in custo_tab.itens],
            "llm_ok": llm_ok,
            "llm_erro": llm_erro,
            "llm_total": custo_llm.custo_total if custo_llm else 0,
            "llm_itens": [f"R$ {i.custo}: {i.descricao}" for i in (custo_llm.itens if custo_llm else [])],
        })
        if len(linhas) >= limit:
            break

    if not linhas:
        console.print("[yellow]Nenhum lote encontrado com os filtros dados.[/yellow]")
        return

    # Tabela resumo
    tbl = Table(title="Reforma: tabela YAML vs LLM")
    tbl.add_column("Lote")
    tbl.add_column("Veículo")
    tbl.add_column("#Av", justify="right")
    tbl.add_column("Sev")
    tbl.add_column("Motor")
    tbl.add_column("Obs", justify="center")
    tbl.add_column("Tabela R$", justify="right")
    tbl.add_column("LLM R$", justify="right")
    tbl.add_column("Δ%", justify="right")
    for r in linhas:
        if not r["llm_ok"]:
            delta = "[red]LLM ✗[/red]"
            llm_str = "—"
        elif r["tabela_total"] == 0 and r["llm_total"] == 0:
            delta = "—"
            llm_str = f"{r['llm_total']:,}".replace(",", ".")
        elif r["tabela_total"] == 0:
            delta = "[yellow]+∞[/yellow]"
            llm_str = f"{r['llm_total']:,}".replace(",", ".")
        else:
            pct = (r["llm_total"] - r["tabela_total"]) / r["tabela_total"] * 100
            cor = "green" if abs(pct) < 15 else ("yellow" if abs(pct) < 40 else "red")
            delta = f"[{cor}]{pct:+.0f}%[/{cor}]"
            llm_str = f"{r['llm_total']:,}".replace(",", ".")
        tbl.add_row(
            r["lote_id"], r["veiculo"],
            str(r["n_avarias"]), r["severidade"],
            "ok" if r["motor_ok"] else "NÃO",
            "✓" if r["tem_observacoes"] else "—",
            f"{r['tabela_total']:,}".replace(",", "."),
            llm_str, delta,
        )
    console.print(tbl)

    # Detalhe item-a-item
    for r in linhas:
        console.print(f"\n[bold cyan]{r['lote_id']} — {r['veiculo']}[/bold cyan]")
        console.print(f"  [dim]Tabela (R$ {r['tabela_total']:,})[/dim]".replace(",", "."))
        for it in r["tabela_itens"]:
            console.print(f"    • {it}")
        if r["llm_ok"]:
            console.print(f"  [dim]LLM (R$ {r['llm_total']:,})[/dim]".replace(",", "."))
            for it in r["llm_itens"]:
                console.print(f"    • {it}")
        else:
            console.print(f"  [red]LLM falhou: {r['llm_erro']}[/red]")

    # Métricas agregadas (só lotes onde LLM rodou)
    ok = [r for r in linhas if r["llm_ok"]]
    if ok:
        totais_tab = sum(r["tabela_total"] for r in ok)
        totais_llm = sum(r["llm_total"] for r in ok)
        delta_pct = (totais_llm - totais_tab) / max(totais_tab, 1) * 100
        console.print(
            f"\n[bold]Agregado ({len(ok)}/{len(linhas)} lotes com LLM ok):[/bold] "
            f"tabela R$ {totais_tab:,} | LLM R$ {totais_llm:,} | Δ% {delta_pct:+.0f}%".replace(",", ".")
        )


if __name__ == "__main__":
    app()
