"""Scraper Playwright para b2b.autoavaliar.com.br.

Responsabilidades:
- Login com usuário/senha (persiste sessão em cookies JSON)
- Coleta de listagem com scroll infinito
- Coleta de detalhe (body_text + laudo_pdf_url)
- Download de PDF do laudo

Estratégia anti-bot:
- Browser real (Chromium headless) via Playwright
- Cookies persistidos → login só roda quando sessão expirar
- Sleep aleatório 2-4s entre GETs de detalhe
- Coleta sequencial (sem paralelismo no scraper)
"""

from __future__ import annotations

import asyncio
import json
import random
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from carros_sa.tenancy import EmpresaConfig

BASE_URL = "https://b2b.autoavaliar.com.br"
LOGIN_URL = f"{BASE_URL}/login"
LISTAGEM_URL = f"{BASE_URL}/avaliacoes"

# Seletor JS que extrai os cards da listagem (mesmo shape do JSON manual)
_EXTRACT_CARDS_JS = """
() => {
    const results = [];
    // Tenta seletores comuns usados no Auto Avaliar
    const selectors = [
        'a[href*="/avaliacoes/"]',
        '.vehicle-card a',
        '[data-lote-id]',
        'article a[href*="/avaliacoes/"]',
    ];
    let cards = [];
    for (const sel of selectors) {
        cards = Array.from(document.querySelectorAll(sel));
        if (cards.length > 0) break;
    }
    // Deduplica por href
    const seen = new Set();
    for (const el of cards) {
        const href = el.href || el.getAttribute('href') || '';
        if (!href.includes('/avaliacoes/') || seen.has(href)) continue;
        seen.add(href);
        // loteId é o segmento numérico da URL: /avaliacoes/{group}/{loteId}/{slug}
        const m = href.match(/\\/avaliacoes\\/[^/]+\\/(\\d+)\\//);
        if (!m) continue;
        const loteId = m[1];
        // Busca o container ancestral para pegar innerText completo do card
        let container = el;
        for (let i = 0; i < 6; i++) {
            if (!container.parentElement) break;
            container = container.parentElement;
            const t = container.innerText || '';
            // Para quando encontrar container com info suficiente (preço, cidade)
            if (t.match(/\\d{1,3}\\.\\d{3},\\d{2}/) && t.match(/\\/[A-Z]{2}/)) break;
        }
        const lines = (container.innerText || '').split('\\n').map(l => l.trim()).filter(Boolean);
        results.push({ loteId, href, lines });
    }
    return results;
}
"""

_EXTRACT_PDF_URL_JS = """
() => {
    // Procura link de PDF do laudo na página de detalhe
    const links = Array.from(document.querySelectorAll('a[href]'));
    for (const a of links) {
        const h = a.href || '';
        if (h.includes('.pdf') || h.includes('laudo') || h.includes('storage.googleapis')) {
            return h;
        }
    }
    // Fallback: procura no texto da página
    const m = document.body.innerText.match(/https?:\\/\\/[^\\s"']+\\.pdf/);
    return m ? m[0] : null;
}
"""


# ---------------------------------------------------------------------------
# Gerenciamento de cookies
# ---------------------------------------------------------------------------

def _cookies_path() -> Path:
    import os
    default = Path.home() / ".secrets" / "autoavaliar_cookies.json"
    return Path(os.environ.get("AUTOAVALIAR_COOKIES_PATH", str(default)))


def _salvar_cookies(cookies: list[dict], path: Optional[Path] = None) -> None:
    p = path or _cookies_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))


def _carregar_cookies(path: Optional[Path] = None) -> Optional[list[dict]]:
    p = path or _cookies_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def login(page, email: str, password: str) -> None:
    """Faz login no Auto Avaliar. Salva cookies. Levanta RuntimeError se falhar."""
    await page.goto(LOGIN_URL, wait_until="networkidle")
    await page.wait_for_timeout(1000)

    # Tenta selectors — Auto Avaliar usa name/id como hash, então priorizamos placeholder
    for email_sel in [
        "input[placeholder*='E-mail' i]",
        "input[placeholder*='email' i]",
        "input[type='email']",
        "input[name='email']",
        "#email",
    ]:
        try:
            await page.fill(email_sel, email, timeout=3000)
            break
        except Exception:
            continue

    for pwd_sel in [
        "input[placeholder*='Senha' i]",
        "input[placeholder*='senha' i]",
        "input[type='password']",
        "input[name='password']",
        "#password",
    ]:
        try:
            await page.fill(pwd_sel, password, timeout=3000)
            break
        except Exception:
            continue

    # Submit
    for submit_sel in ["button[type='submit']", "input[type='submit']", "button:has-text('Entrar')", "button:has-text('Login')"]:
        try:
            await page.click(submit_sel, timeout=3000)
            break
        except Exception:
            continue

    # Aguarda navegação pós-login
    await page.wait_for_timeout(3000)

    # Verifica se saiu da página de login
    if "/login" in page.url or "/entrar" in page.url or "dados-invalidos" in page.url:
        raise RuntimeError(
            f"Login falhou — URL pós-submit: {page.url}. "
            "Verifique AUTOAVALIAR_EMAIL e AUTOAVALIAR_PASSWORD no .env"
        )

    # Salva cookies
    cookies = await page.context.cookies()
    _salvar_cookies(cookies)


