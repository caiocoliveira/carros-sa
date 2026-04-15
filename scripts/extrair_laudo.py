"""Dispara o ExtratorLaudo sobre um PDF local.

Uso:
    PYTHONPATH=. .venv/bin/python scripts/extrair_laudo.py data/laudos_amostra/21854782_fiesta.pdf

Lê VISION_PROVIDER do ambiente (default: gemini). Precisa da key correspondente
em .env (GEMINI_API_KEY ou ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from carros_sa.agents.extrator_laudo import (
    extrair_laudo,
    extrair_laudo_visual,
    hash_pdf,
    parse_laudo_textual,
)
from carros_sa.agents.vision_clients import build_default_client

load_dotenv()
console = Console()


def main(pdf_path: str) -> None:
    path = Path(pdf_path)
    if not path.exists():
        console.print(f"[red]PDF não encontrado: {path}[/red]")
        sys.exit(1)

    console.print(Panel(f"PDF: {path}\nSHA1: {hash_pdf(path)}", title="Laudo"))

    # 1. Camada textual (sem LLM)
    t0 = time.time()
    txt = parse_laudo_textual(path)
    dt_txt = time.time() - t0

    tbl_txt = Table(title=f"Camada textual (PyMuPDF) — {dt_txt*1000:.0f} ms")
    tbl_txt.add_column("Campo"); tbl_txt.add_column("Valor")
    tbl_txt.add_row("Placa", txt.placa or "-")
    tbl_txt.add_row("Chassi", txt.chassi or "-")
    tbl_txt.add_row("KM (laudo)", f"{txt.km_laudo:,}" if txt.km_laudo else "-")
    tbl_txt.add_row("Licenciado", str(txt.licenciado))
    tbl_txt.add_row("Roubo/furto ativo", str(txt.roubo_furto_ativo))
    tbl_txt.add_row("Comunicado de venda", str(txt.comunicado_venda))
    tbl_txt.add_row("Chassi original", str(txt.chassi_original))
    tbl_txt.add_row("Motor original", str(txt.motor_original))
    tbl_txt.add_row("Odômetro legível", str(txt.odometro_legivel))
    console.print(tbl_txt)

    # 2. Camada visual (LLM)
    console.print("\n[cyan]→ Chamando VisionClient na página 2 (diagrama estrutural)...[/cyan]")
    client = build_default_client()
    console.print(f"  provider: [bold]{type(client).__name__}[/bold]")

    t0 = time.time()
    visual = extrair_laudo_visual(path, client)
    dt_vis = time.time() - t0

    console.print(f"[green]✓ Resposta em {dt_vis:.1f}s[/green]\n")
    console.print(Panel(json.dumps(visual, indent=2, ensure_ascii=False), title="JSON do VisionClient"))

    # 3. Consolidação em LaudoEstruturado
    laudo = extrair_laudo(path, client)
    console.print(Panel(
        f"severidade_geral: [bold]{laudo.severidade_geral.value}[/bold]\n"
        f"motor_ok: {laudo.motor_ok}\n"
        f"documentacao: {laudo.documentacao.value}\n"
        f"confidence: {laudo.confidence:.2f}\n"
        f"n_avarias: {len(laudo.avarias)}",
        title="LaudoEstruturado (consolidado)",
    ))
    if laudo.avarias:
        tbl_av = Table(title="Avarias detectadas")
        tbl_av.add_column("Peça"); tbl_av.add_column("Severidade"); tbl_av.add_column("Descrição")
        for av in laudo.avarias:
            tbl_av.add_row(av.parte, av.severidade.value, av.descricao)
        console.print(tbl_av)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/laudos_amostra/21854782_fiesta.pdf")
