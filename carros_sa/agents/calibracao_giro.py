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

# Corte de idade pro sub-bucket (novo ≤ N anos, velho > N). Calibrado no
# histórico real de Uberlândia onde hatch ≤3 anos gira muito diferente de >3.
_CORTE_IDADE_ANOS = 3

# Faixas de idade + sentinela "any" pro fallback
_FAIXA_NOVO = "novo"
_FAIXA_VELHO = "velho"
_FAIXA_ANY = "any"


# (empresa_id, categoria, faixa_idade) -> (dias_giro_calibrado, calculado_em)
_cache: Dict[Tuple[str, CategoriaVeiculo, str], Tuple[int, datetime]] = {}


def _faixa_idade(ano_veiculo: Optional[int], ano_referencia: Optional[int] = None) -> str:
    """Retorna 'novo' se ≤ _CORTE_IDADE_ANOS, 'velho' se mais, 'any' se desconhecido."""
    if ano_veiculo is None:
        return _FAIXA_ANY
    ref = ano_referencia if ano_referencia is not None else datetime.now().year
    return _FAIXA_NOVO if (ref - ano_veiculo) <= _CORTE_IDADE_ANOS else _FAIXA_VELHO


def _categoria_de_modelo(modelo: str) -> CategoriaVeiculo:
    """Inferência grosseira por substring no nome do modelo.

    Mantém alinhado com [orquestrador._calcular_frete](../orquestrador.py)
    pra que histórico e novos lotes caiam na mesma bucket.
    """
    m = (modelo or "").lower()
    if any(k in m for k in ("hilux", "s10", "saveiro", "strada", "ranger", "frontier", "amarok", "toro")):
        return CategoriaVeiculo.PICAPE
    if any(k in m for k in (
        "compass", "hr-v", "tracker", "creta", "haval", "evoque", "spin", "renegade",
        "pajero", "freelander", "750i", "activ", "longitude", "sport", "duster", "ecosport",
    )):
        return CategoriaVeiculo.SUV
    if any(k in m for k in (
        "onix", "hb20", "gol", "fiesta", "polo", "ka ", "march", "sandero", "uno", "mobi",
        "argo", "biz", "bizz",
    )):
        return CategoriaVeiculo.HATCH
    if any(k in m for k in (
        "cruze", "corolla", "civic", "jetta", "voyage", "virtus", "logan", "prisma",
        "versa", "fluence", "focus sedan", "ka sedan", "j5", "j5i", "cobalt",
    )):
        return CategoriaVeiculo.SEDAN
    return CategoriaVeiculo.OUTRO


def calibrar_dias_giro(
    empresa_id: str,
    categoria: CategoriaVeiculo,
    session: Optional[Session],
    fallback: int,
    ano: Optional[int] = None,
    ano_referencia: Optional[int] = None,
) -> int:
    """Devolve dias_giro calibrado pra (empresa, categoria[, faixa_idade]), ou `fallback`.

    Cascade de fallback:
      1. (categoria, faixa_idade) com ≥3 vendas → média por sub-bucket
      2. categoria inteira com ≥3 vendas → média por categoria (comportamento antigo)
      3. fallback hardcoded

    Args:
        empresa_id: empresa cujas vendas históricas serão usadas
        categoria: bucket de veículo
        session: SQLModel Session aberta (None → retorna direto fallback)
        fallback: prior hardcoded a usar quando não há amostras suficientes
        ano: ano do veículo sendo avaliado. Se None, calibração agrega toda
             a categoria (comportamento legado antes do Bloco C.1).
        ano_referencia: ano corrente (injetável pra testes determinísticos).
             None = `datetime.now().year`.

    Returns:
        Inteiro positivo de dias.
    """
    if session is None:
        return fallback

    faixa_alvo = _faixa_idade(ano, ano_referencia)
    chave = (empresa_id, categoria, faixa_alvo)
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

    # Agrupa em dois níveis: (categoria, faixa) e (categoria) pra cascade
    dias_categoria = []
    dias_sub_bucket = []
    for arr, lote in rows:
        if _categoria_de_modelo(lote.modelo) != categoria:
            continue
        if arr.vendido_em is None or arr.data is None:
            continue
        delta = (arr.vendido_em - arr.data).days
        if delta <= 0:
            continue
        dias_categoria.append(delta)
        if faixa_alvo != _FAIXA_ANY and _faixa_idade(lote.ano, ano_referencia) == faixa_alvo:
            dias_sub_bucket.append(delta)

    # Nível 1: sub-bucket com amostras suficientes
    if faixa_alvo != _FAIXA_ANY and len(dias_sub_bucket) >= _MIN_AMOSTRAS_CALIBRACAO:
        media = int(round(sum(dias_sub_bucket) / len(dias_sub_bucket)))
        _cache[chave] = (media, datetime.utcnow())
        return media

    # Nível 2: categoria inteira
    if len(dias_categoria) >= _MIN_AMOSTRAS_CALIBRACAO:
        media = int(round(sum(dias_categoria) / len(dias_categoria)))
        _cache[chave] = (media, datetime.utcnow())
        return media

    # Nível 3: fallback hardcoded
    _cache[chave] = (fallback, datetime.utcnow())
    return fallback


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
