"""Smoke tests do AppSettings — garante defaults estáveis e override via env."""

from __future__ import annotations

import pytest

from carros_sa.config import AppSettings, get_settings, reset_settings_cache


def test_defaults_ancorados():
    """Valores default são os números que o código legado usava hardcoded.
    Se algum mudar, precisa ser decisão consciente (e este teste quebra)."""
    s = AppSettings()
    assert s.scrape_sleep_min_s == 5.0
    assert s.scrape_sleep_max_s == 8.0
    assert s.pdf_min_bytes == 5_000
    assert s.body_text_sample_bytes == 8_000
    assert s.laudo_confidence_ok_threshold == 0.6
    assert s.llm_retry_delays_s == (0, 15, 45)
    assert 429 in s.llm_retry_http_codes and 503 in s.llm_retry_http_codes
    assert s.reforma_reserva_imprevistos_brl == 1_000
    assert "SP" in s.ufs_adjacentes_mg
    assert s.frete_km_mesma_uf == 150


def test_marcadores_pdf_incluem_casos_conhecidos():
    """Marcador negativo 'RELATÓRIO DE TRANSPARÊNCIA' é essencial — footer
    institucional da Auto Avaliar contaminou a base em abril/2026."""
    s = AppSettings()
    assert "LAUDO" in s.pdf_markers_positivos
    assert "CHASSI" in s.pdf_markers_positivos
    assert any("TRANSPAR" in m for m in s.pdf_markers_negativos)


def test_override_via_env(monkeypatch: pytest.MonkeyPatch):
    """AppSettings respeita CARROS_SA_<CAMPO> do ambiente."""
    monkeypatch.setenv("CARROS_SA_SCRAPE_SLEEP_MIN_S", "2.0")
    monkeypatch.setenv("CARROS_SA_REFORMA_RESERVA_IMPREVISTOS_BRL", "500")
    reset_settings_cache()
    try:
        s = get_settings()
        assert s.scrape_sleep_min_s == 2.0
        assert s.reforma_reserva_imprevistos_brl == 500
    finally:
        reset_settings_cache()


def test_get_settings_cacheado():
    """get_settings() devolve a MESMA instância — cache lru_cache(maxsize=1)."""
    reset_settings_cache()
    a = get_settings()
    b = get_settings()
    assert a is b
