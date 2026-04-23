"""Guard do invariante 'todo carro na planilha tem laudo baixado + revisado + linkado'.

Estrutura espelha `test_decoy_laudo_guard.py`:
  1. Testes funcionais com DB em memória — cobrem cada `CausaGap`.
  2. Testes de espelhamento dos filtros — auditor precisa ver exatamente os
     mesmos lotes que o `SheetsExporter._query` mostra na planilha.
  3. Hygiene check do DB real — falha `make test` se houver gap em produção,
     forçando o operador a rodar o pipeline de cura antes de declarar verde.

Filosofia: depois que `limpar_decoys` + `reprocessar_lotes_do_db --somente-laudo-pendente`
rodam no cron, o DB *deve* ficar sem gaps. Se este teste falha, ou (a) os
scripts não foram executados, ou (b) há causa nova que o pipeline atual não
cura — em qualquer caso o operador é avisado antes de abrir a planilha
acreditando que tá tudo certo.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import (
    AvaliacaoLote,
    LaudoCache,
    Lote,
)
from carros_sa.tools.lista_laudo_audit import (
    CausaGap,
    _PDF_STORAGE_DIR,
    auditar_lista_laudos,
)

URL_LAUDO_OK = "https://storage.googleapis.com/doc-b2b/abc123.pdf"
URL_DECOY = (
    "https://repo-site-aav-production.storage.googleapis.com/app/uploads/"
    "2025/10/Relatorio-de-Transparencia.pdf"
)
EMPRESA_ID = "carros_uberlandia"


def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


_UNSET = object()


def _add_lote(
    session: Session,
    lote_id: str,
    laudo_url: Optional[str],
    *,
    fim_em=_UNSET,
    encerrado_badge: bool = False,
    marca: str = "Ford",
    modelo: str = "Fiesta",
    ano: int = 2017,
) -> Lote:
    # Sentinel evita o pitfall do `fim_em or default` quando o caller
    # passa explicitamente fim_em=None pra simular lote SHOWROOM.
    if fim_em is _UNSET:
        fim_em = datetime.now() + timedelta(days=3)
    detalhe = {"laudo_pdf_url": laudo_url}
    if encerrado_badge:
        detalhe["encerrado"] = True
    lote = Lote(
        id=lote_id,
        leilao="auto_avaliar",
        url=f"https://autoavaliar.com.br/lote/{lote_id}",
        marca=marca,
        modelo=modelo,
        ano=ano,
        km=50000,
        lance_atual=15000,
        fim_em=fim_em,
        origem_cidade="Uberlândia",
        origem_uf="MG",
        raw_json={"detalhe": detalhe},
        scraped_at=datetime.utcnow(),
    )
    session.add(lote)
    return lote


def _add_avaliacao(session: Session, lote_id: str, empresa_id: str = EMPRESA_ID) -> None:
    """Adiciona AvaliacaoLote sintética — só os campos NOT NULL importam pro audit."""
    session.add(AvaliacaoLote(
        empresa_id=empresa_id,
        lote_id=lote_id,
        preco_alvo=20000,
        preco_max=22000,
        score_roi=0.15,
        fator_risco=1.0,
        fator_liquidez=1.0,
        margem_aplicada=0.15,
        frete_incluso=500,
        reforma_estimada=0,
        taxas_leilao=1500,
        preco_giro=25000,
        preco_giro_fipe=25000,
        preco_giro_aa=25000,
        fipe=26000,
        webmotors_mediana=None,
        dias_giro_estimado=90,
        justificativa="teste",
        reforma_racional=None,
        criado_em=datetime.utcnow(),
    ))


def _add_laudo(session: Session, lote_id: str, confidence: float) -> None:
    session.add(LaudoCache(
        lote_id=lote_id,
        avarias_json=[],
        severidade_geral="nenhuma",
        motor_ok=True,
        documentacao="ok",
        categoria_veiculo="hatch",
        confidence=confidence,
        modelo_llm="gemini-flash",
        custo_usd=0.001,
        extraido_em=datetime.utcnow(),
    ))


def _criar_pdf_dummy(lote_id: str) -> Path:
    """Cria um arquivo placeholder em data/laudos_pdfs/ pra o audit ver 'baixado'.

    O audit só checa existência (não conteúdo) — `_pdf_eh_laudo_valido`
    no orquestrador é quem rejeita PDF inválido. Essa separação é
    intencional: o audit responde 'tem o arquivo?'; o validador responde
    'o arquivo é laudo de carro?'.
    """
    _PDF_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = _PDF_STORAGE_DIR / f"{lote_id}.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 dummy laudo content " * 100)
    return pdf_path


@pytest.fixture
def cleanup_pdfs():
    """Garante que PDFs criados pelos testes não vazam pro DB de produção."""
    criados: list = []
    yield criados
    for p in criados:
        p.unlink(missing_ok=True)


# =============================================================================
# Funcionais — uma classe por causa
# =============================================================================

class TestCausaUrlAusente:
    def test_lote_sem_laudo_url_e_reportado(self, cleanup_pdfs):
        engine = _engine_mem()
        with Session(engine) as session:
            _add_lote(session, "L1", laudo_url=None)
            _add_avaliacao(session, "L1")
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        assert res.total_na_planilha == 1
        assert res.completos == 0
        assert res.total_gaps == 1
        assert res.gaps[0].causa == CausaGap.URL_AUSENTE
        assert res.gaps[0].lote_id == "L1"


class TestCausaUrlDecoy:
    def test_url_decoy_persistida_e_reportada(self, cleanup_pdfs):
        engine = _engine_mem()
        with Session(engine) as session:
            _add_lote(session, "L_DECOY", laudo_url=URL_DECOY)
            _add_avaliacao(session, "L_DECOY")
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        assert res.total_gaps == 1
        assert res.gaps[0].causa == CausaGap.URL_DECOY


class TestCausaPdfNaoBaixado:
    def test_url_valida_mas_pdf_inexistente(self, cleanup_pdfs):
        engine = _engine_mem()
        # NÃO cria arquivo PDF — simula download que nunca aconteceu.
        with Session(engine) as session:
            _add_lote(session, "L_SEM_PDF", laudo_url=URL_LAUDO_OK)
            _add_avaliacao(session, "L_SEM_PDF")
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        assert res.total_gaps == 1
        assert res.gaps[0].causa == CausaGap.PDF_NAO_BAIXADO


class TestCausaLaudoNaoRevisado:
    def test_pdf_existe_mas_laudo_cache_ausente(self, cleanup_pdfs):
        engine = _engine_mem()
        with Session(engine) as session:
            _add_lote(session, "L_SEM_CACHE", laudo_url=URL_LAUDO_OK)
            _add_avaliacao(session, "L_SEM_CACHE")
            cleanup_pdfs.append(_criar_pdf_dummy("L_SEM_CACHE"))
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        assert res.total_gaps == 1
        assert res.gaps[0].causa == CausaGap.LAUDO_NAO_REVISADO
        assert "LaudoCache ausente" in res.gaps[0].detalhe

    def test_laudo_cache_baixa_confidence_e_reportado(self, cleanup_pdfs):
        engine = _engine_mem()
        with Session(engine) as session:
            _add_lote(session, "L_LOWCONF", laudo_url=URL_LAUDO_OK)
            _add_avaliacao(session, "L_LOWCONF")
            cleanup_pdfs.append(_criar_pdf_dummy("L_LOWCONF"))
            _add_laudo(session, "L_LOWCONF", confidence=0.5)
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        assert res.total_gaps == 1
        assert res.gaps[0].causa == CausaGap.LAUDO_NAO_REVISADO
        assert "0.50" in res.gaps[0].detalhe

    def test_laudo_cache_confidence_no_threshold_passa(self, cleanup_pdfs):
        """Boundary: confidence == 0.6 conta como revisado (>=, não >)."""
        engine = _engine_mem()
        with Session(engine) as session:
            _add_lote(session, "L_BOUNDARY", laudo_url=URL_LAUDO_OK)
            _add_avaliacao(session, "L_BOUNDARY")
            cleanup_pdfs.append(_criar_pdf_dummy("L_BOUNDARY"))
            _add_laudo(session, "L_BOUNDARY", confidence=0.6)
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        assert res.total_gaps == 0
        assert res.completos == 1


# =============================================================================
# Lote completo (caso feliz) + ordem de causas
# =============================================================================

class TestLoteCompleto:
    def test_3_invariantes_satisfeitos_nao_e_reportado(self, cleanup_pdfs):
        engine = _engine_mem()
        with Session(engine) as session:
            _add_lote(session, "L_OK", laudo_url=URL_LAUDO_OK)
            _add_avaliacao(session, "L_OK")
            cleanup_pdfs.append(_criar_pdf_dummy("L_OK"))
            _add_laudo(session, "L_OK", confidence=0.9)
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        assert res.total_na_planilha == 1
        assert res.completos == 1
        assert res.total_gaps == 0


class TestOrdemDeCausasRaiz:
    def test_url_ausente_nao_reporta_pdf_nao_baixado(self, cleanup_pdfs):
        """Sem URL não dá pra ter PDF — reportar PDF_NAO_BAIXADO seria sintoma derivado.
        Operador resolve URL primeiro, depois o resto cai em cascata."""
        engine = _engine_mem()
        with Session(engine) as session:
            _add_lote(session, "L_URLLESS", laudo_url=None)
            _add_avaliacao(session, "L_URLLESS")
            # Note: NÃO cria PDF — mas a URL é o gate primário.
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        assert res.total_gaps == 1
        assert res.gaps[0].causa == CausaGap.URL_AUSENTE


# =============================================================================
# Espelhamento de filtros do SheetsExporter
# =============================================================================

class TestEspelhaFiltrosDoExport:
    """Auditor só pode reclamar de lotes que o operador realmente vê na planilha.
    Reclamar de lote encerrado / sem fim_em é falso positivo."""

    def test_lote_sem_fim_em_e_ignorado(self, cleanup_pdfs):
        engine = _engine_mem()
        with Session(engine) as session:
            _add_lote(session, "L_SHOWROOM", laudo_url=None, fim_em=None)
            _add_avaliacao(session, "L_SHOWROOM")
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        assert res.total_na_planilha == 0
        assert res.total_gaps == 0

    def test_lote_encerrado_por_timer_e_ignorado(self, cleanup_pdfs):
        engine = _engine_mem()
        with Session(engine) as session:
            _add_lote(
                session, "L_OLD", laudo_url=None,
                fim_em=datetime.now() - timedelta(days=2),
            )
            _add_avaliacao(session, "L_OLD")
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        assert res.total_na_planilha == 0

    def test_lote_encerrado_por_badge_e_ignorado(self, cleanup_pdfs):
        engine = _engine_mem()
        with Session(engine) as session:
            _add_lote(session, "L_BADGE", laudo_url=None, encerrado_badge=True)
            _add_avaliacao(session, "L_BADGE")
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        assert res.total_na_planilha == 0

    def test_avaliacao_de_outra_empresa_nao_conta(self, cleanup_pdfs):
        engine = _engine_mem()
        with Session(engine) as session:
            _add_lote(session, "L_OUTRA", laudo_url=None)
            _add_avaliacao(session, "L_OUTRA", empresa_id="empresa_fake_sp")
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        assert res.total_na_planilha == 0


# =============================================================================
# Agregação por causa
# =============================================================================

class TestAgregacao:
    def test_gaps_por_causa_conta_certo(self, cleanup_pdfs):
        engine = _engine_mem()
        with Session(engine) as session:
            _add_lote(session, "A1", laudo_url=None)
            _add_lote(session, "A2", laudo_url=None)
            _add_lote(session, "B1", laudo_url=URL_DECOY)
            for lid in ("A1", "A2", "B1"):
                _add_avaliacao(session, lid)
            session.commit()

            res = auditar_lista_laudos(session, EMPRESA_ID)

        agg = res.gaps_por_causa()
        assert agg == {"url_ausente": 2, "url_decoy": 1}


# =============================================================================
# Hygiene check do DB real
# =============================================================================

class TestHygieneDBReal:
    """Espelho do `TestHygieneDBReal` em test_decoy_laudo_guard.

    Se há um `carros_sa.db` na pasta do repo (operador rodou uma triagem real),
    o invariante 3-em-1 precisa estar verde. Skippa silenciosamente em CI/máquinas
    novas.
    """

    def _db_path(self) -> Optional[Path]:
        explicit = os.environ.get("CARROS_SA_DB")
        if explicit:
            p = Path(explicit)
            return p if p.exists() else None
        for candidate in (
            Path("carros_sa.db"),
            Path(__file__).resolve().parents[1] / "carros_sa.db",
        ):
            if candidate.exists():
                return candidate
        return None

    def test_db_atual_tem_lista_de_laudos_completa(self):
        db_path = self._db_path()
        if db_path is None:
            pytest.skip("sem carros_sa.db — hygiene check só roda com DB presente")

        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        # Empresa default da PoC. Se houver multi-tenant com volume, expandir.
        with Session(engine) as session:
            res = auditar_lista_laudos(session, EMPRESA_ID)

        if res.total_na_planilha == 0:
            pytest.skip(
                f"sem AvaliacaoLote ativa pra {EMPRESA_ID} — nada a auditar "
                f"(rode `make triagem` antes pra popular)."
            )

        if res.total_gaps > 0:
            agg = res.gaps_por_causa()
            amostra = [(g.causa.value, g.lote_id) for g in res.gaps[:5]]
            pytest.fail(
                f"{res.total_gaps}/{res.total_na_planilha} lotes da planilha "
                f"violam o invariante laudo-baixado+revisado+linkado.\n"
                f"  Por causa: {agg}\n"
                f"  Amostra: {amostra}\n"
                f"  Rode `make auditar-lista-laudos` pro relatório completo, "
                f"depois `make limpar-decoys` + retry pra curar."
            )
