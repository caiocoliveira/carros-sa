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
from typing import Dict, Optional, Tuple

from sqlmodel import Session, select

from carros_sa.models import Arrematado, CategoriaVeiculo, Lote


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


def faixa_de_idade(ano_veiculo: int, ano_referencia: Optional[int] = None) -> FaixaIdade:
    # Default lazy = ano atual. Hardcoded 2026 silenciosamente miscalibrava
    # carros 2023 em jan/2027 (idade real 4 → MEDIO; com hardcode idade 3 → NOVO),
    # afetando calibração via Arrematado + prior de dias_giro. Resolvendo em runtime
    # tira o bug latente.
    if ano_referencia is None:
        ano_referencia = datetime.now().year
    idade = max(ano_referencia - ano_veiculo, 0)
    if idade <= 3:
        return FaixaIdade.NOVO
    if idade <= 7:
        return FaixaIdade.MEDIO
    return FaixaIdade.VELHO


# Cache: (empresa_id, categoria, faixa_idade_or_None) -> (dias, calculado_em).
# None no 3º elemento = calibração agregada (sem sub-bucket), usada como fallback
# quando a faixa específica não tem ≥3 amostras.
_cache: Dict[
    Tuple[str, CategoriaVeiculo, Optional[FaixaIdade]],
    Tuple[int, datetime],
] = {}


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


def _calibrar_nivel(
    empresa_id: str,
    categoria: CategoriaVeiculo,
    session: Session,
    faixa: Optional[FaixaIdade],
    ano_ref: int,
) -> Optional[int]:
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
    session: Optional[Session],
    fallback: int,
    faixa_idade: Optional[FaixaIdade] = None,
    ano_referencia: Optional[int] = None,
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

    if ano_referencia is None:
        ano_referencia = datetime.now().year

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


# Floor mínimo realista de giro (em dias) — operação real do Reinaldo (21 carros)
# teve média 92d entre compra e venda; Polo Track real do Caio levou 227d; Onix
# Joy 278d. O prior categórico hardcoded em `_DIAS_GIRO_DEFAULT_POR_FAIXA` chega
# a 25d (HATCH NOVO) — claramente otimista. Sem floor maior, `lucro_reais_por_mes`
# vira `lucro_abs` (dias=30 → ×30/30=×1) e ROI anualizado infla pra 500-600%
# (irreal: benchmark real é 60-75% ao ano).
#
# 60d é compromisso conservador: dobra o pior caso default (HATCH NOVO 25d) sem
# zerar o sinal de lotes onde a calibração via Arrematado real já deu giro <60d.
_FLOOR_DIAS_GIRO_DISPLAY = 60


def lucro_reais_por_mes(
    lucro_absoluto_reais: int,
    dias_giro: Optional[int],
) -> int:
    """Converte lucro esperado (R$) em R$/mês, respeitando o tempo de venda.

    Útil como métrica intuitiva pro operador: "esse lote rende R$X/mês
    enquanto tá no pátio". Permite comparar lotes com capitais e prazos
    muito diferentes na mesma unidade.

    Floor de 60 dias evita o caso degenerado em que dias_giro<=30 (defaults
    otimistas como HATCH NOVO=25d) faz `lucro_mes = lucro_abs × 30/30 = lucro_abs`
    — operador via "Lucro/mês = Lucro total" e achava que recebia esse valor
    todo mês. Com floor 60d o pior caso vira `lucro_abs/2` (mais honesto).
    """
    if lucro_absoluto_reais <= 0:
        return 0
    dias = 90 if dias_giro is None else max(dias_giro, _FLOOR_DIAS_GIRO_DISPLAY)
    # (lucro / dias) * 30 = lucro mensal esperado
    return int(round(lucro_absoluto_reais * 30.0 / dias))


def roi_anualizado(score_roi: float, dias_giro: Optional[int]) -> float:
    """Anualiza o ROI absoluto pelo tempo esperado de venda.

    Distinção semântica:
      - `dias_giro is None` → estimativa ausente. Usa fallback 90 dias (4x ao ano,
        anualização conservadora).
      - `dias_giro` numérico → estimativa presente. Aplica floor de 60 dias pra
        evitar inflação sintética via dias_giro otimista (defaults categóricos
        chegam a 25-30d, ROI saturava em 500-600% — irreal).
    """
    if score_roi is None:
        return 0.0
    if dias_giro is None:
        dias = 90
    else:
        dias = max(dias_giro, _FLOOR_DIAS_GIRO_DISPLAY)
    return score_roi * (365.0 / dias)
