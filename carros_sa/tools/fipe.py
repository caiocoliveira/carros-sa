"""Cliente para a API pública FIPE da Parallelum (v2).

Endpoint base: https://fipe.parallelum.com.br/api/v2

Fluxo (4 chamadas pra obter 1 valor):
  1. GET /cars/brands                                                → lista marcas
  2. GET /cars/brands/{brandId}/models                               → lista modelos
  3. GET /cars/brands/{brandId}/models/{modelId}/years               → lista anos
  4. GET /cars/brands/{brandId}/models/{modelId}/years/{yearId}      → valor

Respostas são cacheadas (in-memory por instância). Persistência em
`modelo_fipe_cache` é responsabilidade do AvaliadorMercado.

## Histórico
Migrado de v1 (`parallelum.com.br/fipe/api/v1`, campos `codigo`/`nome`/`Valor`,
endpoint `/carros/...` em português) para v2 em 2026-04-23 porque v1 começou
a retornar 503 intermitente E porque v2 corrige o split de marcas que gerava
bug de lookup em modelos legacy (ex: "Chery Tiggo 2.0 2015" matchava brand
"Caoa Chery" novo em vez de "Caoa Chery/Chery" legacy → valor ~2.7x errado).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import httpx

FIPE_BASE_URL = "https://fipe.parallelum.com.br/api/v2"

_PRECO_RE = re.compile(r"R\$\s*([\d\.]+),(\d{2})")

# Marcas fora do escopo da FIPE /cars/ — principalmente fabricantes exclusivos
# de motos. Sem FIPE não temos âncora de preço, e o pipeline não consegue
# precificar. Detecção antecipada economiza 1 chamada ao Parallelum + dá motivo
# de descarte claro ("moto") em vez do LookupError genérico do _resolve_marca.
# Honda/Yamaha/Suzuki/BMW são EXCLUÍDAS dessa lista porque também fazem carros
# — deixamos o LookupError normal tratar se vier um modelo de moto delas.
MARCAS_NON_FIPE = frozenset({
    "triumph", "harley-davidson", "harley davidson", "harley",
    "ducati", "kawasaki", "dafra", "kasinski", "royal enfield",
    "royal-enfield", "mv agusta", "mv-agusta", "ktm", "piaggio",
    "vespa",
})


def marca_fora_do_escopo_fipe(marca: str) -> bool:
    """True se a marca é fabricante exclusivo de moto (fora da FIPE carros)."""
    if not marca:
        return False
    return marca.strip().lower() in MARCAS_NON_FIPE

# Rate limit: Parallelum não documenta, mas experimentalmente ~1 req/s é seguro.
# Um pipeline de 50 lotes × 4 chamadas/consulta = 200 requests, o que sem throttle
# bate em 429. Com 0.3s entre chamadas + cache persistente da lista-mestre de
# marcas (1/4 das chamadas some), cai pra ~150 requests distribuídos em ~45s.
_DEFAULT_SLEEP_BETWEEN_REQUESTS = 0.3

# Cache em disco do endpoint /cars/brands — a lista é quase-estática (FIPE
# atualiza mensalmente, mas não adiciona marcas nova), reidratar do disco
# evita 1 chamada por CADA consulta FIPE do pipeline. Nome do arquivo sufixado
# com "_v2" porque o shape do payload mudou do v1 (codigo/nome → code/name) —
# caches antigos do v1 são ignorados automaticamente.
_MARCAS_CACHE_PATH = Path(
    os.environ.get("CARROS_SA_FIPE_MARCAS_CACHE", "data/cache/fipe_brands_v2.json")
)
_MARCAS_CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 dias


def _parse_valor(valor_str: str) -> int:
    """'R$ 30.123,45' -> 30123 (em reais inteiros, descartando centavos)."""
    m = _PRECO_RE.search(valor_str)
    if not m:
        raise ValueError(f"valor FIPE não reconhecido: {valor_str!r}")
    return int(m.group(1).replace(".", ""))


def _normalizar(s: str) -> str:
    """Lowercase + remove acentos/diacríticos + colapsa espaços/pontuação.

    Cobre todos os diacríticos vistos nas marcas FIPE reais — incluindo ë
    (Citroën, senão "Citroen" do Auto Avaliar não matcha) e ï (Daïhatsu/etc).
    """
    s = s.lower().strip()
    # Pares origem→destino alinhados posição a posição.
    # 25 caracteres de cada lado — maketrans exige tamanhos iguais.
    #  5 a's, 4 e's, 4 i's, 5 o's, 4 u's, 1 c, 1 n, 1 y = 25
    repl = str.maketrans(
        "áàâãäéèêëíìîïóòôõöúùûüçñý",
        "aaaaaeeeeiiiiooooouuuucny",
    )
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
        http_client: httpx.Client | None = None,
        sleep_between_requests: float = _DEFAULT_SLEEP_BETWEEN_REQUESTS,
        max_retries: int = 3,
        marcas_disk_cache_path: Path | None = _MARCAS_CACHE_PATH,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = http_client or httpx.Client(timeout=timeout)
        self._cache: dict[str, object] = {}
        self._sleep_between = sleep_between_requests
        self._max_retries = max_retries
        self._last_request_at: float = 0.0
        self._marcas_disk_cache = marcas_disk_cache_path

    def _throttle(self) -> None:
        """Espera o resto do intervalo entre requests — rate limit friendly."""
        if self._sleep_between <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        wait = self._sleep_between - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _get(self, path: str) -> object:
        # 1. Cache in-memory (hit mais quente)
        if path in self._cache:
            return self._cache[path]

        # 2. Cache em disco SÓ pra /cars/brands — lista quase-estática
        #    usada em TODA consulta; evita 1 request por lote do pipeline.
        if path == "/cars/brands":
            disk_hit = self._carregar_marcas_do_disco()
            if disk_hit is not None:
                self._cache[path] = disk_hit
                return disk_hit

        # 3. Request de rede com retry exponencial em 429/5xx
        url = f"{self._base_url}{path}"
        data = self._get_com_retry(url)
        self._cache[path] = data

        if path == "/cars/brands":
            self._persistir_marcas_no_disco(data)

        return data

    def _get_com_retry(self, url: str) -> object:
        """HTTP GET com backoff em 429/5xx. Respeita Retry-After quando presente."""
        backoffs = [1.0, 3.0, 8.0]   # cumulativo: até ~12s entre tentativas
        for tentativa in range(self._max_retries):
            self._throttle()
            resp = self._client.get(url)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                # Respeita Retry-After (segundos) se o servidor mandar
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = backoffs[min(tentativa, len(backoffs) - 1)]
                else:
                    wait = backoffs[min(tentativa, len(backoffs) - 1)]
                if tentativa < self._max_retries - 1:
                    time.sleep(wait)
                    continue
                # Última tentativa falhou — propaga HTTPStatusError
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.json()
        # Unreachable — raise_for_status já teria disparado
        raise RuntimeError("fipe: esgotou retries sem resposta")

    def _carregar_marcas_do_disco(self) -> list[dict] | None:
        """Tenta ler o cache de marcas do disco. None se não existe ou expirou."""
        if self._marcas_disk_cache is None or not self._marcas_disk_cache.exists():
            return None
        try:
            idade = time.time() - self._marcas_disk_cache.stat().st_mtime
            if idade > _MARCAS_CACHE_TTL_SECONDS:
                return None
            with self._marcas_disk_cache.open() as f:
                data = json.load(f)
            if not isinstance(data, list):
                return None
            # Sanity-check do shape v2 (code/name). Se alguém deixou cache v1
            # (codigo/nome) no mesmo path, ignora e força refetch.
            if data and not (isinstance(data[0], dict) and "code" in data[0]):
                return None
            return data
        except (OSError, json.JSONDecodeError):
            return None

    def _persistir_marcas_no_disco(self, data: object) -> None:
        """Salva a lista de marcas no disco pra próximas execuções."""
        if self._marcas_disk_cache is None or not isinstance(data, list):
            return
        try:
            self._marcas_disk_cache.parent.mkdir(parents=True, exist_ok=True)
            with self._marcas_disk_cache.open("w") as f:
                json.dump(data, f)
        except OSError:
            pass  # cache é best-effort, falha silenciosa é aceitável

    # -- Lookups internos --------------------------------------------------

    def _candidatas_marca(self, marca_query: str) -> list[tuple[str, str]]:
        """Retorna TODAS as marcas empatadas no maior score de matching.

        Empate real acontece em pelo menos um caso de produção:
          "Chery" → [(161, "Caoa Chery/Chery"), (245, "Caoa Chery")]
        Ambas empatam no token `chery`. O chamador (`consultar`) testa cada
        candidata e fica com a que deu melhor match de MODELO — assim o Tiggo
        2.0 2015 (que só existe na 161) não cai erradamente na 245.
        """
        marcas: list[dict] = self._get("/cars/brands")  # type: ignore[assignment]
        alvo = _normalizar(marca_query)
        alvo_tokens = set(alvo.split())

        # 1. Match exato — curto-circuita
        for m in marcas:
            if _normalizar(m["name"]) == alvo:
                return [(str(m["code"]), m["name"])]

        # 2. Scoring: token overlap + bônus de prefixo
        scored: list[tuple[int, str, str]] = []
        for m in marcas:
            nome_n = _normalizar(m["name"])
            nome_tokens = set(nome_n.split())
            score = len(alvo_tokens & nome_tokens)
            if nome_n.startswith(alvo) or alvo.startswith(nome_n):
                score += 1
            if score > 0:
                scored.append((score, str(m["code"]), m["name"]))

        if not scored:
            raise LookupError(f"marca não encontrada na FIPE: {marca_query!r}")

        max_score = max(s for s, _, _ in scored)
        empatadas = [(c, n) for s, c, n in scored if s == max_score]
        # Desempate determinístico: menor code numérico primeiro (na FIPE, codes
        # menores costumam ser marcas mais antigas/legacy, que têm o catálogo
        # completo de modelos históricos).
        empatadas.sort(key=lambda t: int(t[0]) if t[0].isdigit() else 0)
        return empatadas

    def _resolve_modelo_com_score(
        self, cod_marca: str, modelo_query: str
    ) -> tuple[str, str, int]:
        """Match do modelo + o score do melhor candidato (pra comparar entre marcas)."""
        modelos: list[dict] = self._get(  # type: ignore[assignment]
            f"/cars/brands/{cod_marca}/models"
        )
        scored = [
            (_score_modelo(modelo_query, m["name"]), -len(m["name"]), str(m["code"]), m["name"])
            for m in modelos
        ]
        scored.sort(reverse=True)
        if not scored or scored[0][0] == 0:
            raise LookupError(f"modelo não encontrado na FIPE: {modelo_query!r}")
        score, _, cod, nome = scored[0]
        return cod, nome, score

    def _resolve_ano(self, cod_marca: str, cod_modelo: str, ano: int) -> str:
        anos: list[dict] = self._get(  # type: ignore[assignment]
            f"/cars/brands/{cod_marca}/models/{cod_modelo}/years"
        )
        # ano vem como "2013-1" (combustível 1=gasolina, 2=alcool, 3=diesel, 4=flex...)
        prefix = f"{ano}-"
        for a in anos:
            if str(a["code"]).startswith(prefix):
                return str(a["code"])
        # fallback: ano mais próximo
        try:
            anos_int = sorted(
                {(int(str(a["code"]).split("-")[0]), str(a["code"])) for a in anos}
            )
        except ValueError:
            raise LookupError(f"anos FIPE com formato inesperado para modelo {cod_modelo}")
        if not anos_int:
            raise LookupError(f"sem anos FIPE pro modelo {cod_modelo}")
        proximo = min(anos_int, key=lambda t: abs(t[0] - ano))
        return proximo[1]

    # -- API pública -------------------------------------------------------

    def consultar(self, marca: str, modelo: str, ano: int) -> int:
        """Retorna o valor FIPE em reais (inteiro).

        Quando a marca tem mais de uma candidata empatada (caso Chery), tenta
        resolver o modelo em CADA uma e fica com o par (marca, modelo) cujo
        score de modelo foi mais alto. Isso evita que um match fraco numa marca
        errada (ex: Tiggo 7 novo em "Caoa Chery") sobreponha o match certo
        numa marca alternativa (Tiggo 2.0 legacy em "Caoa Chery/Chery").
        """
        candidatas = self._candidatas_marca(marca)

        melhor: tuple[int, str, str] | None = None  # (score, cod_marca, cod_modelo)
        for cod_marca, _nome_marca in candidatas:
            try:
                cod_modelo, _nome_modelo, score = self._resolve_modelo_com_score(
                    cod_marca, modelo
                )
            except LookupError:
                continue
            if melhor is None or score > melhor[0]:
                melhor = (score, cod_marca, cod_modelo)

        if melhor is None:
            raise LookupError(
                f"modelo não encontrado em nenhuma marca candidata: {marca!r} / {modelo!r}"
            )

        _, cod_marca, cod_modelo = melhor
        cod_ano = self._resolve_ano(cod_marca, cod_modelo, ano)
        valor_payload = self._get(
            f"/cars/brands/{cod_marca}/models/{cod_modelo}/years/{cod_ano}"
        )
        if not isinstance(valor_payload, dict) or "price" not in valor_payload:
            raise LookupError(f"resposta FIPE sem campo price: {valor_payload!r}")
        return _parse_valor(valor_payload["price"])

    def close(self) -> None:
        self._client.close()
