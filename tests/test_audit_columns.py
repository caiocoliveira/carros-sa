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
            lote = _lote("L001")
            # URL válida de laudo no raw_json — sem isso, o invariante "Laudo
            # analisado ⇒ link presente" dispararia (confidence default 0.95).
            lote.raw_json = {"detalhe": {
                "laudo_pdf_url": "https://storage.googleapis.com/doc-b2b/abc.pdf",
            }}
            session.add(lote)
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


class TestLaudoLinkInvariante:
    """Coluna 'Laudo' deve flagear quando o lote tem laudo analisado mas o link
    está '—'. Esse era o gap silencioso: laudo_analisado=True (status ✓ Viável
    no exporter) sem `laudo_url` válido = operador confia em ROI/Reforma sem
    ter como abrir o PDF do laudo."""

    def test_laudo_analisado_sem_url_e_reportado(self):
        """Lote com LaudoCache confidence=0.95 e raw_json sem laudo_pdf_url
        (URL nunca foi achada pelo scraper) deve gerar violação."""
        engine = _engine_mem()
        with Session(engine) as session:
            lote = _lote("L_NOLINK")
            lote.raw_json = {"detalhe": {}}  # sem laudo_pdf_url
            session.add(lote)
            session.add(_avaliacao("L_NOLINK"))
            session.add(_laudo("L_NOLINK"))  # confidence default 0.95
            session.commit()
        violacoes = audit(engine)
        laudo_violacoes = [v for v in violacoes if v.startswith("⚠ Laudo:")]
        assert laudo_violacoes, f"Esperava violação na coluna Laudo. Obteve: {violacoes}"

    def test_laudo_analisado_com_url_decoy_tambem_e_reportado(self):
        """URL existe no DB mas não passa em is_laudo_pdf_url → exporter mostra
        '—' → invariante deve flagear."""
        engine = _engine_mem()
        with Session(engine) as session:
            lote = _lote("L_DECOY")
            lote.raw_json = {"detalhe": {
                "laudo_pdf_url": (
                    "https://repo-site-aav-production.storage.googleapis.com/"
                    "app/uploads/Relatorio-de-Transparencia.pdf"
                )
            }}
            session.add(lote)
            session.add(_avaliacao("L_DECOY"))
            session.add(_laudo("L_DECOY"))
            session.commit()
        violacoes = audit(engine)
        laudo_violacoes = [v for v in violacoes if v.startswith("⚠ Laudo:")]
        assert laudo_violacoes, f"Esperava violação na coluna Laudo. Obteve: {violacoes}"

    def test_laudo_nao_analisado_com_link_ausente_nao_dispara(self):
        """Quando o laudo NÃO foi analisado (confidence baixa), '—' no link
        é estado esperado — não deve flagear (caso contrário inundaria o log
        com falso positivos durante runs incompletos)."""
        engine = _engine_mem()
        with Session(engine) as session:
            lote = _lote("L_PEND")
            lote.raw_json = {"detalhe": {}}
            session.add(lote)
            session.add(_avaliacao("L_PEND"))
            session.add(_laudo("L_PEND", severidade="nenhuma"))
            # confidence baixa simula fallback _laudo_sem_pdf
            from sqlmodel import select
            laudo_obj = session.exec(select(LaudoCache).where(LaudoCache.lote_id == "L_PEND")).first()
            laudo_obj.confidence = 0.5
            session.add(laudo_obj)
            session.commit()
        violacoes = audit(engine)
        laudo_violacoes = [v for v in violacoes if v.startswith("⚠ Laudo:")]
        assert not laudo_violacoes, f"Não deveria flagear quando laudo_analisado=False: {laudo_violacoes}"

    def test_laudo_analisado_com_url_valida_passa_silencioso(self):
        """Caminho feliz: laudo analisado + URL válida → nada na coluna Laudo."""
        engine = _engine_mem()
        with Session(engine) as session:
            lote = _lote("L_OK")
            lote.raw_json = {"detalhe": {
                "laudo_pdf_url": "https://storage.googleapis.com/doc-b2b/abc.pdf"
            }}
            session.add(lote)
            session.add(_avaliacao("L_OK"))
            session.add(_laudo("L_OK"))
            session.commit()
        violacoes = audit(engine)
        laudo_violacoes = [v for v in violacoes if v.startswith("⚠ Laudo:")]
        assert not laudo_violacoes, f"Caminho feliz não deveria flagear: {laudo_violacoes}"
