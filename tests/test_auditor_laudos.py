"""Auditoria dos 3 eixos do laudo (PDF baixado / revisado / link na planilha).

Este módulo é a salvaguarda contra "carro na planilha sem laudo conferível":
o exporter já filtra encerrados e marca lotes com confidence baixa como
"⚠ LAUDO NÃO ANALISADO", mas até agora não havia nada que reportasse
**por que** o laudo está incompleto (URL ausente vs. decoy filtrado vs.
PDF não baixou vs. extração caiu no fallback).

`auditar()` classifica cada lote ATIVO da empresa em um motivo único — o
mais a montante na cadeia — e os testes abaixo cobrem cada motivo + a
agregação + o caminho feliz silencioso + o auto-heal offline.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools import auditor_laudos
from carros_sa.tools.auditor_laudos import (
    MotivoLaudoFaltante,
    auditar,
    render_relatorio,
)


URL_LAUDO_OK = "https://storage.googleapis.com/doc-b2b/laudo_xpto.pdf"
URL_DECOY = (
    "https://repo-site-aav-production.storage.googleapis.com/app/uploads/"
    "2025/10/Relatorio-de-Transparencia-e-igualdade-Salarial.pdf"
)


def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _lote(
    lote_id: str,
    laudo_pdf_url: Optional[str] = URL_LAUDO_OK,
    *,
    fim_em_delta_days: int = 3,
    marca: str = "Ford",
    modelo: str = "Fiesta",
    ano: int = 2017,
) -> Lote:
    """Lote de fixture; default = ativo (fim no futuro) + URL de laudo válida."""
    if fim_em_delta_days is None:
        fim_em = None
    else:
        fim_em = datetime.now() + timedelta(days=fim_em_delta_days)
    return Lote(
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
        raw_json={"detalhe": {"laudo_pdf_url": laudo_pdf_url}},
        scraped_at=datetime.utcnow(),
    )


def _avaliacao(lote_id: str, empresa_id: str = "carros_uberlandia") -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id=empresa_id,
        lote_id=lote_id,
        preco_alvo=20000,
        preco_max=25000,
        score_roi=0.2,
        fator_risco=0.9,
        fator_liquidez=1.0,
        margem_aplicada=0.15,
        frete_incluso=1500,
        reforma_estimada=2000,
        taxas_leilao=2000,
        preco_giro=30000,
        preco_giro_fipe=30000,
        preco_giro_aa=None,
        fipe=32000,
        webmotors_mediana=33000,
        dias_giro_estimado=90,
        justificativa="ok",
        criado_em=datetime.utcnow(),
    )


def _laudo(lote_id: str, confidence: float = 0.95) -> LaudoCache:
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


# ---------------------------------------------------------------------------
# Helpers pra simular o filesystem dos PDFs (sem mexer em data/laudos_pdfs real)
# ---------------------------------------------------------------------------

@pytest.fixture
def pdf_dir(tmp_path, monkeypatch):
    """Aponta `_PDF_DIR` do auditor pro tmp_path do teste — isolamento total."""
    monkeypatch.setattr(auditor_laudos, "_PDF_DIR", tmp_path)
    return tmp_path


def _criar_pdf_fake(dir_path: Path, lote_id: str, conteudo: bytes = b"%PDF-1.4 fake") -> Path:
    p = dir_path / f"{lote_id}.pdf"
    p.write_bytes(conteudo + b" " * 6000)  # garante stat().st_size >= 5000
    return p


def _stub_pdf_validador(monkeypatch, retorno: bool = True):
    """Bypassa o `_pdf_eh_laudo_valido` (PyMuPDF) — retorna fixo True/False.

    Sem isso o validador tenta ler o PDF binário fake como se fosse PDF real.
    """
    monkeypatch.setattr(auditor_laudos, "_pdf_eh_laudo_valido", lambda p: retorno)


# ---------------------------------------------------------------------------
# Caminho feliz: tudo verde → 0 problemas
# ---------------------------------------------------------------------------

class TestAuditOK:
    def test_lote_completo_e_classificado_como_ok(self, pdf_dir, monkeypatch):
        _stub_pdf_validador(monkeypatch, True)
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
            session.add(_laudo("L001"))
            session.commit()
            _criar_pdf_fake(pdf_dir, "L001")

            res = auditar(session, "carros_uberlandia")
            assert res.total_lotes_ativos == 1
            assert res.lotes_ok == 1
            assert res.total_problemas == 0

    def test_db_vazio_ok_silencioso(self):
        engine = _engine_mem()
        with Session(engine) as session:
            res = auditar(session, "carros_uberlandia")
            assert res.total_lotes_ativos == 0
            assert res.total_problemas == 0


# ---------------------------------------------------------------------------
# Filtros que excluem lotes da auditoria (espelha o exporter)
# ---------------------------------------------------------------------------

class TestFiltros:
    def test_lote_encerrado_nao_entra_na_auditoria(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_PASS", fim_em_delta_days=-1))  # passou ontem
            session.add(_avaliacao("L_PASS"))
            session.commit()

            res = auditar(session, "carros_uberlandia")
            assert res.total_lotes_ativos == 0

    def test_lote_sem_fim_em_nao_entra(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_SEM", fim_em_delta_days=None))
            session.add(_avaliacao("L_SEM"))
            session.commit()

            res = auditar(session, "carros_uberlandia")
            assert res.total_lotes_ativos == 0

    def test_lote_de_outra_empresa_ignorado(self, pdf_dir, monkeypatch):
        _stub_pdf_validador(monkeypatch, True)
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_NOSSO"))
            session.add(_avaliacao("L_NOSSO", empresa_id="carros_uberlandia"))
            session.add(_laudo("L_NOSSO"))
            _criar_pdf_fake(pdf_dir, "L_NOSSO")

            session.add(_lote("L_OUTRA"))
            session.add(_avaliacao("L_OUTRA", empresa_id="empresa_fake_sp"))
            session.commit()

            res = auditar(session, "carros_uberlandia")
            assert res.total_lotes_ativos == 1


# ---------------------------------------------------------------------------
# Classificação dos motivos — um teste por motivo, com lote mínimo
# ---------------------------------------------------------------------------

class TestClassificacao:
    def test_url_ausente_no_db(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_NOURL", laudo_pdf_url=None))
            session.add(_avaliacao("L_NOURL"))
            session.commit()
            res = auditar(session, "carros_uberlandia")
            assert MotivoLaudoFaltante.URL_AUSENTE_NO_DB in res.por_motivo
            assert res.por_motivo[MotivoLaudoFaltante.URL_AUSENTE_NO_DB][0].lote_id == "L_NOURL"

    def test_url_filtrada_decoy(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_DEC", laudo_pdf_url=URL_DECOY))
            session.add(_avaliacao("L_DEC"))
            session.commit()
            res = auditar(session, "carros_uberlandia")
            assert MotivoLaudoFaltante.URL_FILTRADA_DECOY in res.por_motivo

    def test_pdf_nao_baixado(self, pdf_dir):
        """URL válida no DB mas nada em data/laudos_pdfs (download falhou)."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_PDF"))  # URL válida default
            session.add(_avaliacao("L_PDF"))
            session.commit()
            res = auditar(session, "carros_uberlandia")
            assert MotivoLaudoFaltante.PDF_NAO_BAIXADO in res.por_motivo

    def test_pdf_local_invalido(self, pdf_dir, monkeypatch):
        """Arquivo existe mas validador rejeitou (ex.: PDF de footer institucional)."""
        _stub_pdf_validador(monkeypatch, False)
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_INV"))
            session.add(_avaliacao("L_INV"))
            session.commit()
            _criar_pdf_fake(pdf_dir, "L_INV")
            res = auditar(session, "carros_uberlandia")
            assert MotivoLaudoFaltante.PDF_LOCAL_INVALIDO in res.por_motivo

    def test_laudo_cache_ausente(self, pdf_dir, monkeypatch):
        """PDF baixou e validou, mas pipeline não rodou extração."""
        _stub_pdf_validador(monkeypatch, True)
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_NCACHE"))
            session.add(_avaliacao("L_NCACHE"))
            session.commit()
            _criar_pdf_fake(pdf_dir, "L_NCACHE")
            res = auditar(session, "carros_uberlandia")
            assert MotivoLaudoFaltante.LAUDO_CACHE_AUSENTE in res.por_motivo

    def test_extracao_baixa_confianca(self, pdf_dir, monkeypatch):
        """Pipeline rodou mas caiu no fallback (visão LLM falhou)."""
        _stub_pdf_validador(monkeypatch, True)
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_LOWC"))
            session.add(_avaliacao("L_LOWC"))
            session.add(_laudo("L_LOWC", confidence=0.5))  # fallback
            session.commit()
            _criar_pdf_fake(pdf_dir, "L_LOWC")
            res = auditar(session, "carros_uberlandia")
            assert MotivoLaudoFaltante.EXTRACAO_BAIXA_CONFIANCA in res.por_motivo


