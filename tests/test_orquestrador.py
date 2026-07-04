"""Testes unitários do Orquestrador e funções auxiliares.

Sem Playwright real, sem LLM, sem rede. Todos os agentes são mockados.
SQLite in-memory para isolamento.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from carros_sa.models import (
    Avaria,
    AvaliacaoLote,
    CategoriaVeiculo,
    CustoLogistico,
    CustoReforma,
    ItemReforma,
    LaudoCache,
    LaudoEstruturado,
    Lote,
    SeveridadeAvaria,
    SinalMercado,
    StatusDocumentacao,
)
from carros_sa.orquestrador import (
    OrquestradorResult,
    ResultadoLote,
    _calcular_frete,
    _laudo_sem_pdf,
    _pdf_eh_laudo_valido,
    _pdf_persistente_path,
    _persistir_flags_no_lote,
    _pipeline_lote,
    _upsert_avaliacao,
    _upsert_laudo_cache,
    _upsert_lote,
)
from carros_sa.models import Avaliacao, PrecoReferenciaAA
from carros_sa.scraping.parsers import DetalheFlags


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _empresa():
    from carros_sa.tenancy import carregar_empresa
    return carregar_empresa("carros_uberlandia")


def _lote(lote_id: str = "L001", uf: str = "MG") -> Lote:
    return Lote(
        id=lote_id,
        leilao="autoavaliar",
        url=f"https://b2b.autoavaliar.com.br/avaliacoes/grp/{lote_id}/ford-fiesta",
        marca="Ford",
        modelo="Fiesta",
        ano=2013,
        km=45000,
        lance_atual=20000,
        origem_cidade="Uberlândia",
        origem_uf=uf,
        scraped_at=datetime.utcnow(),
    )


def _laudo_estruturado(severidade: SeveridadeAvaria = SeveridadeAvaria.LEVE) -> LaudoEstruturado:
    return LaudoEstruturado(
        avarias=[Avaria(parte="porta_dianteira_esquerda", severidade=severidade)],
        severidade_geral=severidade,
        motor_ok=True,
        documentacao=StatusDocumentacao.OK,
        categoria_veiculo=CategoriaVeiculo.HATCH,
        confidence=0.9,
    )


def _sinal_mercado() -> SinalMercado:
    return SinalMercado(
        fipe=30000,
        webmotors_mediana=27000,
        webmotors_p25=24000,
        n_anuncios_competidores=5,
        dias_giro_estimado=30,
    )


def _custo_reforma() -> CustoReforma:
    return CustoReforma(
        itens=[ItemReforma(descricao="porta leve", custo=400)],
        custo_total=400,
        range_min=300,
        range_max=500,
    )


def _avaliacao_obj(lote_id: str = "L001") -> Avaliacao:
    return Avaliacao(
        lote_id=lote_id,
        empresa_id="carros_uberlandia",
        preco_alvo=18000,
        preco_max=19000,
        score_roi=0.25,
        fator_risco=1.1,
        fator_liquidez=1.0,
        margem_aplicada=0.18,
        frete_incluso=800,
        reforma_estimada=400,
        taxas_leilao=1600,
        preco_giro=24000,
        preco_giro_fipe=24000,
        preco_giro_aa=None,
        fipe=28000,
        webmotors_mediana=25000,
        justificativa="Teste.",
    )


# ---------------------------------------------------------------------------
# Testes de _calcular_frete
# ---------------------------------------------------------------------------

class TestCalcularFrete:
    def test_frete_mesma_cidade_do_patio_e_zero(self):
        """Lote na mesma cidade do pátio (Uberlândia) → comprador busca, frete 0."""
        empresa = _empresa()
        lote = _lote(uf="MG")   # _lote usa origem_cidade="Uberlândia"
        frete = _calcular_frete(lote, empresa)
        assert frete.distancia_km == 0
        assert frete.frete_estimado == 0
        assert frete.destino_cidade == "Uberlândia"

    def test_frete_cidade_proxima_mesmo_uf(self):
        """Uberaba/MG (~100km de Uberlândia) usa distância haversine real."""
        empresa = _empresa()
        lote = _lote(uf="MG")
        lote.origem_cidade = "Uberaba"
        frete = _calcular_frete(lote, empresa)
        # 90 < haversine(Uberlândia, Uberaba) < 110
        assert 80 < frete.distancia_km < 120
        assert frete.frete_estimado > 0

    def test_frete_cidade_fora_do_dataset_cai_em_heuristica(self):
        """Cidade com nome inusitado (não achada no dataset IBGE) → fallback por UF."""
        empresa = _empresa()
        lote = _lote(uf="MG")
        lote.origem_cidade = "Cidade-Fictícia-XYZ"
        frete = _calcular_frete(lote, empresa)
        # Não achou → usa heurística antiga: mesma UF = 150km
        assert frete.distancia_km == 150
        assert frete.frete_estimado > 0

    def test_frete_uf_distante_maior_que_proxima(self):
        empresa = _empresa()
        lote_sp = _lote(uf="SP")
        lote_sp.origem_cidade = "São Paulo"
        lote_am = _lote(uf="AM")
        lote_am.origem_cidade = "Manaus"
        frete_sp = _calcular_frete(lote_sp, empresa)
        frete_am = _calcular_frete(lote_am, empresa)
        assert frete_sp.distancia_km < frete_am.distancia_km
        assert frete_sp.frete_estimado <= frete_am.frete_estimado

    def test_frete_categoria_explicita_usada(self):
        """Quando categoria é passada (caso de uso real do pipeline), usa-se
        ela direto — não re-deriva do nome do modelo."""
        empresa = _empresa()
        lote = _lote(uf="GO")
        lote.origem_cidade = "Goiânia"
        # Goiânia → Uberlândia haversine ~270km → faixa 0-300.
        # PICAPE=1500, HATCH=800.
        frete_picape = _calcular_frete(lote, empresa, categoria=CategoriaVeiculo.PICAPE)
        frete_hatch = _calcular_frete(lote, empresa, categoria=CategoriaVeiculo.HATCH)
        assert frete_picape.frete_estimado == 1500
        assert frete_hatch.frete_estimado == 800
        assert frete_picape.categoria_veiculo == CategoriaVeiculo.PICAPE

    def test_frete_fallback_usa_categoria_de_modelo_alinhada(self):
        """Sem categoria explícita, usa `_categoria_de_modelo` (mesma lista de
        calibracao_giro). Toro era OUTRO antes do fix; agora PICAPE.

        Faixa 0-300: PICAPE=1500, OUTRO=1200. Diferença prova alinhamento.
        """
        empresa = _empresa()
        lote = _lote(uf="MG")
        lote.modelo = "Toro Volcano 2.0 Diesel"
        lote.origem_cidade = "Araguari"  # ~40km do pátio (Uberlândia)
        frete = _calcular_frete(lote, empresa)
        assert frete.categoria_veiculo == CategoriaVeiculo.PICAPE
        assert frete.frete_estimado == 1500  # PICAPE 0-300, não OUTRO 1200


# ---------------------------------------------------------------------------
# Testes de _pipeline_lote
# ---------------------------------------------------------------------------

class TestPipelineLote:
    def _setup(self):
        engine = _engine()
        session = Session(engine)
        lote = _lote("L001", uf="MG")
        session.add(lote)
        session.commit()
        return engine, session, lote

    def test_early_exit_pula_laudo(self):
        """Lote com early_exit não deve chamar extrair_laudo."""
        engine, session, lote = self._setup()
        empresa = _empresa()

        mock_page = AsyncMock()
        mock_loop = MagicMock()

        # coletar_detalhe retorna body com lote reprovado estrutural
        body_com_reprovado = "REPROVADO ESTRUTURAL\nSTATUS DO LAUDO\nReprovado"
        mock_loop.run_until_complete.return_value = (body_com_reprovado, None)

        vision_client = MagicMock()

        with patch("carros_sa.orquestrador.coletar_detalhe", new_callable=AsyncMock) as mock_det, \
             patch("carros_sa.orquestrador.extrair_laudo") as mock_laudo:
            mock_det.return_value = (body_com_reprovado, None)
            mock_loop.run_until_complete.side_effect = lambda coro: (body_com_reprovado, None)

            import asyncio
            loop = asyncio.new_event_loop()
            try:
                res = loop.run_until_complete(
                    _pipeline_lote(lote, mock_page, vision_client, empresa, session, __import__("pathlib").Path("/tmp"))
                )
            finally:
                loop.close()

        # Pode ter early_exit ou ser avaliado — o importante é não chamar extrair_laudo se early_exit
        assert res.lote_id == "L001"

    def _inserir_avaliacao_existente(self, session):
        session.add(AvaliacaoLote(
            empresa_id="carros_uberlandia",
            lote_id="L001",
            preco_alvo=18000,
            preco_max=19000,
            score_roi=0.25,
            fator_risco=1.1,
            fator_liquidez=1.0,
            margem_aplicada=0.18,
            frete_incluso=800,
            reforma_estimada=400,
            taxas_leilao=1600,
            preco_giro=24000,
            preco_giro_fipe=24000,
            preco_giro_aa=None,
            justificativa="Avaliação existente.",
            criado_em=datetime.utcnow(),
        ))

    def _inserir_laudo(self, session, lote_id: str, confidence: float):
        from carros_sa.models import LaudoCache
        session.add(LaudoCache(
            lote_id=lote_id,
            avarias_json=[],
            severidade_geral="nenhuma",
            motor_ok=True,
            documentacao="ok",
            categoria_veiculo="outro",
            confidence=confidence,
            modelo_llm="gemini-flash",
            custo_usd=0.0,
            extraido_em=datetime.utcnow(),
        ))

    def test_lote_ja_avaliado_com_laudo_ok_nao_reavalia(self, tmp_path, monkeypatch):
        """Short-circuit: avaliação existente + laudo com conf≥0.6 + PDF on-disk
        + URL válida em raw_json → retorna sem rodar pipeline.

        Requer PDF on-disk desde 2026-05-09 — antes bastavam (avaliação + cache),
        mas em CI a pasta `data/laudos_pdfs/` ressuscita vazia entre runs e a
        auditoria estrita falhava por `pdf_ausente`. Agora `pdf_ok` é parte do
        critério de short-circuit pra alinhar com a invariante da auditoria.

        2026-05-10: também exige `raw_json.detalhe.laudo_pdf_url` válida — o
        bug DD3 zerava essa URL em todo re-scrape da listagem e o pipeline
        ficava preso pulando reprocessamento.
        """
        engine, session, lote = self._setup()
        empresa = _empresa()

        self._inserir_avaliacao_existente(session)
        self._inserir_laudo(session, "L001", confidence=0.9)
        # Persiste detalhe.laudo_pdf_url válido (host na allowlist do
        # `is_laudo_pdf_url`) — ó cobrir as 4 condições do short-circuit.
        atual = session.get(Lote, "L001")
        atual.raw_json = {"detalhe": {"laudo_pdf_url": "https://storage.googleapis.com/doc-b2b/laudo.pdf"}}
        session.add(atual)
        session.commit()

        # PDF on-disk mockado em tmp_path — `_pdf_persistente_path` é resolvido
        # via `_PDF_STORAGE_DIR` no módulo do orquestrador, fácil de patchar.
        monkeypatch.setattr("carros_sa.orquestrador._PDF_STORAGE_DIR", tmp_path)
        (tmp_path / "L001.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 200_000)

        mock_page = AsyncMock()
        vision_client = MagicMock()

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            with patch("carros_sa.orquestrador.coletar_detalhe", new_callable=AsyncMock) as mock_det:
                res = loop.run_until_complete(
                    _pipeline_lote(lote, mock_page, vision_client, empresa, session, __import__("pathlib").Path("/tmp"))
                )
                mock_det.assert_not_called()
        finally:
            loop.close()

        assert res.avaliado is True
        assert res.preco_alvo == 18000
        # Paridade P5b: `ResultadoLote.lucro_absoluto` populado no short-circuit.
        # Antes de 2026-07-04 o ranking do "Top da rodada" (cli.py + triagem_diaria)
        # usava `roi_pct` (intrinsic ROI) — divergia do sheets/audit/top que rankeam
        # por lucro absoluto (LESSONS.md/P5b). Agora carregado pelo helper canônico
        # `_lucro_absoluto_efetivo` (basis efetivo — mesmo do sheets).
        # Fixture: preco_giro=24000, score_roi=0.25, lance_atual (do _lote helper).
        # lance_atual=20000 > preco_alvo=18000 → zona apertada.
        # capital_alvo = 24000 / 1.25 = 19200; capital_ef = 19200 + (20000-18000) = 21200
        # score_ef = (24000 - 21200) / 21200 ≈ 0.1321
        # lucro_ef = 24000 * 0.1321 / 1.1321 ≈ 2800
        assert res.lucro_absoluto is not None
        assert 2500 <= res.lucro_absoluto <= 3100, (
            f"Esperava ~R$ 2800 de lucro absoluto efetivo (zona apertada), "
            f"obtido R$ {res.lucro_absoluto}"
        )

    def test_lote_ja_avaliado_com_laudo_ok_mas_pdf_ausente_reavalia(self, tmp_path, monkeypatch):
        """Short-circuit NÃO dispara quando o PDF sumiu do disco (cenário CI).

        Em runs subsequentes do workflow, state/db pode falhar ao restaurar
        `data/laudos_pdfs/<lote>.pdf` (network glitch, branch corrompida).
        Antes do fix de 2026-05-09, o lote tinha cache>=0.6 + AvaliacaoLote, o
        short-circuit pulava o pipeline, e o `auditar_laudos --strict` no fim
        do workflow falhava por `pdf_ausente`. Defesa em profundidade: pipeline
        re-roda pra re-baixar o PDF.
        """
        engine, session, lote = self._setup()
        empresa = _empresa()

        self._inserir_avaliacao_existente(session)
        self._inserir_laudo(session, "L001", confidence=0.9)
        session.commit()

        # tmp_path EXISTE mas o PDF não foi escrito.
        monkeypatch.setattr("carros_sa.orquestrador._PDF_STORAGE_DIR", tmp_path)

        mock_page = AsyncMock()
        vision_client = MagicMock()

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            with patch(
                "carros_sa.orquestrador.coletar_detalhe",
                new_callable=AsyncMock,
                return_value=("", None),
            ) as mock_det:
                loop.run_until_complete(
                    _pipeline_lote(lote, mock_page, vision_client, empresa, session, __import__("pathlib").Path("/tmp"))
                )
                # coletar_detalhe DEVE ter sido chamado — o pipeline precisa
                # voltar pra re-baixar o PDF que sumiu.
                mock_det.assert_called_once()
        finally:
            loop.close()

    def test_lote_ja_avaliado_com_pdf_e_cache_mas_url_ausente_reavalia(self, tmp_path, monkeypatch):
        """Short-circuit NÃO dispara quando `raw_json.detalhe.laudo_pdf_url`
        está ausente/inválido — mesmo com avaliação + laudo cache + PDF on-disk.

        Cenário do bug DD3 (2026-05-10): 95 lotes ativos no DB tinham PDF
        baixado e cache forte, mas `_upsert_lote` zerava `detalhe` em todo
        re-scrape da listagem. Sem este check, o pipeline pulava `coletar_detalhe`
        e a URL nunca voltava — coluna "Ver laudo" da planilha sumia. Defesa
        em profundidade: pipeline re-roda pra repopular a URL via
        `_persistir_flags_no_lote`.
        """
        engine, session, lote = self._setup()
        empresa = _empresa()

        self._inserir_avaliacao_existente(session)
        self._inserir_laudo(session, "L001", confidence=0.9)

        # Simula o estado pós-bug: PDF on-disk + cache forte, mas raw_json
        # SEM detalhe (zerado por re-scrape da listagem antes do fix DD3).
        atual = session.get(Lote, "L001")
        atual.raw_json = {"loja": "carbel", "detalhe": None}
        session.add(atual)
        session.commit()

        monkeypatch.setattr("carros_sa.orquestrador._PDF_STORAGE_DIR", tmp_path)
        (tmp_path / "L001.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 200_000)

        mock_page = AsyncMock()
        vision_client = MagicMock()

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            with patch(
                "carros_sa.orquestrador.coletar_detalhe",
                new_callable=AsyncMock,
                return_value=("", None),
            ) as mock_det:
                loop.run_until_complete(
                    _pipeline_lote(lote, mock_page, vision_client, empresa, session, __import__("pathlib").Path("/tmp"))
                )
                # coletar_detalhe DEVE ter sido chamado — pipeline precisa
                # voltar pra repopular a URL.
                mock_det.assert_called_once()
        finally:
            loop.close()

    def test_lote_ja_avaliado_mas_laudo_pendente_reavalia(self):
        """Avaliação existente + laudo conf<0.6 → pipeline RE-executa (re-scrapa detalhe).

        Proteção contra o bug de abril/2026: o retry `--somente-laudo-pendente`
        escolhia lotes com conf<0.6, mas `_pipeline_lote` saía antes do
        `coletar_detalhe` porque AvaliacaoLote existia. 104 lotes ativos
        ficavam travados em confidence=0.5 para sempre.
        """
        engine, session, lote = self._setup()
        empresa = _empresa()

        self._inserir_avaliacao_existente(session)
        # Laudo placeholder de `_laudo_sem_pdf` — é a situação clássica do 104.
        self._inserir_laudo(session, "L001", confidence=0.5)
        session.commit()

        mock_page = AsyncMock()
        vision_client = MagicMock()

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            with patch(
                "carros_sa.orquestrador.coletar_detalhe",
                new_callable=AsyncMock,
                return_value=("", None),
            ) as mock_det:
                loop.run_until_complete(
                    _pipeline_lote(lote, mock_page, vision_client, empresa, session, __import__("pathlib").Path("/tmp"))
                )
                # coletar_detalhe DEVE ter sido chamado — a essência do fix.
                mock_det.assert_called_once()
        finally:
            loop.close()

    def test_lote_ja_avaliado_sem_laudo_reavalia(self):
        """Avaliação existente + NENHUM LaudoCache → re-executa pipeline.
        (caso dos lotes mais antigos onde o laudo cache foi derrubado mas a
        avaliação ficou.)"""
        engine, session, lote = self._setup()
        empresa = _empresa()

        self._inserir_avaliacao_existente(session)
        session.commit()

        mock_page = AsyncMock()
        vision_client = MagicMock()

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            with patch(
                "carros_sa.orquestrador.coletar_detalhe",
                new_callable=AsyncMock,
                return_value=("", None),
            ) as mock_det:
                loop.run_until_complete(
                    _pipeline_lote(lote, mock_page, vision_client, empresa, session, __import__("pathlib").Path("/tmp"))
                )
                mock_det.assert_called_once()
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Testes de persistência
# ---------------------------------------------------------------------------

class TestValidadorPdfLaudo:
    """Proteção contra o bug de abril/2026 que baixou 'Relatório de Transparência
    Salarial' em 100% dos lotes por causa de seletor JS frouxo no scraper."""

    def test_pdf_laudo_real_eh_valido(self):
        """Gold test: o PDF do Fiesta real é reconhecido como laudo de carro."""
        from pathlib import Path
        pdf = Path(__file__).resolve().parent.parent / "data" / "laudos_amostra" / "21854782_fiesta.pdf"
        if not pdf.exists():
            pytest.skip(f"PDF gold test não existe em {pdf}")
        assert _pdf_eh_laudo_valido(pdf) is True

    def test_pdf_inexistente_nao_e_valido(self, tmp_path):
        assert _pdf_eh_laudo_valido(tmp_path / "nao_existe.pdf") is False

    def test_pdf_muito_pequeno_nao_e_valido(self, tmp_path):
        """Página institucional de 1KB não tem o conteúdo de um laudo real."""
        p = tmp_path / "minusculo.pdf"
        p.write_bytes(b"%PDF-1.4\n" + b"x" * 200)
        assert _pdf_eh_laudo_valido(p) is False

    def test_pdf_transparencia_salarial_e_rejeitado(self, tmp_path):
        """Se o conteúdo menciona 'Transparência Salarial' na 1a página é denylist."""
        # Cria um PDF mínimo válido com o texto do falso positivo
        try:
            from reportlab.pdfgen import canvas
        except Exception:
            pytest.skip("reportlab não instalado — pular teste de denylist")
        p = tmp_path / "transparencia.pdf"
        c = canvas.Canvas(str(p))
        c.drawString(100, 750, "Relatório de Transparência Salarial - 2o Semestre")
        c.drawString(100, 730, "Igualdade Salarial entre Mulheres e Homens")
        c.showPage()
        c.save()
        assert _pdf_eh_laudo_valido(p) is False

    def test_pdf_path_persistente_usa_storage_dir(self):
        path = _pdf_persistente_path("L123")
        assert path.name == "L123.pdf"
        assert path.parent.name == "laudos_pdfs"
        assert path.parent.exists()  # função cria o dir


