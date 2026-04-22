"""Abstração de client de LLM text-only + implementações.

`TextLLMClient.generate_json(prompt) -> dict` — contrato único.
Espelha `vision_clients.py` mas sem imagem — pra agentes que raciocinam em
texto puro (estimador de reforma, sanity check de laudo, categoria inferrer).

Inclui `FallbackTextLLMClient` que encadeia múltiplos clients, com o mesmo
padrão Gemini (grátis) → Haiku (~$0.001-$0.005/chamada).
"""

from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod

from carros_sa.agents._llm_retry import call_with_server_retry, try_clients_cascade

logger = logging.getLogger(__name__)


class TextLLMClient(ABC):
    """Interface mínima para extração de JSON de um prompt textual."""

    @abstractmethod
    def generate_json(self, prompt: str) -> dict:
        ...

    @property
    @abstractmethod
    def custo_estimado_usd(self) -> float:
        """Custo aproximado por chamada."""


# =============================================================================
# Gemini (free tier) — google-genai
# =============================================================================

class GeminiTextClient(TextLLMClient):
    """Gemini Flash via SDK oficial `google-genai` — text-only.

    Lê GEMINI_API_KEY do ambiente. Retry manual 0s/15s/45s pra 503 UNAVAILABLE.
    """

    def __init__(self, model: str = "gemini-2.5-flash", api_key: str | None = None):
        from google import genai

        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY não configurada")
        self._client = genai.Client(api_key=key)
        self._model = model

    def generate_json(self, prompt: str) -> dict:
        from google.genai import types

        def _call() -> dict:
            response = self._client.models.generate_content(
                model=self._model,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.0,
                ),
            )
            raw = (response.text or "").strip()
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
            return json.loads(raw)

        return call_with_server_retry(
            _call, client_name="GeminiTextClient", logger=logger,
        )

    @property
    def custo_estimado_usd(self) -> float:
        return 0.0


# =============================================================================
# Anthropic Haiku — text-only
# =============================================================================

class AnthropicTextClient(TextLLMClient):
    """Haiku 4.5 via `anthropic` SDK — text-only."""

    def __init__(self, model: str = "claude-haiku-4-5", api_key: str | None = None):
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key) if api_key else Anthropic()
        self._model = model

    def generate_json(self, prompt: str) -> dict:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)

    @property
    def custo_estimado_usd(self) -> float:
        return 0.001  # text-only é mais barato que imagem


# =============================================================================
# Fallback em cascata
# =============================================================================

class FallbackTextLLMClient(TextLLMClient):
    """Tenta cada client em ordem; primeiro sucesso vence."""

    def __init__(self, clients: list[TextLLMClient]):
        if not clients:
            raise ValueError("FallbackTextLLMClient precisa de pelo menos 1 client")
        self._clients = clients
        self._custo = max(c.custo_estimado_usd for c in clients)
        self._ultimo_usado: str | None = None

    def generate_json(self, prompt: str) -> dict:
        resultado, nome = try_clients_cascade(
            self._clients,
            lambda c: c.generate_json(prompt),
            logger=logger,
            cascade_name="FallbackTextLLMClient",
        )
        self._ultimo_usado = nome
        return resultado

    @property
    def custo_estimado_usd(self) -> float:
        return self._custo

    @property
    def ultimo_usado(self) -> str | None:
        return self._ultimo_usado


# =============================================================================
# Factory
# =============================================================================

def build_default_text_client() -> TextLLMClient:
    """Cascata default: Gemini primário + Haiku fallback (se ANTHROPIC_API_KEY setada).

    Respeita `TEXT_LLM_PROVIDER` quando explícito: gemini | anthropic | auto.
    """
    provider = os.environ.get("TEXT_LLM_PROVIDER", "auto").lower()

    if provider == "gemini":
        return GeminiTextClient()
    if provider == "anthropic":
        return AnthropicTextClient()

    if provider in ("", "auto"):
        clients: list[TextLLMClient] = []
        if os.environ.get("GEMINI_API_KEY"):
            clients.append(GeminiTextClient())
        if os.environ.get("ANTHROPIC_API_KEY"):
            clients.append(AnthropicTextClient())
        if not clients:
            raise RuntimeError(
                "Nenhum provider de texto configurado. Defina GEMINI_API_KEY "
                "e/ou ANTHROPIC_API_KEY no .env."
            )
        if len(clients) == 1:
            return clients[0]
        return FallbackTextLLMClient(clients)

    raise ValueError(f"TEXT_LLM_PROVIDER desconhecido: {provider}")
