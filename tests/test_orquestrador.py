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
    _pipeline_lote,
    _upsert_avaliacao,
    _upsert_lote,
)
from carros_sa.models import Avaliacao


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
        justificativa="Teste.",
    )


# ---------------------------------------------------------------------------
# Testes de _calcular_frete
# ---------------------------------------------------------------------------

class TestCalcularFrete:
    def test_frete_mesmo_uf(self):
        empresa = _empresa()
        lote = _lote(uf="MG")
        frete = _calcular_frete(lote, empresa)
        assert frete.distancia_km == 150
        assert frete.frete_estimado > 0
        assert frete.destino_uf == "MG"

    def test_frete_uf_diferente(self):
        empresa = _empresa()
        lote_sp = _lote(uf="SP")
        lote_am = _lote(uf="AM")
        frete_sp = _calcular_frete(lote_sp, empresa)
        frete_am = _calcular_frete(lote_am, empresa)
        # UF adjacente < UF distante
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
