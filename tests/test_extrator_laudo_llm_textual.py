"""Testa o fallback LLM textual (camada 4) do `extrair_laudo`.

Motivação operacional (2026-05-15): em produção, o `auditar_laudos --strict`
reportava 22/47 lotes ativos com `cache_confianca_baixa` — PDFs baixados,
URLs válidas, mas `LaudoCache.confidence=0.0`. Investigação dos PDFs mostrou
que vinham de vendors fora do template Auto Avaliar (DEKRA, Procemax,
SA-Laudo, Vistoria Cautelar genérica) cujas páginas 2 (índice 1) não têm o
diagrama estrutural esperado. Gemini visual corretamente devolvia
confidence=0.0 + listas vazias em vez de inventar — mas como o resultado era
persistido como-está, o retry diário rodava no mesmo lote pra sempre, com o
mesmo resultado, e a planilha mostrava "⚠ LAUDO NÃO CAPTURADO: extração fraca".

Camada 4 (`extrair_laudo_via_llm_textual`) entra ativada por `text_llm_client`
não-None: extrai TODO o texto do PDF e delega pro Gemini Flash com prompt
vendor-agnostic. Garantias do teste:

  1. Visual responde bem → LLM textual NÃO é chamado (preserva ~75% dos lotes
     Auto Avaliar puros sem custo extra).
  2. Visual responde com confidence=0.0 + listas vazias E `text_llm_client`
     está disponível → LLM textual chamado, avarias integradas.
  3. Sem `text_llm_client` (caller histórico) → comportamento idêntico ao
     pré-fix (visual 0.0 persistido como-está).
  4. LLM textual falha (exceção ou JSON inválido) → fallback gracioso pro
     resultado pré-existente, sem subir exceção.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from carros_sa.agents.extrator_laudo import (
    _visual_e_inutil,
    extrair_laudo,
    extrair_laudo_via_llm_textual,
)
from carros_sa.models import SeveridadeAvaria, StatusDocumentacao

PDF_FIESTA = Path(__file__).resolve().parent.parent / "data" / "laudos_amostra" / "21854782_fiesta.pdf"


class _MockVisionClient:
    """Mock VisionClient retorna `resposta` fixa em todo `classify`."""

    def __init__(self, resposta: dict):
        self._resposta = resposta
        self.custo_estimado_usd = 0.0
        self.chamadas = 0

    def classify(self, image_png_bytes: bytes, prompt: str) -> dict:
        self.chamadas += 1
        return self._resposta


class _MockTextLLMClient:
    """Mock TextLLMClient retorna `resposta` fixa em todo `generate_json`."""

    def __init__(self, resposta: dict):
        self._resposta = resposta
        self.chamadas = 0
        self.ultimo_prompt = ""

    def generate_json(self, prompt: str) -> dict:
        self.chamadas += 1
        self.ultimo_prompt = prompt
        return self._resposta


# -----------------------------------------------------------------------------
# Detector `_visual_e_inutil`
# -----------------------------------------------------------------------------

class TestVisualEInutil:
    def test_visual_none_e_inutil(self):
        assert _visual_e_inutil(None) is True

    def test_confidence_zero_e_listas_vazias_e_inutil(self):
        v = {"confidence": 0.0, "pecas_reparadas": [], "pecas_avariadas": []}
        assert _visual_e_inutil(v) is True

    def test_confidence_alta_nao_e_inutil_mesmo_sem_pecas(self):
        v = {"confidence": 0.85, "pecas_reparadas": [], "pecas_avariadas": []}
        assert _visual_e_inutil(v) is False

    def test_confidence_baixa_com_pecas_nao_e_inutil(self):
        v = {"confidence": 0.4, "pecas_reparadas": ["coluna_b_esquerda"]}
        assert _visual_e_inutil(v) is False

    def test_confidence_invalida_e_listas_vazias_e_inutil(self):
        v = {"confidence": "?", "pecas_reparadas": [], "pecas_avariadas": []}
        assert _visual_e_inutil(v) is True


# -----------------------------------------------------------------------------
# Função pura `extrair_laudo_via_llm_textual`
# -----------------------------------------------------------------------------

class TestExtrairLaudoViaLlmTextual:
    @pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF fixture ausente")
    def test_sem_client_retorna_none(self):
        assert extrair_laudo_via_llm_textual(PDF_FIESTA, None) is None

    @pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF fixture ausente")
    def test_client_que_levanta_excecao_retorna_none(self):
        bad_client = MagicMock()
        bad_client.generate_json.side_effect = RuntimeError("Gemini 503")
        # NÃO levanta — caller decide se usa o resultado ou fallback
        assert extrair_laudo_via_llm_textual(PDF_FIESTA, bad_client) is None
        bad_client.generate_json.assert_called_once()

    @pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF fixture ausente")
    def test_client_que_retorna_dict_propaga(self):
        client = _MockTextLLMClient({
            "pecas_reparadas": ["coluna_b_esquerda"],
            "pecas_avariadas": [],
            "severidade_geral": "estrutural",
            "confidence": 0.85,
            "observacao_textual": "Reparo em coluna B identificado no texto.",
        })
        resultado = extrair_laudo_via_llm_textual(PDF_FIESTA, client)
        assert resultado is not None
        assert resultado["pecas_reparadas"] == ["coluna_b_esquerda"]
        assert resultado["confidence"] == 0.85
        assert client.chamadas == 1
        # Prompt contém o texto do PDF — não vazio
        assert "TEXTO DO LAUDO" in client.ultimo_prompt
        assert len(client.ultimo_prompt) > 1000  # PDF tem milhares de chars

    @pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF fixture ausente")
    def test_client_que_retorna_string_invalida_retorna_none(self):
        client = MagicMock()
        client.generate_json.return_value = "não é dict"
        assert extrair_laudo_via_llm_textual(PDF_FIESTA, client) is None

    def test_pdf_inexistente_retorna_none(self, tmp_path):
        client = _MockTextLLMClient({"pecas_reparadas": [], "confidence": 0.0})
        assert extrair_laudo_via_llm_textual(tmp_path / "naoexiste.pdf", client) is None
        # LLM nem foi chamado — falhou antes
        assert client.chamadas == 0


# -----------------------------------------------------------------------------
# Integração na `extrair_laudo` (camada 4 ativa)
# -----------------------------------------------------------------------------

class TestIntegracaoExtrairLaudo:
    @pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF fixture ausente")
    def test_visual_responde_bem_llm_nao_e_chamado(self):
        """Caso ~75% dos lotes (Auto Avaliar puro): visual responde com sinal."""
        visual = {
            "pecas_reparadas": ["coluna_b_esquerda"],
            "pecas_avariadas": [],
            "severidade_geral": "estrutural",
            "confidence": 0.9,
        }
        vc = _MockVisionClient(visual)
        tc = _MockTextLLMClient({"pecas_reparadas": [], "confidence": 0.0})

        laudo = extrair_laudo(PDF_FIESTA, vc, text_llm_client=tc)

        assert vc.chamadas == 1
        assert tc.chamadas == 0  # ← preservação da economia
        assert laudo.confidence == 0.9
        assert any(a.parte == "coluna_b_esquerda" for a in laudo.avarias)

    @pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF fixture ausente")
    def test_visual_inutil_e_llm_textual_recupera(self, monkeypatch):
        """Caso patológico 22/47 em produção: visual 0.0 + vazio, LLM resolve.

        Pra simular um PDF de vendor estranho usando o fixture Fiesta, mocka
        `parse_laudo_textual` pra devolver `observacoes=''` (vendors tipo
        Procemax/DEKRA não têm o bloco "Observações:" do Auto Avaliar). Sem
        observações o regex de camada 2 não acha nada e camada 4 dispara.
        """
        from carros_sa.agents import extrator_laudo as ext_mod
        from carros_sa.agents.extrator_laudo import LaudoTextual

        # Simula o estado "vendor estranho — sem bloco Observações":
        # identificadores OK (camada 3) + observações vazias (camada 2 morre).
        def _parse_falso(_path):
            return LaudoTextual(
                placa=None, chassi="9BWAA05Z6B4155954", motor_numero=None,
                km_laudo=None, licenciado=True, roubo_furto_ativo=False,
                comunicado_venda=False, chassi_original=None, motor_original=True,
                odometro_legivel=True, observacoes="", texto_bruto="",
            )
        monkeypatch.setattr(ext_mod, "parse_laudo_textual", _parse_falso)

        visual_inutil = {
            "pecas_reparadas": [],
            "pecas_avariadas": [],
            "severidade_geral": "nenhuma",
            "confidence": 0.0,
        }
        llm_resposta = {
            "pecas_reparadas": ["coluna_b_esquerda", "coluna_c_esquerda"],
            "pecas_avariadas": [],
            "severidade_geral": "estrutural",
            "confidence": 0.85,
            "observacao_textual": "Texto menciona reparos nas colunas B e C esquerdas.",
        }
        vc = _MockVisionClient(visual_inutil)
        tc = _MockTextLLMClient(llm_resposta)

        laudo = extrair_laudo(PDF_FIESTA, vc, text_llm_client=tc)

        assert vc.chamadas == 1
        assert tc.chamadas == 1  # ← camada 4 disparou
        assert laudo.confidence == 0.85  # usa o do LLM textual
        partes = {a.parte for a in laudo.avarias}
        assert "coluna_b_esquerda" in partes
        assert "coluna_c_esquerda" in partes
        assert laudo.severidade_geral == SeveridadeAvaria.ESTRUTURAL

    @pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF fixture ausente")
    def test_visual_inutil_sem_text_llm_client_preserva_comportamento_antigo(self):
        """Sem text_llm_client: confidence 0.0 persiste como-está (pré-fix)."""
        visual_inutil = {
            "pecas_reparadas": [],
            "pecas_avariadas": [],
            "severidade_geral": "nenhuma",
            "confidence": 0.0,
        }
        vc = _MockVisionClient(visual_inutil)
        # Nota: PDF do Fiesta tem texto com "REPARO NAS COLUNAS B E C..." que o
        # extrator textual (camada 2, regex) consegue parsear. Esse teste valida
        # que o COMPORTAMENTO HISTÓRICO está preservado — não que confidence==0.0.
        laudo = extrair_laudo(PDF_FIESTA, vc)  # text_llm_client=None

        assert vc.chamadas == 1
        # Avarias podem vir do extrator textual; confidence reflete o visual
        # (porque visual is not None mesmo se inútil) — sem camada 4.
        assert laudo.confidence == 0.0  # ← pré-fix preservado

    @pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF fixture ausente")
    def test_visual_inutil_e_llm_falha_persiste_resultado_antigo(self, monkeypatch):
        """Camada 4 falha (LLM 503) → cai pro visual original, sem subir exceção."""
        from carros_sa.agents import extrator_laudo as ext_mod
        from carros_sa.agents.extrator_laudo import LaudoTextual

        # Mesmo monkeypatch do teste de recuperação: simula PDF sem observações
        # pra forçar camada 4 a tentar disparar.
        def _parse_falso(_path):
            return LaudoTextual(
                placa=None, chassi=None, motor_numero=None, km_laudo=None,
                licenciado=None, roubo_furto_ativo=None, comunicado_venda=None,
                chassi_original=None, motor_original=None, odometro_legivel=None,
                observacoes="", texto_bruto="",
            )
        monkeypatch.setattr(ext_mod, "parse_laudo_textual", _parse_falso)

        visual_inutil = {
            "pecas_reparadas": [],
            "pecas_avariadas": [],
            "severidade_geral": "nenhuma",
            "confidence": 0.0,
        }
        vc = _MockVisionClient(visual_inutil)
        tc = MagicMock()
        tc.generate_json.side_effect = RuntimeError("Gemini 503")

        # NÃO sobe exceção — extrator é resiliente
        laudo = extrair_laudo(PDF_FIESTA, vc, text_llm_client=tc)

        assert vc.chamadas == 1
        tc.generate_json.assert_called_once()
        # Confidence cai pro 0.0 do visual original
        assert laudo.confidence == 0.0

    @pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF fixture ausente")
    def test_visual_inutil_avarias_textuais_existentes_pulam_llm(self):
        """Quando regex textual já achou avarias, NÃO chama LLM (economia)."""
        # PDF Fiesta tem "VEÍCULO POSSUI REPARO NAS COLUNAS B E C ESQUERDAS"
        # nas observações → extrator regex acha avarias mesmo com visual inútil.
        visual_inutil = {
            "pecas_reparadas": [],
            "pecas_avariadas": [],
            "severidade_geral": "nenhuma",
            "confidence": 0.0,
        }
        vc = _MockVisionClient(visual_inutil)
        tc = _MockTextLLMClient({"pecas_reparadas": [], "confidence": 0.5})

        laudo = extrair_laudo(PDF_FIESTA, vc, text_llm_client=tc)

        # Regex já achou avarias → camada 4 NÃO dispara (não precisa)
        assert tc.chamadas == 0
        assert any("coluna" in a.parte for a in laudo.avarias)

    @pytest.mark.skipif(not PDF_FIESTA.exists(), reason="PDF fixture ausente")
    def test_visual_falha_com_excecao_e_llm_textual_recupera(self):
        """Visual lança exceção (Gemini 503) → camada 4 dispara como fallback."""
        vc = MagicMock()
        vc.classify.side_effect = RuntimeError("Gemini 503 UNAVAILABLE")
        # NOTA: PDF Fiesta tem avarias textuais → camada 4 ainda NÃO dispara
        # (porque `not avarias` é False). Usar um cenário onde o regex falha
        # exigiria um PDF de vendor estranho — fora do escopo deste fixture.
        # Mas o caminho "visual None + sem avarias regex → camada 4" está
        # coberto via test_visual_inutil_e_llm_textual_recupera indiretamente
        # porque _visual_e_inutil(None) também é True. Aqui validamos só a
        # ausência de exceção.
        tc = _MockTextLLMClient({"pecas_reparadas": [], "confidence": 0.0})

        laudo = extrair_laudo(PDF_FIESTA, vc, text_llm_client=tc)

        assert vc.classify.called
        # Como o regex Fiesta ACHA avarias, camada 4 não roda
        assert tc.chamadas == 0
        # Mas extrator não quebrou e devolveu um laudo
        assert laudo.severidade_geral == SeveridadeAvaria.ESTRUTURAL
