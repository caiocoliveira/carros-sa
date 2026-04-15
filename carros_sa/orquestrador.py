"""Orquestrador — encadeia todos os agentes para avaliar lotes de um leilão.

Fluxo por lote:
    1. coletar_detalhe → DetalheFlags
    2. early_exit? → descartar
    3. baixar_pdf → /tmp/carros_sa/<lote_id>.pdf
    4. extrair_laudo(pdf, vision_client) → LaudoEstruturado
    5. avaliar(marca, modelo, ano, similares) → SinalMercado
    6. estimar(laudo, empresa) → CustoReforma
    7. _calcular_frete(lote, empresa) → CustoLogistico
    8. precificar(...) → Avaliacao
    9. upsert AvaliacaoLote no SQLite

Scraping é sequencial (anti-bot). Processamento pós-PDF pode ser paralelo
mas nesse MVP roda sequencial para simplicidade.
"""

from __future__ import annotations

import asyncio
import random
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from sqlmodel import Session, select

from carros_sa.agents.avaliador_mercado import avaliar as avaliar_mercado
from carros_sa.agents.estimador_reforma import estimar as estimar_reforma
from carros_sa.agents.extrator_laudo import extrair_laudo
from carros_sa.models import (
    AvaliacaoLote,
    CategoriaVeiculo,
    CustoLogistico,
    LaudoCache,
    LoteRaw,
    Lote,
    SeveridadeAvaria,
    StatusDocumentacao,
    Avaria,
    LaudoEstruturado,
)
from carros_sa.precificador import precificar
from carros_sa.scraping.parsers import parse_card_lines, parse_detalhe
from carros_sa.scraping.scraper_autoavaliar import (
    baixar_pdf,
    coletar_detalhe,
    coletar_listagem,
)
from carros_sa.tenancy import EmpresaConfig, carregar_empresa

# UFs adjacentes a MG (para heurística de frete)
_UFS_ADJACENTES_MG = {"SP", "RJ", "ES", "BA", "GO", "MS", "DF"}


# ---------------------------------------------------------------------------
# Frete heurístico (sem geo lookup externo)
# ---------------------------------------------------------------------------

def _calcular_frete(lote: Lote, empresa: EmpresaConfig) -> CustoLogistico:
    """Estima frete por heurística de UF. Usa tabela YAML da empresa."""
    origem_uf = (lote.origem_uf or "").upper()
    destino_uf = empresa.patio.uf.upper()

    if not origem_uf or origem_uf == destino_uf:
        distancia_km = 150  # mesmo estado
    elif origem_uf in _UFS_ADJACENTES_MG or destino_uf in _UFS_ADJACENTES_MG:
        distancia_km = 400  # estado vizinho
    else:
        distancia_km = 700  # estado distante

    # Categoria do veículo — usa OUTRO se não tiver laudo ainda
    categoria = CategoriaVeiculo.OUTRO
    try:
        # Tenta inferir da marca/modelo (heurística simples)
        modelo_lower = (lote.modelo or "").lower()
        if any(k in modelo_lower for k in ("hilux", "s10", "saveiro", "strada", "ranger")):
            categoria = CategoriaVeiculo.PICAPE
        elif any(k in modelo_lower for k in ("compass", "hr-v", "tracker", "creta", "haval", "evoque")):
            categoria = CategoriaVeiculo.SUV
        elif any(k in modelo_lower for k in ("onix", "hb20", "gol", "fiesta", "polo", "ka ")):
            categoria = CategoriaVeiculo.HATCH
        elif any(k in modelo_lower for k in ("cruze", "corolla", "civic", "jetta")):
            categoria = CategoriaVeiculo.SEDAN
    except Exception:
        pass

    frete = empresa.frete_para(distancia_km, categoria)

    return CustoLogistico(
        origem_cidade=lote.origem_cidade or "Desconhecida",
        origem_uf=origem_uf or "??",
        destino_cidade=empresa.patio.cidade,
        destino_uf=destino_uf,
        distancia_km=distancia_km,
        categoria_veiculo=categoria,
        frete_estimado=frete,
        fonte_cotacao="tabela_empresa",
    )


# ---------------------------------------------------------------------------
# Laudo fallback (quando não há PDF ou visão falha)
# ---------------------------------------------------------------------------

