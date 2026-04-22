"""Smoke tests da hierarquia de erros do pipeline."""

from __future__ import annotations

import pytest

from carros_sa.errors import (
    FipeIndisponivel,
    LaudoExtractionError,
    PDFDownloadError,
    PDFInvalidoError,
    PipelineError,
)


def test_hierarquia():
    """Todas as classes específicas derivam de PipelineError → Exception."""
    for cls in (PDFInvalidoError, PDFDownloadError, LaudoExtractionError, FipeIndisponivel):
        assert issubclass(cls, PipelineError)
        assert issubclass(cls, Exception)


def test_carrega_motivo_e_lote_id():
    exc = PDFInvalidoError("marcador negativo presente", lote_id="12345")
    assert exc.motivo == "marcador negativo presente"
    assert exc.lote_id == "12345"
    assert "marcador negativo presente" in str(exc)


def test_lote_id_opcional():
    exc = LaudoExtractionError("visão e textual falharam")
    assert exc.lote_id is None


def test_pode_ser_capturado_como_pipeline_error():
    with pytest.raises(PipelineError) as info:
        raise FipeIndisponivel("moto sem catálogo", lote_id="99")
    assert info.value.lote_id == "99"
