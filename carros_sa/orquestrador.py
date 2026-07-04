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
from carros_sa.agents.estimador_reforma_llm import estimar_llm as estimar_reforma_llm
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
from carros_sa.scraping.parsers import extrair_loja_do_card, parse_card_lines, parse_detalhe
from carros_sa.scraping.scraper_autoavaliar import (
    baixar_pdf,
    coletar_detalhe,
    coletar_listagem,
)
from carros_sa.tenancy import EmpresaConfig, carregar_empresa
from carros_sa.tools.sheets import _lucro_absoluto_efetivo

# UFs adjacentes a MG (para heurística de frete)
_UFS_ADJACENTES_MG = {"SP", "RJ", "ES", "BA", "GO", "MS", "DF"}

# Diretório permanente de PDFs de laudo. Fora de tmp_dir pra permitir reprocessamento
# offline (sem re-baixar, sem re-autenticar) via `scripts/reprocessar_laudos.py`.
_PDF_STORAGE_DIR = Path(__file__).resolve().parent.parent / "data" / "laudos_pdfs"


def _pdf_persistente_path(lote_id: str) -> Path:
    """Path onde o PDF do laudo é salvo — um por lote_id."""
    _PDF_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return _PDF_STORAGE_DIR / f"{lote_id}.pdf"


def _pdf_eh_laudo_valido(pdf_path: Path) -> bool:
    """Heurística barata pra rejeitar PDFs que NÃO são laudos de carro.

    Motivação: em abril/2026 um seletor JS frouxo pegou o link do rodapé
    institucional ("Relatório de Transparência Salarial") em 100% dos lotes e
    contaminou a base inteira. Esta checagem bate no conteúdo textual do PDF
    procurando marcadores típicos de laudo de inspeção veicular.
    """
    if not pdf_path.exists() or pdf_path.stat().st_size < 5_000:
        return False
    try:
        import fitz  # PyMuPDF, já é dep do extrator de laudo
        with fitz.open(str(pdf_path)) as pdf:
            if pdf.page_count == 0:
                return False
            txt = (pdf[0].get_text() or "").upper()
    except Exception:
        return False
    # Marcadores positivos — qualquer um bate
    positivos = ("LAUDO", "INSPEÇÃO", "INSPECAO", "AVALIAÇÃO", "AVALIACAO", "VEÍCULO", "VEICULO", "CHASSI", "PLACA")
    if not any(m in txt for m in positivos):
        return False
    # Marcadores negativos — se aparece, NÃO é laudo
    negativos = ("TRANSPARÊNCIA SALARIAL", "TRANSPARENCIA SALARIAL", "IGUALDADE SALARIAL", "RELATÓRIO DE TRANSPARÊNCIA")
    if any(m in txt for m in negativos):
        return False
    return True


# ---------------------------------------------------------------------------
# Frete heurístico (sem geo lookup externo)
# ---------------------------------------------------------------------------