class TestPersistencia:
    def test_upsert_lote_novo(self):
        from carros_sa.models import LoteRaw
        engine = _engine()
        with Session(engine) as session:
            lote_raw = LoteRaw(
                lote_id="L999",
                leilao="autoavaliar",
                url="https://b2b.autoavaliar.com.br/avaliacoes/g/L999/ford-fiesta",
                marca="Ford",
                modelo="Fiesta",
                ano=2013,
                km=45000,
                lance_atual=20000,
                origem_cidade="Uberlândia",
                origem_uf="MG",
            )
            lote = _upsert_lote(lote_raw, session)
            session.commit()

            persistido = session.get(Lote, "L999")
            assert persistido is not None
            assert persistido.marca == "Ford"

    def test_upsert_lote_preserva_detalhe_em_re_scrape(self):
        """Re-scrape de listagem NÃO pode zerar `raw_json.detalhe` da coleta anterior.

        Bug raiz que escondia 95/187 lotes ativos com URL inválida em 2026-05-10:
        `_upsert_lote` reconstruía `raw_json` a partir do LoteRaw da listagem
        (sem `detalhe`) e sobrescrevia o existente — só `loja` era preservada.
        Combinado com o short-circuit do `_pipeline_lote` (que pulava
        `coletar_detalhe` quando avaliação + cache + PDF existiam), a URL do
        laudo persistida em `raw_json.detalhe.laudo_pdf_url` ficava None pra
        sempre e a planilha perdia o link "Ver laudo".
        """
        from carros_sa.models import LoteRaw
        engine = _engine()
        with Session(engine) as session:
            # 1ª passagem: ingest da listagem
            lote_raw = LoteRaw(
                lote_id="L_PRES",
                leilao="autoavaliar",
                url="https://b2b.autoavaliar.com.br/avaliacoes/g/L_PRES/honda-fit",
                marca="Honda",
                modelo="Fit",
                ano=2018,
                km=50000,
                lance_atual=30000,
                origem_cidade="Uberlândia",
                origem_uf="MG",
            )
            _upsert_lote(lote_raw, session)
            session.commit()

            # 2ª etapa: pipeline coleta detalhe + persiste a URL no raw_json
            persistido = session.get(Lote, "L_PRES")
            raw = dict(persistido.raw_json or {})
            raw["detalhe"] = {"laudo_pdf_url": "https://storage.googleapis.com/doc-b2b/laudo-honda.pdf"}
            raw["body_text_sample"] = "TEXTO DO DETALHE..."
            persistido.raw_json = raw
            session.add(persistido)
            session.commit()

            # 3ª passagem: re-scrape da listagem (cron diário). Antes do fix,
            # esse upsert ZERAVA o detalhe.
            _upsert_lote(lote_raw, session)
            session.commit()

            atual = session.get(Lote, "L_PRES")
            assert atual.raw_json.get("detalhe") == {
                "laudo_pdf_url": "https://storage.googleapis.com/doc-b2b/laudo-honda.pdf"
            }, "Re-scrape de listagem zerou detalhe.laudo_pdf_url — bug DD3"
            assert atual.raw_json.get("body_text_sample") == "TEXTO DO DETALHE..."

    def test_upsert_avaliacao_nova(self):
        engine = _engine()
        with Session(engine) as session:
            lote = _lote("L777")
            session.add(lote)
            session.commit()

            av = _avaliacao_obj("L777")
            _upsert_avaliacao(av, "carros_uberlandia", session)
            session.commit()

            persistida = session.exec(
                select(AvaliacaoLote)
                .where(AvaliacaoLote.lote_id == "L777")
            ).first()
            assert persistida is not None
            assert persistida.preco_alvo == 18000


