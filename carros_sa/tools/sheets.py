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
    "Preço Giro FIPE (R$)",
    "Preço Giro Auto Avaliar (R$)",
    "FIPE % (lance min)",
    "ROI se pagar o máximo (%)",
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
        """Lê SQLite, escreve aba <empresa_id> na Sheet. Retorna n linhas exportadas."""
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

            rows.append({
                "lote_id": av.lote_id,
                "modelo": f"{lote.marca} {lote.modelo} {lote.ano}",
                "fim_em": fim_em_str,
                "km": lote.km,
                "lance_atual": lote.lance_atual or 0,
                "preco_max": av.preco_max,
                "preco_giro_fipe": av.preco_giro_fipe,
                "preco_giro_aa": av.preco_giro_aa,
                "fipe_pct_lance_minimo": lote.fipe_pct_lance_minimo,
                "roi_pct": _calcular_roi_no_maximo(av),
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
            sheet_rows.append([
                rank,
                situacao,
                r["lote_id"],
                r["modelo"],
                r["fim_em"],
                r["km"] if r["km"] is not None else "—",
                r["lance_atual"],
                r["preco_max"],
                r["preco_giro_fipe"],
                r["preco_giro_aa"] if r["preco_giro_aa"] is not None else "—",
                f"{r['fipe_pct_lance_minimo']}%" if r["fipe_pct_lance_minimo"] is not None else "—",
                r["roi_pct"],
                r["fator_risco"],
                r["severidade"],
                r["motor_ok"],
                r["reforma_estimada"],
                r["frete"],
                r["justificativa"],
                r["url"],
                ts,
            ])

        ws.clear()
        ws.update(sheet_rows, value_input_option="USER_ENTERED")

        # Congela linha do header
        ws.freeze(rows=1)

    @property
    def sheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self._spreadsheet_id}"