def _calcular_frete(
    lote: Lote,
    empresa: EmpresaConfig,
    categoria: Optional[CategoriaVeiculo] = None,
) -> CustoLogistico:
    """Estima frete por distância haversine real (origem do lote → pátio da empresa).

    Fallback pra heurística de UF quando a cidade de origem não está no dataset
    de municípios (ex.: nome grafado de forma inusitada). Caso especial:
    mesma cidade do pátio → distância 0 → frete 0 (comprador busca o carro).

    `categoria` é opcional pra retrocompat dos testes; em produção, o
    `_pipeline_lote` SEMPRE passa a categoria já resolvida (laudo + fallback
    `_categoria_de_modelo`). Quando `None`, derivamos por uma heurística
    LOCAL idêntica à do `calibracao_giro._categoria_de_modelo` pra evitar
    drift entre frete e dias_giro (Toro era PICAPE no calibrador mas OUTRO
    no frete antigo).
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
        if not origem_uf or origem_uf == destino_uf:
            distancia_km = 150
        elif origem_uf in _UFS_ADJACENTES_MG or destino_uf in _UFS_ADJACENTES_MG:
            distancia_km = 400
        else:
            distancia_km = 700

    # Categoria — preferência pelo argumento já resolvido (laudo + fallback do
    # pipeline). Sem ele, derivamos da MESMA tabela usada por calibracao_giro
    # pra evitar divergência (Toro: PICAPE em ambos; Pulse/Tucson: SUV em ambos).
    if categoria is None:
        from carros_sa.agents.calibracao_giro import _categoria_de_modelo
        categoria = _categoria_de_modelo(lote.modelo or "")

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
        # Trunca pra 8KB — suficiente pra diagnosticar parser sem inflar SQLite.
        # Típico body_text de página Auto Avaliar: 20-50KB.
        raw["body_text_sample"] = body_text[:8000]
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

    Preserva também `detalhe` e `body_text_sample` da coleta anterior — esses
    campos são populados por `_persistir_flags_no_lote` (após `coletar_detalhe`)
    e o LoteRaw da listagem não os carrega. Sem esta preservação, todo
    re-scrape da listagem ZERAVA `detalhe.laudo_pdf_url` (e o resto do detalhe)
    e, junto com o short-circuit `ja_avaliado AND laudo_ok AND pdf_ok` em
    `_pipeline_lote`, o pipeline NÃO re-coletava detalhe — então a URL ficava
    None até o lote encerrar. Sintoma: 95/187 lotes ativos com
    `url_invalida_ou_ausente` em `auditar_laudos` (cache + PDF presentes,
    URL=None) — coluna "Ver laudo" da planilha sumia silenciosamente.
    """
    existente = session.get(Lote, lote_raw.lote_id)
    raw_json = lote_raw.model_dump(mode="json")
    if loja:
        raw_json["loja"] = loja
    elif existente and isinstance(existente.raw_json, dict) and existente.raw_json.get("loja"):
        # Preserva loja de coleta anterior quando a atual não trouxe o dado.
        raw_json["loja"] = existente.raw_json["loja"]
    if existente and isinstance(existente.raw_json, dict):
        for k in ("detalhe", "body_text_sample"):
            v = existente.raw_json.get(k)
            if v is not None:
                raw_json[k] = v
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
    """Persiste LaudoCache (global, por lote_id).

    `tentativas_extracao` é circuit-breaker: incrementa em extração fraca
    (confidence < 0.6 — input ruim, LLM nada pôde fazer), zera em sucesso
    (>= 0.6). Filtro de retry consulta pra evitar perma-loop em lotes com
    body_text vazio ou PDF inacessível.
    """
    existente = session.get(LaudoCache, lote_id)
    tentativas_prev = (existente.tentativas_extracao if existente else 0) or 0
    tentativas_novo = 0 if laudo.confidence >= 0.6 else tentativas_prev + 1
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
        tentativas_extracao=tentativas_novo,
    )
    if existente:
        for k, v in dados.items():
            setattr(existente, k, v)
        session.add(existente)
    else:
        session.add(LaudoCache(**dados))


