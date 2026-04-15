"""ExtratorLaudo — transforma o PDF de laudo cautelar do Auto Avaliar em LaudoEstruturado.

Duas camadas:
  1. Parse textual via PyMuPDF — captura chassi, placa, licenciamento, originalidade
     e observações. Determinístico, zero custo.
  2. Haiku 4.5 Vision sobre a página 2 do laudo (diagrama estrutural) — classifica
     cada peça em {Original, Avariado, Reparado/Soldado/Substituído} usando a
     legenda impressa na própria imagem.

Dado global (cacheado por hash do PDF): um laudo serve N empresas.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

from carros_sa.models import (
    Avaria,
    CategoriaVeiculo,
    LaudoEstruturado,
    SeveridadeAvaria,
    StatusDocumentacao,
)

# =============================================================================
# Camada 1 — parse textual (PyMuPDF)
# =============================================================================

@dataclass
class LaudoTextual:
    placa: Optional[str]
    chassi: Optional[str]
    motor_numero: Optional[str]
    km_laudo: Optional[int]
    licenciado: Optional[bool]
    roubo_furto_ativo: Optional[bool]
    comunicado_venda: Optional[bool]
    chassi_original: Optional[bool]
    motor_original: Optional[bool]
    odometro_legivel: Optional[bool]
    observacoes: str
    texto_bruto: str


_CHASSI_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")
_PLACA_RE = re.compile(r"\b([A-Z]{3}[\d][A-Z0-9][\d]{2})\b")  # formato antigo + Mercosul
# Odômetro no PDF do Auto Avaliar aparece como número isolado (3-6 dígitos com
# ponto de milhar), após "CONSTATOU-SE QUE:" ou próximo do chassi/motor.
_KM_CONTEXTO_RE = re.compile(
    r"(?:CONSTATOU-SE QUE:?|Od[oô]metro:?|Odômetro do Veículo:?)\s*\n?\s*([\d]{1,3}\.[\d]{3}(?:\.[\d]{3})?)",
    re.IGNORECASE,
)


def parse_laudo_textual(pdf_path: Path) -> LaudoTextual:
    doc = fitz.open(str(pdf_path))
    texto = "\n".join(page.get_text() for page in doc)
    doc.close()

    # Normalizações simples pra facilitar regex
    lower = texto.lower()

    chassi = _CHASSI_RE.search(texto)
    placa = _PLACA_RE.search(texto)
    km = _KM_CONTEXTO_RE.search(texto)

    licenciado = None
    if "veículo está licenciado" in lower or "licenciado? sim" in lower or "sim, veículo está licenciado" in lower:
        licenciado = True
    elif "não licenciado" in lower or "licenciado? não" in lower:
        licenciado = False

    roubo_ativo = None
    if "não consta roubo/furto ativo" in lower:
        roubo_ativo = False
    elif "consta roubo" in lower or "furto ativo" in lower:
        roubo_ativo = True

    com_venda = None
    if "não existe comunicado de venda" in lower:
        com_venda = False
    elif "existe comunicado de venda" in lower:
        com_venda = True

    chassi_original = None
    if "chassi original" in lower or "chassi era original" in lower:
        chassi_original = True
    elif "chassi adulterado" in lower or "adulteração" in lower and "chassi" in lower:
        chassi_original = False

    motor_original = None
    if "motor em conformidade com a bin" in lower:
        motor_original = True

    odometro_legivel = None
    if "visualização impossibilitada" in lower and "odômetro" in lower:
        odometro_legivel = False
    elif km:
        odometro_legivel = True

    # Observações: linhas com "ATENÇÃO"/"OBSERVAÇÃO" ou no fim
    obs_blocks = re.findall(r"(OBSERVA[ÇC][ÕO]ES?[:\s][\s\S]{0,500})", texto, re.IGNORECASE)
    observacoes = "\n---\n".join(obs_blocks)

    return LaudoTextual(
        placa=placa.group(1) if placa else None,
        chassi=chassi.group(1) if chassi else None,
        motor_numero=None,
        km_laudo=int(km.group(1).replace(".", "")) if km else None,
        licenciado=licenciado,
        roubo_furto_ativo=roubo_ativo,
        comunicado_venda=com_venda,
        chassi_original=chassi_original,
        motor_original=motor_original,
        odometro_legivel=odometro_legivel,
        observacoes=observacoes,
        texto_bruto=texto,
    )


# =============================================================================
# Camada 2 — Haiku Vision na página 2 (diagrama estrutural)
# =============================================================================

_HAIKU_MODEL = "claude-haiku-4-5"


_PROMPT_PAGINA_ESTRUTURAL = """Você recebe a PÁGINA 2 de um laudo cautelar veicular brasileiro.
A página mostra um diagrama de um carro visto de cima com círculos coloridos em várias peças.

LEGENDA DOS CÍRCULOS (impressa na própria imagem):
  - Verde  → "Original" / "Pintura Original" (peça íntegra)
  - Amarelo → "Avariado / Pequenos Danos" (dano superficial)
  - Vermelho → "Reparado / Soldado / Substituído" (intervenção estrutural)