class TestUpsertLaudoCacheTentativas:
    """Circuit-breaker: cada extração com confidence<0.6 incrementa o
    contador; uma extração ≥0.6 zera. Filtro de retry usa pra parar de
    queimar LLM em lotes onde o problema é input, não extrator."""

    def _persistir(self, session, lote_id: str, confidence: float):
        laudo = _laudo_estruturado()
        laudo.confidence = confidence
        _upsert_laudo_cache(lote_id, laudo, session)
        session.commit()
        return session.get(LaudoCache, lote_id)

    def test_primeira_extracao_fraca_seta_um(self):
        engine = _engine()
        with Session(engine) as session:
            session.add(_lote("L_T1"))
            session.commit()
            cache = self._persistir(session, "L_T1", confidence=0.3)
            assert cache.tentativas_extracao == 1

    def test_segunda_extracao_fraca_incrementa(self):
        engine = _engine()
        with Session(engine) as session:
            session.add(_lote("L_T2"))
            session.commit()
            self._persistir(session, "L_T2", confidence=0.3)
            cache = self._persistir(session, "L_T2", confidence=0.3)
            assert cache.tentativas_extracao == 2

    def test_extracao_forte_zera_contador(self):
        engine = _engine()
        with Session(engine) as session:
            session.add(_lote("L_T3"))
            session.commit()
            self._persistir(session, "L_T3", confidence=0.3)
            self._persistir(session, "L_T3", confidence=0.3)
            cache = self._persistir(session, "L_T3", confidence=0.85)
            assert cache.tentativas_extracao == 0

    def test_extracao_forte_de_cara_fica_zero(self):
        engine = _engine()
        with Session(engine) as session:
            session.add(_lote("L_T4"))
            session.commit()
            cache = self._persistir(session, "L_T4", confidence=0.9)
            assert cache.tentativas_extracao == 0


