"""Testes de carros_sa.tools.geo — raio operacional e distâncias haversine.

Usa o CSV real de municípios em data/geo/municipios.csv (5570 cidades BR).
"""

from __future__ import annotations

import pytest

from carros_sa.tools.geo import (
    Municipio,
    cidades_no_raio,
    distancia_haversine_km,
    buscar_municipio,
)


# =============================================================================
# Haversine — distâncias conhecidas entre capitais
# =============================================================================

def test_haversine_mesma_cidade_retorna_zero():
    uberlandia = (-18.9141, -48.2749)
    assert distancia_haversine_km(*uberlandia, *uberlandia) == 0


def test_haversine_uberlandia_uberaba_cerca_de_100km():
    # Uberlândia e Uberaba ficam ~100km de distância rodoviária; linha reta é menor.
    uberlandia = (-18.9141, -48.2749)
    uberaba = (-19.7472, -47.9381)
    d = distancia_haversine_km(*uberlandia, *uberaba)
    assert 90 < d < 110


def test_haversine_uberlandia_brasilia_cerca_de_400km():
    uberlandia = (-18.9141, -48.2749)
    brasilia = (-15.7797, -47.9297)
    d = distancia_haversine_km(*uberlandia, *brasilia)
    assert 350 < d < 450


# =============================================================================
# Busca municipal por nome+UF
# =============================================================================

def test_buscar_municipio_uberlandia_mg():
    m = buscar_municipio("Uberlândia", "MG")
    assert m is not None
    assert m.uf == "MG"
    assert round(m.latitude, 2) == -18.91


def test_buscar_municipio_case_insensitive_sem_acento():
    assert buscar_municipio("uberlandia", "mg") is not None
    assert buscar_municipio("UBERLÂNDIA", "MG") is not None


def test_buscar_municipio_inexistente_retorna_none():
    assert buscar_municipio("Xpto-não-existe", "MG") is None


# =============================================================================
# Cidades no raio — a feature principal
# =============================================================================

def test_cidades_no_raio_zero_retorna_apenas_a_propria():
    resultado = cidades_no_raio("Uberlândia", "MG", raio_km=0)
    assert len(resultado) == 1
    assert resultado[0].nome_normalizado == "uberlandia"


def test_cidades_no_raio_pequeno_inclui_vizinhas_diretas():
    # Raio 60km a partir de Uberlândia deve capturar pelo menos Araguari (~40km),
    # Tupaciguara e Prata.
    resultado = cidades_no_raio("Uberlândia", "MG", raio_km=60)
    nomes = {m.nome_normalizado for m in resultado}
    assert "uberlandia" in nomes
    assert "araguari" in nomes  # ~40km


def test_cidades_no_raio_medio_pega_estado_vizinho():
    # Raio 150km alcança Catalão/GO (~120km) — valida que cruza UF.
    resultado = cidades_no_raio("Uberlândia", "MG", raio_km=150)
    ufs = {m.uf for m in resultado}
    assert "MG" in ufs
    assert "GO" in ufs, "Raio 150km deveria incluir Catalão/GO"


def test_cidades_no_raio_ordenadas_por_distancia():
    resultado = cidades_no_raio("Uberlândia", "MG", raio_km=200)
    # Primeira sempre é a própria Uberlândia (distância 0)
    assert resultado[0].nome_normalizado == "uberlandia"
    # E o restante deve estar ordenado crescente por distância
    distancias = [m.distancia_do_ponto_km for m in resultado]
    assert distancias == sorted(distancias)


def test_cidades_no_raio_cidade_inexistente_levanta():
    with pytest.raises(ValueError, match="não encontrada"):
        cidades_no_raio("Cidade-que-não-existe", "MG", raio_km=100)
