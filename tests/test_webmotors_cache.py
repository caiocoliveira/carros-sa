"""Gold tests do cache Webmotors (workstream G — 2026-05-12).

Cobre:
  - Upsert por id: primeiro_visto preservado, ultimo_visto atualizado
  - TTL 24h: anúncios vistos há >24h não entram em `obter_anuncios_cacheados`
  - Match de ano em faixa: busca 2013 inclui anúncios com `ano = 2012, 2013, 2014`
  - `marcar_anuncios_sumidos`: sumiu_em é setado quando ultimo_visto < batch_start
  - `obter_estatisticas_cacheadas`: integra com `webmotors.estatisticas`
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from carros_sa.models import AnuncioWebmotors
from carros_sa.tools.webmotors import AnuncioWM
from carros_sa.tools.webmotors_cache import (
    marcar_anuncios_sumidos,
    obter_anuncios_cacheados,
    obter_estatisticas_cacheadas,
    persistir_anuncios,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _anuncio(id_: str, marca="Ford", modelo="Fiesta", ano=2013, preco=30000, km=100000):
    return AnuncioWM(
        id=id_, marca=marca, modelo=modelo, versao="1.6 SE",
        ano_fab=ano, ano_mod=ano, km=km, cidade="São Paulo", uf="SP",
        preco=preco,
    )


def test_persistir_anuncios_novo_insere(session):
    anuncios = [_anuncio("a1", preco=32000), _anuncio("a2", preco=28000)]
    n = persistir_anuncios(session, anuncios)
    assert n == 2
    rows = session.exec(select(AnuncioWebmotors)).all()
    assert len(rows) == 2
    assert rows[0].marca == "Ford"
    assert rows[0].preco in {32000, 28000}


def test_persistir_anuncios_upsert_preserva_primeiro_visto(session):
    ts1 = datetime(2026, 5, 1, 3, 0)
    persistir_anuncios(session, [_anuncio("a1", preco=30000)], agora=ts1)

    ts2 = datetime(2026, 5, 2, 3, 0)
    persistir_anuncios(session, [_anuncio("a1", preco=29000)], agora=ts2)

    rows = session.exec(select(AnuncioWebmotors)).all()
    assert len(rows) == 1  # mesmo id → 1 row
    assert rows[0].primeiro_visto == ts1  # NÃO mudou
    assert rows[0].ultimo_visto == ts2  # atualizou
    assert rows[0].preco == 29000  # preço atualizou


def test_obter_anuncios_cacheados_respeita_ttl(session):
    """Anúncios com ultimo_visto < (agora - 24h) NÃO entram no cache fresh."""
    ts_recente = datetime(2026, 5, 12, 3, 0)
    ts_velho = datetime(2026, 5, 10, 3, 0)  # 48h atrás

    persistir_anuncios(session, [_anuncio("a_recente", preco=30000)], agora=ts_recente)
    persistir_anuncios(session, [_anuncio("a_velho", preco=99000)], agora=ts_velho)

    anuncios = obter_anuncios_cacheados(
        session, "Ford", "Fiesta", 2013,
        ttl=timedelta(hours=24), agora=ts_recente,
    )
    assert len(anuncios) == 1
    assert anuncios[0].id == "a_recente"


def test_obter_anuncios_cacheados_match_de_ano_em_faixa(session):
    """Webmotors lista anúncios em faixas (ex.: 2013/2014). Busca por 2013 deve
    incluir anúncios com `ano = 2012`, `2013` ou `2014` (cobre ano_fab/ano_mod
    de ambos os lados)."""
    ts = datetime(2026, 5, 12, 3, 0)
    persistir_anuncios(session, [
        _anuncio("a_2012", ano=2012, preco=27000),
        _anuncio("a_2013", ano=2013, preco=30000),
        _anuncio("a_2014", ano=2014, preco=32000),
        _anuncio("a_2010", ano=2010, preco=20000),  # fora da faixa
    ], agora=ts)

    anuncios = obter_anuncios_cacheados(session, "Ford", "Fiesta", 2013, agora=ts)
    ids = sorted(a.id for a in anuncios)
    assert ids == ["a_2012", "a_2013", "a_2014"]


def test_marcar_anuncios_sumidos_seta_sumiu_em(session):
    """Anúncios que NÃO foram vistos no batch atual ganham `sumiu_em`."""
    ts_batch_anterior = datetime(2026, 5, 11, 3, 0)
    ts_batch_atual = datetime(2026, 5, 12, 3, 0)

    persistir_anuncios(session, [
        _anuncio("a_velho", preco=30000),
        _anuncio("a_atual", preco=29000),
    ], agora=ts_batch_anterior)

    # Só `a_atual` reaparece no batch de hoje
    persistir_anuncios(session, [_anuncio("a_atual", preco=29000)], agora=ts_batch_atual)

    n = marcar_anuncios_sumidos(
        session, "Ford", "Fiesta", 2013,
        visto_em_ou_apos=ts_batch_atual,
        agora=ts_batch_atual,
    )
    assert n == 1
    rows = session.exec(select(AnuncioWebmotors)).all()
    sumidos = {r.id: r.sumiu_em for r in rows}
    assert sumidos["a_velho"] == ts_batch_atual
    assert sumidos["a_atual"] is None


def test_marcar_sumido_e_reaparecer_limpa_sumiu_em(session):
    """Anúncio marcado sumido que reaparece em batch posterior limpa `sumiu_em`."""
    ts1 = datetime(2026, 5, 1, 3, 0)
    ts2 = datetime(2026, 5, 5, 3, 0)
    ts3 = datetime(2026, 5, 10, 3, 0)

    persistir_anuncios(session, [_anuncio("a1", preco=30000)], agora=ts1)
    # No batch 2, `a1` não apareceu — marca sumido
    marcar_anuncios_sumidos(session, "Ford", "Fiesta", 2013,
                            visto_em_ou_apos=ts2, agora=ts2)
    row = session.get(AnuncioWebmotors, "a1")
    assert row.sumiu_em == ts2

    # No batch 3, `a1` reaparece (vendedor reativou anúncio)
    persistir_anuncios(session, [_anuncio("a1", preco=28500)], agora=ts3)
    row = session.get(AnuncioWebmotors, "a1")
    assert row.sumiu_em is None
    assert row.ultimo_visto == ts3


def test_obter_estatisticas_cacheadas_devolve_none_sem_amostra(session):
    """Sem anúncios cacheados → None (sinaliza ao avaliador 'usar placeholder')."""
    stats = obter_estatisticas_cacheadas(session, "Ford", "Fiesta", 2013)
    assert stats is None


def test_obter_estatisticas_cacheadas_calcula_mediana(session):
    ts = datetime(2026, 5, 12, 3, 0)
    persistir_anuncios(session, [
        _anuncio("a1", preco=28000, km=80000),
        _anuncio("a2", preco=30000, km=100000),
        _anuncio("a3", preco=32000, km=120000),
    ], agora=ts)

    stats = obter_estatisticas_cacheadas(session, "Ford", "Fiesta", 2013, agora=ts)
    assert stats is not None
    assert stats.n_anuncios == 3
    assert stats.mediana == 30000
    assert stats.km_mediana == 100000


def test_anuncios_sumidos_nao_entram_no_cache_fresh(session):
    """Anúncio com `sumiu_em != NULL` é filtrado mesmo se ultimo_visto é recente."""
    ts = datetime(2026, 5, 12, 3, 0)
    persistir_anuncios(session, [_anuncio("a1", preco=30000)], agora=ts)
    marcar_anuncios_sumidos(session, "Ford", "Fiesta", 2013,
                            visto_em_ou_apos=ts + timedelta(hours=1), agora=ts)

    anuncios = obter_anuncios_cacheados(session, "Ford", "Fiesta", 2013,
                                        agora=ts + timedelta(hours=2))
    assert anuncios == []
