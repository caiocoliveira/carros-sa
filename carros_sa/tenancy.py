"""Configuração por empresa (tenant).

Cada empresa = YAML em `config/empresas/<id>.yaml` + linha em tabela `empresa`.
O YAML é a fonte de verdade da config operacional (pátio, margens, frete, risco).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from carros_sa.models import CategoriaVeiculo

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "empresas"

class PatioConfig(BaseModel):
    cidade: str
    uf: str
    cep: str | None = None

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

class CustosOperacionais(BaseModel):
    """Decomposição dos custos operacionais não-veículo recorrentes por carro.

    Itens calibrados a partir da operação real do Polo Track 2024 (compra Auto Avaliar
    em 2025-11, venda em 2026-03). Ver `data/historico/uberlandia_arrematado.csv`
    e ROADMAP.md (Bloco A da calibração).

    Quando presente no YAML, substitui o `custo_op_fixo` agregado — o precificador
    consome a soma. Quando ausente, fallback pro int legado em `EmpresaConfig.custo_op_fixo`.
    """

    despachante: int = 0          # transferência + autorizações
    higienizacao: int = 0          # limpeza, polimento pré-anúncio
    marketing_medio: int = 0       # Instagram/Facebook/portais BCO BV/MobiAuto
    laudo_cautelar: int = 0        # laudo de pré-venda (ANFAVEA)
    combustivel: int = 0           # média por carro (ida/teste/entrega)
    outros: int = 0                # catch-all configurável por tenant

    @property
    def total(self) -> int:
        return (
            self.despachante
            + self.higienizacao
            + self.marketing_medio
            + self.laudo_cautelar
            + self.combustivel
            + self.outros
        )

class EmpresaConfig(BaseModel):
    """Snapshot carregado de config/empresas/<id>.yaml."""

    empresa_id: str
    nome: str
    patio: PatioConfig
    margem: MargemConfig
    raio_max_km: int = 1000
    # Raio operacional — cidades dentro desse haversine a partir do pátio são
    # raspadas. Distinto de raio_max_km (teto legado; não usado no frete real).
    raio_operacao_km: int = 150
    fator_risco_bounds: tuple[float, float] = (1.0, 2.0)
    fator_liquidez_bounds: tuple[float, float] = (1.0, 1.8)
    # tabela_frete: chave = "min-max" km, valor = dict categoria -> reais
    tabela_frete: dict  # dict[str, dict[str, int]]
    categorias_aceitas: list  # list[CategoriaVeiculo]
    # Taxa do leilão pode vir em duas formas — somadas no precificador.
    # `taxa_leilao_pct` é proporcional ao lance vencedor (típico de leilão judicial).
    # `taxa_leilao_fixa` é R$ fixos independentes do lance (típico de Auto Avaliar = R$999).
    taxa_leilao_pct: float = Field(default=0.0, ge=0.0, le=0.5)
    taxa_leilao_fixa: int = Field(default=0, ge=0)
    # Custos operacionais — formato novo decomposto OU int legado agregado.
    # `_custo_op_aplicado` resolve qual usar via model_validator.
    custos_operacionais: CustosOperacionais | None = None
    custo_op_fixo: int = 0

    @model_validator(mode="after")
    def _resolver_custo_op(self) -> "EmpresaConfig":
        """Se YAML traz `custos_operacionais` decomposto, deriva o agregado.

        Se traz só `custo_op_fixo` (formato legado), mantém. Permite migração
        gradual sem quebrar configs antigas.
        """
        if self.custos_operacionais is not None:
            # Decomposto venceu — sobrescreve o int agregado pra refletir a soma
            object.__setattr__(self, "custo_op_fixo", self.custos_operacionais.total)
        return self

    def frete_para(self, distancia_km: int, categoria: CategoriaVeiculo) -> int:
        """Lookup na tabela de frete. Além do último range → retorna maior + 30% extra.

        Caso especial: `distancia_km == 0` (mesma cidade do pátio) retorna 0.
        O comprador busca o carro pessoalmente, sem logística contratada.
        """
        if distancia_km == 0:
            return 0

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

    def cidades_de_busca(self) -> list:
        """Lista de Municipio no raio operacional (crescente por distância; pátio primeiro).

        Import local pra evitar ciclo `geo → tenancy → geo`.
        """
        from carros_sa.tools.geo import cidades_no_raio
        return cidades_no_raio(
            cidade_base=self.patio.cidade,
            uf_base=self.patio.uf,
            raio_km=self.raio_operacao_km,
        )

@lru_cache(maxsize=32)
def carregar_empresa(empresa_id: str, config_dir: Path | None = None) -> EmpresaConfig:
    base = Path(config_dir) if config_dir else CONFIG_DIR
    path = base / f"{empresa_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Config da empresa não encontrada: {path}")
    with path.open() as f:
        raw = yaml.safe_load(f)
    return EmpresaConfig(**raw)

def listar_empresas(config_dir: Path | None = None) -> list:
    base = Path(config_dir) if config_dir else CONFIG_DIR
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.yaml"))
