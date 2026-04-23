"""Tests pra retry/backoff/throttle do FipeClient contra rate limit 429.

Motivação: em 2026-04-16 a triagem produziu 21 erros por HTTP 429 da Parallelum
após ~50 lotes. O pipeline bate ~4 requests por lote em `/cars/brands`,
`/models`, `/years`, valor. Sem retry+throttle+cache persistente da lista
de marcas, o serviço bloqueia em questão de segundos.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List

import httpx
import pytest

from carros_sa.tools.fipe import FipeClient


class _FakeResp:
    """httpx.Response-like pra mockar respostas sem rede."""

    def __init__(self, status_code: int, json_data=None, retry_after=None):
        self.status_code = status_code
        self._json = json_data or []
        self.headers = {}
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=self  # type: ignore[arg-type]
            )


class _FakeHttpx:
    """Cliente httpx fake — escrevemos a sequência de respostas na ordem desejada."""

    def __init__(self, respostas: List[_FakeResp]):
        self._queue = list(respostas)
        self.calls: List[str] = []

    def get(self, url: str):
        self.calls.append(url)
        if not self._queue:
            raise AssertionError(f"sem resposta mockada pra {url}")
        return self._queue.pop(0)

    def close(self):
        pass


def test_fipe_429_é_retentado_com_backoff(monkeypatch):
    """Primeiro 429 → retry bem-sucedido na 2ª tentativa."""
    sleeps: List[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    fake = _FakeHttpx([
        _FakeResp(429),
        _FakeResp(200, json_data=[{"code": "21", "name": "Fiat"}]),
    ])
    c = FipeClient(
        http_client=fake,  # type: ignore[arg-type]
        sleep_between_requests=0,
        marcas_disk_cache_path=None,
    )

    data = c._get("/cars/brands")

    assert data == [{"code": "21", "name": "Fiat"}]
    assert len(fake.calls) == 2          # 1 falha + 1 sucesso
    assert any(s >= 1.0 for s in sleeps)  # teve pelo menos 1 backoff


def test_fipe_429_respeita_retry_after(monkeypatch):
    """Quando servidor manda Retry-After: 5, o cliente espera exatamente 5s."""
    sleeps: List[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    fake = _FakeHttpx([
        _FakeResp(429, retry_after=5),
        _FakeResp(200, json_data=[]),
    ])
    c = FipeClient(
        http_client=fake,  # type: ignore[arg-type]
        sleep_between_requests=0,
        marcas_disk_cache_path=None,
    )
    c._get("/cars/brands")
    assert 5.0 in sleeps


def test_fipe_429_desistir_apos_max_retries(monkeypatch):
    """3 tentativas esgotadas → levanta HTTPStatusError."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake = _FakeHttpx([_FakeResp(429), _FakeResp(429), _FakeResp(429)])
    c = FipeClient(
        http_client=fake,  # type: ignore[arg-type]
        sleep_between_requests=0,
        max_retries=3,
        marcas_disk_cache_path=None,
    )
    with pytest.raises(httpx.HTTPStatusError):
        c._get("/cars/brands")
    assert len(fake.calls) == 3


def test_fipe_cache_disco_evita_refetch_de_marcas(tmp_path, monkeypatch):
    """/carros/marcas é cacheado em disco → 2º FipeClient não bate na rede."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    cache_file = tmp_path / "marcas.json"
    payload = [{"code": "21", "name": "Fiat"}, {"code": "59", "name": "Ford"}]

    fake1 = _FakeHttpx([_FakeResp(200, json_data=payload)])
    c1 = FipeClient(
        http_client=fake1,  # type: ignore[arg-type]
        sleep_between_requests=0,
        marcas_disk_cache_path=cache_file,
    )
    c1._get("/cars/brands")
    assert cache_file.exists()
    with cache_file.open() as f:
        assert json.load(f) == payload

    # Novo cliente, zero respostas mockadas — se bater na rede, explode.
    fake2 = _FakeHttpx([])
    c2 = FipeClient(
        http_client=fake2,  # type: ignore[arg-type]
        sleep_between_requests=0,
        marcas_disk_cache_path=cache_file,
    )
    data = c2._get("/cars/brands")
    assert data == payload
    assert fake2.calls == []   # nenhuma chamada real de rede


def test_fipe_cache_disco_expirado_refaz_fetch(tmp_path, monkeypatch):
    """Cache mais velho que TTL → re-fetch."""
    import os
    monkeypatch.setattr(time, "sleep", lambda s: None)
    cache_file = tmp_path / "marcas.json"
    cache_file.write_text('[{"code":"1","name":"Old"}]')
    # Envelhece o arquivo pra ~60 dias atrás (TTL é 30 dias)
    old_ts = time.time() - 60 * 24 * 3600
    os.utime(cache_file, (old_ts, old_ts))

    novo_payload = [{"code": "99", "name": "Novo"}]
    fake = _FakeHttpx([_FakeResp(200, json_data=novo_payload)])
    c = FipeClient(
        http_client=fake,  # type: ignore[arg-type]
        sleep_between_requests=0,
        marcas_disk_cache_path=cache_file,
    )
    data = c._get("/cars/brands")
    assert data == novo_payload
    assert len(fake.calls) == 1


def test_fipe_normalizar_remove_trema_e_cedilha():
    """Citroën do FIPE tem que bater com 'Citroen' vindo do Auto Avaliar."""
    from carros_sa.tools.fipe import _normalizar
    assert _normalizar("Citroën") == _normalizar("Citroen") == "citroen"
    # Outros diacríticos comuns das marcas FIPE
    assert _normalizar("Mercedes-Benz") == "mercedes benz"
    assert _normalizar("Peugeot") == "peugeot"


def test_fipe_throttle_espaça_requests(monkeypatch):
    """Duas chamadas consecutivas devem esperar sleep_between_requests entre elas."""
    sleeps: List[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    # Força monotonic a avançar 0 entre chamadas → throttle sempre precisa esperar
    fake_clock = iter([0.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(fake_clock))

    fake = _FakeHttpx([
        _FakeResp(200, json_data=[{"code": "21", "name": "Fiat"}]),
        _FakeResp(200, json_data=[]),
    ])
    c = FipeClient(
        http_client=fake,  # type: ignore[arg-type]
        sleep_between_requests=0.5,
        marcas_disk_cache_path=None,
    )
    c._get("/cars/brands")
    c._get("/cars/brands/21/models")

    # Pelo menos um sleep de ~0.5s foi chamado (throttle entre os requests)
    assert any(abs(s - 0.5) < 0.01 for s in sleeps)
