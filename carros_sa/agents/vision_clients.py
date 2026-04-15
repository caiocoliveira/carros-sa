"""Abstração de client de visão + implementações.

`VisionClient.classify(image_png_bytes, prompt) -> dict` — contrato único.
Cada backend (Anthropic, Gemini, Ollama) implementa do seu jeito, mas retorna o mesmo shape de JSON.

Isso deixa o `ExtratorLaudo` agnóstico e permite A/B entre providers sem tocar no pipeline.
"""

from __future__ import annotations

import base64
import json
import os
import re
from abc import ABC, abstractmethod
from typing import Optional


class VisionClient(ABC):
    """Interface mínima para extração de JSON de uma imagem via LLM multimodal."""

    @abstractmethod
    def classify(self, image_png_bytes: bytes, prompt: str) -> dict:
        ...

    @property
    @abstractmethod
    def custo_estimado_usd(self) -> float:
        """Custo aproximado por chamada (pra logar em laudo.custo_usd)."""


# =============================================================================
# Gemini (free tier) — google-genai
# =============================================================================

class GeminiVisionClient(VisionClient):
    """Gemini Flash 2.0 via SDK oficial `google-genai`.

    Lê GEMINI_API_KEY do ambiente. Modelo default: gemini-2.0-flash (rápido + grátis no free tier).
    """

    def __init__(self, model: str = "gemini-2.5-flash", api_key: Optional[str] = None):
        from google import genai  # import preguiçoso

        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY não configurada (verifique .env ou export)")
        self._client = genai.Client(api_key=key)
        self._model = model

    def classify(self, image_png_bytes: bytes, prompt: str) -> dict:
        from google.genai import types

        response = self._client.models.generate_content(
            model=self._model,
            contents=[
                types.Part.from_bytes(data=image_png_bytes, mime_type="image/png"),
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        raw = (response.text or "").strip()
        # Algumas variantes incluem fences — remover
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)

    @property
    def custo_estimado_usd(self) -> float:
        return 0.0  # free tier


# =============================================================================
# Anthropic (Haiku 4.5) — anthropic SDK
# =============================================================================

class AnthropicVisionClient(VisionClient):
    """Haiku 4.5 via `anthropic` SDK. Lê ANTHROPIC_API_KEY do ambiente."""

    def __init__(self, model: str = "claude-haiku-4-5", api_key: Optional[str] = None):
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key) if api_key else Anthropic()
        self._model = model

    def classify(self, image_png_bytes: bytes, prompt: str) -> dict:
        img_b64 = base64.standard_b64encode(image_png_bytes).decode()
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)

    @property
    def custo_estimado_usd(self) -> float:
        return 0.005  # ordem de grandeza pra 1 imagem + ~500 tokens de resposta


# =============================================================================
# Ollama local (Gemma 3, Qwen 2.5 VL, etc.)
# =============================================================================

class OllamaVisionClient(VisionClient):
    """Ollama local. Default gemma3:4b. Requer `ollama serve` rodando (default port 11434)."""

    def __init__(self, model: str = "gemma3:4b", host: str = "http://localhost:11434"):
        import httpx  # import preguiçoso
        self._http = httpx.Client(base_url=host, timeout=120)
        self._model = model

    def classify(self, image_png_bytes: bytes, prompt: str) -> dict:
        img_b64 = base64.standard_b64encode(image_png_bytes).decode()
        response = self._http.post(
            "/api/generate",
            json={
                "model": self._model,
                "prompt": prompt,
                "images": [img_b64],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.0},
            },
        )
        response.raise_for_status()
        raw = response.json()["response"].strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)

    @property
    def custo_estimado_usd(self) -> float:
        return 0.0


# =============================================================================
# Factory — escolhe client por env var
# =============================================================================

def build_default_client() -> VisionClient:
    """VISION_PROVIDER={gemini|anthropic|ollama} — default: gemini (free)."""
    provider = os.environ.get("VISION_PROVIDER", "gemini").lower()
    if provider == "gemini":
        return GeminiVisionClient()
    if provider == "anthropic":
        return AnthropicVisionClient()
    if provider == "ollama":
        return OllamaVisionClient()
    raise ValueError(f"VISION_PROVIDER desconhecido: {provider}")
