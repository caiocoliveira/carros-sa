"""Gold tests do AvaliadorMercado.

Usa fixture FIPE pré-gravada (tests/fixtures/fipe_fiesta_2013.json) montada a
partir do shape real da API Parallelum. Não bate na rede.

Cobertura:
  - Lookup FIPE (marca → modelo → ano → valor) com nomes "sujos" estilo Auto Avaliar
  - Combinação com similares reais (Fiesta 2013 do lote 21854782: 45k, 23.2k, 27k, ...)
  - Cache persistente em ModeloFipeCache: 2ª chamada não bate na fonte
  - Fallback FIPE-only quando não há similares
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from carros_sa.agents.avaliador_mercado import avaliar
from carros_sa.models import CategoriaVeiculo, ModeloFipeCache
from carros_sa.tools.fipe import FipeClient, _parse_valor

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fipe_fiesta_2013.json"


class FakeFipeClient(FipeClient):
    """FipeClient que serve responses de um dict path → payload, contando hits."""

    def __init__(self, responses: dict):
        # NÃO chama super().__init__ — não queremos abrir httpx.Client.
        self._responses = responses
        self._cache: dict = {}
        self.calls: list = []

    def _get(self, path: str):
        if path in self._cache:
            return self._cache[path]
        self.calls.append(path)
        if path not in self._responses:
            raise LookupError(f"fixture sem resposta para {path}")
        data = self._responses[path]
        self._cache[path] = data
        return data

    def close(self):
        pass


@pytest.fixture
def fipe_responses() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def in_memory_session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_parse_valor_brl():
    assert _parse_valor("R$ 30.876,00") == 30876
    assert _parse_valor("R$ 1.234.567,89") == 1234567


def test_avaliador_fiesta_com_similares_reais(fipe_responses, in_memory_session):
    """Fiesta 2013 do lote 21854782: similares reais da plataforma + FIPE.

    Similares observados na seção 'Talvez se interesse por' do detalhe:
      45.000, 23.200, 27.000, 29.500, 31.000, 35.500
    """
    fake = FakeFipeClient(fipe_responses)
    similares = [45000, 23200, 27000, 29500, 31000, 35500]

    sinal = avaliar(
        marca="FORD",
        modelo="FIESTA 1.6 SE HATCH 16V FLEX 4P MANUAL",
        ano=2013,
        km=171053,
        similares_precos=similares,
        categoria=CategoriaVeiculo.HATCH,
        fipe_client=fake,
        session=in_memory_session,
    )

    # FIPE bateu da fixture (R$ 30.876)
    assert sinal.fipe == 30876
    # Mediana dos similares = (29500+31000)/2 = 30250
    assert sinal.webmotors_mediana == 30250
    # p25 (n=6): índice 0.25*5=1.25 entre 25k-ish
    assert 23200 <= sinal.webmotors_p25 <= 29500
    assert sinal.n_anuncios_competidores == 6
    # n>=6 → ajuste de liquidez: hatch baseline 25 - 5 = 20
    assert sinal.dias_giro_estimado == 20

    # 4 chamadas HTTP "esperadas": marcas, modelos, anos, valor
    assert len(fake.calls) == 4


def test_cache_persistente_evita_segunda_chamada(fipe_responses, in_memory_session):
    fake = FakeFipeClient(fipe_responses)

    s1 = avaliar(
        marca="FORD",
        modelo="FIESTA 1.6 SE HATCH",
        ano=2013,
        similares_precos=[28000, 30000, 32000],
        categoria=CategoriaVeiculo.HATCH,
        fipe_client=fake,
        session=in_memory_session,
    )
    chamadas_primeira = len(fake.calls)
    assert chamadas_primeira == 4

    # Reabre cliente do zero (sem cache in-memory) — segunda chamada deve usar
    # ModeloFipeCache no SQLite e não bater em rota nenhuma.
    fake2 = FakeFipeClient(fipe_responses)
    s2 = avaliar(
        marca="FORD",
        modelo="FIESTA 1.6 SE HATCH",
        ano=2013,
        similares_precos=[28000, 30000, 32000],
        categoria=CategoriaVeiculo.HATCH,
        fipe_client=fake2,
        session=in_memory_session,
    )
    assert s1.fipe == s2.fipe
    assert fake2.calls == []  # cache hit → zero chamadas

    # E exatamente 1 linha persistida.
    rows = in_memory_session.exec(select(ModeloFipeCache)).all()
    assert len(rows) == 1
    assert rows[0].valor == 30876


def test_fallback_sem_similares(fipe_responses, in_memory_session):
    fake = FakeFipeClient(fipe_responses)
    sinal = avaliar(
        marca="Ford",
        modelo="Fiesta 1.6 SE Hatch",
        ano=2013,
        similares_precos=None,
        categoria=CategoriaVeiculo.HATCH,
        fipe_client=fake,
        session=in_memory_session,
    )
    assert sinal.fipe == 30876
    # FIPE × 0.90 e × 0.78
    assert sinal.webmotors_mediana == round(30876 * 0.90)
    assert sinal.webmotors_p25 == round(30876 * 0.78)
    assert sinal.n_anuncios_competidores == 0
    # n=0 não dispara ajuste de liquidez (range é 1..2 ou >=6) → baseline hatch 25
    assert sinal.dias_giro_estimado == 25
