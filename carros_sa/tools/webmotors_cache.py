"""Cache de anúncios Webmotors em SQLite (`anuncio_webmotors`).

Workstream G — leitura/escrita da tabela já existente em `models.py` (não
mexer no schema). Encapsula:
  - Upsert por `id` (preserva `primeiro_visto`, atualiza `ultimo_visto`)
  - Marcação `sumiu_em` (proxy de venda — workstream G.2, tracking longitudinal)
  - TTL 24h pra `obter_estatisticas`: cache fresh evita re-fetch noturno

Sobre o match de ano: Webmotors lista anúncios em faixas `ano_fab/ano_mod`
(ex.: "2013/2014"). Persistimos `ano = ano_mod` mas a query do cache amplia
pra `WHERE ano IN (ano-1, ano, ano+1)` cobrindo casos onde `ano_fab` do
anúncio bate com o ano do lote consultado.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from sqlmodel import Session, select

from carros_sa.models import AnuncioWebmotors
from carros_sa.tools.webmotors import AnuncioWM, EstatisticasWM, estatisticas

logger = logging.getLogger(__name__)

# TTL do cache: 24h. Webmotors atualiza inventário diariamente; mediana de
# preço não muda materialmente em 24h. Re-fetch noturno só dispara pra
# (marca, modelo, ano) que não tem cache fresh.
_CACHE_TTL = timedelta(hours=24)


def _to_anuncio_wm(row: AnuncioWebmotors) -> AnuncioWM:
    """`AnuncioWebmotors` (SQLModel persistido) → `AnuncioWM` (datacalss puro)."""
    # `regiao` no DB é "Cidade (UF)" — split pro shape esperado pelo parser.
    cidade, uf = "", ""
    if row.regiao and "(" in row.regiao and ")" in row.regiao:
        cidade = row.regiao.split("(")[0].strip()
        uf = row.regiao.split("(")[1].split(")")[0].strip()
    return AnuncioWM(
        id=row.id,
        marca=row.marca,
        modelo=row.modelo,
        versao="",  # não persistido
        ano_fab=row.ano,
        ano_mod=row.ano,
        km=row.km or 0,
        cidade=cidade,
        uf=uf,
        preco=row.preco,
        abaixo_da_fipe=False,  # não persistido
        oferta_destaque=False,
    )


def persistir_anuncios(
    session: Session,
    anuncios: List[AnuncioWM],
    *,
    agora: Optional[datetime] = None,
) -> int:
    """Upsert por id. Preserva `primeiro_visto`; atualiza `ultimo_visto`.

    `sumiu_em` é mantido como NULL aqui — quem marca é
    `marcar_anuncios_sumidos` depois do batch noturno completo.
    """
    if not anuncios:
        return 0
    ts = agora or datetime.utcnow()
    inserted = 0
    for a in anuncios:
        existing = session.get(AnuncioWebmotors, a.id)
        regiao = f"{a.cidade} ({a.uf})" if a.cidade and a.uf else None
        if existing is None:
            session.add(AnuncioWebmotors(
                id=a.id,
                marca=a.marca,
                modelo=a.modelo,
                ano=a.ano_mod,
                km=a.km or None,
                preco=a.preco,
                url=f"https://www.webmotors.com.br/comprar/{a.id}",
                regiao=regiao,
                primeiro_visto=ts,
                ultimo_visto=ts,
                sumiu_em=None,
            ))
            inserted += 1
        else:
            existing.preco = a.preco
            existing.km = a.km or existing.km
            existing.regiao = regiao or existing.regiao
            existing.ultimo_visto = ts
            existing.sumiu_em = None  # reapareceu — limpa marcação anterior
    session.commit()
    return inserted


def marcar_anuncios_sumidos(
    session: Session,
    marca: str,
    modelo: str,
    ano: int,
    *,
    visto_em_ou_apos: datetime,
    agora: Optional[datetime] = None,
) -> int:
    """Marca `sumiu_em=now()` em anúncios desse (marca,modelo,ano) cuja última
    visualização é anterior ao batch atual.

    Lógica: se o cron noturno rodou e o anúncio não foi visto, ele saiu do
    estoque — provavelmente vendido. Workstream G.2 usa `sumiu_em` pra
    calibrar `dias_giro` via tempo real de mercado.
    """
    ts = agora or datetime.utcnow()
    rows = session.exec(
        select(AnuncioWebmotors).where(
            AnuncioWebmotors.marca == marca,
            AnuncioWebmotors.modelo == modelo,
            AnuncioWebmotors.ano == ano,
            AnuncioWebmotors.sumiu_em.is_(None),
            AnuncioWebmotors.ultimo_visto < visto_em_ou_apos,
        )
    ).all()
    for r in rows:
        r.sumiu_em = ts
    session.commit()
    return len(rows)


def obter_anuncios_cacheados(
    session: Session,
    marca: str,
    modelo: str,
    ano: int,
    *,
    ttl: timedelta = _CACHE_TTL,
    agora: Optional[datetime] = None,
) -> List[AnuncioWM]:
    """Lê anúncios fresh do cache (ultimo_visto > now - ttl) pra (marca, modelo, ano).

    Match de ano cobre faixa `ano_fab/ano_mod` (anúncio "2013/2014" bate com
    busca de 2013 OU 2014). Sem cache fresh → lista vazia.
    """
    ts = agora or datetime.utcnow()
    cutoff = ts - ttl
    marca_norm = marca.strip().lower()
    modelo_norm = modelo.strip().lower()
    rows = session.exec(
        select(AnuncioWebmotors).where(
            AnuncioWebmotors.marca.ilike(marca_norm),
            AnuncioWebmotors.modelo.ilike(modelo_norm),
            AnuncioWebmotors.ano.in_([ano - 1, ano, ano + 1]),
            AnuncioWebmotors.ultimo_visto >= cutoff,
            AnuncioWebmotors.sumiu_em.is_(None),
        )
    ).all()
    return [_to_anuncio_wm(r) for r in rows]


def obter_estatisticas_cacheadas(
    session: Session,
    marca: str,
    modelo: str,
    ano: int,
    *,
    ttl: timedelta = _CACHE_TTL,
    agora: Optional[datetime] = None,
) -> Optional[EstatisticasWM]:
    """Devolve `EstatisticasWM` do cache fresh, ou None se sem amostra.

    Diferente do fallback antigo (FIPE×0.97 quando sem dado AA), aqui o
    contrato é honesto: sem amostra Webmotors → None. Caller decide se
    suprime display ou usa fallback (mas o caller correto pro workstream G é
    `avaliador_mercado`, que vai sinalizar `n_anuncios_competidores=0` e
    deixar `webmotors_mediana` igual à FIPE como placeholder neutro — o
    display ainda mostra "—" quando `n=0` pra refletir "sem sinal de mercado").
    """
    anuncios = obter_anuncios_cacheados(
        session, marca, modelo, ano, ttl=ttl, agora=agora,
    )
    if not anuncios:
        return None
    return estatisticas(marca, modelo, ano, anuncios=anuncios)
