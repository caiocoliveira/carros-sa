"""Cliente FIPE via fipe.parallelum.com.br (API v2) com cache em SQLite.

Cache hit-first: consulta `modelo_fipe_cache` antes de bater na rede.
HTTP é injetável pra testes (qualquer objeto com .get(url) -> .json()).

Match de marca/modelo é fuzzy-soft: tenta exato (case-insensitive), depois
substring. PoC; se houver ambiguidade séria, volta como `None` e o agente
sobe pra fallback.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional

import httpx
from sqlmodel import Session, select

from carros_sa.db import get_engine
from carros_sa.models import ModeloFipeCache

BASE_URL = "https://fipe.parallelum.com.br/api/v2"


class FipeClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        http_client: Optional[httpx.Client] = None,
        session_factory: Optional[Callable[[], Session]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.http = http_client or httpx.Client(timeout=10.0)
        self._session_factory = session_factory

    # ---------- API pública ----------

    def consultar(self, marca: str, modelo: str, ano: int) -> Optional[int]:
        """Retorna valor FIPE em reais (int). None se não encontrou."""
        cached = self._buscar_cache(marca, modelo, ano)
        if cached is not None:
            return cached
        valor = self._buscar_api(marca, modelo, ano)
        if valor is not None:
            self._salvar_cache(marca, modelo, ano, valor)
        return valor

    # ---------- Cache ----------

    def _new_session(self) -> Session:
        if self._session_factory:
            return self._session_factory()
        return Session(get_engine())

    def _buscar_cache(self, marca: str, modelo: str, ano: int) -> Optional[int]:
        with self._new_session() as s:
            stmt = select(ModeloFipeCache).where(
                ModeloFipeCache.marca == marca.lower(),
                ModeloFipeCache.modelo == modelo.lower(),
                ModeloFipeCache.ano == ano,
            )
            row = s.exec(stmt).first()
            return row.valor if row else None

    def _salvar_cache(self, marca: str, modelo: str, ano: int, valor: int) -> None:
        with self._new_session() as s:
            s.add(
                ModeloFipeCache(
                    marca=marca.lower(),
                    modelo=modelo.lower(),
                    ano=ano,
                    valor=valor,
                )
            )
            s.commit()

    # ---------- Rede ----------

    def _buscar_api(self, marca: str, modelo: str, ano: int) -> Optional[int]:
        brands = self._get(f"{self.base_url}/cars/brands")
        brand = _match_nome(brands, marca)
        if not brand:
            return None
        models = self._get(f"{self.base_url}/cars/brands/{brand['code']}/models")
        if isinstance(models, dict) and "models" in models:
            models = models["models"]
        model = _match_nome(models, modelo)
        if not model:
            return None
        years = self._get(
            f"{self.base_url}/cars/brands/{brand['code']}/models/{model['code']}/years"
        )
        year = _match_ano(years, ano)
        if not year:
            return None
        price = self._get(
            f"{self.base_url}/cars/brands/{brand['code']}/models/{model['code']}/years/{year['code']}"
        )
        # v2 retorna {"price": "R$ 22.345,00", ...} ou lista com 1 item
        if isinstance(price, list) and price:
            price = price[0]
        return _parse_brl(price.get("price", ""))

    def _get(self, url: str):
        resp = self.http.get(url)
        resp.raise_for_status()
        return resp.json()


# =============================================================================
# Helpers de matching
# =============================================================================

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower().strip())


def _match_nome(items: List[dict], query: str) -> Optional[dict]:
    """Match por igualdade case-insensitive; fallback pra substring."""
    if not items:
        return None
    q = _norm(query)
    for it in items:
        if _norm(it.get("name", "")) == q:
            return it
    candidatos = [it for it in items if q in _norm(it.get("name", ""))]
    if len(candidatos) == 1:
        return candidatos[0]
    # Se múltiplos, prefere o mais curto (heurística: nome base vs versões)
    if candidatos:
        return min(candidatos, key=lambda it: len(it.get("name", "")))
    return None


def _match_ano(years: List[dict], ano: int) -> Optional[dict]:
    """Code da FIPE é tipo '2013-1' (gasolina), '2013-3' (flex). Pega o primeiro do ano."""
    alvo = str(ano)
    for y in years:
        code = str(y.get("code", ""))
        if code.startswith(f"{alvo}-") or code == alvo:
            return y
    return None


_BRL_RE = re.compile(r"R?\$?\s*([\d\.]+)(,\d{2})?")


def _parse_brl(s: str) -> Optional[int]:
    """'R$ 22.345,00' -> 22345."""
    if not s:
        return None
    m = _BRL_RE.search(s)
    if not m:
        return None
    try:
        return int(m.group(1).replace(".", ""))
    except ValueError:
        return None
