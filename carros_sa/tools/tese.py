"""Tese de compra — sinalização prescritiva baseada no histórico.

Pra cada lote ranqueado, responde: "isso se parece com o que a gente já
comprou?". NÃO afeta score ROI nem filtra — é uma coluna informativa na
planilha, o operador decide.

Três níveis:
  🟢 tipica         — modelo/ticket/km batem com o padrão (3 de 3 eixos)
  🟡 fora_da_curva  — um eixo fora (ex: modelo novo mas ticket OK)
  🔴 atipica        — 2+ sinais de risco combinados (V6 alta km + nicho raro, etc)

Configuração vive em `config/tese.yaml`. Mexer lá não exige release.

Fluxo típico:
    estat = carregar_historico_stat(session)
    cfg = carregar_config_tese()
    tese = calcular_tese(lote.marca, lote.modelo, lote.km, av.preco_max, estat, cfg)
    # tese.texto vai direto na célula
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml
from sqlmodel import Session, select

from carros_sa.models import Arrematado, Lote


# =============================================================================
# Config
# =============================================================================

CONFIG_PATH = Path("config/tese.yaml")


@dataclass
class SinalRuimConfig:
    nome: str
    motivo: str
    patterns: List[str] = field(default_factory=list)
    excludes: List[str] = field(default_factory=list)
    km_minimo: Optional[int] = None
    compras_minimas_modelo: Optional[int] = None  # pra "eletrico_sem_revenda"


@dataclass
class TeseConfig:
    ticket_min: int
    ticket_max: int
    km_min: int
    km_max: int
    compras_minimas_modelo: int
    sinais_ruins: List[SinalRuimConfig]


def carregar_config_tese(path: Path = CONFIG_PATH) -> TeseConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    tipica = raw["tipica"]
    sinais = []
    for nome, bloco in (raw.get("sinais_ruins") or {}).items():
        bloco = bloco or {}
        sinais.append(SinalRuimConfig(
            nome=nome,
            motivo=bloco.get("motivo", nome),
            patterns=list(bloco.get("patterns") or []),
            excludes=list(bloco.get("excludes") or []),
            km_minimo=bloco.get("km_minimo"),
            compras_minimas_modelo=bloco.get("compras_minimas_modelo"),
        ))
    return TeseConfig(
        ticket_min=int(tipica["ticket_min"]),
        ticket_max=int(tipica["ticket_max"]),
        km_min=int(tipica["km_min"]),
        km_max=int(tipica["km_max"]),
        compras_minimas_modelo=int(tipica.get("compras_minimas_modelo", 2)),
        sinais_ruins=sinais,
    )


# =============================================================================
# Histórico agregado
# =============================================================================

@dataclass
class HistoricoStat:
    """Resumo do histórico Arrematado pronto pra consultas O(1)."""

    modelos_contagem: dict  # "marca_slug|modelo_slug" → n compras
    ticket_min: int
    ticket_max: int
    total_compras: int

    def contagem_modelo(self, marca: str, modelo: str) -> int:
        return self.modelos_contagem.get(_chave_modelo(marca, modelo), 0)


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _chave_modelo(marca: str, modelo: str) -> str:
    """Chave canônica = slug(marca) + "|" + primeira palavra do modelo.

    Agrupa variações: "Focus Titanium Plus" e "Focus 2.0 SE" caem no mesmo
    bucket "ford|focus". Granularidade futura (ex: separar Renegade Sport de
    Longitude) seria mudança aqui só.
    """
    marca_k = _slug(marca)
    modelo_primeira = _slug(modelo).split("_")[0] if modelo else ""
    return f"{marca_k}|{modelo_primeira}"


def carregar_historico_stat(session: Session) -> HistoricoStat:
    """Uma query, compila tudo que a Tese precisa."""
    contagem: dict = {}
    valores: List[int] = []
    rows = session.exec(
        select(Arrematado, Lote).join(Lote, Arrematado.lote_id == Lote.id)
    ).all()
    for arr, lote in rows:
        if lote is None:
            continue
        chave = _chave_modelo(lote.marca or "", lote.modelo or "")
        contagem[chave] = contagem.get(chave, 0) + 1
        if arr.preco_real and arr.preco_real > 0:
            valores.append(arr.preco_real)
    return HistoricoStat(
        modelos_contagem=contagem,
        ticket_min=min(valores) if valores else 0,
        ticket_max=max(valores) if valores else 0,
        total_compras=len(rows),
    )


# =============================================================================
# Cálculo da Tese
# =============================================================================

@dataclass
class Tese:
    nivel: str           # "tipica" | "fora_da_curva" | "atipica"
    texto: str           # célula pronta pra planilha
    razoes: List[str]    # pra debug/log (ordem: positivas, depois negativas)


def _casa_padrao(modelo: str, patterns: List[str], excludes: List[str]) -> bool:
    if not modelo or not patterns:
        return False
    m = modelo.lower()
    if any(ex in m for ex in excludes):
        return False
    return any(p in m for p in patterns)


def _detectar_sinais_ruins(
    marca: str,
    modelo: str,
    km: Optional[int],
    lance_max: int,
    hist: HistoricoStat,
    cfg: TeseConfig,
) -> List[str]:
    """Retorna lista de motivos disparados (vazia = nenhum sinal ruim)."""
    disparados: List[str] = []

    for s in cfg.sinais_ruins:
        if s.nome == "nicho_sem_repeticao":
            # modelo com menos compras do que o mínimo histórico da "tipica".
            # Usa compras_minimas_modelo do `tipica` pra manter um único
            # threshold; Cadenza (1 compra) e Civic (0) ambos caem aqui.
            if hist.contagem_modelo(marca, modelo) < cfg.compras_minimas_modelo:
                disparados.append(s.motivo)
            continue

        if s.nome == "ticket_acima_teto":
            if lance_max > cfg.ticket_max:
                disparados.append(s.motivo)
            continue

        # Sinais baseados em pattern no nome do modelo
        if not _casa_padrao(modelo, s.patterns, s.excludes):
            continue
        if s.km_minimo is not None and (km or 0) < s.km_minimo:
            continue
        if s.compras_minimas_modelo is not None:
            if hist.contagem_modelo(marca, modelo) >= s.compras_minimas_modelo:
                continue
        disparados.append(s.motivo)

    return disparados


def calcular_tese(
    marca: str,
    modelo: str,
    km: Optional[int],
    lance_max: int,
    historico: HistoricoStat,
    config: TeseConfig,
) -> Tese:
    """Classifica o lote em 3 níveis sem modificar score ROI.

    Regras:
      - tipica: 3 eixos batem (modelo ≥ N compras, ticket na faixa, km na faixa)
      - atipica: ≥ 2 sinais ruins
      - fora_da_curva: resto
    """
    n_compras = historico.contagem_modelo(marca, modelo)
    modelo_ok = n_compras >= config.compras_minimas_modelo
    ticket_ok = config.ticket_min <= lance_max <= config.ticket_max
    km_ok = km is not None and config.km_min <= km <= config.km_max

    sinais = _detectar_sinais_ruins(marca, modelo, km, lance_max, historico, config)
    razoes: List[str] = []

    if len(sinais) >= 2:
        nivel = "atipica"
        razoes = sinais
        # ex: "🔴 atípica — V6 gasolina + km alto · nicho sem histórico"
        texto = f"🔴 atípica — {' · '.join(sinais)}"
        return Tese(nivel=nivel, texto=texto, razoes=razoes)

    if modelo_ok and ticket_ok and km_ok and not sinais:
        primeira = _slug(modelo).split("_")[0].title() if modelo else modelo
        razoes.append(f"{primeira} ×{n_compras}")
        razoes.append(f"R$ {lance_max//1000}k na faixa")
        razoes.append(f"{(km or 0)//1000}k km")
        texto = f"🟢 típica — {primeira} ×{n_compras}, R$ {lance_max//1000}k, {(km or 0)//1000}k km"
        return Tese(nivel="tipica", texto=texto, razoes=razoes)

    # fora_da_curva — um eixo fora ou 1 sinal ruim isolado
    detalhes: List[str] = []
    # Se o sinal "nicho sem histórico" já vai aparecer, não precisamos também
    # dizer "modelo novo (N)" — redundante pro operador.
    nicho_ja_vai_aparecer = any("sem histórico" in s for s in sinais)
    if not modelo_ok and not nicho_ja_vai_aparecer:
        detalhes.append(f"modelo novo ({n_compras} no histórico)")
    if not ticket_ok:
        detalhes.append(f"ticket R$ {lance_max//1000}k fora")
    if not km_ok:
        if km is None:
            detalhes.append("km ausente")
        else:
            detalhes.append(f"{km//1000}k km fora")
    for s in sinais:
        detalhes.append(s)
    if not detalhes:
        detalhes.append("perfil misto")
    texto = f"🟡 fora da curva — {' · '.join(detalhes)}"
    return Tese(nivel="fora_da_curva", texto=texto, razoes=detalhes)
