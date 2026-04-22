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
import logging
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
from carros_sa.config import get_settings
from carros_sa.errors import (
    FipeIndisponivel,
    LaudoExtractionError,
    PDFDownloadError,
    PDFInvalidoError,
)
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
from carros_sa.scraping.parsers import extrair_loja_do_card, parse_card_lines, parse_detalhe
from carros_sa.scraping.scraper_autoavaliar import (
    baixar_pdf,
    coletar_detalhe,
    coletar_listagem,
)
from carros_sa.tenancy import EmpresaConfig, carregar_empresa

logger = logging.getLogger(__name__)


def _pdf_persistente_path(lote_id: str) -> Path:
    """Path onde o PDF do laudo é salvo — um por lote_id."""
    storage_dir = get_settings().pdf_storage_dir
    storage_dir.mkdir(parents=True, exist_ok=True)
    return storage_dir / f"{lote_id}.pdf"


def _pdf_eh_laudo_valido(pdf_path: Path) -> bool:
    """Heurística barata pra rejeitar PDFs que NÃO são laudos de carro.

    Motivação: em abril/2026 um seletor JS frouxo pegou o link do rodapé
    institucional ("Relatório de Transparência Salarial") em 100% dos lotes e
    contaminou a base inteira. Esta checagem bate no conteúdo textual do PDF
    procurando marcadores típicos de laudo de inspeção veicular.
    """
    settings = get_settings()
    if not pdf_path.exists() or pdf_path.stat().st_size < settings.pdf_min_bytes:
        return False
    import fitz  # PyMuPDF, já é dep do extrator de laudo
    try:
        with fitz.open(str(pdf_path)) as pdf:
            if pdf.page_count == 0:
                return False
            txt = (pdf[0].get_text() or "").upper()
    except (fitz.FileDataError, OSError) as exc:
        logger.warning("PDF corrompido ou inacessível %s: %s", pdf_path, exc)
        return False
    if not any(m in txt for m in settings.pdf_markers_positivos):
        return False
    if any(m in txt for m in settings.pdf_markers_negativos):
        return False
    return True


# ---------------------------------------------------------------------------
# Frete heurístico (sem geo lookup externo)
# ---------------------------------------------------------------------------

