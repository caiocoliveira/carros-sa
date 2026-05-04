"""Auditoria de completude de laudo: as 3 condições juntas + fix do exporter.

Cobertura:

1. `verificar_laudo_completo` para cada combinação relevante das 3 condições
   (PDF presente, LaudoCache conf>=0.6, URL passa em is_laudo_pdf_url).

2. `auditar` agrega corretamente, contabiliza completos vs incompletos e
   ignora lotes encerrados quando `apenas_ativos=True` (espelho do exporter).

3. Exporter renderiza "PDF salvo (link expirado)" quando o PDF local existe
   mas a URL pré-assinada já expirou. É o estado novo introduzido pelo fix —
   antes, o usuário via "—" idêntico ao caso de "laudo nem foi analisado",
   ofuscando a diferença operacional.

Esses testes blindam contra a regressão "lotes ativos sem laudo completo
voltam a aparecer na planilha sem ninguém perceber". Quando algum dos 3
sintomas reaparecer, `make auditar-laudos` reporta + `make test` pega aqui.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.laudo_audit import (
    PDF_DIR_DEFAULT,
    auditar,
    verificar_laudo_completo,
)
from carros_sa.tools.sheets import SheetsExporter

URL_OK = "https://storage.googleapis.com/doc-b2b/laudo-abc.pdf"
URL_DECOY = (
    "https://repo-site-aav-production.storage.googleapis.com/app/uploads/"
    "Relatorio-de-Transparencia.pdf"
)


def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _lote(
    lote_id: str = "L001",
    *,
    laudo_pdf_url: Optional[str] = URL_OK,
    laudo_drive_url: Optional[str] = None,
    fim_em: Optional[datetime] = None,
    encerrado: bool = False,
) -> Lote:
    if fim_em is None:
        fim_em = datetime.now() + timedelta(days=3)
    detalhe = {"laudo_pdf_url": laudo_pdf_url}
    if laudo_drive_url is not None:
        detalhe["laudo_drive_url"] = laudo_drive_url
    if encerrado:
        detalhe["encerrado"] = True
    return Lote(
        id=lote_id,
        leilao="auto_avaliar",
        url=f"https://autoavaliar.com.br/lote/{lote_id}",
        marca="Ford",
        modelo="Fiesta",
        ano=2017,
        km=50_000,
        lance_atual=15_000,
        fim_em=fim_em,
        origem_cidade="Uberlândia",
        origem_uf="MG",
        raw_json={"detalhe": detalhe},
        scraped_at=datetime.utcnow(),
    )


def _laudo(lote_id: str = "L001", confidence: float = 0.9) -> LaudoCache:
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


def _avaliacao(lote_id: str = "L001", empresa_id: str = "carros_uberlandia") -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id=empresa_id,
        lote_id=lote_id,
        preco_alvo=22000,
        preco_max=25000,
        score_roi=0.2,
        fator_risco=0.8,
        fator_liquidez=1.0,
        margem_aplicada=0.15,
        frete_incluso=1500,
        reforma_estimada=2000,
        taxas_leilao=2000,
        preco_giro=30000,
        preco_giro_fipe=30000,
        justificativa="ok",
        criado_em=datetime.utcnow(),
    )


def _criar_pdf_fake(pdf_dir: Path, lote_id: str, size_bytes: int = 200_000) -> Path:
    pdf_dir.mkdir(parents=True, exist_ok=True)
    p = pdf_dir / f"{lote_id}.pdf"
    p.write_bytes(b"%PDF-1.4\n" + b"x" * size_bytes)
    return p


# ---------------------------------------------------------------------------
# verificar_laudo_completo — matriz das 3 condições
# ---------------------------------------------------------------------------

class TestVerificarLaudoCompleto:
    def test_tudo_ok_e_completo(self, tmp_path):
        _criar_pdf_fake(tmp_path, "L001")
        s = verificar_laudo_completo(_lote("L001"), _laudo(confidence=0.9), pdf_dir=tmp_path)
        assert s.completo is True
        assert s.motivo is None

    def test_pdf_ausente_marca_incompleto(self, tmp_path):
        # PDF não foi criado — só URL e cache OK.
        s = verificar_laudo_completo(_lote("L001"), _laudo(confidence=0.9), pdf_dir=tmp_path)
        assert s.pdf_local is False
        assert s.completo is False
        assert "pdf_ausente" in s.motivo

    def test_pdf_muito_pequeno_marca_incompleto(self, tmp_path):
        """Arquivos <5KB são quase sempre erro de download (HTML salvo)."""
        _criar_pdf_fake(tmp_path, "L001", size_bytes=100)  # ~100B + header
        s = verificar_laudo_completo(_lote("L001"), _laudo(), pdf_dir=tmp_path)
        assert s.pdf_local is False

    def test_cache_baixa_confianca_marca_incompleto(self, tmp_path):
        """confidence=0.5 é o fallback `_laudo_sem_pdf` — não conta como analisado."""
        _criar_pdf_fake(tmp_path, "L001")
        s = verificar_laudo_completo(_lote("L001"), _laudo(confidence=0.5), pdf_dir=tmp_path)
        assert s.laudo_cache_ok is False
        assert "cache_confianca_baixa" in s.motivo

    def test_cache_ausente_marca_incompleto(self, tmp_path):
        _criar_pdf_fake(tmp_path, "L001")
        s = verificar_laudo_completo(_lote("L001"), None, pdf_dir=tmp_path)
        assert s.laudo_cache_ok is False

    def test_url_decoy_marca_incompleto(self, tmp_path):
        _criar_pdf_fake(tmp_path, "L001")
        s = verificar_laudo_completo(
            _lote("L001", laudo_pdf_url=URL_DECOY),
            _laudo(),
            pdf_dir=tmp_path,
        )
        assert s.url_persistida_ok is False
        assert "url_invalida_ou_ausente" in s.motivo

    def test_url_ausente_marca_incompleto(self, tmp_path):
        _criar_pdf_fake(tmp_path, "L001")
        s = verificar_laudo_completo(
            _lote("L001", laudo_pdf_url=None),
            _laudo(),
            pdf_dir=tmp_path,
        )
        assert s.url_persistida_ok is False

    def test_motivo_concatena_multiplas_falhas(self, tmp_path):
        s = verificar_laudo_completo(
            _lote("L001", laudo_pdf_url=None),  # URL ausente
            None,                                # cache ausente
            pdf_dir=tmp_path,                    # PDF ausente
        )
        assert "pdf_ausente" in s.motivo
        assert "cache_confianca_baixa" in s.motivo
        assert "url_invalida_ou_ausente" in s.motivo

    def test_drive_url_satisfaz_url_persistida_mesmo_sem_storage_url(self, tmp_path):
        """Drive URL é link permanente — basta dela existir pra audit considerar
        URL satisfeita. Storage pré-assinada expira em ~1h, Drive não."""
        _criar_pdf_fake(tmp_path, "L001")
        s = verificar_laudo_completo(
            _lote("L001",
                  laudo_pdf_url=None,  # storage expirou/sumiu
                  laudo_drive_url="https://drive.google.com/file/d/abc/view"),
            _laudo(confidence=0.9),
            pdf_dir=tmp_path,
        )
        assert s.url_persistida_ok is True
        assert s.completo is True

    def test_drive_url_e_storage_url_ambas_validas_continua_completo(self, tmp_path):
        _criar_pdf_fake(tmp_path, "L001")
        s = verificar_laudo_completo(
            _lote("L001", laudo_pdf_url=URL_OK,
                  laudo_drive_url="https://drive.google.com/file/d/abc/view"),
            _laudo(confidence=0.9),
            pdf_dir=tmp_path,
        )
        assert s.url_persistida_ok is True
        assert s.completo is True


# ---------------------------------------------------------------------------
# auditar — agregação + filtro de ativos
# ---------------------------------------------------------------------------

class TestAuditar:
    def test_lote_completo_e_lote_pendente_separados(self, tmp_path):
        engine = _engine_mem()
        _criar_pdf_fake(tmp_path, "L_OK")
        # L_OK: tudo certo. L_FAIL: sem PDF, sem URL.
        with Session(engine) as session:
            session.add(_lote("L_OK", laudo_pdf_url=URL_OK))
            session.add(_avaliacao("L_OK"))
            session.add(_laudo("L_OK", confidence=0.95))
            session.add(_lote("L_FAIL", laudo_pdf_url=None))
            session.add(_avaliacao("L_FAIL"))
            session.add(_laudo("L_FAIL", confidence=0.5))
            session.commit()

            rel = auditar(session, "carros_uberlandia", pdf_dir=tmp_path)

        assert rel.total == 2
        assert rel.completos == 1
        assert len(rel.incompletos) == 1
        assert rel.incompletos[0].lote_id == "L_FAIL"
        assert rel.sem_pdf == 1
        assert rel.cache_baixa_conf == 1
        assert rel.url_invalida == 1

    def test_apenas_ativos_ignora_encerrados_e_passados(self, tmp_path):
        engine = _engine_mem()
        with Session(engine) as session:
            # Lote ativo, incompleto.
            session.add(_lote("L_ATIVO", laudo_pdf_url=None))
            session.add(_avaliacao("L_ATIVO"))
            # Lote encerrado por badge — deve ser ignorado.
            session.add(_lote("L_ENCERRADO", laudo_pdf_url=None, encerrado=True))
            session.add(_avaliacao("L_ENCERRADO"))
            # Lote com fim_em no passado — deve ser ignorado.
            session.add(_lote("L_PASSADO", laudo_pdf_url=None,
                              fim_em=datetime.now() - timedelta(days=1)))
            session.add(_avaliacao("L_PASSADO"))
            session.commit()

            rel = auditar(session, "carros_uberlandia", pdf_dir=tmp_path)

        assert rel.total == 1
        assert rel.incompletos[0].lote_id == "L_ATIVO"

    def test_incluir_encerrados_conta_tudo(self, tmp_path):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_ATIVO", laudo_pdf_url=None))
            session.add(_avaliacao("L_ATIVO"))
            session.add(_lote("L_ENCERRADO", laudo_pdf_url=None, encerrado=True))
            session.add(_avaliacao("L_ENCERRADO"))
            session.commit()

            rel = auditar(session, "carros_uberlandia", pdf_dir=tmp_path,
                          apenas_ativos=False)

        assert rel.total == 2

    def test_empresa_diferente_nao_aparece(self, tmp_path):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L1", laudo_pdf_url=None))
            session.add(_avaliacao("L1", empresa_id="outra_empresa"))
            session.commit()

            rel = auditar(session, "carros_uberlandia", pdf_dir=tmp_path)

        assert rel.total == 0

    def test_sem_avaliacao_nao_e_auditado(self, tmp_path):
        """Lote ingerido mas ainda não avaliado não tem por que ser auditado —
        ele nem aparece na planilha. Audit foca no que o operador VÊ."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_NAO_AVALIADO", laudo_pdf_url=None))
            session.commit()

            rel = auditar(session, "carros_uberlandia", pdf_dir=tmp_path)

        assert rel.total == 0


