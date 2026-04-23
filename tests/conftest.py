"""Fixtures globais da suite.

Resetar caches `lru_cache` entre testes evita state leak — se teste A carrega
config empresa X, teste B que monkey-patcheia algo (env var, YAML) pode ver
o valor cacheado de A em vez do fresh. Hoje nenhum teste cai nesse caso,
mas é defesa em profundidade — quando alguém adicionar um teste que mexer
em env var, os caches vão estar limpos.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_lru_caches():
    """Roda antes/depois de cada teste — limpa todo lru_cache relevante."""
    from carros_sa.agents.estimador_reforma import carregar_tabela
    from carros_sa.config import reset_settings_cache
    from carros_sa.tenancy import carregar_empresa
    from carros_sa.tools.geo import carregar_municipios
    from carros_sa.tools.popularidade import carregar_ranking

    def clear_all():
        reset_settings_cache()
        carregar_empresa.cache_clear()
        carregar_tabela.cache_clear()
        carregar_municipios.cache_clear()
        carregar_ranking.cache_clear()

    clear_all()
    yield
    clear_all()
