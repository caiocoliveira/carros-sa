"""Popularidade de modelo via ranking de emplacamentos (FENABRAVE).

Usa um YAML curado mensal (`config/mercado/fenabrave_ranking_<YYYY-MM>.yaml`)
que lista os top-30 modelos por categoria. A posição no ranking vira um
**bucket relativo** (BLOCKBUSTER → ILIQUIDO) que multiplica o `dias_giro_estimado`.

Filosofia:
  - O dado absoluto ("Polo emplaca 5k/mês") não diz nada útil. O dado relativo
    ("Polo é o 4º hatch mais vendido = top 15%") dá um sinal calibrado.
  - Acionado on-demand pra lotes que passaram pré-filtro (preco_max > lance_atual)
    — não pra todos os lotes raspados, evitando custo desnecessário.
  - FENABRAVE conta carros 0km. É proxy pra demanda de usado: modelo top em
    emplacamento novo costuma ser top em demanda usada também (mesma marca,
    mesmas peças, parque circulante crescente).
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from carros_sa.models import CategoriaVeiculo


# =============================================================================
# Bucket relativo de popularidade
# =============================================================================

class BucketPopularidade(str, Enum):
    """Bucket relativo dentro da categoria — multiplica `dias_giro_estimado`."""

    BLOCKBUSTER = "blockbuster"  # top 5 da categoria  → × 0.6
    POPULAR = "popular"          # top 6-15            → × 0.8
    NORMAL = "normal"            # top 16-30           → × 1.0
    NICHO = "nicho"              # fora do top-30      → × 1.3
    ILIQUIDO = "iliquido"        # fora E ≥10 anos     → × 1.6


_MULTIPLICADORES: Dict[BucketPopularidade, float] = {
    BucketPopularidade.BLOCKBUSTER: 0.6,
    BucketPopularidade.POPULAR: 0.8,
    BucketPopularidade.NORMAL: 1.0,
    BucketPopularidade.NICHO: 1.3,
    BucketPopularidade.ILIQUIDO: 1.6,
}


def multiplicador(bucket: BucketPopularidade) -> float:
    """Devolve fator pra ajustar `dias_giro_estimado` (ex.: 0.6 = vende 40% mais rápido)."""
    return _MULTIPLICADORES[bucket]


# =============================================================================
# Carregamento do ranking YAML (cacheado)
# =============================================================================

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "mercado"


@lru_cache(maxsize=8)
def carregar_ranking(yaml_path: Optional[str] = None) -> Dict[str, List[str]]:
    """Lê o YAML mais recente em config/mercado/ ou um path explícito.

    Retorna: {categoria_str: [modelo1, modelo2, ...]} em ordem decrescente
    de emplacamentos (índice 0 = mais vendido).
    """
    if yaml_path:
        path = Path(yaml_path)
    else:
        candidatos = sorted(CONFIG_DIR.glob("fenabrave_ranking_*.yaml"), reverse=True)
        if not candidatos:
            return {}
        path = candidatos[0]

    if not path.exists():
        return {}

    with path.open() as fh:
        data = yaml.safe_load(fh)

    return data.get("ranking_por_categoria", {}) or {}


def invalidar_cache() -> None:
    """Limpa o cache do ranking — útil em testes e após updates manuais."""
    carregar_ranking.cache_clear()


# =============================================================================
# Matching de modelo
# =============================================================================

_NAO_ALFANUM = re.compile(r"[^a-z0-9]+")


def _slug(s: str) -> str:
    """Normaliza pra slug ASCII pra matching tolerante a acentos/grafia."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return _NAO_ALFANUM.sub(" ", s.lower()).strip()


def _modelo_bate(modelo_lote: str, modelo_ranking: str) -> bool:
    """Modelo do lote (ex.: 'Polo Track 1.0') casa com modelo do ranking ('Polo Track')?

    Estratégia: ranking aparece como substring (case/acento insensitive) no
    nome do lote. Trata 'Polo Track' antes de 'Polo' por causa da ordem de
    `_buscar_posicao` (mais específico antes do genérico).
    """
    return _slug(modelo_ranking) in _slug(modelo_lote)


# =============================================================================
# API principal
# =============================================================================

def bucket_modelo(
    marca: str,
    modelo: str,
    categoria: CategoriaVeiculo,
    ano: Optional[int] = None,
    ano_referencia: int = 2026,
    yaml_path: Optional[str] = None,
) -> BucketPopularidade:
    """Devolve bucket relativo do modelo dentro da sua categoria.

    Args:
        marca, modelo: do lote
        categoria: classificação inferida (do laudo ou heurística)
        ano: ano do veículo — usado pra bucket ILIQUIDO (≥10 anos + fora do top)
        ano_referencia: ano "agora" pra cálculo de idade
        yaml_path: path explícito (default: pega o YAML mais recente em config/mercado)
    """
    ranking = carregar_ranking(yaml_path)
    modelos_categoria = ranking.get(categoria.value, [])

    # Busca posição (1-indexed); -1 se não está no ranking. Itera em ordem do
    # YAML pra que matches mais específicos ("Polo Track") venham antes de
    # genéricos ("Polo") quando ambos casarem.
    posicao = -1
    for i, m_rank in enumerate(modelos_categoria, start=1):
        if _modelo_bate(modelo, m_rank):
            posicao = i
            break

    if posicao == -1:
        # Fora do ranking: NICHO por default; ILIQUIDO se também for ≥10 anos
        if ano is not None and (ano_referencia - ano) >= 10:
            return BucketPopularidade.ILIQUIDO
        return BucketPopularidade.NICHO

    if posicao <= 5:
        return BucketPopularidade.BLOCKBUSTER
    if posicao <= 15:
        return BucketPopularidade.POPULAR
    if posicao <= 30:
        return BucketPopularidade.NORMAL
    return BucketPopularidade.NICHO  # caso a lista exceda 30 (defensivo)


def ajustar_dias_giro(
    dias_base: int,
    bucket: BucketPopularidade,
) -> int:
    """Aplica o multiplicador do bucket nos dias_giro estimados.

    Floor de 15 dias evita resultados absurdos quando combinado com calibrações
    já agressivas (ex.: blockbuster × 0.6 num modelo que já tem prior 25d = 15d).
    """
    ajustado = int(round(dias_base * multiplicador(bucket)))
    return max(ajustado, 15)
