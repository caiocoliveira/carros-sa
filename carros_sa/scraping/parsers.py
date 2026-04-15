"""Parsers determinísticos do b2b.autoavaliar.com.br.

Toma o output de JS coletado via Chrome MCP e devolve modelos Pydantic validados.
Zero dependência de browser — funções puras, testáveis em isolamento.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from carros_sa.models import CategoriaVeiculo, LoteRaw

# =============================================================================
# Listagem (cards)
# =============================================================================

_PRECO_RE = re.compile(r"^\d{1,3}(?:\.\d{3})*,\d{2}$")
_ANO_RE = re.compile(r"^(\d{4})/(\d{4})$")
_KM_RE = re.compile(r"^\d{1,3}(?:\.\d{3})*$")
_TIMER_RE = re.compile(r"^\d{1,3}:\d{2}:\d{2}(?::\d{2})?$")
_PCT_RE = re.compile(r"^(\d{1,3})%$")
_CIDADE_UF_RE = re.compile(r"^([^/]+)/([A-Z]{2})$", re.IGNORECASE)


def _parse_br_int(s: str) -> int:
    """'22.900,00' -> 22900; '171.053' -> 171053."""
    s = s.replace(".", "").split(",")[0]
    return int(s)


def _timer_para_fim_em(agora: datetime, timer: str) -> Optional[datetime]:
    """Timer DD:HH:MM[:SS] → datetime relativo a agora."""
    parts = timer.split(":")
    if len(parts) == 4:
        d, h, m, s = parts
    elif len(parts) == 3:
        d, h, m = parts
        s = "0"
    else:
        return None
    try:
        delta = timedelta(days=int(d), hours=int(h), minutes=int(m), seconds=int(s))
        return agora + delta
    except ValueError:
        return None


_BADGES_IGNORADOS = {"anúncio destaque", "showroom", "oportunidade", "destaque"}
_COMBUSTIVEIS = {"FLEX", "GASOLINA", "DIESEL", "ETANOL", "ALCOOL", "ÁLCOOL", "HIBRIDO", "HÍBRIDO", "ELETRICO", "ELÉTRICO", "GNV"}


def parse_card_lines(
    lines: list,
    lote_id: str,
    href: str,
    leilao: str = "autoavaliar",
    agora: Optional[datetime] = None,
) -> LoteRaw:
    """Converte innerText de um `div.vehicle[id^=avl]` em LoteRaw.

    Ancora por conteúdo, não posição — variações do site (SHOWROOM, 2 preços,
    modelo multi-linha, sem rating) são toleradas desde que as âncoras existam:
        cidade/uf, ano fab/mod, combustível, km, timer.
    """
    agora = agora or datetime.utcnow()
    cleaned = [ln.strip() for ln in lines if ln.strip()]

    # Âncoras por regex/set
    origem_cidade: Optional[str] = None
    origem_uf: Optional[str] = None
    ano_modelo = 0
    km: Optional[int] = None
    fim_em = None
    precos: list = []
    combustivel_idx: Optional[int] = None
    ano_idx: Optional[int] = None

    for i, ln in enumerate(cleaned):
        if origem_cidade is None:
            m = _CIDADE_UF_RE.match(ln)
            if m:
                origem_cidade = m.group(1).strip()
                origem_uf = m.group(2).upper()
                continue
        if ano_idx is None:
            m = _ANO_RE.match(ln)
            if m:
                ano_modelo = int(m.group(2))
                ano_idx = i
        if _PRECO_RE.match(ln):
            precos.append(_parse_br_int(ln))
        if _TIMER_RE.match(ln) and fim_em is None:
            fim_em = _timer_para_fim_em(agora, ln)
        if ln.upper() in _COMBUSTIVEIS and combustivel_idx is None:
            combustivel_idx = i

    # Preço: quando há 2 (original + atual), usa o menor como lance atual
    lance_atual = min(precos) if precos else 0

    # Marca/modelo: linhas em MAIÚSCULA antes da linha do preço, descartando
    # badges e a cidade/UF/% linhas.
    preco_idx = None
    for i, ln in enumerate(cleaned):
        if _PRECO_RE.match(ln):
            preco_idx = i
            break

    cabeca = cleaned[: preco_idx if preco_idx is not None else len(cleaned)]
    header_titulo = [
        ln for ln in cabeca
        if ln.isupper() and not _PCT_RE.match(ln) and ln.lower() not in _BADGES_IGNORADOS
        and not _CIDADE_UF_RE.match(ln)
    ]
    marca = header_titulo[0] if header_titulo else ""
    modelo_partes = header_titulo[1:] if len(header_titulo) > 1 else []

    # Versão: primeira linha após o ÚLTIMO preço, antes do ano.
    # (cards com 2 preços têm o segundo preço ANTES da versão)
    versao = ""
    if ano_idx is not None:
        for j in range(ano_idx - 1, -1, -1):
            if _PRECO_RE.match(cleaned[j]):
                break  # achou o último preço
        else:
            j = -1
        if j >= 0 and j + 1 < ano_idx:
            versao = cleaned[j + 1]

    modelo = " ".join(modelo_partes).title()
    if versao:
        modelo = f"{modelo} {versao}".strip()

    # KM: número PT-BR simples entre o ano (+1) e o timer
    if ano_idx is not None:
        for ln in cleaned[ano_idx + 1 : ano_idx + 6]:
            if _KM_RE.match(ln) and "." in ln:  # KM sempre tem ponto de milhar
                km = _parse_br_int(ln)
                break

    return LoteRaw(
        lote_id=lote_id,
        leilao=leilao,
        url=href,
        marca=marca.title(),
        modelo=modelo,
        ano=ano_modelo,
        km=km,
        lance_atual=lance_atual,
        fim_em=fim_em,
        origem_cidade=origem_cidade,
        origem_uf=origem_uf,
    )


# =============================================================================
# Detalhe (flags estruturadas do HTML — sem LLM)
# =============================================================================

class DetalheFlags:
    """Sinais extraídos do HTML da página de detalhe, antes de gastar LLM."""

    def __init__(
        self,
        *,
        specs: dict,
        status_laudo: Optional[str] = None,
        status_documento: Optional[str] = None,
        prazo_transferencia_dias: Optional[int] = None,
        prazo_liberacao_dias: Optional[int] = None,
        itens_reprovados: list = None,
        opcionais: list = None,
        ipva_pago: Optional[bool] = None,
        observacoes_anunciante: str = "",
        laudo_pdf_url: Optional[str] = None,
        similares_precos: list = None,
    ):
        self.specs = specs
        self.status_laudo = status_laudo
        self.status_documento = status_documento
        self.prazo_transferencia_dias = prazo_transferencia_dias
        self.prazo_liberacao_dias = prazo_liberacao_dias
        self.itens_reprovados = itens_reprovados or []
        self.opcionais = opcionais or []
        self.ipva_pago = ipva_pago
        self.observacoes_anunciante = observacoes_anunciante
        self.laudo_pdf_url = laudo_pdf_url
        self.similares_precos = similares_precos or []

    @property
    def reprovado_estrutural(self) -> bool:
        return any("estrutural" in it.lower() for it in self.itens_reprovados)

    @property
    def laudo_aprovado(self) -> bool:
        return self.status_laudo is not None and "aprovado" in self.status_laudo.lower() and "não" not in self.status_laudo.lower()

    @property
    def early_exit(self) -> Optional[str]:
        """Razão pra descartar o lote ANTES de chamar LLM. None = seguir em frente."""
        if self.reprovado_estrutural:
            return "reprovado_estrutural"
        if self.status_laudo and "não aprovado" in self.status_laudo.lower():
            return "laudo_nao_aprovado"
        if self.prazo_transferencia_dias and self.prazo_transferencia_dias > 45:
            return f"prazo_transferencia_excessivo_{self.prazo_transferencia_dias}d"
        return None


_PRAZO_RE = re.compile(r"(\d+)\s*dias?", re.IGNORECASE)


def parse_detalhe(body_text: str, laudo_pdf_url: Optional[str] = None) -> DetalheFlags:
    """Extrai flags estruturadas do `innerText` da página de detalhe."""
    lines = [ln.strip() for ln in body_text.split("\n")]

    # 1. Specs (label/valor adjacentes)
    spec_labels = {"ANO", "KM", "COR", "PLACA", "CÂMBIO", "ORIGEM", "MOTOR", "PORTAS", "COMBUSTÍVEL"}
    specs: dict = {}
    for i, ln in enumerate(lines[:-1]):
        if ln in spec_labels and lines[i + 1] and lines[i + 1] not in spec_labels:
            specs.setdefault(ln, lines[i + 1])

    # 2. Status do laudo / documento
    status_laudo = _extrai_apos_label(lines, "STATUS DO LAUDO")
    status_documento = _extrai_apos_label(lines, "STATUS DO DOCUMENTO")

    # 3. Prazos em dias
    prazo_transf_raw = _extrai_apos_label(lines, "PRAZO ESTIMADO DE TRANSFERÊNCIA")
    prazo_lib_raw = _extrai_apos_label(lines, "PRAZO DE LIBERAÇÃO DO VEÍCULO")
    prazo_transferencia = _PRAZO_RE.search(prazo_transf_raw or "")
    prazo_liberacao = _PRAZO_RE.search(prazo_lib_raw or "")

    # 4. Itens reprovados (heurística: linhas com "REPROVADO" em caixa alta)
    itens_reprovados = [ln for ln in lines if "REPROVADO" in ln and len(ln) < 80]

    # 5. Opcionais — após label "OPCIONAIS" até encontrar próximo header (linha em caixa alta longa)
    opcionais = _extrai_lista_apos_label(lines, "OPCIONAIS", ["ITENS AVALIADOS", "DOCUMENTAÇÃO"])

    # 6. IPVA — qualquer linha contendo "IPVA" e ("PAGO" ou "PAGA")
    ipva_pago = None
    for ln in lines:
        if "IPVA" in ln.upper():
            if "PAGO" in ln.upper() or "PAGA" in ln.upper():
                ipva_pago = True
                break
            if "PENDENTE" in ln.upper() or "DÉBITO" in ln.upper() or "ATRASO" in ln.upper():
                ipva_pago = False
                break

    # 7. Observações do anunciante (bloco longo final)
    observacoes = _extrai_observacoes(body_text)

    # 8. Preços de similares na plataforma (seção "Talvez se interesse por")
    similares_precos = _extrai_precos_similares(body_text)

    return DetalheFlags(
        specs=specs,
        status_laudo=status_laudo,
        status_documento=status_documento,
        prazo_transferencia_dias=int(prazo_transferencia.group(1)) if prazo_transferencia else None,
        prazo_liberacao_dias=int(prazo_liberacao.group(1)) if prazo_liberacao else None,
        itens_reprovados=itens_reprovados,
        opcionais=opcionais,
        ipva_pago=ipva_pago,
        observacoes_anunciante=observacoes,
        laudo_pdf_url=laudo_pdf_url,
        similares_precos=similares_precos,
    )


def _extrai_apos_label(lines: list, label: str) -> Optional[str]:
    for i, ln in enumerate(lines[:-1]):
        if ln.upper().strip() == label.upper():
            return lines[i + 1]
    return None


def _extrai_lista_apos_label(lines: list, label: str, terminadores: list) -> list:
    resultado = []
    capturando = False
    for ln in lines:
        up = ln.upper().strip()
        if up.endswith(label.upper()):
            capturando = True
            continue
        if capturando:
            if any(t in up for t in terminadores) or len(ln) > 60:
                break
            if ln and len(ln) < 50:
                resultado.append(ln)
    return resultado


def _extrai_observacoes(body_text: str) -> str:
    """Bloco longo de observações do anunciante (após documentação)."""
    marker_re = re.compile(r"(LEIA O ANÚNCIO|ATEN[ÇC][ÃA]O)", re.IGNORECASE)
    m = marker_re.search(body_text)
    if not m:
        return ""
    start = m.start()
    end = body_text.find("Talvez se interesse", start)
    trecho = body_text[start : end if end > 0 else start + 2000]
    return trecho.strip()


def _extrai_precos_similares(body_text: str) -> list:
    """Pega preços (R$) da seção 'Talvez se interesse por'."""
    idx = body_text.find("Talvez se interesse")
    if idx < 0:
        return []
    bloco = body_text[idx : idx + 2000]
    return [_parse_br_int(m) for m in re.findall(r"\d{1,3}(?:\.\d{3})*,\d{2}", bloco)]
