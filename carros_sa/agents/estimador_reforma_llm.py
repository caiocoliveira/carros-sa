"""EstimadorReformaLLM — estima custo de reforma via LLM.

Substitui a tabela YAML estática (família × severidade) por um LLM que lê:
  - LaudoEstruturado (avarias, severidade_geral, motor_ok, documentação)
  - Carro (marca, modelo, ano, km, lance_atual)
  - Região/pátio da empresa (SP capital ≠ Uberlândia interior)
  - Observações do inspetor (texto livre do PDF)

O LLM devolve itens específicos ("Coluna B esq. soldada + repintura — R$ 3.800")
em vez de um valor de tabela cego. Custo rouge varia POR CARRO mesmo quando a
severidade é igual, que é o ponto: Gol 1.0 2014 com motor suspeito ≠ Range Rover
2018 com motor suspeito — hoje a tabela devolvia R$ 4.000 pros dois.

Fallback: qualquer exceção ou JSON malformado do LLM → cai no estimador
determinístico. O pipeline nunca fica sem custo.
"""

from __future__ import annotations

import logging
from typing import Optional

from carros_sa.agents.estimador_reforma import estimar as estimar_deterministico
from carros_sa.agents.text_llm_clients import TextLLMClient
from carros_sa.models import (
    CustoReforma,
    ItemReforma,
    LaudoEstruturado,
)
from carros_sa.tenancy import EmpresaConfig

logger = logging.getLogger(__name__)


_PROMPT_TEMPLATE = """Você é um perito em reforma de veículos pós-leilão no Brasil.
Estime o CUSTO TOTAL de reparo/reforma em REAIS para o veículo abaixo, considerando
o mercado de peças, mão-de-obra e complexidade do carro.

Veículo:
  Marca: {marca}
  Modelo: {modelo}
  Ano: {ano}
  KM: {km}
  Lance atual: R$ {lance_atual}

Pátio da oficina (referência de mão-de-obra):
  Cidade: {cidade_patio} / {uf_patio}

Diagnóstico do laudo cautelar:
  Severidade geral: {severidade_geral}
  Motor original (numeração confere): {motor_ok}
  Documentação: {documentacao}
  Avarias estruturais/funcionais identificadas:
{avarias_block}

Observações livres do inspetor (pode estar vazio):
{observacoes}

Regras:
  1. Seja ESPECÍFICO por carro — um Range Rover com motor suspeito custa MUITO mais
     que um Gol 1.0 com motor suspeito, porque peças importadas + mão-de-obra
     especializada. Fuja de valores genéricos.
  2. Mão-de-obra em São Paulo capital é ~25% mais cara que interior de MG.
  3. Quando severidade_geral é 'estrutural', SEMPRE inclua um item de
     "alinhamento de chassi + calibração de airbag/ADAS".
  4. Motor não original + sem dano estrutural → estime retífica/inspeção segundo
     complexidade do motor daquele modelo específico (atmosférico popular <
     turbo premium < motor importado).
  5. Devolva entre 1 e 8 itens. Cada item é uma linha de serviço real.

Responda APENAS em JSON no formato:
{{
  "itens": [
    {{"descricao": "texto curto do serviço + peça", "custo": <int em reais>}}
  ],
  "custo_total": <int em reais, soma dos itens>,
  "range_min": <int, piso plausível>,
  "range_max": <int, teto plausível>,
  "confidence": <float 0.0-1.0>,
  "justificativa": "uma frase explicando o driver principal do custo"
}}
"""


def _format_avarias(laudo: LaudoEstruturado) -> str:
    if not laudo.avarias:
        return "    (nenhuma avaria estrutural específica identificada)"
    return "\n".join(
        f"    - {av.parte} ({av.severidade.value}): {av.descricao or '-'}"
        for av in laudo.avarias
    )


