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

from carros_sa.agents.estimador_reforma import (
    aplicar_piso_imprevistos,
    estimar_deterministico,
)
from carros_sa.agents.text_llm_clients import TextLLMClient
from carros_sa.models import (
    CustoReforma,
    ItemReforma,
    LaudoEstruturado,
)
from carros_sa.tenancy import EmpresaConfig

logger = logging.getLogger(__name__)


_PROMPT_TEMPLATE = """Você é um perito em reforma de veículos pós-leilão no Brasil.
Estime o CUSTO de REPARO DE DANOS em REAIS — APENAS os serviços necessários
pra devolver o carro à condição de revenda, considerando as avarias listadas
e o status mecânico/documental.

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

Regras de ESCOPO (o que SIM e o que NÃO incluir):
  1. INCLUA somente reparos DIRETAMENTE causados pelas avarias listadas acima
     ou pelo status de motor/documentação.
  2. NÃO INCLUA — esses custos já estão fora do orçamento de reforma:
     - Manutenção preventiva (troca de óleo, filtros, fluidos de rotina)
     - Detalhamento / polimento / higienização / estética pós-leilão
     - Revisão elétrica geral ou inspeção de sistemas sem problema relatado
     - Regularização de documentação em cartório/DETRAN
     - Alinhamento de suspensão preventivo (só se chassi foi batido)
     - Diagnóstico eletrônico de praxe (sem falha indicada)
  3. Se NÃO há avarias listadas E motor_ok='sim' E documentação='ok', retorne
     `itens: []` — não invente serviços. Piso de imprevistos é aplicado pós-LLM.
  4. "Alinhamento de chassi + calibração ADAS/airbag" SOMENTE quando
     severidade_geral == 'estrutural' (não aplique em severidade média/grave
     de lataria sem comprometimento estrutural).
  5. Motor não original + severidade NÃO estrutural → estime retífica/inspeção
     segundo complexidade do motor (atmosférico popular R$ 2-4k, turbo comum
     R$ 4-6k, turbo premium/importado R$ 6-15k). NÃO aplique este item quando
     severidade é estrutural (motor já entra junto com a colisão na tabela).
  6. Seja ESPECÍFICO por carro — Range Rover com peça importada custa MUITO
     mais que Gol popular. Fuja de valores genéricos.
  7. Mão-de-obra em São Paulo capital ~25% mais cara que interior de MG.
  8. Devolva entre 0 e 8 itens. 0 = veículo limpo, sem reparo necessário.

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

    - `itens` pode ser lista vazia (carro sem avarias → 0 reparos).
    - `custo_total` é recomputado como soma dos itens (LLM às vezes erra a aritmética).
    - `range_min/range_max` — se ausentes/ruins, deriva como custo_total ± 25%.
    - Piso de imprevistos (config) aplicado via `aplicar_piso_imprevistos`.
    Levanta ValueError se a resposta for estruturalmente inutilizável
    (shape inválido).
    """
    itens_raw = raw.get("itens")
    if not isinstance(itens_raw, list):
        raise ValueError("resposta do LLM sem campo 'itens' como lista")

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

    # Itens vazios é caso VÁLIDO agora (veículo limpo).
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

    custo = CustoReforma(
        itens=itens,
        custo_total=custo_total,
        range_min=range_min,
        range_max=range_max,
        racional=racional,
    )
    return aplicar_piso_imprevistos(custo)


def estimar_llm(
    laudo: LaudoEstruturado,
    lote_info: dict,
    empresa: EmpresaConfig,
    llm_client: TextLLMClient,
    observacoes_pdf: str = "",
    config_dir: str | None = None,
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
