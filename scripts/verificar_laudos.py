#!/usr/bin/env python
"""Verifica completude do laudo dos lotes ativos e auto-cura gaps.

Para cada lote que apareceria na planilha (fim_em > agora, tem AvaliacaoLote
pra empresa, não marcado como encerrado no detalhe), checa as TRÊS condições
que o usuário exige pra considerar "laudo pronto":

    1. URL válida de laudo em `lote.raw_json["detalhe"]["laudo_pdf_url"]`
       (passa `is_laudo_pdf_url`) — é o que vira HYPERLINK na coluna Laudo.
    2. PDF baixado em `data/laudos_pdfs/<lote_id>.pdf` e reconhecido como
       laudo real por `_pdf_eh_laudo_valido` (heurística de conteúdo).
    3. LaudoCache com `confidence >= 0.6` (= extração real, não fallback
       `_laudo_sem_pdf` de confidence 0.5).

Cada lote cai em uma das categorias:

    ok                   — passa nas três condições
    url_decoy            — URL persistida falha no gate; AUTO-LIMPA
    url_ausente          — laudo_pdf_url é None (scraper não achou modal lazy)
    pdf_nao_baixado      — URL ok, mas arquivo local não existe
    pdf_local_invalido   — arquivo existe mas não é laudo real; AUTO-REMOVE
    laudo_ausente        — LaudoCache não existe
    laudo_nao_analisado  — LaudoCache existe com confidence<0.6

Auto-heal em cada run:
    - `url_decoy`: delega pra `limpar_decoys_laudo.limpar_decoys` (limpa
      raw_json + derruba LaudoCache → retry no próximo ciclo rebaixa).
    - `pdf_local_invalido`: apaga o PDF + derruba LaudoCache (se veio daquele
      PDF) pra não contaminar reprocessamentos offline.
    - `laudo_nao_analisado` com confidence<0.6: derruba LaudoCache pra que
      `--somente-laudo-pendente` do retry entre no lote novamente.

Exit code:
    0  — 100% ok (ou só sobrou o que era auto-curável)
    1  — sobraram lotes precisando de re-scrape/re-download (o retry do cron
         vai cuidar no próximo ciclo; se persistir, olhar a amostra impressa)

Importável: `verificar(session, empresa_id, ...) -> ResultadoVerificacao`.

CLI:
    PYTHONPATH=. python scripts/verificar_laudos.py
    PYTHONPATH=. python scripts/verificar_laudos.py --dry-run
    PYTHONPATH=. python scripts/verificar_laudos.py --empresa carros_uberlandia
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import typer
from rich.console import Console
from rich.table import Table
from sqlmodel import Session, select

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.orquestrador import _PDF_STORAGE_DIR, _pdf_eh_laudo_valido
from carros_sa.scraping.parsers import is_laudo_pdf_url
from scripts.limpar_decoys_laudo import limpar_decoys

console = Console()
app = typer.Typer(add_completion=False)

# Confidence mínima pra considerar laudo "analisado de verdade".
# Espelha o threshold usado no SheetsExporter pra marcar "⚠ LAUDO NÃO ANALISADO".
_CONF_MIN_ANALISADO = 0.6

# Categorias ordenadas por severidade (pior primeiro) — afeta ordem de impressão.
CATEGORIAS = (
    "ok",
    "url_decoy",
    "url_ausente",
    "pdf_nao_baixado",
    "pdf_local_invalido",
    "laudo_ausente",
    "laudo_nao_analisado",
)


@dataclass
class DiagnosticoLote:
    lote_id: str
    modelo: str
    categoria: str
    url: Optional[str]
    pdf_path: Optional[str]
    confidence: Optional[float]


@dataclass
class ResultadoVerificacao:
    total_ativos: int = 0
    por_categoria: Dict[str, List[DiagnosticoLote]] = field(default_factory=dict)
    # Ações de cura realizadas neste run (contagem por tipo).
    decoys_limpos: int = 0
    pdfs_removidos: int = 0
    laudos_derrubados: int = 0

    @property
    def n_ok(self) -> int:
        return len(self.por_categoria.get("ok", []))

    @property
    def n_pendentes(self) -> int:
        """Lotes que não estão 'ok' depois do auto-heal — precisam de retry."""
        return self.total_ativos - self.n_ok


def _classificar(
    lote: Lote,
    laudo: Optional[LaudoCache],
    pdf_dir: Path,
) -> tuple[str, Optional[str], Optional[Path]]:
    """Retorna (categoria, url_persistida, pdf_path_se_existir)."""
    detalhe = (lote.raw_json or {}).get("detalhe") or {}
    url = detalhe.get("laudo_pdf_url")
    pdf_path = pdf_dir / f"{lote.id}.pdf"
    pdf_exists = pdf_path.exists()

    # URL presente mas falha no gate → decoy (maior prioridade, polui o resto).
    if url and not is_laudo_pdf_url(url):
        return ("url_decoy", url, pdf_path if pdf_exists else None)

    # Sem URL → scraper não extraiu. Pode ser anúncio sem laudo no modal lazy;
    # o retry com visita nova da página talvez recupere.
    if not url:
        return ("url_ausente", None, pdf_path if pdf_exists else None)

    # URL ok mas PDF local ausente — retry faz download via pipeline_lote.
    if not pdf_exists:
        return ("pdf_nao_baixado", url, None)

    # PDF existe mas não é laudo (ex.: baixamos PDF institucional no passado
    # e esqueceu no disco). _pdf_eh_laudo_valido detecta via marcadores.
    if not _pdf_eh_laudo_valido(pdf_path):
        return ("pdf_local_invalido", url, pdf_path)

    # PDF ok — resta o LaudoCache.
    if laudo is None:
        return ("laudo_ausente", url, pdf_path)

    if (laudo.confidence or 0) < _CONF_MIN_ANALISADO:
        return ("laudo_nao_analisado", url, pdf_path)

    return ("ok", url, pdf_path)


def verificar(
    session: Session,
    empresa_id: str = "carros_uberlandia",
    *,
    dry_run: bool = False,
    pdf_dir: Optional[Path] = None,
) -> ResultadoVerificacao:
    """Audita e cura gaps de laudo em lotes ativos do empresa_id.

    Em `dry_run=True` nenhum estado é mutado (nem arquivos, nem DB) — útil
    pro teste de regressão que só mede quantos lotes ainda precisam de retry.
    """
    pdf_dir = pdf_dir if pdf_dir is not None else _PDF_STORAGE_DIR
    result = ResultadoVerificacao(por_categoria={c: [] for c in CATEGORIAS})

    agora = datetime.now()
    ativos_com_avaliacao = session.exec(
        select(Lote, AvaliacaoLote)
        .where(Lote.id == AvaliacaoLote.lote_id)
        .where(AvaliacaoLote.empresa_id == empresa_id)
        .where(Lote.fim_em > agora)
    ).all()

    # Filtra lotes marcados como encerrados (badge ARREMATADO/etc) — espelha
    # o filtro do SheetsExporter pra medir exatamente o que o usuário vê.
    lotes_visiveis: List[Lote] = []
    for lote, _av in ativos_com_avaliacao:
        detalhe = (lote.raw_json or {}).get("detalhe") or {}
        if bool(detalhe.get("encerrado")):
            continue
        lotes_visiveis.append(lote)

    result.total_ativos = len(lotes_visiveis)

    for lote in lotes_visiveis:
        laudo = session.get(LaudoCache, lote.id)
        categoria, url, pdf_path = _classificar(lote, laudo, pdf_dir)

        # ---- Auto-heal ----
        if not dry_run:
            if categoria == "pdf_local_invalido" and pdf_path is not None:
                # Remove PDF podre localmente — próximo pipeline baixa de novo.
                pdf_path.unlink(missing_ok=True)
                result.pdfs_removidos += 1
                # Dropa LaudoCache se veio desse PDF — confidence<0.6 ou fuzzy.
                if laudo is not None:
                    session.delete(laudo)
                    result.laudos_derrubados += 1
            elif categoria == "laudo_nao_analisado" and laudo is not None:
                # Derruba cache stale pra próximo `--somente-laudo-pendente` reentrar.
                session.delete(laudo)
                result.laudos_derrubados += 1

        result.por_categoria[categoria].append(DiagnosticoLote(
            lote_id=lote.id,
            modelo=f"{lote.marca} {lote.modelo} {lote.ano}",
            categoria=categoria,
            url=url,
            pdf_path=str(pdf_path) if pdf_path else None,
            confidence=(laudo.confidence if laudo else None),
        ))

    # Decoys — delega pro helper já coberto por testes (limpa todos os lotes,
    # não só os ativos; é barato e idempotente). Só quando não é dry_run.
    if not dry_run:
        limpeza = limpar_decoys(session, dry_run=False)
        result.decoys_limpos = limpeza.decoys_limpos
        # `limpar_decoys` já derruba os LaudoCache dos decoys dele — soma aqui
        # pra reportar corretamente no relatório final.
        result.laudos_derrubados += limpeza.laudos_derrubados
        session.commit()

    return result


def _imprimir_relatorio(res: ResultadoVerificacao) -> None:
    tabela = Table(title="Completude do laudo — lotes ativos")
    tabela.add_column("Categoria", style="bold")
    tabela.add_column("Qtd", justify="right")
    tabela.add_column("Exemplo (lote · modelo)")
    for cat in CATEGORIAS:
        lotes = res.por_categoria.get(cat, [])
        qtd = len(lotes)
        if qtd == 0:
            continue
        cor = "green" if cat == "ok" else "yellow"
        exemplo = f"{lotes[0].lote_id} · {lotes[0].modelo[:30]}" if lotes else "—"
        tabela.add_row(f"[{cor}]{cat}[/{cor}]", str(qtd), exemplo)
    console.print(tabela)

    if res.decoys_limpos or res.pdfs_removidos or res.laudos_derrubados:
        console.print(
            f"\n[cyan]Auto-heal:[/cyan] "
            f"{res.decoys_limpos} decoys limpos · "
            f"{res.pdfs_removidos} PDFs inválidos removidos · "
            f"{res.laudos_derrubados} LaudoCache derrubados"
        )

    console.print(
        f"\n[bold]{res.n_ok}/{res.total_ativos} lotes com laudo completo "
        f"(baixado + analisado + link).[/bold]"
    )
    if res.n_pendentes > 0:
        console.print(
            f"[yellow]{res.n_pendentes} lote(s) dependem do próximo retry do "
            f"orquestrador pra completar (rodar `make triagem` ou o cron).[/yellow]"
        )


@app.command()
def main(
    empresa: str = typer.Option("carros_uberlandia", help="ID da empresa"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Não persiste nada."),
) -> None:
    """Audita completude de laudos nos lotes ativos da empresa e auto-cura."""
    from carros_sa.db import get_session, init_db

    init_db()
    with get_session() as session:
        res = verificar(session, empresa_id=empresa, dry_run=dry_run)

    _imprimir_relatorio(res)

    # Exit !=0 quando sobrou algo pendente pra facilitar alerta de cron/CI.
    if res.n_pendentes > 0:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
