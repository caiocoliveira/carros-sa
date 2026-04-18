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

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.scraping.parsers import is_laudo_pdf_url

HEADER = [
    "Rank",
    "Situação",
    "Lote ID",
    "Modelo",
    "Ano",
    "Cidade",
    "Fim do Leilão",
    "KM",
    "Lance Atual (R$)",
    "Lance Máximo (R$)",
    "FIPE (R$)",
    "Preço Giro FIPE (R$)",
    "Preço Giro Auto Avaliar (R$)",
    "FIPE % (lance min)",
    "ROI se pagar o máximo (%)",
    "Dias até venda (est.)",
    "ROI anualizado (%)",
    "Popularidade",
    "Fator Risco",
    "Severidade Laudo",
    "Motor OK",
    "Reforma Estimada (R$)",
    "Racional Reforma",
    "Frete (R$)",
    "Justificativa",
    "URL",
    "Laudo (PDF)",
    "Coletado em",
]

# Formato numérico explícito por coluna. Necessário porque `ws.clear()` apaga
# valores mas PRESERVA formatação de célula — colunas cuja posição já foi
# ocupada por "Atualizado em" em versões anteriores do HEADER herdaram formato
# DATE, e inteiros (R$) passaram a ser renderizados como datas (número serial).
# Reaplicar NUMBER a cada export desfaz a contaminação histórica e blinda
# contra reordenações futuras.
_NUMBER_INTEIRO = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}
_NUMBER_DECIMAL_1 = {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}
_NUMBER_DECIMAL_3 = {"numberFormat": {"type": "NUMBER", "pattern": "0.000"}}

