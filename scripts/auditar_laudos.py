#!/usr/bin/env python
"""Auditoria de laudos — reporta e resolve gaps entre planilha e laudos baixados.

Pergunta de produto: **todos os carros da planilha têm laudo baixado, revisado
e link clicável?** Se não, **por quê?** Este script responde de forma estruturada.

Classifica cada lote ativo em 6 status (ver `carros_sa.tools.auditoria_laudos`):
    OK
    SEM_DETALHE      ← pipeline precisa raspar o detalhe (rodar triagem)
    SEM_URL          ← scraper não achou link; triagem pode re-tentar
    URL_DECOY        ← auto-fix: chama `limpar_decoys`
    PDF_AUSENTE      ← triagem re-baixa (URL assinada expira, não dá pra
                       só re-baixar sem re-scrape do detalhe)
    EXTRACAO_FALHOU  ← auto-fix: re-extrai do PDF local via reprocessar_laudos

Uso:
    make auditar-laudos                        # relatório
    make auditar-laudos FIX=1                  # relatório + auto-fix onde dá

Exit code != 0 quando resta qualquer gap após auto-fix — permite plugar em CI
ou cron pra alertar quando a triagem do dia deixou lotes sem laudo revisável.
"""

from __future__ import annotations

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from carros_sa.db import get_session, init_db
from carros_sa.tools.auditoria_laudos import (
    ResultadoAuditoria,
    StatusLaudo,
    auditar_empresa,
)

load_dotenv()
console = Console()
app = typer.Typer(add_completion=False)


# Mensagem-fix curta por status — o que o operador (ou o auto-fix) precisa
# fazer pra destravar o lote. Impresso na tabela.
_ACAO = {
    StatusLaudo.SEM_DETALHE: "rodar `make triagem` (raspa detalhe)",
    StatusLaudo.SEM_URL: "rodar `make triagem` (re-abre detalhe p/ achar URL)",
    StatusLaudo.URL_DECOY: "auto-fix: `make limpar-decoys`",
    StatusLaudo.PDF_AUSENTE: "rodar `make triagem` (URL assinada expira)",
    StatusLaudo.EXTRACAO_FALHOU: "auto-fix: reprocessar laudo do PDF local",
}


def _imprimir_relatorio(res: ResultadoAuditoria, *, apos_fix: bool = False) -> None:
    titulo_sufixo = " (após auto-fix)" if apos_fix else ""
    tbl = Table(title=f"Auditoria de laudos — {res.empresa_id}{titulo_sufixo}")
    tbl.add_column("Status")
    tbl.add_column("Qtd.", justify="right")
    tbl.add_column("Ação sugerida")
    tbl.add_column("Amostra", overflow="fold", max_width=40)

    # Ordem de exibição: OK primeiro (confirmação positiva), depois gaps.
    ordem = [StatusLaudo.OK] + [s for s in StatusLaudo if s != StatusLaudo.OK]
    for status in ordem:
        qtd = res.por_status.get(status, 0)
        if qtd == 0:
            continue
        lotes = res.lotes_por_status.get(status, [])
        amostra = ", ".join(lotes[:3]) + (f" … (+{len(lotes) - 3})" if len(lotes) > 3 else "")
        cor = "green" if status == StatusLaudo.OK else "yellow"
        acao = "—" if status == StatusLaudo.OK else _ACAO[status]
        tbl.add_row(f"[{cor}]{status.value}[/{cor}]", str(qtd), acao, amostra)

    console.print(tbl)
    console.print(
        f"\n[bold]{res.ok}/{res.total} lotes com laudo revisável. "
        f"Gaps: {res.gaps}.[/bold]"
    )


def _auto_fix_url_decoy(session) -> int:
    """Remove URLs-decoy + derruba LaudoCache afetado. Retorna nº de lotes tocados."""
    from scripts.limpar_decoys_laudo import limpar_decoys

    # `limpar_decoys` faz o commit dentro da função.
    r = limpar_decoys(session)
    return r.decoys_limpos


