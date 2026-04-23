"""Testes dos helpers compartilhados de retry/fallback de LLM."""

from __future__ import annotations

import logging

import pytest

from carros_sa.agents._llm_retry import call_with_server_retry, try_clients_cascade


LOGGER = logging.getLogger("test_llm_retry")


# --------------------------------------------------------------------------- #
# try_clients_cascade                                                         #
# --------------------------------------------------------------------------- #

class _Client:
    def __init__(self, name: str, behavior):
        self.__class__ = type(name, (_Client,), {})
        self._behavior = behavior

    def work(self) -> str:
        return self._behavior()


def test_cascade_retorna_primeiro_sucesso():
    a = _Client("Primary", lambda: "ok-primary")
    b = _Client("Secondary", lambda: "ok-secondary")
    resultado, nome = try_clients_cascade(
        [a, b], lambda c: c.work(), logger=LOGGER, cascade_name="T",
    )
    assert resultado == "ok-primary"
    assert nome == "Primary"


def test_cascade_cai_pro_proximo_quando_primeiro_falha():
    def boom():
        raise RuntimeError("primary down")

    a = _Client("Primary", boom)
    b = _Client("Secondary", lambda: "ok-secondary")
    resultado, nome = try_clients_cascade(
        [a, b], lambda c: c.work(), logger=LOGGER, cascade_name="T",
    )
    assert resultado == "ok-secondary"
    assert nome == "Secondary"


def test_cascade_com_todos_falhando_reerga_ultimo_erro():
    def boom1():
        raise RuntimeError("primary down")

    def boom2():
        raise ValueError("secondary down")

    a = _Client("Primary", boom1)
    b = _Client("Secondary", boom2)
    with pytest.raises(ValueError, match="secondary down"):
        try_clients_cascade([a, b], lambda c: c.work(), logger=LOGGER, cascade_name="T")


def test_cascade_lista_vazia_levanta_valueerror():
    with pytest.raises(ValueError, match="pelo menos 1 client"):
        try_clients_cascade([], lambda c: c, logger=LOGGER, cascade_name="T")


# --------------------------------------------------------------------------- #
# call_with_server_retry                                                      #
# --------------------------------------------------------------------------- #

class _FakeServerError(Exception):
    """Stub que imita google.genai.errors.ServerError com atributo .code."""

    def __init__(self, code: int, msg: str = "server error"):
        super().__init__(msg)
        self.code = code


def test_retry_sucesso_na_primeira(monkeypatch: pytest.MonkeyPatch):
    """Caminho feliz — retorna direto sem sleep."""
    monkeypatch.setattr("google.genai.errors.ServerError", _FakeServerError, raising=False)

    chamadas = {"n": 0}

    def call():
        chamadas["n"] += 1
        return {"ok": True}

    resultado = call_with_server_retry(call, client_name="TestClient", logger=LOGGER)
    assert resultado == {"ok": True}
    assert chamadas["n"] == 1


def test_retry_repetindo_ate_sucesso(monkeypatch: pytest.MonkeyPatch):
    """Duas falhas 503 com sucesso na 3ª tentativa."""
    monkeypatch.setattr("google.genai.errors.ServerError", _FakeServerError, raising=False)
    monkeypatch.setattr("time.sleep", lambda *_: None)  # não dormir de verdade

    chamadas = {"n": 0}

    def call():
        chamadas["n"] += 1
        if chamadas["n"] < 3:
            raise _FakeServerError(code=503)
        return {"final": True}

    resultado = call_with_server_retry(call, client_name="TestClient", logger=LOGGER)
    assert resultado == {"final": True}
    assert chamadas["n"] == 3


def test_retry_codigo_nao_retryable_reerga_imediato(monkeypatch: pytest.MonkeyPatch):
    """Código não-retryable (fora do config.llm_retry_http_codes) propaga direto.

    HTTP 501 (Not Implemented) é ServerError mas NÃO está na lista default
    de retry (429, 500, 502, 503, 504). Modelo realista — 501 vem quando
    a API fica sem capacidade permanente, não faz sentido tentar de novo.
    """
    monkeypatch.setattr("google.genai.errors.ServerError", _FakeServerError, raising=False)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    chamadas = {"n": 0}

    def call():
        chamadas["n"] += 1
        raise _FakeServerError(code=501, msg="not implemented")

    with pytest.raises(_FakeServerError):
        call_with_server_retry(call, client_name="TestClient", logger=LOGGER)
    assert chamadas["n"] == 1
