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
    // 1. Links diretos <a href> — padrão mais comum
    const links = Array.from(document.querySelectorAll('a[href]'));
    for (const a of links) {
        const h = a.href || '';
        if (h.includes('.pdf')
            || h.includes('doc-b2b')
            || h.includes('storage.googleapis')
            || (h.includes('laudo') && !h.includes('/assets/'))) {
            return h;
        }
    }
    // 2. Atributos data-* ou href em <button> / <div> (Auto Avaliar às vezes usa onclick+data)
    const candidatos = Array.from(document.querySelectorAll('[data-url], [data-href], [data-pdf]'));
    for (const el of candidatos) {
        const h = el.dataset.url || el.dataset.href || el.dataset.pdf || '';
        if (h.includes('.pdf') || h.includes('doc-b2b') || h.includes('storage.googleapis')) {
            return h;
        }
    }
    // 3. iframes (modal do laudo pode renderizar dentro de um)
    const iframes = Array.from(document.querySelectorAll('iframe[src]'));
    for (const f of iframes) {
        const s = f.src || '';
        if (s.includes('.pdf') || s.includes('doc-b2b')) {
            return s;
        }
    }
    // 4. Fallback: regex no HTML inteiro (pega URLs assinadas mesmo em JS inline)
    const html = document.documentElement.outerHTML;
    const m = html.match(/https?:\\/\\/(?:storage\\.googleapis\\.com\\/doc-b2b|cdn-aav\\.autoavaliar\\.com\\.br)\\/[^\\s"'<>]+\\.pdf[^\\s"'<>]*/);
    return m ? m[0] : null;
}
"""

# Abre o modal do laudo cautelar se existir — Auto Avaliar renderiza o PDF
# via lazy load ao clicar em botão/link com texto "laudo".
_ABRIR_MODAL_LAUDO_JS = """
() => {
    const alvos = Array.from(document.querySelectorAll('a, button, [role="button"], div[class*="laudo"], span[class*="laudo"]'));
    for (const el of alvos) {
        const txt = (el.textContent || '').toLowerCase();
        if (txt.includes('laudo') && (txt.includes('completo') || txt.includes('cautelar') || txt.includes('ver'))) {
            try { el.click(); return true; } catch (e) {}
        }
    }
    return false;
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

_MAX_PAGINAS = 20  # teto defensivo pro range de `?p=N`


_CONTA_PAGINAS_JS = """
() => {
    const nums = Array.from(document.querySelectorAll('a.button[data-page]'))
        .map(a => parseInt(a.getAttribute('data-page'), 10))
        .filter(n => !Number.isNaN(n));
    return nums.length ? Math.max(...nums) : 1;
}
"""


async def _coletar_listagem_cidade(
    page,
    cidade: str,
    uf: str,
    horizonte_dias: int,
) -> list:
    """Coleta cards de UMA cidade, iterando TODAS as páginas via `?p=N`.

    Auto Avaliar pagina com param `p` (não scroll infinito) — o DOM mostra os
    links da paginação como `<a class="button" data-page="N">`. A gente lê
    `max(data-page)` na primeira página e itera até lá, limitando a
    `_MAX_PAGINAS` pra não rodar loop runaway se o DOM mudar.
    """
    base_url = (
        f"{LISTAGEM_URL}?location={uf.lower()}&cities={quote(cidade.lower())}"
        f"&report=yes&order=recforyou"
    )

    # Página 1: descobre total de páginas
    await page.goto(base_url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    total_paginas = await page.evaluate(_CONTA_PAGINAS_JS)
    try:
        total_paginas = int(total_paginas)
    except (TypeError, ValueError):
        total_paginas = 1
    total_paginas = max(1, min(total_paginas, _MAX_PAGINAS))

    vistos: set = set()
    agregado: list = []

    async def _coleta_pagina_atual() -> None:
        raw_cards = await page.evaluate(_EXTRACT_CARDS_JS)
        for card in raw_cards or []:
            lote_id = card.get("loteId")
            if not lote_id or lote_id in vistos:
                continue
            vistos.add(lote_id)
            agregado.append(card)

    await _coleta_pagina_atual()

    for p in range(2, total_paginas + 1):
        await page.goto(f"{base_url}&p={p}", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)
        await _coleta_pagina_atual()

    # Filtra por horizonte DEPOIS de agregar todas as páginas
    agora = datetime.now()
    limite = agora + timedelta(days=horizonte_dias)
    resultado = []
    for card in agregado:
        from carros_sa.scraping.parsers import _timer_para_fim_em, _TIMER_RE
        timer_linha = next((l for l in card["lines"] if _TIMER_RE.match(l)), None)
        if timer_linha:
            fim_em = _timer_para_fim_em(agora, timer_linha)
            if fim_em and fim_em > limite:
                continue
        resultado.append(card)

    return resultado


async def coletar_listagem(
    page,
    empresa: EmpresaConfig,
    horizonte_dias: int = 7,
) -> list[dict]:
    """
    Coleta cards do Auto Avaliar iterando pelas cidades do raio operacional da empresa.

    Itera `empresa.cidades_de_busca()` (haversine a partir do pátio, ordenado
    por distância) e combina resultados deduplicando por lote_id — mesmo lote
    aparecendo em buscas de 2 cidades diferentes é contado só 1 vez.

    Retorna lista de {loteId, href, lines} — mesmo shape antigo.
    """
    try:
        municipios = empresa.cidades_de_busca()
    except Exception:
        # Fallback: só o pátio (comportamento legado) se o dataset não estiver disponível
        municipios = []

    # Se o dataset geo não retornou nada, caímos pra busca só da cidade do pátio
    if not municipios:
        return await _coletar_listagem_cidade(
            page, empresa.patio.cidade, empresa.patio.uf, horizonte_dias,
        )

    vistos: set = set()
    agregado: list[dict] = []
    for m in municipios:
        try:
            cards = await _coletar_listagem_cidade(page, m.nome, m.uf, horizonte_dias)
        except Exception:
            continue   # 1 cidade falhou → segue pras outras
        for card in cards:
            lote_id = card.get("loteId")
            if lote_id and lote_id not in vistos:
                vistos.add(lote_id)
                agregado.append(card)
        # Sleep leve entre cidades pra reduzir risco de rate-limit
        await page.wait_for_timeout(random.randint(800, 1500))

    return agregado


# ---------------------------------------------------------------------------
# Coleta de detalhe
# ---------------------------------------------------------------------------

async def coletar_detalhe(page, url: str) -> tuple[str, Optional[str]]:
    """
    Abre página de detalhe de um lote.
    Retorna (body_text, laudo_pdf_url).

    Tenta extrair `laudo_pdf_url` em duas passadas: primeiro no DOM inicial, e
    se falhar, clica em botão/link "Ver Laudo" pra renderizar o modal lazy e
    tenta de novo. Sem ambiguidade — a 2ª passada é best-effort, se não achar
    ainda retorna None (pipeline cai em `_laudo_sem_pdf` que agora aproveita
    `flags.reprovado_estrutural` quando aplicável).
    """
    await page.goto(url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(1500)

    body_text: str = await page.evaluate("() => document.body.innerText")
    laudo_pdf_url: Optional[str] = await page.evaluate(_EXTRACT_PDF_URL_JS)

    # 2ª passada: se PDF não foi achado no DOM inicial, tenta revelar o modal
    # do laudo (Auto Avaliar às vezes lazy-loada). Só abre o modal se valer a pena.
    if not laudo_pdf_url:
        try:
            clicou = await page.evaluate(_ABRIR_MODAL_LAUDO_JS)
            if clicou:
                await page.wait_for_timeout(1500)
                laudo_pdf_url = await page.evaluate(_EXTRACT_PDF_URL_JS)
        except Exception:
            pass

    return body_text, laudo_pdf_url


# ---------------------------------------------------------------------------
# Download de PDF
# ---------------------------------------------------------------------------

async def baixar_pdf(
    url: str,
    dest: Path,
    cookies: Optional[list[dict]] = None,
    max_retries: int = 3,
) -> Path:
    """Baixa PDF do laudo via httpx com retry-após-429.

    Auto Avaliar rate-limita agressivamente o download do laudo
    (triagem 2026-04-16: 49/55 pdfs deram 429). Backoff exponencial:
    15s, 30s, 60s entre tentativas. Após max_retries, propaga a exceção.
    """
    import asyncio as _aio

    cookie_header = ""
    if cookies:
        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    headers = {"Cookie": cookie_header} if cookie_header else {}
    dest.parent.mkdir(parents=True, exist_ok=True)

    delays = [15, 30, 60]  # segundos entre retries 1→2→3
    ultima_exc: Optional[Exception] = None

    for tentativa in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
                r = await client.get(url, headers=headers)
                if r.status_code == 429 and tentativa < max_retries:
                    await _aio.sleep(delays[tentativa])
                    continue
                r.raise_for_status()
                dest.write_bytes(r.content)
                return dest
        except httpx.HTTPStatusError as exc:
            ultima_exc = exc
            if exc.response.status_code == 429 and tentativa < max_retries:
                await _aio.sleep(delays[tentativa])
                continue
            raise

    # Se chegou aqui, esgotou retries (só cai aqui em 429 cronicamente)
    if ultima_exc:
        raise ultima_exc
    raise RuntimeError(f"baixar_pdf falhou após {max_retries+1} tentativas: {url}")


def _sleep_aleatorio(min_s: float = 2.0, max_s: float = 4.0) -> None:
    """Sleep síncrono entre requests para reduzir risco de bloqueio."""
    import time
    time.sleep(random.uniform(min_s, max_s))
