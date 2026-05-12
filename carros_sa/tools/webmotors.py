"""Cliente Webmotors — parser de resultados + função `estatisticas()`.

Estratégia de coleta:
  * As classes CSS do Webmotors são CSS-Modules com hash volátil (`_Card_18bss_1`)
    — **não ancorar por class**. A âncora confiável é a URL
    `/comprar/{marca}/{modelo}/{versao}/{portas}/{ano1-ano2}/{id}` e o
    `innerText` estruturado de cada card.
  * Cada card tem linhas na ordem:
        [opc. "N/M" carousel] / [opc. "OFERTA DESTAQUE"] / [opc. "ABAIXO DA FIPE"]
        "FORD FIESTA"                    ← MARCA MODELO em maiúsculas
        "1.6 Se Hatch 16v Flex 4p Manual"← versão
        "2013/2014"                      ← ano_fab/ano_mod
        "148.741 Km"                     ← km
        "Cuiabá (MT)"                    ← cidade (UF)
        "R$ 31.936"                      ← preço
        "Ver parcelas"

Esse parser roda offline sobre esse innerText. O `_fetch_playwright()` é o
esqueleto pra coleta ao vivo, **desligado por padrão** — workstream G liga.

Red flag (ver CLAUDE.md): NÃO rodar scraping agressivo sem discutir ritmo e
fingerprint. Webmotors tem Cloudflare; use Playwright stealth, rate limit
>= 10s/request, cache 24h.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median
from typing import Callable, List, Optional, Tuple

# =============================================================================
# Modelo parseado (não é SQLModel — conversão pra AnuncioWebmotors é opcional)
# =============================================================================

@dataclass
class AnuncioWM:
    id: str
    marca: str
    modelo: str
    versao: str
    ano_fab: int
    ano_mod: int
    km: int
    cidade: str
    uf: str
    preco: int
    abaixo_da_fipe: bool = False
    oferta_destaque: bool = False


@dataclass
class EstatisticasWM:
    p25: int
    mediana: int
    n_anuncios: int
    # km mediana dos anúncios do (marca, modelo, ano). 0 quando não há amostra.
    # Usado pelo precificador pra ajustar a âncora de venda quando a km do lote
    # destoa da km típica do mercado (ver carros_sa/ajuste_km.py).
    km_mediana: int = 0


# =============================================================================
# Parser (funções puras)
# =============================================================================

_ANO_RE = re.compile(r"^(\d{4})/(\d{4})$")
_KM_RE = re.compile(r"^([\d\.]+)\s*Km$", re.IGNORECASE)
_CIDADE_RE = re.compile(r"^(.+?)\s*\(([A-Z]{2})\)$")
_PRECO_RE = re.compile(r"^R\$\s*([\d\.]+)")
_BADGES = {"OFERTA DESTAQUE", "ABAIXO DA FIPE"}
_CAROUSEL_RE = re.compile(r"^\d+/\d+$")


def _parse_br_int(s: str) -> int:
    return int(s.replace(".", "").split(",")[0])


def parse_card(texto: List[str], anuncio_id: str) -> Optional[AnuncioWM]:
    """Converte lista de linhas `innerText` de um card em AnuncioWM.

    Retorna None se falta âncora essencial (preço, ano, ou modelo). Resiliente
    a variações de ordem dos badges iniciais.
    """
    linhas = [l.strip() for l in texto if l.strip()]
    if not linhas:
        return None

    oferta_destaque = "OFERTA DESTAQUE" in linhas
    abaixo_da_fipe = "ABAIXO DA FIPE" in linhas

    ano_idx = None
    ano_fab = ano_mod = 0
    km = 0
    cidade = uf = ""
    preco = 0
    for i, l in enumerate(linhas):
        m = _ANO_RE.match(l)
        if m and ano_idx is None:
            ano_fab, ano_mod = int(m.group(1)), int(m.group(2))
            ano_idx = i
            continue
        m = _KM_RE.match(l)
        if m and km == 0:
            km = _parse_br_int(m.group(1))
            continue
        m = _CIDADE_RE.match(l)
        if m and not cidade:
            cidade, uf = m.group(1).strip(), m.group(2)
            continue
        m = _PRECO_RE.match(l)
        if m and preco == 0:
            preco = _parse_br_int(m.group(1))

    if preco == 0 or ano_idx is None:
        return None

    # Cabeça: linhas antes do ano, excluindo badges e carousel count.
    cabeca = [
        l for l in linhas[:ano_idx]
        if l.upper() not in _BADGES and not _CAROUSEL_RE.match(l)
    ]
    if len(cabeca) < 2:
        return None
    marca_modelo = cabeca[0]  # "FORD FIESTA"
    versao = cabeca[1] if len(cabeca) >= 2 else ""
    partes = marca_modelo.split(None, 1)
    marca = partes[0].title() if partes else ""
    modelo = partes[1].title() if len(partes) > 1 else ""

    return AnuncioWM(
        id=anuncio_id,
        marca=marca,
        modelo=modelo,
        versao=versao,
        ano_fab=ano_fab,
        ano_mod=ano_mod,
        km=km,
        cidade=cidade,
        uf=uf,
        preco=preco,
        abaixo_da_fipe=abaixo_da_fipe,
        oferta_destaque=oferta_destaque,
    )


def parse_resultados(cards_json: List[dict]) -> List[AnuncioWM]:
    """Recebe a lista do JSON coletado (`{id, anos, texto}`) → AnuncioWM[]."""
    anuncios = []
    for c in cards_json:
        a = parse_card(c.get("texto", []), c.get("id", ""))
        if a is not None:
            anuncios.append(a)
    return anuncios


# =============================================================================
# API pública: estatisticas()
# =============================================================================

def _percentil(sorted_vals: List[int], p: float) -> int:
    n = len(sorted_vals)
    if n == 0:
        return 0
    if n == 1:
        return sorted_vals[0]
    k = (p / 100.0) * (n - 1)
    f = int(k)
    c = min(f + 1, n - 1)
    return int(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


FetchFn = Callable[[str, str], List[AnuncioWM]]


def estatisticas(
    marca: str,
    modelo: str,
    ano: int,
    *,
    anuncios: Optional[List[AnuncioWM]] = None,
    fetch: Optional[FetchFn] = None,
) -> EstatisticasWM:
    """Estatísticas de preço p/ (marca, modelo, ano) no Webmotors.

    `ano` casa com ano_fab OU ano_mod (Webmotors lista anúncios em faixas).
    `anuncios` tem precedência sobre `fetch` — use `anuncios=[...]` em testes.
    Se nenhum for passado, chama `_fetch_playwright` (bloqueado por padrão).
    """
    if anuncios is None:
        if fetch is not None:
            anuncios = fetch(marca, modelo)
        else:
            anuncios = _fetch_playwright(marca, modelo)

    mm = marca.lower().strip()
    mod = modelo.lower().strip()
    relevantes = [
        a for a in anuncios
        if a.marca.lower() == mm and a.modelo.lower() == mod
        and ano in (a.ano_fab, a.ano_mod)
    ]
    precos = sorted(a.preco for a in relevantes)
    if not precos:
        return EstatisticasWM(p25=0, mediana=0, n_anuncios=0, km_mediana=0)
    kms = [a.km for a in relevantes if a.km > 0]
    km_med = int(median(kms)) if kms else 0
    return EstatisticasWM(
        p25=_percentil(precos, 25),
        mediana=int(median(precos)),
        n_anuncios=len(precos),
        km_mediana=km_med,
    )


# =============================================================================
# Fetch ao vivo — esqueleto, NÃO ligar sem discussão (workstream G)
# =============================================================================

def _fetch_playwright(marca: str, modelo: str) -> List[AnuncioWM]:
    """Bloqueado por design — `estatisticas()` sem `anuncios`/`fetch` injetado.

    Workstream G (2026-05-12) habilitou coleta ao vivo, MAS o entry point
    público é o CLI `carros-sa webmotors-coletar` que aplica rate-limit ≥60s
    e cache 24h em DB. Chamar `estatisticas()` síncrono sem passar
    `anuncios=` é caminho que tenta fazer 1 fetch isolado e pode queimar IP.

    Pra usar dado live no avaliador: chame `webmotors_cache.obter_anuncios_cacheados(...)`
    e passe em `anuncios=`. O cron noturno popula o cache.
    """
    raise NotImplementedError(
        "Coleta ao vivo do Webmotors só via CLI `carros-sa webmotors-coletar` "
        "(rate-limit + cache). Pra usar dado live, leia do cache via "
        "`webmotors_cache.obter_anuncios_cacheados()` e passe em `anuncios=`. "
        "Ver workstream G no ROADMAP."
    )