def _calcular_frete(lote: Lote, empresa: EmpresaConfig) -> CustoLogistico:
    """Estima frete por distância haversine real (origem do lote → pátio da empresa).

    Fallback pra heurística de UF quando a cidade de origem não está no dataset
    de municípios (ex.: nome grafado de forma inusitada). Caso especial:
    mesma cidade do pátio → distância 0 → frete 0 (comprador busca o carro).
    """
    from carros_sa.tools.geo import buscar_municipio, distancia_haversine_km

    origem_uf = (lote.origem_uf or "").upper()
    origem_cidade = (lote.origem_cidade or "").strip()
    destino_uf = empresa.patio.uf.upper()
    destino_cidade = empresa.patio.cidade

    # 1. Tentativa preferencial: distância real via haversine sobre o dataset IBGE
    distancia_km: Optional[int] = None
    if origem_cidade and origem_uf:
        origem_m = buscar_municipio(origem_cidade, origem_uf)
        destino_m = buscar_municipio(destino_cidade, destino_uf)
        if origem_m and destino_m:
            d = distancia_haversine_km(
                origem_m.latitude, origem_m.longitude,
                destino_m.latitude, destino_m.longitude,
            )
            distancia_km = int(round(d))

    # 2. Fallback: heurística de UF (como era antes)
    if distancia_km is None:
        settings = get_settings()
        if not origem_uf or origem_uf == destino_uf:
            distancia_km = settings.frete_km_mesma_uf
        elif (origem_uf in settings.ufs_adjacentes_mg
              or destino_uf in settings.ufs_adjacentes_mg):
            distancia_km = settings.frete_km_uf_adjacente
        else:
            distancia_km = settings.frete_km_uf_distante

    # Categoria do veículo — heurística por nome do modelo; OUTRO se não casar.
    categoria = CategoriaVeiculo.OUTRO
    modelo_lower = (lote.modelo or "").lower()
    if any(k in modelo_lower for k in ("hilux", "s10", "saveiro", "strada", "ranger")):
        categoria = CategoriaVeiculo.PICAPE
    elif any(k in modelo_lower for k in ("compass", "hr-v", "tracker", "creta", "haval", "evoque")):
        categoria = CategoriaVeiculo.SUV
    elif any(k in modelo_lower for k in ("onix", "hb20", "gol", "fiesta", "polo", "ka ")):
        categoria = CategoriaVeiculo.HATCH
    elif any(k in modelo_lower for k in ("cruze", "corolla", "civic", "jetta")):
        categoria = CategoriaVeiculo.SEDAN

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

    Usado quando o PDF existe mas tem <2 páginas ou a visão falha (ex.: Gemini
    503 UNAVAILABLE). Agora extrai avarias do bloco "Observações" do inspetor —
    menções como "VEÍCULO POSSUI REPARO NAS COLUNAS B e C" viram Avaria concreta
    e a severidade é derivada das peças detectadas.
    """
    from carros_sa.agents.extrator_laudo import (
        extrair_avarias_textuais, _severidade_consolidada,
    )

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

    # Avarias do bloco Observações (texto livre do inspetor) — novo no workstream L
    avarias = extrair_avarias_textuais(txt.observacoes)
    severidade = _severidade_consolidada(avarias)

    # Motor: usa dado textual se disponível. Estrutural degrada motor_ok automaticamente.
    motor_ok = (txt.motor_original if txt.motor_original is not None else True) and \
               severidade != SeveridadeAvaria.ESTRUTURAL

    # Confidence: 0.7 quando extraiu avarias do texto (melhor qualidade); 0.6 só identificadores.
    confidence = 0.7 if avarias else 0.6

    return LaudoEstruturado(
        avarias=avarias,
        severidade_geral=severidade,
        motor_ok=motor_ok,
        documentacao=documentacao,
        categoria_veiculo=CategoriaVeiculo.OUTRO,
        confidence=confidence,
    )


def _laudo_sem_pdf(flags=None) -> LaudoEstruturado:
    """Laudo quando não há PDF disponível.

    Aproveita sinais do scraper de detalhe quando disponíveis:
    - `reprovado_estrutural` do scraper → promove severidade pra ESTRUTURAL
      (sem saber peça específica — o EstimadorReforma aplica o adicional
      estrutural da empresa, suficiente pra descartar no ranking)
    - `itens_reprovados` com menção a peças → cria Avaria explícita quando
      possível
    - `status_documento` → mapeia pra PENDENCIA_LEVE/GRAVE

    Confidence 0.5 neutro, 0.55 se detectou sinal estrutural (sinal mais forte
    que "não sei" mas sem cobertura visual, ainda abaixo de PDF).
    """
    from carros_sa.models import Avaria

    avarias: list = []
    severidade_geral = SeveridadeAvaria.NENHUMA
    documentacao = StatusDocumentacao.OK
    confidence = 0.5

    if flags is not None:
        # Documentação
        sd = (flags.status_documento or "").lower()
        if "pendente" in sd or "irregular" in sd or "débito" in sd:
            documentacao = StatusDocumentacao.PENDENCIA_LEVE
        elif "grave" in sd or "judicial" in sd or "bloqueio" in sd:
            documentacao = StatusDocumentacao.PENDENCIA_GRAVE

        # Sinal estrutural do scraper → severidade ESTRUTURAL mesmo sem PDF.
        # EstimadorReforma aplica adicional estrutural da empresa (R$ ~5k em MG)
        # e o ranking descarta, que é o comportamento correto.
        if getattr(flags, "reprovado_estrutural", False):
            severidade_geral = SeveridadeAvaria.ESTRUTURAL
            avarias.append(Avaria(
                parte="estrutural_indefinido",
                severidade=SeveridadeAvaria.ESTRUTURAL,
                descricao="Scraper detectou REPROVADO ESTRUTURAL no HTML; sem PDF pra detalhar peça",
            ))
            confidence = 0.55

    motor_ok = True and severidade_geral != SeveridadeAvaria.ESTRUTURAL

    return LaudoEstruturado(
        avarias=avarias,
        severidade_geral=severidade_geral,
        motor_ok=motor_ok,
        documentacao=documentacao,
        categoria_veiculo=CategoriaVeiculo.OUTRO,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------

def _persistir_flags_no_lote(
    lote: Lote,
    flags,
    session: Session,
    body_text: Optional[str] = None,
) -> None:
    """Salva o resultado do parse_detalhe em `lote.raw_json["detalhe"]` e promove
    preço-referência Auto Avaliar + % FIPE para colunas first-class do Lote.

    Também alimenta o histórico `PrecoReferenciaAA` — útil pra lotes futuros
    do mesmo modelo que caiam em cidades sem esse dado embutido.

    Quando `body_text` é passado, persiste uma amostra truncada
    (`body_text_sample`, primeiros 8KB) pra debug offline do parser quando
    campos ficarem vazios em massa. Sem isso, só é possível iterar no parser
    raspando Auto Avaliar ao vivo de novo.

    Idempotente: reescrever sobrescreve; múltiplas coletas do mesmo lote
    criam múltiplas linhas de histórico, o que é esperado (série temporal).
    """
    from carros_sa.models import PrecoReferenciaAA
    from carros_sa.scraping.scraper_detalhe import _flags_to_dict

    raw = dict(lote.raw_json or {})
    raw["detalhe"] = _flags_to_dict(flags)
    if body_text:
        # Trunca pra diagnosticar parser sem inflar SQLite (body típico 20-50KB).
        raw["body_text_sample"] = body_text[: get_settings().body_text_sample_bytes]
    lote.raw_json = raw

    if flags.preco_referencia_aa is not None:
        lote.preco_referencia_aa = flags.preco_referencia_aa
        session.add(PrecoReferenciaAA(
            marca=lote.marca,
            modelo=lote.modelo,
            ano=lote.ano,
            preco=flags.preco_referencia_aa,
            fipe_pct_lance_minimo=flags.fipe_pct_lance_minimo,
            origem_lote_id=lote.id,
        ))
    if flags.fipe_pct_lance_minimo is not None:
        lote.fipe_pct_lance_minimo = flags.fipe_pct_lance_minimo

    session.add(lote)


def _upsert_lote(
    lote_raw: LoteRaw, session: Session, *, loja: Optional[str] = None
) -> Lote:
    """Persiste ou atualiza Lote no SQLite. Retorna o objeto persistido.

    `loja` é mergeado em `raw_json["loja"]` quando informado — não faz parte do
    contrato LoteRaw, fica como campo adicional pra sobreviver em reprocessos.
    """
    existente = session.get(Lote, lote_raw.lote_id)
    raw_json = lote_raw.model_dump(mode="json")
    if loja:
        raw_json["loja"] = loja
    elif existente and isinstance(existente.raw_json, dict) and existente.raw_json.get("loja"):
        # Preserva loja de coleta anterior quando a atual não trouxe o dado.
        raw_json["loja"] = existente.raw_json["loja"]
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
        raw_json=raw_json,
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
        preco_giro_fipe=avaliacao.preco_giro_fipe,
        preco_giro_aa=avaliacao.preco_giro_aa,
        fipe=avaliacao.fipe,
        webmotors_mediana=avaliacao.webmotors_mediana,
        dias_giro_estimado=avaliacao.dias_giro_estimado,
        justificativa=avaliacao.justificativa,
        reforma_racional=avaliacao.reforma_racional,
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

class _DescarteLote(Exception):
    """Sinaliza descarte voluntário do lote (early-exit, FIPE indisponível…).

    Levantada pelos estágios pra curto-circuitar o pipeline sem virar erro de
    execução — o handler no `_pipeline_lote` converte em ResultadoLote com
    `motivo_descarte` preenchido.
    """

    def __init__(self, motivo: str):
        self.motivo = motivo
        super().__init__(motivo)


async def _estagio_detalhe(
    lote: Lote,
    page,
    session: Session,
) -> tuple[object, str, Optional[str]]:
    """Raspa página de detalhe, persiste flags + body, devolve (flags, body, pdf_url).

    Levanta `_DescarteLote` se o scraper marcar early_exit ou se a marca não
    está no catálogo FIPE. Em ambos os casos o pipeline deve parar antes do
    laudo (sem gastar LLM).
    """
    settings = get_settings()
    await asyncio.sleep(
        random.uniform(settings.scrape_sleep_min_s, settings.scrape_sleep_max_s)
    )
    body_text, pdf_url = await coletar_detalhe(page, lote.url)
    flags = parse_detalhe(body_text, pdf_url)

    # COMMIT GRANULAR: sem isso, qualquer erro subsequente (FIPE 404, laudo,
    # Webmotors timeout) dispara rollback e apaga o detalhe raspado. Comitando
    # aqui, o reprocessamento vira 1 request.
    _persistir_flags_no_lote(lote, flags, session, body_text=body_text)
    session.commit()

    if flags.early_exit:
        raise _DescarteLote(flags.early_exit)

    # Marca fora do catálogo FIPE (motos, exóticos) — descartar antes do LLM.
    from carros_sa.tools.fipe import marca_fora_do_escopo_fipe
    if marca_fora_do_escopo_fipe(lote.marca):
        raise _DescarteLote(f"marca_fora_fipe_moto: {lote.marca}")

    return flags, body_text, pdf_url


async def _estagio_laudo(
    lote: Lote,
    flags,
    pdf_url: Optional[str],
    page,
    vision_client,
) -> tuple[LaudoEstruturado, Optional[Path]]:
    """Baixa PDF e extrai laudo com fallbacks em cascata.

    Cascata: visão Gemini (PDF válido) → textual (PyMuPDF) → sem-PDF (só flags
    do scraper). Sempre retorna um laudo; pipeline nunca fica sem.
    """
    pdf_dest: Optional[Path] = None
    if pdf_url:
        pdf_dest = _pdf_persistente_path(lote.id)
        cookies = await page.context.cookies()
        try:
            await baixar_pdf(pdf_url, pdf_dest, cookies)
            if not _pdf_eh_laudo_valido(pdf_dest):
                pdf_dest.unlink(missing_ok=True)
                pdf_dest = None
        except Exception as exc:
            logger.warning(
                "baixar_pdf falhou p/ %s (%s: %s) — pipeline segue sem PDF",
                lote.id, type(exc).__name__, exc,
            )
            pdf_dest = None

    if pdf_dest is None:
        return _laudo_sem_pdf(flags), None

    try:
        return extrair_laudo(pdf_dest, vision_client), pdf_dest
    except Exception as exc:
        logger.warning(
            "extrair_laudo visual falhou p/ %s (%s: %s) — fallback textual",
            lote.id, type(exc).__name__, exc,
        )
    try:
        from carros_sa.agents.extrator_laudo import parse_laudo_textual
        txt = parse_laudo_textual(pdf_dest)
        return _laudo_de_textual(txt, flags), pdf_dest
    except Exception as exc:
        logger.warning(
            "parse textual falhou p/ %s (%s: %s) — laudo sem PDF",
            lote.id, type(exc).__name__, exc,
        )
        return _laudo_sem_pdf(flags), pdf_dest


def _estagio_mercado(
    lote: Lote,
    flags,
    laudo: LaudoEstruturado,
    empresa: EmpresaConfig,
    session: Session,
):
    """Consulta FIPE/Webmotors. Levanta `_DescarteLote` se FIPE indisponível."""
    categoria = laudo.categoria_veiculo
    if categoria == CategoriaVeiculo.OUTRO:
        from carros_sa.agents.calibracao_giro import _categoria_de_modelo
        categoria = _categoria_de_modelo(lote.modelo)

    try:
        return avaliar_mercado(
            marca=lote.marca,
            modelo=lote.modelo,
            ano=lote.ano,
            km=lote.km,
            similares_precos=flags.similares_precos or None,
            categoria=categoria,
            session=session,
            empresa_id=empresa.empresa_id,
        )
    except LookupError as exc:
        # FIPE Parallelum não cobre motos (Dafra, Triumph, Harley) nem exóticos
        # fora de linha. Sem FIPE perde a âncora — descartar é mais correto
        # que bloquear a planilha inteira.
        raise _DescarteLote(f"fipe_indisponivel: {exc}") from exc


def _estagio_reforma(
    lote: Lote,
    laudo: LaudoEstruturado,
    pdf_dest: Optional[Path],
    empresa: EmpresaConfig,
    text_llm_client,
):
    """Estima reforma via facade — LLM enriquecido com observações se disponível."""
    observacoes = ""
    if text_llm_client is not None and pdf_dest and pdf_dest.exists():
        try:
            from carros_sa.agents.extrator_laudo import parse_laudo_textual
            observacoes = parse_laudo_textual(pdf_dest).observacoes or ""
        except Exception as exc:
            logger.debug(
                "observações do laudo indisponíveis p/ %s (%s): prompt sem enriquecimento",
                lote.id, type(exc).__name__,
            )
    return estimar_reforma(
        laudo, empresa,
        llm_client=text_llm_client,
        lote_info={
            "marca": lote.marca, "modelo": lote.modelo, "ano": lote.ano,
            "km": lote.km, "lance_atual": lote.lance_atual,
        },
        observacoes_pdf=observacoes,
    )

async def _pipeline_lote(
    lote: Lote,
    page,
    vision_client,
    empresa: EmpresaConfig,
    session: Session,
    tmp_dir: Path,
    text_llm_client=None,
) -> ResultadoLote:
    """Roda pipeline completo para um lote. Retorna ResultadoLote."""

    modelo_str = f"{lote.marca} {lote.modelo} {lote.ano}"

    # Short-circuit: só pula o pipeline se ESTÁ realmente completo — avaliação
    # existente E laudo com confiança ≥ 0.6. Antes, bastava ter AvaliacaoLote,
    # o que tornava o flag `--somente-laudo-pendente` do script de retry
    # inofensivo: ele escolhia lotes com conf<0.6, mas aqui o pipeline saía
    # sem re-raspar detalhe nem re-extrair laudo. Resultado: 104 lotes ativos
    # com AvaliacaoLote stale + LaudoCache placeholder (conf=0.5, detalhe
    # ausente) nunca reprocessavam.
    ja_avaliado = session.exec(
        select(AvaliacaoLote)
        .where(AvaliacaoLote.empresa_id == empresa.empresa_id)
        .where(AvaliacaoLote.lote_id == lote.id)
    ).first()
    laudo_atual = session.get(LaudoCache, lote.id)
    threshold = get_settings().laudo_confidence_ok_threshold
    laudo_ok = laudo_atual is not None and (laudo_atual.confidence or 0) >= threshold
    if ja_avaliado and laudo_ok:
        return ResultadoLote(lote_id=lote.id, modelo=modelo_str, avaliado=True,
                             preco_alvo=ja_avaliado.preco_alvo,
                             roi_pct=round(ja_avaliado.score_roi * 100, 1))

    try:
        flags, _body, pdf_url = await _estagio_detalhe(lote, page, session)
        laudo, pdf_dest = await _estagio_laudo(lote, flags, pdf_url, page, vision_client)
        _upsert_laudo_cache(lote.id, laudo, session)

        mercado = _estagio_mercado(lote, flags, laudo, empresa, session)
        reforma = _estagio_reforma(lote, laudo, pdf_dest, empresa, text_llm_client)
        frete = _calcular_frete(lote, empresa)

        lote_raw = LoteRaw(
            lote_id=lote.id, leilao=lote.leilao, url=lote.url,
            marca=lote.marca, modelo=lote.modelo, ano=lote.ano, km=lote.km,
            lance_atual=lote.lance_atual, fim_em=lote.fim_em,
            origem_cidade=lote.origem_cidade, origem_uf=lote.origem_uf,
        )
        avaliacao = precificar(lote_raw, laudo, mercado, reforma, frete, empresa)
        _upsert_avaliacao(avaliacao, empresa.empresa_id, session)
        session.commit()

        return ResultadoLote(
            lote_id=lote.id, modelo=modelo_str, avaliado=True,
            preco_alvo=avaliacao.preco_alvo,
            roi_pct=round(avaliacao.score_roi * 100, 1),
        )

    except _DescarteLote as descarte:
        return ResultadoLote(
            lote_id=lote.id, modelo=modelo_str,
            avaliado=False, motivo_descarte=descarte.motivo,
        )
    except Exception as exc:
        # Safety net: um lote ruim não pode derrubar o batch. Log estruturado
        # via logger (antes era print no stderr — causou mistério de "21 erros
        # silenciosos" em 2026-04-16 quando o logger não tinha sido configurado).
        logger.error(
            "pipeline_lote ERRO em %s (%s): %s: %s",
            lote.id, modelo_str, type(exc).__name__, exc,
            exc_info=True,
        )
        session.rollback()
        return ResultadoLote(lote_id=lote.id, modelo=modelo_str,
                             avaliado=False, erro=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Orquestrador principal
# ---------------------------------------------------------------------------

async def orquestrar(
    empresa_id: str,
    session: Session,
    page,
    vision_client,
    horizonte_dias: int = 7,
    text_llm_client=None,
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
            loja = extrair_loja_do_card(card["lines"])
            existente = session.get(Lote, lote_raw.lote_id)
            _upsert_lote(lote_raw, session, loja=loja)
            if not existente:
                lotes_ids_novos.append(lote_raw.lote_id)
        except Exception as exc:
            # Um card com schema inesperado não pode derrubar a ingesta toda.
            logger.warning(
                "ingesta: card %s ignorado (%s: %s)",
                card.get("loteId", "?"), type(exc).__name__, exc,
            )
    session.commit()
    result.n_novos = len(lotes_ids_novos)

    # 3. Pipeline: todos os lotes no banco sem AvaliacaoLote para esta empresa
    #    (inclui scrape de hoje + lotes já persistidos de dias anteriores)
    ids_ja_avaliados = {
        r.lote_id for r in session.exec(
            select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa_id)
        ).all()
    }
    # Exclui Lotes sintéticos importados via `arrematado-import` (leilao="historico_offline")
    # — não têm URL real; só existem pra preservar FK do Arrematado.
    lotes_a_avaliar = [
        l for l in session.exec(select(Lote)).all()
        if l.id not in ids_ja_avaliados and l.leilao != "historico_offline"
    ]

    tmp_dir = Path(tempfile.mkdtemp(prefix="carros_sa_"))

    for lote in lotes_a_avaliar:
        res = await _pipeline_lote(
            lote, page, vision_client, empresa, session, tmp_dir,
            text_llm_client=text_llm_client,
        )
        result.lotes.append(res)
        if res.erro:
            result.n_erros += 1
        elif res.motivo_descarte:
            result.n_descartados += 1
        else:
            result.n_avaliados += 1

    return result