def _build_prompt(
    laudo: LaudoEstruturado,
    lote_info: dict,
    empresa: EmpresaConfig,
    observacoes_pdf: str,
) -> str:
    return _PROMPT_TEMPLATE.format(
        marca=lote_info.get("marca", "?"),
        modelo=lote_info.get("modelo", "?"),
        ano=lote_info.get("ano", "?"),
        km=lote_info.get("km") or "desconhecido",
        lance_atual=lote_info.get("lance_atual") or "desconhecido",
        cidade_patio=empresa.patio.cidade,
        uf_patio=empresa.patio.uf,
        severidade_geral=laudo.severidade_geral.value,
        motor_ok="sim" if laudo.motor_ok else "NÃO",
        documentacao=laudo.documentacao.value,
        avarias_block=_format_avarias(laudo),
        observacoes=(observacoes_pdf or "(sem observações livres no laudo)").strip()[:2000],
    )


def _parse_resposta(raw: dict) -> CustoReforma:
    """Valida e normaliza a resposta do LLM em CustoReforma.

    - `itens` precisa ser lista não vazia com {descricao, custo} válidos.
    - `custo_total` é recomputado como soma dos itens (LLM às vezes erra a aritmética).
    - `range_min/range_max` — se ausentes/ruins, deriva como custo_total ± 25%.
    Levanta ValueError se a resposta for inutilizável.
    """
    itens_raw = raw.get("itens")
    if not isinstance(itens_raw, list) or not itens_raw:
        raise ValueError("resposta do LLM sem lista 'itens' válida")

    itens: list[ItemReforma] = []
    for entry in itens_raw:
        if not isinstance(entry, dict):
            continue
        descricao = str(entry.get("descricao", "")).strip()
        try:
            custo = int(round(float(entry.get("custo", 0))))
        except (TypeError, ValueError):
            continue
        if custo <= 0 or not descricao:
            continue
        itens.append(ItemReforma(descricao=descricao, custo=custo))

    if not itens:
        raise ValueError("nenhum item válido na resposta do LLM")

    custo_total = sum(it.custo for it in itens)

    range_min_raw = raw.get("range_min")
    range_max_raw = raw.get("range_max")
    try:
        range_min = int(range_min_raw) if range_min_raw is not None else int(custo_total * 0.75)
        range_max = int(range_max_raw) if range_max_raw is not None else int(custo_total * 1.25)
    except (TypeError, ValueError):
        range_min = int(custo_total * 0.75)
        range_max = int(custo_total * 1.25)

    # Sanidade: min <= total <= max; se LLM mandou invertido, deriva da incerteza.
    if not (range_min <= custo_total <= range_max):
        range_min = int(custo_total * 0.75)
        range_max = int(custo_total * 1.25)

    # Justificativa livre do LLM — sobrevive em CustoReforma.racional pra ser
    # exibida na planilha. Opcional: LLMs podem omitir o campo sem quebrar.
    racional_raw = raw.get("justificativa")
    racional = str(racional_raw).strip() if isinstance(racional_raw, str) and racional_raw.strip() else None

    return CustoReforma(
        itens=itens,
        custo_total=custo_total,
        range_min=range_min,
        range_max=range_max,
        racional=racional,
    )


def estimar_llm(
    laudo: LaudoEstruturado,
    lote_info: dict,
    empresa: EmpresaConfig,
    llm_client: TextLLMClient,
    observacoes_pdf: str = "",
    config_dir: Optional[str] = None,
) -> CustoReforma:
    """Estima CustoReforma via LLM; cai no determinístico em qualquer falha.

    `lote_info` aceita chaves: marca, modelo, ano, km, lance_atual.
    `observacoes_pdf` é o texto livre do bloco Observações do laudo (opcional).
    """
    prompt = _build_prompt(laudo, lote_info, empresa, observacoes_pdf)
    try:
        raw = llm_client.generate_json(prompt)
        return _parse_resposta(raw)
    except Exception as e:
        logger.warning(
            "EstimadorReformaLLM: fallback pro determinístico (%s: %s)",
            type(e).__name__, e,
        )
        return estimar_deterministico(laudo, empresa, config_dir=config_dir)
