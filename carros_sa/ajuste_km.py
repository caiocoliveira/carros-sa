"""Ajuste da âncora de venda por km.

A mediana de preços Webmotors reflete um carro "típico" do (marca, modelo, ano)
— com km também típica. Se o NOSSO lote tem km muito acima/abaixo dessa mediana
de mercado, a âncora de venda precisa ser calibrada: carro com km alta vale
menos que a mediana; carro com km baixa vale mais.

`fator_km` retorna um multiplicador ∈ [0.75, 1.15] aplicado antes da dedução de
reforma/frete/taxas no precificador. Bounds evitam resultados absurdos para
outliers (ex.: lote com 5k km num mercado com mediana de 150k km).

Quando qualquer entrada é ausente (km do lote ou mediana do mercado), devolve
1.0 — no-op, sem penalizar o lote.
"""

from __future__ import annotations

from typing import Optional

# Sensibilidade: 30% do delta relativo vira ajuste de preço.
# Ex.: lote com km 50% acima da mediana → -15% no preço (antes do clamp).
# Calibrado para ficar dentro dos bounds [0.75, 1.15] na faixa realista
# (km ~0.5x a ~2x a mediana) sem saturar cedo demais.
_SENSIBILIDADE = 0.30

_FATOR_MIN = 0.75
_FATOR_MAX = 1.15


def fator_km(
    km_lote: Optional[int],
    km_mediana_mercado: Optional[int],
) -> float:
    """Multiplicador para calibrar a âncora de venda pela km do lote.

    > 1.0 quando o lote tem km abaixo da mediana de mercado (carro vale mais)
    < 1.0 quando o lote tem km acima da mediana (carro vale menos)
    == 1.0 quando há empate ou dados faltam
    """
    if not km_lote or not km_mediana_mercado or km_mediana_mercado <= 0:
        return 1.0
    delta_pct = (km_mediana_mercado - km_lote) / km_mediana_mercado
    fator = 1.0 + delta_pct * _SENSIBILIDADE
    return max(_FATOR_MIN, min(_FATOR_MAX, fator))
