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
from carros_sa.tools.sheets import (
    COLUMN_FORMATS,
    HEADER,
    SheetsExporter,
    _calcular_roi_no_maximo,
    _col_letter,
)


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

    def test_exportar_sem_laudo_marca_nao_analisado(self):
        """Lote sem LaudoCache é exportado mas sinaliza "LAUDO NÃO ANALISADO" e zera
        campos numéricos derivados do laudo — operador não pode dar lance sem conferir
        primeiro (feedback usuário 2026-04-18)."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
            # sem LaudoCache → laudo não foi analisado
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
        rows = mock_ws.update.call_args_list[0][0][0]
        data_row = rows[2]
        assert "LAUDO NÃO ANALISADO" in data_row[HEADER.index("Situação")]
        assert data_row[HEADER.index("Severidade Laudo")] == "não analisado"
        assert data_row[HEADER.index("Motor OK")] == "—"
        # Numéricos derivados de um laudo vazio viram traço: piso de R$ 1k em
        # "Reforma Estimada" + ROI/preço-alvo calculados com reforma=piso seriam
        # tudo chute, então a planilha esconde.
        assert data_row[HEADER.index("Reforma Estimada (R$)")] == "—"
        assert data_row[HEADER.index("Lance Máximo (R$)")] == "—"
        assert data_row[HEADER.index("ROI se pagar o máximo (%)")] == "—"
        assert data_row[HEADER.index("ROI anualizado (%)")] == "—"
        assert "não dê lance" in data_row[HEADER.index("Racional Reforma")].lower()

    def test_exportar_laudo_fallback_confidence_baixa_marca_nao_analisado(self):
        """LaudoCache com confidence=0.5 é fallback `_laudo_sem_pdf` — trata igual a
        laudo ausente. Limite 0.6 aceita só laudos realmente extraídos de PDF."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
            laudo = _laudo("L001")
            laudo.confidence = 0.5  # fallback
            laudo.severidade_geral = "nenhuma"
            session.add(laudo)
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
        rows = mock_ws.update.call_args_list[0][0][0]
        data_row = rows[2]
        assert "LAUDO NÃO ANALISADO" in data_row[HEADER.index("Situação")]
        assert data_row[HEADER.index("Reforma Estimada (R$)")] == "—"

    def test_exportar_laudo_confidence_alta_mantem_valores(self):
        """LaudoCache com confidence>=0.6 é laudo real — mantém campos numéricos."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
            session.add(_laudo("L001"))  # confidence 0.95 via default
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
        data_row = rows[2]
        assert "LAUDO NÃO ANALISADO" not in data_row[HEADER.index("Situação")]
        assert data_row[HEADER.index("Severidade Laudo")] == "leve"
        assert data_row[HEADER.index("Reforma Estimada (R$)")] == 3000

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

    def test_exportar_laudo_url_valida_vira_hyperlink_ver_laudo(self):
        """Se `lote.raw_json['detalhe']['laudo_pdf_url']` passar pelo filtro
        de decoy, a coluna 'Laudo (PDF)' deve virar =HYPERLINK('Ver laudo')."""
        engine = _engine_mem()
        with Session(engine) as session:
            lote = _lote("L001")
            lote.raw_json = {
                "detalhe": {
                    "laudo_pdf_url": "https://storage.googleapis.com/doc-b2b/laudos/L001/laudo.pdf?sig=abc",
                }
            }
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
        idx_laudo = HEADER.index("Laudo (PDF)")
        cell = rows[2][idx_laudo]
        assert cell.startswith("=HYPERLINK(")
        assert "doc-b2b/laudos/L001/laudo.pdf" in cell
        assert "Ver laudo" in cell

    def test_exportar_laudo_url_decoy_transparencia_vira_placeholder(self):
        """URL de Relatório de Transparência (decoy conhecido) NÃO deve virar
        hyperlink — célula fica '—'. Evita o usuário clicar num PDF de RH
        achando que é laudo do carro. Cobre 83/85 lotes afetados antes do fix."""
        engine = _engine_mem()
        decoy = (
            "https://repo-site-aav-production.storage.googleapis.com/app/uploads/"
            "2025/10/Relatorio-de-Transparencia-e-igualdade-Salarial-"
            "de-Mulheres-e-Homens-2-o-semestre.pdf"
        )
        with Session(engine) as session:
            lote = _lote("L001")
            lote.raw_json = {"detalhe": {"laudo_pdf_url": decoy}}
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
        idx_laudo = HEADER.index("Laudo (PDF)")
        assert rows[2][idx_laudo] == "—"

    def test_exportar_laudo_url_ausente_vira_placeholder(self):
        """Lote sem `laudo_pdf_url` em raw_json → célula '—', sem crash."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))   # raw_json default = {}
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
        idx_laudo = HEADER.index("Laudo (PDF)")
        assert rows[2][idx_laudo] == "—"

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

    def test_reaplica_formato_numerico_em_reforma_e_frete(self):
        """ws.clear() preserva formato de célula; exporter DEVE reaplicar NUMBER
        nas colunas R$ senão inteiros herdam formato DATE antigo e viram datas."""
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

        # batch_format deve ter sido chamado com formato NUMBER pras colunas R$
        assert mock_ws.batch_format.called, "batch_format não foi chamado"
        formatos = mock_ws.batch_format.call_args_list[0][0][0]

        # Mapeia range→pattern pra consulta fácil
        ranges = {f["range"]: f["format"]["numberFormat"]["pattern"] for f in formatos}

        # Reforma e Frete (o bug reportado) viram NUMBER, não DATE
        reforma_letter = _col_letter(HEADER.index("Reforma Estimada (R$)"))
        frete_letter = _col_letter(HEADER.index("Frete (R$)"))
        assert ranges[f"{reforma_letter}:{reforma_letter}"] == "#,##0"
        assert ranges[f"{frete_letter}:{frete_letter}"] == "#,##0"

    def test_col_letter_converte_indices(self):
        """Índice 0-based → letra de coluna (A, B, ..., Z, AA)."""
        assert _col_letter(0) == "A"
        assert _col_letter(17) == "R"   # Reforma Estimada
        assert _col_letter(18) == "S"   # Frete
        assert _col_letter(25) == "Z"
        assert _col_letter(26) == "AA"

    def test_todas_colunas_em_COLUMN_FORMATS_estao_no_HEADER(self):
        """Cadeado contra typos — cada chave de COLUMN_FORMATS precisa bater
        EXATAMENTE com um item do HEADER, senão o format não é aplicado."""
        for col_name in COLUMN_FORMATS:
            assert col_name in HEADER, f"{col_name!r} não está em HEADER"

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
    são FILTRADOS completamente da planilha — operador só quer ver o que ainda
    dá pra arrematar. Antes a gente empurrava pro fim da lista, mas isso só
    polui: lote encerrado é ruído, não ação possível.
    """

    def test_lote_com_timer_vencido_nao_aparece(self):
        engine = _engine_mem()
        with Session(engine) as session:
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
                n = exporter.exportar("uberlandia_mg", session)

        assert n == 0
        rows = mock_ws.update.call_args_list[0][0][0]
        # Só banner (linha 1) + header (linha 2). Nenhuma linha de dados.
        assert len(rows) == 2

    def test_lote_com_badge_arrematado_no_raw_json_nao_aparece(self):
        """Mesmo com timer ainda vivo, badge ARREMATADO no detalhe filtra o lote."""
        engine = _engine_mem()
        with Session(engine) as session:
            lote = _lote("L001", lance_atual=20000)
            lote.fim_em = datetime.now() + timedelta(days=5)
            lote.raw_json = {"detalhe": {"encerrado": True}}
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
                n = exporter.exportar("uberlandia_mg", session)

        assert n == 0

    def test_encerrados_filtrados_ativos_exportados(self):
        """Mix de ativos e encerrados: só os ativos entram na planilha."""
        engine = _engine_mem()
        with Session(engine) as session:
            # L001: ativo viável
            l1 = _lote("L001", lance_atual=20000)
            l1.fim_em = datetime.now() + timedelta(days=3)
            session.add(l1)
            session.add(_avaliacao("L001", preco_max=30000))

            # L002: ativo caro (lance > max) — ainda entra, o operador decide
            l2 = _lote("L002", modelo="Gol", lance_atual=50000)
            l2.fim_em = datetime.now() + timedelta(days=3)
            session.add(l2)
            session.add(_avaliacao("L002", preco_max=30000))

            # L003: encerrado — NÃO entra
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
                n = exporter.exportar("uberlandia_mg", session)

        assert n == 2
        rows = mock_ws.update.call_args_list[0][0][0]
        idx_lote_id = HEADER.index("Lote ID")
        lote_ids = [rows[i][idx_lote_id] for i in range(2, len(rows))]
        assert "L003" not in lote_ids
        assert set(lote_ids) == {"L001", "L002"}


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


class TestReformaRacional:
    """Racional do valor da reforma aparece na planilha como coluna dedicada."""

    def test_header_inclui_coluna_racional_reforma(self):
        assert "Racional Reforma" in HEADER

    def test_exporta_racional_reforma_da_avaliacao(self):
        engine = _engine_mem()
        racional = "Coluna B esq. solda+pintura R$3800 · Alinhamento chassi R$2800"
        with Session(engine) as session:
            session.add(_lote("L001"))
            av = _avaliacao("L001", score_roi=0.3)
            av.reforma_racional = racional
            session.add(av)
            session.add(_laudo("L001"))
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
        idx_racional = HEADER.index("Racional Reforma")
        # row[2] é a primeira linha de dados (rank 1)
        assert rows[2][idx_racional] == racional

    def test_racional_ausente_mostra_travessao(self):
        """Avaliações antigas sem racional_reforma populado exibem '—' sem quebrar."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            av = _avaliacao("L001")  # reforma_racional fica None
            session.add(av)
            session.add(_laudo("L001"))
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
        idx_racional = HEADER.index("Racional Reforma")
        assert rows[2][idx_racional] == "—"


