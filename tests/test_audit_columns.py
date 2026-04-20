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
from carros_sa.tools.audit import CHECKS, INVARIANTES_INTERNAS, audit
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
        """ROI anualizado > 1000% sugere erro de cálculo (ex: dias_giro=1, floor deveria ser 30).

        ROI anual = score_roi × 365 / dias_giro × 100. score_roi=5.0 com dias_giro=30
        → 5 × 365/30 × 100 ≈ 6083% — fora de qualquer racional econômico.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=1000))
            session.add(_avaliacao("L001", score_roi=5.0, dias_giro_estimado=30))
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
# Sanity cruzando precificador com âncoras de mercado (FIPE)
# Responde perguntas do usuário: faz sentido preco_max > FIPE? faz sentido
# preco_giro_fipe muito diferente da FIPE? Resposta: NÃO — auditoria flagga.
# ---------------------------------------------------------------------------

class TestAuditVsFipe:
    def test_lance_maximo_acima_fipe_reportado(self):
        """preco_max > FIPE × 1.05 → capital bruto excede a âncora de revenda.

        Cenário: precificador produziu teto de R$ 30k num carro com FIPE R$ 20k.
        Revender acima da FIPE quase nunca acontece na janela do Auto Avaliar
        (usualmente mediana ≤ FIPE × 0.95), então capital > FIPE é red flag.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao(
                "L001",
                preco_max=30_000,
                fipe=20_000,  # FIPE × 1.05 = 21.000, preco_max=30.000 viola
            ))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("Lance Máximo" in v and "FIPE" in v for v in violacoes), (
            f"Esperava violação de Lance Máximo vs FIPE em {violacoes}"
        )

    def test_lance_maximo_abaixo_fipe_nao_reportado(self):
        """preco_max ≤ FIPE × 1.05 é o caminho feliz e NÃO deve gerar violação."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao(
                "L001",
                preco_max=20_000,
                fipe=32_000,  # folga confortável
            ))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert not any("Lance Máximo" in v and "FIPE" in v for v in violacoes), (
            f"Não deveria ter violação de teto vs FIPE em {violacoes}"
        )

    def test_preco_giro_fipe_muito_acima_fipe_reportado(self):
        """preco_giro_fipe > FIPE × 1.20 sugere webmotors outlier ou erro de parsing."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao(
                "L001",
                fipe=20_000,
                preco_giro_fipe=30_000,  # 150% da FIPE — suspeito
            ))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("preco_giro_fipe" in v for v in violacoes), (
            f"Esperava violação de preco_giro_fipe vs FIPE em {violacoes}"
        )

    def test_preco_giro_fipe_muito_abaixo_fipe_reportado(self):
        """preco_giro_fipe < FIPE × 0.60 sugere mercado travado ou erro de parsing."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao(
                "L001",
                fipe=30_000,
                preco_giro_fipe=15_000,  # 50% da FIPE — travado demais
            ))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("preco_giro_fipe" in v for v in violacoes), (
            f"Esperava violação de preco_giro_fipe vs FIPE em {violacoes}"
        )

    def test_preco_giro_fipe_perto_da_fipe_nao_reportado(self):
        """preco_giro_fipe em [FIPE×0.60, FIPE×1.20] é a janela saudável."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao(
                "L001",
                fipe=30_000,
                preco_giro_fipe=28_500,  # 95% da FIPE — típico
            ))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert not any("preco_giro_fipe" in v for v in violacoes), (
            f"Não deveria ter violação de preco_giro_fipe vs FIPE em {violacoes}"
        )


# ---------------------------------------------------------------------------
# Situação "⚠ LAUDO NÃO ANALISADO" precisa estar no domínio válido — evita
# falso positivo no check de Situação quando lote novo ainda não teve laudo lido.
# ---------------------------------------------------------------------------

class TestSituacaoLaudoNaoAnalisado:
    def test_lote_sem_laudo_tem_situacao_valida_sem_violacao(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
            # Sem _laudo() — lote em retry pendente.
            session.commit()
        violacoes = audit(engine)
        assert not any("Situação" in v for v in violacoes), (
            f"Situação deveria aceitar '⚠ LAUDO NÃO ANALISADO' — {violacoes}"
        )

    def test_laudo_com_confidence_baixa_cai_em_nao_analisado(self):
        """Laudo fallback (_laudo_sem_pdf) grava confidence ≤ 0.55 — não conta como analisado."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
            laudo = _laudo("L001")
            laudo.confidence = 0.4  # fallback sem PDF real
            session.add(laudo)
            session.commit()
        violacoes = audit(engine)
        # Não deve gerar violação de Situação; "⚠ LAUDO NÃO ANALISADO" é válido.
        assert not any("Situação" in v for v in violacoes), (
            f"confidence baixa deveria virar '⚠ LAUDO NÃO ANALISADO' — {violacoes}"
        )


# ---------------------------------------------------------------------------
# Smoke: INVARIANTES_INTERNAS exportado e estruturado corretamente.
# ---------------------------------------------------------------------------

class TestInvariantesInternasShape:
    def test_invariantes_internas_e_lista_de_tuplas(self):
        assert isinstance(INVARIANTES_INTERNAS, list)
        for entry in INVARIANTES_INTERNAS:
            assert isinstance(entry, tuple) and len(entry) == 2
            label, validator = entry
            assert isinstance(label, str) and label
            assert callable(validator)
