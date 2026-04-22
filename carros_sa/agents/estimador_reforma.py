"""EstimadorReforma — converte LaudoEstruturado em CustoReforma.

Determinístico, sem LLM. Tabela de preços vive em `config/reforma/<empresa>.yaml`
(uma por empresa — mão de obra varia por região).

Cada Avaria do laudo é casada a uma "família" de peça (longarina, coluna, porta,
paralama, capô/tampa, teto, painel) por prefixo do nome (`Avaria.parte`), e o
custo é a célula (família × severidade) da tabela. Famílias desconhecidas caem no
bloco `default`.

Adicionais fixos:
  - severidade_geral == ESTRUTURAL → custo de alinhamento/retrabalho de chassi
  - motor_ok == False AND severidade não-estrutural → custo de pendência mecânica
    isolada (não dobra com adicional estrutural pra evitar dupla contagem; o
    extrator já zera motor_ok quando severidade é ESTRUTURAL).

Range min/max é o `custo_total ± incerteza_pct` da empresa.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from carros_sa.config import get_settings
from carros_sa.models import (
    CustoReforma,
    ItemReforma,
    LaudoEstruturado,
    SeveridadeAvaria,
)
from carros_sa.tenancy import EmpresaConfig

if TYPE_CHECKING:
    from carros_sa.agents.text_llm_clients import TextLLMClient

logger = logging.getLogger(__name__)

_ITEM_RESERVA = "reserva pra imprevistos (ajustes/retoques que aparecem no pátio)"


def aplicar_piso_imprevistos(custo: CustoReforma) -> CustoReforma:
    """Garante custo_total >= reforma_reserva_imprevistos_brl (config).

    Premissa operacional: qualquer carro que passa pelo pátio gera uns R$ 1.000
    de "coisinhas" (retoque pontual, borracha de porta, limpeza profunda de
    estofado). Aplicado tanto no determinístico quanto no LLM pra que as duas
    estimativas sejam comparáveis.
    """
    piso = get_settings().reforma_reserva_imprevistos_brl
    if custo.custo_total >= piso:
        return custo

    faltante = piso - custo.custo_total
    itens_novos = list(custo.itens) + [ItemReforma(descricao=_ITEM_RESERVA, custo=faltante)]
    return CustoReforma(
        itens=itens_novos,
        custo_total=piso,
        range_min=int(piso * 0.75),
        range_max=int(piso * 1.25),
        racional=getattr(custo, "racional", None),
    )

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "reforma"


# Ordem importa: prefixos mais específicos primeiro ("capo_tampa" antes de "tampa").
_FAMILIAS = (
    ("longarina", "longarina"),
    ("coluna", "coluna"),
    ("capo_tampa", "capo_tampa"),
    ("tampa", "tampa"),
    ("porta", "porta"),
    ("paralama", "paralama"),
    ("teto", "teto"),
    ("painel", "painel"),
)


def _familia_de(parte: str) -> str:
    """Mapeia `Avaria.parte` à chave de família na tabela."""
    nome = parte.lower()
    for prefixo, familia in _FAMILIAS:
        if nome.startswith(prefixo):
            return familia
    return "default"


@lru_cache(maxsize=32)
def carregar_tabela(empresa_id: str, config_dir: str | None = None) -> dict:
    """Lê e cacheia a tabela de reforma da empresa."""
    base = Path(config_dir) if config_dir else CONFIG_DIR
    path = base / f"{empresa_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Tabela de reforma não encontrada: {path}")
    with path.open() as f:
        return yaml.safe_load(f)


def _custo_pecas(familia: str, severidade: SeveridadeAvaria, tabela: dict) -> int:
    """Lookup (familia, severidade) com fallback no bloco `default`."""
    bloco = tabela.get("pecas", {}).get(familia) or tabela.get("default", {})
    valor = bloco.get(severidade.value)
    if valor is None:
        # Fallback final: célula equivalente em `default`.
        valor = tabela.get("default", {}).get(severidade.value, 0)
    return int(valor)


def estimar_deterministico(
    laudo: LaudoEstruturado,
    empresa: EmpresaConfig,
    config_dir: str | None = None,
) -> CustoReforma:
    """Calcula CustoReforma para um laudo dado a empresa via tabela YAML.

    Pipeline:
      1. Para cada Avaria → item com custo (familia × severidade) na tabela.
      2. Adicional estrutural fixo se severidade_geral é ESTRUTURAL.
      3. Adicional motor se motor_ok=False e severidade não-estrutural.
      4. range_min/range_max = custo_total ± incerteza_pct.
    """
    tabela = carregar_tabela(empresa.empresa_id, config_dir)
    itens: list[ItemReforma] = []

    for avaria in laudo.avarias:
        if avaria.severidade == SeveridadeAvaria.NENHUMA:
            continue
        familia = _familia_de(avaria.parte)
        custo = _custo_pecas(familia, avaria.severidade, tabela)
        if custo <= 0:
            continue
        descricao = f"{avaria.parte} ({avaria.severidade.value})"
        itens.append(ItemReforma(descricao=descricao, custo=custo))

    if laudo.severidade_geral == SeveridadeAvaria.ESTRUTURAL:
        extra = int(tabela.get("adicional_estrutural", 0))
        if extra > 0:
            itens.append(ItemReforma(
                descricao="adicional estrutural (alinhamento chassi, recalibração)",
                custo=extra,
            ))
    elif not laudo.motor_ok:
        extra = int(tabela.get("adicional_motor_nao_ok", 0))
        if extra > 0:
            itens.append(ItemReforma(
                descricao="pendência mecânica (motor não conforme)",
                custo=extra,
            ))

    custo_total = sum(item.custo for item in itens)
    incerteza = float(tabela.get("incerteza_pct", 0.25))
    range_min = int(custo_total * (1.0 - incerteza))
    range_max = int(custo_total * (1.0 + incerteza))

    custo = CustoReforma(
        itens=itens,
        custo_total=custo_total,
        range_min=range_min,
        range_max=range_max,
    )
    return aplicar_piso_imprevistos(custo)


def estimar(
    laudo: LaudoEstruturado,
    empresa: EmpresaConfig,
    *,
    llm_client: "TextLLMClient | None" = None,
    lote_info: dict | None = None,
    observacoes_pdf: str = "",
    config_dir: str | None = None,
) -> CustoReforma:
    """Facade único: LLM quando disponível, determinístico caso contrário.

    Se `llm_client` vier None (ou `lote_info`/`observacoes_pdf` ausentes pra
    LLM ter contexto suficiente), delega direto pro determinístico. Com LLM,
    tenta extrair itens específicos por carro; fallback interno pro
    determinístico em qualquer erro.
    """
    if llm_client is None:
        return estimar_deterministico(laudo, empresa, config_dir=config_dir)

    # Import tardio pra não criar ciclo — o LLM module importa este.
    from carros_sa.agents.estimador_reforma_llm import estimar_llm

    return estimar_llm(
        laudo=laudo,
        lote_info=lote_info or {},
        empresa=empresa,
        llm_client=llm_client,
        observacoes_pdf=observacoes_pdf,
        config_dir=config_dir,
    )