def _laudo_de_textual(txt, flags=None) -> LaudoEstruturado:
    """Constrói LaudoEstruturado a partir da camada textual do PDF (sem diagrama visual).

    Usado quando o PDF existe mas tem <2 páginas ou a visão falha.
    confidence=0.6 — melhor que sem PDF (0.5), pior que extração completa.
    """
    # Documentação: prioriza dados textuais do PDF
    if txt.roubo_furto_ativo or txt.comunicado_venda:
        documentacao = StatusDocumentacao.PENDENCIA_GRAVE
    elif txt.licenciado is False:
        documentacao = StatusDocumentacao.PENDENCIA_LEVE
    elif txt.licenciado is True:
        documentacao = StatusDocumentacao.OK
    else:
        # Fallback: usa flags da página de detalhe
        documentacao = StatusDocumentacao.OK
        if flags is not None:
            sd = (flags.status_documento or "").lower()
            if "pendente" in sd or "irregular" in sd or "débito" in sd:
                documentacao = StatusDocumentacao.PENDENCIA_LEVE
            elif "grave" in sd or "judicial" in sd or "bloqueio" in sd:
                documentacao = StatusDocumentacao.PENDENCIA_GRAVE

    # Motor: usa dado textual se disponível, senão assume OK
    motor_ok = txt.motor_original if txt.motor_original is not None else True

    # Sem diagrama estrutural não sabemos as avarias visuais — severity=NENHUMA
    # mas confidence reflete incerteza
    return LaudoEstruturado(
        avarias=[],
        severidade_geral=SeveridadeAvaria.NENHUMA,
        motor_ok=motor_ok,
        documentacao=documentacao,
        categoria_veiculo=CategoriaVeiculo.OUTRO,
        confidence=0.6,
    )


def _laudo_sem_pdf(flags=None) -> LaudoEstruturado:
    """Laudo neutro quando não há PDF disponível.

    Usa informações da página de detalhe se disponíveis (flags).
    confidence=0.5 = neutro (não pune ausência de PDF como pior caso).
    """
    from carros_sa.scraping.parsers import DetalheFlags

    documentacao = StatusDocumentacao.OK
    if flags is not None:
        sd = (flags.status_documento or "").lower()
        if "pendente" in sd or "irregular" in sd or "débito" in sd:
            documentacao = StatusDocumentacao.PENDENCIA_LEVE
        elif "grave" in sd or "judicial" in sd or "bloqueio" in sd:
            documentacao = StatusDocumentacao.PENDENCIA_GRAVE

    return LaudoEstruturado(
        avarias=[],
        severidade_geral=SeveridadeAvaria.NENHUMA,
        motor_ok=True,
        documentacao=documentacao,
        categoria_veiculo=CategoriaVeiculo.OUTRO,
        confidence=0.5,  # neutro — não sabemos, mas não assumimos pior caso
    )


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------

def _upsert_lote(lote_raw: LoteRaw, session: Session) -> Lote:
    """Persiste ou atualiza Lote no SQLite. Retorna o objeto persistido."""
    existente = session.get(Lote, lote_raw.lote_id)
    row = Lote(
        id=lote_raw.lote_id,
        leilao=lote_raw.leilao,
        url=lote_raw.url,
        marca=lote_raw.marca,
        modelo=lote_raw.modelo,
        ano=lote_raw.ano,
        km=lote_raw.km,
        lance_atual=lote_raw.lance_atual,
        fim_em=lote_raw.fim_em,
        origem_cidade=lote_raw.origem_cidade,
        origem_uf=lote_raw.origem_uf,
        raw_json=lote_raw.model_dump(mode="json"),
        scraped_at=datetime.utcnow(),
    )
    if existente:
        for k, v in row.model_dump(exclude={"id"}).items():
            setattr(existente, k, v)
        session.add(existente)
        return existente
    session.add(row)
    session.flush()  # garante id disponível
    return row


def _upsert_laudo_cache(lote_id: str, laudo: LaudoEstruturado, session: Session, modelo_llm: str = "gemini-flash") -> None:
    """Persiste LaudoCache (global, por lote_id)."""
    existente = session.get(LaudoCache, lote_id)
    dados = dict(
        lote_id=lote_id,
        avarias_json=[a.model_dump() for a in laudo.avarias],
        severidade_geral=laudo.severidade_geral.value,
        motor_ok=laudo.motor_ok,
        documentacao=laudo.documentacao.value,
        categoria_veiculo=laudo.categoria_veiculo.value,
        confidence=laudo.confidence,
        modelo_llm=modelo_llm,
        custo_usd=0.001,
        extraido_em=datetime.utcnow(),
    )
    if existente:
        for k, v in dados.items():
            setattr(existente, k, v)
        session.add(existente)
    else:
        session.add(LaudoCache(**dados))


