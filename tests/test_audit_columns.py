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
    # preco_giro <= fipe × 1.10 — invariante cruzada no audit alerta acima.
    # 35k vs 40k = 87.5% FIPE (caso realista: webmotors mediana < FIPE).
    preco_giro: int = 35000,
    reforma_estimada: int = 3000,
    frete_incluso: int = 1500,
    fator_risco: float = 0.8,
    dias_giro_estimado: Optional[int] = 90,
    fipe: Optional[int] = 40000,
    webmotors_mediana: Optional[int] = 35000,
    preco_giro_fipe: int = 35000,
    preco_giro_aa: Optional[int] = None,
    score_roi: float = 0.3,
    justificativa: str = "Laudo leve, FIPE R$40k, giro estimado 90 dias.",
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
        """ROI anualizado > 500% sinaliza dias_giro otimista ou margem×fator inflado.

        Threshold apertado de 1000% → 500% pra pegar saturação realista (operação
        Reinaldo: 60-75% ano; sistema chega a 500% só quando dias_giro<60d
        otimista colide com fatores próximos do teto).
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=1000))
            # score_roi=2.0 × 365/60 (floor) = 1217% > 500
            session.add(_avaliacao(
                "L001",
                preco_max=1000,
                preco_giro=100_000,
                reforma_estimada=0,
                frete_incluso=0,
                dias_giro_estimado=30,
                score_roi=2.0,
            ))
            session.commit()
        violacoes = audit(engine)
        assert any("ROI" in v for v in violacoes), f"Esperava violação de ROI em {violacoes}"

    def test_roi_300_pct_nao_reportado(self):
        """ROI ~300% (lote viável típico do Polo/Compass simulados) NÃO deve
        ser flagged — está dentro da zona "otimista mas plausível"."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            # score_roi=0.50, dias_giro=60 (floor) → 0.50×365/60 = 304%
            session.add(_avaliacao(
                "L001",
                dias_giro_estimado=60,
                score_roi=0.50,
            ))
            session.commit()
        violacoes = audit(engine)
        assert not any("ROI" in v for v in violacoes), (
            f"ROI 304% não deveria flag (dentro da zona aceita): {violacoes}"
        )

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
# Paridade audit ↔ sheets (mesmo set de lotes auditado vs exibido)
# ---------------------------------------------------------------------------

class TestAuditParidadeSheets:
    """Audit deve auditar o que o operador vê. SheetsExporter filtra:
      1. fim_em is None (sumiu do leilão ativo do AA)
      2. encerrado (timer vencido OU badge ARREMATADO)

    Antes da paridade ficar explícita, audit reportava violações em lotes que
    nunca apareciam na planilha — alarme falso que o operador não conseguia
    confirmar abrindo a UI.
    """

    def test_lote_sem_fim_em_nao_e_auditado(self):
        engine = _engine_mem()
        with Session(engine) as session:
            # Lote sem fim_em mas com dado claramente bugado (KM absurdo).
            # Deveria ser ignorado pelo audit (não aparece na planilha).
            lote = _lote("L_SEM_FIM", km=5_000_000)
            lote.fim_em = None
            session.add(lote)
            session.add(_avaliacao("L_SEM_FIM"))
            session.commit()
        violacoes = audit(engine)
        assert violacoes == [], (
            f"Audit não deveria reportar lotes sem fim_em (filtrados da planilha): {violacoes}"
        )

    def test_lote_encerrado_por_timer_nao_e_auditado(self):
        engine = _engine_mem()
        with Session(engine) as session:
            # Lote com timer já passado + KM absurdo.
            lote = _lote("L_ENCERRADO", km=5_000_000)
            lote.fim_em = datetime.now() - timedelta(days=1)
            session.add(lote)
            session.add(_avaliacao("L_ENCERRADO"))
            session.commit()
        violacoes = audit(engine)
        assert violacoes == []

    def test_lote_ativo_continua_sendo_auditado(self):
        """Sanidade: se o filtro novo não derruba o lote ativo, KM absurdo continua flag."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_ATIVO", km=5_000_000))
            session.add(_avaliacao("L_ATIVO"))
            session.commit()
        violacoes = audit(engine)
        assert any("KM" in v for v in violacoes), f"Lote ativo perdeu auditoria: {violacoes}"


# ---------------------------------------------------------------------------
# Invariantes que cruzam colunas (não pertencem a uma coluna individual)
# ---------------------------------------------------------------------------

class TestAuditInvariantesCruzadas:
    """Validações que comparam DOIS campos da avaliação. Não cabem em CHECKS
    (que validam coluna por coluna) — vivem em CROSS_CHECKS.
    """

    def test_preco_giro_acima_de_fipe_x_110_e_reportado(self):
        """preco_giro > FIPE × 1.10 sinaliza f_km saturando ou mediana inflada.

        Por construção, webmotors_mediana × f_km tem teto teórico FIPE×0.97×1.15
        ≈ 111.5% — ultrapassar 110% é red flag.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            # FIPE 50k, preco_giro 60k = 120% FIPE → flag
            session.add(_avaliacao(
                "L001",
                fipe=50_000,
                preco_giro=60_000,
                preco_giro_fipe=60_000,
            ))
            session.commit()
        violacoes = audit(engine)
        assert any("Preço-Giro" in v or "preco_giro" in v.lower() for v in violacoes), (
            f"Esperava violação de preco_giro vs FIPE: {violacoes}"
        )

    def test_preco_giro_dentro_de_fipe_x_110_nao_reporta(self):
        """preco_giro = FIPE × 1.05 (dentro do esperado) não deve disparar."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao(
                "L001",
                fipe=50_000,
                preco_giro=int(50_000 * 1.05),
                preco_giro_fipe=int(50_000 * 1.05),
            ))
            session.commit()
        violacoes = audit(engine)
        assert not any("Preço-Giro" in v for v in violacoes), (
            f"105% FIPE não deveria flag: {violacoes}"
        )

    def test_preco_alvo_maior_que_preco_max_e_reportado(self):
        """preco_alvo > preco_max viola invariante do precificador (margem
        aplicada deveria ser >= margem mínima absoluta)."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            av = _avaliacao("L001", preco_max=20_000)
            av.preco_alvo = 25_000  # > preco_max — bug
            session.add(av)
            session.commit()
        violacoes = audit(engine)
        assert any("Preço-Alvo" in v or "preco_alvo" in v.lower() for v in violacoes), (
            f"Esperava violação preco_alvo > preco_max: {violacoes}"
        )
