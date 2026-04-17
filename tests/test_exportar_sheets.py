"""Testes unitários do SheetsExporter.

Todos os testes mockam o gspread (sem chamadas reais ao Google).
O SQLite usado é in-memory para isolamento total.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.sheets import SheetsExporter, _calcular_roi_no_maximo, HEADER


# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------

def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _lote(lote_id: str = "L001", marca: str = "Ford", modelo: str = "Fiesta", ano: int = 2013,
          km: Optional[int] = 45000, lance_atual: int = 20000,
          fim_em: Optional[datetime] = None) -> Lote:
    # fim_em default no futuro pra passar pelo filtro de "sem data de leilão" do
    # exportador. Testes que querem cobrir o caso None (lote fora de leilão ativo)
    # passam fim_em=... explicitamente como sentinela.
    if fim_em is None:
        fim_em = datetime.now() + timedelta(days=3)
    return Lote(
        id=lote_id,
        leilao="auto_arremate",
        url=f"https://autoavaliar.com.br/lote/{lote_id}",
        marca=marca,
        modelo=modelo,
        ano=ano,
        km=km,
        lance_atual=lance_atual,
        fim_em=fim_em,
        scraped_at=datetime.utcnow(),
    )


def _avaliacao(
    lote_id: str = "L001",
    empresa_id: str = "uberlandia_mg",
    score_roi: float = 0.3,
    preco_giro: int = 35000,
    preco_max: int = 30000,
    preco_giro_fipe: int = 35000,
    preco_giro_aa=None,
) -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id=empresa_id,
        lote_id=lote_id,
        preco_alvo=25000,
        preco_max=preco_max,
        score_roi=score_roi,
        fator_risco=0.8,
        fator_liquidez=1.0,
        margem_aplicada=0.15,
        frete_incluso=1500,
        reforma_estimada=3000,
        taxas_leilao=int(preco_max * 0.08),  # taxas baseadas no lance máximo
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

class TestCalcularRoiNoMaximo:
    def test_roi_positivo(self):
        """ROI se ganhar no lance máximo: (giro - capital_max) / capital_max."""
        av = _avaliacao(preco_giro=35000, preco_max=25000)
        av.reforma_estimada = 3000
        av.frete_incluso = 1500
        av.taxas_leilao = int(25000 * 0.08)  # 2000
        # capital = 25000 + 3000 + 1500 + 2000 = 31500
        # lucro = 35000 - 31500 = 3500
        # roi = 3500 / 31500 * 100 ≈ 11.1
        roi = _calcular_roi_no_maximo(av)
        assert roi == pytest.approx(11.1, abs=0.5)

    def test_preco_max_zero_retorna_zero(self):
        av = _avaliacao()
        av.preco_max = 0
        assert _calcular_roi_no_maximo(av) == 0.0


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
        # update é chamado 2x: primeira = aba da empresa, segunda = aba Glossário
        assert mock_ws.update.call_count == 2
        # Layout: rows[0] = banner de última atualização, rows[1] = HEADER
        call_args = mock_ws.update.call_args_list[0][0][0]
        assert "Última atualização" in call_args[0][0]
        assert call_args[1] == HEADER

    def test_exportar_viaveis_aparecem_primeiro(self):
        """Lotes com preco_max > lance_atual (viáveis) devem vir antes dos inviáveis."""
        engine = _engine_mem()
        with Session(engine) as session:
            # L001: lance=20000, preco_max=30000 → viável (folga +10k)
            session.add(_lote("L001", lance_atual=20000))
            session.add(_avaliacao("L001", score_roi=0.1, preco_max=30000))
            # L002: lance=50000, preco_max=30000 → inviável (caro demais)
            session.add(_lote("L002", modelo="Compass", lance_atual=50000))
            session.add(_avaliacao("L002", score_roi=0.8, preco_max=30000))
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

        rows = mock_ws.update.call_args_list[0][0][0]  # primeira chamada = aba de dados (segunda é o Glossário)
        # row[0] = banner, row[1] = header, row[2] = rank 1, row[3] = rank 2
        idx_lote_id = HEADER.index("Lote ID")
        idx_situacao = HEADER.index("Situação")
        # Rank 1 deve ser L001 (viável), rank 2 deve ser L002 (inviável)
        assert rows[2][idx_lote_id] == "L001"
        assert "Viável" in rows[2][idx_situacao]
        assert rows[3][idx_lote_id] == "L002"
        assert "Caro" in rows[3][idx_situacao]

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
        rows = mock_ws.update.call_args_list[0][0][0]  # primeira chamada = aba de dados (segunda é o Glossário)
        # rows[0]=banner, rows[1]=header, rows[2]=primeiro lote
        data_row = rows[2]
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
        # update é chamado com só banner + header (nenhuma linha de dado)
        rows = mock_ws.update.call_args_list[0][0][0]  # primeira chamada = aba de dados (segunda é o Glossário)
        assert len(rows) == 2
        assert "Última atualização" in rows[0][0]
        assert rows[1] == HEADER

    def test_exportar_situacao_viavel_vs_caro(self):
        """Coluna Situação deve refletir preco_max vs lance_atual corretamente."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=25000))  # lance < preco_max (30k) → viável
            session.add(_avaliacao("L001", preco_max=30000))
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

        rows = mock_ws.update.call_args_list[0][0][0]  # primeira chamada = aba de dados (segunda é o Glossário)
        idx_situacao = HEADER.index("Situação")
        # rows[0]=banner, rows[1]=header, rows[2]=primeiro lote
        assert "Viável" in rows[2][idx_situacao]

    def test_exportar_url_como_hyperlink_clicavel(self):
        """A coluna URL deve virar =HYPERLINK(url, "Abrir anúncio") pra célula ficar curta e clicável."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))  # url = https://autoavaliar.com.br/lote/L001
            session.add(_avaliacao("L001"))
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

        rows = mock_ws.update.call_args_list[0][0][0]
        idx_url = HEADER.index("URL")
        url_cell = rows[2][idx_url]
        assert url_cell.startswith("=HYPERLINK(")
        assert "https://autoavaliar.com.br/lote/L001" in url_cell
        assert "Abrir anúncio" in url_cell

    def test_exportar_url_vazia_nao_gera_hyperlink(self):
        """Lote sem URL deve cair pro placeholder '—', sem fórmula HYPERLINK quebrada."""
        engine = _engine_mem()
        with Session(engine) as session:
            lote = _lote("L001")
            lote.url = ""  # sem URL
            session.add(lote)
            session.add(_avaliacao("L001"))
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

        rows = mock_ws.update.call_args_list[0][0][0]
        idx_url = HEADER.index("URL")
        assert rows[2][idx_url] == "—"

    def test_exportar_roi_baseado_no_lance_maximo(self):
        """ROI deve ser calculado sobre o lance máximo, não sobre lance_atual."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=20000))
            av = _avaliacao("L001", preco_giro=35000, preco_max=25000)
            session.add(av)
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

        rows = mock_ws.update.call_args_list[0][0][0]  # primeira chamada = aba de dados (segunda é o Glossário)
        idx_roi = HEADER.index("ROI se pagar o máximo (%)")
        roi_val = rows[2][idx_roi]
        # ROI ≈ 10-12% (baseado no lance máximo, não no lance atual de 20k)
        assert 5 < roi_val < 20


