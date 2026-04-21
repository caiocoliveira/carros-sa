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
          fim_em: Optional[datetime] = None,
          com_laudo_url: bool = True) -> Lote:
    # fim_em default no futuro pra passar pelo filtro de "sem data de leilão" do
    # exportador. Testes que querem cobrir o caso None (lote fora de leilão ativo)
    # passam fim_em=... explicitamente como sentinela.
    if fim_em is None:
        fim_em = datetime.now() + timedelta(days=3)
    # A aba principal exige laudo_pdf_url válida (invariante pós-fix do split em
    # pendentes). Helpers antigos criavam lote sem URL porque a invariante
    # ainda não existia — agora injetamos URL de laudo válida por default e
    # testes que cobrem cenários de URL ausente passam `com_laudo_url=False`.
    raw_json = {}
    if com_laudo_url:
        raw_json = {
            "detalhe": {
                "laudo_pdf_url": (
                    f"https://storage.googleapis.com/doc-b2b/laudos/{lote_id}/laudo.pdf?sig=test"
                ),
            },
        }
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
        raw_json=raw_json,
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
        # update é chamado 3x: main + pendentes + Glossário (cidades pula p/ empresa
        # sem YAML). Mock único de worksheet colapsa todas as chamadas no mesmo mock.
        assert mock_ws.update.call_count == 3
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
            session.add(_laudo("L001"))
            # L002: lance=50000, preco_max=30000 → inviável (caro demais)
            session.add(_lote("L002", modelo="Compass", lance_atual=50000))
            session.add(_avaliacao("L002", score_roi=0.8, preco_max=30000))
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
                exporter.exportar("uberlandia_mg", session)

        rows = mock_ws.update.call_args_list[0][0][0]  # primeira chamada = aba de dados (segunda é o Glossário)
        # row[0] = banner, row[1] = header, row[2] = rank 1, row[3] = rank 2
        idx_modelo = HEADER.index("Modelo")
        idx_situacao = HEADER.index("Situação")
        # Rank 1 deve ser L001 (Fiesta, viável), rank 2 deve ser L002 (Compass, inviável)
        assert "Fiesta" in rows[2][idx_modelo]
        assert "Viável" in rows[2][idx_situacao]
        assert "Compass" in rows[3][idx_modelo]
        assert "Caro" in rows[3][idx_situacao]

    def test_exportar_sem_laudo_vai_pra_pendentes(self):
        """Lote sem LaudoCache falha o invariante (laudo não analisado) → NÃO entra
        na main; é movido pra aba de pendentes com Motivo explicando. Defende o
        contrato 'laudo baixado+revisado+link' da aba principal — operador só
        vê ali o que pode avaliar diretamente."""
        engine = _engine_mem()
        with Session(engine) as session:
            # Lote tem URL de laudo (default do helper), mas não tem LaudoCache.
            # Motivo esperado: `extracao_falhou` (URL ok, extração não rolou).
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
                n = exporter.exportar("uberlandia_mg", session)

        assert n == 0, "main deve estar vazia — lote sem laudo cai em pendentes"
        # update[0] = main (vazia), update[1] = pendentes (com o lote), update[2] = glossário
        main_rows = mock_ws.update.call_args_list[0][0][0]
        assert len(main_rows) == 2, "só banner + header na main"
        pendentes_rows = mock_ws.update.call_args_list[1][0][0]
        # rows[0]=banner, rows[1]=header, rows[2]=primeiro lote pendente
        assert len(pendentes_rows) >= 3
        motivo_cell = pendentes_rows[2][1]  # coluna Motivo é a 2ª em HEADER_PENDENTES
        assert "extração ficou abaixo do limiar" in motivo_cell

    def test_exportar_sem_laudo_e_sem_url_vira_pendente_com_motivo_duplo(self):
        """Lote sem LaudoCache E sem URL de laudo → motivo 'sem_url_e_extracao_falhou'.
        Cenário mais crítico: scraper falhou E LLM também não rodou. Operador precisa
        abrir o anúncio manualmente pra decidir."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", com_laudo_url=False))
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
        pendentes_rows = mock_ws.update.call_args_list[1][0][0]
        motivo_cell = pendentes_rows[2][1]
        assert "Scraper não achou o link" in motivo_cell

    def test_exportar_laudo_fallback_confidence_baixa_vai_pra_pendentes(self):
        """LaudoCache com confidence=0.5 é fallback `_laudo_sem_pdf` — trata igual a
        laudo ausente. Limite 0.6 aceita só laudos realmente extraídos de PDF, e
        abaixo disso o lote cai em pendentes (motivo `extracao_falhou`, pois a URL
        está presente por default do helper)."""
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

        assert n == 0, "confidence<0.6 conta como não-revisado → cai em pendentes"
        main_rows = mock_ws.update.call_args_list[0][0][0]
        assert len(main_rows) == 2
        pendentes_rows = mock_ws.update.call_args_list[1][0][0]
        motivo_cell = pendentes_rows[2][1]
        assert "extração ficou abaixo do limiar" in motivo_cell

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
        assert data_row[HEADER.index("Reforma (R$)")] == 3000

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

        rows = mock_ws.update.call_args_list[0][0][0]  # primeira chamada = aba de dados (segunda é o Glossário)
        idx_situacao = HEADER.index("Situação")
        # rows[0]=banner, rows[1]=header, rows[2]=primeiro lote
        assert "Viável" in rows[2][idx_situacao]

    def test_exportar_url_como_hyperlink_clicavel(self):
        """A coluna Anúncio deve virar =HYPERLINK(url, "Abrir anúncio") pra célula ficar curta e clicável."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))  # url = https://autoavaliar.com.br/lote/L001
            session.add(_avaliacao("L001"))
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
        idx_url = HEADER.index("Anúncio")
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
            session.add(_laudo("L001"))  # laudo revisado — requerido pra main
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
        idx_laudo = HEADER.index("Laudo")
        cell = rows[2][idx_laudo]
        assert cell.startswith("=HYPERLINK(")
        assert "doc-b2b/laudos/L001/laudo.pdf" in cell
        assert "Ver laudo" in cell

    def test_exportar_laudo_url_decoy_transparencia_vai_pra_pendentes(self):
        """URL de Relatório de Transparência (decoy conhecido) não vira link na main —
        lote cai em pendentes com motivo `url_invalida_ou_decoy`. Cobre o cenário
        histórico (83/85 lotes afetados antes do fix de deteção de decoy)."""
        engine = _engine_mem()
        decoy = (
            "https://repo-site-aav-production.storage.googleapis.com/app/uploads/"
            "2025/10/Relatorio-de-Transparencia-e-igualdade-Salarial-"
            "de-Mulheres-e-Homens-2-o-semestre.pdf"
        )
        with Session(engine) as session:
            lote = _lote("L001", com_laudo_url=False)
            lote.raw_json = {"detalhe": {"laudo_pdf_url": decoy}}
            session.add(lote)
            session.add(_avaliacao("L001"))
            session.add(_laudo("L001"))  # laudo extraído OK, só URL é decoy
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

        assert n == 0, "URL decoy ≠ URL válida → não entra na main"
        pendentes_rows = mock_ws.update.call_args_list[1][0][0]
        # Motivo esperado: `url_invalida_ou_decoy`
        motivo_cell = pendentes_rows[2][1]
        assert "URL do laudo ausente ou aponta pra decoy" in motivo_cell

    def test_exportar_laudo_url_ausente_vai_pra_pendentes(self):
        """Lote sem `laudo_pdf_url` em raw_json cai em pendentes com motivo
        `url_invalida_ou_decoy` (mesmo bucket de URL decoy — ambos = sem link válido)."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", com_laudo_url=False))  # raw_json vazio
            session.add(_avaliacao("L001"))
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
                n = exporter.exportar("uberlandia_mg", session)

        assert n == 0
        pendentes_rows = mock_ws.update.call_args_list[1][0][0]
        motivo_cell = pendentes_rows[2][1]
        assert "URL do laudo ausente ou aponta pra decoy" in motivo_cell

    def test_exportar_url_vazia_nao_gera_hyperlink(self):
        """Lote sem URL deve cair pro placeholder '—', sem fórmula HYPERLINK quebrada."""
        engine = _engine_mem()
        with Session(engine) as session:
            lote = _lote("L001")
            lote.url = ""  # sem URL
            session.add(lote)
            session.add(_avaliacao("L001"))
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
        idx_url = HEADER.index("Anúncio")
        assert rows[2][idx_url] == "—"

    def test_reaplica_formato_numerico_em_reforma_e_lance_maximo(self):
        """ws.clear() preserva formato de célula; exporter DEVE reaplicar NUMBER
        nas colunas R$ senão inteiros herdam formato DATE antigo e viram datas."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
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

        # batch_format deve ter sido chamado com formato NUMBER pras colunas R$
        assert mock_ws.batch_format.called, "batch_format não foi chamado"
        formatos = mock_ws.batch_format.call_args_list[0][0][0]

        # Mapeia range→pattern pra consulta fácil
        ranges = {f["range"]: f["format"]["numberFormat"]["pattern"] for f in formatos}

        # Reforma e Lance Máximo viram NUMBER, não DATE
        reforma_letter = _col_letter(HEADER.index("Reforma (R$)"))
        max_letter = _col_letter(HEADER.index("Lance Máximo (R$)"))
        assert ranges[f"{reforma_letter}:{reforma_letter}"] == "#,##0"
        assert ranges[f"{max_letter}:{max_letter}"] == "#,##0"

    def test_col_letter_converte_indices(self):
        """Índice 0-based → letra de coluna (A, B, ..., Z, AA)."""
        assert _col_letter(0) == "A"
        assert _col_letter(17) == "R"
        assert _col_letter(18) == "S"
        assert _col_letter(25) == "Z"
        assert _col_letter(26) == "AA"

    def test_todas_colunas_em_COLUMN_FORMATS_estao_no_HEADER(self):
        """Cadeado contra typos — cada chave de COLUMN_FORMATS precisa bater
        EXATAMENTE com um item do HEADER, senão o format não é aplicado."""
        for col_name in COLUMN_FORMATS:
            assert col_name in HEADER, f"{col_name!r} não está em HEADER"

    def test_exportar_roi_baseado_no_lance_maximo(self):
        """ROI anualizado deve ser calculado sobre o lance máximo, não sobre lance_atual."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=20000))
            av = _avaliacao("L001", preco_giro=35000, preco_max=25000)
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

        rows = mock_ws.update.call_args_list[0][0][0]  # primeira chamada = aba de dados (segunda é o Glossário)
        idx_roi = HEADER.index("ROI anualizado (%)")
        roi_val = rows[2][idx_roi]
        # ROI no máximo ≈ 11%; sem dias_giro preenchido, fallback = 90d
        # ROI anualizado ≈ 11 × 365/90 ≈ 44%. Faixa larga pra acomodar pequenas
        # variações do preco_alvo/fator_risco.
        assert 20 < roi_val < 80


class TestInvarianteMainSheetCompleta:
    """Invariante: TODO lote na aba principal tem laudo baixado, revisado (confidence
    >= 0.6) E link válido (passa `is_laudo_pdf_url`). Qualquer combinação faltante
    (URL decoy, URL ausente, confidence baixa, sem LaudoCache) deve empurrar o lote
    pra aba `<empresa>_pendentes`. Defende o feedback "garantir que todos carros na
    lista tem laudo baixado, revisado e link na planilha" — se essa garantia
    quebrar, esse teste é o primeiro a cair.
    """

    def test_classificar_pendencia_os_quatro_quadrantes(self):
        """Matriz 2×2 (laudo_analisado × laudo_url_valida) cobre todas as possibilidades.
        Implementação explicita invariante: só um canto retorna None (tudo ok)."""
        from carros_sa.tools.sheets import _classificar_pendencia

        # (1,1) — ok, não há motivo
        assert _classificar_pendencia(
            laudo_analisado=True, laudo_url_valida=True,
        ) is None
        # (0,0) — ambos faltando, motivo composto
        assert _classificar_pendencia(
            laudo_analisado=False, laudo_url_valida=False,
        ) == "sem_url_e_extracao_falhou"
        # (1,0) — laudo extraído mas URL decoy/ausente
        assert _classificar_pendencia(
            laudo_analisado=True, laudo_url_valida=False,
        ) == "url_invalida_ou_decoy"
        # (0,1) — URL ok mas extração falhou
        assert _classificar_pendencia(
            laudo_analisado=False, laudo_url_valida=True,
        ) == "extracao_falhou"

    def test_main_sheet_nunca_tem_lote_pendente(self):
        """E2E: mistura 4 lotes (1 ok + 3 em cada motivo de pendência), confirma que
        a main só exporta o ok e os outros 3 caem no pendentes. Se alguém quebrar
        o filtro em `exportar()` ou mudar `_classificar_pendencia` sem coordenar,
        esse teste pega antes de ir pro usuário."""
        engine = _engine_mem()
        with Session(engine) as session:
            # OK: laudo revisado + URL válida
            session.add(_lote("L_OK"))
            session.add(_avaliacao("L_OK"))
            session.add(_laudo("L_OK"))
            # SEM URL + SEM LAUDO
            session.add(_lote("L_AMBOS", com_laudo_url=False))
            session.add(_avaliacao("L_AMBOS"))
            # URL VÁLIDA + SEM LAUDO (extracao_falhou)
            session.add(_lote("L_SEMREV"))
            session.add(_avaliacao("L_SEMREV"))
            # URL DECOY + LAUDO OK (url_invalida_ou_decoy)
            decoy = "https://repo-site-aav-production.storage.googleapis.com/Relatorio-de-Transparencia.pdf"
            l_decoy = _lote("L_DECOY", com_laudo_url=False)
            l_decoy.raw_json = {"detalhe": {"laudo_pdf_url": decoy}}
            session.add(l_decoy)
            session.add(_avaliacao("L_DECOY"))
            session.add(_laudo("L_DECOY"))
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

        assert n == 1, "só L_OK passa pra main"
        # update[0] = main, update[1] = pendentes, update[2] = glossário
        main_rows = mock_ws.update.call_args_list[0][0][0]
        # banner + header + 1 data row
        assert len(main_rows) == 3
        # Nenhuma célula na main pode ter "LAUDO NÃO ANALISADO" nem "—" em Laudo
        idx_laudo = HEADER.index("Laudo")
        data_row = main_rows[2]
        laudo_cell = data_row[idx_laudo]
        assert laudo_cell.startswith("=HYPERLINK("), (
            f"main tem Laudo sem hyperlink: {laudo_cell!r} — invariante quebrado"
        )
        assert "—" not in laudo_cell

        # Pendentes tem os 3 lotes faltosos, cada um com motivo distinto.
        pendentes_rows = mock_ws.update.call_args_list[1][0][0]
        assert len(pendentes_rows) == 2 + 3  # banner + header + 3 dados
        motivos_presentes = {pendentes_rows[i][1] for i in range(2, 5)}
        # Cada um dos 3 motivos de MOTIVOS_PENDENCIA deve aparecer exatamente uma vez
        from carros_sa.tools.sheets import MOTIVOS_PENDENCIA
        assert motivos_presentes == set(MOTIVOS_PENDENCIA.values()), (
            f"motivos em pendentes não cobrem os 3 quadrantes: {motivos_presentes}"
        )


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
            session.add(_laudo("L001"))
            # L002: sem fim_em → filtrado
            l2 = _lote("L002", modelo="Gol", fim_em=datetime.now() + timedelta(days=3))
            l2.fim_em = None
            session.add(l2)
            session.add(_avaliacao("L002"))
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

        assert n == 1, "só L001 deve entrar; L002 sem fim_em é filtrado"
        rows = mock_ws.update.call_args_list[0][0][0]
        # rows[0]=banner, rows[1]=header, rows[2]=L001 (único dado)
        assert len(rows) == 3
        idx_modelo = HEADER.index("Modelo")
        # Sem Lote ID na planilha, confirmamos via Modelo — só L001 (Fiesta) sobreviveu
        assert "Fiesta" in rows[2][idx_modelo]

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
            session.add(_laudo("L001"))

            # L002: ativo caro (lance > max) — ainda entra, o operador decide
            l2 = _lote("L002", modelo="Gol", lance_atual=50000)
            l2.fim_em = datetime.now() + timedelta(days=3)
            session.add(l2)
            session.add(_avaliacao("L002", preco_max=30000))
            session.add(_laudo("L002"))

            # L003: encerrado — NÃO entra
            l3 = _lote("L003", modelo="Compass", lance_atual=20000)
            l3.fim_em = datetime.now() - timedelta(days=1)
            session.add(l3)
            session.add(_avaliacao("L003", preco_max=50000))
            session.add(_laudo("L003"))
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
        idx_modelo = HEADER.index("Modelo")
        modelos_exportados = {rows[i][idx_modelo] for i in range(2, len(rows))}
        # L003 = Compass — encerrado, não entra; L001 = Fiesta e L002 = Gol sobrevivem
        assert not any("Compass" in m for m in modelos_exportados)
        assert any("Fiesta" in m for m in modelos_exportados)
        assert any("Gol" in m for m in modelos_exportados)


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


class TestCidadesFreteSheet:
    """Aba de cidades do raio operacional + frete por categoria por cidade."""

    def _separa_chamadas(self, mock_sh):
        """Devolve (ws_empresa, ws_pendentes, ws_cidades, ws_glossario) na ordem em que foram criados.
        Ordem bate com a sequência de _write_*_sheet em SheetsExporter.exportar."""
        # `worksheet(...)` levanta porque a aba ainda não existe → cai no add_worksheet,
        # que retorna a sequência de mocks dada.
        criados = [MagicMock(), MagicMock(), MagicMock(), MagicMock()]
        mock_sh.worksheet.side_effect = Exception("não existe")
        mock_sh.add_worksheet.side_effect = criados
        return criados

    def test_aba_cidades_existe_para_empresa_real(self):
        """Empresa real (`carros_uberlandia`) → aba `cidades_carros_uberlandia` é escrita."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001", empresa_id="carros_uberlandia"))
            session.add(_laudo("L001"))
            session.commit()

        mock_sh = MagicMock()
        mock_gc = MagicMock()
        mock_gc.open_by_key.return_value = mock_sh
        ws_empresa, ws_pendentes, ws_cidades, ws_glossario = self._separa_chamadas(mock_sh)

        with patch("gspread.service_account", return_value=mock_gc):
            exporter = _exporter()
            with Session(engine) as session:
                exporter.exportar("carros_uberlandia", session)

        # Ordem das chamadas a add_worksheet: empresa, pendentes, cidades, glossário
        titulos = [c.kwargs["title"] for c in mock_sh.add_worksheet.call_args_list]
        assert titulos == [
            "carros_uberlandia",
            "carros_uberlandia_pendentes",
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
        _, _, ws_cidades, _ = self._separa_chamadas(mock_sh)

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
        _, _, ws_cidades, _ = self._separa_chamadas(mock_sh)

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
                # `uberlandia_mg` não tem YAML — exportar deve completar sem erro
                n = exporter.exportar("uberlandia_mg", session)
        assert n == 1
