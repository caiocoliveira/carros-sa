"""Testes do AvaliadorMercado e do FipeClient.

Gold test usa similares REAIS extraídos pela página de detalhe do Fiesta
21854782 (Auto Avaliar, 2026-04-14). FIPE é mockada — testes não batem em rede.
"""

from __future__ import annotations

from typing import Optional

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.agents.avaliador_mercado import _percentil, avaliar
from carros_sa.models import ModeloFipeCache  # noqa: F401  (registra metadata)
from carros_sa.tools.fipe import FipeClient, _match_ano, _match_nome, _parse_brl


# =============================================================================
# Fakes
# =============================================================================

class FakeFipeOK:
    def __init__(self, valor: int = 28_000):
        self.valor = valor
        self.chamadas = 0

    def consultar(self, marca, modelo, ano) -> Optional[int]:
        self.chamadas += 1
        return self.valor


class FakeFipeMiss:
    def consultar(self, marca, modelo, ano) -> Optional[int]:
        return None


# =============================================================================
# avaliar() — gold test com dado real
# =============================================================================

# Similares reais que a plataforma mostrou no detalhe do Fiesta 21854782
# (Ford Fiesta 1.6 SE Hatch 16V Flex 4P Manual, 2012/2013).
# Faixa típica de hatch popular usado: 23k–45k.
SIMILARES_FIESTA_2013 = [45_000, 23_200, 27_000, 30_000, 28_500, 26_400, 32_000]


def test_avalia_fiesta_2013_combina_fipe_com_similares_da_plataforma():
    sinal = avaliar(
        "Ford", "Fiesta", 2013,
        similares_precos=SIMILARES_FIESTA_2013,
        fipe_client=FakeFipeOK(28_000),
    )

    assert sinal.fipe == 28_000
    assert sinal.n_anuncios_competidores == 7
    # mediana de [23200,26400,27000,28500,30000,32000,45000] = 28500
    assert sinal.webmotors_mediana == 28_500
    # p25 fica abaixo da mediana (peças do começo da distribuição)
    assert sinal.webmotors_p25 < sinal.webmotors_mediana
    assert sinal.webmotors_p25 >= 23_200  # nunca abaixo do menor
    # 7 competidores = mercado com competição moderada
    assert sinal.dias_giro_estimado == 35


def test_avalia_descarta_valores_ruidosos_abaixo_do_minimo():
    """Parcela mensal R$ 599 ou listings quebrados não devem entrar na estatística."""
    similares = [599, 1_200, 27_000, 28_500, 30_000]  # 2 ruídos
    sinal = avaliar("Ford", "Fiesta", 2013,
                    similares_precos=similares,
                    fipe_client=FakeFipeOK(28_000))
    assert sinal.n_anuncios_competidores == 3
    assert sinal.webmotors_mediana == 28_500


def test_avalia_sem_similares_usa_fipe_como_ancora_e_aplica_p25_padrao():
    sinal = avaliar("Ford", "Fiesta", 2013,
                    similares_precos=[],
                    fipe_client=FakeFipeOK(28_000))
    assert sinal.fipe == 28_000
    assert sinal.webmotors_mediana == 28_000
    assert sinal.webmotors_p25 == 23_800  # 28000 * 0.85
    assert sinal.n_anuncios_competidores == 0
    assert sinal.dias_giro_estimado == 60  # mercado seco, giro lento


def test_avalia_sem_fipe_mas_com_similares_usa_mediana_como_ancora():
    sinal = avaliar("Marca", "Modelo", 2020,
                    similares_precos=[40_000, 50_000, 60_000],
                    fipe_client=FakeFipeMiss())
    # FIPE caiu pra mediana
    assert sinal.fipe == 50_000
    assert sinal.webmotors_mediana == 50_000


def test_avalia_sem_fipe_nem_similares_levanta_erro():
    with pytest.raises(ValueError, match="Sem FIPE nem similares"):
        avaliar("X", "Y", 2020, similares_precos=[], fipe_client=FakeFipeMiss())


# =============================================================================
# FipeClient — cache hit-first
# =============================================================================

def test_fipe_client_cache_hit_evita_chamada_de_rede(tmp_path):
    """Se o cache tem o valor, _buscar_api nunca é chamado."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ModeloFipeCache(marca="ford", modelo="fiesta", ano=2013, valor=27_900))
        s.commit()

    class HttpQueExplode:
        def get(self, url):
            raise AssertionError(f"Não devia bater na rede; url={url}")

    client = FipeClient(
        http_client=HttpQueExplode(),
        session_factory=lambda: Session(engine),
    )
    assert client.consultar("Ford", "Fiesta", 2013) == 27_900


def test_fipe_client_busca_e_persiste_no_cache(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(engine)

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self): pass
        def json(self): return self._payload

    class FakeHttp:
        def __init__(self):
            self.calls = 0
            self.routes = {
                "brands": [{"code": "21", "name": "Ford"}],
                "models": [{"code": "5940", "name": "Fiesta 1.6 SE Hatch 16V Flex 4P"}],
                "years": [{"code": "2013-3", "name": "2013 Flex"}],
                "price": {"price": "R$ 27.890,00"},
            }
        def get(self, url):
            self.calls += 1
            if "/brands/21/models/5940/years/2013-3" in url:
                return FakeResp(self.routes["price"])
            if "/brands/21/models/5940/years" in url:
                return FakeResp(self.routes["years"])
            if "/brands/21/models" in url:
                return FakeResp(self.routes["models"])
            return FakeResp(self.routes["brands"])

    http = FakeHttp()
    client = FipeClient(
        http_client=http,
        session_factory=lambda: Session(engine),
    )

    valor = client.consultar("Ford", "Fiesta 1.6 SE Hatch 16V Flex 4P", 2013)
    assert valor == 27_890
    assert http.calls == 4

    # 2ª chamada — deve vir do cache, sem hit de rede
    http.calls = 0
    valor2 = client.consultar("Ford", "Fiesta 1.6 SE Hatch 16V Flex 4P", 2013)
    assert valor2 == 27_890
    assert http.calls == 0


# =============================================================================
# Helpers internos
# =============================================================================

def test_parse_brl():
    assert _parse_brl("R$ 27.890,00") == 27_890
    assert _parse_brl("12.345") == 12_345
    assert _parse_brl("") is None
    assert _parse_brl("sem-numero") is None


def test_match_nome_exato_e_substring():
    items = [{"name": "Ford", "code": "21"}, {"name": "Fiat", "code": "22"}]
    assert _match_nome(items, "ford")["code"] == "21"
    assert _match_nome(items, "FORD")["code"] == "21"
    items2 = [{"name": "Fiesta 1.6 SE", "code": "1"}, {"name": "Outro", "code": "2"}]
    assert _match_nome(items2, "Fiesta")["code"] == "1"
    assert _match_nome(items, "Toyota") is None


def test_match_ano_pega_primeiro_combustivel():
    years = [{"code": "2013-1", "name": "2013 Gas"}, {"code": "2013-3", "name": "2013 Flex"}]
    assert _match_ano(years, 2013)["code"] == "2013-1"
    assert _match_ano(years, 2014) is None


def test_percentil_interpolacao():
    assert _percentil([10, 20, 30, 40, 50], 25) == 20
    assert _percentil([10, 20, 30, 40, 50], 50) == 30
    assert _percentil([100], 25) == 100
