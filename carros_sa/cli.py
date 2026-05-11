"""CLI unificada do Carros SA.

Instalado como entry point `carros-sa` via pyproject.toml.

Subcomandos:
    triagem        — pipeline completo (scraping → avaliação → Sheets)
    top            — ranking das melhores avaliações do DB (offline)
    ingest         — ingere JSON de listagem no SQLite
    extrair-laudo  — ExtratorLaudo standalone num PDF
    sheets         — exporta avaliações pro Google Sheets
    empresas       — lista empresas configuradas em config/empresas/

Uso:
    carros-sa triagem --empresa carros_uberlandia --top 10
    carros-sa top --empresa carros_uberlandia --n 10
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

# override=True faz secrets do .env vencerem o que estiver exportado no shell.
# Sem isso, vars vazias herdadas do parent (ex.: outro tooling que setou
# ANTHROPIC_API_KEY="" pra própria execução) sobrescrevem silenciosamente
# a chave real do .env e o pipeline cai pro fallback Gemini-only sem aviso.
load_dotenv(override=True)

app = typer.Typer(
    add_completion=False,
    help="Carros SA — triagem automática de lotes em leilão de carro.",
    no_args_is_help=True,
)
console = Console()


def _imprimir_vision_provider(client) -> None:
    """Mostra qual provider de visão está ativo + warn se sem cascata.

    Quando só Gemini está configurado (sem ANTHROPIC_API_KEY no .env), o
    fallback Haiku não roda. Triagem 2026-04-16 mostrou que isso resulta em
    ~75% dos lotes caindo no fallback textual quando Gemini dá erro
    silencioso, viesando fator_risco. O warn lembra de configurar.
    """
    nome = type(client).__name__
    if nome == "FallbackVisionClient":
        # Cascata ativa: Gemini → Haiku (ou qualquer combinação configurada)
        cliente_nomes = [type(c).__name__ for c in client._clients]
        console.print(f"[cyan]Vision provider:[/cyan] {nome} → {' → '.join(cliente_nomes)}")
    else:
        console.print(f"[cyan]Vision provider:[/cyan] {nome}")
        if nome == "GeminiVisionClient":
            console.print(
                "[yellow]⚠ Apenas Gemini ativo. Setar ANTHROPIC_API_KEY no .env "
                "ativa cascata Gemini→Haiku — cobre overload silencioso "
                "(~$5-15/mês em volumes de PoC).[/yellow]"
            )


# ---------------------------------------------------------------------------
# top — ranking offline a partir do SQLite
# ---------------------------------------------------------------------------

@app.command()
def top(
    empresa: str = typer.Option("carros_uberlandia", help="ID da empresa"),
    n: int = typer.Option(10, "--n", "--top", help="Quantos lotes mostrar (top N por ROI)"),
    por_absoluto: bool = typer.Option(False, "--absoluto", help="Ordena por ROI absoluto (sem anualizar)"),
    incluir_inviaveis: bool = typer.Option(
        False, "--incluir-inviaveis",
        help="Mostra também lotes onde lance_atual > preco_max (default: oculta)",
    ),
) -> None:
    """Lista as top N avaliações da empresa já persistidas no SQLite.

    Coluna principal de retorno: 'ROI alvo (%)' = `score_efetivo × 100` (cru,
    sem anualizar, base efetiva que reflete o lance atual real). Coerente com
    a coluna 'ROI alvo (%)' da planilha — paridade explícita. score_efetivo =
    score_roi original quando lance_atual ≤ preco_alvo; reduzido em zona
    apertada (capital efetivo cresce). Por construção `capital × ROI ≈ Lucro`
    bate (operator mental math passa).

    Ordenação default: ROI ANUALIZADO interno (`score_efetivo × 365 / dias_giro`).
    Premia carros de giro rápido — duas linhas com ROI alvo igual podem aparecer
    em ordens diferentes porque o desempate usa o tempo de giro. `--absoluto`
    força ranking pelo `score_roi` INTRINSIC (alvo teórico) — útil pra sniff-test
    de oportunidade independente do lance atual; difere de score_efetivo apenas
    em zona apertada e lotes inviáveis.

    Filtro default: oculta lotes inviáveis (lance atual já passou do nosso teto).
    Use `--incluir-inviaveis` pra ver tudo (útil pra calibrar fórmula).
    """
    from sqlmodel import select

    from carros_sa.agents.calibracao_giro import roi_anualizado
    from carros_sa.db import get_session, init_db
    from carros_sa.models import AvaliacaoLote, Lote

    init_db()

    with get_session() as session:
        stmt = (
            select(AvaliacaoLote, Lote)
            .join(Lote, Lote.id == AvaliacaoLote.lote_id)  # type: ignore[arg-type]
            .where(AvaliacaoLote.empresa_id == empresa)
        )
        todas = session.exec(stmt).all()

    if not todas:
        console.print(
            f"[yellow]Nenhuma avaliação para empresa '{empresa}'. "
            "Rode [bold]carros-sa triagem[/bold] primeiro.[/yellow]"
        )
        raise typer.Exit(0)

    # Filtro de viabilidade: lance atual já passou do preco_max → não cabe lance.
    # Sem isso, ranking enche de lotes "caros demais" e esconde os de fato comprávies.
    total_avaliados = len(todas)
    if not incluir_inviaveis:
        todas = [(av, lote) for av, lote in todas if av.preco_max > (lote.lance_atual or 0)]

    if not todas:
        console.print(
            f"[yellow]Nenhum lote viável (lance atual ≤ preço máximo) entre os "
            f"{total_avaliados} avaliados. Use [bold]--incluir-inviaveis[/bold] "
            "pra ver todos.[/yellow]"
        )
        raise typer.Exit(0)

    n_inviaveis = total_avaliados - len(todas)

    # Anota cada linha com ROI anualizado HONESTO (entrada por max(lance, alvo)).
    # `score_roi_efetivo` cai pra `score_roi` quando `lance_atual ≤ preco_alvo`,
    # mas reduz o ROI exibido quando o leilão já passou do alvo. Ranking fica
    # alinhado com o cenário real de compra.
    from carros_sa.tools.sheets import _score_roi_efetivo
    enriquecidas = [
        (
            av,
            lote,
            roi_anualizado(_score_roi_efetivo(av, lote.lance_atual), av.dias_giro_estimado),
        )
        for av, lote in todas
    ]
    if por_absoluto:
        # `--absoluto` = ranking pelo ROI INTRINSIC (score_roi cru, sem
        # anualização). Mantido propositalmente intrinsic mesmo após o fix
        # de display efetivo de 2026-05-10 — esse flag responde a "quais
        # lotes têm POTENCIAL econômico se eu conseguir entrar pelo alvo
        # calibrado", útil pra sniff-test de oportunidade. Default (sem
        # `--absoluto`) usa anualizado sobre score_efetivo, que é a métrica
        # realista do que o operador vai ganhar dado o lance atual real.
        enriquecidas.sort(key=lambda x: x[0].score_roi, reverse=True)
    else:
        enriquecidas.sort(key=lambda x: x[2], reverse=True)
    rows = enriquecidas[:n]

    # Inferência de popularidade on-demand pra cada lote do top (barato, sem rede)
    from carros_sa.agents.calibracao_giro import _categoria_de_modelo
    from carros_sa.tools.popularidade import bucket_modelo

    sufixo = "ROI alvo" if por_absoluto else "ROI anualizado interno (giro)"
    titulo = f"Top {len(rows)} lotes — {empresa} (ordem: {sufixo})"
    if not incluir_inviaveis and n_inviaveis > 0:
        titulo += f" — {n_inviaveis} inviável(is) ocultado(s)"
    tbl = Table(title=titulo)
    tbl.add_column("Lote")
    tbl.add_column("Modelo")
    tbl.add_column("Ano", justify="right")
    tbl.add_column("Lance", justify="right")
    tbl.add_column("Preço-Alvo", justify="right")
    tbl.add_column("ROI alvo", justify="right")
    tbl.add_column("Dias", justify="right")
    tbl.add_column("Lucro", justify="right")
    tbl.add_column("Pop.", justify="left")
    tbl.add_column("Risco", justify="right")
    from carros_sa.tools.sheets import _lucro_absoluto_efetivo
    for av, lote, _roi_anual_ranking_only in rows:
        cat = _categoria_de_modelo(lote.modelo)
        bucket = bucket_modelo(lote.marca, lote.modelo, cat, ano=lote.ano)
        # Lucro absoluto efetivo: usa entrada por `max(lance_atual, preco_alvo)`
        # pra refletir o capital empatado real (no alvo se leilão ainda permite,
        # acima do alvo se já passou). Bate com a coluna "Lucro (R$)" da planilha.
        lucro_esperado = _lucro_absoluto_efetivo(av, lote.lance_atual)
        # ROI exibido = score_efetivo (mesmo basis do Lucro) — fix P5b 2026-05-10.
        # Antes era `score_roi` intrinsic, divergindo do Lucro exibido em zona
        # apertada (operator math `capital × ROI ≈ Lucro` não batia).
        score_ef = _score_roi_efetivo(av, lote.lance_atual)
        tbl.add_row(
            lote.id,
            f"{lote.marca} {lote.modelo[:30]}",
            str(lote.ano),
            f"R$ {lote.lance_atual:,}",
            f"R$ {av.preco_alvo:,}",
            f"{score_ef * 100:.1f}%",
            str(av.dias_giro_estimado) if av.dias_giro_estimado else "—",
            f"R$ {lucro_esperado:,}",
            bucket.value,
            f"{av.fator_risco:.2f}",
        )
    console.print(tbl)


# ---------------------------------------------------------------------------
# empresas — listar configs
# ---------------------------------------------------------------------------

@app.command()
def empresas() -> None:
    """Lista empresas configuradas em config/empresas/*.yaml."""
    config_dir = Path("config/empresas")
    if not config_dir.exists():
        console.print("[red]config/empresas/ não encontrado (rode do root do repo)[/red]")
        raise typer.Exit(1)

    yamls = sorted(config_dir.glob("*.yaml"))
    if not yamls:
        console.print("[yellow]Nenhuma empresa configurada[/yellow]")
        raise typer.Exit(0)

    from carros_sa.tenancy import carregar_empresa

    tbl = Table(title="Empresas configuradas")
    tbl.add_column("ID")
    tbl.add_column("Pátio")
    tbl.add_column("Margem-alvo", justify="right")
    tbl.add_column("Config")
    for y in yamls:
        empresa_id = y.stem
        try:
            emp = carregar_empresa(empresa_id)
            tbl.add_row(
                empresa_id,
                f"{emp.patio.cidade}/{emp.patio.uf}",
                f"{emp.margem.base:.0%}",
                str(y.relative_to(Path.cwd())) if y.is_absolute() else str(y),
            )
        except Exception as exc:
            tbl.add_row(empresa_id, "[red]erro[/red]", "-", f"[red]{exc}[/red]")
    console.print(tbl)


# ---------------------------------------------------------------------------
# ingest — JSON de listagem → SQLite
# ---------------------------------------------------------------------------

@app.command()
def ingest(
    arquivo: Path = typer.Argument(..., help="JSON de listagem (ex: data/scrapes/*.json)"),
) -> None:
    """Ingere um JSON de listagem coletada no SQLite (upsert em lote)."""
    if not arquivo.exists():
        console.print(f"[red]Arquivo não encontrado: {arquivo}[/red]")
        raise typer.Exit(1)

    from sqlmodel import select

    from carros_sa.db import get_session, init_db
    from carros_sa.models import Lote
    from carros_sa.scraping.parsers import extrair_loja_do_card, parse_card_lines

    init_db()
    data = json.loads(arquivo.read_text())
    lotes_raw = data.get("lotes_amostra", [])
    parsed = []
    falhas = 0
    for entry in lotes_raw:
        try:
            lote = parse_card_lines(entry["lines"], entry["loteId"], entry["href"])
            parsed.append((lote, extrair_loja_do_card(entry["lines"])))
        except Exception:
            falhas += 1

    with get_session() as session:
        for lote, loja in parsed:
            existente = session.get(Lote, lote.lote_id)
            raw = lote.model_dump(mode="json")
            if loja:
                raw["loja"] = loja
            elif existente and isinstance(existente.raw_json, dict) and existente.raw_json.get("loja"):
                raw["loja"] = existente.raw_json["loja"]
            row = Lote(
                id=lote.lote_id,
                leilao=lote.leilao,
                url=lote.url,
                marca=lote.marca,
                modelo=lote.modelo,
                ano=lote.ano,
                km=lote.km,
                lance_atual=lote.lance_atual,
                fim_em=lote.fim_em,
                origem_cidade=lote.origem_cidade,
                origem_uf=lote.origem_uf,
                raw_json=raw,
            )
            if existente:
                for k, v in row.model_dump(exclude={"id"}).items():
                    setattr(existente, k, v)
            else:
                session.add(row)
        session.commit()
        total = len(session.exec(select(Lote)).all())

    console.print(
        f"[green]✓ {len(parsed)}/{len(lotes_raw)} lotes ingeridos[/green]"
        + (f" ([red]{falhas} falhas[/red])" if falhas else "")
    )
    console.print(f"Total em `lote`: {total}")


# ---------------------------------------------------------------------------
# arrematado-import — CSV de histórico → Lote sintético + Arrematado
# ---------------------------------------------------------------------------

@app.command("arrematado-import")
def arrematado_import_cmd(
    arquivo: Path = typer.Argument(
        ...,
        help=(
            "CSV de histórico. Colunas obrigatórias: marca, modelo, ano, valor_compra. "
            "Opcionais: km, data_compra, valor_venda, data_venda, observacoes. "
            "Custos pós-arremate em DOIS formatos: (legacy) custos_extras agregado, "
            "ou (decomposto, HH-2) taxa_leilao_real + frete_real + transferencia_real "
            "+ higienizacao_real + outros_extras_real + gastos_reforma_real. "
            "Decomposto vence quando qualquer bucket está preenchido."
        ),
    ),
    empresa: str = typer.Option(..., help="ID da empresa (ex: carros_uberlandia)"),
) -> None:
    """Importa histórico de compra/venda offline pra tabela Arrematado.

    Cria um Lote sintético (`leilao="historico_offline"`) por linha pra preservar
    a integridade referencial do schema. Idempotente — re-rodar atualiza linhas
    existentes (matching por marca+modelo+ano+valor_compra).
    """
    if not arquivo.exists():
        console.print(f"[red]Arquivo não encontrado: {arquivo}[/red]")
        raise typer.Exit(1)

    from carros_sa.db import get_session, init_db
    from carros_sa.tools.historico_import import importar_historico, parse_csv

    init_db()
    rows, erros_parse = parse_csv(arquivo)

    if erros_parse:
        console.print(f"[yellow]{len(erros_parse)} linha(s) com erro de parse:[/yellow]")
        for linha, msg in erros_parse[:5]:
            console.print(f"  linha {linha}: {msg}")
        if len(erros_parse) > 5:
            console.print(f"  ... e mais {len(erros_parse) - 5}")

    with get_session() as session:
        result = importar_historico(rows, empresa, session)

    tbl = Table(title=f"Importação histórico — {empresa}")
    tbl.add_column("Métrica")
    tbl.add_column("Valor", justify="right")
    tbl.add_row("Linhas no CSV", str(len(rows) + len(erros_parse)))
    tbl.add_row("Lotes criados", f"[green]{result.criados}[/green]")
    tbl.add_row("Lotes atualizados", str(result.atualizados))
    tbl.add_row("Erros parse", str(len(erros_parse)))
    tbl.add_row("Erros import", str(len(result.erros)))
    console.print(tbl)

    if result.erros:
        console.print("[red]Erros de import:[/red]")
        for linha, msg in result.erros[:10]:
            console.print(f"  linha {linha}: {msg}")


# ---------------------------------------------------------------------------
# extrair-laudo — PDF → LaudoEstruturado
# ---------------------------------------------------------------------------

@app.command("extrair-laudo")
def extrair_laudo_cmd(
    pdf: Path = typer.Argument(..., help="Caminho do PDF de laudo"),
) -> None:
    """Roda ExtratorLaudo num PDF local e imprime o LaudoEstruturado."""
    if not pdf.exists():
        console.print(f"[red]PDF não encontrado: {pdf}[/red]")
        raise typer.Exit(1)

    from carros_sa.agents.extrator_laudo import extrair_laudo
    from carros_sa.agents.vision_clients import build_default_client

    client = build_default_client()
    _imprimir_vision_provider(client)
    laudo = extrair_laudo(pdf, client)
    console.print_json(laudo.model_dump_json(indent=2))


# ---------------------------------------------------------------------------
# sheets — exporta avaliações pro Google Sheets
# ---------------------------------------------------------------------------

@app.command()
def sheets(
    empresa: str = typer.Option(..., help="ID da empresa"),
    sheet_id: Optional[str] = typer.Option(None, help="Override do GOOGLE_SHEETS_ID"),
    credentials: Optional[str] = typer.Option(None, help="Override do GOOGLE_SERVICE_ACCOUNT_PATH"),
) -> None:
    """Exporta avaliações da empresa para uma aba no Google Sheets."""
    sheet_id = sheet_id or os.environ.get("GOOGLE_SHEETS_ID")
    credentials = credentials or os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")

    if not sheet_id:
        console.print("[red]Erro: defina GOOGLE_SHEETS_ID no .env ou passe --sheet-id[/red]")
        raise typer.Exit(1)
    if not credentials:
        console.print("[red]Erro: defina GOOGLE_SERVICE_ACCOUNT_PATH no .env ou passe --credentials[/red]")
        raise typer.Exit(1)
    if not Path(credentials).exists():
        console.print(f"[red]Arquivo de credenciais não encontrado: {credentials}[/red]")
        raise typer.Exit(1)

    from carros_sa.db import get_session, init_db
    from carros_sa.tools.sheets import SheetsExporter

    init_db()
    exporter = SheetsExporter(spreadsheet_id=sheet_id, credentials_path=credentials)
    with get_session() as session:
        n = exporter.exportar(empresa_id=empresa, session=session)

    console.print(f"[green]✓ {n} lotes exportados → aba \"{empresa}\"[/green]")
    console.print(f"  Sheet: {exporter.sheet_url}")


# ---------------------------------------------------------------------------
# triagem — pipeline completo (thin wrapper do scripts/triagem_diaria.py)
# ---------------------------------------------------------------------------

@app.command()
def triagem(
    empresa: str = typer.Option("carros_uberlandia", help="ID da empresa"),
    horizonte_dias: int = typer.Option(
        30,
        help=(
            "Janela de exibição na planilha (lotes com fim nos próximos N dias). "
            "A coleta puxa tudo — isso filtra só o que aparece na Sheet."
        ),
    ),
    headless: bool = typer.Option(True, help="False = abre browser visível (debug)"),
    sem_sheets: bool = typer.Option(False, help="Pular exportação para Sheets"),
    top_n: int = typer.Option(10, "--top", help="Quantos lotes mostrar no ranking final"),
) -> None:
    """Roda pipeline completo: scraping → avaliação → Google Sheets."""
    import asyncio

    email = os.environ.get("AUTOAVALIAR_EMAIL")
    password = os.environ.get("AUTOAVALIAR_PASSWORD")
    if not email or not password:
        console.print("[red]Erro: AUTOAVALIAR_EMAIL e AUTOAVALIAR_PASSWORD devem estar no .env[/red]")
        raise typer.Exit(1)

    asyncio.run(_run_triagem(empresa, horizonte_dias, headless, sem_sheets, top_n, email, password))


async def _run_triagem(
    empresa_id: str,
    horizonte_dias: int,
    headless: bool,
    sem_sheets: bool,
    top_n: int,
    email: str,
    password: str,
) -> None:
    from playwright.async_api import async_playwright

    from carros_sa.agents.vision_clients import build_default_client
    from carros_sa.agents.text_llm_clients import build_default_text_client
    from carros_sa.db import get_session, init_db
    from carros_sa.orquestrador import orquestrar
    from carros_sa.scraping.scraper_autoavaliar import garantir_autenticado

    init_db()
    vision_client = build_default_client()
    _imprimir_vision_provider(vision_client)

    # Text LLM pro EstimadorReformaLLM. Se nada configurado, pipeline usa
    # determinístico (sem quebrar). Log explícito pra transparência do custo.
    try:
        text_llm_client = build_default_text_client()
        console.print(f"[cyan]Reforma LLM:[/cyan] {type(text_llm_client).__name__}")
    except RuntimeError:
        text_llm_client = None
        console.print("[yellow]Reforma LLM: desabilitado (sem API key) → usando tabela determinística[/yellow]")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        console.print("\n[bold]Autenticando no Auto Avaliar...[/bold]")
        try:
            await garantir_autenticado(page, email, password)
            console.print("[green]✓ Sessão ativa[/green]")
        except Exception as exc:
            console.print(f"[red]Erro de autenticação: {exc}[/red]")
            await browser.close()
            raise typer.Exit(1)

        console.print(
            f"\n[bold]Coletando leilões ({empresa_id}, horizonte {horizonte_dias}d)...[/bold]"
        )
        with get_session() as session:
            # `horizonte_dias=None` → coleta toda a listagem (inclui leilões
            # agendados pra daqui a semanas). O recorte de exibição acontece
            # depois, no `SheetsExporter.exportar(horizonte_exibicao_dias=...)`.
            result = await orquestrar(
                empresa_id=empresa_id,
                session=session,
                page=page,
                vision_client=vision_client,
                horizonte_dias=None,
                text_llm_client=text_llm_client,
            )

        await browser.close()

    console.print(
        f"\n[green]✓ {result.n_coletados} coletados ({result.n_novos} novos) | "
        f"{result.n_avaliados} avaliados | {result.n_descartados} descartados | "
        f"{result.n_erros} erros[/green]"
    )

    if result.lotes:
        rankeados = sorted(result.lotes, key=lambda x: (x.roi_pct or -1), reverse=True)[:top_n]
        tbl = Table(title=f"Top {len(rankeados)} da rodada")
        tbl.add_column("Lote")
        tbl.add_column("Modelo")
        tbl.add_column("Status")
        tbl.add_column("Preço-Alvo", justify="right")
        tbl.add_column("ROI%", justify="right")
        for r in rankeados:
            if r.erro:
                status = f"[red]ERRO: {r.erro[:40]}[/red]"
            elif r.motivo_descarte:
                status = f"[yellow]descartado: {r.motivo_descarte}[/yellow]"
            elif r.preco_alvo:
                status = "[green]avaliado[/green]"
            else:
                status = "[dim]já avaliado[/dim]"
            tbl.add_row(
                r.lote_id,
                r.modelo[:35],
                status,
                f"R$ {r.preco_alvo:,}" if r.preco_alvo else "—",
                f"{r.roi_pct:.1f}%" if r.roi_pct is not None else "—",
            )
        console.print(tbl)

    if sem_sheets:
        console.print("\n[dim]--sem-sheets: exportação pulada[/dim]")
        # Audit roda mesmo sem export — usuário ainda quer saber se algum
        # lote ficou pendente após o pipeline.
        n_pendentes = _auditar_apos_triagem(empresa_id)
        if n_pendentes > 0:
            raise typer.Exit(1)
        return

    sheet_id = os.environ.get("GOOGLE_SHEETS_ID")
    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")
    if not sheet_id or not creds_path:
        console.print(
            "\n[yellow]GOOGLE_SHEETS_ID / GOOGLE_SERVICE_ACCOUNT_PATH não setados — "
            "exportação pulada.[/yellow]"
        )
        n_pendentes = _auditar_apos_triagem(empresa_id)
        if n_pendentes > 0:
            raise typer.Exit(1)
        return

    from carros_sa.tools.sheets import SheetsExporter

    console.print("\n[bold]Exportando para Google Sheets...[/bold]")
    try:
        exporter = SheetsExporter(spreadsheet_id=sheet_id, credentials_path=creds_path)
        with get_session() as session:
            n = exporter.exportar(
                empresa_id=empresa_id,
                session=session,
                horizonte_exibicao_dias=horizonte_dias,
            )
        console.print(f"[green]✓ {n} lotes exportados → aba \"{empresa_id}\"[/green]")
        console.print(f"  Sheet: {exporter.sheet_url}")
    except Exception as exc:
        # Falha no Sheets é falha do produto principal — operador vai abrir
        # planilha desatualizada sem saber. Saímos com exit 1 (mesmo padrão
        # do `triagem_diaria.py` pós-PR #57). Audit final NÃO roda nesse
        # caminho — sem export, não faz sentido auditar consistência sheet↔DB.
        console.print(f"[red]Erro ao exportar: {exc}[/red]")
        raise typer.Exit(1)

    # Audit final — fecha o laço "todo lote ativo na planilha tem laudo
    # baixado + revisado + linkado". Sem isso, lotes que escapam do retry
    # ficam silenciosamente como "⚠ LAUDO NÃO CAPTURADO" e o operador só
    # descobre olhando a aba. Falha (exit 1) é o sinal explícito pro cron/log.
    n_pendentes = _auditar_apos_triagem(empresa_id)
    if n_pendentes > 0:
        raise typer.Exit(1)


def _auditar_apos_triagem(empresa_id: str) -> int:
    """Audita laudos ativos depois do pipeline + export. Retorna nº de incompletos.

    Imprime relatório curto + ponteiro pra ações de correção quando há
    pendências. Não decide exit code aqui — quem chama decide se trava
    (CLI manual: travar; testes: ignorar).
    """
    from carros_sa.db import get_session
    from carros_sa.tools.laudo_audit import auditar

    with get_session() as session:
        rel = auditar(session, empresa_id)

    pct = (rel.completos / rel.total * 100) if rel.total else 100.0
    console.print(
        f"\n[bold]Auditoria de laudos ({empresa_id}):[/bold] "
        f"{rel.completos}/{rel.total} completos ({pct:.1f}%)"
    )
    if not rel.incompletos:
        console.print("  [green]✓ Todos os lotes ativos têm laudo baixado, revisado e linkado.[/green]")
        return 0

    console.print(
        f"  [yellow]⚠ {len(rel.incompletos)} incompleto(s)[/yellow] — "
        f"sem PDF: {rel.sem_pdf}, conf<0.6: {rel.cache_baixa_conf}, URL inválida: {rel.url_invalida}"
    )
    for s in rel.incompletos[:10]:
        console.print(f"    [dim]{s.lote_id}[/dim] {s.modelo[:32]} → {s.motivo}")
    if len(rel.incompletos) > 10:
        console.print(f"    [dim]… +{len(rel.incompletos) - 10} truncados[/dim]")
    console.print(
        "  [cyan]Pra destravar:[/cyan] "
        "[code]make limpar-decoys && PYTHONPATH=. .venv/bin/python "
        f"scripts/reprocessar_lotes_do_db.py --empresa {empresa_id} "
        "--somente-ativos --somente-laudo-pendente[/code]"
    )
    return len(rel.incompletos)


# ---------------------------------------------------------------------------
# registrar-compra — entrada interativa de compra real no CSV + DB
# ---------------------------------------------------------------------------

# Schema canônico do CSV de histórico (HH-2). Manter em sync com
# `data/historico/<empresa>_arrematado.csv` — qualquer coluna nova em HH-N+
# precisa ser adicionada aqui também.
_HISTORICO_CSV_HEADER = [
    "marca", "modelo", "ano", "km", "valor_compra", "data_compra",
    "custos_extras", "valor_venda", "data_venda",
    "taxa_leilao_real", "frete_real", "transferencia_real",
    "higienizacao_real", "outros_extras_real", "gastos_reforma_real",
    "observacoes",
]


def _upsert_csv_row(csv_path: Path, nova_linha: dict) -> bool:
    """Atualiza ou append a `nova_linha` no CSV de histórico.

    Chave de idempotência: (marca, modelo, ano, valor_compra) — mesma do DB.

    Retorna True se atualizou linha existente, False se fez append.
    Garante que o CSV terá EXATAMENTE as colunas de `_HISTORICO_CSV_HEADER`
    (preenche vazio nos campos ausentes de linhas legacy).
    """
    import csv as csv_module

    atualizado = False
    rows: list[dict] = []

    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as fh:
            reader = csv_module.DictReader(fh)
            for row in reader:
                if (
                    row.get("marca", "").strip() == nova_linha["marca"].strip()
                    and row.get("modelo", "").strip() == nova_linha["modelo"].strip()
                    and row.get("ano", "").strip() == nova_linha["ano"]
                    and row.get("valor_compra", "").strip() == nova_linha["valor_compra"]
                ):
                    rows.append(nova_linha)
                    atualizado = True
                else:
                    rows.append(dict(row))

    if not atualizado:
        rows.append(nova_linha)

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        import csv as csv_module  # noqa: F811 — re-import inside function is fine
        writer = csv_module.DictWriter(fh, fieldnames=_HISTORICO_CSV_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in _HISTORICO_CSV_HEADER})

    return atualizado


@app.command("registrar-compra")
def registrar_compra(
    empresa: str = typer.Option(..., "--empresa", prompt="Empresa (ex: carros_uberlandia)", help="ID da empresa"),
    marca: str = typer.Option(..., "--marca", prompt="Marca (ex: Ford)", help="Marca do veículo"),
    modelo: str = typer.Option(..., "--modelo", prompt="Modelo (ex: Fusion Titanium 2.0 AWD)", help="Modelo do veículo"),
    ano: int = typer.Option(..., "--ano", prompt="Ano", help="Ano do veículo"),
    valor: int = typer.Option(..., "--valor", prompt="Valor de compra (R$)", help="Valor pago no leilão"),
    km: Optional[int] = typer.Option(None, "--km", help="Quilometragem"),
    data: Optional[str] = typer.Option(None, "--data", help="Data de compra (YYYY-MM-DD ou DD/MM/YYYY)"),
    taxa: Optional[int] = typer.Option(None, "--taxa", help="Taxa do leiloeiro (R$)"),
    frete: Optional[int] = typer.Option(None, "--frete", help="Frete (R$)"),
    transf: Optional[int] = typer.Option(None, "--transf", help="Transferência interestadual DETRAN (R$)"),
    higi: Optional[int] = typer.Option(None, "--higi", help="Higienização/polimento (R$)"),
    outros: Optional[int] = typer.Option(None, "--outros", help="Outros extras (R$)"),
    reforma: Optional[int] = typer.Option(None, "--reforma", help="Reforma/peças (R$)"),
    obs: str = typer.Option("", "--obs", help="Observações livres"),
    valor_venda: Optional[int] = typer.Option(None, "--valor-venda", help="Valor de venda (se já vendido)"),
    data_venda: Optional[str] = typer.Option(None, "--data-venda", help="Data de venda (YYYY-MM-DD ou DD/MM/YYYY)"),
    csv_override: Optional[Path] = typer.Option(None, "--csv", help="Override do caminho do CSV (default: data/historico/<empresa>_arrematado.csv)"),
) -> None:
    """Registra uma compra real no CSV de histórico e no banco.

    Aceita flags ou prompts interativos quando campo obrigatório for omitido.
    Idempotente: re-rodar com mesmo (marca, modelo, ano, valor) atualiza em vez de duplicar.
    Escreve no formato decomposto (HH-2) — taxa, frete, transf, higi, outros, reforma separados.
    """
    from datetime import datetime as dt

    from carros_sa.db import get_session, init_db
    from carros_sa.tenancy import carregar_empresa
    from carros_sa.tools.historico_import import HistoricoRow, _parse_data, importar_historico

    # Valida empresa antes de qualquer escrita — falha rápido com mensagem útil.
    try:
        carregar_empresa(empresa)
    except (FileNotFoundError, Exception) as exc:
        console.print(
            f"[red]Empresa '{empresa}' não encontrada: {exc}\n"
            "Rode [bold]carros-sa empresas[/bold] pra ver as disponíveis.[/red]"
        )
        raise typer.Exit(1)

    ano_atual = dt.now().year

    if valor <= 0:
        console.print("[red]Erro: valor de compra deve ser maior que zero[/red]")
        raise typer.Exit(1)

    if not (1980 <= ano <= ano_atual + 1):
        console.print(f"[red]Erro: ano {ano} fora do intervalo [1980, {ano_atual + 1}][/red]")
        raise typer.Exit(1)

    try:
        data_compra = _parse_data(data)
    except ValueError as exc:
        console.print(f"[red]Erro na data de compra: {exc}[/red]")
        raise typer.Exit(1)

    try:
        dt_venda = _parse_data(data_venda)
    except ValueError as exc:
        console.print(f"[red]Erro na data de venda: {exc}[/red]")
        raise typer.Exit(1)

    csv_path = csv_override or (Path("data/historico") / f"{empresa}_arrematado.csv")

    nova_linha = {
        "marca": marca,
        "modelo": modelo,
        "ano": str(ano),
        "km": str(km) if km is not None else "",
        "valor_compra": str(valor),
        "data_compra": data_compra.strftime("%Y-%m-%d") if data_compra else "",
        "custos_extras": "",  # sempre decomposto em registrar-compra
        "valor_venda": str(valor_venda) if valor_venda is not None else "",
        "data_venda": dt_venda.strftime("%Y-%m-%d") if dt_venda else "",
        "taxa_leilao_real": str(taxa) if taxa is not None else "",
        "frete_real": str(frete) if frete is not None else "",
        "transferencia_real": str(transf) if transf is not None else "",
        "higienizacao_real": str(higi) if higi is not None else "",
        "outros_extras_real": str(outros) if outros is not None else "",
        "gastos_reforma_real": str(reforma) if reforma is not None else "",
        "observacoes": obs,
    }

    atualizado_csv = _upsert_csv_row(csv_path, nova_linha)

    row_obj = HistoricoRow(
        marca=marca,
        modelo=modelo,
        ano=ano,
        km=km,
        valor_compra=valor,
        data_compra=data_compra,
        taxa_leilao_real=taxa,
        frete_real=frete,
        transferencia_real=transf,
        higienizacao_real=higi,
        outros_extras_real=outros,
        gastos_reforma_real=reforma,
        valor_venda=valor_venda,
        data_venda=dt_venda,
        observacoes=obs,
    )

    init_db()
    with get_session() as session:
        result = importar_historico([row_obj], empresa, session)

    acao = "atualizado" if (atualizado_csv or result.atualizados > 0) else "criado"

    tbl = Table(title=f"Registro {acao} — {empresa}")
    tbl.add_column("Campo")
    tbl.add_column("Valor")
    tbl.add_row("Veículo", f"{marca} {modelo} {ano}")
    tbl.add_row("Valor compra", f"R$ {valor:,}")
    if data_compra:
        tbl.add_row("Data compra", data_compra.strftime("%d/%m/%Y"))
    if km is not None:
        tbl.add_row("KM", f"{km:,}")
    custos_desc = " | ".join(
        f"{label}: R$ {val:,}"
        for label, val in [
            ("Taxa", taxa), ("Frete", frete), ("Transf", transf),
            ("Higi", higi), ("Outros", outros), ("Reforma", reforma),
        ]
        if val is not None
    )
    if custos_desc:
        tbl.add_row("Custos extras", custos_desc)
    if valor_venda is not None:
        tbl.add_row("Valor venda", f"R$ {valor_venda:,}")
    if obs:
        tbl.add_row("Obs", obs)
    console.print(tbl)

    console.print(f"\n[green]✓ CSV:[/green] {csv_path}")
    if result.erros:
        console.print(f"[yellow]⚠ Aviso DB: {result.erros[0][1]}[/yellow]")
    else:
        console.print("[green]✓ DB sincronizado[/green]")

    console.print(
        "\n[dim]Próximos passos:[/dim]\n"
        "  • Rode [bold]carros-sa triagem[/bold] na próxima janela pra atualizar a planilha\n"
        f"  • Quando vender: [bold]carros-sa registrar-compra --empresa {empresa} "
        f"--marca {marca!r} --modelo {modelo!r} --ano {ano} --valor {valor} "
        "--valor-venda N --data-venda YYYY-MM-DD[/bold]"
    )


if __name__ == "__main__":
    app()
