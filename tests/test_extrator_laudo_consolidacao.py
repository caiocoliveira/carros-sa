"""Testa a consolidação LaudoTextual + JSON de visão → LaudoEstruturado.

Usa fixture do Gemini salva (tests/fixtures/21854782_visual_gemini.json) pra não
depender de API nem de internet. Valida que:
  - 2 colunas reparadas → severidade ESTRUTURAL
  - motor_ok = False (incompatível com estrutural mesmo com motor original textual)
  - documentação OK (licenciado, sem roubo)
  - avarias individuais marcadas como GRAVE (pq são peças estruturais)
"""

from pathlib import Path

import pytest

from carros_sa.agents.extrator_laudo import extrair_laudo
from carros_sa.models import SeveridadeAvaria, StatusDocumentacao

PDF = Path(__file__).resolve().parent.parent / "data" / "laudos_amostra" / "21854782_fiesta.pdf"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "21854782_visual_gemini.json"


class FixedResponseClient:
    """Mock VisionClient que devolve um JSON pré-gravado — rodar offline."""

    def __init__(self, fixture_path: Path):
        import json
        self._response = json.loads(fixture_path.read_text())
        self.custo_estimado_usd = 0.0

    def classify(self, image_png_bytes: bytes, prompt: str) -> dict:
        return self._response


@pytest.mark.skipif(not PDF.exists() or not FIXTURE.exists(), reason="PDF ou fixture ausente")
def test_consolidacao_fiesta_reprovado_estrutural():
    client = FixedResponseClient(FIXTURE)
    laudo = extrair_laudo(PDF, client)

    assert laudo.severidade_geral == SeveridadeAvaria.ESTRUTURAL
    assert laudo.motor_ok is False                     # severidade estrutural anula motor_ok
    assert laudo.documentacao == StatusDocumentacao.OK
    assert laudo.confidence >= 0.85

    # Duas avarias (uma por coluna reparada), ambas GRAVE
    assert len(laudo.avarias) == 2
    partes = {a.parte for a in laudo.avarias}
    assert partes == {"coluna_b_esquerda", "coluna_c_esquerda"}
    assert all(a.severidade == SeveridadeAvaria.GRAVE for a in laudo.avarias)
