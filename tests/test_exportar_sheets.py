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
    _col_letter,
    _lucro_absoluto_efetivo,
    _lucro_absoluto_no_alvo,
    _score_roi_efetivo,
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

class TestLucroAbsolutoNoAlvo:
    """Fórmula exata do lucro absoluto no preço-alvo:
        score_roi = lucro / capital_alvo  ⇒  capital_alvo = preco_giro / (1 + score_roi)
        lucro = preco_giro − capital_alvo = preco_giro × score_roi / (1 + score_roi)

    Substitui a aproximação anterior `score_roi × preco_alvo`, que subestimava
    sistematicamente em ~10% (capital_alvo > preco_alvo por causa de
    reforma/frete/taxas/custo_op).
    """

    def test_lucro_exato_polo_track(self):
        # Polo Track real: preco_giro=68000, score_roi=0.576 → lucro≈24864
        av = _avaliacao(preco_giro=68000, score_roi=0.576)
        lucro = _lucro_absoluto_no_alvo(av)
        # 68000 × 0.576 / 1.576 ≈ 24852 (±2 por rounding)
        assert lucro == pytest.approx(24852, abs=2)

    def test_score_zero_retorna_zero(self):
        av = _avaliacao(score_roi=0.0)
        assert _lucro_absoluto_no_alvo(av) == 0

    def test_score_negativo_retorna_zero(self):
        # Lote com custos > preco_giro (deveria ter sido descartado upstream).
        av = _avaliacao(score_roi=-0.1)
        assert _lucro_absoluto_no_alvo(av) == 0

    def test_preco_giro_zero_retorna_zero(self):
        av = _avaliacao(preco_giro=0, score_roi=0.3)
        assert _lucro_absoluto_no_alvo(av) == 0

    def test_score_roi_none_nao_quebra(self):
        """Defesa contra registros antigos com NULL — `None <= 0` levantava
        TypeError e quebrava planilha inteira. Agora cai no caminho de zero."""
        av = _avaliacao(score_roi=0.3)
        av.score_roi = None
        assert _lucro_absoluto_no_alvo(av) == 0


class TestScoreRoiEfetivo:
    """ROI honesto: quando lance_atual > preco_alvo, o capital empatado real
    cresce e o ROI efetivo cai. Quando lance_atual ≤ preco_alvo, mantém o
    score_roi original (entrada pelo alvo é factível)."""

    def test_lance_abaixo_do_alvo_devolve_score_roi(self):
        av = _avaliacao(score_roi=0.4, preco_giro=50000)
        # lance abaixo do alvo (default=25000) → score_efetivo = score_roi
        assert _score_roi_efetivo(av, 20000) == pytest.approx(0.4, abs=1e-9)

    def test_lance_no_alvo_devolve_score_roi(self):
        av = _avaliacao(score_roi=0.4, preco_giro=50000)
        assert _score_roi_efetivo(av, 25000) == pytest.approx(0.4, abs=1e-9)

    def test_lance_acima_do_alvo_reduz_score(self):
        # capital_alvo = 50000 / 1.4 ≈ 35714. Lance 30k > alvo 25k:
        # capital_ef = 35714 + (30000-25000) = 40714. score_ef = (50000-40714)/40714 ≈ 0.228
        av = _avaliacao(score_roi=0.4, preco_giro=50000)
        score_ef = _score_roi_efetivo(av, 30000)
        assert score_ef < 0.4
        assert score_ef == pytest.approx(0.228, abs=0.01)

    def test_lance_none_devolve_score_roi(self):
        av = _avaliacao(score_roi=0.4, preco_giro=50000)
        assert _score_roi_efetivo(av, None) == pytest.approx(0.4, abs=1e-9)

    def test_lance_destruidor_zera_score(self):
        # Lance absurdo > preco_giro torna capital_ef > preco_giro → ROI<0,
        # cai pra 0 (não exibimos ROI negativo realista — operador já tem o
        # sinal de "Caro demais").
        av = _avaliacao(score_roi=0.4, preco_giro=50000, preco_max=30000)
        score_ef = _score_roi_efetivo(av, 100000)
        assert score_ef <= 0.0  # capital_ef >= preco_giro

    def test_score_roi_none_nao_quebra(self):
        av = _avaliacao(score_roi=0.4, preco_giro=50000)
        av.score_roi = None
        assert _score_roi_efetivo(av, 30000) == 0.0

    def test_preco_alvo_none_nao_quebra(self):
        """Defesa contra registros antigos com NULL: `preco_alvo` é
        non-nullable no schema atual, mas migrações antigas podem ter
        deixado lixo. `lance_atual - None` levantava TypeError silencioso
        que quebrava a planilha inteira (score_efetivo é chamado por linha).
        Garantia: não levanta. O valor numérico devolvido pode ser negativo
        (display em sheets/audit suprime via filtro de viabilidade), mas
        importante é não derrubar a planilha.
        """
        av = _avaliacao(score_roi=0.4, preco_giro=50000)
        av.preco_alvo = None  # type: ignore[assignment]
        score_ef = _score_roi_efetivo(av, 30000)
        # Antes do fix: TypeError. Depois: float (não importa o sinal).
        assert isinstance(score_ef, float)


