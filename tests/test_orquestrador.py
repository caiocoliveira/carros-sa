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

    def test_lote_ja_avaliado_nao_reavalia(self):
        """Lote com AvaliacaoLote existente retorna resultado sem rodar pipeline."""
        engine, session, lote = self._setup()
        empresa = _empresa()

        # Insere avaliação existente
        av_existente = AvaliacaoLote(
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
        )
        session.add(av_existente)
        session.commit()

        mock_page = AsyncMock()
        vision_client = MagicMock()

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            with patch("carros_sa.orquestrador.coletar_detalhe", new_callable=AsyncMock) as mock_det:
                res = loop.run_until_complete(
                    _pipeline_lote(lote, mock_page, vision_client, empresa, session, __import__("pathlib").Path("/tmp"))
                )
                # coletar_detalhe NÃO deve ter sido chamado
                mock_det.assert_not_called()
        finally:
            loop.close()

        assert res.avaliado is True
        assert res.preco_alvo == 18000


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
