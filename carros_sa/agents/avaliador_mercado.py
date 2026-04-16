"""AvaliadorMercado — combina FIPE + similares Auto Avaliar em SinalMercado.

Webmotors (workstream B) ainda não existe; enquanto isso usamos os preços de
similares que a própria plataforma Auto Avaliar mostra na página de detalhe
(`DetalheFlags.similares_precos`). Quando B chegar, troca-se a fonte de
mediana/p25 sem mexer no contrato.

Cache:
  - In-memory por instância do FipeClient (auto)
  - Persistente em `modelo_fipe_cache` quando uma `Session` é passada
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import List, Optional

from sqlmodel import Session, select

from carros_sa.models import CategoriaVeiculo, ModeloFipeCache, SinalMercado
from carros_sa.tools.fipe import FipeClient

# Heurística inicial de giro por categoria. Calibrar com workstream H quando
# tivermos dados de `arrematado.vendido_em - data`.
_DIAS_GIRO_DEFAULT = {
    CategoriaVeiculo.HATCH: 25,
    CategoriaVeiculo.SEDAN: 30,
    CategoriaVeiculo.SUV: 35,
    CategoriaVeiculo.UTILITARIO: 45,
    CategoriaVeiculo.PICAPE: 35,
    CategoriaVeiculo.OUTRO: 40,
}

# TTL do cache persistente FIPE — Parallelum atualiza tabela mensalmente.
_FIPE_CACHE_TTL = timedelta(days=20)


def _consultar_fipe_com_cache(
    fipe: FipeClient,
    marca: str,
    modelo: str,
    ano: int,
    session: Optional[Session],
) -> int:
    if session is None:
        return fipe.consultar(marca, modelo, ano)

    stmt = select(ModeloFipeCache).where(
        ModeloFipeCache.marca == marca,
        ModeloFipeCache.modelo == modelo,
        ModeloFipeCache.ano == ano,
    ).order_by(ModeloFipeCache.consultado_em.desc())
    hit = session.exec(stmt).first()
    if hit and (datetime.utcnow() - hit.consultado_em) < _FIPE_CACHE_TTL:
        return hit.valor

    valor = fipe.consultar(marca, modelo, ano)
    session.add(
        ModeloFipeCache(marca=marca, modelo=modelo, ano=ano, valor=valor)
    )
    session.commit()
    return valor


def _percentil_25(valores: List[int]) -> int:
    """p25 robusto pra amostras pequenas. n=1 → o próprio valor."""
    if not valores:
        raise ValueError("lista vazia")
    if len(valores) == 1:
        return valores[0]
    s = sorted(valores)
    # método linear simples: índice = 0.25 * (n-1)
    idx = 0.25 * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    frac = idx - lo
    return int(round(s[lo] + (s[hi] - s[lo]) * frac))


def avaliar(
    marca: str,
    modelo: str,
    ano: int,
    km: Optional[int] = None,
    similares_precos: Optional[List[int]] = None,
    categoria: CategoriaVeiculo = CategoriaVeiculo.OUTRO,
    fipe_client: Optional[FipeClient] = None,
    session: Optional[Session] = None,
    empresa_id: Optional[str] = None,
    aplicar_popularidade: bool = True,
) -> SinalMercado:
    """Devolve SinalMercado para um (marca, modelo, ano).

    `similares_precos` é a lista de preços que a página de detalhe de Auto
    Avaliar mostra na seção 'Talvez se interesse por'. Quando vazio, caímos
    na heurística FIPE × 0.9 (mediana) / 0.78 (p25).

    Quando `empresa_id` e `session` são passados, `dias_giro_estimado` é
    calibrado a partir do histórico real (Arrematado da empresa) — fallback
    pro prior categórico hardcoded quando há <3 amostras na categoria.
    """
    fipe = fipe_client or FipeClient()
    fipe_valor = _consultar_fipe_com_cache(fipe, marca, modelo, ano, session)

    sim = [p for p in (similares_precos or []) if p > 0]
    if sim:
        mediana = int(round(statistics.median(sim)))
        p25 = _percentil_25(sim)
        n = len(sim)
    else:
        # sem dados de competidores: usa FIPE como referência de revenda.
        # O usuário confirmou que vende próximo da FIPE — então mediana≈97%
        # (margem de negociação de ~3%). p25≈88% é conservador pra ranking.
        # Webmotors (workstream B) substituirá esses fallbacks por dados reais.
        mediana = int(round(fipe_valor * 0.97))
        p25 = int(round(fipe_valor * 0.88))
        n = 0

    # Prior categórico — base do dias_giro
    prior = _DIAS_GIRO_DEFAULT.get(categoria, 40)

    # Calibração com Arrematado (Bloco C) — só ativa quando empresa+session presentes
    if empresa_id and session is not None:
        from carros_sa.agents.calibracao_giro import calibrar_dias_giro
        dias_giro = calibrar_dias_giro(empresa_id, categoria, session, fallback=prior)
    else:
        dias_giro = prior

    # ajuste leve por liquidez observada: muitos competidores → mercado mais
    # líquido, gira mais rápido. Pouquíssimos → ilíquido.
    if n >= 6:
        dias_giro = max(15, dias_giro - 5)
    elif 0 < n <= 2:
        dias_giro += 10

    # Ajuste relativo via popularidade FENABRAVE (top emplacamentos da categoria).
    # Modelo blockbuster (top-5) gira ~40% mais rápido; modelo nicho ~30% mais lento.
    # Acionado on-demand pra todo cálculo de mercado, mas só faz sentido pra
    # categorias que aparecem no ranking (excluí UTILITARIO genérico, etc.).
    if aplicar_popularidade:
        from carros_sa.tools.popularidade import ajustar_dias_giro, bucket_modelo
        bucket = bucket_modelo(marca, modelo, categoria, ano=ano)
        dias_giro = ajustar_dias_giro(dias_giro, bucket)

    return SinalMercado(
        fipe=fipe_valor,
        webmotors_mediana=mediana,
        webmotors_p25=p25,
        n_anuncios_competidores=n,
        dias_giro_estimado=dias_giro,
    )
