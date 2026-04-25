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
# Camada 2b — Parser da seção "PINTURA V1" do cautelar Auto Avaliar
# =============================================================================
#
# O cautelar V2 + Pintura tem uma seção dedicada ao estado da pintura/funilaria,
# fora do bloco "Observações" e fora do diagrama estrutural da página 2. Formato
# típico (após PyMuPDF):
#
#     PINTURA V1
#     • VISTA SUPERIOR
#     1 - COLUNA DIANTEIRA DIREITA:
#     PINTURA EM BOAS CONDIÇÕES
#     6 - TAMPA TRASEIRA:
#     AMASSADO, RISCADO OU RALADO (5CM A 20CM)
#     ...
#
# Esses defeitos são **cosméticos** (lataria/pintura) — diferente da seção
# estrutural, que rastreia coluna/longarina **reparada/soldada**. Aqui:
#  - "PINTURA EM BOAS CONDIÇÕES" / "ORIGINAL" → ignora (sem avaria).
#  - "PEQUENO AMASSADO/RISCADO/RALADO (ATÉ 5CM)" → SeveridadeAvaria.LEVE.
#  - "AMASSADO/RISCADO/RALADO (5CM A 20CM)" / faixas maiores → MEDIA.
#  - "REPARAD<x>" / "REPINTAD<x>" / "SUBSTITU<x>" → MEDIA (intervenção prévia).
#
# Importante: avarias daqui são MEDIA mesmo em colunas — `_severidade_consolidada`
# só promove pra ESTRUTURAL quando a coluna está GRAVE ou ESTRUTURAL, então a
# pintura sozinha não falsifica um diagnóstico estrutural.

_RE_BLOCO_PINTURA = re.compile(
    r"PINTURA\s+V\d+\s*(?:\n.*?)?(?=OBSERVA[ÇC][ÃA]O|OBSERVA[ÇC][ÕO]ES\s+GERAIS|\Z)",
    re.DOTALL | re.IGNORECASE,
)

# Linha de item dentro do bloco: "<n> - <NOME PEÇA>:" seguido (na próxima linha)
# da condição. PyMuPDF preserva as quebras de linha do PDF.
_RE_LINHA_PINTURA = re.compile(
    r"^\s*\d+\s*-\s*([^:\n]+?):\s*\n\s*([^\n]+)",
    re.MULTILINE,
)

# Mapa nome da peça do laudo → `parte` interno do projeto.
# Ordem importa: mais específico primeiro (PARA-CHOQUE antes de qualquer alias).
def _peca_pintura_para_parte(nome: str) -> Optional[str]:
    """Normaliza o nome da peça do bloco PINTURA V1 para o `parte` interno.

    Retorna None quando a peça não é mapeável (ex.: nome desconhecido).
    """
    n = nome.upper().strip()

    # Coluna: posição (DIANTEIRA=A, CENTRAL=B, TRASEIRA=C) + lado.
    m = re.search(r"COLUNA\s+(DIANTEIRA|CENTRAL|TRASEIRA)\s+(ESQUERD[OA]|DIREIT[OA])", n)
    if m:
        pos_map = {"DIANTEIRA": "a", "CENTRAL": "b", "TRASEIRA": "c"}
        lado = "esquerda" if m.group(2).startswith("ESQUERD") else "direita"
        return f"coluna_{pos_map[m.group(1)]}_{lado}"

    # Para-choque (com ou sem hífen): só tem posição (DIANTEIRO/TRASEIRO), sem lado.
    m = re.search(r"PARA[\s\-]?CHOQUE\s+(DIANTEIR[OA]|TRASEIR[OA])", n)
    if m:
        pos = "dianteiro" if m.group(1).startswith("DIANTEIR") else "traseiro"
        return f"para_choque_{pos}"

    # Para-lama: posição + lado.
    m = re.search(r"PARA[\s\-]?LAMA\s+(DIANTEIR[OA]|TRASEIR[OA])\s+(ESQUERD[OA]|DIREIT[OA])", n)
    if m:
        pos = "dianteiro" if m.group(1).startswith("DIANTEIR") else "traseiro"
        lado = "esquerdo" if m.group(2).startswith("ESQUERD") else "direito"
        return f"paralama_{pos}_{lado}"

    # Porta: posição + lado.
    m = re.search(r"PORTA\s+(DIANTEIRA|TRASEIRA)\s+(ESQUERDA|DIREITA)", n)
    if m:
        pos = m.group(1).lower()
        lado = m.group(2).lower()
        return f"porta_{pos}_{lado}"

    # Lateral traseira: chapa lateral grande (folha lat. traseira do laudo estrutural).
    m = re.search(r"LATERAL\s+(TRASEIRA|DIANTEIRA)\s+(ESQUERDA|DIREITA)", n)
    if m:
        pos = m.group(1).lower()
        lado = m.group(2).lower()
        return f"lateral_{pos}_{lado}"

    # Capô e tampa traseira (peças únicas, sem lado).
    if re.search(r"CAP[ÔO]\b", n):
        return "capo_tampa_motor"
    if re.search(r"TAMPA\s+TRASEIRA", n):
        return "tampa_traseira"
    if re.search(r"\bTETO\b", n):
        return "teto"
    # Painel frontal/traseiro (raro na pintura, mas cobre).
    m = re.search(r"PAINEL\s+(FRONTAL|TRASEIRO)", n)
    if m:
        return f"painel_{m.group(1).lower()}"

    return None


