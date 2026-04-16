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

HEADER = [
    "Rank",
    "Situação",
    "Lote ID",
    "Modelo",
    "Fim do Leilão",
    "KM",
    "Lance Atual (R$)",
    "Lance Máximo (R$)",
    "FIPE (R$)",
    "Webmotors Mediana (R$)",
    "Preço Giro FIPE (R$)",
    "Preço Giro Auto Avaliar (R$)",
    "FIPE % (lance min)",
    "ROI se pagar o máximo (%)",
    "Dias até venda (est.)",
    "ROI anualizado (%)",
    "Fator Risco",
    "Severidade Laudo",
    "Motor OK",
    "Reforma Estimada (R$)",
    "Frete (R$)",
    "Justificativa",
    "URL",
    "Atualizado em",
]


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
        # Ordenação: viáveis primeiro (preco_max > lance_atual), dentro de cada grupo
        # ordena por folga de lance decrescente (mais margem de negociação primeiro)
        rows_sorted = sorted(
            rows,
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

        rows: List[dict] = []
        for av in avaliacoes:
            lote = session.get(Lote, av.lote_id)
            if lote is None:
                continue
            laudo: Optional[LaudoCache] = session.get(LaudoCache, av.lote_id)

            fim_em_str = "—"
            if lote.fim_em is not None:
                try:
                    fim_em_str = lote.fim_em.strftime("%d/%m/%Y %H:%M")
                except Exception:
                    fim_em_str = str(lote.fim_em)

            viavel = av.preco_max > (lote.lance_atual or 0)

            from carros_sa.agents.calibracao_giro import roi_anualizado
            roi_max = _calcular_roi_no_maximo(av)
            roi_anual = roi_anualizado(roi_max / 100.0, av.dias_giro_estimado) * 100

            rows.append({
                "lote_id": av.lote_id,
                "modelo": f"{lote.marca} {lote.modelo} {lote.ano}",
                "fim_em": fim_em_str,
                "km": lote.km,
                "lance_atual": lote.lance_atual or 0,
                "preco_max": av.preco_max,
                "fipe": av.fipe,
                "webmotors_mediana": av.webmotors_mediana,
                "preco_giro_fipe": av.preco_giro_fipe,
                "preco_giro_aa": av.preco_giro_aa,
                "fipe_pct_lance_minimo": lote.fipe_pct_lance_minimo,
                "roi_pct": roi_max,
                "dias_giro": av.dias_giro_estimado,
                "roi_anualizado": round(roi_anual, 1),
                "fator_risco": round(av.fator_risco, 3),
                "severidade": laudo.severidade_geral if laudo else "—",
                "motor_ok": ("Sim" if laudo.motor_ok else "NÃO") if laudo else "—",
                "reforma_estimada": av.reforma_estimada,
                "frete": av.frete_incluso,
                "justificativa": av.justificativa,
                "url": lote.url,
                "viavel": viavel,
            })
        return rows

    def _write_sheet(self, empresa_id: str, rows: List[dict]) -> None:
        """Abre/cria aba, limpa, escreve header + rows."""
        gc = self._client()
        sh = gc.open_by_key(self._spreadsheet_id)

        # Abre ou cria a aba com nome = empresa_id
        try:
            ws = sh.worksheet(empresa_id)
        except Exception:
            ws = sh.add_worksheet(title=empresa_id, rows=500, cols=len(HEADER))

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        sheet_rows = [HEADER]
        for rank, r in enumerate(rows, start=1):
            situacao = "✓ Viável" if r["viavel"] else "✗ Caro demais"
            # URL → HYPERLINK clicável com label curto ("Abrir anúncio")
            # em vez da URL crua longa. Sheets interpreta com USER_ENTERED.
            if r["url"]:
                url_escaped = r["url"].replace('"', '""')
                url_cell = f'=HYPERLINK("{url_escaped}"; "Abrir anúncio")'
            else:
                url_cell = "—"
            sheet_rows.append([
                rank,
                situacao,
                r["lote_id"],
                r["modelo"],
                r["fim_em"],
                r["km"] if r["km"] is not None else "—",
                r["lance_atual"],
                r["preco_max"],
                r["fipe"] if r["fipe"] is not None else "—",
                r["webmotors_mediana"] if r["webmotors_mediana"] is not None else "—",
                r["preco_giro_fipe"],
                r["preco_giro_aa"] if r["preco_giro_aa"] is not None else "—",
                f"{r['fipe_pct_lance_minimo']}%" if r["fipe_pct_lance_minimo"] is not None else "—",
                r["roi_pct"],
                r["dias_giro"] if r["dias_giro"] is not None else "—",
                r["roi_anualizado"],
                r["fator_risco"],
                r["severidade"],
                r["motor_ok"],
                r["reforma_estimada"],
                r["frete"],
                r["justificativa"],
                url_cell,
                ts,
            ])

        ws.clear()
        ws.update(sheet_rows, value_input_option="USER_ENTERED")

        # Congela linha do header
        ws.freeze(rows=1)

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
                "✓ Viável se Lance Máximo > Lance Atual, senão ✗ Caro demais",
                "Filtro de sanidade: se o mínimo já passou do nosso teto, não há lance a fazer",
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
                "Marca + modelo + ano extraídos do card via regex",
                "Identificação humana do veículo",
            ],
            [
                "Fim do Leilão",
                "Auto Avaliar",
                "Timer DD:HH:MM:SS do card convertido pra datetime absoluto",
                "Urgência — lotes com fim próximo precisam de decisão antes",
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
                "Webmotors Mediana (R$)",
                "Webmotors scraper",
                "Mediana dos preços dos anúncios ativos do mesmo modelo/ano",
                "Preço real de mercado (varejo) agora, não 'oficial' como FIPE",
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
                "EstimadorReforma",
                "Σ (família_peça × severidade) na tabela YAML da empresa + adicional estrutural se aplicável",
                "Custo ANTES de vender — sai direto do preço de giro. Empresa-específico (mão de obra varia por cidade)",
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
                "Atualizado em",
                "Derivado",
                "Timestamp local do momento em que a aba foi reescrita",
                "Saber se os números são frescos (pipeline diário roda às 7h)",
            ],
        ]

        ws.clear()
        ws.update(glossario, value_input_option="USER_ENTERED")
        ws.freeze(rows=1)

    @property
    def sheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self._spreadsheet_id}"
