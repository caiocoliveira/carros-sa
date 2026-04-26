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

from carros_sa.models import CategoriaVeiculo, ModeloFipeCache, PrecoReferenciaAA, SinalMercado
from carros_sa.tools.fipe import FipeClient

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
    similares_precos: Optional[List[int]] = None,
    categoria: CategoriaVeiculo = CategoriaVeiculo.OUTRO,
    fipe_client: Optional[FipeClient] = None,
    session: Optional[Session] = None,
    empresa_id: Optional[str] = None,
    aplicar_popularidade: bool = True,
    webmotors_km_mediana: Optional[int] = None,
    auto_avaliar_ref: Optional[int] = None,
) -> SinalMercado:
    """Devolve SinalMercado para um (marca, modelo, ano).

    `similares_precos` é a lista de preços que a página de detalhe de Auto
    Avaliar mostra na seção 'Talvez se interesse por'. Quando vazio, caímos
    na heurística FIPE × 0.9 (mediana) / 0.78 (p25).

    Quando `empresa_id` e `session` são passados, `dias_giro_estimado` é
    calibrado a partir do histórico real (Arrematado da empresa) — fallback
    pro prior categórico hardcoded quando há <3 amostras na categoria.

    `auto_avaliar_ref` é o preço-referência da Tabela Auto Avaliar (campo
    "ULTIMA AVALIAÇÃO" embutido no anúncio). Quando não passado mas há
    `session`, faz lookup no `PrecoReferenciaAA` (histórico de outros lotes
    do mesmo modelo+ano) — atualiza o sinal mesmo pra lotes que não trazem
    o dado embutido. Cap de 30 dias evita usar referência velha demais.
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

    # Prior já discrimina por faixa_idade — Polo 2024 NOVO (25d) ≠ Polo 2018 MEDIO
    # (50d) mesmo quando cai no fallback hardcoded. Calibração com Arrematado
    # sobrescreve quando há ≥3 amostras pra (categoria, faixa).
    from carros_sa.agents.calibracao_giro import calibrar_dias_giro, faixa_de_idade
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
        from carros_sa.tools.popularidade import ajustar_dias_giro, bucket_modelo
        bucket = bucket_modelo(marca, modelo, categoria, ano=ano)
        # Passa faixa_idade pra correção granular — picape velha blockbuster
        # não acelera tanto quanto picape nova blockbuster.
        dias_giro = ajustar_dias_giro(dias_giro, bucket, faixa_idade=faixa)

    aa_ref = auto_avaliar_ref
    if aa_ref is None and session is not None:
        aa_ref = _buscar_preco_referencia_aa(marca, modelo, ano, session)

    return SinalMercado(
        fipe=fipe_valor,
        webmotors_mediana=mediana,
        webmotors_p25=p25,
        n_anuncios_competidores=n,
        dias_giro_estimado=dias_giro,
        webmotors_km_mediana=webmotors_km_mediana,
        auto_avaliar_ref=aa_ref,
    )


# Janela do histórico de PrecoReferenciaAA. Auto Avaliar reprecifica modelos
# com frequência — referência de >30 dias atrás é menos confiável que o cálculo
# baseado em FIPE+similares. Conservador no recente, ignora antigo.
_AA_REF_HISTORICO_TTL = timedelta(days=30)


def _buscar_preco_referencia_aa(
    marca: str, modelo: str, ano: int, session: Session,
) -> Optional[int]:
    """Lookup mais recente em PrecoReferenciaAA pra (marca, modelo, ano).

    Match exato por marca+modelo+ano. Modelos parecidos com versão diferente
    ("Polo Track" vs "Polo Highline") NÃO casam — preço-referência depende
    bastante de versão/trim e usar o vizinho errado polui mais que ajuda.
    """
    stmt = (
        select(PrecoReferenciaAA)
        .where(PrecoReferenciaAA.marca == marca)
        .where(PrecoReferenciaAA.modelo == modelo)
        .where(PrecoReferenciaAA.ano == ano)
        .order_by(PrecoReferenciaAA.coletado_em.desc())
    )
    hit = session.exec(stmt).first()
    if hit is None:
        return None
    if (datetime.utcnow() - hit.coletado_em) > _AA_REF_HISTORICO_TTL:
        return None
    return hit.preco
