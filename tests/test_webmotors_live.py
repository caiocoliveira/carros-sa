"""Tests do fetch ao vivo Webmotors (workstream G — 2026-05-12).

NÃO bate na rede. Mocka `page` do Playwright pra exercitar:
  - `_build_search_url` com slug correto
  - `fetch_anuncios` parsea innerText e devolve AnuncioWM[]
  - WebmotorsLiveError em Cloudflare challenge / zero cards
  - `fetch_com_retry` backoff e re-raise após exaurir tentativas
"""

from __future__ import annotations

from typing import Any, List

import pytest

from carros_sa.tools.webmotors_live import (
    WebmotorsLiveError,
    _build_search_url,
    fetch_anuncios,
    fetch_com_retry,
)


class FakePage:
    """Mock minimalista de `playwright.async_api.Page`. Cada `evaluate(js)`
    devolve do mapa `responses` na ordem em que as chaves foram inseridas
    (suporta múltiplas chamadas com mesmo JS template via lista)."""

    def __init__(self, *, cards: List[dict], is_cloudflare: bool = False,
                 raise_on_goto: bool = False):
        self._cards = cards
        self._is_cloudflare = is_cloudflare
        self._raise_on_goto = raise_on_goto
        self.goto_urls: list = []

    async def goto(self, url, **_kwargs):
        self.goto_urls.append(url)
        if self._raise_on_goto:
            raise RuntimeError("network error")

    async def wait_for_timeout(self, _ms):
        pass

    async def evaluate(self, js, *_args):
        if "cf-browser-verification" in js or "Just a moment" in js:
            return self._is_cloudflare
        if "scrollTo" in js:
            return None
        # Default: assume extract_cards JS
        return self._cards


def test_build_search_url_com_slug_simples():
    url = _build_search_url("Ford", "Fiesta", 2013)
    assert "ford" in url.lower()
    assert "fiesta" in url.lower()
    assert "2013" in url


def test_build_search_url_com_espacos_e_acentos():
    """Slug deve lowercase + hifenizar espaços."""
    url = _build_search_url("Volkswagen", "Polo Track", 2024)
    assert "volkswagen" in url
    assert "polo-track" in url


@pytest.mark.asyncio
async def test_fetch_anuncios_parsea_cards():
    cards = [{
        "id": "12345678",
        "href": "/comprar/ford/fiesta/1.6-se/4/2013-2014/12345678",
        "anos": "2013-2014",
        "texto": [
            "FORD FIESTA", "1.6 SE Hatch 16v Flex 4p Manual",
            "2013/2014", "120.000 Km", "São Paulo (SP)", "R$ 35.000",
            "Ver parcelas",
        ],
    }]
    page = FakePage(cards=cards)
    anuncios = await fetch_anuncios(page, "Ford", "Fiesta", 2013)
    assert len(anuncios) == 1
    assert anuncios[0].preco == 35000
    assert anuncios[0].km == 120000
    assert anuncios[0].marca == "Ford"


@pytest.mark.asyncio
async def test_fetch_anuncios_cloudflare_dispara_erro():
    page = FakePage(cards=[], is_cloudflare=True)
    with pytest.raises(WebmotorsLiveError, match="cloudflare"):
        await fetch_anuncios(page, "Ford", "Fiesta", 2013)


@pytest.mark.asyncio
async def test_fetch_anuncios_zero_cards_dispara_erro():
    page = FakePage(cards=[])
    with pytest.raises(WebmotorsLiveError, match="zero cards"):
        await fetch_anuncios(page, "Ford", "Fiesta", 2013)


@pytest.mark.asyncio
async def test_fetch_anuncios_falha_de_navegacao():
    page = FakePage(cards=[], raise_on_goto=True)
    with pytest.raises(WebmotorsLiveError, match="navegação falhou"):
        await fetch_anuncios(page, "Ford", "Fiesta", 2013)


@pytest.mark.asyncio
async def test_fetch_com_retry_reraises_apos_tentativas(monkeypatch):
    """Retry exausta após N tentativas — propaga último erro."""
    page = FakePage(cards=[], is_cloudflare=True)

    # Patch sleep pra não esperar de verdade
    async def fast_sleep(_s):
        pass
    monkeypatch.setattr("carros_sa.tools.webmotors_live.asyncio.sleep", fast_sleep)

    with pytest.raises(WebmotorsLiveError, match="cloudflare"):
        await fetch_com_retry(page, "Ford", "Fiesta", 2013, tentativas=2,
                              backoff_inicial_s=1)


@pytest.mark.asyncio
async def test_fetch_com_retry_sucesso_apos_falha(monkeypatch):
    """Primeira tentativa falha, segunda sucesso — retorna anúncios."""
    call_count = {"n": 0}

    class FlakyPage(FakePage):
        async def evaluate(self, js, *args):
            if "cf-browser-verification" in js:
                # Primeira chamada: cloudflare; segunda: limpa
                call_count["n"] += 1
                return call_count["n"] == 1
            if "scrollTo" in js:
                return None
            return [{
                "id": "1", "href": "/comprar/x/y/1", "anos": "2013-2014",
                "texto": ["FORD FIESTA", "1.6 SE", "2013/2014",
                          "100.000 Km", "São Paulo (SP)", "R$ 30.000"],
            }]

    page = FlakyPage(cards=[])
    async def fast_sleep(_s):
        pass
    monkeypatch.setattr("carros_sa.tools.webmotors_live.asyncio.sleep", fast_sleep)

    anuncios = await fetch_com_retry(page, "Ford", "Fiesta", 2013,
                                     tentativas=2, backoff_inicial_s=1)
    assert len(anuncios) == 1
    assert anuncios[0].preco == 30000
