"""Gold tests do AvaliadorMercado.

Usa fixture FIPE pré-gravada (tests/fixtures/fipe_fiesta_2013.json) montada a
partir do shape real da API Parallelum. Não bate na rede.

Cobertura:
  - Lookup FIPE (marca → modelo → ano → valor) com nomes "sujos" estilo Auto Avaliar
  - Combinação com amostra Webmotors (workstream G) injetada via `webmotors_anuncios`
  - Cache persistente em ModeloFipeCache: 2ª chamada não bate na fonte
  - Sem amostra: mediana = FIPE (placeholder neutro), n_anuncios=0 (display vira "—")

Workstream G (2026-05-12): fonte de mediana mudou de "similares Auto Avaliar"
(poluídos por outliers categóricos) pra "Webmotors live cache". Cap defensivo
n<5 → FIPE×1.20 foi removido (band-aid pro AA, não mais necessário).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from carros_sa.agents.avaliador_mercado import avaliar
from carros_sa.models import CategoriaVeiculo, ModeloFipeCache
from carros_sa.tools.fipe import FipeClient, _parse_valor
from carros_sa.tools.webmotors import AnuncioWM


def _anuncios(precos: List[int], marca: str = "Ford", modelo: str = "Fiesta",
              ano: int = 2013, km_default: int = 100_000) -> List[AnuncioWM]:
    """Helper: lista de AnuncioWM a partir de uma lista de preços. Match de
    ano cobre `ano_fab == ano_mod == ano` (suficiente pros testes)."""
    return [
        AnuncioWM(
            id=f"wm{i}", marca=marca, modelo=modelo, versao="",
            ano_fab=ano, ano_mod=ano, km=km_default, cidade="São Paulo", uf="SP",
            preco=preco,
        )
        for i, preco in enumerate(precos)
    ]

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fipe_fiesta_2013.json"
FIXTURE_CHERY = Path(__file__).resolve().parent / "fixtures" / "fipe_chery_tiggo_2015.json"


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


def test_avaliador_fiesta_com_amostra_webmotors(fipe_responses, in_memory_session):
    """Fiesta 2013: amostra Webmotors injetada + FIPE.

    Preços observados em anúncios Webmotors fiesta 2013 (fixture real do
    workstream B): 45.000, 23.200, 27.000, 29.500, 31.000, 35.500.
    """
    fake = FakeFipeClient(fipe_responses)
    anuncios = _anuncios([45000, 23200, 27000, 29500, 31000, 35500])

    sinal = avaliar(
        marca="FORD",
        modelo="FIESTA 1.6 SE HATCH 16V FLEX 4P MANUAL",
        ano=2013,
        km=171053,
        webmotors_anuncios=anuncios,
        categoria=CategoriaVeiculo.HATCH,
        fipe_client=fake,
        session=in_memory_session,
    )

    # FIPE bateu da fixture (R$ 30.876)
    assert sinal.fipe == 30876
    # Mediana dos anúncios = (29500+31000)/2 = 30250
    assert sinal.webmotors_mediana == 30250
    # p25 (n=6): índice 0.25*5=1.25 entre 25k-ish
    assert 23200 <= sinal.webmotors_p25 <= 29500
    assert sinal.n_anuncios_competidores == 6
    # Fiesta 2013 (idade 13) → hatch VELHO → prior 100d
    # n>=6 → ajuste de liquidez: -5 = 95
    assert sinal.dias_giro_estimado == 95

    # 4 chamadas HTTP "esperadas": marcas, modelos, anos, valor
    assert len(fake.calls) == 4


def test_cache_persistente_evita_segunda_chamada(fipe_responses, in_memory_session):
    fake = FakeFipeClient(fipe_responses)
    anuncios = _anuncios([28000, 30000, 32000])

    s1 = avaliar(
        marca="FORD",
        modelo="FIESTA 1.6 SE HATCH",
        ano=2013,
        webmotors_anuncios=anuncios,
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
        webmotors_anuncios=anuncios,
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


def test_chery_tiggo_2015_nao_cai_em_marca_errada(in_memory_session):
    """Regressão do bug que motivou a migração pra v2 em 2026-04-23.

    A FIPE tem DUAS marcas Chery: "Caoa Chery" (245, só modelos novos) e
    "Caoa Chery/Chery" (161, catálogo completo legacy + atual). Query "Chery"
    empata o token `chery` nas duas. O código v1 antigo usava `>` estrito no
    scoring, ficava com a primeira iterada (245), e o Tiggo 2.0 2015 acabava
    matchando um Tiggo 7 novo ~R$ 114k em vez do valor real ~R$ 41k.

    Esse teste garante que mesmo com a 245 aparecendo primeiro na lista de
    marcas, `consultar` tenta AMBAS as candidatas e fica com a do melhor match
    de modelo (Tiggo 2.0 → 4 tokens na 161, Tiggo 7 Pro → 2 tokens na 245).
    """
    fipe_responses = json.loads(FIXTURE_CHERY.read_text())
    fake = FakeFipeClient(fipe_responses)

    sinal = avaliar(
        marca="Chery",
        modelo="Tiggo 2.0 16V GASOLINA 4P AUTOMATICO",
        ano=2015,
        km=120000,
        webmotors_anuncios=None,
        categoria=CategoriaVeiculo.SUV,
        fipe_client=fake,
        session=in_memory_session,
    )

    assert sinal.fipe == 41512, (
        f"Esperava FIPE do Tiggo 2.0 2015 (R$ 41.512), veio R$ {sinal.fipe}. "
        f"Se veio ~114k, bateu de novo no bug de marca errada."
    )


def test_sem_amostra_webmotors_mediana_eh_fipe_placeholder(fipe_responses, in_memory_session):
    """Sem amostra Webmotors: mediana = FIPE (placeholder neutro), n=0.

    Workstream G (2026-05-12): substituiu o fallback antigo `FIPE × 0.97` que
    era da era AA-driven. Agora o contrato é honesto: sem dado real de mercado,
    `webmotors_mediana = fipe` e `n_anuncios_competidores = 0` sinaliza ao
    display "sem amostra" → coluna "Mediana mercado" mostra "—".
    """
    fake = FakeFipeClient(fipe_responses)
    sinal = avaliar(
        marca="Ford",
        modelo="Fiesta 1.6 SE Hatch",
        ano=2013,
        webmotors_anuncios=None,
        categoria=CategoriaVeiculo.HATCH,
        fipe_client=fake,
        session=in_memory_session,
    )
    assert sinal.fipe == 30876
    # Sem amostra: mediana = FIPE (placeholder). NÃO mais FIPE × 0.97.
    assert sinal.webmotors_mediana == 30876
    # p25 sentinela conservador (não usado em cálculo)
    assert sinal.webmotors_p25 == round(30876 * 0.88)
    # n=0 — display em sheets/audit mostra "—" pra "Mediana mercado"
    assert sinal.n_anuncios_competidores == 0
    # Fiesta 2013 → hatch VELHO → prior 100d. n=0 não dispara ajuste de liquidez.
    assert sinal.dias_giro_estimado == 100


def test_amostra_webmotors_pequena_nao_eh_capada(fipe_responses, in_memory_session):
    """Workstream G: cap defensivo n<5 → FIPE×1.20 foi REMOVIDO.

    O cap era band-aid pra similares poluídos do Auto Avaliar (Tiggo 7 entre
    Tiggos 2, Airtrek entre Outlander). Webmotors live tem amostra precisa
    por (marca, modelo, ano) — sem mistura categórica. Confiamos no que a
    fonte retorna; se a mediana sair alta, audit avisa via
    `_check_mediana_distante_fipe` (>1.20× → flag informativo, não cap).
    """
    fake = FakeFipeClient(fipe_responses)
    # FIPE Fiesta 2013 = R$ 30.876. n=2, mediana = (30k+45k)/2 = 37.5k = 121% FIPE.
    sinal = avaliar(
        marca="Ford", modelo="Fiesta 1.6 SE Hatch", ano=2013,
        webmotors_anuncios=_anuncios([30000, 45000]),
        categoria=CategoriaVeiculo.HATCH,
        fipe_client=fake, session=in_memory_session,
    )
    # Mediana NÃO é capada — fica nos 37.5k reais
    assert sinal.webmotors_mediana == 37500
    assert sinal.webmotors_mediana > sinal.fipe * 1.20  # passa do "cap antigo"
    assert sinal.n_anuncios_competidores == 2


def test_km_mediana_derivado_da_amostra_webmotors(fipe_responses, in_memory_session):
    """`webmotors_km_mediana` é calculado a partir dos `km` dos anúncios injetados
    quando não é passado externamente. Workstream G fecha o gap do ROADMAP:523."""
    fake = FakeFipeClient(fipe_responses)
    anuncios = [
        AnuncioWM(id="a", marca="Ford", modelo="Fiesta", versao="", ano_fab=2013,
                  ano_mod=2013, km=80_000, cidade="SP", uf="SP", preco=32000),
        AnuncioWM(id="b", marca="Ford", modelo="Fiesta", versao="", ano_fab=2013,
                  ano_mod=2013, km=120_000, cidade="RJ", uf="RJ", preco=28000),
        AnuncioWM(id="c", marca="Ford", modelo="Fiesta", versao="", ano_fab=2013,
                  ano_mod=2013, km=100_000, cidade="MG", uf="MG", preco=30000),
    ]
    sinal = avaliar(
        marca="Ford", modelo="Fiesta 1.6 SE Hatch", ano=2013, km=150_000,
        webmotors_anuncios=anuncios,
        categoria=CategoriaVeiculo.HATCH,
        fipe_client=fake, session=in_memory_session,
    )
    # mediana dos km: 100_000 (elemento do meio)
    assert sinal.webmotors_km_mediana == 100_000
