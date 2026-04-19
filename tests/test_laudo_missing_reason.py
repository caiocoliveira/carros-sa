"""Testes do motivo estruturado de laudo ausente (MISSING_URL_NAO_CAPTURADA etc).

Gold case: Gol 21862502 e Cruze 21865772 na triagem de Uberlândia 2026-04-14 —
ambos com body_text mencionando 'LAUDO DO VEÍCULO' mas `laudo_pdf_url=null`
porque o coletor via Chrome MCP não abriu o modal lazy-loaded. Antes da
classificação estruturada esses lotes viravam '—' na planilha, indistinguíveis
de ausência real de laudo, e o gap ficava invisível por semanas.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import SQLModel

from carros_sa.db import get_engine, get_session
from carros_sa.models import Lote
from carros_sa.scraping.parsers import (
    DetalheFlags,
    detectar_motivo_laudo_ausente,
    parse_detalhe,
)
from carros_sa.scraping.scraper_detalhe import processar_detalhe
from carros_sa.tools.sheets import _rotulo_laudo_ausente


# =============================================================================
# Detector isolado
# =============================================================================

def test_detectar_motivo_body_vazio():
    """innerText vazio ou minúsculo → body_vazio."""
    assert detectar_motivo_laudo_ausente("") == DetalheFlags.MISSING_BODY_VAZIO
    assert detectar_motivo_laudo_ausente("   ") == DetalheFlags.MISSING_BODY_VAZIO
    assert detectar_motivo_laudo_ausente("tiny") == DetalheFlags.MISSING_BODY_VAZIO


def test_detectar_motivo_sem_laudo_declarado():
    """Body mencionando 'SEM LAUDO' → ausência legítima."""
    body = (
        "Ford Fiesta 1.6\n"
        "ULTIMA AVALIAÇÃO\n22.900,00\n"
        "ANO\n2013/2014\n"
        "SEM LAUDO\n"
        "Veículo sem avaliação cautelar disponível\n"
    )
    assert detectar_motivo_laudo_ausente(body) == DetalheFlags.MISSING_SEM_LAUDO


def test_detectar_motivo_url_nao_capturada_gold_gol():
    """Gold real: Gol 21862502 — body tem 'LAUDO DO VEÍCULO' mas scraper não pegou URL."""
    body_gol = (
        "VOLKSWAGEN GOL 1.0 MI 8V FLEX 4P MANUAL G.VI\n"
        "BYD Uberlandia - Uberlândia/MG\n"
        "Grupo Aguia Branca - ANÚNCIO Nº 21862502\n"
        "Veículo de Repasse\n"
        "ULTIMA AVALIAÇÃO\n27.500,00 / GOL\nR$\n AVALIE AGORA\n"
        "LAUDO DO VEÍCULO\n"
        "SIMULAR FRETE\n"
        "FINALIZA EM 15/04/2026 as 17:00:00\n"
        "ANO\n2013/2014\nKM\n112.397\n"
    )
    assert detectar_motivo_laudo_ausente(body_gol) == DetalheFlags.MISSING_URL_NAO_CAPTURADA


def test_detectar_motivo_url_nao_capturada_variacoes_de_rotulo():
    """Aceita rótulos alternativos do DOM do AA: 'Acessar laudo', 'Ver laudo', 'laudo cautelar'."""
    base = (
        "Ford Fiesta 1.6 SE HATCH 16V FLEX 4P MANUAL\n"
        "Uberlandia/MG\nANO\n2012/2013\nKM\n171.053\n"
        "ULTIMA AVALIAÇÃO\n22.900,00\n"
    )
    for rotulo in ("LAUDO DO VEÍCULO", "Acessar laudo", "Ver laudo", "Laudo Cautelar", "laudo completo"):
        body = base + rotulo + "\n"
        motivo = detectar_motivo_laudo_ausente(body)
        assert motivo == DetalheFlags.MISSING_URL_NAO_CAPTURADA, f"rótulo={rotulo!r}"


def test_detectar_motivo_retorna_none_quando_sem_sinal():
    """Body longo sem 'laudo' nem 'sem laudo' → None (não classifica)."""
    body = "Ford Fiesta\n" + ("ANO\n2013\nKM\n100000\n" * 20)
    assert detectar_motivo_laudo_ausente(body) is None


# =============================================================================
# parse_detalhe preenche `laudo_missing_reason` apenas quando URL é nula
# =============================================================================

def test_parse_detalhe_nao_classifica_quando_url_presente():
    body = "Ford Fiesta\nANO\n2013\nKM\n100.000\nLAUDO DO VEÍCULO\n"
    flags = parse_detalhe(body, laudo_pdf_url="https://storage.googleapis.com/doc-b2b/abc.pdf")
    assert flags.laudo_pdf_url is not None
    assert flags.laudo_missing_reason is None


def test_parse_detalhe_classifica_gap_quando_url_nula():
    """Gold Gol: URL nula + 'LAUDO DO VEÍCULO' no body → MISSING_URL_NAO_CAPTURADA."""
    body = (
        "VOLKSWAGEN GOL\nANO\n2013/2014\nKM\n112.397\n"
        "ULTIMA AVALIAÇÃO\n27.500,00 / GOL\n"
        "LAUDO DO VEÍCULO\n"
    )
    flags = parse_detalhe(body, laudo_pdf_url=None)
    assert flags.laudo_pdf_url is None
    assert flags.laudo_missing_reason == DetalheFlags.MISSING_URL_NAO_CAPTURADA


# =============================================================================
# scraper_detalhe persiste o motivo em raw_json + captura download falhou
# =============================================================================

@pytest.fixture
def db(tmp_path, monkeypatch) -> Path:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CARROS_SA_DB", str(db_path))
    import carros_sa.db as db_module
    db_module.DEFAULT_DB_PATH = db_path
    engine = get_engine(db_path)
    SQLModel.metadata.create_all(engine)
    with get_session(db_path) as s:
        s.add(Lote(
            id="21862502",
            leilao="autoavaliar",
            url="https://b2b.autoavaliar.com.br/avaliacoes/kuruma/21862502/volkswagen-gol",
            marca="Volkswagen",
            modelo="Gol 1.0 MI 8V FLEX",
            ano=2014,
            km=112_397,
            lance_atual=27_500,
            origem_cidade="Uberlândia",
            origem_uf="MG",
            raw_json={"origem": "listagem"},
        ))
        s.commit()
    return db_path


def test_processar_detalhe_persiste_missing_reason_em_raw_json(db, tmp_path):
    """Gold: Gol com body_text real + laudo_pdf_url=null → raw_json.detalhe.laudo_missing_reason='url_nao_capturada'."""
    body_gol = (
        "VOLKSWAGEN GOL\n"
        "ANO\n2013/2014\n"
        "KM\n112.397\n"
        "ULTIMA AVALIAÇÃO\n27.500,00 / GOL\n"
        "Laudo aprovado\n"       # evita early_exit reprovado
        "APROVADO ESTRUTURAL\n"
        "LAUDO DO VEÍCULO\n"
    )
    with get_session(db) as s:
        resultado = processar_detalhe(
            lote_id="21862502",
            body_text=body_gol,
            laudo_pdf_url=None,
            session=s,
            pdf_dir=tmp_path / "laudos",
            downloader=lambda u, p: None,
        )
    assert resultado.passou is True
    assert resultado.pdf_baixado is False

    with get_session(db) as s:
        lote = s.get(Lote, "21862502")
    detalhe = lote.raw_json["detalhe"]
    assert detalhe["laudo_pdf_url"] is None
    assert detalhe["laudo_missing_reason"] == DetalheFlags.MISSING_URL_NAO_CAPTURADA


def test_processar_detalhe_download_falhou_registra_motivo(db, tmp_path):
    """Downloader explode → raw_json marca 'download_falhou' antes de propagar erro."""
    def dl_explode(url: str, destino: Path) -> None:
        raise ConnectionError("429 Too Many Requests")

    body_feliz = (
        "VOLKSWAGEN GOL\nANO\n2013/2014\nKM\n112.397\n"
        "Laudo aprovado\nAPROVADO ESTRUTURAL\n"
        "LAUDO DO VEÍCULO\n"
    )
    with get_session(db) as s:
        with pytest.raises(ConnectionError):
            processar_detalhe(
                lote_id="21862502",
                body_text=body_feliz,
                laudo_pdf_url="https://storage.googleapis.com/doc-b2b/fake.pdf",
                session=s,
                pdf_dir=tmp_path / "laudos",
                downloader=dl_explode,
            )

    with get_session(db) as s:
        lote = s.get(Lote, "21862502")
    detalhe = lote.raw_json["detalhe"]
    assert detalhe["laudo_missing_reason"] == "download_falhou"
    assert "429" in detalhe["laudo_download_erro"]


def test_processar_detalhe_download_sucesso_limpa_missing_reason(db, tmp_path):
    """Download bem-sucedido deve NÃO deixar missing_reason dangling."""
    body_feliz = (
        "VOLKSWAGEN GOL\nANO\n2013/2014\nKM\n112.397\n"
        "Laudo aprovado\nAPROVADO ESTRUTURAL\n"
        "LAUDO DO VEÍCULO\n"
    )
    def dl_ok(url: str, destino: Path) -> None:
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"%PDF-1.4 fake")

    with get_session(db) as s:
        processar_detalhe(
            lote_id="21862502",
            body_text=body_feliz,
            laudo_pdf_url="https://storage.googleapis.com/doc-b2b/fake.pdf",
            session=s,
            pdf_dir=tmp_path / "laudos",
            downloader=dl_ok,
        )

    with get_session(db) as s:
        lote = s.get(Lote, "21862502")
    detalhe = lote.raw_json["detalhe"]
    assert detalhe["laudo_missing_reason"] is None
    assert detalhe["pdf_path_local"].endswith("21862502.pdf")


# =============================================================================
# Sheet exporter renderiza rótulo estruturado
# =============================================================================

@pytest.mark.parametrize("motivo,esperado", [
    ("url_nao_capturada", "⚠ URL não capturada — re-scrape"),
    ("body_vazio", "⚠ Detalhe não coletado — re-scrape"),
    ("download_falhou", "⚠ Download falhou — retry"),
    ("sem_laudo", "Laudo inexistente"),
    (None, "—"),
    ("motivo_novo_que_nao_existe_ainda", "⚠ motivo_novo_que_nao_existe_ainda"),
])
def test_rotulo_laudo_ausente_mapeia_cada_motivo(motivo, esperado):
    assert _rotulo_laudo_ausente(motivo) == esperado
