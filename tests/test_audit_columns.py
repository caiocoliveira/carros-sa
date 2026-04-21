"""Testes da auditoria automática de colunas (carros_sa.tools.audit).

Invariantes declarativas por coluna: cruza o valor exportado contra o propósito
declarado no Glossário da planilha (sheets.py). O hook SessionEnd roda isto
silenciosamente — só imprime algo quando acha anomalia.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.audit import CHECKS, audit
from carros_sa.tools.sheets import HEADER


def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _lote(
    lote_id: str = "L001",
    marca: str = "Ford",
    modelo: str = "Fiesta",
    ano: int = 2013,
    km: Optional[int] = 45000,
    lance_atual: int = 20000,
    fim_em: Optional[datetime] = None,
) -> Lote:
    return Lote(
        id=lote_id,
        leilao="auto_arremate",
        url=f"https://autoavaliar.com.br/lote/{lote_id}",
        marca=marca,
        modelo=modelo,
        ano=ano,
        km=km,
        lance_atual=lance_atual,
        fim_em=fim_em or (datetime.now() + timedelta(days=3)),
        scraped_at=datetime.utcnow(),
    )


def _avaliacao(
    lote_id: str = "L001",
    empresa_id: str = "uberlandia_mg",
    preco_max: int = 30000,
    preco_giro: int = 40000,
    reforma_estimada: int = 3000,
    frete_incluso: int = 1500,
    fator_risco: float = 0.8,
    dias_giro_estimado: Optional[int] = 90,
    fipe: Optional[int] = 32000,
    webmotors_mediana: Optional[int] = 34000,
    preco_giro_fipe: int = 30000,
    preco_giro_aa: Optional[int] = None,
    justificativa: str = "Laudo leve, FIPE R$30k, giro estimado 90 dias.",
) -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id=empresa_id,
        lote_id=lote_id,
        preco_alvo=25000,
        preco_max=preco_max,
        score_roi=0.3,
        fator_risco=fator_risco,
        fator_liquidez=1.0,
        margem_aplicada=0.15,
        frete_incluso=frete_incluso,
        reforma_estimada=reforma_estimada,
        taxas_leilao=int(preco_max * 0.08),
        preco_giro=preco_giro,
        preco_giro_fipe=preco_giro_fipe,
        preco_giro_aa=preco_giro_aa,
        fipe=fipe,
        webmotors_mediana=webmotors_mediana,
        dias_giro_estimado=dias_giro_estimado,
        justificativa=justificativa,
        criado_em=datetime.utcnow(),
    )


def _laudo(
    lote_id: str = "L001",
    severidade: str = "leve",
    motor_ok: bool = True,
) -> LaudoCache:
    return LaudoCache(
        lote_id=lote_id,
        avarias_json=[],
        severidade_geral=severidade,
        motor_ok=motor_ok,
        documentacao="ok",
        categoria_veiculo="hatch",
        confidence=0.95,
        modelo_llm="gemini-flash",
        custo_usd=0.001,
        extraido_em=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Paridade HEADER ↔ CHECKS: toda coluna nova da planilha precisa ter uma
# entrada correspondente (mesmo que permissiva) senão a auditoria vira ponto cego.
# ---------------------------------------------------------------------------

class TestParidadeHeaderChecks:
    def test_todas_colunas_do_header_estao_em_checks(self):
        faltando = set(HEADER) - set(CHECKS.keys())
        assert not faltando, f"Colunas sem invariante declarada: {faltando}"

    def test_checks_nao_tem_colunas_fantasma(self):
        """CHECKS não deve ter colunas que não existem em HEADER."""
        fantasma = set(CHECKS.keys()) - set(HEADER)
        assert not fantasma, f"CHECKS referencia coluna inexistente em HEADER: {fantasma}"


# ---------------------------------------------------------------------------
# Silêncio em caminho feliz
# ---------------------------------------------------------------------------

class TestAuditHappyPath:
    def test_db_vazio_retorna_lista_vazia(self):
        engine = _engine_mem()
        violacoes = audit(engine)
        assert violacoes == []

    def test_linha_valida_nao_gera_violacoes(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert violacoes == []


# ---------------------------------------------------------------------------
# Detecção de valores fora do racional do Glossário
# ---------------------------------------------------------------------------

class TestAuditDeteccao:
    def test_roi_absurdo_reportado(self):
        """ROI anualizado > 1000% sugere erro de cálculo — fora do racional econômico."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=1000))
            # preco_giro enorme vs preco_max minúsculo → ROI no máximo explode,
            # anualizado com dias_giro=30 vira ~5 dígitos.
            session.add(_avaliacao("L001", preco_max=1000, preco_giro=100_000,
                                   reforma_estimada=0, frete_incluso=0,
                                   dias_giro_estimado=30))
            session.commit()
        violacoes = audit(engine)
        assert any("ROI" in v for v in violacoes), f"Esperava violação de ROI em {violacoes}"

    def test_reforma_negativa_reportada(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001", reforma_estimada=-100))
            session.commit()
        violacoes = audit(engine)
        assert any("Reforma" in v for v in violacoes), f"Violacoes: {violacoes}"

    def test_km_absurdo_reportado(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", km=5_000_000))
            session.add(_avaliacao("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("KM" in v for v in violacoes), f"Violacoes: {violacoes}"

    def test_modelo_string_vazia_reportado(self):
        engine = _engine_mem()
        with Session(engine) as session:
            lote = _lote("L001", marca="", modelo="", ano=2013)
            session.add(lote)
            session.add(_avaliacao("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("Modelo" in v for v in violacoes), f"Violacoes: {violacoes}"


# ---------------------------------------------------------------------------
# Agregação por coluna (várias linhas → uma única violação com contagem)
# ---------------------------------------------------------------------------

class TestAuditAgregacao:
    def test_multiplas_linhas_problema_mesma_coluna_agrupadas(self):
        engine = _engine_mem()
        with Session(engine) as session:
            for i in range(3):
                lid = f"L00{i+1}"
                session.add(_lote(lid))
                # 3 lotes com reforma_estimada negativa
                session.add(_avaliacao(lid, reforma_estimada=-100))
            session.commit()
        violacoes = audit(engine)
        reforma = [v for v in violacoes if "Reforma" in v]
        assert len(reforma) == 1, f"Esperava uma única linha agregada, obtive: {reforma}"
        assert "3" in reforma[0], f"Linha deveria reportar contagem de 3 linhas: {reforma[0]}"

    def test_problemas_em_colunas_diferentes_reportados_separados(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", km=5_000_000))  # KM absurdo
            session.add(_avaliacao("L001", reforma_estimada=-50))  # Reforma negativa

            lote2 = _lote("L002", ano=1900)  # Ano fora da faixa
            session.add(lote2)
            session.add(_avaliacao("L002"))
            session.commit()
        violacoes = audit(engine)
        texto = "\n".join(violacoes)
        assert "KM" in texto
        assert "Reforma" in texto
        assert "Ano" in texto


# ---------------------------------------------------------------------------
# Cross-column: FIPE × preco_giro × lance_máximo
# Cobre a pergunta do operador (2026-04-21): "faz sentido lance_max >> FIPE?
# faz sentido giro_fipe mt diferente da FIPE?" — a planilha hoje não mostra
# giro_fipe na visão enxuta, mas o DB persiste; auditoria cruza o racional
# econômico antes do dado sair pra decisão humana.
# ---------------------------------------------------------------------------

class TestAuditCrossColumn:
    def test_lance_maximo_acima_da_fipe_reportado(self):
        """preco_max > FIPE × 1.05 viola o racional (margem deveria garantir teto < venda)."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=1000))
            # FIPE R$ 30k mas preco_max R$ 40k — 1.33x, violação clara
            session.add(_avaliacao("L001", preco_max=40_000, fipe=30_000,
                                   preco_giro=45_000, preco_giro_fipe=45_000,
                                   reforma_estimada=0, frete_incluso=0))
            session.commit()
        violacoes = audit(engine)
        lance = [v for v in violacoes if "Lance Máximo" in v and "FIPE" in v]
        assert lance, f"Esperava violação de Lance Máximo vs FIPE, obtive: {violacoes}"

    def test_lance_maximo_dentro_da_fipe_nao_reporta(self):
        """Caso normal: preco_max < FIPE — silêncio."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=15_000))
            session.add(_avaliacao("L001", preco_max=25_000, fipe=32_000,
                                   preco_giro=31_000, preco_giro_fipe=31_000))
            session.commit()
        violacoes = audit(engine)
        assert all("Lance Máximo" not in v or "FIPE" not in v for v in violacoes), (
            f"Não deveria flagar caso normal: {violacoes}"
        )

    def test_preco_giro_fipe_muito_acima_da_fipe_reportado(self):
        """preco_giro_fipe > FIPE × 1.30 sinaliza webmotors_mediana inflada ou FIPE baixa."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=1000))
            # preco_giro_fipe 1.5x FIPE — fora da faixa [0.6, 1.3]
            session.add(_avaliacao("L001", preco_max=25_000, fipe=30_000,
                                   preco_giro=45_000, preco_giro_fipe=45_000))
            session.commit()
        violacoes = audit(engine)
        interno = [v for v in violacoes if "preco_giro_fipe" in v]
        assert interno, f"Esperava violação de preco_giro_fipe vs FIPE: {violacoes}"

    def test_preco_giro_fipe_muito_abaixo_da_fipe_reportado(self):
        """preco_giro_fipe < FIPE × 0.60 também sinaliza dado bagunçado."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=1000))
            # 0.40x FIPE
            session.add(_avaliacao("L001", preco_max=5_000, fipe=30_000,
                                   preco_giro=12_000, preco_giro_fipe=12_000))
            session.commit()
        violacoes = audit(engine)
        interno = [v for v in violacoes if "preco_giro_fipe" in v]
        assert interno, f"Esperava violação de preco_giro_fipe muito baixo: {violacoes}"

    def test_preco_giro_aa_acima_da_fipe_reportado(self):
        """AA é atacado — preco_giro_aa > FIPE × 1.30 é suspeito."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=1000))
            # giro_aa 1.5x FIPE — AA não deveria ser tão maior que FIPE
            session.add(_avaliacao("L001", preco_max=25_000, fipe=30_000,
                                   preco_giro=45_000, preco_giro_fipe=30_000,
                                   preco_giro_aa=45_000))
            session.commit()
        violacoes = audit(engine)
        interno = [v for v in violacoes if "preco_giro_aa" in v]
        assert interno, f"Esperava violação de preco_giro_aa vs FIPE: {violacoes}"
