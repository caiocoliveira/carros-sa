"""AvaliadorMercado — combina FIPE (âncora) + similares da plataforma Auto Avaliar
para produzir um SinalMercado.

Webmotors fica pra workstream B; até lá, usamos os similares já visíveis na
página de detalhe (`DetalheFlags.similares_precos`) como proxy de "preço de
giro real". Quando o B chegar, troca-se o input sem mexer no contrato.

Saída global (mesmo modelo/ano serve N empresas).
"""

from __future__ import annotations

from statistics import median
from typing import List, Optional

from carros_sa.models import SinalMercado
from carros_sa.tools.fipe import FipeClient


def avaliar(
    marca: str,
    modelo: str,
    ano: int,
    similares_precos: Optional[List[int]] = None,
    fipe_client: Optional[FipeClient] = None,
) -> SinalMercado:
    """Avalia o mercado pra (marca, modelo, ano).

    similares_precos: preços R$ vistos na seção 'Talvez se interesse por' do
        próprio Auto Avaliar (parser já expõe). Filtra ruído (zeros, valores
        absurdos < R$ 3k que costumam ser parcela mensal).

    Erros: levanta ValueError se não há FIPE nem similares. Sem nada, qualquer
        número seria chute — melhor falhar e o orquestrador pular o lote.
    """
    similares = sorted(_filtrar(similares_precos or []))
    fipe_client = fipe_client or FipeClient()
    fipe = fipe_client.consultar(marca, modelo, ano)

    if similares:
        med = int(median(similares))
        p25 = _percentil(similares, 25)
        n = len(similares)
    else:
        if fipe is None:
            raise ValueError(
                f"Sem FIPE nem similares para {marca} {modelo} {ano}; não dá pra avaliar."
            )
        # Sem competidores visíveis: usa FIPE como mediana e aplica desconto
        # padrão de 15% pro p25 (heurística inicial; calibrar depois).
        med = fipe
        p25 = max(1, int(fipe * 0.85))
        n = 0

    if fipe is None:
        # Sem FIPE mas com similares: âncora vira a mediana (já é preço de
        # mercado real). Marca como aproximado pelo dias_giro/confidence.
        fipe = med

    return SinalMercado(
        fipe=fipe,
        webmotors_mediana=med,
        webmotors_p25=p25,
        n_anuncios_competidores=n,
        dias_giro_estimado=_estimar_dias_giro(n),
    )


# =============================================================================
# Helpers
# =============================================================================

# Filtra valores que claramente não são preço de carro (ex.: parcela mensal R$ 599,00)
_PRECO_MIN = 3_000


def _filtrar(precos: List[int]) -> List[int]:
    return [p for p in precos if p >= _PRECO_MIN]


def _percentil(sorted_vals: List[int], p: float) -> int:
    """Percentil p (0..100) por interpolação linear. sorted_vals deve estar ordenado."""
    n = len(sorted_vals)
    if n == 0:
        return 0
    if n == 1:
        return sorted_vals[0]
    k = (p / 100.0) * (n - 1)
    f = int(k)
    c = min(f + 1, n - 1)
    return int(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def _estimar_dias_giro(n_competidores: int) -> int:
    """Heurística inicial. Calibrar com tracking longitudinal (workstream G).

    Mais anúncios competindo = giro mais rápido (preço pressionado a fechar).
    Mas zero anúncios também = mercado seco, demora a casar comprador.
    """
    if n_competidores == 0:
        return 60
    if n_competidores < 5:
        return 45
    if n_competidores < 15:
        return 35
    return 30
