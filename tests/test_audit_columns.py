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
    score_roi: float = 0.3,
    justificativa: str = "Laudo leve, FIPE R$30k, giro estimado 90 dias.",
) -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id=empresa_id,
        lote_id=lote_id,
        preco_alvo=25000,
        preco_max=preco_max,
        score_roi=score_roi,
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
        """ROI anualizado > 1000% sugere score_roi inflado ou dias_giro=1 (floor não aplicado)."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=1000))
            # ROI anualizado = score_roi × 365 / dias_giro. Score 5.0 (500% no
            # alvo, valor que NÃO deveria existir) × 365 / 30 = 6083% > 1000.
            session.add(_avaliacao(
                "L001",
                preco_max=1000,
                preco_giro=100_000,
                reforma_estimada=0,
                frete_incluso=0,
                dias_giro_estimado=30,
                score_roi=5.0,
            ))
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

class TestAuditCrossCheck:
    """Invariantes que cruzam mais de uma coluna (preco_alvo vs preco_max,
    preco_giro_fipe vs FIPE, lance_atual vs preco_alvo).
    """

    def test_preco_giro_fipe_acima_de_fipe_x_110_sinalizado(self):
        """Combinação fallback FIPE×0.97 + f_km saturado a 1.15 produz
        preco_giro_fipe ≈ 1.115 × FIPE — duas premissas otimistas em série.
        Audit avisa pra checar Webmotors live e km mediana de mercado.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao(
                "L001",
                fipe=70_000,
                preco_giro_fipe=80_000,  # 80k / 70k = 1.143 > 1.10
            ))
            session.commit()
        violacoes = audit(engine)
        assert any("preco_giro_fipe" in v and "1.10" in v for v in violacoes), (
            f"Esperava violação preco_giro_fipe > FIPE×1.10 em {violacoes}"
        )

    def test_preco_giro_fipe_dentro_da_tolerancia_nao_sinaliza(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao(
                "L001",
                fipe=70_000,
                preco_giro_fipe=75_000,  # 75k / 70k = 1.071 < 1.10
            ))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert not any("preco_giro_fipe" in v for v in violacoes), (
            f"Não esperava violação dentro da tolerância: {violacoes}"
        )

    def test_zona_apertada_lance_acima_do_alvo_sinalizada(self):
        """`lance_atual > preco_alvo` mas ainda `≤ preco_max` é zona apertada:
        operador pode dar lance, mas o ROI exibido deve usar score_efetivo
        (não o intrinsic). Audit avisa pra confirmar que o ROI realista bate.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=27_000))  # alvo=25k, max=30k
            session.add(_avaliacao("L001", preco_max=30_000))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("Zona apertada" in v for v in violacoes), (
            f"Esperava aviso de zona apertada em {violacoes}"
        )

    def test_lance_abaixo_do_alvo_nao_dispara_zona_apertada(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=20_000))  # alvo=25k, max=30k
            session.add(_avaliacao("L001", preco_max=30_000))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert not any("Zona apertada" in v for v in violacoes), (
            f"Não esperava zona apertada quando lance < alvo: {violacoes}"
        )


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
