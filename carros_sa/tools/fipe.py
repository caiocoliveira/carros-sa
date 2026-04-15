"""Cliente para a API pública FIPE da Parallelum.

Endpoint base: https://parallelum.com.br/fipe/api/v1

Fluxo (4 chamadas pra obter 1 valor):
  1. GET /carros/marcas                                              → lista marcas
  2. GET /carros/marcas/{codMarca}/modelos                           → lista modelos
  3. GET /carros/marcas/{codMarca}/modelos/{codModelo}/anos          → lista anos
  4. GET /carros/marcas/{codMarca}/modelos/{codModelo}/anos/{codAno} → valor

Respostas são cacheadas (in-memory por instância). Persistência em
`modelo_fipe_cache` é responsabilidade do AvaliadorMercado.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import httpx

FIPE_BASE_URL = "https://parallelum.com.br/fipe/api/v1"

_PRECO_RE = re.compile(r"R\$\s*([\d\.]+),(\d{2})")


def _parse_valor(valor_str: str) -> int:
    """'R$ 30.123,45' -> 30123 (em reais inteiros, descartando centavos)."""
    m = _PRECO_RE.search(valor_str)
    if not m:
        raise ValueError(f"valor FIPE não reconhecido: {valor_str!r}")
    return int(m.group(1).replace(".", ""))


def _normalizar(s: str) -> str:
    """Lowercase + remove acentos comuns + colapsa espaços/pontuação."""
    s = s.lower().strip()
    repl = str.maketrans("áàâãéêíóôõúüç", "aaaaeeiooouuc")
    s = s.translate(repl)
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _score_modelo(query: str, candidato: str) -> int:
    """Pontua quão bem `candidato` (nome do modelo na FIPE) bate com `query`.

    Retorna nº de tokens da query presentes no candidato (ordem ignorada).
    Empate é resolvido por menor diff de tamanho — mas isso é responsabilidade
    do chamador (sort key).
    """
    q_tokens = set(_normalizar(query).split())
    c_tokens = set(_normalizar(candidato).split())
    return len(q_tokens & c_tokens)


class FipeClient:
    """Cliente FIPE com cache in-memory por instância.

    Para testes, injetar `http_get` substituindo as chamadas de rede.
    """

    def __init__(
        self,
        base_url: str = FIPE_BASE_URL,
        timeout: float = 10.0,
        http_client: Optional[httpx.Client] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = http_client or httpx.Client(timeout=timeout)
        self._cache: Dict[str, object] = {}

    def _get(self, path: str) -> object:
        if path in self._cache:
            return self._cache[path]
        url = f"{self._base_url}{path}"
        resp = self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        self._cache[path] = data
        return data

    # -- Lookups internos --------------------------------------------------

    def _resolve_marca(self, marca_query: str) -> Tuple[str, str]:
        marcas: List[dict] = self._get("/carros/marcas")  # type: ignore[assignment]
        alvo = _normalizar(marca_query)
        for m in marcas:
            if _normalizar(m["nome"]) == alvo:
                return m["codigo"], m["nome"]
        # fallback: prefixo
        for m in marcas:
            if _normalizar(m["nome"]).startswith(alvo) or alvo.startswith(_normalizar(m["nome"])):
                return m["codigo"], m["nome"]
        raise LookupError(f"marca não encontrada na FIPE: {marca_query!r}")

    def _resolve_modelo(self, cod_marca: str, modelo_query: str) -> Tuple[str, str]:
        payload = self._get(f"/carros/marcas/{cod_marca}/modelos")
        modelos: List[dict] = payload["modelos"] if isinstance(payload, dict) else payload  # type: ignore[index]
        scored = [
            (_score_modelo(modelo_query, m["nome"]), -len(m["nome"]), m["codigo"], m["nome"])
            for m in modelos
        ]
        scored.sort(reverse=True)
        if not scored or scored[0][0] == 0:
            raise LookupError(f"modelo não encontrado na FIPE: {modelo_query!r}")
        _, _, cod, nome = scored[0]
        return str(cod), nome

    def _resolve_ano(self, cod_marca: str, cod_modelo: str, ano: int) -> str:
        anos: List[dict] = self._get(  # type: ignore[assignment]
            f"/carros/marcas/{cod_marca}/modelos/{cod_modelo}/anos"
        )
        # ano vem como "2013-1" (combustível 1=gasolina, 2=alcool, 3=diesel, 4=flex...)
        prefix = f"{ano}-"
        for a in anos:
            if str(a["codigo"]).startswith(prefix):
                return str(a["codigo"])
        # fallback: ano mais próximo
        try:
            anos_int = sorted(
                {(int(str(a["codigo"]).split("-")[0]), str(a["codigo"])) for a in anos}
            )
        except ValueError:
            raise LookupError(f"anos FIPE com formato inesperado para modelo {cod_modelo}")
        if not anos_int:
            raise LookupError(f"sem anos FIPE pro modelo {cod_modelo}")
        proximo = min(anos_int, key=lambda t: abs(t[0] - ano))
        return proximo[1]

    # -- API pública -------------------------------------------------------

    def consultar(self, marca: str, modelo: str, ano: int) -> int:
        """Retorna o valor FIPE em reais (inteiro)."""
        cod_marca, _ = self._resolve_marca(marca)
        cod_modelo, _ = self._resolve_modelo(cod_marca, modelo)
        cod_ano = self._resolve_ano(cod_marca, cod_modelo, ano)
        valor_payload = self._get(
            f"/carros/marcas/{cod_marca}/modelos/{cod_modelo}/anos/{cod_ano}"
        )
        if not isinstance(valor_payload, dict) or "Valor" not in valor_payload:
            raise LookupError(f"resposta FIPE sem campo Valor: {valor_payload!r}")
        return _parse_valor(valor_payload["Valor"])

    def close(self) -> None:
        self._client.close()