COLUMN_FORMATS = {
    "KM": _NUMBER_INTEIRO,
    "Lance Atual (R$)": _NUMBER_INTEIRO,
    "Lance Máximo (R$)": _NUMBER_INTEIRO,
    "FIPE (R$)": _NUMBER_INTEIRO,
    "Preço Giro FIPE (R$)": _NUMBER_INTEIRO,
    "Preço Giro Auto Avaliar (R$)": _NUMBER_INTEIRO,
    "Reforma Estimada (R$)": _NUMBER_INTEIRO,
    "Frete (R$)": _NUMBER_INTEIRO,
    "ROI se pagar o máximo (%)": _NUMBER_DECIMAL_1,
    "Fator Risco": _NUMBER_DECIMAL_3,
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
        """Lê SQLite, escreve aba <empresa_id> + aba Glossário na Sheet. Retorna n linhas exportadas."""
        rows = self._query(empresa_id, session)
        # Filtro duro: lote encerrado (timer vencido OU badge ARREMATADO) é
        # ruído — operador não pode mais dar lance. Antes empurrávamos pro
        # final; agora removemos completamente.
        rows_ativos = [r for r in rows if not r["encerrado"]]
        rows_sorted = sorted(
            rows_ativos,
            key=lambda r: (
                0 if r["viavel"] else 1,        # viáveis antes dos inviáveis
                -(r["preco_max"] - r["lance_atual"]),  # maior folga primeiro
            ),
        )
        self._write_sheet(empresa_id, rows_sorted)
        self._write_glossario_sheet()
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
                _categoria_de_modelo, roi_anualizado,
            )
            from carros_sa.tools.popularidade import bucket_modelo
            roi_max = _calcular_roi_no_maximo(av)
            roi_anual = roi_anualizado(roi_max / 100.0, av.dias_giro_estimado) * 100
            cat_inferida = _categoria_de_modelo(lote.modelo)
            bucket = bucket_modelo(lote.marca, lote.modelo, cat_inferida, ano=lote.ano)

            # Encerrado = badge "ARREMATADO" visto no detalhe OU timer já passou.
            # Dupla checagem pra cobrir os dois vetores (snapshot velho + detecção
            # direta no HTML da próxima coleta).
            detalhe_raw = (lote.raw_json or {}).get("detalhe") or {}
            encerrado_por_badge = bool(detalhe_raw.get("encerrado"))
            encerrado_por_timer = (
                lote.fim_em is not None and lote.fim_em < agora
            )
            encerrado = encerrado_por_badge or encerrado_por_timer

            scraped_at_str = "—"
            if lote.scraped_at is not None:
                try:
                    scraped_at_str = lote.scraped_at.strftime("%d/%m/%Y %H:%M")
                except Exception:
                    scraped_at_str = str(lote.scraped_at)

            # URL do PDF do laudo — filtra decoys (Transparência, listing) que
            # ainda podem estar em `raw_json` de coletas antigas. Só exibimos
            # link clicável se a URL passa pelo `is_laudo_pdf_url`.
            laudo_url_raw = detalhe_raw.get("laudo_pdf_url")
            laudo_url = laudo_url_raw if is_laudo_pdf_url(laudo_url_raw) else None

            rows.append({
                "lote_id": av.lote_id,
                "modelo": f"{lote.marca} {lote.modelo}",
                "ano": lote.ano,
                "cidade": lote.origem_cidade or "—",
                "fim_em": fim_em_str,
                "km": lote.km,
                "lance_atual": lote.lance_atual or 0,
                "preco_max": av.preco_max,
                "fipe": av.fipe,
                "preco_giro_fipe": av.preco_giro_fipe,
                "preco_giro_aa": av.preco_giro_aa,
                "fipe_pct_lance_minimo": lote.fipe_pct_lance_minimo,
                "roi_pct": roi_max,
                "dias_giro": av.dias_giro_estimado,
                "roi_anualizado": round(roi_anual, 1),
                "popularidade": bucket.value,
                "fator_risco": round(av.fator_risco, 3),
                "severidade": laudo.severidade_geral if laudo else "—",
                "motor_ok": ("Sim" if laudo.motor_ok else "NÃO") if laudo else "—",
                "reforma_estimada": av.reforma_estimada,
                "reforma_racional": av.reforma_racional,
                "frete": av.frete_incluso,
                "justificativa": av.justificativa,
                "url": lote.url,
                "laudo_url": laudo_url,
                "viavel": viavel,
                "encerrado": encerrado,
                "scraped_at": scraped_at_str,
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
            situacao = "✓ Viável" if r["viavel"] else "✗ Caro demais"
            # URL → HYPERLINK clicável com label curto ("Abrir anúncio")
            # em vez da URL crua longa. Sheets interpreta com USER_ENTERED.
            if r["url"]:
                url_escaped = r["url"].replace('"', '""')
                url_cell = f'=HYPERLINK("{url_escaped}"; "Abrir anúncio")'
            else:
                url_cell = "—"
            # Link pro PDF do laudo. Se URL passou pelo filtro de decoy
            # (is_laudo_pdf_url), vira HYPERLINK "Ver laudo"; se não, "—".
            # Motivação: usuário quer conferir o laudo antes de dar lance
            # sem precisar clicar no anúncio, abrir o modal e esperar carregar.
            if r["laudo_url"]:
                laudo_escaped = r["laudo_url"].replace('"', '""')
                laudo_cell = f'=HYPERLINK("{laudo_escaped}"; "Ver laudo")'
            else:
                laudo_cell = "—"
            sheet_rows.append([
                rank,
                situacao,
                r["lote_id"],
                r["modelo"],
                r["ano"],
                r["cidade"],
                r["fim_em"],
                r["km"] if r["km"] is not None else "—",
                r["lance_atual"],
                r["preco_max"],
                r["fipe"] if r["fipe"] is not None else "—",
                r["preco_giro_fipe"],
                r["preco_giro_aa"] if r["preco_giro_aa"] is not None else "—",
                f"{r['fipe_pct_lance_minimo']}%" if r["fipe_pct_lance_minimo"] is not None else "—",
                r["roi_pct"],
                r["dias_giro"] if r["dias_giro"] is not None else "—",
                r["roi_anualizado"],
                r["popularidade"],
                r["fator_risco"],
                r["severidade"],
                r["motor_ok"],
                r["reforma_estimada"],
                r["reforma_racional"] or "—",
                r["frete"],
                r["justificativa"],
                url_cell,
                laudo_cell,
                r["scraped_at"],
            ])

        ws.clear()
        self._reaplicar_formato_numerico(ws)
        ws.update(sheet_rows, value_input_option="USER_ENTERED")

        # Congela banner (linha 1) + header (linha 2)
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
                "✓ Viável se Lance Máximo > Lance Atual, senão ✗ Caro demais. Lotes encerrados (badge ARREMATADO ou Fim do Leilão já passou) são FILTRADOS antes do export — não aparecem na aba.",
                "Planilha só mostra lotes onde ainda dá pra dar lance. Dentro dos ativos, se o mínimo já passou do teto não há lance a fazer.",
            ],
            [
                "Lote ID",
                "Auto Avaliar",
                "Segmento numérico da URL /avaliacoes/<empresa>/<ID>/<slug>",
                "Chave estável pra deduplicar e reabrir o anúncio",
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
                "Âncora de depreciação FIPE e pro bucket de popularidade; separado da coluna Modelo pra facilitar filtro/ordenação",
            ],
            [
                "Cidade",
                "Auto Avaliar (detalhe)",
                "Campo origem_cidade do lote (onde o carro está pra retirada); '—' se não informado",
                "Input de frete e logística — cidades distantes do pátio aumentam custo de trazer o carro",
            ],
            [
                "Fim do Leilão",
                "Auto Avaliar",
                "Timer HH:MM:SS[:centésimos] do card convertido pra datetime absoluto. Lotes SEM fim visível são filtrados do export (Auto Avaliar só mostra countdown enquanto o lote está em leilão ativo; sem countdown = já arrematado ou link morto).",
                "Urgência — lotes com fim próximo precisam de decisão antes. Ausência da coluna significa que o lote deixou de ser visível no leilão.",
            ],
            [
                "KM",
                "Auto Avaliar",
                "Número parseado da página de detalhe (specs)",
                "Input pro giro (KM alto = revenda mais difícil)",
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
                "(preco_giro − reforma − frete − custo_op − margem_min×giro) ÷ (1 + taxa_leilão). Equação resolve circularidade: taxa é cobrada sobre o próprio lance vencedor (~8%).",
                "Teto ABSOLUTO — acima disso a margem mínima da empresa não é respeitada mesmo no melhor cenário",
            ],
            [
                "FIPE (R$)",
                "API FIPE Parallelum",
                "Valor da tabela FIPE pro modelo+ano, R$ bruto",
                "Âncora pública de preço de varejo — linha de base nacional",
            ],
            [
                "Preço Giro FIPE (R$)",
                "Precificador",
                "min(FIPE × 0.95, Webmotors p25). Corta 5% da FIPE pra aproximar de atacado e limita pelo p25 (25% mais baratos) do Webmotors.",
                "Preço de venda conservador ancorado em FIPE — linha de base se não tiver dado Auto Avaliar",
            ],
            [
                "Preço Giro Auto Avaliar (R$)",
                "Precificador",
                "min(Auto Avaliar ref, Webmotors p25). 'Auto Avaliar ref' vem da 'ULTIMA AVALIAÇÃO' do anúncio (tabela do próprio marketplace de atacado).",
                "Preço atacado real — geralmente mais baixo que FIPE. Quando presente, ganha peso no preco_giro final (menor entre os dois)",
            ],
            [
                "FIPE % (lance min)",
                "Auto Avaliar",
                "Atributo .tag-percent-value no DOM do anúncio (badge na foto)",
                "Sinal do vendedor sobre valor relativo: % baixo = modelo desvalorizado, alto = perto da FIPE",
            ],
            [
                "ROI se pagar o máximo (%)",
                "Derivado",
                "(preco_giro − capital_total) ÷ capital_total × 100, onde capital_total = lance_max + reforma + frete + taxas(~8% do max) + custo_op",
                "Retorno PERCENTUAL no pior cenário aceitável (se ganhar o lote pagando exatamente o teto)",
            ],
            [
                "Dias até venda (est.)",
                "CalibracaoGiro / AvaliacaoLote.dias_giro_estimado",
                "Média histórica de dias_até_venda por categoria do veículo, calibrada a partir da tabela Arrematado. Fallback pra prior hardcoded quando há <3 vendas da categoria.",
                "Input do ROI anualizado — quantos dias em média demora pra girar esse tipo de carro na empresa",
            ],
            [
                "ROI anualizado (%)",
                "Derivado",
                "(1 + ROI)^(365/dias_giro) − 1, com floor de 30 dias. Usa 90d se dias_giro_estimado é NULL.",
                "Normaliza ROI absoluto pelo tempo de giro — carro rápido com ROI menor pode ganhar de carro lento com ROI maior",
            ],
            [
                "Fator Risco",
                "Precificador",
                "bounds.lo + (bounds.hi − bounds.lo) × peso, onde peso = severidade_laudo + documentação + motor + (1 − confidence laudo)",
                "Multiplica a margem exigida. Lote mais arriscado → margem maior → lance mais baixo",
            ],
            [
                "Severidade Laudo",
                "ExtratorLaudo",
                "Classificação do laudo em nenhuma / leve / média / grave / estrutural, via análise do PDF + fotos com LLM",
                "Sinal primário de risco — 'estrutural' é descartado automaticamente no scraper",
            ],
            [
                "Motor OK",
                "ExtratorLaudo",
                "Bool do campo motor_ok do laudo estruturado",
                "Motor comprometido destrói margem de revenda; penaliza fator_risco forte",
            ],
            [
                "Reforma Estimada (R$)",
                "EstimadorReformaLLM (fallback: tabela YAML)",
                "Custo total dos itens retornados pelo LLM; se LLM falhar, soma da tabela (família_peça × severidade) + adicional estrutural quando aplicável",
                "Custo ANTES de vender — sai direto do preço de giro. Empresa-específico (mão de obra varia por cidade)",
            ],
            [
                "Racional Reforma",
                "EstimadorReformaLLM (fallback: itens da tabela)",
                "Justificativa livre do LLM quando disponível; senão, sumário 'descrição (R$valor)' dos itens do determinístico",
                "Auditoria rápida sem abrir o laudo: dá pra ver POR QUE a reforma ficou naquele valor",
            ],
            [
                "Frete (R$)",
                "Tabela da empresa",
                "empresa.frete_para(km_origem→pátio, categoria_veículo) com extrapolação +30% acima da maior faixa",
                "Custo de trazer o carro ao pátio — aumenta com distância e peso do veículo",
            ],
            [
                "Justificativa",
                "Precificador",
                "String montada com todas as variáveis da fórmula pra auditoria humana",
                "Facilita revisar por que um lote ficou com aquele preco_alvo sem abrir código",
            ],
            [
                "URL",
                "Auto Avaliar",
                "=HYPERLINK do link direto do anúncio no b2b.autoavaliar.com.br, rotulado 'Abrir anúncio'",
                "1 clique pra abrir o anúncio original e fazer checagens manuais ou lance",
            ],
            [
                "Laudo (PDF)",
                "Scraper detalhe",
                "=HYPERLINK pro PDF do laudo cautelar do lote, rotulado 'Ver laudo'. Extraído do DOM do anúncio (storage.googleapis.com/doc-b2b ou cdn-aav.autoavaliar.com.br). URLs que não são laudo real (Relatório de Transparência Salarial, páginas de listagem) são filtradas e a célula fica '—'.",
                "Olhar o laudo sem precisar abrir o anúncio — ajuda a bater olho em avarias antes de dar lance. '—' significa que o laudo não foi achado (pode existir lote com selo 'SEM LAUDO' ou modal que o scraper não conseguiu abrir).",
            ],
            [
                "Coletado em",
                "Lote.scraped_at",
                "Timestamp do momento em que esse lote foi raspado do Auto Avaliar (por linha)",
                "Saber a idade específica daquela linha — lote raspado hoje é confiável, lote raspado há 3 dias pode já ter sido arrematado (veja também o banner de Última atualização no topo da aba)",
            ],
        ]

        ws.clear()
        ws.update(glossario, value_input_option="USER_ENTERED")
        ws.freeze(rows=1)

    @property
    def sheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self._spreadsheet_id}"