# ---------------------------------------------------------------------------
# Agregação + relatório textual
# ---------------------------------------------------------------------------

class TestAgregacaoERender:
    def test_multiplos_motivos_diferentes_sao_contados_separados(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L1", laudo_pdf_url=None))   # URL ausente
            session.add(_avaliacao("L1"))
            session.add(_lote("L2", laudo_pdf_url=URL_DECOY))  # decoy
            session.add(_avaliacao("L2"))
            session.add(_lote("L3"))                         # PDF não baixado
            session.add(_avaliacao("L3"))
            session.commit()

            res = auditar(session, "carros_uberlandia")
            assert res.total_lotes_ativos == 3
            assert res.total_problemas == 3
            assert len(res.por_motivo[MotivoLaudoFaltante.URL_AUSENTE_NO_DB]) == 1
            assert len(res.por_motivo[MotivoLaudoFaltante.URL_FILTRADA_DECOY]) == 1
            assert len(res.por_motivo[MotivoLaudoFaltante.PDF_NAO_BAIXADO]) == 1

    def test_relatorio_silencioso_quando_tudo_ok(self, pdf_dir, monkeypatch):
        _stub_pdf_validador(monkeypatch, True)
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_OK"))
            session.add(_avaliacao("L_OK"))
            session.add(_laudo("L_OK"))
            session.commit()
            _criar_pdf_fake(pdf_dir, "L_OK")

            res = auditar(session, "carros_uberlandia")
            relatorio = render_relatorio(res)
            assert "✓" in relatorio
            assert "ok=1" in relatorio
            assert "problemas=0" in relatorio

    def test_relatorio_inclui_descricao_acionavel_de_cada_motivo(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_DEC", laudo_pdf_url=URL_DECOY))
            session.add(_avaliacao("L_DEC"))
            session.commit()
            res = auditar(session, "carros_uberlandia")
            relatorio = render_relatorio(res)
            assert "url_filtrada_decoy" in relatorio
            assert "limpar-decoys" in relatorio  # ação clara pro operador
            assert "L_DEC" in relatorio  # ID exemplo


# ---------------------------------------------------------------------------
# Auto-heal — re-extrai laudo de PDF local válido (sem rede)
# ---------------------------------------------------------------------------

class TestAutoHealLocal:
    def test_re_extrai_quando_pdf_valido_e_confidence_baixa(self, pdf_dir, monkeypatch):
        """Reproduz o cenário Gemini-503: PDF baixou OK no run inicial, visão
        falhou e ficou confidence=0.5. Agora com client funcionando, re-extrai."""
        _stub_pdf_validador(monkeypatch, True)

        # Stub do extrair_laudo retornando confidence alta — simula visão funcionando
        from carros_sa.models import LaudoEstruturado, CategoriaVeiculo, StatusDocumentacao, SeveridadeAvaria

        def _fake_extrair(pdf_path, vision_client):
            return LaudoEstruturado(
                avarias=[],
                severidade_geral=SeveridadeAvaria.LEVE,
                motor_ok=True,
                documentacao=StatusDocumentacao.OK,
                categoria_veiculo=CategoriaVeiculo.HATCH,
                confidence=0.92,
            )
        monkeypatch.setattr(
            "carros_sa.agents.extrator_laudo.extrair_laudo", _fake_extrair,
        )

        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_HEAL"))
            session.add(_avaliacao("L_HEAL"))
            session.add(_laudo("L_HEAL", confidence=0.5))  # fallback ruim
            session.commit()
            _criar_pdf_fake(pdf_dir, "L_HEAL")

            res = auditar(session, "carros_uberlandia")
            assert MotivoLaudoFaltante.EXTRACAO_BAIXA_CONFIANCA in res.por_motivo

            heal = auditor_laudos.auto_heal_local(session, res, vision_client=object())
            assert heal.re_extraidos == ["L_HEAL"]
            assert not heal.falhas

            # Pós-heal: nova auditoria considera ok
            res2 = auditar(session, "carros_uberlandia")
            assert res2.lotes_ok == 1
            assert res2.total_problemas == 0

    def test_nao_tenta_heal_quando_pdf_local_e_invalido(self, pdf_dir, monkeypatch):
        """Auto-heal só atua sobre PDFs locais VÁLIDOS — PDF corrompido não
        seria curado por re-extração, precisa de re-download."""
        _stub_pdf_validador(monkeypatch, False)
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_BAD"))
            session.add(_avaliacao("L_BAD"))
            session.commit()
            _criar_pdf_fake(pdf_dir, "L_BAD")  # arquivo existe mas validador rejeita

            res = auditar(session, "carros_uberlandia")
            heal = auditor_laudos.auto_heal_local(session, res, vision_client=object())
            assert heal.re_extraidos == []


# ---------------------------------------------------------------------------
# Guard de DB real — força operador a rodar `make auditar-laudos` antes do release.
# Skippa gracefully em CI/máquinas sem DB (mesmo padrão do test_decoy_laudo_guard).
# ---------------------------------------------------------------------------

class TestHygieneDBReal:
    def _db_path(self) -> Optional[Path]:
        import os
        explicit = os.environ.get("CARROS_SA_DB")
        if explicit:
            p = Path(explicit)
            return p if p.exists() else None
        for cand in (
            Path("carros_sa.db"),
            Path(__file__).resolve().parents[1] / "carros_sa.db",
        ):
            if cand.exists():
                return cand
        return None

    def _empresas_no_db(self, engine) -> list[str]:
        from sqlmodel import select
        with Session(engine) as session:
            return list({
                av.empresa_id for av in session.exec(select(AvaliacaoLote)).all()
            })

    def test_db_atual_nao_tem_lote_ativo_sem_laudo(self):
        db_path = self._db_path()
        if db_path is None:
            pytest.skip("sem carros_sa.db — hygiene check só roda com DB presente")

        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )

        empresas = self._empresas_no_db(engine)
        if not empresas:
            pytest.skip("DB existe mas sem AvaliacaoLote — nada pra auditar")

        problemas_por_empresa: dict[str, str] = {}
        with Session(engine) as session:
            for emp in empresas:
                res = auditar(session, emp)
                if res.total_problemas > 0:
                    problemas_por_empresa[emp] = render_relatorio(res, max_exemplos=3)

        if problemas_por_empresa:
            corpo = "\n\n".join(problemas_por_empresa.values())
            pytest.fail(
                "Há lote(s) ativo(s) na planilha sem laudo completo (PDF baixado, "
                "revisado e link válido). Rode `make auditar-laudos` ou "
                "`make auditar-laudos-heal` pra detalhes/correção:\n\n" + corpo
            )