class TestLucroAbsolutoEfetivo:
    """Espelha _lucro_absoluto_no_alvo mas usando o capital efetivo (lance > alvo)."""

    def test_lance_abaixo_do_alvo_igual_a_lucro_no_alvo(self):
        av = _avaliacao(score_roi=0.3, preco_giro=50000)
        assert _lucro_absoluto_efetivo(av, 20000) == _lucro_absoluto_no_alvo(av)

    def test_lance_acima_do_alvo_reduz_lucro(self):
        av = _avaliacao(score_roi=0.4, preco_giro=50000)
        lucro_alvo = _lucro_absoluto_no_alvo(av)
        lucro_ef = _lucro_absoluto_efetivo(av, 30000)
        assert lucro_ef > 0
        assert lucro_ef < lucro_alvo

    def test_lance_destruidor_zera_lucro(self):
        av = _avaliacao(score_roi=0.4, preco_giro=50000)
        assert _lucro_absoluto_efetivo(av, 100000) == 0


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

    def test_exportar_fipe_em_coluna_dedicada(self):
        """Coluna FIPE (R$) renderiza `av.fipe` direto. Não depende do laudo —
        fica visível mesmo quando o lote está com 'LAUDO NÃO ANALISADO'.
        Registros sem fipe (pré-workstream K) caem pro placeholder '—'."""
        engine = _engine_mem()
        with Session(engine) as session:
            # L001: avaliação com fipe preenchido + laudo ok
            session.add(_lote("L001"))
            av1 = _avaliacao("L001")
            av1.fipe = 32000
            session.add(av1)
            session.add(_laudo("L001"))
            # L002: sem laudo (não analisado), mas com fipe — deve aparecer
            session.add(_lote("L002", modelo="Gol", lance_atual=18000))
            av2 = _avaliacao("L002")
            av2.fipe = 25000
            session.add(av2)
            # L003: avaliação sem fipe (registro antigo, NULL) → '—'
            session.add(_lote("L003", modelo="Onix", lance_atual=22000))
            av3 = _avaliacao("L003")
            av3.fipe = None
            session.add(av3)
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
                exporter.exportar("uberlandia_mg", session)

        rows = mock_ws.update.call_args_list[0][0][0]
        idx_fipe = HEADER.index("FIPE (R$)")
        idx_modelo = HEADER.index("Modelo")
        # Mapeia modelo → fipe
        fipe_por_modelo = {rows[i][idx_modelo]: rows[i][idx_fipe] for i in range(2, len(rows))}
        assert fipe_por_modelo["Fiesta"] == 32000
        assert fipe_por_modelo["Gol"] == 25000      # FIPE aparece mesmo sem laudo
        assert fipe_por_modelo["Onix"] == "—"       # registro antigo sem fipe

    def test_exportar_marca_e_modelo_em_colunas_separadas(self):
        """Marca e Modelo são colunas dedicadas — operador filtra por fabricante
        sem depender de string composta. Modelo cell guarda só `lote.modelo`."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", marca="Ford", modelo="Fiesta"))
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
        idx_marca = HEADER.index("Marca")
        idx_modelo = HEADER.index("Modelo")
        assert rows[2][idx_marca] == "Ford"
        assert rows[2][idx_modelo] == "Fiesta"

    def test_exportar_ranking_por_roi_anualizado_entre_viaveis(self):
        """Entre lotes viáveis com laudo analisado, rank é por ROI anualizado desc.

        Bug histórico: planilha ranqueava por folga absoluta `preco_max - lance_atual`
        (ver sheets.py:142 antes do fix de 2026-05). CLI `top` ranqueia por ROI
        anualizado; resultado: operador via duas ordens conflitantes da mesma
        fonte. Métrica única agora.

        Cenário deste teste:
          - Lote BARATO: lance baixo (folga grande) mas ROI baixo → deve vir DEPOIS
          - Lote LUCRATIVO: lance alto (folga pequena) mas ROI alto → deve vir ANTES
        """
        engine = _engine_mem()
        with Session(engine) as session:
            # BARATO: lance < preco_alvo (entrada factível pelo alvo). ROI = 10% →
            # score_efetivo = 0.10 → 0.10 × 365 / 90 = 40.6%
            session.add(_lote("L_BARATO", marca="VW", modelo="Gol", lance_atual=5000))
            av_b = _avaliacao(
                "L_BARATO", score_roi=0.10, preco_giro=35000, preco_max=30000,
            )
            av_b.preco_alvo = 25000  # lance 5000 < alvo 25000 (zona normal)
            av_b.dias_giro_estimado = 90
            session.add(av_b)
            session.add(_laudo("L_BARATO"))
            # LUCRATIVO: lance < preco_alvo (entrada factível pelo alvo). ROI = 50% →
            # score_efetivo = 0.50 → 0.50 × 365 / 90 = 202.8%
            session.add(_lote("L_LUCRATIVO", marca="VW", modelo="Polo", lance_atual=70000))
            av_l = _avaliacao(
                "L_LUCRATIVO", score_roi=0.50, preco_giro=120000, preco_max=75000,
            )
            av_l.preco_alvo = 72000  # lance 70000 < alvo 72000 (zona normal, folga pra alvo)
            av_l.dias_giro_estimado = 90
            session.add(av_l)
            session.add(_laudo("L_LUCRATIVO"))
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
        idx_modelo = HEADER.index("Modelo")
        # rows[0]=banner, rows[1]=header, rows[2]=rank 1, rows[3]=rank 2
        assert rows[2][idx_modelo] == "Polo", (
            f"Esperava L_LUCRATIVO no rank 1 (ROI maior), veio {rows[2][idx_modelo]}"
        )
        assert rows[3][idx_modelo] == "Gol"

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

    def test_exportar_sem_laudo_marca_nao_capturado(self):
        """Lote sem LaudoCache é exportado mas sinaliza "LAUDO NÃO CAPTURADO" e zera
        campos numéricos derivados do laudo — operador não pode dar lance sem conferir
        primeiro (feedback usuário 2026-04-18). Renomeado de "NÃO ANALISADO" pra
        "NÃO CAPTURADO" porque na prática o laudo existe no AA quase sempre — quem
        falhou foi o scraper (modal lazy / 429), não o anunciante."""
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
        assert "LAUDO NÃO CAPTURADO" in data_row[HEADER.index("Situação")]
        # Numéricos derivados de um laudo vazio viram traço: piso de R$ 1k em
        # "Reforma" + ROI/preço-alvo calculados com reforma=piso seriam
        # tudo chute, então a planilha esconde.
        assert data_row[HEADER.index("Reforma (R$)")] == "—"
        assert data_row[HEADER.index("Lance Máximo (R$)")] == "—"
        assert data_row[HEADER.index("ROI alvo (%)")] == "—"
        assert data_row[HEADER.index("Lucro (R$)")] == "—"

    def test_exportar_laudo_fallback_confidence_baixa_marca_nao_capturado(self):
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
        assert "LAUDO NÃO CAPTURADO" in data_row[HEADER.index("Situação")]
        assert data_row[HEADER.index("Reforma (R$)")] == "—"

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
        assert "LAUDO NÃO CAPTURADO" not in data_row[HEADER.index("Situação")]
        assert data_row[HEADER.index("Reforma (R$)")] == 3000

    def test_exportar_horizonte_exibicao_corta_lotes_muito_futuros(self):
        """Quando `horizonte_exibicao_dias=N` é passado, lotes com fim > agora+N dias
        ficam fora da planilha. Regressão do feedback 2026-04-23: a gente
        passou a coletar o pipeline inteiro (sem cortar no scraper), então o
        exporter é quem define a janela que o usuário enxerga."""
        engine = _engine_mem()
        agora = datetime.now()
        with Session(engine) as session:
            session.add(_lote("L_HOJE", fim_em=agora + timedelta(hours=4)))
            session.add(_lote("L_DAQUI_15D", fim_em=agora + timedelta(days=15)))
            session.add(_lote("L_DAQUI_45D", fim_em=agora + timedelta(days=45)))
            session.add(_avaliacao("L_HOJE"))
            session.add(_avaliacao("L_DAQUI_15D"))
            session.add(_avaliacao("L_DAQUI_45D"))
            session.add(_laudo("L_HOJE"))
            session.add(_laudo("L_DAQUI_15D"))
            session.add(_laudo("L_DAQUI_45D"))
            session.commit()

        mock_ws = MagicMock()
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_gc = MagicMock()
        mock_gc.open_by_key.return_value = mock_sh

        with patch("gspread.service_account", return_value=mock_gc):
            exporter = _exporter()
            with Session(engine) as session:
                n = exporter.exportar(
                    "uberlandia_mg", session, horizonte_exibicao_dias=30,
                )

        # L_HOJE e L_DAQUI_15D passam; L_DAQUI_45D fora da janela.
        assert n == 2

    def test_exportar_horizonte_exibicao_none_mantem_tudo(self):
        """`horizonte_exibicao_dias=None` (default) NÃO filtra por janela — só
        os filtros antigos (fim_em=None, encerrado) continuam ativos."""
        engine = _engine_mem()
        agora = datetime.now()
        with Session(engine) as session:
            session.add(_lote("L_HOJE", fim_em=agora + timedelta(hours=4)))
            session.add(_lote("L_LONGE", fim_em=agora + timedelta(days=90)))
            session.add(_avaliacao("L_HOJE"))
            session.add(_avaliacao("L_LONGE"))
            session.add(_laudo("L_HOJE"))
            session.add(_laudo("L_LONGE"))
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

    def test_exportar_inviavel_oculta_lucro_roi_tese(self):
        """Lote '✗ Caro demais' (lance > preco_max) NÃO deve mostrar Lucro,
        ROI anualizado nem Tese — esses números pressupõem comprar pelo preço-alvo
        (que é menor que o lance atual nesse caso, cenário fantasioso). Mantemos
        Lance Máximo + FIPE + Reforma pra o operador entender o descarte.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            # lance_atual=35k > preco_max=30k → "✗ Caro demais"
            session.add(_lote("L001", lance_atual=35_000))
            session.add(_avaliacao("L001", preco_max=30_000, score_roi=0.8))
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
        row = rows[2]
        # Substring match: como o lote default não tem PDF/URL persistidos,
        # o exporter sufixa `(laudo: PDF ausente + URL inválida)` por simetria
        # com `✓ Viável (laudo: ...)` (R.4 — motivo específico em ambos os
        # ramos). O foco do teste é a supressão de numéricos especulativos.
        situacao = row[HEADER.index("Situação")]
        assert "✗ Caro demais" in situacao
        # Numéricos especulativos suprimidos
        assert row[HEADER.index("Lucro (R$)")] == "—"
        assert row[HEADER.index("ROI alvo (%)")] == "—"
        assert row[HEADER.index("Tese")] == "—"
        # Mas contexto pra triagem manual continua visível
        assert row[HEADER.index("Lance Máximo (R$)")] == 30_000
        assert row[HEADER.index("Reforma (R$)")] == 3_000
        assert row[HEADER.index("FIPE (R$)")] == "—"  # _avaliacao default não passa fipe

    def test_exportar_url_como_hyperlink_clicavel(self):
        """A coluna Anúncio deve virar =HYPERLINK(url, "Abrir anúncio") pra célula ficar curta e clicável."""
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
        idx_laudo = HEADER.index("Laudo")
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
        idx_laudo = HEADER.index("Laudo")
        assert rows[2][idx_laudo] == "—"

    def test_resize_encolhe_grade_pra_len_header(self):
        """Slim-down do HEADER (27→15 cols em abr/2026) deixou colunas órfãs
        no Sheets — `ws.clear()` não derruba colunas, só apaga valores no
        range ativo. Resultado observado pelo operador: 'Laudo (PDF)'
        zumbi em Z mostrando '—' pra todo mundo mesmo com lotes '✓ Viável'.
        Fix: `ws.resize(cols=len(HEADER))` antes do update derruba qualquer
        coluna além do HEADER atual no servidor."""
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

        # Resize precisa ter sido chamado com cols=len(HEADER) ANTES do clear,
        # senão a aba antiga (27 cols) mantém colunas P→AA congeladas.
        assert mock_ws.resize.called, "ws.resize não foi chamado — colunas órfãs do HEADER antigo continuam no sheet"
        kwargs = mock_ws.resize.call_args.kwargs
        assert kwargs.get("cols") == len(HEADER)
        # Resize antes de clear: ordem importa pra evitar perda transitória de dados
        # (clear de grade larga + resize depois é equivalente, mas resize→clear
        # garante que o estado intermediário visto por leitores concorrentes seja
        # já o estado final estreito).
        call_order = [c[0] for c in mock_ws.method_calls]
        assert call_order.index("resize") < call_order.index("clear")

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
        idx_url = HEADER.index("Anúncio")
        assert rows[2][idx_url] == "—"

    def test_reaplica_formato_numerico_em_reforma_e_lance_maximo(self):
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

    def test_exportar_roi_alvo_eh_score_roi_intrinsic(self):
        """ROI alvo = score_roi × 100 (cru, sem anualizar).

        Antes (até 2026-05-08) era 'ROI anualizado (%)' = score_roi × 365 / dias_giro
        com floor 60d. Operador pediu pra ver o ROI cru da operação no preço-alvo —
        anualizar dependia de calibração de giro frequentemente otimista. Ranking
        interno continua usando o anualizado (key do sorted), mas a coluna exibe
        o intrinsic puro.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=20000))
            # score_roi=0.3 conhecido
            av = _avaliacao("L001", preco_giro=35000, preco_max=25000, score_roi=0.3)
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
        idx_roi = HEADER.index("ROI alvo (%)")
        roi_val = rows[2][idx_roi]
        # 0.3 × 100 = 30.0% (ROI cru intrinsic, não anualizado)
        assert roi_val == pytest.approx(30.0, abs=0.1)


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
