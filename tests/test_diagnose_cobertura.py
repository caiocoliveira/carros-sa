"""Testes do script `scripts/diagnose_cobertura.py`.

Read-only: usa SQLite in-memory, não chama LLM nem Gemini API. Captura stdout
via `io.StringIO` e valida que o relatório identifica o problema certo +
sugere o comando esperado.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from scripts.diagnose_cobertura import diagnosticar


def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _lote(
    lote_id: str = "L001",
    marca: str = "Ford",
    modelo: str = "Fiesta",
    ano: int = 2013,
    fim_em: Optional[datetime] = None,
) -> Lote:
    return Lote(
        id=lote_id,
        leilao="auto_arremate",
        url=f"https://autoavaliar.com.br/lote/{lote_id}",
        marca=marca,
        modelo=modelo,
        ano=ano,
        km=45000,
        lance_atual=20000,
        fim_em=fim_em or (datetime.now() + timedelta(days=3)),
        scraped_at=datetime.utcnow(),
    )


def _avaliacao(lote_id: str = "L001", reforma_estimada: int = 3000) -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id="uberlandia_mg",
        lote_id=lote_id,
        preco_alvo=25000,
        preco_max=30000,
        score_roi=0.3,
        fator_risco=0.8,
        fator_liquidez=1.0,
        margem_aplicada=0.15,
        frete_incluso=1500,
        reforma_estimada=reforma_estimada,
        taxas_leilao=2400,
        preco_giro=35000,
        preco_giro_fipe=35000,
        fipe=32000,
        webmotors_mediana=34000,
        dias_giro_estimado=90,
        justificativa="teste",
        criado_em=datetime.utcnow(),
    )


def _laudo(lote_id: str = "L001", confidence: float = 0.95) -> LaudoCache:
    return LaudoCache(
        lote_id=lote_id,
        avarias_json=[],
        severidade_geral="leve",
        motor_ok=True,
        documentacao="ok",
        categoria_veiculo="hatch",
        confidence=confidence,
        modelo_llm="gemini-flash",
        custo_usd=0.001,
        extraido_em=datetime.utcnow(),
    )


class TestDiagnoseCobertura:
    def test_aponta_lotes_sem_laudo_quando_dominante(self, monkeypatch):
        """5 lotes ativos: 2 com laudo válido, 3 sem laudo (cache_baixa_conf).
        Sugestão deve apontar pra retry de laudos pendentes."""
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-twenty-chars-plus")
        engine = _engine_mem()
        with Session(engine) as session:
            for i in range(2):
                lid = f"L_OK_{i}"
                session.add(_lote(lid))
                session.add(_avaliacao(lid, reforma_estimada=3000))
                session.add(_laudo(lid, confidence=0.95))
            for i in range(3):
                lid = f"L_FALLBACK_{i}"
                session.add(_lote(lid))
                session.add(_avaliacao(lid, reforma_estimada=0))
                session.add(_laudo(lid, confidence=0.5))  # fallback
            session.commit()

            buf = io.StringIO()
            diagnosticar(session, out=buf)

        out = buf.getvalue()
        assert "Universo: 5 lotes ativos" in out
        assert "sem laudo válido: 3" in out
        # Sugestão direciona pra retry de laudos
        assert "lotes sem laudo válido" in out
        assert "limpar-decoys" in out or "triagem" in out

    def test_aponta_bug_no_estimador_quando_todos_tem_laudo_valido(self, monkeypatch):
        """5 lotes c/ laudo válido (confidence=0.95), todos com reforma=0.
        Sugestão deve apontar pra bug no EstimadorReformaLLM, não pra retry."""
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-twenty-chars-plus")
        engine = _engine_mem()
        with Session(engine) as session:
            for i in range(5):
                lid = f"L_BUG_{i}"
                session.add(_lote(lid))
                session.add(_avaliacao(lid, reforma_estimada=0))
                session.add(_laudo(lid, confidence=0.95))
            session.commit()

            buf = io.StringIO()
            diagnosticar(session, out=buf)

        out = buf.getvalue()
        assert "com laudo + reforma=0: 5" in out
        # Sugestão menciona bug no estimador
        assert "EstimadorReformaLLM" in out or "estimador" in out.lower()

    def test_db_vazio_nao_crasha(self, monkeypatch):
        """DB sem lotes ativos → relatório sai limpo, sem traceback."""
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-twenty-chars-plus")
        engine = _engine_mem()
        with Session(engine) as session:
            buf = io.StringIO()
            diagnosticar(session, out=buf)

        out = buf.getvalue()
        assert "Universo: 0 lotes ativos" in out

    def test_sinaliza_gemini_api_key_ausente(self, monkeypatch):
        """Sem GEMINI_API_KEY a sugestão prioriza setar config antes de retry."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        engine = _engine_mem()
        with Session(engine) as session:
            # Pelo menos um lote pra que o relatório tenha conteúdo
            session.add(_lote("L001"))
            session.add(_avaliacao("L001", reforma_estimada=0))
            session.add(_laudo("L001", confidence=0.95))
            session.commit()

            buf = io.StringIO()
            diagnosticar(session, out=buf)

        out = buf.getvalue()
        assert "Gemini healthcheck: FALHA" in out
        assert "GEMINI_API_KEY" in out
