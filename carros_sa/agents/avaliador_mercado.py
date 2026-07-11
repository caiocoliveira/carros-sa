"""AvaliadorMercado — combina FIPE + amostra de mercado (Webmotors live) em SinalMercado.

Fonte de mediana/p25 (workstream G, 2026-05-12):
  - **Webmotors live** via cache populado pelo CLI `carros-sa webmotors-coletar`
    (cron noturno, rate-limit ≥60s, TTL 24h). `session` ativa o lookup.
  - **Sem amostra fresh** → `webmotors_mediana = fipe` como placeholder neutro
    (não muda o cálculo — precificador é FIPE-only desde 2026-05-08), e
    `n_anuncios_competidores = 0` sinaliza ao display "sem sinal de mercado"
    (sheets/audit mostram "—").
  - **Similares do Auto Avaliar foram descontinuados** como fonte de mediana
    nesse fluxo: amostras frequentemente poluídas (Tiggo 7 entre Tiggo 2,
    Airtrek vs Outlander) e exigiam cap defensivo `FIPE×1.20` que mascarava
    o ruído. Workstream G pula AA aqui.

Cache:
  - FIPE in-memory + persistente em `modelo_fipe_cache` (quando `session` passada)
  - Webmotors em `anuncio_webmotors` (TTL 24h via `webmotors_cache`)
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import List, Optional

from sqlmodel import Session, select

from carros_sa.agents.calibracao_giro import calibrar_dias_giro, faixa_de_idade
from carros_sa.models import CategoriaVeiculo, ModeloFipeCache, SinalMercado
from carros_sa.tools.fipe import FipeClient
from carros_sa.tools.popularidade import ajustar_dias_giro, bucket_modelo
from carros_sa.tools.webmotors_cache import obter_anuncios_cacheados

# Heurística inicial de giro por (categoria, faixa_idade). Calibrada com
# histórico do operador (2026-04): carros novos popularizam mais rápido;
# carros velhos de nicho demoram muito. Valores aproximados — sobrescritos
# por dados reais quando Arrematado tem ≥3 amostras pra (categoria, faixa).
_DIAS_GIRO_DEFAULT_POR_FAIXA = {
    # (categoria, faixa_idade) → dias prior
    # NOVO (≤3 anos): 1a mão, garantia, pool grande
    # MEDIO (4-7 anos): rodado mas moderno
    # VELHO (8+ anos): pool restrito, manutenção cara
    CategoriaVeiculo.HATCH:      {"novo": 25, "medio": 50, "velho": 100},
    CategoriaVeiculo.SEDAN:      {"novo": 30, "medio": 55, "velho": 110},
    CategoriaVeiculo.SUV:        {"novo": 35, "medio": 60, "velho": 95},
    CategoriaVeiculo.PICAPE:     {"novo": 35, "medio": 50, "velho": 85},
    CategoriaVeiculo.UTILITARIO: {"novo": 45, "medio": 65, "velho": 120},
    CategoriaVeiculo.OUTRO:      {"novo": 40, "medio": 60, "velho": 100},
}

# Compatibilidade: fallback pra quando não temos `ano` (legado).
_DIAS_GIRO_DEFAULT = {
    cat: faixas["medio"] for cat, faixas in _DIAS_GIRO_DEFAULT_POR_FAIXA.items()
}


def _prior_dias_giro(categoria: CategoriaVeiculo, faixa) -> int:
    """Look-up do prior hardcoded por (categoria, faixa)."""
    faixa_str = faixa.value if hasattr(faixa, "value") else str(faixa)
    faixas = _DIAS_GIRO_DEFAULT_POR_FAIXA.get(categoria, _DIAS_GIRO_DEFAULT_POR_FAIXA[CategoriaVeiculo.OUTRO])
    return faixas.get(faixa_str, faixas["medio"])

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
    categoria: CategoriaVeiculo = CategoriaVeiculo.OUTRO,
    fipe_client: Optional[FipeClient] = None,
    session: Optional[Session] = None,
    empresa_id: Optional[str] = None,
    aplicar_popularidade: bool = True,
    webmotors_km_mediana: Optional[int] = None,
    webmotors_anuncios: Optional[List["AnuncioWM"]] = None,  # type: ignore[name-defined]  # noqa: F821
) -> SinalMercado:
    """Devolve SinalMercado para um (marca, modelo, ano).

    Mediana/p25 vêm do **Webmotors** (workstream G). Em produção,
    `webmotors_anuncios` é None e o cache em `anuncio_webmotors` é
    consultado via `session` (TTL 24h, populado pelo cron noturno
    `carros-sa webmotors-coletar`). Testes podem injetar
    `webmotors_anuncios=[AnuncioWM(...), ...]` pra bypassar o DB.

    Sem amostra fresh: `webmotors_mediana = fipe` (placeholder neutro — não
    afeta cálculo do precificador, que é FIPE-only desde 2026-05-08), e
    `n_anuncios_competidores = 0` faz o display em sheets/audit mostrar "—"
    pra evitar passar a impressão de "sinal real" quando o cache não cobre.

    Quando `empresa_id` e `session` são passados, `dias_giro_estimado` é
    calibrado a partir do histórico real (Arrematado da empresa) — fallback
    pro prior categórico hardcoded quando há <3 amostras na categoria.
    """
    fipe = fipe_client or FipeClient()
    fipe_valor = _consultar_fipe_com_cache(fipe, marca, modelo, ano, session)

    # Lookup do cache Webmotors (workstream G). Testes injetam lista direta
    # via `webmotors_anuncios=`. Em produção, `session` ativa o cache do DB.
    anuncios = webmotors_anuncios
    if anuncios is None and session is not None:
        anuncios = obter_anuncios_cacheados(session, marca, modelo, ano)

    precos = sorted(a.preco for a in (anuncios or []) if a.preco > 0)
    if precos:
        mediana = int(statistics.median(precos))
        p25 = _percentil_25(precos)
        n = len(precos)
        # km_mediana derivado da própria amostra Webmotors quando disponível —
        # mais preciso que `webmotors_km_mediana` injetado externamente. Caller
        # pode override passando explicitamente.
        if webmotors_km_mediana is None:
            kms = [a.km for a in (anuncios or []) if a.km and a.km > 0]
            if kms:
                webmotors_km_mediana = int(statistics.median(kms))
    else:
        # Sem amostra: FIPE como placeholder neutro pra preservar contrato
        # (`SinalMercado.webmotors_mediana: int` rejeita ≤0). p25 sentinela
        # conservador. Display em sheets/audit suprime quando `n=0`.
        mediana = fipe_valor
        p25 = int(round(fipe_valor * 0.88))
        n = 0

    # Prior já discrimina por faixa_idade — Polo 2024 NOVO (25d) ≠ Polo 2018 MEDIO
    # (50d) mesmo quando cai no fallback hardcoded. Calibração com Arrematado
    # sobrescreve quando há ≥3 amostras pra (categoria, faixa).
    faixa = faixa_de_idade(ano)
    prior = _prior_dias_giro(categoria, faixa)

    if empresa_id and session is not None:
        dias_giro = calibrar_dias_giro(
            empresa_id, categoria, session, fallback=prior, faixa_idade=faixa,
        )
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
        bucket = bucket_modelo(marca, modelo, categoria, ano=ano)
        # Passa faixa_idade pra correção granular — picape velha blockbuster
        # não acelera tanto quanto picape nova blockbuster.
        dias_giro = ajustar_dias_giro(dias_giro, bucket, faixa_idade=faixa)

    return SinalMercado(
        fipe=fipe_valor,
        webmotors_mediana=mediana,
        webmotors_p25=p25,
        n_anuncios_competidores=n,
        dias_giro_estimado=dias_giro,
        webmotors_km_mediana=webmotors_km_mediana,
    )
