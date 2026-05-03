"""Backfill de PDFs locais → Google Drive.

Testa `scripts/backfill_laudos_drive.py:backfill` sem bater na API do Google.
Mocka `LaudoDriveClient.upload` e verifica:

  - Filtra corretamente: pula lotes sem PDF, sem cache forte e que já têm Drive URL.
  - Persiste `laudo_drive_url` + `laudo_drive_id` em `raw_json.detalhe`.
  - Idempotente: rodar 2x não levanta erro nem duplica chamadas.
  - `dry_run=True` não persiste nada.
  - Filtro por empresa restringe pra lotes daquela empresa.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.laudo_drive import DriveUploadResult
from scripts.backfill_laudos_drive import backfill


def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _lote(lote_id: str, *, drive_url: Optional[str] = None) -> Lote:
    detalhe = {"laudo_pdf_url": "https://storage.googleapis.com/doc-b2b/x.pdf"}
    if drive_url:
        detalhe["laudo_drive_url"] = drive_url
        detalhe["laudo_drive_id"] = "EXISTING"
    return Lote(
        id=lote_id,
        leilao="auto_avaliar",
        url=f"https://x/{lote_id}",
        marca="Ford",
        modelo="Fiesta",
        ano=2017,
        km=50_000,
        lance_atual=15_000,
        fim_em=datetime.now() + timedelta(days=3),
        origem_cidade="Uberlândia",
        origem_uf="MG",
        raw_json={"detalhe": detalhe},
        scraped_at=datetime.utcnow(),
    )


def _laudo(lote_id: str, *, conf: float = 0.9) -> LaudoCache:
    return LaudoCache(
        lote_id=lote_id,
        avarias_json=[],
        severidade_geral="leve",
        motor_ok=True,
        documentacao="ok",
        categoria_veiculo="hatch",
        confidence=conf,
        modelo_llm="gemini-flash",
        custo_usd=0.0,
        extraido_em=datetime.utcnow(),
    )


def _avaliacao(lote_id: str, empresa_id: str = "carros_uberlandia") -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id=empresa_id, lote_id=lote_id,
        preco_alvo=22000, preco_max=25000,
        score_roi=0.2, fator_risco=0.8, fator_liquidez=1.0,
        margem_aplicada=0.15, frete_incluso=1500,
        reforma_estimada=2000, taxas_leilao=2000,
        preco_giro=30000, preco_giro_fipe=30000,
        justificativa="ok", criado_em=datetime.utcnow(),
    )


def _criar_pdf(pdf_dir: Path, lote_id: str, size: int = 200_000) -> Path:
    pdf_dir.mkdir(parents=True, exist_ok=True)
    p = pdf_dir / f"{lote_id}.pdf"
    p.write_bytes(b"%PDF-1.4\n" + b"x" * size)
    return p


def _drive_mock(uploads: list):
    """Mock do LaudoDriveClient — captura chamadas a upload e devolve fake links."""
    client = MagicMock()
    def _upload(lote_id, pdf_path):
        uploads.append((lote_id, str(pdf_path)))
        return DriveUploadResult(
            file_id=f"FID_{lote_id}",
            web_view_link=f"https://drive.google.com/file/d/FID_{lote_id}/view",
            criado_agora=True,
        )
    client.upload.side_effect = _upload
    return client


class TestBackfillFiltros:
    def test_sobe_lote_com_pdf_e_cache_forte(self, tmp_path: Path):
        engine = _engine_mem()
        _criar_pdf(tmp_path, "L1")
        with Session(engine) as session:
            session.add(_lote("L1"))
            session.add(_laudo("L1"))
            session.add(_avaliacao("L1"))
            session.commit()

            uploads: list = []
            res = backfill(session, _drive_mock(uploads), pdf_dir=tmp_path)

            assert len(uploads) == 1
            assert uploads[0][0] == "L1"
            assert res["candidatos"] == 1
            assert res["subidos"] == 1

            lote = session.get(Lote, "L1")
            assert lote.raw_json["detalhe"]["laudo_drive_url"] == \
                "https://drive.google.com/file/d/FID_L1/view"
            assert lote.raw_json["detalhe"]["laudo_drive_id"] == "FID_L1"

    def test_pula_lote_sem_pdf(self, tmp_path: Path):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L1"))
            session.add(_laudo("L1"))
            session.add(_avaliacao("L1"))
            session.commit()

            uploads: list = []
            res = backfill(session, _drive_mock(uploads), pdf_dir=tmp_path)
            assert uploads == []
            assert res["candidatos"] == 0

    def test_pula_lote_com_cache_fraco(self, tmp_path: Path):
        """Lote com `confidence<0.6` (fallback _laudo_sem_pdf) não vale subir
        — o PDF pode até estar lá mas a extração não viu nada útil."""
        engine = _engine_mem()
        _criar_pdf(tmp_path, "L1")
        with Session(engine) as session:
            session.add(_lote("L1"))
            session.add(_laudo("L1", conf=0.5))
            session.add(_avaliacao("L1"))
            session.commit()

            uploads: list = []
            backfill(session, _drive_mock(uploads), pdf_dir=tmp_path)
            assert uploads == []

    def test_pula_lote_que_ja_tem_drive_url(self, tmp_path: Path):
        engine = _engine_mem()
        _criar_pdf(tmp_path, "L1")
        with Session(engine) as session:
            session.add(_lote(
                "L1",
                drive_url="https://drive.google.com/file/d/PRE_EXISTENTE/view",
            ))
            session.add(_laudo("L1"))
            session.add(_avaliacao("L1"))
            session.commit()

            uploads: list = []
            backfill(session, _drive_mock(uploads), pdf_dir=tmp_path)
            assert uploads == []

    def test_filtro_por_empresa(self, tmp_path: Path):
        engine = _engine_mem()
        _criar_pdf(tmp_path, "L1")
        _criar_pdf(tmp_path, "L2")
        with Session(engine) as session:
            session.add(_lote("L1"))
            session.add(_laudo("L1"))
            session.add(_avaliacao("L1", empresa_id="empresa_A"))
            session.add(_lote("L2"))
            session.add(_laudo("L2"))
            session.add(_avaliacao("L2", empresa_id="empresa_B"))
            session.commit()

            uploads: list = []
            res = backfill(
                session, _drive_mock(uploads),
                empresa_id="empresa_A", pdf_dir=tmp_path,
            )
            assert res["candidatos"] == 1
            assert uploads[0][0] == "L1"


class TestBackfillIdempotencia:
    def test_segunda_passada_nao_chama_upload(self, tmp_path: Path):
        engine = _engine_mem()
        _criar_pdf(tmp_path, "L1")
        with Session(engine) as session:
            session.add(_lote("L1"))
            session.add(_laudo("L1"))
            session.add(_avaliacao("L1"))
            session.commit()

            uploads: list = []
            backfill(session, _drive_mock(uploads), pdf_dir=tmp_path)
            uploads2: list = []
            res2 = backfill(session, _drive_mock(uploads2), pdf_dir=tmp_path)

            # Drive URL já está persistida → 2ª passada filtra fora.
            assert uploads2 == []
            assert res2["candidatos"] == 0


class TestBackfillDryRun:
    def test_dry_run_nao_persiste(self, tmp_path: Path):
        engine = _engine_mem()
        _criar_pdf(tmp_path, "L1")
        with Session(engine) as session:
            session.add(_lote("L1"))
            session.add(_laudo("L1"))
            session.add(_avaliacao("L1"))
            session.commit()

            uploads: list = []
            res = backfill(
                session, _drive_mock(uploads),
                pdf_dir=tmp_path, dry_run=True,
            )
            assert res["candidatos"] == 1
            assert res["subidos"] == 0
            assert uploads == []
            lote = session.get(Lote, "L1")
            assert lote.raw_json["detalhe"].get("laudo_drive_url") is None


class TestBackfillErros:
    def test_erro_no_upload_nao_quebra_loop(self, tmp_path: Path):
        engine = _engine_mem()
        _criar_pdf(tmp_path, "L1")
        _criar_pdf(tmp_path, "L2")
        with Session(engine) as session:
            session.add(_lote("L1"))
            session.add(_laudo("L1"))
            session.add(_avaliacao("L1"))
            session.add(_lote("L2"))
            session.add(_laudo("L2"))
            session.add(_avaliacao("L2"))
            session.commit()

            client = MagicMock()
            calls = {"n": 0}
            def _upload(lote_id, pdf_path):
                calls["n"] += 1
                if lote_id == "L1":
                    raise RuntimeError("boom")
                return DriveUploadResult(
                    file_id=f"FID_{lote_id}",
                    web_view_link=f"https://drive.google.com/file/d/FID_{lote_id}/view",
                    criado_agora=True,
                )
            client.upload.side_effect = _upload

            res = backfill(session, client, pdf_dir=tmp_path)
            assert res["erros"] == 1
            assert res["subidos"] == 1
            # L2 ainda foi processado depois do erro de L1
            assert calls["n"] == 2
            lote2 = session.get(Lote, "L2")
            assert lote2.raw_json["detalhe"].get("laudo_drive_url") is not None