def _auto_fix_extracao(session, lotes_ids: list) -> int:
    """Re-extrai laudo do PDF local (sem rede). Usa o pipeline do reprocessar_laudos.

    Vantagem de rodar aqui dentro do auditor: não re-raspa nada, só aproveita
    os PDFs já em `data/laudos_pdfs/` e re-chama o ExtratorLaudo. Se o vision
    client tá offline (sem API key), retorna 0 sem quebrar — usuário vê no
    relatório e sabe que precisa configurar a chave.
    """
    if not lotes_ids:
        return 0

    # Lazy imports pra não puxar SDKs do Gemini/Anthropic quando não há gap
    # extracao_falhou — auditar_laudos é chamado por cron e queremos que o
    # caminho feliz não dependa de credencial de LLM.
    from carros_sa.agents.extrator_laudo import extrair_laudo, parse_laudo_textual
    from carros_sa.agents.text_llm_clients import build_default_text_client
    from carros_sa.agents.vision_clients import build_default_client
    from carros_sa.agents.avaliador_mercado import avaliar as avaliar_mercado
    from carros_sa.agents.estimador_reforma import estimar as estimar_reforma
    from carros_sa.agents.estimador_reforma_llm import estimar_llm as estimar_reforma_llm
    from carros_sa.models import Lote
    from carros_sa.orquestrador import (
        _calcular_frete,
        _laudo_de_textual,
        _laudo_sem_pdf,
        _upsert_avaliacao,
        _upsert_laudo_cache,
    )
    from carros_sa.precificador import precificar
    from carros_sa.tenancy import carregar_empresa
    from scripts.reprocessar_laudos import _reprocessar_um

    try:
        vision = build_default_client()
    except Exception as e:
        console.print(f"[yellow]vision client indisponível ({e}) — pulando extracao auto-fix[/yellow]")
        return 0

    try:
        text_llm = build_default_text_client()
    except Exception:
        text_llm = None

    # Todos os lotes afetados devem ser da mesma empresa (é a unidade do relatório).
    # Como podemos precisar reprocessar de várias empresas num loop futuro, aceitamos
    # carregamento via primeiro lote.
    arrumados = 0
    for lote_id in lotes_ids:
        lote = session.get(Lote, lote_id)
        if lote is None:
            continue
        # Usa a empresa associada à avaliação atual do lote. Aqui assumimos 1
        # empresa por auditoria (default do CLI) — o script passa empresa_id
        # no loop externo e a `empresa_cfg` é carregada lá.
        try:
            empresa_cfg = carregar_empresa(_EMPRESA_ATUAL)
        except FileNotFoundError:
            break

        res_lote = _reprocessar_um(
            lote, empresa_cfg, vision, session, dry_run=False,
            parse_laudo_textual=parse_laudo_textual,
            extrair_laudo=extrair_laudo,
            laudo_de_textual=_laudo_de_textual,
            laudo_sem_pdf=_laudo_sem_pdf,
            avaliar_mercado=avaliar_mercado,
            estimar_reforma=estimar_reforma,
            estimar_reforma_llm=estimar_reforma_llm,
            text_llm_client=text_llm,
            calcular_frete=_calcular_frete,
            precificar_fn=precificar,
            upsert_laudo=_upsert_laudo_cache,
            upsert_aval=_upsert_avaliacao,
        )
        if res_lote.get("status") == "ok" and (res_lote.get("confidence") or 0) >= 0.6:
            arrumados += 1

    session.commit()
    return arrumados


# Global passado entre `main` e `_auto_fix_extracao` — o caminho de re-extração
# depende da `empresa_cfg` e, pra evitar proliferação de parâmetros, guardamos
# o empresa_id do run atual aqui. Escopo 100% local ao script.
_EMPRESA_ATUAL: str = ""


@app.command()
def main(
    empresa: str = typer.Option("carros_uberlandia", help="ID da empresa"),
    auto_fix: bool = typer.Option(
        False, "--auto-fix/--no-auto-fix",
        help="Aplica correções possíveis (decoys, re-extração de PDF local).",
    ),
) -> None:
    """Audita laudos de uma empresa e (opcionalmente) aplica auto-fix."""
    global _EMPRESA_ATUAL
    _EMPRESA_ATUAL = empresa

    init_db()

    with get_session() as session:
        res = auditar_empresa(empresa, session)
        _imprimir_relatorio(res)

        if auto_fix and res.gaps > 0:
            console.print("\n[cyan]Aplicando auto-fix...[/cyan]")
            n_decoys = _auto_fix_url_decoy(session)
            if n_decoys:
                console.print(f"  [green]✓ {n_decoys} URLs-decoy limpas[/green]")
            n_extracoes = _auto_fix_extracao(
                session, res.lotes_por_status.get(StatusLaudo.EXTRACAO_FALHOU, []),
            )
            if n_extracoes:
                console.print(f"  [green]✓ {n_extracoes} laudos re-extraídos[/green]")

            # Re-audita pra mostrar o efeito do fix e atualizar o exit code.
            res = auditar_empresa(empresa, session)
            _imprimir_relatorio(res, apos_fix=True)

    if res.gaps > 0:
        # Exit != 0 → cron/CI alertam quando a triagem do dia deixou lotes
        # sem laudo revisável. Os motivos `SEM_DETALHE` / `SEM_URL` /
        # `PDF_AUSENTE` exigem re-scrape (triagem completa) — auditor
        # aponta e sai com erro pra acionar humano ou cron follow-up.
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