def _severidade_pintura(condicao: str) -> Optional[SeveridadeAvaria]:
    """Mapeia o texto da condição de pintura para SeveridadeAvaria. None = sem avaria."""
    c = condicao.upper().strip()

    # Sem avaria — ignora.
    if "BOAS CONDI" in c or "ORIGINAL" in c or "SEM AVARIA" in c:
        return None

    # Pequena avaria.
    if "PEQUENO" in c or "ATÉ 5CM" in c or "ATE 5CM" in c:
        return SeveridadeAvaria.LEVE

    # Avaria moderada (faixa típica do laudo) ou repintura/reparo prévio.
    if (
        "5CM A 20CM" in c or "AMASSADO" in c or "RISCADO" in c or "RALADO" in c
        or "REPARAD" in c or "REPINTAD" in c or "SUBSTITU" in c
        or "ACIMA DE 20" in c or "MAIOR QUE" in c
    ):
        return SeveridadeAvaria.MEDIA

    # Texto desconhecido mas não-vazio: assumir LEVE pra não inflar custo.
    return SeveridadeAvaria.LEVE


def extrair_avarias_pintura(texto_bruto: Optional[str]) -> list:
    """Extrai `list[Avaria]` da seção "PINTURA V1" do cautelar V2 + Pintura.

    Operates no `texto_bruto` (output de `parse_laudo_textual`), localizando o
    bloco entre o título "PINTURA V1" e a próxima seção (OBSERVAÇÃO/Z).

    Retorna lista vazia quando a seção não existe (laudos antigos sem módulo
    de pintura) ou quando todas as peças estão "EM BOAS CONDIÇÕES".
    """
    if not texto_bruto:
        return []

    bloco_match = _RE_BLOCO_PINTURA.search(texto_bruto)
    if not bloco_match:
        return []
    bloco = bloco_match.group(0)

    avarias: list = []
    vistas: set = set()
    for m in _RE_LINHA_PINTURA.finditer(bloco):
        nome_raw = m.group(1).strip()
        condicao = m.group(2).strip()

        severidade = _severidade_pintura(condicao)
        if severidade is None:
            continue

        parte = _peca_pintura_para_parte(nome_raw)
        if parte is None:
            continue
        if parte in vistas:
            continue
        vistas.add(parte)

        avarias.append(Avaria(
            parte=parte,
            severidade=severidade,
            descricao=f"Pintura: {condicao[:120]}",
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
    import logging as _logging
    _log = _logging.getLogger(__name__)

    txt = parse_laudo_textual(pdf_path)
    visual: Optional[dict] = None
    visao_falhou = False
    try:
        visual = extrair_laudo_visual(pdf_path, vision_client)
    except Exception as exc:
        # Loga em WARN — sem isso a falha vira invisível e o ranking fica
        # enviesado pelo fallback pessimista (motor_ok=False, doc=DESCONHECIDO).
        # Triagem 2026-04-16: 123/163 lotes caíram nesse path silenciosamente,
        # virando fator_risco saturado em 1.65 sem motivo concreto.
        _log.warning(
            "extrair_laudo_visual falhou em %s (%s: %s) — caindo pra textual",
            pdf_path.name, type(exc).__name__, exc,
        )
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

    # Camada 4 — seção PINTURA V1 do cautelar V2+Pintura. Sempre tenta extrair:
    # captura defeitos cosméticos (amassados, riscos, ralados) que o diagrama
    # estrutural da página 2 não vê e que não aparecem em "Observações".
    # Antes desse fix, lotes com estrutura limpa mas pintura ruim caíam todos no
    # piso de R$ 1.000 (visão="nenhuma" → avarias=[] → custo=0 → piso).
    for av_pint in extrair_avarias_pintura(txt.texto_bruto):
        if av_pint.parte not in vistas_parte:
            vistas_parte.add(av_pint.parte)
            avarias.append(av_pint)

    # Severidade: combina valor da visão (página 2 estrutural) com o que as
    # avarias textuais + pintura indicam. Pega o MAIOR — antes a visão "nenhuma"
    # zerava as avarias de pintura silenciosamente.
    severidade_visual = None
    if visual is not None and "severidade_geral" in visual:
        severidade_visual = _SEVERIDADE_MAP.get(
            visual.get("severidade_geral", "nenhuma"), SeveridadeAvaria.NENHUMA,
        )
    severidade_consolidada = _severidade_consolidada(avarias)
    if severidade_visual is None:
        severidade = severidade_consolidada
    else:
        severidade = max(severidade_visual, severidade_consolidada, key=_severidade_rank)

    # Documentação: consolidação textual
    if txt.roubo_furto_ativo or txt.comunicado_venda:
        doc = StatusDocumentacao.PENDENCIA_GRAVE
    elif txt.licenciado is False:
        doc = StatusDocumentacao.PENDENCIA_LEVE
    elif txt.licenciado is True:
        doc = StatusDocumentacao.OK
    else:
        doc = StatusDocumentacao.DESCONHECIDO

    # Motor OK: assume True quando ausência de info textual (ausência ≠ problema).
    # Apenas False quando explicitamente identificado motor adulterado/inconsistente,
    # OU quando severidade for ESTRUTURAL (motor afetado por design). Antes
    # `bool(None)=False` injustamente penalizava 75% dos laudos com fator_risco
    # +0.3 sem causa real (triagem 2026-04-16).
    motor_textual = txt.motor_original if txt.motor_original is not None else True
    motor_ok = motor_textual and severidade != SeveridadeAvaria.ESTRUTURAL

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


_RANK_SEVERIDADE = {
    SeveridadeAvaria.NENHUMA: 0,
    SeveridadeAvaria.LEVE: 1,
    SeveridadeAvaria.MEDIA: 2,
    SeveridadeAvaria.GRAVE: 3,
    SeveridadeAvaria.ESTRUTURAL: 4,
}


def _severidade_rank(s: SeveridadeAvaria) -> int:
    return _RANK_SEVERIDADE.get(s, 0)


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
