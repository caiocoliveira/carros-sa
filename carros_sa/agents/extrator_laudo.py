"""ExtratorLaudo — transforma o PDF de laudo cautelar do Auto Avaliar em LaudoEstruturado.

Três camadas complementares:
  1. Visão (Gemini Flash / Haiku) sobre a página 2 do laudo — classifica cada peça
     em {Original, Avariado, Reparado} lendo o diagrama colorido.
  2. Parse textual via PyMuPDF do bloco "Observações" — regex no texto livre
     do inspetor ("VEÍCULO POSSUI REPARO NAS COLUNAS B e C..."). Usado como
     fallback quando a camada de visão falha (ex.: Gemini 503 por overload)
     e como reforço mesmo no caminho feliz.
  3. Parse textual de identificadores (chassi, placa, licenciamento, motor) —
     determinístico, zero custo, sempre executado.

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

    # Observações: o PDF do Auto Avaliar tem o rótulo "Observações:" em vários
    # pontos. Nos que são campo de formulário, o texto seguinte começa em
    # CAIXA ALTA (convenção do laudo); os falsos positivos (prosa técnica como
    # "observações e constatações do Inspetor") começam em minúsculo. Anconramos
    # no caractere maiúsculo seguinte pra filtrar.
    obs_matches = re.findall(
        r"Observa[çc][õo]es:?\s+([A-ZÁÉÍÓÚÃÕÂÊÎÔÛÜÇ][^\n]{4,300})",
        texto,
    )
    observacoes = "\n".join(m.strip() for m in obs_matches)

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
# Camada 2 — Extrator de avarias a partir do bloco "Observações" (texto livre)
# =============================================================================
#
# Usado principalmente como fallback quando a camada de visão falha (ex.:
# Gemini 503 UNAVAILABLE por overload). Procura menções a reparo/substituição
# de peças estruturais ("VEÍCULO POSSUI REPARO NAS COLUNAS B e C DO LADO
# ESQUERDO") e constrói lista de `Avaria` equivalente à que a visão geraria.
#
# Verbo que indica reparo (negativo pra valor residual).
_RE_VERBO_REPARO = re.compile(
    r"\b(reparo|reparad[oa]|repintad[oa]|repintura|soldad[oa]|substitu[íi]d[oa]|"
    r"substitui[çc][ãa]o|amassad[oa]|danific[oa]|corro[ií]d[oa])\b",
    re.IGNORECASE,
)


def _normaliza_lado(s: str) -> Optional[str]:
    if not s:
        return None
    t = s.lower()
    if "esq" in t:
        return "esquerda"
    if "dir" in t:
        return "direita"
    return None


def _normaliza_posicao(s: str) -> Optional[str]:
    if not s:
        return None
    t = s.lower()
    if "dianteir" in t:
        return "dianteira"
    if "traseir" in t:
        return "traseira"
    return None


def _nomes_coluna(sentenca: str) -> list:
    """Detecta colunas mencionadas ('B e C', 'B', 'A, B e C', etc) + lado comum."""
    # "COLUNAS B e C DO LADO ESQUERDO" / "COLUNA B DIREITA" / "COLUNAS A, B"
    pat = re.compile(
        r"coluna[s]?\s+((?:[a-d])(?:\s*(?:e|,|/|\bE\b)\s*[a-d])*)"
        r"(?:\s+(?:do\s+)?(?:lado\s+)?(esquerd[oa]|direit[oa]))?",
        re.IGNORECASE,
    )
    resultado = []
    for m in pat.finditer(sentenca):
        letras = re.findall(r"[a-d]", m.group(1), re.IGNORECASE)
        lado = _normaliza_lado(m.group(2) or "")
        for letra in letras:
            base = f"coluna_{letra.lower()}"
            resultado.append(f"{base}_{lado}" if lado else base)
    return resultado


def _nomes_peca_com_posicao_lado(sentenca: str, alvo: str, aliases: list) -> list:
    """Para peças tipo longarina/paralama/porta que podem ter posição+lado
    adjacentes: captura 'LONGARINA DIANTEIRA DIREITA', 'PORTA TRASEIRA ESQUERDA'.
    """
    pat = re.compile(
        rf"\b({'|'.join(aliases)})\b"
        r"(?:\s+(dianteir[oa]|traseir[oa]))?"
        r"(?:\s+(esquerd[oa]|direit[oa]))?",
        re.IGNORECASE,
    )
    resultado = []
    for m in pat.finditer(sentenca):
        pos = _normaliza_posicao(m.group(2) or "")
        lado = _normaliza_lado(m.group(3) or "")
        nome = alvo
        if pos:
            nome = f"{nome}_{pos}"
        if lado:
            nome = f"{nome}_{lado}"
        resultado.append(nome)
    return resultado


def _nomes_capo(sentenca: str) -> list:
    """Capô / tampa do motor."""
    if re.search(r"cap[ôo]\b|tampa\s+do\s+motor", sentenca, re.IGNORECASE):
        if re.search(r"tampa\s+do\s+motor", sentenca, re.IGNORECASE):
            return ["tampa_motor"]
        return ["capo_tampa_motor"]
    return []


def _nomes_teto(sentenca: str) -> list:
    if re.search(r"\bteto\b", sentenca, re.IGNORECASE):
        return ["teto"]
    return []


def _nomes_painel(sentenca: str) -> list:
    m = re.search(r"painel\s+(frontal|traseir[oa])", sentenca, re.IGNORECASE)
    if m:
        qualificador = "frontal" if "frontal" in m.group(1).lower() else "traseiro"
        return [f"painel_{qualificador}"]
    return []


# Severidade base por família de peça — colunas e longarinas são sinal
# estrutural (ou quase), o resto é chapa externa.
_SEVERIDADE_POR_PREFIXO = [
    ("coluna_", SeveridadeAvaria.GRAVE),
    ("longarina", SeveridadeAvaria.GRAVE),
    ("capo_tampa_motor", SeveridadeAvaria.MEDIA),
    ("tampa_motor", SeveridadeAvaria.MEDIA),
    ("tampa_traseira", SeveridadeAvaria.MEDIA),
    ("teto", SeveridadeAvaria.MEDIA),
    ("paralama", SeveridadeAvaria.MEDIA),
    ("porta", SeveridadeAvaria.MEDIA),
    ("painel", SeveridadeAvaria.MEDIA),
]


def _severidade_de(nome: str) -> SeveridadeAvaria:
    for prefixo, sev in _SEVERIDADE_POR_PREFIXO:
        if nome.startswith(prefixo):
            return sev
    return SeveridadeAvaria.LEVE


def extrair_avarias_textuais(observacoes: Optional[str]) -> list:
    """Extrai `list[Avaria]` a partir do texto livre do campo Observações.

    Retorna lista vazia se `observacoes` for None/empty ou não contiver nenhum
    verbo de reparo. Best-effort — a camada visual continua sendo a fonte
    primária de avarias; essa função é fallback quando a visão falha.
    """
    if not observacoes:
        return []

    sentencas = re.split(r"[.;\n]+", observacoes)
    vistas: set = set()
    avarias: list = []

    for sent in sentencas:
        if not _RE_VERBO_REPARO.search(sent):
            continue

        candidatos: list = []
        candidatos.extend(_nomes_coluna(sent))
        candidatos.extend(_nomes_peca_com_posicao_lado(sent, "longarina", ["longarina"]))
        candidatos.extend(_nomes_peca_com_posicao_lado(sent, "paralama", ["paralama"]))
        candidatos.extend(_nomes_peca_com_posicao_lado(sent, "porta", ["porta"]))
        candidatos.extend(_nomes_capo(sent))
        candidatos.extend(_nomes_teto(sent))
        candidatos.extend(_nomes_painel(sent))

        for nome in candidatos:
            if nome in vistas:
                continue
            vistas.add(nome)
            severidade = _severidade_de(nome)
            avarias.append(Avaria(
                parte=nome,
                severidade=severidade,
                descricao=f"Observações do laudo: '{sent.strip()[:120]}'",
            ))
    return avarias


# =============================================================================
# Camada 3 — Haiku Vision na página 2 (diagrama estrutural)
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
    """Pipeline completo: parse textual + visão + consolidação em LaudoEstruturado.

    Se a camada de visão falhar (ex.: Gemini 503 UNAVAILABLE), cai pra apenas o
    extrator textual (observações + documentação), com confidence reduzida.
    """
    txt = parse_laudo_textual(pdf_path)
    visual: Optional[dict] = None
    visao_falhou = False
    try:
        visual = extrair_laudo_visual(pdf_path, vision_client)
    except Exception:
        visao_falhou = True

    # Avarias: começam do visual (fonte primária) + enriquecem do textual
    avarias: list = []
    vistas_parte: set = set()

    if visual is not None:
        for nome in visual.get("pecas_reparadas", []):
            if nome in vistas_parte:
                continue
            vistas_parte.add(nome)
            sev = (
                SeveridadeAvaria.GRAVE
                if ("coluna" in nome or "longarina" in nome)
                else SeveridadeAvaria.MEDIA
            )
            avarias.append(Avaria(parte=nome, severidade=sev, descricao="reparado/soldado/substituído"))
        for nome in visual.get("pecas_avariadas", []):
            if nome in vistas_parte:
                continue
            vistas_parte.add(nome)
            avarias.append(Avaria(parte=nome, severidade=SeveridadeAvaria.LEVE, descricao="avariado/pequenos danos"))

    # Sempre tenta enriquecer com o bloco Observações — pode achar peças que
    # o diagrama não cobre ou sinalizar gravidade quando visão falhou.
    for av_text in extrair_avarias_textuais(txt.observacoes):
        if av_text.parte not in vistas_parte:
            vistas_parte.add(av_text.parte)
            avarias.append(av_text)

    # Severidade: prioriza valor da visão, senão deriva das avarias textuais.
    if visual is not None and "severidade_geral" in visual:
        severidade = _SEVERIDADE_MAP.get(visual.get("severidade_geral", "nenhuma"), SeveridadeAvaria.NENHUMA)
    else:
        severidade = _severidade_consolidada(avarias)

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

    # Confidence: alta quando visão respondeu; menor quando só textual serviu.
    confidence = float(visual.get("confidence", 0.7)) if visual is not None else (
        0.65 if avarias else 0.5
    )

    return LaudoEstruturado(
        avarias=avarias,
        severidade_geral=severidade,
        motor_ok=motor_ok,
        documentacao=doc,
        categoria_veiculo=categoria_veiculo,
        confidence=confidence,
    )


def _severidade_consolidada(avarias: list) -> SeveridadeAvaria:
    """Deriva severidade_geral da lista de avarias (quando a visão falhou).

    Regras iguais às usadas no prompt do Gemini:
    - estrutural: qualquer coluna/longarina reparada/substituída
    - grave: 3+ peças quaisquer marcadas graves ou estruturais
    - media: 1-2 peças de chapa externa média
    - leve: só avariado
    - nenhuma: nada
    """
    if not avarias:
        return SeveridadeAvaria.NENHUMA

    tem_estrutural = any(
        ("coluna" in a.parte or "longarina" in a.parte)
        and a.severidade in (SeveridadeAvaria.GRAVE, SeveridadeAvaria.ESTRUTURAL)
        for a in avarias
    )
    if tem_estrutural:
        return SeveridadeAvaria.ESTRUTURAL

    n_graves = sum(1 for a in avarias if a.severidade == SeveridadeAvaria.GRAVE)
    if n_graves >= 3:
        return SeveridadeAvaria.GRAVE

    n_medias = sum(1 for a in avarias if a.severidade == SeveridadeAvaria.MEDIA)
    if n_medias >= 1:
        return SeveridadeAvaria.MEDIA

    return SeveridadeAvaria.LEVE


# =============================================================================
# Utilidades
# =============================================================================

def hash_pdf(pdf_path: Path) -> str:
    """SHA1 do PDF — chave de cache global."""
    h = hashlib.sha1()
    h.update(Path(pdf_path).read_bytes())
    return h.hexdigest()