class TestCidadesFreteSheet:
    """Aba de cidades do raio operacional + frete por categoria por cidade."""

    def _separa_chamadas(self, mock_sh):
        """Devolve (mock_ws_empresa, mock_ws_cidades, mock_ws_glossario) na ordem em que foram criados."""
        # `worksheet(...)` levanta porque a aba ainda não existe → cai no add_worksheet,
        # que retorna a sequência de mocks dada.
        criados = [MagicMock(), MagicMock(), MagicMock()]
        mock_sh.worksheet.side_effect = Exception("não existe")
        mock_sh.add_worksheet.side_effect = criados
        return criados

    def test_aba_cidades_existe_para_empresa_real(self):
        """Empresa real (`carros_uberlandia`) → aba `cidades_carros_uberlandia` é escrita."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001", empresa_id="carros_uberlandia"))
            session.commit()

        mock_sh = MagicMock()
        mock_gc = MagicMock()
        mock_gc.open_by_key.return_value = mock_sh
        ws_empresa, ws_cidades, ws_glossario = self._separa_chamadas(mock_sh)

        with patch("gspread.service_account", return_value=mock_gc):
            exporter = _exporter()
            with Session(engine) as session:
                exporter.exportar("carros_uberlandia", session)

        # Ordem das chamadas a add_worksheet: empresa, cidades, glossário
        titulos = [c.kwargs["title"] for c in mock_sh.add_worksheet.call_args_list]
        assert titulos == [
            "carros_uberlandia",
            "cidades_carros_uberlandia",
            "Glossário",
        ]
        # Aba de cidades teve update chamado
        assert ws_cidades.update.called

    def test_aba_cidades_inclui_patio_com_distancia_zero_e_frete_zero(self):
        """Pátio (Uberlândia) deve aparecer na lista, distância 0, frete 0."""
        engine = _engine_mem()
        mock_sh = MagicMock()
        mock_gc = MagicMock()
        mock_gc.open_by_key.return_value = mock_sh
        _, ws_cidades, _ = self._separa_chamadas(mock_sh)

        with patch("gspread.service_account", return_value=mock_gc):
            exporter = _exporter()
            with Session(engine) as session:
                exporter.exportar("carros_uberlandia", session)

        rows = ws_cidades.update.call_args_list[0][0][0]
        # rows[0] = banner, rows[1] = header, rows[2] = primeira cidade (pátio)
        header = rows[1]
        idx_dist = header.index("Distância (km)")
        idx_hatch = header.index("Frete Hatch (R$)")
        idx_outro = header.index("Frete Outro (R$)")
        primeira = rows[2]
        assert primeira[0] == "Uberlândia"
        assert primeira[1] == "MG"
        assert primeira[idx_dist] == 0
        assert primeira[idx_hatch] == 0
        assert primeira[idx_outro] == 0

    def test_aba_cidades_conta_lotes_ativos_por_origem(self):
        """Lotes ativos com origem em uma cidade do raio aparecem contados naquela linha."""
        engine = _engine_mem()
        with Session(engine) as session:
            # Dois lotes ativos em Araguari (raio de Uberlândia)
            l1 = _lote("L001")
            l1.origem_cidade = "Araguari"
            l1.origem_uf = "MG"
            l2 = _lote("L002", modelo="Gol")
            l2.origem_cidade = "ARAGUARI"  # case diferente — normalização precisa colar
            l2.origem_uf = "mg"
            # Um lote em Uberlândia
            l3 = _lote("L003", modelo="Onix")
            l3.origem_cidade = "Uberlândia"
            l3.origem_uf = "MG"
            session.add(l1)
            session.add(l2)
            session.add(l3)
            session.commit()

        mock_sh = MagicMock()
        mock_gc = MagicMock()
        mock_gc.open_by_key.return_value = mock_sh
        _, ws_cidades, _ = self._separa_chamadas(mock_sh)

        with patch("gspread.service_account", return_value=mock_gc):
            exporter = _exporter()
            with Session(engine) as session:
                exporter.exportar("carros_uberlandia", session)

        rows = ws_cidades.update.call_args_list[0][0][0]
        header = rows[1]
        idx_qtd = header.index("Lotes ativos no DB")
        # Mapeia cidade → contagem
        contagem = {(r[0], r[1]): r[idx_qtd] for r in rows[2:]}
        assert contagem[("Uberlândia", "MG")] == 1
        assert contagem[("Araguari", "MG")] == 2

    def test_empresa_inexistente_skipa_aba_cidades_silenciosamente(self):
        """Sem YAML da empresa, aba é pulada — fluxo principal não quebra."""
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
                # `uberlandia_mg` não tem YAML — exportar deve completar sem erro
                n = exporter.exportar("uberlandia_mg", session)
        assert n == 1
