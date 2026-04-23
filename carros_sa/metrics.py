"""Métricas puras de ROI + classificação de veículo — zero dependência de agentes.

Antes: cli.py, orquestrador.py, audit.py, sheets.py importavam
`_categoria_de_modelo` (prefixo privado!) + `roi_anualizado` +
`lucro_reais_por_mes` do módulo `agents/calibracao_giro`. CLI de ranking
dependendo de módulo de agente é acoplamento cruzado — qualquer mudança na
calibração (que é comportamento de negócio) arriscava quebrar o CLI (que é
só apresentação).

Agora: funções puras aqui, sem estado, sem imports de `agents/`. Quem calibra
`dias_giro` vira consumer dessas métricas, não fonte delas.

`calibracao_giro.py` re-exporta as funções como aliases pra preservar
compatibilidade com testes existentes e callers antigos.
"""

from __future__ import annotations

from carros_sa.models import CategoriaVeiculo


def categoria_de_modelo(modelo: str) -> CategoriaVeiculo:
    """Inferência grosseira de categoria por substring no nome do modelo.

    Mantém alinhado com o `_calcular_frete` do orquestrador pra que histórico
    e novos lotes caiam no mesmo bucket. Lista expandida após rodagens reais
    — inclui marcas chinesas (Tiggo/Haval), lançamentos Fiat (Fastback/Pulse/
    Cronos), SUVs compactos e picapes menos comuns (Oroch/Maverick/Triton).
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
        "pulse", "fastback",
        "tiggo", "tucson", "cherokee", "commander", "territory", "santa fe", "trailblazer",
        "kicks", "captur", "t-cross", "corolla cross", "wr-v", "outlander", "sportage",
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


def lucro_reais_por_mes(
    lucro_absoluto_reais: int,
    dias_giro: int | None,
) -> int:
    """Converte lucro esperado (R$) em R$/mês, respeitando o tempo de venda.

    Útil como métrica intuitiva pro operador: "esse lote rende R$X/mês
    enquanto tá no pátio". Permite comparar lotes com capitais e prazos
    muito diferentes na mesma unidade.

    Floor de 30 dias (mesma lógica do roi_anualizado) evita números absurdos.
    """
    if lucro_absoluto_reais <= 0:
        return 0
    dias = 90 if dias_giro is None else max(dias_giro, 30)
    return int(round(lucro_absoluto_reais * 30.0 / dias))


def roi_anualizado(score_roi: float, dias_giro: int | None) -> float:
    """Anualiza o ROI absoluto pelo tempo esperado de venda.

    Semântica:
      - `dias_giro is None` → estimativa ausente. Fallback 90 dias (4x/ano,
        anualização conservadora).
      - `dias_giro` numérico (até 0) → aplica floor de 30 dias pra evitar que
        carros com previsão "absurdo baixo" inflem o ROI.
    """
    if score_roi is None:
        return 0.0
    if dias_giro is None:
        dias = 90
    else:
        dias = max(dias_giro, 30)
    return score_roi * 365.0 / dias