Sua tarefa: extrair, peça por peça, qual a cor/classificação.

Peças do diagrama (use exatamente estes nomes):
  painel_frontal, painel_traseiro,
  capo_tampa_motor, tampa_traseira, teto_veiculo,
  longarina_dianteira_esquerda, longarina_dianteira_direita,
  longarina_traseira_esquerda, longarina_traseira_direita,
  coluna_a_esquerda, coluna_a_direita,
  coluna_b_esquerda, coluna_b_direita,
  coluna_c_esquerda, coluna_c_direita,
  coluna_d_esquerda, coluna_d_direita,
  paralama_dianteiro_esquerdo, paralama_dianteiro_direito,
  paralama_traseiro_esquerdo, paralama_traseiro_direito,
  porta_dianteira_esquerda, porta_dianteira_direita,
  porta_traseira_esquerda, porta_traseira_direita

Retorne APENAS JSON válido no formato:
{
  "pecas": {
    "<nome_peca>": "original" | "avariado" | "reparado"
  },
  "severidade_geral": "nenhuma" | "leve" | "media" | "grave" | "estrutural",
  "pecas_reparadas": ["<nomes>"],
  "pecas_avariadas": ["<nomes>"],
  "confidence": 0.0-1.0,
  "observacao_visual": "1-2 frases descrevendo o padrão do dano"
}

Regras para severidade_geral:
- "estrutural" se ANY longarina ou coluna aparecer como "reparado"
- "grave" se 3+ peças quaisquer aparecerem como "reparado"
- "media" se 1-2 peças de carroceria externa (porta, paralama, capô) reparadas
- "leve" se só avariado/pequenos danos
- "nenhuma" se tudo original

NÃO inclua comentários, NÃO inclua texto fora do JSON."""


def extrair_laudo_visual(pdf_path: Path, vision_client) -> dict:
    """Renderiza página 2 do PDF (diagrama estrutural) e delega a classificação pro `VisionClient`.

    vision_client: instância de carros_sa.agents.vision_clients.VisionClient
    Retorna o dict decodificado do JSON.
    """
    doc = fitz.open(str(pdf_path))
    if doc.page_count < 2:
        doc.close()
        raise ValueError(f"PDF com menos de 2 páginas: {pdf_path}")
    pix = doc[1].get_pixmap(dpi=150)
    png_bytes = pix.tobytes("png")
    doc.close()

    return vision_client.classify(png_bytes, _PROMPT_PAGINA_ESTRUTURAL)


# =============================================================================
# Combinação: LaudoEstruturado final
# =============================================================================

_SEVERIDADE_MAP = {
    "nenhuma": SeveridadeAvaria.NENHUMA,
    "leve": SeveridadeAvaria.LEVE,
    "media": SeveridadeAvaria.MEDIA,
    "grave": SeveridadeAvaria.GRAVE,
    "estrutural": SeveridadeAvaria.ESTRUTURAL,
}


def extrair_laudo(
    pdf_path: Path,
    vision_client,
    categoria_veiculo: CategoriaVeiculo = CategoriaVeiculo.OUTRO,
) -> LaudoEstruturado:
    """Pipeline completo: parse textual + visão + consolidação em LaudoEstruturado."""
    txt = parse_laudo_textual(pdf_path)
    visual = extrair_laudo_visual(pdf_path, vision_client)

    # Avarias: uma por peça reparada/avariada
    avarias = []
    for nome in visual.get("pecas_reparadas", []):
        avarias.append(Avaria(parte=nome, severidade=SeveridadeAvaria.GRAVE if "coluna" in nome or "longarina" in nome else SeveridadeAvaria.MEDIA, descricao="reparado/soldado/substituído"))
    for nome in visual.get("pecas_avariadas", []):
        avarias.append(Avaria(parte=nome, severidade=SeveridadeAvaria.LEVE, descricao="avariado/pequenos danos"))

    severidade = _SEVERIDADE_MAP.get(visual.get("severidade_geral", "nenhuma"), SeveridadeAvaria.NENHUMA)

    # Documentação: consolidação textual
    if txt.roubo_furto_ativo or txt.comunicado_venda:
        doc = StatusDocumentacao.PENDENCIA_GRAVE
    elif txt.licenciado is False:
        doc = StatusDocumentacao.PENDENCIA_LEVE
    elif txt.licenciado is True:
        doc = StatusDocumentacao.OK
    else:
        doc = StatusDocumentacao.DESCONHECIDO

    motor_ok = bool(txt.motor_original) and severidade != SeveridadeAvaria.ESTRUTURAL

    return LaudoEstruturado(
        avarias=avarias,
        severidade_geral=severidade,
        motor_ok=motor_ok,
        documentacao=doc,
        categoria_veiculo=categoria_veiculo,
        confidence=float(visual.get("confidence", 0.7)),
    )


# =============================================================================
# Utilidades
# =============================================================================

def hash_pdf(pdf_path: Path) -> str:
    """SHA1 do PDF — chave de cache global."""
    h = hashlib.sha1()
    h.update(Path(pdf_path).read_bytes())
    return h.hexdigest()
