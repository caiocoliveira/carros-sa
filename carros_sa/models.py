"""Contratos Pydantic e tabelas SQLModel do Carros SA.

Separação em dois blocos:
  1. Contratos Pydantic (fluxo entre agentes, validação I/O)
  2. SQLModel tables (persistência) — globais vs. por empresa
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from sqlmodel import JSON, Column, Field as SQLField, SQLModel


# =============================================================================
# Enums compartilhados
# =============================================================================

class CategoriaVeiculo(str, Enum):
    HATCH = "hatch"
    SEDAN = "sedan"
    SUV = "suv"
    UTILITARIO = "utilitario"
    PICAPE = "picape"
    OUTRO = "outro"


class SeveridadeAvaria(str, Enum):
    NENHUMA = "nenhuma"
    LEVE = "leve"
    MEDIA = "media"
    GRAVE = "grave"
    ESTRUTURAL = "estrutural"


class StatusDocumentacao(str, Enum):
    OK = "ok"
    PENDENCIA_LEVE = "pendencia_leve"    # multas, licenciamento
    PENDENCIA_GRAVE = "pendencia_grave"  # leilão judicial, sinistro
    DESCONHECIDO = "desconhecido"


# =============================================================================
# Contratos Pydantic (inter-agentes)
# =============================================================================

class LoteRaw(BaseModel):
    """Saída do ScraperLeilao — dados brutos de um lote."""

    lote_id: str
    leilao: str                           # "auto_arremate"
    url: str
    marca: str
    modelo: str
    ano: int
    km: Optional[int] = None
    lance_atual: int                      # em reais
    fim_em: Optional[datetime] = None
    fotos_urls: list[str] = Field(default_factory=list)
    laudo_texto: str = ""
    origem_cidade: Optional[str] = None
    origem_uf: Optional[str] = None
    origem_cep: Optional[str] = None
    # Referências de preço extraídas do próprio anúncio Auto Avaliar (SSR).
    # Preenchidas pelo scraper quando a página de detalhe carrega; None quando
    # o lote não tem os sinais (ex.: tipos de anúncio sem "ULTIMA AVALIAÇÃO").
    preco_referencia_aa: Optional[int] = None   # R$ sugerido pela Tabela Auto Avaliar
    fipe_pct_lance_minimo: Optional[int] = None # 0..999 — % do lance min sobre FIPE


class Avaria(BaseModel):
    parte: str                            # "lataria_dianteira", "motor", ...
    severidade: SeveridadeAvaria
    descricao: str = ""


class LaudoEstruturado(BaseModel):
    """Saída do ExtratorLaudo — global, cacheado por hash."""

    avarias: list[Avaria]
    severidade_geral: SeveridadeAvaria
    motor_ok: bool
    documentacao: StatusDocumentacao
    categoria_veiculo: CategoriaVeiculo
    confidence: float = Field(ge=0.0, le=1.0)


class SinalMercado(BaseModel):
    """Saída do AvaliadorMercado — FIPE + Webmotors + Tabela Auto Avaliar. Global."""

    fipe: int
    webmotors_mediana: int
    webmotors_p25: int
    n_anuncios_competidores: int
    dias_giro_estimado: int               # heurística inicial
    # Preço-referência da Tabela Auto Avaliar (mercado atacado real). Opcional:
    # None quando o lote não trouxe o dado ou o histórico ainda não tem entrada
    # pra esse modelo/ano. Quando presente, Precificador usa como âncora alternativa
    # e escolhe o menor entre preco_giro_fipe e preco_giro_aa.
    auto_avaliar_ref: Optional[int] = None

    @field_validator("fipe", "webmotors_mediana", "webmotors_p25")
    @classmethod
    def _positivo(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("preço deve ser positivo")
        return v

    @field_validator("auto_avaliar_ref")
    @classmethod
    def _aa_positivo(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("auto_avaliar_ref deve ser positivo se informado")
        return v


class ItemReforma(BaseModel):
    descricao: str
    custo: int


class CustoReforma(BaseModel):
    """Saída do EstimadorReforma — POR empresa (tabela de custo varia)."""

    itens: list[ItemReforma]
    custo_total: int
    range_min: int
    range_max: int


class CustoLogistico(BaseModel):
    """Saída do CalculadoraFrete — POR empresa (pátio varia)."""

    origem_cidade: str
    origem_uf: str
    destino_cidade: str
    destino_uf: str
    distancia_km: int
    categoria_veiculo: CategoriaVeiculo
    frete_estimado: int
    fonte_cotacao: str                    # "tabela_empresa" | "cotacao_manual"


class Avaliacao(BaseModel):
    """Saída do Precificador — POR empresa."""

    lote_id: str
    empresa_id: str
    preco_alvo: int                       # lance-alvo (não cobrir acima)
    preco_max: int                        # limite absoluto com desconto da margem mínima
    score_roi: float                      # ROI esperado normalizado [0,1+]
    fator_risco: float
    fator_liquidez: float
    margem_aplicada: float
    frete_incluso: int
    reforma_estimada: int
    taxas_leilao: int
    preco_giro: int                       # preço de venda de referência consolidado (menor entre FIPE e AA)
    # Decomposição do preço de giro por fonte — permite à triagem mostrar
    # ambas as âncoras (FIPE e Tabela Auto Avaliar) lado a lado.
    preco_giro_fipe: int                  # ancorado em FIPE descontado ∩ webmotors p25
    preco_giro_aa: Optional[int] = None   # ancorado em Tabela Auto Avaliar; None se sem dado
    # Âncoras brutas — persistidas pra que o relatório mostre FIPE puro e a mediana
    # Webmotors ao lado do preço de giro, facilitando auditoria humana.
    fipe: int
    webmotors_mediana: int
    justificativa: str


# =============================================================================
# SQLModel tables — GLOBAIS (sem empresa_id)
# =============================================================================

class Empresa(SQLModel, table=True):
    __tablename__ = "empresa"
    id: str = SQLField(primary_key=True)         # ex: "carros_uberlandia"
    nome: str
    config_yaml_path: str
    criada_em: datetime = SQLField(default_factory=datetime.utcnow)


class Lote(SQLModel, table=True):
    __tablename__ = "lote"
    id: str = SQLField(primary_key=True)         # lote_id do leilão
    leilao: str = SQLField(index=True)
    url: str
    marca: str = SQLField(index=True)
    modelo: str = SQLField(index=True)
    ano: int
    km: Optional[int] = None
    lance_atual: int
    fim_em: Optional[datetime] = None
    origem_cidade: Optional[str] = None
    origem_uf: Optional[str] = None
    origem_cep: Optional[str] = None
    # Sinais Tabela Auto Avaliar (ver LoteRaw pro significado). Persistidos
    # junto com o Lote pra não depender de abrir raw_json["detalhe"].
    preco_referencia_aa: Optional[int] = None
    fipe_pct_lance_minimo: Optional[int] = None
    raw_json: dict = SQLField(default_factory=dict, sa_column=Column(JSON))
    scraped_at: datetime = SQLField(default_factory=datetime.utcnow)


class LaudoCache(SQLModel, table=True):
    """Extração do laudo é GLOBAL — mesmo lote serve várias empresas."""

    __tablename__ = "laudo"
    lote_id: str = SQLField(primary_key=True, foreign_key="lote.id")
    avarias_json: list = SQLField(default_factory=list, sa_column=Column(JSON))
    severidade_geral: str
    motor_ok: bool
    documentacao: str
    categoria_veiculo: str
    confidence: float
    modelo_llm: str
    custo_usd: float = 0.0
    extraido_em: datetime = SQLField(default_factory=datetime.utcnow)


class AnuncioWebmotors(SQLModel, table=True):
    __tablename__ = "anuncio_webmotors"
    id: str = SQLField(primary_key=True)
    marca: str = SQLField(index=True)
    modelo: str = SQLField(index=True)
    ano: int
    km: Optional[int] = None
    preco: int
    url: str
    regiao: Optional[str] = None
    primeiro_visto: datetime
    ultimo_visto: datetime
    sumiu_em: Optional[datetime] = None          # proxy de venda


class ModeloFipeCache(SQLModel, table=True):
    __tablename__ = "modelo_fipe_cache"
    id: Optional[int] = SQLField(default=None, primary_key=True)
    marca: str = SQLField(index=True)
    modelo: str = SQLField(index=True)
    ano: int
    valor: int
    consultado_em: datetime = SQLField(default_factory=datetime.utcnow)


class PrecoReferenciaAA(SQLModel, table=True):
    """Histórico longitudinal de preços-referência da Tabela Auto Avaliar.

    Alimentado pelo scraper a cada vez que um anúncio traz "ULTIMA AVALIAÇÃO".
    Serve a dois propósitos:
      1. Consultar preço de referência de um modelo que já apareceu em leilão
         ao avaliar um lote futuro do mesmo modelo (amortiza escassez).
      2. Construir série temporal pra calibração (workstream H).
    """

    __tablename__ = "preco_referencia_aa"
    id: Optional[int] = SQLField(default=None, primary_key=True)
    marca: str = SQLField(index=True)
    modelo: str = SQLField(index=True)
    ano: int = SQLField(index=True)
    versao: Optional[str] = None
    preco: int                                       # R$ da ULTIMA AVALIAÇÃO
    fipe_pct_lance_minimo: Optional[int] = None      # % sobre FIPE anotado no lote-fonte
    origem_lote_id: Optional[str] = None             # lote_id que trouxe o dado
    coletado_em: datetime = SQLField(default_factory=datetime.utcnow, index=True)


# =============================================================================
# SQLModel tables — POR EMPRESA
# =============================================================================

class AvaliacaoLote(SQLModel, table=True):
    __tablename__ = "avaliacao_lote"
    id: Optional[int] = SQLField(default=None, primary_key=True)
    empresa_id: str = SQLField(foreign_key="empresa.id", index=True)
    lote_id: str = SQLField(foreign_key="lote.id", index=True)
    preco_alvo: int
    preco_max: int
    score_roi: float
    fator_risco: float
    fator_liquidez: float
    margem_aplicada: float
    frete_incluso: int
    reforma_estimada: int
    taxas_leilao: int
    preco_giro: int                                # consolidado (menor entre FIPE e AA)
    preco_giro_fipe: int                           # decomposto — ancorado em FIPE
    preco_giro_aa: Optional[int] = None            # decomposto — ancorado em Tabela Auto Avaliar
    # Âncoras brutas pro relatório — FIPE puro e mediana Webmotors no momento da avaliação.
    # Podem ficar NULL em registros antigos (pré-workstream K).
    fipe: Optional[int] = None
    webmotors_mediana: Optional[int] = None
    justificativa: str
    criado_em: datetime = SQLField(default_factory=datetime.utcnow)


class Arrematado(SQLModel, table=True):
    """Ground truth — por empresa."""

    __tablename__ = "arrematado"
    id: Optional[int] = SQLField(default=None, primary_key=True)
    empresa_id: str = SQLField(foreign_key="empresa.id", index=True)
    lote_id: str = SQLField(foreign_key="lote.id", index=True)
    preco_real: int
    data: datetime
    gastos_reforma_real: Optional[int] = None
    vendido_por: Optional[int] = None
    vendido_em: Optional[datetime] = None


class CalibracaoCoeficiente(SQLModel, table=True):
    __tablename__ = "calibracao_coeficientes"
    id: Optional[int] = SQLField(default=None, primary_key=True)
    empresa_id: str = SQLField(foreign_key="empresa.id", index=True)
    coeficiente: str                              # ex: "fipe_bias", "reforma_severidade_media"
    valor: float
    calibrado_em: datetime = SQLField(default_factory=datetime.utcnow)
