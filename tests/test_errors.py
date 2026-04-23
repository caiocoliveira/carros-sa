"""Smoke tests da hierarquia de erros do pipeline."""

from __future__ import annotations

import pytest

from carros_sa.errors import FipeIndisponivel, PipelineError


def test_hierarquia():
    """FipeIndisponivel deriva de PipelineError → Exception."""
    assert issubclass(FipeIndisponivel, PipelineError)
    assert issubclass(PipelineError, Exception)


def test_carrega_motivo_e_lote_id():
    exc = FipeIndisponivel("marca fora do catálogo", lote_id="12345")
    assert exc.motivo == "marca fora do catálogo"
    assert exc.lote_id == "12345"
    assert "marca fora do catálogo" in str(exc)


def test_lote_id_opcional():
    exc = FipeIndisponivel("motor indisponível")
    assert exc.lote_id is None


def test_pode_ser_capturado_como_pipeline_error():
    with pytest.raises(PipelineError) as info:
        raise FipeIndisponivel("moto sem catálogo", lote_id="99")
    assert info.value.lote_id == "99"
