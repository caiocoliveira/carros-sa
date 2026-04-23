"""Módulo geográfico — busca por raio operacional a partir do pátio.

Fonte: dataset MIT de municípios brasileiros em `data/geo/municipios.csv`
(5570 linhas; colunas: codigo_ibge, nome, latitude, longitude, …). Lidos uma
vez por processo e mantidos em memória.

Usado pelo scraper pra iterar listagens de múltiplas cidades no raio e pelo
orquestrador pra calcular frete via distância real (haversine) ao invés da
heurística grosseira de UF.
"""

from __future__ import annotations

import csv
import math
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "geo" / "municipios.csv"

@dataclass
class Municipio:
    """Snapshot leve de uma linha do CSV de municípios."""

    codigo_ibge: int
    nome: str
    uf: str                                       # sigla (MG, GO, SP…)
    latitude: float
    longitude: float
    distancia_do_ponto_km: float = 0.0            # populado em cidades_no_raio

    @property
    def nome_normalizado(self) -> str:
        return _normaliza(self.nome)

# =============================================================================
# Normalização
# =============================================================================

def _normaliza(s: str) -> str:
    """Minúscula + remove acentos — usado pra busca case/accent-insensitive."""
    nfkd = unicodedata.normalize("NFKD", s)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return sem_acento.strip().lower()

# =============================================================================
# Carregamento
# =============================================================================

# Tabela codigo_uf (IBGE) → sigla, pra evitar ler estados.csv
_CODIGO_UF_PARA_SIGLA = {
    11: "RO", 12: "AC", 13: "AM", 14: "RR", 15: "PA", 16: "AP", 17: "TO",
    21: "MA", 22: "PI", 23: "CE", 24: "RN", 25: "PB", 26: "PE", 27: "AL", 28: "SE", 29: "BA",
    31: "MG", 32: "ES", 33: "RJ", 35: "SP",
    41: "PR", 42: "SC", 43: "RS",
    50: "MS", 51: "MT", 52: "GO", 53: "DF",
}

@lru_cache(maxsize=1)
def carregar_municipios() -> tuple[Municipio, ...]:
    """Lê o CSV uma vez e devolve tupla imutável (cacheada)."""
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"Dataset de municípios não encontrado em {DATA_PATH}. "
            "Esperado CSV com colunas codigo_ibge,nome,latitude,longitude,…,codigo_uf,…"
        )

    resultados: list[Municipio] = []
    with DATA_PATH.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                codigo_uf = int(row["codigo_uf"])
            except (KeyError, ValueError):
                continue
            uf = _CODIGO_UF_PARA_SIGLA.get(codigo_uf)
            if uf is None:
                continue
            resultados.append(Municipio(
                codigo_ibge=int(row["codigo_ibge"]),
                nome=row["nome"],
                uf=uf,
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
            ))
    return tuple(resultados)

# =============================================================================
# Distância haversine
# =============================================================================

_R_TERRA_KM = 6371.0

def distancia_haversine_km(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
) -> float:
    """Distância em linha reta (grande círculo) entre dois pontos, em km.

    Não é a distância rodoviária real — rodovia pode ser ~20% maior que linha
    reta no Brasil. Serve pra faixas de frete de atacado.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _R_TERRA_KM * c

# =============================================================================
# Buscas
# =============================================================================

def buscar_municipio(nome: str, uf: str) -> Municipio | None:
    """Procura por (nome, UF) case/accent-insensitive. None se não achar."""
    alvo_nome = _normaliza(nome)
    alvo_uf = uf.strip().upper()
    for m in carregar_municipios():
        if m.uf == alvo_uf and m.nome_normalizado == alvo_nome:
            return m
    return None

def cidades_no_raio(
    cidade_base: str,
    uf_base: str,
    raio_km: float,
) -> list[Municipio]:
    """Lista municípios dentro do raio (inclusive) a partir de (cidade_base, uf_base).

    Retorna ordenado por distância crescente — a cidade base sempre é o primeiro
    elemento (distância 0). Levanta ValueError se a cidade-base não existir.
    """
    base = buscar_municipio(cidade_base, uf_base)
    if base is None:
        raise ValueError(f"Cidade-base não encontrada: {cidade_base}/{uf_base}")

    resultado: list[Municipio] = []
    for m in carregar_municipios():
        d = distancia_haversine_km(base.latitude, base.longitude, m.latitude, m.longitude)
        if d <= raio_km:
            # Cópia com distância preenchida — evita mutar instância cacheada
            resultado.append(Municipio(
                codigo_ibge=m.codigo_ibge,
                nome=m.nome,
                uf=m.uf,
                latitude=m.latitude,
                longitude=m.longitude,
                distancia_do_ponto_km=round(d, 1),
            ))
    resultado.sort(key=lambda m: m.distancia_do_ponto_km)
    return resultado