def _upsert_avaliacao(avaliacao, empresa_id: str, session: Session) -> None:
    """Persiste ou atualiza AvaliacaoLote (por empresa + lote)."""
    existente = session.exec(
        select(AvaliacaoLote)
        .where(AvaliacaoLote.empresa_id == empresa_id)
        .where(AvaliacaoLote.lote_id == avaliacao.lote_id)
    ).first()

    dados = dict(
        empresa_id=empresa_id,
        lote_id=avaliacao.lote_id,
        preco_alvo=avaliacao.preco_alvo,
        preco_max=avaliacao.preco_max,
        score_roi=avaliacao.score_roi,
        fator_risco=avaliacao.fator_risco,
        fator_liquidez=avaliacao.fator_liquidez,
        margem_aplicada=avaliacao.margem_aplicada,
        frete_incluso=avaliacao.frete_incluso,
        reforma_estimada=avaliacao.reforma_estimada,
        taxas_leilao=avaliacao.taxas_leilao,
        preco_giro=avaliacao.preco_giro,
        justificativa=avaliacao.justificativa,
        criado_em=datetime.utcnow(),
    )
    if existente:
        for k, v in dados.items():
            setattr(existente, k, v)
        session.add(existente)
    else:
        session.add(AvaliacaoLote(**dados))


# ---------------------------------------------------------------------------
# Resultado
# ---------------------------------------------------------------------------

@dataclass
class ResultadoLote:
    lote_id: str
    modelo: str
    avaliado: bool
    motivo_descarte: Optional[str] = None
    preco_alvo: Optional[int] = None
    roi_pct: Optional[float] = None
    erro: Optional[str] = None


@dataclass
class OrquestradorResult:
    empresa_id: str
    n_coletados: int = 0
    n_novos: int = 0
    n_avaliados: int = 0
    n_descartados: int = 0
    n_erros: int = 0
    lotes: List[ResultadoLote] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pipeline por lote (async — roda dentro do event loop do orquestrador)
# ---------------------------------------------------------------------------

async def _pipeline_lote(
    lote: Lote,
    page,
    vision_client,
    empresa: EmpresaConfig,
    session: Session,
    tmp_dir: Path,
) -> ResultadoLote:
    """Roda pipeline completo para um lote. Retorna ResultadoLote."""

    modelo_str = f"{lote.marca} {lote.modelo} {lote.ano}"

    # Verifica se já foi avaliado nesta empresa
    ja_avaliado = session.exec(
        select(AvaliacaoLote)
        .where(AvaliacaoLote.empresa_id == empresa.empresa_id)
        .where(AvaliacaoLote.lote_id == lote.id)
    ).first()
    if ja_avaliado:
        return ResultadoLote(lote_id=lote.id, modelo=modelo_str, avaliado=True,
                             preco_alvo=ja_avaliado.preco_alvo,
                             roi_pct=round(ja_avaliado.score_roi * 100, 1))

    try:
        # 1. Detalhe (sleep anti-bot antes de cada request)
        await asyncio.sleep(random.uniform(2.0, 4.0))
        body_text, pdf_url = await coletar_detalhe(page, lote.url)
        flags = parse_detalhe(body_text, pdf_url)

        # 2. Early exit
        if flags.early_exit:
            return ResultadoLote(lote_id=lote.id, modelo=modelo_str,
                                 avaliado=False, motivo_descarte=flags.early_exit)

        # 3. PDF + Laudo
        laudo: LaudoEstruturado
        if pdf_url:
            pdf_dest = tmp_dir / f"{lote.id}.pdf"
            cookies = await page.context.cookies()
            await baixar_pdf(pdf_url, pdf_dest, cookies)
            try:
                # Tentativa 1: extração completa (textual + visão)
                laudo = extrair_laudo(pdf_dest, vision_client)
            except Exception:
                # Tentativa 2: só camada textual (quando PDF tem <2 páginas ou visão falha)
                try:
                    from carros_sa.agents.extrator_laudo import parse_laudo_textual
                    txt = parse_laudo_textual(pdf_dest)
                    laudo = _laudo_de_textual(txt, flags)
                except Exception:
                    # Último recurso: sem PDF
                    laudo = _laudo_sem_pdf(flags)
        else:
            laudo = _laudo_sem_pdf(flags)

        # 4. Persist laudo cache
        _upsert_laudo_cache(lote.id, laudo, session)

        # 5. Mercado
        lote_raw = LoteRaw(
            lote_id=lote.id,
            leilao=lote.leilao,
            url=lote.url,
            marca=lote.marca,
            modelo=lote.modelo,
            ano=lote.ano,
            km=lote.km,
            lance_atual=lote.lance_atual,
            fim_em=lote.fim_em,
            origem_cidade=lote.origem_cidade,
            origem_uf=lote.origem_uf,
        )
        mercado = avaliar_mercado(
            marca=lote.marca,
            modelo=lote.modelo,
            ano=lote.ano,
            km=lote.km,
            similares_precos=flags.similares_precos or None,
            categoria=laudo.categoria_veiculo,
            session=session,
        )

        # 6. Reforma
        reforma = estimar_reforma(laudo, empresa)

        # 7. Frete
        frete = _calcular_frete(lote, empresa)

        # 8. Precificar
        avaliacao = precificar(lote_raw, laudo, mercado, reforma, frete, empresa)

        # 9. Persist
        _upsert_avaliacao(avaliacao, empresa.empresa_id, session)
        session.commit()

        roi_pct = round(avaliacao.score_roi * 100, 1)
        return ResultadoLote(lote_id=lote.id, modelo=modelo_str, avaliado=True,
                             preco_alvo=avaliacao.preco_alvo, roi_pct=roi_pct)

    except Exception as exc:
        session.rollback()
        return ResultadoLote(lote_id=lote.id, modelo=modelo_str,
                             avaliado=False, erro=str(exc))


