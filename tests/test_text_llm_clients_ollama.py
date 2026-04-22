"""Unit tests do OllamaTextClient e novos modos da factory.

Não chamam Ollama real — `httpx.Client.post` é mockado (regra CLAUDE.md
linha 99: sem LLM real em teste). Valida:
  - request shape (sem campo `images`, format=json, temperature=0)
  - parse de resposta com fence markdown
  - raise_for_status em erro HTTP
  - fallback Ollama→Gemini quando Ollama não responde
"""

from __future__ import annotations

import json
from typing import List, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest

from carros_sa.agents.text_llm_clients import (
    FallbackTextLLMClient,
    OllamaTextClient,
    TextLLMClient,
    build_default_text_client,
)


def _fake_response(json_body: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestOllamaTextClient:
    def test_request_shape_has_no_images_field(self):
        client = OllamaTextClient(model="gemma3:4b")
        with patch.object(client._http, "post") as mock_post:
            mock_post.return_value = _fake_response(
                {"response": '{"ok": true}'},
            )
            client.generate_json("prompt de teste")

        assert mock_post.call_count == 1
        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        assert payload["model"] == "gemma3:4b"
        assert payload["prompt"] == "prompt de teste"
        assert payload["stream"] is False
        assert payload["format"] == "json"
        assert payload["options"] == {"temperature": 0.0}
        assert "images" not in payload

    def test_strips_markdown_fence_from_response(self):
        client = OllamaTextClient()
        payload = '```json\n{"itens": [], "custo_total": 1234}\n```'
        with patch.object(client._http, "post") as mock_post:
            mock_post.return_value = _fake_response({"response": payload})
            result = client.generate_json("x")

        assert result == {"itens": [], "custo_total": 1234}

    def test_parses_raw_json_without_fence(self):
        client = OllamaTextClient()
        with patch.object(client._http, "post") as mock_post:
            mock_post.return_value = _fake_response(
                {"response": '{"a": 1}'},
            )
            assert client.generate_json("x") == {"a": 1}

    def test_http_error_propagates(self):
        client = OllamaTextClient()
        with patch.object(client._http, "post") as mock_post:
            mock_post.return_value = _fake_response(
                {"error": "model not found"}, status_code=404,
            )
            with pytest.raises(httpx.HTTPStatusError):
                client.generate_json("x")

    def test_custo_estimado_zero(self):
        assert OllamaTextClient().custo_estimado_usd == 0.0

    def test_custom_host_and_model(self):
        client = OllamaTextClient(model="gemma3:12b", host="http://remote:11434")
        assert client._model == "gemma3:12b"
        assert str(client._http.base_url) == "http://remote:11434"


class _FakeFailingOllama(TextLLMClient):
    """Simula Ollama offline (ConnectError)."""

    def generate_json(self, prompt: str) -> dict:
        raise httpx.ConnectError("connection refused")

    @property
    def custo_estimado_usd(self) -> float:
        return 0.0


class _FakeGeminiOk(TextLLMClient):
    def __init__(self):
        self.chamado = False

    def generate_json(self, prompt: str) -> dict:
        self.chamado = True
        return {"itens": [], "custo_total": 999, "from": "gemini"}

    @property
    def custo_estimado_usd(self) -> float:
        return 0.0


class TestFallbackOllamaToGemini:
    def test_ollama_offline_cai_pro_gemini(self):
        gemini = _FakeGeminiOk()
        fb = FallbackTextLLMClient([_FakeFailingOllama(), gemini])

        result = fb.generate_json("x")

        assert result["from"] == "gemini"
        assert gemini.chamado is True
        assert fb.ultimo_usado == "_FakeGeminiOk"


class TestFactoryNovoProvider:
    def test_provider_ollama_retorna_ollama_client(self, monkeypatch):
        monkeypatch.setenv("TEXT_LLM_PROVIDER", "ollama")
        client = build_default_text_client()
        assert isinstance(client, OllamaTextClient)

    def test_provider_ollama_mais_gemini_monta_fallback(self, monkeypatch):
        monkeypatch.setenv("TEXT_LLM_PROVIDER", "ollama+gemini")
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")

        # GeminiTextClient.__init__ importa google.genai — mockar pra não exigir
        # a dep em ambientes de CI minimal.
        with patch(
            "carros_sa.agents.text_llm_clients.GeminiTextClient.__init__",
            return_value=None,
        ):
            client = build_default_text_client()

        assert isinstance(client, FallbackTextLLMClient)
        assert isinstance(client._clients[0], OllamaTextClient)
        from carros_sa.agents.text_llm_clients import GeminiTextClient
        assert isinstance(client._clients[1], GeminiTextClient)

    def test_provider_desconhecido_levanta(self, monkeypatch):
        monkeypatch.setenv("TEXT_LLM_PROVIDER", "llama-caseiro")
        with pytest.raises(ValueError, match="desconhecido"):
            build_default_text_client()