class TestSheetsExporterFimEmObrigatorio:
    """Lotes sem fim_em (Auto Avaliar não está mais mostrando countdown = lote
    saiu do leilão ativo) não entram no export. Feedback do usuário 2026-04-16:
    100% dos lotes sem data de leilão estavam arrematados ou com link morto.
    """

    def test_lote_sem_fim_em_e_excluido_do_export(self):
        engine = _engine_mem()
        with Session(engine) as session:
            # L001: com fim_em → aparece
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
            # L002: sem fim_em → filtrado
            l2 = _lote("L002", modelo="Gol", fim_em=datetime.now() + timedelta(days=3))
            l2.fim_em = None
            session.add(l2)
            session.add(_avaliacao("L002"))
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

        assert n == 1, "só L001 deve entrar; L002 sem fim_em é filtrado"
        rows = mock_ws.update.call_args_list[0][0][0]
        # rows[0]=banner, rows[1]=header, rows[2]=L001 (único dado)
        assert len(rows) == 3
        idx_lote_id = HEADER.index("Lote ID")
        assert rows[2][idx_lote_id] == "L001"

    def test_sem_fim_em_e_sem_avaliacoes_retorna_zero_limpo(self):
        """Todos os lotes sem fim_em → export vazio (só banner+header), sem crash."""
        engine = _engine_mem()
        with Session(engine) as session:
            l = _lote("L001")
            l.fim_em = None
            session.add(l)
            session.add(_avaliacao("L001"))
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


