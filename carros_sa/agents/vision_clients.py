"""Abstração de client de visão + implementações.

`VisionClient.classify(image_png_bytes, prompt) -> dict` — contrato único.
Cada backend (Anthropic, Gemini, Ollama) implementa do seu jeito, mas retorna
o mesmo shape de JSON.

Inclui `FallbackVisionClient` que encadeia múltiplos clients — útil pra lidar
com picos de overload do Gemini (`503 UNAVAILABLE`) caindo automaticamente
pro Anthropic Haiku (~$0.005/chamada).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from typing import List, Optional

logger = logging.getLogger(__name__)


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
        from google.genai.errors import ServerError

        # Retry manual com backoff agressivo pra absorver 503 UNAVAILABLE
        # (picos de demanda no Gemini Flash). 3 tentativas: 0s, 15s, 45s.
        ultimo_erro: Optional[Exception] = None
        for tentativa, espera in enumerate([0, 15, 45]):
            if espera:
                logger.warning(
                    "GeminiVisionClient: retry em %ds após %s",
                    espera, type(ultimo_erro).__name__,
                )
                time.sleep(espera)
            try:
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
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
                return json.loads(raw)
            except ServerError as e:
                ultimo_erro = e
                # 503 / 429 / 500 — vale a pena tentar de novo
                if getattr(e, "code", None) in (429, 500, 502, 503, 504) and tentativa < 2:
                    continue
                raise
        assert ultimo_erro is not None
        raise ultimo_erro

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
# Fallback em cascata — tenta clients em ordem até um sucesso
# =============================================================================

class FallbackVisionClient(VisionClient):
    """Tenta cada client em ordem; aceita o primeiro que não levantar exceção.

    Casos típicos:
    - Gemini (grátis) → Haiku (~$0.005) — cobre overload 503 sem ficar sem análise
    - Gemini → Ollama local — em ambiente offline/air-gapped

    Logs mostram qual client conseguiu, pra acompanhar custo real em produção.
    """

    def __init__(self, clients: List[VisionClient]):
        if not clients:
            raise ValueError("FallbackVisionClient precisa de pelo menos 1 client")
        self._clients = clients
        # Custo estimado = max entre os possíveis (pior caso — todos caíram menos o último)
        self._custo = max(c.custo_estimado_usd for c in clients)
        self._ultimo_usado: Optional[str] = None

    def classify(self, image_png_bytes: bytes, prompt: str) -> dict:
        ultimo_erro: Optional[Exception] = None
        for client in self._clients:
            nome = type(client).__name__
            try:
                resultado = client.classify(image_png_bytes, prompt)
                if ultimo_erro is not None:
                    logger.warning(
                        "FallbackVisionClient: sucesso em %s após falha do primário (%s)",
                        nome, type(ultimo_erro).__name__,
                    )
                self._ultimo_usado = nome
                return resultado
            except Exception as e:
                logger.warning("FallbackVisionClient: %s falhou (%s), tentando próximo…", nome, e)
                ultimo_erro = e
        # Todos falharam
        assert ultimo_erro is not None
        raise ultimo_erro

    @property
    def custo_estimado_usd(self) -> float:
        return self._custo

    @property
    def ultimo_usado(self) -> Optional[str]:
        """Nome da última implementação que respondeu com sucesso."""
        return self._ultimo_usado


# =============================================================================
# Factory — escolhe client por env var
# =============================================================================

def build_default_client() -> VisionClient:
    """Monta o VisionClient a ser usado pelo pipeline.

    Política por default (`VISION_PROVIDER` ausente ou "auto"):
      - Sempre inclui Gemini (grátis via free tier, requer GEMINI_API_KEY)
      - Se `ANTHROPIC_API_KEY` estiver setada, adiciona Haiku como FALLBACK
        (cai só se Gemini levantar exceção — tipicamente 503 UNAVAILABLE)
      - Se só um estiver configurado, retorna esse único cliente.

    Overrides explícitos via `VISION_PROVIDER`:
      - "gemini"    → só Gemini
      - "anthropic" → só Anthropic Haiku
      - "ollama"    → só Ollama local
      - "auto"      → cascata (default)
    """
    provider = os.environ.get("VISION_PROVIDER", "auto").lower()

    if provider == "gemini":
        return GeminiVisionClient()
    if provider == "anthropic":
        return AnthropicVisionClient()
    if provider == "ollama":
        return OllamaVisionClient()

    if provider in ("", "auto"):
        clients: List[VisionClient] = []
        if os.environ.get("GEMINI_API_KEY"):
            clients.append(GeminiVisionClient())
        if os.environ.get("ANTHROPIC_API_KEY"):
            clients.append(AnthropicVisionClient())
        if not clients:
            raise RuntimeError(
                "Nenhum provider de visão configurado. Defina GEMINI_API_KEY e/ou "
                "ANTHROPIC_API_KEY no .env, ou VISION_PROVIDER=ollama."
            )
        if len(clients) == 1:
            return clients[0]
        return FallbackVisionClient(clients)

    raise ValueError(f"VISION_PROVIDER desconhecido: {provider}")
