"""Fixtures compartilhadas e registro de markers.

Centraliza os skips condicionados a dados reais (PDFs de laudo, fixtures de visão)
em vez de repetir `@pytest.mark.skipif` em cada arquivo. Também declara o marker
`requires_real_data` pra que `pytest -m "not requires_real_data"` rode só os
testes puramente determinísticos — útil em CI quando os arquivos gold não estão
disponíveis.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PDF_FIESTA = REPO_ROOT / "data" / "laudos_amostra" / "21854782_fiesta.pdf"
FIXTURE_VISUAL_FIESTA = REPO_ROOT / "tests" / "fixtures" / "21854782_visual_gemini.json"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_real_data: marca testes que dependem de PDFs/fixtures "
        "reais (pulam silenciosamente se ausentes)",
    )


@pytest.fixture(scope="session")
def pdf_fiesta_real() -> Path:
    """Path pro PDF real do lote 21854782 (Fiesta 2013 REPROVADO ESTRUTURAL).

    Pula o teste com mensagem clara se o PDF não está presente no checkout.
    """
    if not PDF_FIESTA.exists():
        pytest.skip(
            f"PDF real ausente em {PDF_FIESTA.relative_to(REPO_ROOT)}. "
            "Este gold test depende de dado real; ver CLAUDE.md seção 'Dado real de referência'."
        )
    return PDF_FIESTA


@pytest.fixture(scope="session")
def fixture_visual_fiesta() -> Path:
    """Path pro JSON de resposta Gemini pro lote Fiesta (evita chamar LLM em teste)."""
    if not FIXTURE_VISUAL_FIESTA.exists():
        pytest.skip(
            f"Fixture de visão ausente em {FIXTURE_VISUAL_FIESTA.relative_to(REPO_ROOT)}."
        )
    return FIXTURE_VISUAL_FIESTA
