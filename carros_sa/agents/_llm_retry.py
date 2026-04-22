"""Helpers compartilhados entre `vision_clients` e `text_llm_clients`.

Antes: retry manual com backoff + cascata de fallback duplicados em 4 lugares
(GeminiVisionClient, GeminiTextClient, FallbackVisionClient, FallbackTextLLMClient).
Mudar política de retry significava tocar em 4 funções idênticas.

Agora: dois helpers. `call_with_server_retry` aplica o loop de backoff do config;
`try_clients_cascade` itera uma lista de clients retornando o primeiro sucesso.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Iterable, Sequence, TypeVar

from carros_sa.config import get_settings

T = TypeVar("T")
C = TypeVar("C")


def call_with_server_retry(
    call: Callable[[], T],
    *,
    client_name: str,
    logger: logging.Logger,
) -> T:
    """Executa `call()` com backoff do config pra `google.genai.errors.ServerError`.

    Delays vêm de `settings.llm_retry_delays_s` (default 0s/15s/45s). Só
    faz retry se `exc.code` cair em `settings.llm_retry_http_codes`
    (429/500/502/503/504). Exceções fora desse padrão (ValueError, JSONDecodeError,
    auth errors) propagam imediatamente — não queremos mascarar bug de código.
    """
    # Lazy import: só carrega o SDK do Gemini se alguém realmente chamar.
    from google.genai.errors import ServerError

    settings = get_settings()
    delays = settings.llm_retry_delays_s
    ultima_tentativa = len(delays) - 1
    ultimo_erro: Exception | None = None

    for tentativa, espera in enumerate(delays):
        if espera:
            logger.warning(
                "%s: retry em %ds após %s",
                client_name, espera, type(ultimo_erro).__name__,
            )
            time.sleep(espera)
        try:
            return call()
        except ServerError as exc:
            ultimo_erro = exc
            code = getattr(exc, "code", None)
            if code in settings.llm_retry_http_codes and tentativa < ultima_tentativa:
                continue
            raise

    assert ultimo_erro is not None
    raise ultimo_erro


def try_clients_cascade(
    clients: Sequence[C],
    call: Callable[[C], T],
    *,
    logger: logging.Logger,
    cascade_name: str,
) -> tuple[T, str]:
    """Tenta `call(client)` em cada client da sequência; retorna primeiro sucesso.

    Devolve `(resultado, nome_do_client_que_respondeu)`. Loga warning a cada
    falha antes de tentar o próximo. Se todos falharem, re-ergue o último erro.
    """
    if not clients:
        raise ValueError(f"{cascade_name} precisa de pelo menos 1 client")

    ultimo_erro: Exception | None = None
    for client in clients:
        nome = type(client).__name__
        try:
            resultado = call(client)
            if ultimo_erro is not None:
                logger.warning(
                    "%s: sucesso em %s após falha primária (%s)",
                    cascade_name, nome, type(ultimo_erro).__name__,
                )
            return resultado, nome
        except Exception as exc:
            logger.warning("%s: %s falhou (%s), tentando próximo…", cascade_name, nome, exc)
            ultimo_erro = exc

    assert ultimo_erro is not None
    raise ultimo_erro


__all__: Iterable[str] = ("call_with_server_retry", "try_clients_cascade")