# ---------------------------------------------------------------------------
# Testes de _persistir_flags_no_lote (workstream N)
# ---------------------------------------------------------------------------

class TestPersistirFlagsNoLote:
    def test_persiste_detalhe_e_precos_aa_no_lote(self):
        """Flags com preço AA + FIPE% viram colunas first-class no Lote + histórico."""
        engine = _engine()
        with Session(engine) as session:
            lote = _lote("L555")
            session.add(lote)
            session.commit()

            flags = DetalheFlags(
                specs={"ANO": "2013"},
                status_laudo="Laudo aprovado",
                preco_referencia_aa=31_000,
                fipe_pct_lance_minimo=34,
            )
            _persistir_flags_no_lote(lote, flags, session)
            session.commit()

            atual = session.get(Lote, "L555")
            assert atual.preco_referencia_aa == 31_000
            assert atual.fipe_pct_lance_minimo == 34
            assert atual.raw_json["detalhe"]["preco_referencia_aa"] == 31_000
            # Histórico recebeu 1 entrada
            historico = session.exec(select(PrecoReferenciaAA)).all()
            assert len(historico) == 1
            assert historico[0].preco == 31_000
            assert historico[0].origem_lote_id == "L555"

    def test_persiste_sem_precos_aa_nao_cria_historico(self):
        engine = _engine()
        with Session(engine) as session:
            lote = _lote("L556")
            session.add(lote)
            session.commit()

            flags = DetalheFlags(specs={}, preco_referencia_aa=None, fipe_pct_lance_minimo=None)
            _persistir_flags_no_lote(lote, flags, session)
            session.commit()

            atual = session.get(Lote, "L556")
            assert atual.preco_referencia_aa is None
            assert session.exec(select(PrecoReferenciaAA)).all() == []


