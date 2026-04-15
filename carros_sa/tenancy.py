"""Configuração por empresa (tenant).

Cada empresa = YAML em `config/empresas/<id>.yaml` + linha em tabela `empresa`.
O YAML é a fonte de verdade da config operacional (pátio, margens, frete, risco).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

import yaml
from pydantic import BaseModel, Field, field_validator

from carros_sa.models import CategoriaVeiculo

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "empresas"


class PatioConfig(BaseModel):
    cidade: str
    uf: str
    cep: Optional[str] = None


class MargemConfig(BaseModel):
    base: float = Field(ge=0.0, le=1.0)
    minima_absoluta: float = Field(ge=0.0, le=1.0)

    @field_validator("minima_absoluta")
    @classmethod
    def _min_menor_que_base(cls, v: float, info) -> float:
        base = info.data.get("base")
        if base is not None and v > base:
            raise ValueError("minima_absoluta não pode exceder base")
        return v


class EmpresaConfig(BaseModel):
    """Snapshot carregado de config/empresas/<id>.yaml."""

    empresa_id: str
    nome: str
    patio: PatioConfig
    margem: MargemConfig
    raio_max_km: int = 1000
    fator_risco_bounds: Tuple[float, float] = (1.0, 2.0)
    fator_liquidez_bounds: Tuple[float, float] = (1.0, 1.8)
    # tabela_frete: chave = "min-max" km, valor = dict categoria -> reais
    tabela_frete: dict  # dict[str, dict[str, int]]
    categorias_aceitas: list  # list[CategoriaVeiculo]
    taxa_leilao_pct: float = Field(ge=0.0, le=0.5)
    custo_op_fixo: int = 0

    def frete_para(self, distancia_km: int, categoria: CategoriaVeiculo) -> int:
        """Lookup na tabela de frete. Além do último range → retorna maior + 30% extra."""
        faixas = []  # list[tuple[int, int, dict[str, int]]]
        for chave, valores in self.tabela_frete.items():
            lo, hi = chave.split("-")
            faixas.append((int(lo), int(hi), valores))
        faixas.sort(key=lambda x: x[0])

        for lo, hi, valores in faixas:
            if lo <= distancia_km < hi:
                return valores[categoria.value]

        # Excedeu a maior faixa — extrapola conservadoramente
        _, _, maiores = faixas[-1]
        return int(maiores[categoria.value] * 1.3)


@lru_cache(maxsize=32)
def carregar_empresa(empresa_id: str, config_dir: Optional[Path] = None) -> EmpresaConfig:
    base = Path(config_dir) if config_dir else CONFIG_DIR
    path = base / f"{empresa_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Config da empresa não encontrada: {path}")
    with path.open() as f:
        raw = yaml.safe_load(f)
    return EmpresaConfig(**raw)


def listar_empresas(config_dir: Optional[Path] = None) -> list:
    base = Path(config_dir) if config_dir else CONFIG_DIR
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.yaml"))
