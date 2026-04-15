"""Gold tests do ScraperDetalheLote (workstream D).

Usa o innerText REAL do Fiesta 21854782 (mesmo body do test_parsers) e SQLite
em tmp_path. Downloader de PDF é stub — zero rede.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import pytest
from sqlmodel import SQLModel

from carros_sa.db import get_engine, get_session
from carros_sa.models import Lote
from carros_sa.scraping.scraper_detalhe import processar_detalhe
from tests.test_parsers import DETALHE_FIESTA_BODY


LAUDO_PDF_URL = "https://storage.googleapis.com/doc-b2b/8c9fbea96f.pdf"


@pytest.fixture
def db(tmp_path, monkeypatch) -> Path:
    """Cria SQLite isolado em tmp + insere o Fiesta como Lote."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CARROS_SA_DB", str(db_path))
    # Recarrega DEFAULT_DB_PATH nos módulos que já importaram
    import carros_sa.db as db_module
    db_module.DEFAULT_DB_PATH = db_path

    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)

    with get_session(db_path) as s:
        s.add(Lote(
            id="21854782",
            leilao="autoavaliar",
            url="https://b2b.autoavaliar.com.br/avaliacoes/saga/21854782/ford-fiesta",
            marca="Ford",
            modelo="Fiesta 1.6 Se Hatch",
            ano=2013,
            km=171_053,
            lance_atual=22_900,
            origem_cidade="Uberlandia",
            origem_uf="MG",
            raw_json={"origem": "listagem"},
        ))
        s.commit()
    return db_path


def _stub_downloader_factory() -> Tuple[List[Tuple[str, Path]], object]:
    chamadas: List[Tuple[str, Path]] = []

    def stub(url: str, destino: Path) -> None:
        chamadas.append((url, destino))
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"%PDF-1.4 fake")

    return chamadas, stub


def test_processar_detalhe_fiesta_reprovado_estrutural_pula_pdf(db, tmp_path):
    """Caso real: Fiesta 21854782 tem REPROVADO ESTRUTURAL → early_exit, NÃO baixa PDF."""
    chamadas, stub = _stub_downloader_factory()

    with get_session(db) as s:
        resultado = processar_detalhe(
            lote_id="21854782",
            body_text=DETALHE_FIESTA_BODY,
            laudo_pdf_url=LAUDO_PDF_URL,
            session=s,
            pdf_dir=tmp_path / "laudos",
            downloader=stub,
        )

    assert resultado.passou is False
    assert resultado.early_exit == "reprovado_estrutural"
    assert resultado.pdf_baixado is False
    assert resultado.pdf_path is None
    assert chamadas == [], "Downloader não pode ser chamado quando há early_exit"

    # Persistência: lote.raw_json["detalhe"] foi enriquecido
    with get_session(db) as s:
        lote = s.get(Lote, "21854782")
    assert lote.raw_json["origem"] == "listagem"  # campo prévio preservado
    detalhe = lote.raw_json["detalhe"]
    assert detalhe["early_exit"] == "reprovado_estrutural"
    assert detalhe["reprovado_estrutural"] is True
    assert detalhe["status_laudo"] == "Laudo não aprovado"
    assert detalhe["specs"]["KM"] == "171.053"
    assert detalhe["ipva_pago"] is True
    assert "pdf_path_local" not in detalhe


def test_processar_detalhe_caso_feliz_baixa_pdf(db, tmp_path):
    """Sem reprovação estrutural + laudo aprovado → baixa PDF e registra path."""
    body_feliz = DETALHE_FIESTA_BODY.replace(
        "Laudo não aprovado", "Laudo aprovado"
    ).replace(
        "REPROVADO ESTRUTURAL", "APROVADO ESTRUTURAL"
    )
    chamadas, stub = _stub_downloader_factory()

    with get_session(db) as s:
        resultado = processar_detalhe(
            lote_id="21854782",
            body_text=body_feliz,
            laudo_pdf_url=LAUDO_PDF_URL,
            session=s,
            pdf_dir=tmp_path / "laudos",
            downloader=stub,
        )

    assert resultado.passou is True
    assert resultado.early_exit is None
    assert resultado.pdf_baixado is True
    assert resultado.pdf_path == tmp_path / "laudos" / "21854782.pdf"
    assert resultado.pdf_path.read_bytes().startswith(b"%PDF")
    assert chamadas == [(LAUDO_PDF_URL, tmp_path / "laudos" / "21854782.pdf")]

    with get_session(db) as s:
        lote = s.get(Lote, "21854782")
    assert lote.raw_json["detalhe"]["pdf_path_local"].endswith("21854782.pdf")


def test_processar_detalhe_sem_url_de_pdf_nao_falha(db, tmp_path):
    """Lote feliz mas sem URL de laudo → passa, não baixa, não explode."""
    body_feliz = DETALHE_FIESTA_BODY.replace(
        "Laudo não aprovado", "Laudo aprovado"
    ).replace("REPROVADO ESTRUTURAL", "APROVADO ESTRUTURAL")
    chamadas, stub = _stub_downloader_factory()

    with get_session(db) as s:
        resultado = processar_detalhe(
            lote_id="21854782",
            body_text=body_feliz,
            laudo_pdf_url=None,
            session=s,
            pdf_dir=tmp_path / "laudos",
            downloader=stub,
        )

    assert resultado.passou is True
    assert resultado.pdf_baixado is False
    assert chamadas == []


def test_processar_detalhe_lote_inexistente_falha(db, tmp_path):
    with get_session(db) as s:
        with pytest.raises(ValueError, match="não existe"):
            processar_detalhe(
                lote_id="99999999",
                body_text=DETALHE_FIESTA_BODY,
                laudo_pdf_url=None,
                session=s,
                pdf_dir=tmp_path / "laudos",
                downloader=lambda u, p: None,
            )