async def sessao_valida(page) -> bool:
    """Verifica se a sessão atual é válida (sem redirect para login ou logout)."""
    try:
        await page.goto(LISTAGEM_URL, wait_until="domcontentloaded", timeout=10000)
        await page.wait_for_timeout(1000)
        url = page.url
        return (
            "/login" not in url
            and "/entrar" not in url
            and "logout" not in url
            and "dados-invalidos" not in url
        )
    except Exception:
        return False


async def garantir_autenticado(page, email: str, password: str) -> None:
    """Restaura cookies salvos ou faz login se necessário."""
    cookies = _carregar_cookies()
    if cookies:
        await page.context.add_cookies(cookies)
        if await sessao_valida(page):
            return  # sessão restaurada com sucesso

    # Cookies expirados ou inexistentes — faz login de novo
    await login(page, email, password)


# ---------------------------------------------------------------------------
# Coleta de listagem
# ---------------------------------------------------------------------------

async def coletar_listagem(
    page,
    empresa: EmpresaConfig,
    horizonte_dias: int = 7,
) -> list[dict]:
    """
    Coleta cards da listagem do Auto Avaliar para a cidade/UF da empresa.
    Rola a página para carregar todos os cards.
    Filtra: só lotes com fim_em dentro do horizonte (próximos N dias).
    Retorna lista de {loteId, href, lines} — mesmo shape do JSON manual.
    """
    cidade = quote(empresa.patio.cidade.lower())
    uf = empresa.patio.uf.lower()
    url = f"{LISTAGEM_URL}?location={uf}&cities={cidade}&report=yes&order=recforyou"

    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    # Scroll infinito — rola até parar de aparecer novos cards
    cards_anterior = 0
    for _ in range(30):  # máximo 30 scrolls
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)
        cards_agora = await page.evaluate("document.querySelectorAll('a[href*=\"/avaliacoes/\"]').length")
        if cards_agora == cards_anterior:
            break
        cards_anterior = cards_agora

    # Extrai cards
    raw_cards: list[dict] = await page.evaluate(_EXTRACT_CARDS_JS)

    # Filtra por horizonte
    agora = datetime.now()
    limite = agora + timedelta(days=horizonte_dias)
    resultado = []
    for card in raw_cards:
        from carros_sa.scraping.parsers import parse_card_lines, _timer_para_fim_em, _TIMER_RE
        # Detecta timer nas linhas para filtrar por horizonte
        timer_linha = next((l for l in card["lines"] if _TIMER_RE.match(l)), None)
        if timer_linha:
            fim_em = _timer_para_fim_em(agora, timer_linha)
            if fim_em and fim_em > limite:
                continue  # fora do horizonte
        resultado.append(card)

    return resultado


# ---------------------------------------------------------------------------
# Coleta de detalhe
# ---------------------------------------------------------------------------

async def coletar_detalhe(page, url: str) -> tuple[str, Optional[str]]:
    """
    Abre página de detalhe de um lote.
    Retorna (body_text, laudo_pdf_url).
    """
    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(1500)

    body_text: str = await page.evaluate("() => document.body.innerText")
    laudo_pdf_url: Optional[str] = await page.evaluate(_EXTRACT_PDF_URL_JS)

    return body_text, laudo_pdf_url


# ---------------------------------------------------------------------------
# Download de PDF
# ---------------------------------------------------------------------------

async def baixar_pdf(url: str, dest: Path, cookies: Optional[list[dict]] = None) -> Path:
    """Baixa PDF do laudo via httpx. Usa cookies da sessão se disponíveis."""
    cookie_header = ""
    if cookies:
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    headers = {"Cookie": cookie_header} if cookie_header else {}
    dest.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        dest.write_bytes(r.content)

    return dest


def _sleep_aleatorio(min_s: float = 2.0, max_s: float = 4.0) -> None:
    """Sleep síncrono entre requests para reduzir risco de bloqueio."""
    import time
    time.sleep(random.uniform(min_s, max_s))