def _upsert_avaliacao(
    avaliacao, empresa_id: str, session: Session, *, mercado=None,
) -> None:
    """Persiste ou atualiza AvaliacaoLote (por empresa + lote).

    `mercado` (SinalMercado, opcional) é usado pra propagar
    `n_anuncios_competidores` → `AvaliacaoLote.webmotors_n_anuncios` (workstream G).
    Quando ausente (testes antigos), o campo fica None.
    """
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
        webmotors_n_anuncios=(mercado.n_anuncios_competidores if mercado else None),
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
    # Lucro absoluto EFETIVO (R$ que sobram no fim), basis `_score_roi_efetivo`
    # — cai em zona apertada (lance > alvo). Mesma métrica que sheets, cli.top e
    # audit rankeam (paridade P5b — LESSONS.md/P5b). Antes do fix de 2026-07-04
    # o Top da rodada rankeava por `roi_pct` (intrinsic ROI), divergindo do que
    # o operador via ao abrir a planilha logo depois — cognitive dissonance.
    lucro_absoluto: Optional[int] = None
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
    text_llm_client=None,
) -> ResultadoLote:
    """Roda pipeline completo para um lote. Retorna ResultadoLote."""

    modelo_str = f"{lote.marca} {lote.modelo} {lote.ano}"

    # Short-circuit: só pula o pipeline se ESTÁ realmente completo — avaliação
    # existente E laudo com confiança ≥ 0.6 E PDF on-disk. Antes, bastava ter
    # AvaliacaoLote, o que tornava o flag `--somente-laudo-pendente` do script
    # de retry inofensivo: ele escolhia lotes com conf<0.6, mas aqui o pipeline
    # saía sem re-raspar detalhe nem re-extrair laudo. Resultado: 104 lotes
    # ativos com AvaliacaoLote stale + LaudoCache placeholder (conf=0.5,
    # detalhe ausente) nunca reprocessavam.
    #
    # Adição 2026-05-09: também exige PDF on-disk. Caso de uso CI workflow —
    # state/db agora persiste os PDFs, mas se a restauração da subárvore
    # `laudos_pdfs/` falhar (network glitch, corrupção da branch), o lote tem
    # cache>=0.6 no DB mas o arquivo não está em data/laudos_pdfs/. O retry
    # script abaixo passa a incluir esse caso na fila; aqui o pipeline deixa
    # de pulá-lo, fechando o laço da auditoria.
    #
    # Adição 2026-05-10: também exige `detalhe.laudo_pdf_url` válido em
    # raw_json. O bug raiz (`_upsert_lote` zerando `detalhe` em todo re-scrape
    # da listagem) foi corrigido, mas o short-circuit precisa cobrir TODAS as
    # 3 condições da auditoria — caso contrário lotes com URL faltante (por
    # qualquer motivo: bug futuro, decoy limpado, raw_json corrompido) ficam
    # presos sem reprocessar. Defesa em profundidade: pipeline re-roda pra
    # re-popular a URL.
    ja_avaliado = session.exec(
        select(AvaliacaoLote)
        .where(AvaliacaoLote.empresa_id == empresa.empresa_id)
        .where(AvaliacaoLote.lote_id == lote.id)
    ).first()
    laudo_atual = session.get(LaudoCache, lote.id)
    laudo_ok = laudo_atual is not None and (laudo_atual.confidence or 0) >= 0.6
    pdf_path = _pdf_persistente_path(lote.id)
    pdf_ok = pdf_path.exists() and pdf_path.stat().st_size > 5_000
    detalhe_atual = (lote.raw_json or {}).get("detalhe") if isinstance(lote.raw_json, dict) else None
    url_atual = detalhe_atual.get("laudo_pdf_url") if isinstance(detalhe_atual, dict) else None
    from carros_sa.scraping.parsers import is_laudo_pdf_url
    url_ok = is_laudo_pdf_url(url_atual)
    if ja_avaliado and laudo_ok and pdf_ok and url_ok:
        # `_lucro_absoluto_efetivo` é duck-typed (usa só .score_roi, .preco_giro,
        # .preco_alvo) — funciona tanto com AvaliacaoLote (SQLModel) quanto com
        # Avaliacao (Pydantic). Import consolidado no topo do módulo desde
        # 2026-07-04 (review PR #108 confirmou que não há ciclo — sheets NÃO
        # importa orquestrador direta ou indiretamente).
        lucro_absoluto = _lucro_absoluto_efetivo(ja_avaliado, lote.lance_atual)
        return ResultadoLote(lote_id=lote.id, modelo=modelo_str, avaliado=True,
                             preco_alvo=ja_avaliado.preco_alvo,
                             roi_pct=round(ja_avaliado.score_roi * 100, 1),
                             lucro_absoluto=lucro_absoluto)

    try:
        # 1. Detalhe (sleep anti-bot antes de cada request).
        # Subido de (2-4s) pra (5-8s) após observar 49/55 requests caírem com
        # HTTP 429 no baixar_pdf na triagem de 2026-04-16. Auto Avaliar tem
        # rate-limit agressivo no download de laudos.
        await asyncio.sleep(random.uniform(5.0, 8.0))
        # text_llm_client é passado como fallback pra leiloeiros novos cujo
        # layout DOM não bate com a allowlist atual — ver passada 8 em
        # `coletar_detalhe`. Quando None, o fallback simplesmente não roda.
        body_text, pdf_url = await coletar_detalhe(page, lote.url, llm_client=text_llm_client)
        flags = parse_detalhe(body_text, pdf_url)

        # 1b. Persiste flags do detalhe + preços da Tabela Auto Avaliar no Lote.
        # COMMIT GRANULAR: sem isso, qualquer erro subsequente (ex.: FIPE 404
        # pra moto, falha de laudo, Webmotors timeout) dispara session.rollback()
        # e **apaga também o detalhe raspado**, forçando re-scrape desnecessário.
        # Comitando aqui, o detalhe fica salvo e o reprocessamento vira 1-request.
        _persistir_flags_no_lote(lote, flags, session, body_text=body_text)
        session.commit()

        # 2. Early exit
        if flags.early_exit:
            return ResultadoLote(lote_id=lote.id, modelo=modelo_str,
                                 avaliado=False, motivo_descarte=flags.early_exit)

        # 2b. Marca fora do catálogo FIPE (Triumph, Harley, Ducati, etc — motos).
        # Sem FIPE não conseguimos ancorar preço. Descartamos AQUI em vez de
        # gastar LLM no laudo + bater num LookupError lá na frente. Motivo claro
        # na planilha, fluxo não quebra.
        from carros_sa.tools.fipe import marca_fora_do_escopo_fipe
        if marca_fora_do_escopo_fipe(lote.marca):
            return ResultadoLote(
                lote_id=lote.id, modelo=modelo_str, avaliado=False,
                motivo_descarte=f"marca_fora_fipe_moto: {lote.marca}",
            )

        # 3. PDF + Laudo
        # Persistimos o PDF em data/laudos_pdfs/<lote>.pdf (não em tmp_dir) —
        # assim reprocessar_laudos.py pode re-rodar extração sem baixar de novo,
        # e dá pra auditar manualmente o PDF depois. PDF é pequeno (~300KB) e o
        # volume total (~600 lotes/mês × 300KB = 180MB/mês) é gerenciável.
        laudo: LaudoEstruturado
        pdf_dest: Optional[Path] = None
        if pdf_url:
            pdf_dest = _pdf_persistente_path(lote.id)
            cookies = await page.context.cookies()
            try:
                await baixar_pdf(pdf_url, pdf_dest, cookies)
                if not _pdf_eh_laudo_valido(pdf_dest):
                    # PDF baixado não é um laudo de carro (ex.: footer institucional
                    # pego por seletor frouxo no passado) — descarta e trata como sem PDF.
                    pdf_dest.unlink(missing_ok=True)
                    pdf_dest = None
                    # Zera a URL persistida em raw_json: ela apontou pra um arquivo
                    # que NÃO é laudo, então deixá-la lá envenena (a) o exporter,
                    # que renderia HYPERLINK clicável pro PDF errado, e (b) o retry
                    # diário, que re-baixaria o mesmo arquivo inválido. Combina com
                    # `limpar_decoys_laudo` (defesa retroativa) — aqui a defesa é
                    # contemporânea, dentro do próprio pipeline.
                    raw_atual = dict(lote.raw_json or {})
                    det_atual = dict(raw_atual.get("detalhe") or {})
                    det_atual["laudo_pdf_url"] = None
                    raw_atual["detalhe"] = det_atual
                    lote.raw_json = raw_atual
                    session.add(lote)
                    session.commit()
            except Exception:
                pdf_dest = None

        if pdf_dest is not None:
            try:
                # Tentativa 1: extração completa (textual + visão + LLM textual
                # vendor-agnostic). `text_llm_client` ativa a camada 4 do
                # extrator — fallback que cobre vendors fora do template AA
                # (DEKRA / Procemax / SA-Laudo / Vistoria Cautelar genérica)
                # cujos PDFs faziam Gemini visual devolver confidence=0.0,
                # condenando o lote ao bucket `cache_confianca_baixa` em runs
                # subsequentes (22/47 incompletos em produção 2026-05-15).
                laudo = extrair_laudo(pdf_dest, vision_client, text_llm_client=text_llm_client)
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
        # Categoria: prefere o que o laudo (visão Gemini) classificou; mas se voltar
        # OUTRO (gênero indefinido), usa heurística por nome do modelo — assim o
        # Compass vira SUV e Toro vira PICAPE, ativando a calibração correta de dias_giro.
        categoria = laudo.categoria_veiculo
        if categoria == CategoriaVeiculo.OUTRO:
            from carros_sa.agents.calibracao_giro import _categoria_de_modelo
            categoria = _categoria_de_modelo(lote.modelo)

        try:
            # Workstream G: similares do AA deixaram de ser fonte de mediana
            # (poluídos por outliers categóricos). Mediana vem do cache
            # Webmotors via `session` (TTL 24h, populado pelo cron noturno
            # `carros-sa webmotors-coletar`). Sem cache → mediana=FIPE neutra,
            # display mostra "—" via `n_anuncios_competidores=0`.
            mercado = avaliar_mercado(
                marca=lote.marca,
                modelo=lote.modelo,
                ano=lote.ano,
                km=lote.km,
                categoria=categoria,
                session=session,
                empresa_id=empresa.empresa_id,
            )
        except LookupError as exc:
            # FIPE Parallelum não tem catálogo de motos (Dafra, Triumph, Harley…)
            # nem modelos exóticos fora de linha. Sem FIPE o precificador perde
            # a âncora — descartar é mais correto que falhar com exceção e
            # bloquear a planilha inteira. Lote fica visível com motivo claro.
            return ResultadoLote(
                lote_id=lote.id, modelo=modelo_str, avaliado=False,
                motivo_descarte=f"fipe_indisponivel: {exc}",
            )

        # 6. Reforma — LLM quando disponível (itens específicos por carro),
        # fallback interno pro determinístico se LLM falha/JSON ruim. Sem LLM
        # configurado, determinístico direto.
        if text_llm_client is not None:
            # Observações do inspetor enriquecem o prompt quando temos o PDF.
            # `pdf_dest` é None se não havia URL, download falhou OU validação
            # rejeitou (vide bloco acima). Checa antes de chamar `.exists()` —
            # senão `None.exists()` levantava AttributeError silencioso (engolido
            # pelo try/except interno mas confundia debug).
            observacoes = ""
            if pdf_dest is not None and pdf_dest.exists():
                try:
                    from carros_sa.agents.extrator_laudo import parse_laudo_textual
                    observacoes = parse_laudo_textual(pdf_dest).observacoes or ""
                except Exception:
                    observacoes = ""
            reforma = estimar_reforma_llm(
                laudo=laudo,
                lote_info={
                    "marca": lote.marca, "modelo": lote.modelo, "ano": lote.ano,
                    "km": lote.km, "lance_atual": lote.lance_atual,
                },
                empresa=empresa,
                llm_client=text_llm_client,
                observacoes_pdf=observacoes,
            )
        else:
            reforma = estimar_reforma(laudo, empresa)

        # 7. Frete — passa categoria já resolvida (laudo + fallback _categoria_de_modelo)
        # pra alinhar com a categoria usada na calibração de dias_giro. Sem isso, o
        # frete recalculava com lista mais curta (ex.: Toro virava OUTRO no frete e
        # PICAPE na calibração) e devolvia valor sub/superestimado quando a tabela
        # de frete diferenciava as categorias.
        frete = _calcular_frete(lote, empresa, categoria=categoria)

        # 8. Precificar
        avaliacao = precificar(lote_raw, laudo, mercado, reforma, frete, empresa)

        # 9. Persist
        _upsert_avaliacao(avaliacao, empresa.empresa_id, session, mercado=mercado)
        session.commit()

        roi_pct = round(avaliacao.score_roi * 100, 1)
        # Paridade P5b com ranking do sheets/audit/top: Lucro absoluto EFETIVO
        # (basis score_efetivo — cai em zona apertada). Duck-typed com Avaliacao
        # (Pydantic) que tem os mesmos campos consumidos pelo helper.
        lucro_absoluto = _lucro_absoluto_efetivo(avaliacao, lote_raw.lance_atual)
        return ResultadoLote(lote_id=lote.id, modelo=modelo_str, avaliado=True,
                             preco_alvo=avaliacao.preco_alvo, roi_pct=roi_pct,
                             lucro_absoluto=lucro_absoluto)

    except Exception as exc:
        # Log estruturado pra debug: tipo da exceção + mensagem. Imprime antes
        # do rollback pra garantir que o usuário veja qual lote falhou e por quê
        # (sem esse print, o erro ficava só no ResultadoLote.erro, invisível
        # durante o run — causou misterio de "21 erros silenciosos" em 2026-04-16).
        import sys
        print(
            f"[pipeline_lote] ERRO em {lote.id} ({modelo_str}): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr, flush=True,
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
    horizonte_dias: Optional[int] = None,
    text_llm_client=None,
) -> OrquestradorResult:
    """
    Coleta listagem, ingere lotes novos e roda pipeline de avaliação.

    Args:
        empresa_id: ID da empresa (ex: "carros_uberlandia")
        session: SQLModel Session já aberta
        page: Playwright Page já autenticada
        vision_client: VisionClient instanciado (Gemini, Anthropic, Ollama)
        horizonte_dias: filtro opcional de coleta — se setado, descarta lotes
            com fim > N dias. Default `None` = coleta tudo que aparece na
            listagem; janela de exibição fica a cargo do `SheetsExporter`.

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
    n_cards_dropped = 0
    for card in cards:
        try:
            lote_raw = parse_card_lines(card["lines"], card["loteId"], card["href"])
            loja = extrair_loja_do_card(card["lines"])
            existente = session.get(Lote, lote_raw.lote_id)
            _upsert_lote(lote_raw, session, loja=loja)
            if not existente:
                lotes_ids_novos.append(lote_raw.lote_id)
        except Exception as exc:
            # Antes engolíamos silenciosamente — se 5 de 50 cards falhavam parse
            # (ex.: AA mudou layout), `n_coletados=50` mas `n_novos=45` sem
            # nenhum sinal pro operador. Imprime stderr (vai pro log do workflow
            # e pro artifact) com loteId + tipo do erro pra rastreabilidade.
            n_cards_dropped += 1
            import sys
            print(
                f"[orquestrador] card parse failed loteId={card.get('loteId')!r} "
                f"href={card.get('href')!r}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    session.commit()
    result.n_novos = len(lotes_ids_novos)
    if n_cards_dropped > 0:
        import sys
        print(
            f"[orquestrador] {n_cards_dropped}/{len(cards)} cards descartados por parse error "
            "(ver linhas anteriores)",
            file=sys.stderr,
        )

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
