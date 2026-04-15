"""Testes da camada TEXTUAL do ExtratorLaudo (sem LLM).

Roda offline sobre o PDF real do Fiesta 21854782. A camada de visão fica
coberta por teste de integração separado (tests/integration/) com mock do client.
"""

from pathlib import Path

import pytest

from carros_sa.agents.extrator_laudo import hash_pdf, parse_laudo_textual

PDF_FIESTA = Path(__file__).resolve().parent.parent / "data" / "laudos_amostra" / "21854782_fiesta.pdf"


@pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF de amostra não disponível")
def test_parse_laudo_textual_extrai_identificadores():
    txt = parse_laudo_textual(PDF_FIESTA)
    assert txt.chassi == "3FAFP4EK5DM104385"
    assert txt.placa == "OME8758"
    assert txt.km_laudo == 171_379      # bate com o laudo (diferente do anúncio: 171.053!)


@pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF de amostra não disponível")
def test_parse_laudo_textual_extrai_status_juridico():
    txt = parse_laudo_textual(PDF_FIESTA)
    assert txt.licenciado is True
    assert txt.roubo_furto_ativo is False
    assert txt.comunicado_venda is False
    assert txt.chassi_original is True
    assert txt.motor_original is True


@pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF de amostra não disponível")
def test_parse_laudo_textual_odometro_flag():
    txt = parse_laudo_textual(PDF_FIESTA)
    # Laudo do Fiesta: "Visualização Impossibilitada" do odômetro → flag falsa
    assert txt.odometro_legivel is False


@pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF de amostra não disponível")
def test_hash_pdf_estavel():
    h1 = hash_pdf(PDF_FIESTA)
    h2 = hash_pdf(PDF_FIESTA)
    assert h1 == h2
    assert len(h1) == 40  # SHA1
