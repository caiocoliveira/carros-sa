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
from enum import Enum

from sqlmodel import Session, select

from carros_sa.metrics import (
    categoria_de_modelo as _categoria_de_modelo_impl,
    lucro_reais_por_mes as _lucro_reais_por_mes_impl,
    roi_anualizado as _roi_anualizado_impl,
)
from carros_sa.models import Arrematado, CategoriaVeiculo, Lote

# Re-exports de `carros_sa.metrics` pra backward compat — callers antigos
# faziam `from carros_sa.agents.calibracao_giro import _categoria_de_modelo,
# roi_anualizado, lucro_reais_por_mes`. A fonte da verdade agora é `metrics.py`.
_categoria_de_modelo = _categoria_de_modelo_impl
lucro_reais_por_mes = _lucro_reais_por_mes_impl
roi_anualizado = _roi_anualizado_impl

# TTL do cache — 1h é mais que suficiente pra batch run; calibração nova fica
# disponível na próxima invocação humana.
_CACHE_TTL = timedelta(hours=1)
_MIN_AMOSTRAS_CALIBRACAO = 3

class FaixaIdade(str, Enum):
    """Sub-bucket de idade do veículo pra calibração de giro.

    Feedback real do operador (2026-04-17): Polo Track 2024 (popular NOVO, 227d
    real) e Onix Joy 2018 (popular VELHO, 278d real) têm demandas muito
    diferentes mas caíam na mesma calibração categórica. Granularidade por
    idade aproxima a realidade.

    Thresholds escolhidos:
      - NOVO: ≤3 anos — carro de 1ª mão/garantia de fábrica, público premium
      - MEDIO: 4-7 anos — uso rodado mas ainda moderno, maior pool de compradores
      - VELHO: 8+ anos — pool restrito, questões de manutenção/peças
    """

    NOVO = "novo"
    MEDIO = "medio"
    VELHO = "velho"

def faixa_de_idade(ano_veiculo: int, ano_referencia: int = 2026) -> FaixaIdade:
    idade = max(ano_referencia - ano_veiculo, 0)
    if idade <= 3:
        return FaixaIdade.NOVO
    if idade <= 7:
        return FaixaIdade.MEDIO
    return FaixaIdade.VELHO

# Cache: (empresa_id, categoria, faixa_idade_or_None) -> (dias, calculado_em).
# None no 3º elemento = calibração agregada (sem sub-bucket), usada como fallback
# quando a faixa específica não tem ≥3 amostras.
_cache: dict[
    tuple[str, CategoriaVeiculo, FaixaIdade | None],
    tuple[int, datetime],
] = {}

def _calibrar_nivel(
    empresa_id: str,
    categoria: CategoriaVeiculo,
    session: Session,
    faixa: FaixaIdade | None,
    ano_ref: int,
) -> int | None:
    """Tenta calcular média de dias_giro pra (empresa, cat[, faixa]).

    Retorna inteiro positivo se tem ≥`_MIN_AMOSTRAS_CALIBRACAO` amostras,
    ou `None` se não tem dados suficientes. Cacheado.
    """
    chave = (empresa_id, categoria, faixa)
    cached = _cache.get(chave)
    if cached and (datetime.utcnow() - cached[1]) < _CACHE_TTL:
        # Sentinela -1 marca "já sabido que não tem amostras" (evita re-query)
        return cached[0] if cached[0] >= 0 else None

    stmt = (
        select(Arrematado, Lote)
        .join(Lote, Lote.id == Arrematado.lote_id)
        .where(Arrematado.empresa_id == empresa_id)
        .where(Arrematado.vendido_em.is_not(None))  # type: ignore[union-attr]
    )
    rows = session.exec(stmt).all()

    dias: list = []
    for arr, lote in rows:
        if _categoria_de_modelo(lote.modelo) != categoria:
            continue
        if arr.vendido_em is None or arr.data is None:
            continue
        if faixa is not None and faixa_de_idade(lote.ano, ano_ref) != faixa:
            continue
        delta = (arr.vendido_em - arr.data).days
        if delta > 0:
            dias.append(delta)

    if len(dias) < _MIN_AMOSTRAS_CALIBRACAO:
        _cache[chave] = (-1, datetime.utcnow())  # sentinela "sem dados"
        return None

    media = int(round(sum(dias) / len(dias)))
    _cache[chave] = (media, datetime.utcnow())
    return media

def calibrar_dias_giro(
    empresa_id: str,
    categoria: CategoriaVeiculo,
    session: Session | None,
    fallback: int,
    faixa_idade: FaixaIdade | None = None,
    ano_referencia: int = 2026,
) -> int:
    """Devolve dias_giro calibrado pra (empresa, categoria[, faixa_idade]).

    Estratégia em 3 níveis, cai pro próximo quando <3 amostras:
      1. (categoria, faixa_idade) — granular, só ativa se faixa_idade passado
      2. (categoria) agregado — comportamento legado
      3. `fallback` hardcoded (prior categórico)

    Args:
        faixa_idade: sub-bucket opcional (NOVO/MEDIO/VELHO). Quando passado,
            tenta subcategoria primeiro; cai pra agregado se insuficiente.
        ano_referencia: ano "agora" pra derivar idade dos lotes históricos.
    """
    if session is None:
        return fallback

    if faixa_idade is not None:
        por_faixa = _calibrar_nivel(empresa_id, categoria, session, faixa_idade, ano_referencia)
        if por_faixa is not None:
            return por_faixa

    agregada = _calibrar_nivel(empresa_id, categoria, session, None, ano_referencia)
    if agregada is not None:
        return agregada

    return fallback


def invalidar_cache() -> None:
    """Limpa o cache — útil em testes e quando importou novos arrematados."""
    _cache.clear()
