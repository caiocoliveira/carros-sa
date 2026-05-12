"""Coleta ao vivo do Webmotors — Playwright + stealth.

Workstream G (Fase 1). Estratégia anti-bot acordada:
  - Browser real (Chromium headless) com `playwright-stealth`
  - Caller responsável por **rate-limit ≥ 60s entre chamadas** (cron noturno)
  - Retry com backoff em 403 / Cloudflare challenge / página vazia
  - Sem paralelismo (1 contexto sequencial)
  - Cache 24h no DB (`anuncio_webmotors` via `webmotors_cache.py`) pra evitar
    re-buscar o mesmo (marca, modelo, ano) na mesma noite

Red flag CLAUDE.md: NÃO chamar em loop apertado. Em produção, usar SEMPRE o
CLI `carros-sa webmotors-coletar` que faz o rate-limit e checa cache antes.

Esse módulo é DELIBERADAMENTE thin: navegação + extração de innerText. O parse
é feito em `webmotors.parse_resultados` (puro, testado offline).

Primeira validação manual antes de plugar no cron:
  carros-sa webmotors-coletar --marca Ford --modelo Fiesta --ano 2013 --debug
A URL/seletor podem precisar de ajuste fino — Webmotors usa CSS-Modules com
hash volátil; ancorar por innerText do card é a única estratégia resiliente.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote

from carros_sa.tools.webmotors import AnuncioWM, parse_resultados

logger = logging.getLogger(__name__)

# URL canônica de listagem do Webmotors. Operador pode override via env
# WEBMOTORS_SEARCH_URL_TEMPLATE se o site mudar o padrão de rota.
_DEFAULT_SEARCH_URL = (
    "https://www.webmotors.com.br/carros/estoque?"
    "marca1={marca}&modelo1={modelo}&anode={ano}&anoate={ano}"
)

# Timeout por navegação (Cloudflare challenge pode levar 5-10s).
_NAV_TIMEOUT_MS = 30_000
# Scroll up to 3 vezes pra forçar lazy-load (Webmotors carrega ~24 cards por
# scroll). 3 × 24 = 72 anúncios — mais que suficiente pra mediana confiável
# por (marca, modelo, ano).
_MAX_SCROLLS = 3
# Sleep entre scrolls pra rede acompanhar.
_SCROLL_WAIT_MS = 2_000

# JS que extrai cards do Webmotors. Estratégia idêntica à da fixture coletada
# via Chrome MCP: encontra anchors pra páginas de detalhe e sobe ancestrais até
# pegar o container do card (que contém ano, km, cidade, preço no innerText).
_EXTRACT_CARDS_JS = """
() => {
    const results = [];
    // URL canônica de detalhe: /comprar/{marca}/{modelo}/{versao}/{portas}/{ano1-ano2}/{id}
    const links = Array.from(document.querySelectorAll('a[href*="/comprar/"]'));
    const seen = new Set();
    for (const a of links) {
        const href = a.getAttribute('href') || '';
        // Captura ID do anúncio (último segmento numérico antes do query string)
        const m = href.match(/\\/(\\d{6,})(?:\\?|$|\\/)/);
        if (!m) continue;
        const id = m[1];
        if (seen.has(id)) continue;
        seen.add(id);
        // Sobe ancestrais até pegar container com info do card
        let container = a;
        for (let i = 0; i < 8; i++) {
            if (!container.parentElement) break;
            container = container.parentElement;
            const t = container.innerText || '';
            // Container do card tem preço (R$ X.XXX) E ano (YYYY/YYYY) no innerText
            if (t.match(/R\\$\\s*\\d/) && t.match(/\\d{4}\\/\\d{4}/)) break;
        }
        const lines = (container.innerText || '')
            .split('\\n').map(l => l.trim()).filter(Boolean);
        // Extrai anos do href pra metadata
        const yrm = href.match(/\\/(\\d{4})-(\\d{4})\\//);
        const anos = yrm ? `${yrm[1]}-${yrm[2]}` : '';
        results.push({ id, href, anos, texto: lines });
    }
    return results;
}
"""

_JS_DETECT_CLOUDFLARE = """
() => {
    const txt = (document.body && document.body.innerText) || '';
    return (
        txt.includes('Verifique se você é humano') ||
        txt.includes('Just a moment') ||
        txt.includes('Checking your browser') ||
        txt.includes('cf-browser-verification') ||
        document.title.toLowerCase().includes('just a moment')
    );
}
"""


class WebmotorsLiveError(Exception):
    """Falha na coleta ao vivo (Cloudflare, 403, página vazia, timeout)."""


def _slug(s: str) -> str:
    """Slug minúsculo URL-safe."""
    s = s.strip().lower()
    s = re.sub(r"\s+", "-", s)
    return quote(s, safe="-")


def _build_search_url(marca: str, modelo: str, ano: int) -> str:
    """URL de listagem por (marca, modelo, ano)."""
    import os
    template = os.environ.get("WEBMOTORS_SEARCH_URL_TEMPLATE", _DEFAULT_SEARCH_URL)
    return template.format(marca=_slug(marca), modelo=_slug(modelo), ano=ano)


async def fetch_anuncios(
    page,
    marca: str,
    modelo: str,
    ano: int,
    *,
    timeout_ms: int = _NAV_TIMEOUT_MS,
    max_scrolls: int = _MAX_SCROLLS,
    scroll_wait_ms: int = _SCROLL_WAIT_MS,
) -> List[AnuncioWM]:
    """Navega, extrai e parseia anúncios do Webmotors pra (marca, modelo, ano).

    `page` é um `playwright.async_api.Page` com Stealth aplicado no contexto.
    Caller (CLI) é responsável pelo rate-limit (≥60s entre chamadas) e por
    montar o contexto stealth — esse módulo só faz a navegação+extração.

    Raises `WebmotorsLiveError` em Cloudflare challenge, página vazia (zero
    cards) ou timeout. Caller decide retry / pular.
    """
    url = _build_search_url(marca, modelo, ano)
    logger.info("webmotors_live fetch url=%s", url)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        raise WebmotorsLiveError(f"navegação falhou: {exc}") from exc

    await page.wait_for_timeout(2_000)

    # Cloudflare challenge: aborta antes de parsear lixo
    is_cf = await page.evaluate(_JS_DETECT_CLOUDFLARE)
    if is_cf:
        raise WebmotorsLiveError(
            f"cloudflare_challenge marca={marca} modelo={modelo} ano={ano} — "
            "stealth não passou. Aumentar sleep, trocar IP ou usar proxy."
        )

    # Scroll progressivo pra forçar lazy-load (Webmotors carrega ~24/scroll)
    for _ in range(max_scrolls):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(scroll_wait_ms)

    raw_cards = await page.evaluate(_EXTRACT_CARDS_JS)
    if not raw_cards:
        raise WebmotorsLiveError(
            f"zero cards extraídos marca={marca} modelo={modelo} ano={ano} — "
            "URL pode estar errada ou seletor JS desatualizado"
        )

    anuncios = parse_resultados(raw_cards)
    logger.info(
        "webmotors_live parsed raw=%s parseados=%s marca=%s modelo=%s ano=%s",
        len(raw_cards), len(anuncios), marca, modelo, ano,
    )
    return anuncios


async def fetch_com_retry(
    page,
    marca: str,
    modelo: str,
    ano: int,
    *,
    tentativas: int = 3,
    backoff_inicial_s: int = 30,
) -> List[AnuncioWM]:
    """`fetch_anuncios` com retry exponencial pra erros transientes.

    Cloudflare/timeout/zero cards são todos tratados como retry-able até
    `tentativas`. Backoff dobra a cada falha (30s → 60s → 120s) — caller já
    está num ritmo de 60s/req então retry interno só estende isso.
    """
    last_exc: Optional[Exception] = None
    sleep_s = backoff_inicial_s
    for i in range(tentativas):
        try:
            return await fetch_anuncios(page, marca, modelo, ano)
        except WebmotorsLiveError as exc:
            last_exc = exc
            logger.warning(
                "webmotors_live tentativa=%s/%s falhou: %s — backoff %ss",
                i + 1, tentativas, exc, sleep_s,
            )
            if i + 1 < tentativas:
                await asyncio.sleep(sleep_s)
                sleep_s *= 2
    assert last_exc is not None
    raise last_exc


@dataclass
class StealthBrowser:
    """Wrapper async context manager pra Chromium + Stealth.

    Uso:
        async with StealthBrowser() as page:
            anuncios = await fetch_com_retry(page, "Ford", "Fiesta", 2013)

    Browser/context são criados/destruídos por bloco — o CLI loopa
    chamadas usando o MESMO `page` (uma sessão pro batch inteiro).
    """
    headless: bool = True
    _pw: Optional[object] = None
    _browser: Optional[object] = None
    _context: Optional[object] = None
    _page: Optional[object] = None

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth

        self._pw = await async_playwright().start()
        # Locale pt-BR e timezone São Paulo: emulação geográfica reduz a
        # superfície de fingerprint anti-bot. User-agent comum (Chrome 124).
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        self._context = await self._browser.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        await Stealth().apply_stealth_async(self._context)
        self._page = await self._context.new_page()
        return self._page

    async def __aexit__(self, *_exc):
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
