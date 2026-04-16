"""Testes unitários do SheetsExporter.

Todos os testes mockam o gspread (sem chamadas reais ao Google).
O SQLite usado é in-memory para isolamento total.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.sheets import SheetsExporter, _calcular_roi_pct, HEADER


# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------

def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _lote(lote_id: str = "L001", marca: str = "Ford", modelo: str = "Fiesta", ano: int = 2013,
          km: Optional[int] = 45000, lance_atual: int = 20000) -> Lote:
    return Lote(
        id=lote_id,
        leilao="auto_arremate",
        url=f"https://autoavaliar.com.br/lote/{lote_id}",
        marca=marca,
        modelo=modelo,
        ano=ano,
        km=km,
        lance_atual=lance_atual,
        scraped_at=datetime.utcnow(),
    )


def _avaliacao(
    lote_id: str = "L001",
    empresa_id: str = "uberlandia_mg",
    score_roi: float = 0.3,
    preco_giro: int = 35000,
    preco_giro_fipe: int = 35000,
    preco_giro_aa=None,
) -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id=empresa_id,
        lote_id=lote_id,
        preco_alvo=28000,
        preco_max=30000,
        score_roi=score_roi,
        fator_risco=0.8,
        fator_liquidez=1.0,
        margem_aplicada=0.15,
        frete_incluso=1500,
        reforma_estimada=3000,
        taxas_leilao=600,
        preco_giro=preco_giro,
        preco_giro_fipe=preco_giro_fipe,
        preco_giro_aa=preco_giro_aa,
        justificativa="Laudo leve, FIPE R$30k, giro estimado 30 dias.",
        criado_em=datetime.utcnow(),
    )


def _laudo(lote_id: str = "L001") -> LaudoCache:
    return LaudoCache(
        lote_id=lote_id,
        avarias_json=[],
        severidade_geral="leve",
        motor_ok=True,
        documentacao="ok",
        categoria_veiculo="hatch",
        confidence=0.95,
        modelo_llm="gemini-flash",
        custo_usd=0.001,
        extraido_em=datetime.utcnow(),
    )


def _exporter() -> SheetsExporter:
    return SheetsExporter(
        spreadsheet_id="fake-sheet-id",
        credentials_path="/fake/credentials.json",
    )


# ---------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------

class TestCalcularRoiPct:
    def test_roi_positivo(self):
        av = _avaliacao(preco_giro=35000)
        av.reforma_estimada = 3000
        av.frete_incluso = 1500
        av.taxas_leilao = 600
        # lucro = 35000 - 20000 - 3000 - 1500 - 600 = 9900
        # roi = 9900 / 20000 * 100 = 49.5
        roi = _calcular_roi_pct(av, lance_atual=20000)
        assert roi == pytest.approx(49.5)

    def test_lance_zero_retorna_zero(self):
        av = _avaliacao()
        assert _calcular_roi_pct(av, lance_atual=0) == 0.0


class TestSheetsExporterQuery:
    def test_exportar_retorna_n_linhas(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_lote("L002", modelo="Gol", lance_atual=18000))
            session.add(_avaliacao("L001", score_roi=0.3))
            session.add(_avaliacao("L002", score_roi=0.5))
            session.add(_laudo("L001"))
            session.add(_laudo("L002"))
            session.commit()

        mock_ws = MagicMock()
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_gc = MagicMock()
        mock_gc.open_by_key.return_value = mock_sh

        with patch("gspread.service_account", return_value=mock_gc):
            exporter = _exporter()
            with Session(engine) as session:
                n = exporter.exportar("uberlandia_mg", session)

        assert n == 2
        mock_ws.update.assert_called_once()
        # Verifica que o header foi escrito
        call_args = mock_ws.update.call_args[0][0]
        assert call_args[0] == HEADER

    def test_exportar_ordena_por_score_roi(self):
        """Lote com score_roi maior deve aparecer no topo (rank 1)."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=20000))
            session.add(_lote("L002", modelo="Compass", lance_atual=50000))
            session.add(_avaliacao("L001", score_roi=0.2))
            session.add(_avaliacao("L002", score_roi=0.8))
            session.commit()

        mock_ws = MagicMock()
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_gc = MagicMock()
        mock_gc.open_by_key.return_value = mock_sh

        with patch("gspread.service_account", return_value=mock_gc):
            exporter = _exporter()
            with Session(engine) as session:
                exporter.exportar("uberlandia_mg", session)

        rows = mock_ws.update.call_args[0][0]
        # row[0] = header, row[1] = rank 1, row[2] = rank 2
        # Rank 1 deve ser L002 (score 0.8)
        assert rows[1][1] == "L002"  # coluna Lote ID
        assert rows[1][0] == 1       # rank
        assert rows[2][1] == "L001"
        assert rows[2][0] == 2

    def test_exportar_sem_laudo_nao_quebra(self):
        """LEFT JOIN — lote sem laudo associado deve ser exportado com '—' nos campos do laudo."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
            # sem LaudoCache
            session.commit()

        mock_ws = MagicMock()
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_gc = MagicMock()
        mock_gc.open_by_key.return_value = mock_sh

        with patch("gspread.service_account", return_value=mock_gc):
            exporter = _exporter()
            with Session(engine) as session:
                n = exporter.exportar("uberlandia_mg", session)

        assert n == 1
        rows = mock_ws.update.call_args[0][0]
        data_row = rows[1]
        # Severidade e Motor OK devem ser "—"
        idx_severidade = HEADER.index("Severidade Laudo")
        idx_motor = HEADER.index("Motor OK")
        assert data_row[idx_severidade] == "—"
        assert data_row[idx_motor] == "—"

    def test_exportar_sem_avaliacoes_retorna_zero(self):
        """Empresa sem avaliações deve retornar 0 sem erros."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            # sem AvaliacaoLote
            session.commit()

        mock_ws = MagicMock()
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_gc = MagicMock()
        mock_gc.open_by_key.return_value = mock_sh

        with patch("gspread.service_account", return_value=mock_gc):
            exporter = _exporter()
            with Session(engine) as session:
                n = exporter.exportar("uberlandia_mg", session)

        assert n == 0
        # update é chamado com só o header
        rows = mock_ws.update.call_args[0][0]
        assert len(rows) == 1
        assert rows[0] == HEADER