# ---------------------------------------------------------------------------
# Orquestrador principal
# ---------------------------------------------------------------------------

async def orquestrar(
    empresa_id: str,
    session: Session,
    page,
    vision_client,
    horizonte_dias: int = 7,
) -> OrquestradorResult:
    """
    Coleta listagem, ingere lotes novos e roda pipeline de avaliação.

    Args:
        empresa_id: ID da empresa (ex: "carros_uberlandia")
        session: SQLModel Session já aberta
        page: Playwright Page já autenticada
        vision_client: VisionClient instanciado (Gemini, Anthropic, Ollama)
        horizonte_dias: só lotes com fim em <= N dias

    Returns:
        OrquestradorResult com contagens e detalhes por lote.
    """
    from carros_sa.db import init_db
    init_db()

    empresa = carregar_empresa(empresa_id)
    result = OrquestradorResult(empresa_id=empresa_id)

    # 1. Coleta listagem
    cards = await coletar_listagem(page, empresa, horizonte_dias)
    result.n_coletados = len(cards)

    # 2. Ingesta: parse + upsert Lote
    agora = datetime.now()
    lotes_ids_novos: List[str] = []
    for card in cards:
        try:
            lote_raw = parse_card_lines(card["lines"], card["loteId"], card["href"])
            existente = session.get(Lote, lote_raw.lote_id)
            _upsert_lote(lote_raw, session)
            if not existente:
                lotes_ids_novos.append(lote_raw.lote_id)
        except Exception:
            pass
    session.commit()
    result.n_novos = len(lotes_ids_novos)

    # 3. Pipeline: todos os lotes no banco sem AvaliacaoLote para esta empresa
    #    (inclui scrape de hoje + lotes já persistidos de dias anteriores)
    ids_ja_avaliados = {
        r.lote_id for r in session.exec(
            select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa_id)
        ).all()
    }
    lotes_a_avaliar = [
        l for l in session.exec(select(Lote)).all()
        if l.id not in ids_ja_avaliados
    ]

    tmp_dir = Path(tempfile.mkdtemp(prefix="carros_sa_"))

    for lote in lotes_a_avaliar:
        res = await _pipeline_lote(lote, page, vision_client, empresa, session, tmp_dir)
        result.lotes.append(res)
        if res.erro:
            result.n_erros += 1
        elif res.motivo_descarte:
            result.n_descartados += 1
        else:
            result.n_avaliados += 1

    return result