# ---------------------------------------------------------------------------
# Exporter: nova célula "PDF salvo (link expirado)"
# ---------------------------------------------------------------------------

class TestExporterLaudoCelula:
    def test_pdf_local_sem_url_renderiza_link_expirado(self, tmp_path, monkeypatch):
        """Quando o PDF está em data/laudos_pdfs/ mas a URL pré-assinada já
        expirou, o exporter mostra texto descritivo em vez de '—' — o operador
        precisa saber que o laudo FOI analisado."""
        # Aponta PDF_DIR_DEFAULT pro tmp_path pra teste isolado.
        monkeypatch.setattr("carros_sa.tools.sheets.PDF_DIR_DEFAULT", tmp_path)
        _criar_pdf_fake(tmp_path, "L_LINK_EXPIRADO")

        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_LINK_EXPIRADO", laudo_pdf_url=None))
            session.add(_avaliacao("L_LINK_EXPIRADO"))
            session.add(_laudo("L_LINK_EXPIRADO", confidence=0.95))
            session.commit()

        captured_rows = []
        mock_ws = MagicMock()
        mock_ws.update.side_effect = lambda rows, **kw: captured_rows.append(rows)
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_gc = MagicMock()
        mock_gc.open_by_key.return_value = mock_sh

        with patch("gspread.service_account", return_value=mock_gc):
            exporter = SheetsExporter(spreadsheet_id="x", credentials_path="/x")
            with Session(engine) as session:
                exporter.exportar("carros_uberlandia", session)

        # Primeira chamada de .update = aba da empresa.
        rows_aba_empresa = captured_rows[0]
        # rows[0]=banner, rows[1]=HEADER, rows[2]=primeira linha de dados
        primeira_linha_dados = rows_aba_empresa[2]
        # Coluna "Laudo" é a última do HEADER.
        celula_laudo = primeira_linha_dados[-1]
        assert celula_laudo == "PDF salvo (link expirado)"

    def test_sem_pdf_e_sem_url_renderiza_em_branco(self, tmp_path, monkeypatch):
        """Sem PDF local nem URL válida, continua mostrando '—' (fallback antigo)."""
        monkeypatch.setattr("carros_sa.tools.sheets.PDF_DIR_DEFAULT", tmp_path)
        # Não cria PDF.

        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_VAZIO", laudo_pdf_url=None))
            session.add(_avaliacao("L_VAZIO"))
            session.add(_laudo("L_VAZIO", confidence=0.95))
            session.commit()

        captured_rows = []
        mock_ws = MagicMock()
        mock_ws.update.side_effect = lambda rows, **kw: captured_rows.append(rows)
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_gc = MagicMock()
        mock_gc.open_by_key.return_value = mock_sh

        with patch("gspread.service_account", return_value=mock_gc):
            exporter = SheetsExporter(spreadsheet_id="x", credentials_path="/x")
            with Session(engine) as session:
                exporter.exportar("carros_uberlandia", session)

        primeira_linha_dados = captured_rows[0][2]
        celula_laudo = primeira_linha_dados[-1]
        assert celula_laudo == "—"

    def test_url_valida_continua_renderizando_hyperlink(self, tmp_path, monkeypatch):
        """Regressão: o caso feliz não pode ter quebrado."""
        monkeypatch.setattr("carros_sa.tools.sheets.PDF_DIR_DEFAULT", tmp_path)

        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_URL_OK", laudo_pdf_url=URL_OK))
            session.add(_avaliacao("L_URL_OK"))
            session.add(_laudo("L_URL_OK", confidence=0.95))
            session.commit()

        captured_rows = []
        mock_ws = MagicMock()
        mock_ws.update.side_effect = lambda rows, **kw: captured_rows.append(rows)
        mock_sh = MagicMock()
        mock_sh.worksheet.return_value = mock_ws
        mock_gc = MagicMock()
        mock_gc.open_by_key.return_value = mock_sh

        with patch("gspread.service_account", return_value=mock_gc):
            exporter = SheetsExporter(spreadsheet_id="x", credentials_path="/x")
            with Session(engine) as session:
                exporter.exportar("carros_uberlandia", session)

        celula_laudo = captured_rows[0][2][-1]
        assert celula_laudo.startswith("=HYPERLINK(")
        assert "Ver laudo" in celula_laudo
