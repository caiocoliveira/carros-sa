"""Testes da auditoria de laudos — 1 teste por modo de falha + integração planilha.

Protege o contrato que o usuário expressou como: "todos os carros na lista
precisam ter laudo baixado, revisado E link na planilha; se algum falhar, a
razão específica tem que aparecer". Um regressor que colapse de novo os 5
modos de falha num único ⚠ genérico quebra esses testes.

Convenção dos helpers: construímos um mini-DB em memória, criamos fixtures de
PDF em tmp_path quando o modo exige, e exercitamos o classificador. O caminho
da planilha vem coberto pela integração no final — `situacao_label` alimentado
pela mesma enum que o `SheetsExporter`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.auditoria_laudos import (
    CONFIDENCE_MIN_OK,
    ResultadoAuditoria,
    StatusLaudo,
    auditar_empresa,
    classificar_status,
    situacao_label,
)


URL_LAUDO_OK = "https://storage.googleapis.com/doc-b2b/laudos/L001/x.pdf?sig=abc"
URL_DECOY = (
    "https://repo-site-aav-production.storage.googleapis.com/app/uploads/"
    "2025/10/Relatorio-de-Transparencia-Salarial.pdf"
)


def _engine_mem():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


# Sentinela para diferenciar "detalhe omitido" de "detalhe=None" no helper abaixo.
_OMITIDO = object()


def _lote(
    lote_id: str = "L001",
    *,
    detalhe=_OMITIDO,
    fim_em: Optional[datetime] = None,
) -> Lote:
    """Monta um Lote ativo default.

    `detalhe` omitido → raw_json = {} (simula SEM_DETALHE).
    `detalhe={...}` → preenche `raw_json["detalhe"]` com o dict passado.
    `detalhe=None` → mesmo que omitido (raw_json vazio).
    """
    if fim_em is None:
        fim_em = datetime.now() + timedelta(days=3)
    if detalhe is _OMITIDO or detalhe is None:
        raw_json = {}
    else:
        raw_json = {"detalhe": detalhe}
    return Lote(
        id=lote_id,
        leilao="auto_avaliar",
        url=f"https://autoavaliar.com.br/lote/{lote_id}",
        marca="Ford",
        modelo="Fiesta",
        ano=2017,
        km=50000,
        lance_atual=15000,
        fim_em=fim_em,
        origem_cidade="Uberlândia",
        origem_uf="MG",
        raw_json=raw_json,
        scraped_at=datetime.utcnow(),
    )


def _laudo(lote_id: str = "L001", confidence: float = 0.9) -> LaudoCache:
    return LaudoCache(
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
    )


def _avaliacao(lote_id: str = "L001", empresa_id: str = "uberlandia_mg") -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id=empresa_id,
        lote_id=lote_id,
        preco_alvo=22000, preco_max=25000,
        score_roi=0.25, fator_risco=0.8, fator_liquidez=1.0, margem_aplicada=0.15,
        frete_incluso=1500, reforma_estimada=3000, taxas_leilao=2000,
        preco_giro=30000, preco_giro_fipe=30000,
        justificativa="teste",
    )


class TestClassificarStatus:
    """Um teste por modo de falha — se alguém colapsar os status, quebra aqui."""

    def test_sem_detalhe_quando_raw_json_vazio(self, tmp_path: Path):
        lote = _lote()   # raw_json = {} → sem chave "detalhe"
        assert classificar_status(lote, None, pdf_dir=tmp_path) == StatusLaudo.SEM_DETALHE

    def test_sem_url_quando_detalhe_mas_url_ausente(self, tmp_path: Path):
        lote = _lote(detalhe={"laudo_pdf_url": None, "status_laudo": "sem laudo"})
        assert classificar_status(lote, None, pdf_dir=tmp_path) == StatusLaudo.SEM_URL

    def test_url_decoy_quando_url_presente_mas_reprovada(self, tmp_path: Path):
        lote = _lote(detalhe={"laudo_pdf_url": URL_DECOY})
        assert classificar_status(lote, None, pdf_dir=tmp_path) == StatusLaudo.URL_DECOY

    def test_pdf_ausente_quando_url_ok_mas_sem_arquivo(self, tmp_path: Path):
        lote = _lote(detalhe={"laudo_pdf_url": URL_LAUDO_OK})
        # tmp_path está vazio → PDF não existe
        assert classificar_status(lote, None, pdf_dir=tmp_path) == StatusLaudo.PDF_AUSENTE

    def test_extracao_falhou_quando_pdf_existe_mas_confidence_baixa(self, tmp_path: Path):
        lote = _lote(detalhe={"laudo_pdf_url": URL_LAUDO_OK})
        (tmp_path / "L001.pdf").write_bytes(b"%PDF-1.4 fake")
        laudo = _laudo(confidence=0.5)   # fallback _laudo_sem_pdf
        assert classificar_status(lote, laudo, pdf_dir=tmp_path) == StatusLaudo.EXTRACAO_FALHOU

    def test_extracao_falhou_quando_laudo_cache_ausente_mas_pdf_existe(self, tmp_path: Path):
        lote = _lote(detalhe={"laudo_pdf_url": URL_LAUDO_OK})
        (tmp_path / "L001.pdf").write_bytes(b"%PDF-1.4 fake")
        # laudo=None → LaudoCache nunca foi gravado
        assert classificar_status(lote, None, pdf_dir=tmp_path) == StatusLaudo.EXTRACAO_FALHOU

    def test_ok_quando_tudo_bate(self, tmp_path: Path):
        lote = _lote(detalhe={"laudo_pdf_url": URL_LAUDO_OK})
        (tmp_path / "L001.pdf").write_bytes(b"%PDF-1.4 fake")
        laudo = _laudo(confidence=CONFIDENCE_MIN_OK)  # limite exato
        assert classificar_status(lote, laudo, pdf_dir=tmp_path) == StatusLaudo.OK


class TestSituacaoLabel:
    """Rótulo exibido na planilha — preserva prefixo histórico pra filtros
    do usuário continuarem casando em cima da coluna Situação."""

    def test_ok_retorna_none(self):
        assert situacao_label(StatusLaudo.OK) is None

    def test_todos_gaps_tem_prefixo_laudo_nao_analisado(self):
        for s in StatusLaudo:
            if s == StatusLaudo.OK:
                continue
            label = situacao_label(s)
            assert label is not None
            assert "LAUDO NÃO ANALISADO" in label, f"{s} sem prefixo: {label!r}"

    def test_sufixo_distinto_por_status(self):
        """Sufixos têm que ser diferentes pra o operador saber O QUE fazer."""
        labels = {s: situacao_label(s) for s in StatusLaudo if s != StatusLaudo.OK}
        # Se 2 status gerarem o mesmo label, o ponto do exercício desapareceu.
        assert len(set(labels.values())) == len(labels)


class TestAuditarEmpresa:
    """Auditoria sobre DB real em memória — universo = mesmo filtro do sheet."""

    def test_so_considera_lotes_ativos_com_avaliacao_da_empresa(self, tmp_path: Path):
        engine = _engine_mem()
        with Session(engine) as session:
            # L001: ativo, empresa correta, OK
            l1 = _lote("L001", detalhe={"laudo_pdf_url": URL_LAUDO_OK})
            (tmp_path / "L001.pdf").write_bytes(b"%PDF")
            session.add(l1)
            session.add(_avaliacao("L001"))
            session.add(_laudo("L001", 0.9))

            # L002: ativo, outra empresa → não entra
            session.add(_lote("L002", detalhe={"laudo_pdf_url": URL_LAUDO_OK}))
            session.add(_avaliacao("L002", empresa_id="outra"))

            # L003: empresa correta, mas já encerrado → não entra
            l3 = _lote(
                "L003", detalhe={"laudo_pdf_url": URL_LAUDO_OK},
                fim_em=datetime.now() - timedelta(days=1),
            )
            session.add(l3)
            session.add(_avaliacao("L003"))
            session.commit()

            res = auditar_empresa("uberlandia_mg", session, pdf_dir=tmp_path)

        assert res.total == 1
        assert res.ok == 1
        assert res.gaps == 0

    def test_mix_de_modos_de_falha_conta_cada_um_separado(self, tmp_path: Path):
        engine = _engine_mem()
        with Session(engine) as session:
            # OK
            session.add(_lote("L_OK", detalhe={"laudo_pdf_url": URL_LAUDO_OK}))
            (tmp_path / "L_OK.pdf").write_bytes(b"%PDF")
            session.add(_laudo("L_OK", 0.9))
            session.add(_avaliacao("L_OK"))
            # DECOY
            session.add(_lote("L_DEC", detalhe={"laudo_pdf_url": URL_DECOY}))
            session.add(_avaliacao("L_DEC"))
            # SEM_URL
            session.add(_lote("L_NOU", detalhe={"laudo_pdf_url": None}))
            session.add(_avaliacao("L_NOU"))
            # SEM_DETALHE
            session.add(_lote("L_NOD"))
            session.add(_avaliacao("L_NOD"))
            session.commit()

            res = auditar_empresa("uberlandia_mg", session, pdf_dir=tmp_path)

        assert res.total == 4
        assert res.ok == 1
        assert res.por_status[StatusLaudo.URL_DECOY] == 1
        assert res.por_status[StatusLaudo.SEM_URL] == 1
        assert res.por_status[StatusLaudo.SEM_DETALHE] == 1
        assert set(res.lotes_por_status[StatusLaudo.URL_DECOY]) == {"L_DEC"}


class TestExitCodeCli:
    """Garantia do contrato do CLI: `scripts/auditar_laudos.py` sai com código
    != 0 quando há qualquer gap. Cron/CI dependem disso pra alertar."""

    def _setup_db(self, tmp_path: Path, monkeypatch) -> Path:
        """DB SQLite isolado em tmp_path via `CARROS_SA_DB`. Reimporta `db` pra
        recalcular `DEFAULT_DB_PATH` (módulo congela o valor em import-time)."""
        db_file = tmp_path / "auditor_test.db"
        monkeypatch.setenv("CARROS_SA_DB", str(db_file))

        import importlib

        import carros_sa.db as db_mod
        importlib.reload(db_mod)
        db_mod.init_db()
        return db_file

    def test_exit_code_zero_quando_sem_gaps(self, tmp_path: Path, monkeypatch):
        from typer.testing import CliRunner

        self._setup_db(tmp_path, monkeypatch)
        import carros_sa.db as db_mod
        from scripts.auditar_laudos import app

        # PDF dir real (default do auditor). Limpa depois pra não poluir repo.
        pdf_dir = Path("data/laudos_pdfs")
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_file = pdf_dir / "L_OK.pdf"
        pdf_file.write_bytes(b"%PDF")

        try:
            with db_mod.get_session() as s:
                s.add(_lote("L_OK", detalhe={"laudo_pdf_url": URL_LAUDO_OK}))
                s.add(_avaliacao("L_OK"))
                s.add(_laudo("L_OK", 0.9))
                s.commit()

            res = CliRunner().invoke(app, ["--empresa", "uberlandia_mg"])
            assert res.exit_code == 0, res.output
        finally:
            pdf_file.unlink(missing_ok=True)

    def test_exit_code_nao_zero_quando_ha_gap(self, tmp_path: Path, monkeypatch):
        from typer.testing import CliRunner

        self._setup_db(tmp_path, monkeypatch)
        import carros_sa.db as db_mod
        from scripts.auditar_laudos import app

        with db_mod.get_session() as s:
            s.add(_lote("L_GAP", detalhe={"laudo_pdf_url": URL_DECOY}))
            s.add(_avaliacao("L_GAP"))
            s.commit()

        res = CliRunner().invoke(app, ["--empresa", "uberlandia_mg"])
        assert res.exit_code != 0
        assert "url_decoy" in res.output