# ---------------------------------------------------------------------------
# Testes do _laudo_sem_pdf com flags estruturais (workstream N)
# ---------------------------------------------------------------------------

class TestLaudoSemPdfComFlags:
    def test_flags_reprovado_estrutural_promove_severidade(self):
        """Scraper já sabe que é estrutural → marcamos sem precisar do PDF."""
        flags = DetalheFlags(specs={}, itens_reprovados=["REPROVADO ESTRUTURAL"])
        laudo = _laudo_sem_pdf(flags)
        assert laudo.severidade_geral == SeveridadeAvaria.ESTRUTURAL
        assert laudo.motor_ok is False
        assert len(laudo.avarias) == 1
        assert laudo.avarias[0].severidade == SeveridadeAvaria.ESTRUTURAL
        assert laudo.confidence == 0.55  # um chão a mais que 0.5 puro

    def test_flags_sem_estrutural_mantem_neutro(self):
        flags = DetalheFlags(specs={}, status_laudo="Laudo aprovado", itens_reprovados=[])
        laudo = _laudo_sem_pdf(flags)
        assert laudo.severidade_geral == SeveridadeAvaria.NENHUMA
        assert laudo.avarias == []
        assert laudo.confidence == 0.5

    def test_sem_flags_retorna_conservador(self):
        laudo = _laudo_sem_pdf(None)
        assert laudo.severidade_geral == SeveridadeAvaria.NENHUMA
        assert laudo.confidence == 0.5