class TestSheetsExporterEncerrados:
    """Lotes com leilão encerrado (timer passou ou badge ARREMATADO no detalhe)
    devem ir pro FIM da planilha e ter situação '⚠ Encerrado' — evita o usuário
    perder tempo clicando em anúncio já vendido.
    """

    def test_lote_com_timer_vencido_marca_encerrado(self):
        engine = _engine_mem()
        with Session(engine) as session:
            # L001: leilão já acabou 2 dias atrás → encerrado
            expirado = _lote("L001", lance_atual=20000)
            expirado.fim_em = datetime.now() - timedelta(days=2)
            session.add(expirado)
            session.add(_avaliacao("L001", preco_max=30000))
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

        rows = mock_ws.update.call_args_list[0][0][0]
        idx_situacao = HEADER.index("Situação")
        # rows[0]=banner, rows[1]=header, rows[2]=lote encerrado
        assert "Encerrado" in rows[2][idx_situacao]

    def test_lote_com_badge_arrematado_no_raw_json_marca_encerrado(self):
        """Mesmo com timer ainda vivo, se o detalhe raspado viu 'ARREMATADO' → encerrado."""
        engine = _engine_mem()
        with Session(engine) as session:
            lote = _lote("L001", lance_atual=20000)
            lote.fim_em = datetime.now() + timedelta(days=5)  # timer futuro
            lote.raw_json = {"detalhe": {"encerrado": True}}   # mas scraper viu arrematado
            session.add(lote)
            session.add(_avaliacao("L001", preco_max=30000))
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

        rows = mock_ws.update.call_args_list[0][0][0]
        idx_situacao = HEADER.index("Situação")
        assert "Encerrado" in rows[2][idx_situacao]

    def test_encerrados_vao_pro_fim_da_lista(self):
        """Ordenação: ativos viáveis → ativos caros → encerrados (sempre último)."""
        engine = _engine_mem()
        with Session(engine) as session:
            # L001: ativo viável
            l1 = _lote("L001", lance_atual=20000)
            l1.fim_em = datetime.now() + timedelta(days=3)
            session.add(l1)
            session.add(_avaliacao("L001", preco_max=30000))

            # L002: ativo caro (lance > max)
            l2 = _lote("L002", modelo="Gol", lance_atual=50000)
            l2.fim_em = datetime.now() + timedelta(days=3)
            session.add(l2)
            session.add(_avaliacao("L002", preco_max=30000))

            # L003: encerrado — mesmo sendo "viável no papel", não interessa
            l3 = _lote("L003", modelo="Compass", lance_atual=20000)
            l3.fim_em = datetime.now() - timedelta(days=1)
            session.add(l3)
            session.add(_avaliacao("L003", preco_max=50000))
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

        rows = mock_ws.update.call_args_list[0][0][0]
        idx_lote_id = HEADER.index("Lote ID")
        # rows[0]=banner, rows[1]=header, rows[2..4]=dados
        assert rows[2][idx_lote_id] == "L001"   # viável primeiro
        assert rows[3][idx_lote_id] == "L002"   # caro depois
        assert rows[4][idx_lote_id] == "L003"   # encerrado por último


class TestSheetsExporterTimestamp:
    """Banner 'Última atualização da planilha' na linha 1 pra deixar óbvio o
    quão fresco está o snapshot. Coluna 'Coletado em' por linha mostra
    scraped_at do próprio lote.
    """

    def test_banner_de_ultima_atualizacao_na_linha_1(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
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

        rows = mock_ws.update.call_args_list[0][0][0]
        banner = rows[0][0]
        assert "Última atualização da planilha" in banner
        # Deve conter DD/MM/YYYY do dia em que rodou
        hoje = datetime.now().strftime("%d/%m/%Y")
        assert hoje in banner

    def test_coluna_coletado_em_reflete_scraped_at_do_lote(self):
        """Coluna 'Coletado em' deve mostrar o scraped_at do LOTE, não o timestamp do export."""
        engine = _engine_mem()
        scraped_at_fixo = datetime(2026, 4, 14, 22, 0)
        with Session(engine) as session:
            lote = _lote("L001")
            lote.scraped_at = scraped_at_fixo
            session.add(lote)
            session.add(_avaliacao("L001"))
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

        rows = mock_ws.update.call_args_list[0][0][0]
        idx_coletado = HEADER.index("Coletado em")
        assert rows[2][idx_coletado] == "14/04/2026 22:00"

    def test_freeze_inclui_banner_e_header(self):
        """Congelamento deve cobrir banner (linha 1) + header (linha 2)."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
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

        # O primeiro freeze é da aba de dados (o segundo é o Glossário)
        mock_ws.freeze.assert_any_call(rows=2)
