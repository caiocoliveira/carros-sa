"""Exportador Google Sheets — escreve triagem de lotes numa Sheet compartilhada.

Uso típico:
    exporter = SheetsExporter(spreadsheet_id, credentials_path)
    n = exporter.exportar("uberlandia_mg", session)

Cada empresa_id vira uma aba separada na mesma Sheet. A aba é sobrescrita a cada
chamada (limpa + reescreve), então sempre reflete o estado atual do SQLite.

Setup one-time:
    1. Google Cloud → criar Service Account → baixar JSON (salvar FORA do repo)
    2. Criar Google Sheet → copiar ID da URL
    3. Compartilhar Sheet com o e-mail do service account (papel Editor)
    4. Setar no .env: GOOGLE_SHEETS_ID e GOOGLE_SERVICE_ACCOUNT_PATH
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlmodel import Session, select

from carros_sa.models import AvaliacaoLote, CategoriaVeiculo, LaudoCache, Lote
from carros_sa.scraping.parsers import is_laudo_pdf_url
from carros_sa.tenancy import carregar_empresa

HEADER = [
    "Rank",
    "Situação",
    "Modelo",
    "Ano",
    "Cidade",
    "Fim do Leilão",
    "KM",
    "Lance Atual (R$)",
    "Lance Máximo (R$)",
    "Lucro/mês (R$)",
    "ROI anualizado (%)",
    "Reforma (R$)",
    "Anúncio",
    "Laudo",
]

# Cabeçalho da aba de pendentes (lotes que caíram de fora da main por faltar
# laudo baixado/revisado/link). Coluna "Motivo" explica exatamente o que falta
# pro operador agir (abrir o anúncio manualmente ou aguardar próximo retry).
HEADER_PENDENTES = [
    "Rank",
    "Motivo",
    "Modelo",
    "Ano",
    "Cidade",
    "Fim do Leilão",
    "KM",
    "Lance Atual (R$)",
    "Anúncio",
    "Laudo",
]

# Mensagens humanas por motivo. Chaves fixas → testes batem strings específicas
# sem acoplar em implementação. Se o motivo mudar, os testes capturam via chave.
MOTIVOS_PENDENCIA = {
    "sem_url_e_extracao_falhou": (
        "Scraper não achou o link do laudo no modal e a extração falhou — "
        "abrir o anúncio manualmente pra inspecionar"
    ),
    "url_invalida_ou_decoy": (
        "URL do laudo ausente ou aponta pra decoy (Relatório de Transparência) — "
        "scraper não localizou o PDF real; retry diário vai tentar de novo"
    ),
    "extracao_falhou": (
        "URL do laudo ok, mas extração ficou abaixo do limiar de confiança — "
        "LLM indisponível ou PDF corrompido; retry diário vai tentar de novo"
    ),
}

# Formato numérico explícito por coluna. Necessário porque `ws.clear()` apaga
# valores mas PRESERVA formatação de célula — colunas cuja posição já foi
# ocupada por "Atualizado em" em versões anteriores do HEADER herdaram formato
# DATE, e inteiros (R$) passaram a ser renderizados como datas (número serial).
# Reaplicar NUMBER a cada export desfaz a contaminação histórica e blinda
# contra reordenações futuras.
_NUMBER_INTEIRO = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}
_NUMBER_DECIMAL_1 = {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}

COLUMN_FORMATS = {
    "KM": _NUMBER_INTEIRO,
    "Lance Atual (R$)": _NUMBER_INTEIRO,
    "Lance Máximo (R$)": _NUMBER_INTEIRO,
    "Lucro/mês (R$)": _NUMBER_INTEIRO,
    "Reforma (R$)": _NUMBER_INTEIRO,
    "ROI anualizado (%)": _NUMBER_DECIMAL_1,
}


def _col_letter(idx_0based: int) -> str:
    """Converte índice 0-based em letra de coluna estilo Sheets (A, B, ..., Z, AA, AB, ...)."""
    letters = ""
    idx = idx_0based
    while True:
        letters = chr(ord("A") + idx % 26) + letters
        idx = idx // 26 - 1
        if idx < 0:
            return letters


def _classificar_pendencia(*, laudo_analisado: bool, laudo_url_valida: bool) -> Optional[str]:
    """Retorna chave em MOTIVOS_PENDENCIA se lote falta algum requisito pra main
    sheet, ou None se está completo (laudo baixado+revisado E link válido).

    Separado do `_query` pra permitir teste direto dos quatro quadrantes
    (2x2: laudo_analisado × laudo_url_valida).
    """
    if laudo_analisado and laudo_url_valida:
        return None
    if not laudo_analisado and not laudo_url_valida:
        return "sem_url_e_extracao_falhou"
    if not laudo_url_valida:
        return "url_invalida_ou_decoy"
    return "extracao_falhou"


def _calcular_roi_no_maximo(av: AvaliacaoLote) -> float:
    """ROI garantido se ganhar o lote exatamente pelo lance máximo.

    = (preco_giro - capital_total) / capital_total
    onde capital_total = preco_max + reforma + frete + taxas (8% do max) + custo_op
    """
    if av.preco_max <= 0:
        return 0.0
    capital = av.preco_max + av.reforma_estimada + av.frete_incluso + av.taxas_leilao
    lucro = av.preco_giro - capital
    return round(lucro / max(capital, 1) * 100, 1)


class SheetsExporter:
    """Exporta avaliações do SQLite para uma aba no Google Sheets."""

    def __init__(self, spreadsheet_id: str, credentials_path: str) -> None:
        self._spreadsheet_id = spreadsheet_id
        self._credentials_path = credentials_path
        self._gc = None  # lazy init para não quebrar import sem credenciais

    def _client(self):
        if self._gc is None:
            import gspread
            self._gc = gspread.service_account(filename=self._credentials_path)
        return self._gc

    def exportar(self, empresa_id: str, session: Session) -> int:
        """Lê SQLite e escreve três abas: main (<empresa_id>), pendentes, cidades, Glossário.

        A aba principal só contém lotes "completos" — com laudo baixado,
        revisado (LaudoCache.confidence >= 0.6) e link válido (URL passa
        `is_laudo_pdf_url`). Lotes faltando qualquer uma dessas três coisas
        vão pra aba `<empresa_id>_pendentes` com uma coluna "Motivo" explicando
        o que está faltando. Assim o operador vê só aquilo em que pode agir
        no fluxo principal e mantém visibilidade do backlog em aba separada.

        Retorna n linhas EXPORTADAS NA ABA PRINCIPAL.
        """
        rows = self._query(empresa_id, session)
        # Filtro duro: lote encerrado (timer vencido OU badge ARREMATADO) é
        # ruído — operador não pode mais dar lance. Antes empurrávamos pro
        # final; agora removemos completamente.
        rows_ativos = [r for r in rows if not r["encerrado"]]

        # Particiona: "ok" = laudo baixado+revisado+link válido; resto vai
        # pra aba pendentes com o motivo específico.
        rows_ok = [r for r in rows_ativos if r["motivo_pendencia"] is None]
        rows_pendentes = [r for r in rows_ativos if r["motivo_pendencia"] is not None]

        rows_sorted = sorted(
            rows_ok,
            key=lambda r: (
                0 if r["viavel"] else 1,            # viáveis antes dos inviáveis
                -(r["preco_max"] - r["lance_atual"]),  # maior folga primeiro
            ),
        )
        # Pendentes ordenados por urgência (fim_em mais próximo primeiro) pra
        # o operador priorizar retry manual nos lotes que mais estão pra fechar.
        rows_pendentes_sorted = sorted(
            rows_pendentes,
            key=lambda r: r["fim_em_raw"] or datetime.max,
        )

        self._write_sheet(empresa_id, rows_sorted)
        self._write_pendentes_sheet(empresa_id, rows_pendentes_sorted)
        self._write_cidades_frete_sheet(empresa_id, session)
        self._write_glossario_sheet()

        # GUARD DE INVARIANTE: por construção, nenhum lote em `rows_ok` pode
        # ter motivo_pendencia não-None. Se esse assert disparar em produção,
        # é sinal de que alguém mudou `_classificar_pendencia` sem atualizar
        # o filtro em `exportar`. O teste `test_main_sheet_nunca_tem_lote_pendente`
        # em tests/test_exportar_sheets.py cobre o mesmo invariante em CI.
        assert all(r["motivo_pendencia"] is None for r in rows_sorted), (
            "main sheet tem lote pendente — invariante 'laudo baixado+revisado+link' quebrado"
        )

        return len(rows_sorted)

    def _query(self, empresa_id: str, session: Session) -> List[dict]:
        """JOIN lote + avaliacao_lote + laudo (LEFT JOIN — laudo pode não existir)."""
        avaliacoes = session.exec(
            select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa_id)
        ).all()

        agora = datetime.now()
        rows: List[dict] = []
        for av in avaliacoes:
            lote = session.get(Lote, av.lote_id)
            if lote is None:
                continue

            # Filtro duro: lote sem Fim do Leilão visível não entra na planilha.
            # Observação empírica (feedback usuário 2026-04-16): 100% dos lotes
            # com `fim_em=None` coletados em triagens anteriores já estavam
            # arrematados/vendidos OU o link do anúncio não abria mais. Auto
            # Avaliar só mostra countdown enquanto o lote está em leilão ativo;
            # sumiu o countdown = já saiu do leilão. Melhor esconder do que
            # mandar o usuário clicar em link morto.
            if lote.fim_em is None:
                continue

            laudo: Optional[LaudoCache] = session.get(LaudoCache, av.lote_id)

            try:
                fim_em_str = lote.fim_em.strftime("%d/%m/%Y %H:%M")
            except Exception:
                fim_em_str = str(lote.fim_em)

            viavel = av.preco_max > (lote.lance_atual or 0)

            from carros_sa.agents.calibracao_giro import (
                lucro_reais_por_mes, roi_anualizado,
            )
            roi_max = _calcular_roi_no_maximo(av)
            roi_anual = roi_anualizado(roi_max / 100.0, av.dias_giro_estimado) * 100
            # Lucro esperado / mês — métrica intuitiva pro operador:
            # "esse lote rende R$X/mês enquanto no pátio". Baseado em
            # score_roi × preco_alvo (lucro no caso médio do bid).
            lucro_mes = lucro_reais_por_mes(
                int(av.score_roi * av.preco_alvo), av.dias_giro_estimado,
            )

            # Encerrado = badge "ARREMATADO" visto no detalhe OU timer já passou.
            # Dupla checagem pra cobrir os dois vetores (snapshot velho + detecção
            # direta no HTML da próxima coleta).
            detalhe_raw = (lote.raw_json or {}).get("detalhe") or {}
            encerrado_por_badge = bool(detalhe_raw.get("encerrado"))
            encerrado_por_timer = (
                lote.fim_em is not None and lote.fim_em < agora
            )
            encerrado = encerrado_por_badge or encerrado_por_timer

            # URL do PDF do laudo — filtra decoys (Transparência, listing) que
            # ainda podem estar em `raw_json` de coletas antigas. Só exibimos
            # link clicável se a URL passa pelo `is_laudo_pdf_url`.
            laudo_url_raw = detalhe_raw.get("laudo_pdf_url")
            laudo_url = laudo_url_raw if is_laudo_pdf_url(laudo_url_raw) else None

            # Laudo só conta como "analisado" se veio de um PDF real — fallback
            # `_laudo_sem_pdf` (sem avarias, "não identificou nada") grava
            # confidence <= 0.55. Sem essa distinção o usuário via reforma de
            # R$ 1.000 (piso) em 63/68 lotes e ia achando que o carro "tava ok"
            # quando na verdade ninguém leu o laudo. Regra do usuário: não dar
            # fallback de valor, avisar explicitamente que não foi analisado.
            laudo_analisado = bool(laudo and (laudo.confidence or 0) >= 0.6)

            # Classifica: invariante do sheet principal é "tem laudo baixado,
            # revisado E link visível". `motivo_pendencia=None` = lote completo.
            motivo_pendencia = _classificar_pendencia(
                laudo_analisado=laudo_analisado, laudo_url_valida=bool(laudo_url),
            )

            rows.append({
                "lote_id": av.lote_id,
                "modelo": f"{lote.marca} {lote.modelo}",
                "ano": lote.ano,
                "cidade": lote.origem_cidade or "—",
                "fim_em": fim_em_str,
                "fim_em_raw": lote.fim_em,  # pra ordenar pendentes por urgência
                "km": lote.km,
                "lance_atual": lote.lance_atual or 0,
                "preco_max": av.preco_max,
                "roi_anualizado": round(roi_anual, 1),
                "lucro_mes": lucro_mes,
                "reforma_estimada": av.reforma_estimada,
                "url": lote.url,
                "laudo_url": laudo_url,
                "viavel": viavel,
                "encerrado": encerrado,
                "laudo_analisado": laudo_analisado,
                "motivo_pendencia": motivo_pendencia,
            })
        return rows

    def _write_sheet(self, empresa_id: str, rows: List[dict]) -> None:
        """Abre/cria aba, limpa, escreve timestamp global + header + rows."""
        gc = self._client()
        sh = gc.open_by_key(self._spreadsheet_id)

        # Abre ou cria a aba com nome = empresa_id
        try:
            ws = sh.worksheet(empresa_id)
        except Exception:
            ws = sh.add_worksheet(title=empresa_id, rows=500, cols=len(HEADER))

        ts = datetime.now().strftime("%d/%m/%Y %H:%M")

        # Linha 1: banner global "Última atualização" — deixa óbvio o quão fresco
        # está o snapshot. Preenche a primeira célula e mantém o resto vazio pra
        # não poluir o layout; o freeze(rows=2) congela tanto o banner quanto o
        # header de colunas.
        banner = [f"Última atualização da planilha: {ts}"] + [""] * (len(HEADER) - 1)

        sheet_rows = [banner, HEADER]
        for rank, r in enumerate(rows, start=1):
            # Garante de contrato: quem chega aqui JÁ passou pelo filtro de
            # completude em `exportar()`, então `laudo_analisado` é True e
            # `laudo_url` é válida. Os branches de "⚠ LAUDO NÃO ANALISADO"
            # + numéricos "—" foram removidos — lotes incompletos vão pra
            # aba `<empresa_id>_pendentes` com coluna Motivo explicando.
            situacao = "✓ Viável" if r["viavel"] else "✗ Caro demais"

            # URL → HYPERLINK clicável com label curto ("Abrir anúncio").
            if r["url"]:
                url_escaped = r["url"].replace('"', '""')
                url_cell = f'=HYPERLINK("{url_escaped}"; "Abrir anúncio")'
            else:
                url_cell = "—"
            # Link pro PDF do laudo — por invariante da main, `laudo_url` não é None.
            laudo_escaped = r["laudo_url"].replace('"', '""')
            laudo_cell = f'=HYPERLINK("{laudo_escaped}"; "Ver laudo")'

            sheet_rows.append([
                rank,
                situacao,
                r["modelo"],
                r["ano"],
                r["cidade"],
                r["fim_em"],
                r["km"] if r["km"] is not None else "—",
                r["lance_atual"],
                r["preco_max"],
                r["lucro_mes"],
                r["roi_anualizado"],
                r["reforma_estimada"],
                url_cell,
                laudo_cell,
            ])

        ws.clear()
        self._reaplicar_formato_numerico(ws)
        ws.update(sheet_rows, value_input_option="USER_ENTERED")

        # Congela banner (linha 1) + header (linha 2)
        ws.freeze(rows=2)

    def _write_pendentes_sheet(self, empresa_id: str, rows: List[dict]) -> None:
        """Aba `<empresa_id>_pendentes` — lotes que falharam o invariante da main.

        Cada linha tem coluna "Motivo" descrevendo o que falta (sem URL, extração
        falhou, ambos). Operador usa essa aba pra abrir o anúncio manualmente e
        resolver (conferir laudo no site, dar retry forçado, ou descartar o lote
        se o grupo não expõe laudo de jeito nenhum). Retry diário do cron
        (`reprocessar_lotes_do_db.py --somente-laudo-pendente`) tenta mover
        lotes daqui pra main automaticamente.

        A aba SEMPRE é (re)escrita — inclusive quando rows está vazia, pra que
        o operador veja banner limpo indicando "sem pendências".
        """
        gc = self._client()
        sh = gc.open_by_key(self._spreadsheet_id)
        aba_nome = f"{empresa_id}_pendentes"
        try:
            ws = sh.worksheet(aba_nome)
        except Exception:
            ws = sh.add_worksheet(
                title=aba_nome, rows=max(len(rows) + 10, 100), cols=len(HEADER_PENDENTES),
            )

        ts = datetime.now().strftime("%d/%m/%Y %H:%M")
        if rows:
            subtitulo = f"{len(rows)} lote(s) sem laudo baixado/revisado/link — atualizado {ts}"
        else:
            subtitulo = f"Sem pendências — todos os lotes ativos têm laudo completo ({ts})"
        banner = [subtitulo] + [""] * (len(HEADER_PENDENTES) - 1)

        sheet_rows = [banner, HEADER_PENDENTES]
        for rank, r in enumerate(rows, start=1):
            if r["url"]:
                url_escaped = r["url"].replace('"', '""')
                url_cell = f'=HYPERLINK("{url_escaped}"; "Abrir anúncio")'
            else:
                url_cell = "—"
            # Link do laudo pode existir (extracao_falhou) ou não — respeita o
            # que veio filtrado pelo `is_laudo_pdf_url` lá no _query.
            if r["laudo_url"]:
                laudo_escaped = r["laudo_url"].replace('"', '""')
                laudo_cell = f'=HYPERLINK("{laudo_escaped}"; "Ver laudo")'
            else:
                laudo_cell = "—"

            motivo_humano = MOTIVOS_PENDENCIA.get(r["motivo_pendencia"], r["motivo_pendencia"])
            sheet_rows.append([
                rank,
                motivo_humano,
                r["modelo"],
                r["ano"],
                r["cidade"],
                r["fim_em"],
                r["km"] if r["km"] is not None else "—",
                r["lance_atual"],
                url_cell,
                laudo_cell,
            ])

        ws.clear()
        ws.update(sheet_rows, value_input_option="USER_ENTERED")
        ws.freeze(rows=2)

    @staticmethod
    def _reaplicar_formato_numerico(ws) -> None:
        """Reaplica NUMBER em colunas numéricas.

        `ws.clear()` preserva formato de célula, então colunas cuja posição já
        foi ocupada por um timestamp ("Atualizado em") em versões passadas do
        HEADER continuam marcadas como DATE. Sem este reset, inteiros R$ são
        renderizados como datas (ex.: 5000 → 1913-09-07).
        """
        formats = []
        for col_name, cell_format in COLUMN_FORMATS.items():
            if col_name not in HEADER:
                continue
            letter = _col_letter(HEADER.index(col_name))
            formats.append({"range": f"{letter}:{letter}", "format": cell_format})
        if formats:
            ws.batch_format(formats)

    def _write_cidades_frete_sheet(self, empresa_id: str, session: Session) -> None:
        """Aba 'cidades_<empresa_id>' com cidades do raio operacional + frete por categoria.

        Mostra as cidades onde o scraper procura lotes (haversine ≤ raio_operacao_km
        do pátio), com o frete por categoria de veículo e a contagem de lotes ativos
        (fim_em > now()) já no DB pra cada cidade. Ajuda o operador a entender de
        onde está vindo o inventário e qual o custo logístico real.
        """
        try:
            empresa = carregar_empresa(empresa_id)
        except FileNotFoundError:
            # Sem YAML da empresa não há como derivar raio nem tabela de frete.
            # Pula a aba silenciosamente (caminho atingido apenas em testes
            # que mockam o gspread sem provisionar config).
            return
        cidades = empresa.cidades_de_busca()

        # Conta lotes ativos por (origem_cidade, origem_uf) — case/accent-insensitive
        # via `_normaliza` espelhando o helper de geo.py. Usado pra colar na linha
        # da cidade certa mesmo se o Auto Avaliar gravar "São Gotardo" e o IBGE
        # tiver "Sao Gotardo".
        from carros_sa.tools.geo import _normaliza
        agora = datetime.now()
        lotes_ativos = session.exec(
            select(Lote).where(Lote.fim_em > agora)
        ).all()
        contagem: dict = {}
        for lote in lotes_ativos:
            if not lote.origem_cidade or not lote.origem_uf:
                continue
            chave = (_normaliza(lote.origem_cidade), lote.origem_uf.strip().upper())
            contagem[chave] = contagem.get(chave, 0) + 1

        header = [
            "Cidade", "UF", "Distância (km)",
            "Frete Hatch (R$)", "Frete Sedan (R$)", "Frete SUV (R$)",
            "Frete Picape (R$)", "Frete Utilitário (R$)", "Frete Outro (R$)",
            "Lotes ativos no DB",
        ]

        body = []
        for m in cidades:
            d_km = int(round(m.distancia_do_ponto_km))
            qtd = contagem.get((_normaliza(m.nome), m.uf), 0)
            body.append([
                m.nome, m.uf, d_km,
                empresa.frete_para(d_km, CategoriaVeiculo.HATCH),
                empresa.frete_para(d_km, CategoriaVeiculo.SEDAN),
                empresa.frete_para(d_km, CategoriaVeiculo.SUV),
                empresa.frete_para(d_km, CategoriaVeiculo.PICAPE),
                empresa.frete_para(d_km, CategoriaVeiculo.UTILITARIO),
                empresa.frete_para(d_km, CategoriaVeiculo.OUTRO),
                qtd,
            ])

        ts = datetime.now().strftime("%d/%m/%Y %H:%M")
        banner = [
            f"Cidades no raio de {empresa.raio_operacao_km}km do pátio "
            f"({empresa.patio.cidade}/{empresa.patio.uf}) — atualizado {ts}"
        ] + [""] * (len(header) - 1)

        sheet_rows = [banner, header] + body

        gc = self._client()
        sh = gc.open_by_key(self._spreadsheet_id)
        aba_nome = f"cidades_{empresa_id}"
        try:
            ws = sh.worksheet(aba_nome)
        except Exception:
            ws = sh.add_worksheet(title=aba_nome, rows=max(len(sheet_rows) + 10, 100), cols=len(header))

        ws.clear()
        ws.update(sheet_rows, value_input_option="USER_ENTERED")
        ws.freeze(rows=2)

    def _write_glossario_sheet(self) -> None:
        """Aba fixa 'Glossário' com origem, cálculo e racional de cada coluna da triagem.

        Escrita toda vez que o exportador roda pra garantir que a doc não sai
        de sincronia com o código. Usa sempre o mesmo nome de aba ('Glossário'),
        então multi-tenant compartilha essa doc.
        """
        gc = self._client()
        sh = gc.open_by_key(self._spreadsheet_id)
        aba_nome = "Glossário"
        try:
            ws = sh.worksheet(aba_nome)
        except Exception:
            ws = sh.add_worksheet(title=aba_nome, rows=60, cols=4)

        glossario = [
            ["Campo", "Origem", "Cálculo / Fórmula", "Racional"],
            [
                "Rank",
                "Derivado",
                "Posição após ordenar viáveis primeiro, maior folga (max − atual) primeiro",
                "Lotes que cabem no nosso teto e com mais espaço de negociação sobem",
            ],
            [
                "Situação",
                "Derivado",
                "✓ Viável se Lance Máximo > Lance Atual, senão ✗ Caro demais. Lotes encerrados (badge ARREMATADO ou Fim do Leilão passado) são filtrados antes do export. Lotes sem laudo baixado+revisado+link válido são movidos pra aba '<empresa>_pendentes' com coluna Motivo — NÃO entram na aba principal.",
                "Resumo de uma célula do que o operador pode/deve fazer. Aba principal é invariante: todo lote aqui tem laudo completo e link clicável. Pra acompanhar lotes sem laudo, olhar a aba de pendentes.",
            ],
            [
                "Modelo",
                "Auto Avaliar (listagem)",
                "Marca + modelo extraídos do card via regex (ano fica em coluna separada)",
                "Identificação humana do veículo",
            ],
            [
                "Ano",
                "Auto Avaliar (listagem)",
                "Ano-modelo do veículo extraído do card",
                "Âncora de depreciação FIPE; separado da coluna Modelo pra facilitar filtro/ordenação",
            ],
            [
                "Cidade",
                "Auto Avaliar (detalhe)",
                "Campo origem_cidade do lote (onde o carro está pra retirada); '—' se não informado",
                "Sanity logístico — cidades distantes do pátio entram com frete maior embutido no Lance Máximo",
            ],
            [
                "Fim do Leilão",
                "Auto Avaliar",
                "Timer HH:MM:SS[:centésimos] do card convertido pra datetime absoluto. Lotes SEM fim visível são filtrados do export (Auto Avaliar só mostra countdown enquanto o lote está ativo).",
                "Urgência — lotes com fim próximo precisam de decisão antes",
            ],
            [
                "KM",
                "Auto Avaliar",
                "Número parseado da página de detalhe (specs)",
                "Sanity check rápido de desgaste (KM alto = revenda mais difícil)",
            ],
            [
                "Lance Atual (R$)",
                "Auto Avaliar",
                "Maior lance no momento da raspagem (campo 'ULTIMA AVALIAÇÃO' da plataforma)",
                "Piso que precisamos cobrir pra entrar no leilão",
            ],
            [
                "Lance Máximo (R$)",
                "Precificador",
                "(preco_giro − reforma − frete − custo_op − margem_min×giro) ÷ (1 + taxa_leilão). Equação resolve circularidade da taxa de ~8% cobrada sobre o próprio lance vencedor. Já embute reforma, frete, FIPE/Webmotors e fator de risco do laudo.",
                "Teto ABSOLUTO — acima disso a margem mínima da empresa não é respeitada nem no melhor cenário",
            ],
            [
                "Lucro/mês (R$)",
                "Derivado",
                "lucro_absoluto (score_roi × preco_alvo) × 30 ÷ dias_giro (floor 30d; fallback 90d quando dias_giro=NULL)",
                "Métrica intuitiva: 'esse lote rende R$X/mês enquanto fica no pátio'. Permite comparar lotes de capitais e prazos diferentes na mesma unidade.",
            ],
            [
                "ROI anualizado (%)",
                "Derivado",
                "ROI no máximo × 365 / dias_giro (floor 30d; fallback 90d). ROI no máximo = (preco_giro − capital_total) ÷ capital_total, com capital_total = lance_max + reforma + frete + taxas(~8%) + custo_op.",
                "Normaliza o retorno pelo tempo de giro — carro rápido com ROI menor pode ganhar de carro lento com ROI maior",
            ],
            [
                "Reforma (R$)",
                "EstimadorReformaLLM (fallback: tabela YAML)",
                "Custo total dos itens retornados pelo LLM a partir do laudo; se LLM falhar, soma da tabela (família_peça × severidade) + adicional estrutural quando aplicável. Já descontado do Lance Máximo.",
                "Custo ANTES de vender. Números grandes aqui = lote com dano material relevante; confirmar no PDF do laudo antes do lance.",
            ],
            [
                "Anúncio",
                "Auto Avaliar",
                "=HYPERLINK do link direto do anúncio no b2b.autoavaliar.com.br, rotulado 'Abrir anúncio'",
                "1 clique pra abrir o anúncio original e fazer checagens manuais ou lance",
            ],
            [
                "Laudo",
                "Scraper detalhe",
                "=HYPERLINK pro PDF do laudo cautelar do lote, rotulado 'Ver laudo'. Na aba principal SEMPRE tem link (invariante — lotes sem link vão pra '<empresa>_pendentes').",
                "Evidência material pra confirmar avarias antes do lance. Se clicar e der 404/expirado, retry diário vai revalidar na próxima coleta.",
            ],
            [
                "Aba '<empresa>_pendentes'",
                "Derivado",
                "Lotes ativos sem laudo baixado+revisado+link válido. Coluna Motivo explica o que falta: (a) scraper não achou o link no modal, (b) URL era decoy filtrado, (c) extração LLM ficou abaixo de confidence 0.6.",
                "Operador usa pra decidir se abre o anúncio manualmente, força retry, ou descarta. Retry do cron (2x/dia) tenta mover lotes daqui pra main automaticamente.",
            ],
        ]

        ws.clear()
        ws.update(glossario, value_input_option="USER_ENTERED")
        ws.freeze(rows=1)

    @property
    def sheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self._spreadsheet_id}"
