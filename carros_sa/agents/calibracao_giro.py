"""Calibração de `dias_giro_estimado` a partir do histórico real (Arrematado).

Quando há ≥3 vendas concluídas pra uma categoria de veículo, calcula a média
de `vendido_em - data` e usa como prior. Caso contrário, cai pro hardcoded em
[avaliador_mercado._DIAS_GIRO_DEFAULT](../agents/avaliador_mercado.py).

Cache em memória por (empresa_id, categoria) — invalidação por TTL curto pra
permitir que novas vendas afetem a calibração na próxima run.

Categoria do veículo é inferida do nome do modelo (sem coluna dedicada em Lote
ainda — pattern reusado do orquestrador._calcular_frete).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from sqlmodel import Session, select

from carros_sa.models import Arrematado, CategoriaVeiculo, Lote


# TTL do cache — 1h é mais que suficiente pra batch run; calibração nova fica
# disponível na próxima invocação humana.
_CACHE_TTL = timedelta(hours=1)
_MIN_AMOSTRAS_CALIBRACAO = 3


# (empresa_id, categoria) -> (dias_giro_calibrado, calculado_em)
_cache: Dict[Tuple[str, CategoriaVeiculo], Tuple[int, datetime]] = {}


def _categoria_de_modelo(modelo: str) -> CategoriaVeiculo:
    """Inferência grosseira por substring no nome do modelo.

    Mantém alinhado com [orquestrador._calcular_frete](../orquestrador.py)
    pra que histórico e novos lotes caiam na mesma bucket. Lista expandida
    após rodagens reais — incluindo marcas chinesas (Tiggo/Haval), lançamentos
    Fiat recentes (Fastback/Pulse/Cronos), SUVs compactos e picapes menos
    comuns (Oroch/Maverick/Montana/Triton).
    """
    m = (modelo or "").lower()
    if any(k in m for k in (
        "hilux", "s10", "saveiro", "strada", "ranger", "frontier", "amarok", "toro",
        "l200", "oroch", "maverick", "montana", "courier", "triton", "dakota",
        "f-250", "f-1000", "d-20", "hoggar", "f-100",
    )):
        return CategoriaVeiculo.PICAPE
    if any(k in m for k in (
        "compass", "hr-v", "tracker", "creta", "haval", "evoque", "spin", "renegade",
        "pajero", "freelander", "750i", "activ", "longitude", "sport", "duster", "ecosport",
        # Lançamentos Fiat recentes: Pulse (SUV) e Fastback (SUV coupé)
        "pulse", "fastback",
        # Chineses + SUVs recentes
        "tiggo", "tucson", "cherokee", "commander", "territory", "santa fe", "trailblazer",
        "kicks", "captur", "t-cross", "corolla cross", "wr-v", "outlander", "sportage",
        # Premium SUVs
        "x1", "x3", "x5", "x6", "q3", "q5", "q7", "glk", "gla", "glb",
    )):
        return CategoriaVeiculo.SUV
    if any(k in m for k in (
        "onix", "hb20", "gol", "fiesta", "polo", "ka ", "march", "sandero", "uno", "mobi",
        "argo", "biz", "bizz", "kwid", "208", "c3", "up", "palio", "punto",
        "i30", "picanto", "soul", "jazz", "yaris", "bravo", "etios", "astra", "celta",
    )):
        return CategoriaVeiculo.HATCH
    if any(k in m for k in (
        "cruze", "corolla", "civic", "jetta", "voyage", "virtus", "logan", "prisma",
        "versa", "fluence", "focus sedan", "ka sedan", "j5", "j5i", "cobalt",
        "city", "cronos", "grand siena", "hb20s", "onix plus", "sentra", "linea",
        "megane", "vectra", "astra sedan", "lancer", "yaris sedan",
    )):
        return CategoriaVeiculo.SEDAN
    return CategoriaVeiculo.OUTRO


def calibrar_dias_giro(
    empresa_id: str,
    categoria: CategoriaVeiculo,
    session: Optional[Session],
    fallback: int,
) -> int:
    """Devolve dias_giro calibrado pra (empresa, categoria), ou `fallback` se sem dado.

    Args:
        empresa_id: empresa cujas vendas históricas serão usadas
        categoria: bucket de veículo
        session: SQLModel Session aberta (None → retorna direto fallback)
        fallback: prior hardcoded a usar quando não há amostras suficientes

    Returns:
        Inteiro positivo de dias.
    """
    if session is None:
        return fallback

    chave = (empresa_id, categoria)
    cached = _cache.get(chave)
    if cached and (datetime.utcnow() - cached[1]) < _CACHE_TTL:
        return cached[0]

    # Busca arrematados COM data de venda (vendido_em e data preenchidos)
    stmt = (
        select(Arrematado, Lote)
        .join(Lote, Lote.id == Arrematado.lote_id)
        .where(Arrematado.empresa_id == empresa_id)
        .where(Arrematado.vendido_em.is_not(None))  # type: ignore[union-attr]
    )
    rows = session.exec(stmt).all()

    # Filtra por categoria inferida do modelo
    dias_observados = []
    for arr, lote in rows:
        if _categoria_de_modelo(lote.modelo) != categoria:
            continue
        if arr.vendido_em is None or arr.data is None:
            continue
        delta = (arr.vendido_em - arr.data).days
        if delta > 0:
            dias_observados.append(delta)

    if len(dias_observados) < _MIN_AMOSTRAS_CALIBRACAO:
        # Cache o fallback também — evita re-query toda chamada quando
        # categoria está vazia
        _cache[chave] = (fallback, datetime.utcnow())
        return fallback

    media = int(round(sum(dias_observados) / len(dias_observados)))
    _cache[chave] = (media, datetime.utcnow())
    return media


def invalidar_cache() -> None:
    """Limpa o cache — útil em testes e quando importou novos arrematados."""
    _cache.clear()


def roi_anualizado(score_roi: float, dias_giro: Optional[int]) -> float:
    """Anualiza o ROI absoluto pelo tempo esperado de venda.

    Distinção semântica:
      - `dias_giro is None` → estimativa ausente. Usa fallback 90 dias (4x ao ano,
        anualização conservadora).
      - `dias_giro` numérico (até 0) → estimativa presente. Aplica floor de 30
        dias pra evitar que carros com previsão "absurdo baixo" inflem o ROI.
    """
    if score_roi is None:
        return 0.0
    if dias_giro is None:
        dias = 90
    else:
        dias = max(dias_giro, 30)
    return score_roi * (365.0 / dias)
