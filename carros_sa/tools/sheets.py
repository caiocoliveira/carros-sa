"""Exportador Google Sheets — escreve triagem de lotes numa Sheet compartilhada.

Uso típico:
    exporter = SheetsExporter(spreadsheet_id, credentials_path)
    n = exporter.exportar("uberlandia_mg", session)

Cada empresa_id vira uma aba separada na mesma Sheet. A aba é sobrescrita a cada
chamada (limpa + reescreve), então sempre reflete o estado atual do SQLite.

Setup one-time:
    1. Google Cloud → criar Service Account → baixar JSON (salvar FORA do repo)
    2. Criar Google Sheet → copiar ID da URL
    3. Compartilhar Sheet com o e-mail do service account (papel Editor)
    4. Setar no .env: GOOGLE_SHEETS_ID e GOOGLE_SERVICE_ACCOUNT_PATH
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from sqlmodel import Session, select

from carros_sa.models import AvaliacaoLote, CategoriaVeiculo, LaudoCache, Lote
from carros_sa.scraping.parsers import is_laudo_pdf_url
from carros_sa.tenancy import carregar_empresa
from carros_sa.tools.laudo_audit import PDF_DIR_DEFAULT, verificar_laudo_completo

HEADER = [
    "Rank",
    "Situação",
    "Marca",
    "Modelo",
    "Ano",
    "Cidade",
    "Loja",
    "Fim do Leilão",
    "KM",
    "KM/ano",
    "Lance Atual (R$)",
    "Lance Máximo (R$)",
    "FIPE (R$)",
    "Mediana mercado (R$)",
    "Lucro (R$)",
    "ROI alvo (%)",
    "Reforma (R$)",
    "Racional Reforma",
    "Anúncio",
    "Laudo",
]

# Formato numérico explícito por coluna. Necessário porque `ws.clear()` apaga
# valores mas PRESERVA formatação de célula — colunas cuja posição já foi
# ocupada por "Atualizado em" em versões anteriores do HEADER herdaram formato
# DATE, e inteiros (R$) passaram a ser renderizados como datas (número serial).
# Reaplicar NUMBER a cada export desfaz a contaminação histórica e blinda
# contra reordenações futuras.
_NUMBER_INTEIRO = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}
_NUMBER_DECIMAL_1 = {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}

COLUMN_FORMATS = {
    "KM": _NUMBER_INTEIRO,
    "KM/ano": _NUMBER_INTEIRO,
    "Lance Atual (R$)": _NUMBER_INTEIRO,
    "Lance Máximo (R$)": _NUMBER_INTEIRO,
    "FIPE (R$)": _NUMBER_INTEIRO,
    "Mediana mercado (R$)": _NUMBER_INTEIRO,
    "Lucro (R$)": _NUMBER_INTEIRO,
    "Reforma (R$)": _NUMBER_INTEIRO,
    "ROI alvo (%)": _NUMBER_DECIMAL_1,
}

# Thresholds de km/ano pra cor de fundo da coluna KM. Calibração:
# - ≤15k/ano: média brasileira (~15k) ou abaixo → carro conservado.
# - 15-25k/ano: uso típico-alto (família grande, comute longo) — sem alarme,
#   mas operador deve dar uma olhada.
# - >25k/ano: uso intensivo, alta probabilidade de frota/Uber/táxi —
#   desgaste mecânico desproporcional, revenda mais difícil.
_KM_POR_ANO_VERDE_MAX = 15_000
_KM_POR_ANO_AMARELO_MAX = 25_000

# Cores RGB (0-1) pra background de célula. Tons claros pra manter o número
# legível por cima. Espelham a paleta default das condicionais do Sheets.
_COR_VERDE = {"red": 0.85, "green": 0.92, "blue": 0.83}
_COR_AMARELO = {"red": 0.99, "green": 0.91, "blue": 0.70}
_COR_VERMELHO = {"red": 0.96, "green": 0.80, "blue": 0.80}
_COR_BRANCO = {"red": 1.0, "green": 1.0, "blue": 1.0}

_COR_POR_INDICADOR_KM = {
    "verde": _COR_VERDE,
    "amarelo": _COR_AMARELO,
    "vermelho": _COR_VERMELHO,
}


def _km_por_ano(km: Optional[int], ano: Optional[int], ano_atual: int) -> Optional[int]:
    """km/idade arredondado, com idade floor=1.

    None quando km ou ano ausente. Carro do ano-corrente vira idade=1 (não
    divide por zero); modelo futuro idem. Resultado serve tanto pra colorir
    a célula KM (`_km_indicator`) quanto pra exibir como número na coluna
    "KM/ano".
    """
    if km is None or km < 0 or ano is None:
        return None
    idade = max(1, ano_atual - ano)
    return round(km / idade)


def _km_indicator(km: Optional[int], ano: Optional[int], ano_atual: int) -> Optional[str]:
    """Classifica o KM do lote pelo km/ano em "verde" / "amarelo" / "vermelho".

    Devolve None quando faltam dados (km ou ano None) — célula fica sem cor
    em vez de chutar uma classificação enganosa.
    """
    kpa = _km_por_ano(km, ano, ano_atual)
    if kpa is None:
        return None
    if kpa <= _KM_POR_ANO_VERDE_MAX:
        return "verde"
    if kpa <= _KM_POR_ANO_AMARELO_MAX:
        return "amarelo"
    return "vermelho"


def _git_short_hash() -> str:
    """Short hash do HEAD do checkout que está rodando este export.

    Estampar no banner deixa óbvio QUAL versão do código produziu a Sheet
    atual — se aparecer um hash diferente do HEAD de `main` no GitHub, é
    sinal de que um cron antigo (laptop, worktree esquecido) rodou em
    código stale e sobrescreveu o output do CI. Sem o stamp, o operador
    precisava deduzir isso pelo layout das colunas, o que só funciona
    quando a diferença é visível.

    Lê `.git/HEAD` direto (sem invocar `git` no PATH) — funciona em
    qualquer ambiente que tenha o repo checked out, mesmo onde o binário
    `git` não está instalado. Fail-soft pra "?" quando `.git` não existe
    (deploy isolado, container minimal, copy do código sem versionamento).
    """
    try:
        import pathlib
        head = pathlib.Path(".git/HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref_path = pathlib.Path(".git") / head[5:]
            return ref_path.read_text().strip()[:7]
        return head[:7]
    except Exception:
        return "?"


def _col_letter(idx_0based: int) -> str:
    """Converte índice 0-based em letra de coluna estilo Sheets (A, B, ..., Z, AA, AB, ...)."""
    letters = ""
    idx = idx_0based
    while True:
        letters = chr(ord("A") + idx % 26) + letters
        idx = idx // 26 - 1
        if idx < 0:
            return letters


def _lucro_absoluto_no_alvo(av: AvaliacaoLote) -> int:
    """Lucro esperado em R$ se a empresa comprar pelo preço-alvo (caso médio).

    Identidade exata: `score_roi = lucro / capital_alvo` ⇒
        capital_alvo = preco_giro / (1 + score_roi)
        lucro        = preco_giro - capital_alvo = preco_giro × score_roi / (1 + score_roi)

    Usa só campos persistidos em AvaliacaoLote — não precisa do `custo_op` da
    empresa em runtime. Substitui aproximação anterior `score_roi × preco_alvo`
    que subestimava sistematicamente em ~10% (capital_alvo > preco_alvo por causa
    de reforma/frete/taxas/custo_op).
    """
    # Defesa contra registros antigos com NULL — campos são non-nullable hoje
    # mas migrações passadas podem ter deixado lixo. Sem isso, `None <= 0` levanta
    # TypeError e quebra a planilha inteira.
    if av.score_roi is None or av.preco_giro is None:
        return 0
    if av.score_roi <= 0 or av.preco_giro <= 0:
        return 0
    return int(round(av.preco_giro * av.score_roi / (1.0 + av.score_roi)))


def _score_roi_efetivo(av: AvaliacaoLote, lance_atual: Optional[int]) -> float:
    """ROI honesto considerando entrada pelo `max(lance_atual, preco_alvo)`.

    Quando `lance_atual > preco_alvo`, o operador real entra acima do alvo:
    capital empatado cresce, retorno cai. **AMBAS** as colunas `ROI alvo (%)`
    e `Lucro (R$)` da planilha consomem este helper — display coerente: o
    operador faz a conta `capital × ROI ≈ Lucro` e bate (correção P5f de
    2026-05-10). Antes (até 2026-05-09), `Lucro` era efetivo e `ROI alvo`
    era intrinsic — em zona apertada o ROI ficava 64% mas o Lucro mostrava
    R$ 7k, capital implícito ~R$ 11k, conflitante com qualquer linha real.
    Ranking (sheets `_query`, cli `top`, audit) agora ordena por LUCRO
    ABSOLUTO efetivo desde workstream II (2026-05-16) — derivado deste mesmo
    score via `_lucro_absoluto_efetivo`. `score_roi` persistido em
    `AvaliacaoLote` continua sendo o intrinsic (caso médio no alvo teórico)
    pra rastreabilidade + uso pelo flag `carros-sa top --roi-intrinsic`.

    Aproximação: ignora a parcela `taxa_leilao_pct × delta_lance` no capital
    incremental (≈zero em Auto Avaliar com taxa fixa; até 8% num leilão judicial,
    erro pequeno frente ao gap lance-alvo). Refator no precificador pra computar
    `score_roi_efetivo` exato seria possível, mas exigiria persistir mais campos.
    """
    if av.score_roi is None or av.preco_giro is None or av.preco_giro <= 0:
        return 0.0
    # `preco_alvo` é non-nullable no schema atual, mas registros pré-workstream
    # ou imports históricos podem trazer NULL. Sem essa coalescência, a linha
    # `lance_atual - av.preco_alvo` levantava TypeError e quebrava a planilha.
    alvo = av.preco_alvo or 0
    if lance_atual is None or lance_atual <= alvo:
        return av.score_roi  # entrada pelo alvo é factível
    capital_alvo = av.preco_giro / (1.0 + av.score_roi)
    capital_ef = capital_alvo + (lance_atual - alvo)
    if capital_ef <= 0:
        return 0.0
    return (av.preco_giro - capital_ef) / capital_ef


# Mapa de códigos do `verificar_laudo_completo` (laudo_audit.py) pra texto curto
# legível na célula "Situação". Mantido aqui (não em laudo_audit) porque é
# convenção de UI da planilha — o módulo de auditoria deve ficar puro/textual.
# Multi-falha: junta com " + " na ordem fixa do dict pra estabilidade visual.
_LAUDO_MOTIVO_LEGIVEL = {
    "pdf_ausente": "PDF ausente",
    "cache_confianca_baixa": "extração fraca",
    "url_invalida_ou_ausente": "URL inválida",
}


def _laudo_motivo_legivel(motivo: Optional[str]) -> str:
    """Converte motivo serializado do `verificar_laudo_completo` em texto curto.

    `motivo` é uma string como "pdf_ausente, cache_confianca_baixa" (ordem fixa
    do `_motivo` em laudo_audit.py). Sem motivo (laudo completo): "—".
    """
    if not motivo:
        return "—"
    partes = [p.strip() for p in motivo.split(",") if p.strip()]
    legiveis = [_LAUDO_MOTIVO_LEGIVEL.get(p, p) for p in partes]
    return " + ".join(legiveis) if legiveis else motivo


def _sufixo_warning_operacional(row: dict) -> str:
    """Sufixo ' ⚠ ESTRUTURAL / motor' quando lote viável com laudo analisado tem
    severidade ESTRUTURAL ou motor com problema.

    Cross-checks `_check_severidade_estrutural_em_viavel` e
    `_check_motor_problema_em_viavel` em audit.py já flagam, mas operador focado
    em ROI alto raramente roda audit antes de cada lance — antecipa o aviso
    visualmente na própria coluna "Situação". Não suprime números (laudo é
    confiável, decisão é dele), só sinaliza.

    Não dispara em:
      - lote inviável (display já mostra "✗ Caro demais")
      - laudo NÃO CAPTURADO (display já mostra "⚠ LAUDO NÃO CAPTURADO"
        e oculta números — paridade P5e)
    """
    if not row.get("viavel") or not row.get("laudo_analisado"):
        return ""
    warnings = []
    severidade = str(row.get("severidade") or "").lower()
    if severidade == "estrutural":
        warnings.append("ESTRUTURAL")
    if row.get("motor_ok_bool") is False:
        warnings.append("motor")
    return f" ⚠ {' + '.join(warnings)}" if warnings else ""


def _lucro_absoluto_efetivo(av: AvaliacaoLote, lance_atual: Optional[int]) -> int:
    """Lucro absoluto pelo cenário REAL (entrada por `max(lance_atual, preco_alvo)`).

    Espelha `_lucro_absoluto_no_alvo` mas usa `_score_roi_efetivo`. Quando
    lance_atual ≤ preco_alvo, devolve o mesmo valor — só diverge no caso onde
    o leilão já passou do alvo.
    """
    score_ef = _score_roi_efetivo(av, lance_atual)
    if score_ef <= 0 or av.preco_giro is None or av.preco_giro <= 0:
        return 0
    return int(round(av.preco_giro * score_ef / (1.0 + score_ef)))


class SheetsExporter:
    """Exporta avaliações do SQLite para uma aba no Google Sheets."""

    def __init__(self, spreadsheet_id: str, credentials_path: str) -> None:
        self._spreadsheet_id = spreadsheet_id
        self._credentials_path = credentials_path
        self._gc = None  # lazy init para não quebrar import sem credenciais

    def _client(self):
        if self._gc is None:
            import gspread
            self._gc = gspread.service_account(filename=self._credentials_path)
        return self._gc

    def exportar(
        self,
        empresa_id: str,
        session: Session,
        horizonte_exibicao_dias: Optional[int] = None,
    ) -> int:
        """Lê SQLite, escreve aba <empresa_id> + aba de cidades + aba Glossário. Retorna n linhas exportadas.

        `horizonte_exibicao_dias` (opt-in): limita a planilha a lotes cujo fim
        está dentro de N dias a partir de agora. Default `None` = mostra tudo
        que está ativo no DB. Separado do `horizonte_dias` do scraper: a coleta
        puxa o pipeline inteiro de futuros leilões e o usuário decide a janela
        de exibição sem precisar re-scrape.
        """
        rows = self._query(empresa_id, session, horizonte_exibicao_dias)
        # Filtro duro: lote encerrado (timer vencido OU badge ARREMATADO) é
        # ruído — operador não pode mais dar lance. Antes empurrávamos pro
        # final; agora removemos completamente.
        rows_ativos = [r for r in rows if not r["encerrado"]]
        rows_sorted = sorted(
            rows_ativos,
            key=lambda r: (
                0 if r["laudo_analisado"] else 2,   # lotes com laudo real primeiro;
                                                    # não-analisados só no final pra
                                                    # o operador conferir depois
                0 if r["viavel"] else 1,            # viáveis antes dos inviáveis
                # Ranking principal = lucro absoluto desc — "dinheiros que sobram
                # no final". MESMA métrica que o `carros-sa top` (cli.py) e o
                # audit.py (paridade total exigida — P5b). Antes era ROI anualizado;
                # premiava lote de capital pequeno com ROI alto sobre lote de
                # capital grande com lucro absoluto maior, distorcendo o que o
                # operador vê na coluna Lucro (R$). Lucro aqui é `_lucro_absoluto_efetivo`
                # (basis score_efetivo, mesmo da coluna exibida) — em zona apertada
                # cai com o capital empatado real.
                -(r["lucro"] or 0),
            ),
        )
        self._write_sheet(empresa_id, rows_sorted)
        self._write_cidades_frete_sheet(empresa_id, session)
        self._write_glossario_sheet()
        return len(rows_sorted)

    def _query(
        self,
        empresa_id: str,
        session: Session,
        horizonte_exibicao_dias: Optional[int] = None,
    ) -> List[dict]:
        """JOIN lote + avaliacao_lote + laudo (LEFT JOIN — laudo pode não existir)."""
        avaliacoes = session.exec(
            select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa_id)
        ).all()

        agora = datetime.now()
        limite_exibicao = (
            agora + timedelta(days=horizonte_exibicao_dias)
            if horizonte_exibicao_dias is not None
            else None
        )
        rows: List[dict] = []
        for av in avaliacoes:
            lote = session.get(Lote, av.lote_id)
            if lote is None:
                continue

            # Filtro duro: lote sem Fim do Leilão visível não entra na planilha.
            # Observação empírica (feedback usuário 2026-04-16): 100% dos lotes
            # com `fim_em=None` coletados em triagens anteriores já estavam
            # arrematados/vendidos OU o link do anúncio não abria mais. Auto
            # Avaliar só mostra countdown enquanto o lote está em leilão ativo;
            # sumiu o countdown = já saiu do leilão. Melhor esconder do que
            # mandar o usuário clicar em link morto.
            if lote.fim_em is None:
                continue

            if limite_exibicao is not None and lote.fim_em > limite_exibicao:
                continue

            laudo: Optional[LaudoCache] = session.get(LaudoCache, av.lote_id)

            try:
                fim_em_str = lote.fim_em.strftime("%d/%m/%Y %H:%M")
            except Exception:
                fim_em_str = str(lote.fim_em)

            viavel = av.preco_max > (lote.lance_atual or 0)

            # Display da planilha consome SÓ `score_efetivo` (ROI realista que
            # reflete o lance atual real) — ambas as colunas `Lucro (R$)` e
            # `ROI alvo (%)` derivam dessa base, em paridade aritmética (P5f
            # 2026-05-10). `roi_anualizado` não é mais calculado aqui desde
            # workstream II (2026-05-16): o ranking passou de ROI anualizado
            # pra LUCRO ABSOLUTO ("dinheiros que sobram no fim") e nenhuma
            # coluna exibe a versão anual. Função `roi_anualizado` continua
            # disponível em calibracao_giro.py pra outros usos (priorização
            # da coleta Webmotors no cron G — ver cli.py::webmotors_coletar).
            score_efetivo = _score_roi_efetivo(av, lote.lance_atual)
            # ROI alvo EFETIVO (mesma base do Lucro abaixo) — fix P5f 2026-05-10.
            # Antes era `(av.score_roi or 0) * 100` (intrinsic) enquanto Lucro usava
            # score_efetivo. Em zona apertada operador via ROI 64% e Lucro R$7k mas
            # `capital × ROI ≈ Lucro` não batia (capital implícito ~R$11k, sem
            # correspondência na linha). Ambos efetivos = mental math do operador
            # bate. Quando `lance_atual ≤ preco_alvo`, score_efetivo == score_roi
            # (sem mudança visível); só zona apertada e inviáveis ficam diferentes
            # — e inviáveis já viram "—" no display abaixo.
            roi_alvo = score_efetivo * 100
            lucro = _lucro_absoluto_efetivo(av, lote.lance_atual)

            # Encerrado = badge "ARREMATADO" visto no detalhe OU timer já passou.
            # Dupla checagem pra cobrir os dois vetores (snapshot velho + detecção
            # direta no HTML da próxima coleta).
            detalhe_raw = (lote.raw_json or {}).get("detalhe") or {}
            encerrado_por_badge = bool(detalhe_raw.get("encerrado"))
            encerrado_por_timer = (
                lote.fim_em is not None and lote.fim_em < agora
            )
            encerrado = encerrado_por_badge or encerrado_por_timer

            # URL do PDF do laudo — filtra decoys (Transparência, listing) que
            # ainda podem estar em `raw_json` de coletas antigas. Só exibimos
            # link clicável se a URL passa pelo `is_laudo_pdf_url`.
            laudo_url_raw = detalhe_raw.get("laudo_pdf_url")
            laudo_url = laudo_url_raw if is_laudo_pdf_url(laudo_url_raw) else None

            # Auditoria de completude — fonte única do "todo lote tem laudo
            # baixado + revisado + linkado" descrito no workstream U/V. Os 3
            # booleanos (pdf_local, laudo_cache_ok, url_persistida_ok) viram o
            # motivo agregado que a planilha expõe na Situação — operador vê
            # o porquê SEM precisar abrir log/cron, fechando o laço da
            # auditoria estrita ao olho humano que de fato lê a planilha.
            # Passa `pdf_dir=PDF_DIR_DEFAULT` (referência local do módulo sheets)
            # explicitamente — testes patcham `carros_sa.tools.sheets.PDF_DIR_DEFAULT`
            # via monkeypatch e contam com essa indireção. Sem o argumento, o
            # default da função usa `laudo_audit.PDF_DIR_DEFAULT` direto e o
            # patch fica inerte.
            laudo_status = verificar_laudo_completo(lote, laudo, pdf_dir=PDF_DIR_DEFAULT)
            pdf_local_existe = laudo_status.pdf_local

            # Laudo só conta como "analisado" se veio de um PDF real — fallback
            # `_laudo_sem_pdf` (sem avarias, "não identificou nada") grava
            # confidence <= 0.55. Sem essa distinção o usuário via reforma de
            # R$ 1.000 (piso) em 63/68 lotes e ia achando que o carro "tava ok"
            # quando na verdade ninguém leu o laudo. Regra do usuário: não dar
            # fallback de valor, avisar explicitamente que não foi analisado.
            laudo_analisado = laudo_status.laudo_cache_ok

            loja_raw = (
                lote.raw_json.get("loja")
                if isinstance(lote.raw_json, dict)
                else None
            )

            # severidade / motor_ok pra warning visual em "Situação" — operador
            # focado em ROI alto pode dar lance em lote viável com laudo ESTRUTURAL
            # ou motor problema sem ver o aviso (que existia só em audit, fora do
            # fluxo natural). Esses cross-checks `_check_severidade_estrutural_em_viavel`
            # e `_check_motor_problema_em_viavel` continuam reportando — display
            # passa a antecipar visualmente.
            severidade = laudo.severidade_geral if laudo else None
            motor_ok_bool = laudo.motor_ok if laudo else None

            rows.append({
                "lote_id": av.lote_id,
                "marca": lote.marca,
                "modelo": lote.modelo,
                "ano": lote.ano,
                "cidade": lote.origem_cidade or "—",
                "loja": loja_raw,
                "fim_em": fim_em_str,
                "km": lote.km,
                "km_por_ano": _km_por_ano(lote.km, lote.ano, agora.year),
                "km_indicator": _km_indicator(lote.km, lote.ano, agora.year),
                "lance_atual": lote.lance_atual or 0,
                "preco_max": av.preco_max,
                "fipe": av.fipe,
                "severidade": severidade,
                "motor_ok_bool": motor_ok_bool,
                # Mediana de mercado: dado real do Webmotors via cache populado
                # pelo cron `carros-sa webmotors-coletar` (workstream G, 2026-05-12).
                # Sem amostra → `webmotors_n_anuncios=0/None` faz o display mostrar
                # "—" (ver `_write_sheet`). Operador compara FIPE vs mediana vs
                # Lance Atual lado a lado pra contextualizar a decisão da máquina.
                # Mediana NÃO entra no cálculo de Lance Máximo (precificador é
                # FIPE-only desde 2026-05-08).
                "webmotors_mediana": av.webmotors_mediana,
                "webmotors_n_anuncios": av.webmotors_n_anuncios,
                "roi_alvo": round(roi_alvo, 1),
                "lucro": lucro,
                "reforma_estimada": av.reforma_estimada,
                "reforma_racional": av.reforma_racional,
                "url": lote.url,
                "laudo_url": laudo_url,
                "pdf_local_existe": pdf_local_existe,
                "viavel": viavel,
                "encerrado": encerrado,
                "laudo_analisado": laudo_analisado,
                "laudo_completo": laudo_status.completo,
                "laudo_motivo": laudo_status.motivo,
            })
        return rows

    def _write_sheet(self, empresa_id: str, rows: List[dict]) -> None:
        """Abre/cria aba, limpa, escreve timestamp global + header + rows."""
        gc = self._client()
        sh = gc.open_by_key(self._spreadsheet_id)

        # Abre ou cria a aba com nome = empresa_id
        try:
            ws = sh.worksheet(empresa_id)
        except Exception:
            ws = sh.add_worksheet(title=empresa_id, rows=500, cols=len(HEADER))

        ts = datetime.now().strftime("%d/%m/%Y %H:%M")

        # Linha 1: banner global "Última atualização" — deixa óbvio o quão fresco
        # está o snapshot. Preenche a primeira célula e mantém o resto vazio pra
        # não poluir o layout; o freeze(rows=2) congela tanto o banner quanto o
        # header de colunas. O short hash do commit identifica QUAL checkout
        # produziu esta Sheet — quando aparecer hash ≠ HEAD do main no GitHub
        # é sinal de cron antigo (laptop/worktree esquecido) sobrescrevendo o
        # output do CI; sem o stamp, só dava pra inferir pelo layout das colunas.
        commit = _git_short_hash()
        banner = [f"Última atualização da planilha: {ts} (commit {commit})"] + [""] * (len(HEADER) - 1)

        sheet_rows = [banner, HEADER]
        for rank, r in enumerate(rows, start=1):
            nao_analisado = not r["laudo_analisado"]
            if nao_analisado:
                # "NÃO CAPTURADO" descreve o sintoma com honestidade: o laudo
                # quase sempre EXISTE no Auto Avaliar (status="Aprovado" no
                # body_text), mas o scraper não conseguiu extrair a URL do PDF
                # (modal lazy não rendeu, HTTP 429, etc — ver coletar_detalhe).
                # Antes era "NÃO ANALISADO", o que sugeria que o laudo não
                # existia — passou a impressão errada e levava operador a
                # ignorar lotes recuperáveis.
                #
                # Sufixo com motivo agregado (do `verificar_laudo_completo`)
                # responde "por que esse aqui?" sem operador precisar abrir
                # cron log. PDF ausente = scraper não baixou (URL faltou ou
                # HTTP 429 esgotou retries); extração fraca = baixou mas
                # vision/textual não acharam avarias (Gemini 503, PDF de <2
                # páginas); URL inválida = persistida sem passar em
                # is_laudo_pdf_url (decoy ou já expurgada por limpar_decoys).
                motivo_legivel = _laudo_motivo_legivel(r.get("laudo_motivo"))
                situacao = f"⚠ LAUDO NÃO CAPTURADO: {motivo_legivel}"
            elif not r["laudo_completo"]:
                # Laudo foi extraído (cache forte) MAS algum dos outros sinais
                # falhou — PDF some do disco entre runs OU URL no raw_json
                # ficou stale. O laudo é confiável (numéricos seguem válidos),
                # mas a célula "Laudo" da planilha pode renderizar "PDF salvo
                # (link expirado)" ou "—". Sinaliza explicitamente em vez de
                # esconder: operador clica no anúncio e re-baixa se quiser.
                motivo_legivel = _laudo_motivo_legivel(r.get("laudo_motivo"))
                # Sufixo aplica nos DOIS lados (viável e caro demais) por
                # simetria — operador que filtra por "✗" também precisa
                # saber se o laudo está parcial (PDF some, URL stale).
                # Antes deste ramo `✗ Caro demais` saía sem sufixo e o
                # glossário ficava inconsistente.
                base = "✓ Viável" if r["viavel"] else "✗ Caro demais"
                situacao = f"{base} (laudo: {motivo_legivel}){_sufixo_warning_operacional(r)}"
            elif r["viavel"]:
                situacao = f"✓ Viável{_sufixo_warning_operacional(r)}"
            else:
                situacao = "✗ Caro demais"
            # URL → HYPERLINK clicável com label curto ("Abrir anúncio")
            # em vez da URL crua longa. Sheets interpreta com USER_ENTERED.
            if r["url"]:
                url_escaped = r["url"].replace('"', '""')
                url_cell = f'=HYPERLINK("{url_escaped}"; "Abrir anúncio")'
            else:
                url_cell = "—"
            # Link pro PDF do laudo. Três estados:
            #   1. URL válida (passa em is_laudo_pdf_url)  → HYPERLINK clicável.
            #   2. PDF salvo localmente mas URL ausente/expirada → texto descritivo
            #      "PDF salvo (link expirado)" — sinaliza que o laudo FOI analisado
            #      e está em data/laudos_pdfs/<lote>.pdf, só o link assinado morreu.
            #      (URLs do storage do Auto Avaliar têm validade ~1h.)
            #   3. Sem URL e sem PDF local → "—" (laudo de fato não disponível).
            if r["laudo_url"]:
                laudo_escaped = r["laudo_url"].replace('"', '""')
                laudo_cell = f'=HYPERLINK("{laudo_escaped}"; "Ver laudo")'
            elif r.get("pdf_local_existe"):
                laudo_cell = "PDF salvo (link expirado)"
            else:
                laudo_cell = "—"

            # Quando o laudo não foi analisado de verdade (sem PDF ou extração
            # falhou), NÃO exibimos preço-alvo, ROI nem reforma — seriam chutes
            # baseados em laudo vazio e induziriam o operador a dar lance no
            # escuro. Mantemos identificação do lote + link pra ele resolver
            # manualmente. O retry automático (scripts/retry_laudos_pendentes.py)
            # tenta preencher esses campos na próxima passada.
            if nao_analisado:
                preco_max_cell = "—"
                lucro_cell = "—"
                roi_alvo_cell = "—"
                reforma_cell = "—"
                # Sem laudo o estimador não rodou — racional viraria fallback
                # vazio "—" e poluiria a coluna. Aborta junto com Reforma (R$).
                racional_cell = "—"
            else:
                preco_max_cell = r["preco_max"]
                # Em lotes inviáveis (lance atual já passou do nosso teto),
                # Lucro e ROI alvo pressupõem comprar pelo preço-ALVO
                # — que é menor que o lance atual. Cenário fantasioso. Em lote
                # estrutural inviável, o score_roi inflado pela margem alta
                # (~1.0) produz Lucro de R$5k+ e ROI alvo ~100%, induzindo o
                # operador a achar que vale negociar. Mantemos preco_max +
                # reforma + FIPE pra ele entender por que descartamos.
                if r["viavel"]:
                    lucro_cell = r["lucro"]
                    roi_alvo_cell = r["roi_alvo"]
                else:
                    lucro_cell = "—"
                    roi_alvo_cell = "—"
                reforma_cell = r["reforma_estimada"]
                racional_cell = r["reforma_racional"] or "—"

            # FIPE é referência de mercado, NÃO depende do laudo — sempre mostra
            # quando a avaliação tem o valor (registros pré-workstream K podem
            # estar com fipe=NULL; nesses casos cai pro placeholder).
            fipe_cell = r["fipe"] if r["fipe"] is not None else "—"

            # Mediana de mercado: referência informativa lado a lado com FIPE.
            # Workstream G (2026-05-12): dado vem do Webmotors live (cache 24h).
            # Suprime ("—") quando NÃO há amostra real — `webmotors_n_anuncios`
            # vazio/0 sinaliza que a `webmotors_mediana` persistida é placeholder
            # FIPE neutro (sem sinal de mercado real). Em registros pré-G
            # (`webmotors_n_anuncios IS NULL`) também suprime — operador não
            # consegue distinguir se é dado real ou fallback antigo.
            n_anuncios = r.get("webmotors_n_anuncios") or 0
            if r["webmotors_mediana"] and n_anuncios >= 1:
                mediana_cell = r["webmotors_mediana"]
            else:
                mediana_cell = "—"

            loja_cell = r["loja"] or "—"
            km_por_ano_cell = r["km_por_ano"] if r["km_por_ano"] is not None else "—"

            sheet_rows.append([
                rank,
                situacao,
                r["marca"],
                r["modelo"],
                r["ano"],
                r["cidade"],
                loja_cell,
                r["fim_em"],
                r["km"] if r["km"] is not None else "—",
                km_por_ano_cell,
                r["lance_atual"],
                preco_max_cell,
                fipe_cell,
                mediana_cell,
                lucro_cell,
                roi_alvo_cell,
                reforma_cell,
                racional_cell,
                url_cell,
                laudo_cell,
            ])

        # Encolhe a grade pra exatamente len(HEADER) colunas ANTES do clear.
        # Sem isso, abas criadas em versões com HEADER mais largo (ex.: 27 cols
        # com "Laudo (PDF)" em Z, "Coletado em" em AA) ficam com colunas
        # órfãs depois do slim-down: `ws.clear()` esvazia o range ativo mas
        # não derruba colunas, e `ws.update()` só escreve até `len(HEADER)`,
        # então P→AA congela no estado antigo. Resultado observado pelo
        # operador: "Laudo (PDF)" zumbi em Z mostrando "—" pra todo mundo
        # mesmo com lotes "✓ Viável" e Laudo válido em O. Resize derruba
        # o lixo no servidor — gspread aceita encolher pra cols<atual.
        n_rows = max(len(sheet_rows) + 50, 100)  # folga pra crescer sem reflow
        try:
            ws.resize(rows=n_rows, cols=len(HEADER))
        except Exception:
            # gspread pode falhar se a aba já estiver em len(HEADER) cols
            # (resize trivial vira no-op em algumas versões). Fail-soft —
            # o clear+update abaixo ainda escreve os dados corretos.
            pass

        ws.clear()
        self._reaplicar_formato_numerico(ws)
        ws.update(sheet_rows, value_input_option="USER_ENTERED")
        self._aplicar_cores_km(ws, rows)

        # Congela banner (linha 1) + header (linha 2)
        ws.freeze(rows=2)

    @staticmethod
    def _aplicar_cores_km(ws, rows: List[dict]) -> None:
        """Pinta o fundo da célula KM por linha conforme `km_indicator`.

        Usa `batch_format` em vez de embutir emoji no texto pra manter a célula
        numérica (sortable + format `#,##0` aplicado pela coluna). Sempre reseta
        a coluna inteira pra branco antes de aplicar as cores — `ws.clear()`
        preserva backgroundColor, então sem reset os lotes que perderam
        classificação (km virou None, idade saturou) herdariam a cor do run
        anterior.
        """
        km_letter = _col_letter(HEADER.index("KM"))
        formats = [
            # Reset prévio. `ws.batch_format` aplica entradas em ordem dentro
            # da mesma chamada, então células coloridas abaixo sobrescrevem o
            # branco da coluna.
            {"range": f"{km_letter}:{km_letter}",
             "format": {"backgroundColor": _COR_BRANCO}},
        ]
        for idx, r in enumerate(rows):
            ind = r.get("km_indicator")
            if ind is None:
                continue
            # banner=linha1, header=linha2, dados começam em 3 (1-indexed).
            sheet_row = idx + 3
            formats.append({
                "range": f"{km_letter}{sheet_row}",
                "format": {"backgroundColor": _COR_POR_INDICADOR_KM[ind]},
            })
        ws.batch_format(formats)

    @staticmethod
    def _reaplicar_formato_numerico(ws) -> None:
        """Reaplica NUMBER em colunas numéricas.

        `ws.clear()` preserva formato de célula, então colunas cuja posição já
        foi ocupada por um timestamp ("Atualizado em") em versões passadas do
        HEADER continuam marcadas como DATE. Sem este reset, inteiros R$ são
        renderizados como datas (ex.: 5000 → 1913-09-07).
        """
        formats = []
        for col_name, cell_format in COLUMN_FORMATS.items():
            if col_name not in HEADER:
                continue
            letter = _col_letter(HEADER.index(col_name))
            formats.append({"range": f"{letter}:{letter}", "format": cell_format})
        if formats:
            ws.batch_format(formats)

    def _write_cidades_frete_sheet(self, empresa_id: str, session: Session) -> None:
        """Aba 'cidades_<empresa_id>' com cidades do raio operacional + frete por categoria.

        Mostra as cidades onde o scraper procura lotes (haversine ≤ raio_operacao_km
        do pátio), com o frete por categoria de veículo e a contagem de lotes ativos
        (fim_em > now()) já no DB pra cada cidade. Ajuda o operador a entender de
        onde está vindo o inventário e qual o custo logístico real.
        """
        try:
            empresa = carregar_empresa(empresa_id)
        except FileNotFoundError:
            # Sem YAML da empresa não há como derivar raio nem tabela de frete.
            # Pula a aba silenciosamente (caminho atingido apenas em testes
            # que mockam o gspread sem provisionar config).
            return
        cidades = empresa.cidades_de_busca()

        # Conta lotes ativos por (origem_cidade, origem_uf) — case/accent-insensitive
        # via `_normaliza` espelhando o helper de geo.py. Usado pra colar na linha
        # da cidade certa mesmo se o Auto Avaliar gravar "São Gotardo" e o IBGE
        # tiver "Sao Gotardo".
        from carros_sa.tools.geo import _normaliza
        agora = datetime.now()
        lotes_ativos = session.exec(
            select(Lote).where(Lote.fim_em > agora)
        ).all()
        contagem: dict = {}
        for lote in lotes_ativos:
            if not lote.origem_cidade or not lote.origem_uf:
                continue
            chave = (_normaliza(lote.origem_cidade), lote.origem_uf.strip().upper())
            contagem[chave] = contagem.get(chave, 0) + 1

        header = [
            "Cidade", "UF", "Distância (km)",
            "Frete Hatch (R$)", "Frete Sedan (R$)", "Frete SUV (R$)",
            "Frete Picape (R$)", "Frete Utilitário (R$)", "Frete Outro (R$)",
            "Lotes ativos no DB",
        ]

        body = []
        for m in cidades:
            d_km = int(round(m.distancia_do_ponto_km))
            qtd = contagem.get((_normaliza(m.nome), m.uf), 0)
            body.append([
                m.nome, m.uf, d_km,
                empresa.frete_para(d_km, CategoriaVeiculo.HATCH),
                empresa.frete_para(d_km, CategoriaVeiculo.SEDAN),
                empresa.frete_para(d_km, CategoriaVeiculo.SUV),
                empresa.frete_para(d_km, CategoriaVeiculo.PICAPE),
                empresa.frete_para(d_km, CategoriaVeiculo.UTILITARIO),
                empresa.frete_para(d_km, CategoriaVeiculo.OUTRO),
                qtd,
            ])

        ts = datetime.now().strftime("%d/%m/%Y %H:%M")
        banner = [
            f"Cidades no raio de {empresa.raio_operacao_km}km do pátio "
            f"({empresa.patio.cidade}/{empresa.patio.uf}) — atualizado {ts}"
        ] + [""] * (len(header) - 1)

        sheet_rows = [banner, header] + body

        gc = self._client()
        sh = gc.open_by_key(self._spreadsheet_id)
        aba_nome = f"cidades_{empresa_id}"
        try:
            ws = sh.worksheet(aba_nome)
        except Exception:
            ws = sh.add_worksheet(title=aba_nome, rows=max(len(sheet_rows) + 10, 100), cols=len(header))

        ws.clear()
        ws.update(sheet_rows, value_input_option="USER_ENTERED")
        ws.freeze(rows=2)

    def _write_glossario_sheet(self) -> None:
        """Aba fixa 'Glossário' com origem, cálculo e racional de cada coluna da triagem.

        Escrita toda vez que o exportador roda pra garantir que a doc não sai
        de sincronia com o código. Usa sempre o mesmo nome de aba ('Glossário'),
        então multi-tenant compartilha essa doc.
        """
        gc = self._client()
        sh = gc.open_by_key(self._spreadsheet_id)
        aba_nome = "Glossário"
        try:
            ws = sh.worksheet(aba_nome)
        except Exception:
            ws = sh.add_worksheet(title=aba_nome, rows=60, cols=4)

        glossario = [
            ["Campo", "Origem", "Cálculo / Fórmula", "Racional"],
            [
                "Rank",
                "Derivado",
                "Ordem: (1) lotes com laudo analisado, (2) viáveis (lance atual ≤ Lance Máximo), (3) maior LUCRO ABSOLUTO primeiro (R$ que sobram no fim — coluna 'Lucro (R$)'). Mesma métrica do CLI `carros-sa top` e do audit (paridade explícita exigida — LESSONS.md/P5b). Lucro usa base `score_efetivo` (em zona apertada cai com o capital empatado real) — coerente com o que o operador vê.",
                "'Dinheiros que sobram no final, qto mais melhor' (decisão do usuário, workstream II 2026-05-16). ROI anualizado e folga absoluta foram retirados como modos de ranking porque premiavam lote de capital pequeno com ROI% alto sobre lote de capital grande com lucro absoluto maior — distorcendo o que aparece na coluna Lucro (R$). Use `carros-sa top --roi-intrinsic` quando quiser sniff-test de potencial econômico no alvo teórico (ROI cru, ignora lance atual).",
            ],
            [
                "Situação",
                "Derivado",
                "✓ Viável se Lance Máximo > Lance Atual, senão ✗ Caro demais. ⚠ LAUDO NÃO CAPTURADO: <motivo> quando o laudo está incompleto. Motivos vêm do auditor (`verificar_laudo_completo`): 'PDF ausente' (scraper não baixou), 'extração fraca' (PDF baixado mas vision/textual não consolidaram avarias — confidence<0.6), 'URL inválida' (raw_json tem URL que não passa em is_laudo_pdf_url), ou combinação ('PDF ausente + URL inválida'). Quando o laudo FOI extraído (numéricos válidos) mas algum sinal lateral falhou (PDF sumiu OU URL stale), o sufixo aparece em ambos os ramos: '✓ Viável (laudo: <motivo>)' E '✗ Caro demais (laudo: <motivo>)' — simetria pra que filtros por '✗' também enxerguem o estado parcial. Lotes encerrados são filtrados antes do export. Sufixo ' ⚠ ESTRUTURAL' aparece em lotes viáveis com severidade=ESTRUTURAL no laudo (coluna B/C, longarina, monobloco reparados — operador real costuma descartar categoricamente). Sufixo ' ⚠ motor' em viáveis com motor_ok=False (motor não-original ou com problema — custo de retífica subestimável; revenda mais difícil mesmo após reparo). Combinam: ' ⚠ ESTRUTURAL + motor'.",
                "Resumo de uma célula do que o operador pode/deve fazer + razão exata quando algo está incompleto. Substitui o antigo '⚠ LAUDO NÃO CAPTURADO' genérico que obrigava o operador a abrir log do cron. Em '⚠ LAUDO NÃO CAPTURADO' os números (Lance Máximo, Lucro, ROI, Reforma) ficam '—' até o retry rodar. Em '✗ Caro demais' Lucro e ROI ficam '—'; Lance Máximo, FIPE e Reforma continuam visíveis. O cron diário (triagem→limpar_decoys→retry→audit --strict) tenta fechar todos os 3 sinais antes do próximo export; o que sobrar aparece aqui com motivo explícito. Sufixos ⚠ ESTRUTURAL / ⚠ motor antecipam visualmente os cross-checks operacionais do audit (`_check_severidade_estrutural_em_viavel`, `_check_motor_problema_em_viavel`) — operador focado em ROI raramente roda audit antes de cada lance, e esses lotes podem 'passar' pelo precificador via fator_risco saturado em laudos com lance baixo.",
            ],
            [
                "Marca",
                "Auto Avaliar (listagem)",
                "Marca (`lote.marca`) extraída do card via regex",
                "Coluna dedicada permite filtro/ordenação por fabricante sem depender de string composta",
            ],
            [
                "Modelo",
                "Auto Avaliar (listagem)",
                "Modelo (`lote.modelo`) extraído do card via regex; marca fica em coluna separada e ano em outra",
                "Identificação humana do veículo",
            ],
            [
                "Ano",
                "Auto Avaliar (listagem)",
                "Ano-modelo do veículo extraído do card",
                "Âncora de depreciação FIPE; separado da coluna Modelo pra facilitar filtro/ordenação",
            ],
            [
                "Cidade",
                "Auto Avaliar (detalhe)",
                "Campo origem_cidade do lote (onde o carro está pra retirada); '—' se não informado",
                "Sanity logístico — cidades distantes do pátio entram com frete maior embutido no Lance Máximo",
            ],
            [
                "Loja",
                "Auto Avaliar (listagem)",
                "Nome da concessionária/grupo que está anunciando o lote, extraído das duas últimas linhas do card antes do CTA ('FILIAL · GRUPO'). Persistido em `lote.raw_json['loja']`; '—' quando o card é atípico (sem rótulo) ou em coletas antigas anteriores ao scraper de loja.",
                "Permite filtrar/agrupar por vendedor — algumas lojas são mais confiáveis com laudos e prazos do que outras. Operador pode bloquear lojas com histórico ruim ou priorizar parceiros conhecidos.",
            ],
            [
                "Fim do Leilão",
                "Auto Avaliar",
                "Timer HH:MM:SS[:centésimos] do card convertido pra datetime absoluto. Lotes SEM fim visível são filtrados do export (Auto Avaliar só mostra countdown enquanto o lote está ativo).",
                "Urgência — lotes com fim próximo precisam de decisão antes",
            ],
            [
                "KM",
                "Auto Avaliar",
                "Número parseado da página de detalhe (specs). Cor de fundo da célula sinaliza km/ano (idade = max(1, ano_atual − ano_modelo)): 🟢 ≤15.000 km/ano (média Brasil ou abaixo, conservado), 🟡 15.001–25.000 km/ano (uso típico-alto), 🔴 >25.000 km/ano (uso intensivo, alta probabilidade de frota/Uber/táxi). Sem cor quando ano ou km estão ausentes.",
                "Sanity check rápido de desgaste. KM absoluto sozinho engana (60k num carro 2010 é diferente de 60k num 2024); km/ano normaliza pela idade. Vermelho = revenda mais difícil + desgaste mecânico desproporcional; verde = carro pouco rodado pra idade.",
            ],
            [
                "KM/ano",
                "Derivado",
                "km ÷ max(1, ano_atual − ano_modelo), arredondado. '—' quando km ou ano ausentes. Mesma fórmula que pinta a coluna KM (verde ≤15k/ano, amarelo 15-25k, vermelho >25k), exibida como número pra o operador ler o valor exato em vez de inferir pela cor.",
                "Normaliza o KM pela idade do carro pra comparar lotes de anos diferentes em pé de igualdade. Junto com a cor de fundo do KM forma a leitura rápida de desgaste relativo.",
            ],
            [
                "Lance Atual (R$)",
                "Auto Avaliar",
                "Maior lance no momento da raspagem (campo 'ULTIMA AVALIAÇÃO' da plataforma)",
                "Piso que precisamos cobrir pra entrar no leilão",
            ],
            [
                "Lance Máximo (R$)",
                "Precificador",
                "(preco_giro − reforma − frete − custo_op − margem_min×giro − taxa_fixa) ÷ (1 + taxa_pct). preco_giro = FIPE × f_km × 0.95 (refactor FIPE-only de 2026-05-08). Equação resolve circularidade da taxa proporcional cobrada sobre o lance vencedor. Já embute reforma, frete, FIPE e fator de risco do laudo. Audit dispara checks INDEPENDENTES (podem coexistir na mesma linha): (a) zona apertada — lance_atual > preco_alvo mas ≤ preco_max (entrada acima da margem calibrada, ROI realista < ROI alvo); (b) Lance Máximo > FIPE × 1.05 — red flag (manteve threshold de defesa em camadas; design FIPE-only torna inviável bater FIPE, mas mantemos o check pra detectar regressão); (c) preco_alvo > preco_max — viola identidade do precificador (bug).",
                "Teto ABSOLUTO — acima disso a margem mínima da empresa não é respeitada nem no melhor cenário. Por construção FIPE-only, sempre fica abaixo da FIPE.",
            ],
            [
                "FIPE (R$)",
                "API FIPE (cache `modelo_fipe_cache`)",
                "Valor da Tabela FIPE pra (marca, modelo, ano) consultado no momento da avaliação. Persistido em `avaliacao_lote.fipe` pra não depender de re-consulta. '—' em registros pré-workstream K (NULL).",
                "Âncora ÚNICA do precificador desde 2026-05-08 (refactor FIPE-only). preco_giro = FIPE × f_km × 0.95. Antes era `webmotors_mediana × f_km` com 3 caps em série tentando consertar similares poluídos do Auto Avaliar — bugs categóricos persistiam (Tiggo 7 entre Tiggo 2, Airtrek vs Outlander, Ka descontinuado). FIPE-only mata categoricamente Lance Máximo > FIPE.",
            ],
            [
                "Mediana mercado (R$)",
                "Webmotors live (cache 24h via `carros-sa webmotors-coletar`)",
                "Mediana dos preços de anúncios reais no Webmotors pra (marca, modelo, ano), coletada pelo cron noturno (workstream G, 2026-05-12). Persistida em `avaliacao_lote.webmotors_mediana` junto com `webmotors_n_anuncios` (tamanho da amostra). Quando NÃO há amostra fresh (n=0/NULL), o sistema persiste FIPE como placeholder neutro mas o DISPLAY mostra '—' — sinal honesto de 'sem dado de mercado real ainda, espere o cron rodar'. Anúncios sumidos do estoque ganham `sumiu_em` (proxy de venda, alimenta calibração de giro real — workstream G.2).",
                "Sinal de mercado INFORMATIVO — não entra no cálculo de Lance Máximo (precificador FIPE-only desde 2026-05-08). Operador compara FIPE × Mediana × Lance Atual lado a lado pra contextualizar. Mediana muito acima da FIPE (>1.20×) sinaliza modelos premium em alta; muito abaixo (<0.70×) pode indicar sample fraca ou anúncios vencidos. Histórico: similares do Auto Avaliar foram descontinuados como fonte (workstream G) porque amostras eram poluídas por outliers categóricos (Tiggo 7 vs Tiggo 2 etc.) e exigiam cap defensivo FIPE×1.20 que mascarava o ruído.",
            ],
            [
                "Lucro (R$)",
                "Derivado",
                "lucro_absoluto = preco_giro × score_efetivo ÷ (1 + score_efetivo). score_efetivo = score_roi original quando lance_atual ≤ preco_alvo; reduzido proporcionalmente quando lance_atual > preco_alvo (capital efetivo cresce). Em lotes '✗ Caro demais' a célula vai pra '—': comprar pelo preço-alvo é cenário fantasioso quando o lance atual já passou do nosso teto.",
                "Lucro TOTAL absoluto em R$ projetado pra revenda do lote (preço de venda - capital total investido). Antes era exibido como 'Lucro/mês' normalizando por `dias_giro_estimado`, mas a normalização confundia o operador (depende de calibração de giro frequentemente otimista). Agora mostra o número que efetivamente entra no caixa. Coerente com 'ROI alvo (%)': `Lucro = capital_efetivo × ROI/100` bate por construção (operator mental math passa).",
            ],
            [
                "ROI alvo (%)",
                "Derivado",
                "ROI realista = `score_efetivo × 100`, sem anualização. score_efetivo = score_roi original quando lance_atual ≤ preco_alvo (entrada pelo alvo factível); reduzido proporcionalmente quando lance_atual > preco_alvo (zona apertada — capital efetivo cresce). Em lotes '✗ Caro demais' a célula vai pra '—'. Calibrado por risco/liquidez via `margem_aplicada` (capada em 50% pra evitar que fatores no teto inflem artificialmente).",
                "Mostra o retorno % ESPERADO da OPERAÇÃO (compra → revenda → caixa) sem extrapolar pra horizonte anual, considerando o lance atual real. Em zona apertada (lance_atual entre preco_alvo e preco_max), o ROI exibido cai automaticamente pra refletir o capital extra empatado — antes (até 2026-05-09) a coluna mostrava o ROI alvo intrinsic enquanto Lucro mostrava o efetivo, então `capital × ROI ≈ Lucro` não batia (mental math do operador falhava). Agora ambas usam a mesma base. Audit avisa 'zona apertada' quando lance > alvo. Threshold do audit: > 100% sinaliza provável bug (margem cap em 50% limita score_roi a ~100%, e score_efetivo nunca passa do intrinsic).",
            ],
            [
                "Reforma (R$)",
                "EstimadorReformaLLM (fallback: tabela YAML)",
                "Custo total dos itens retornados pelo LLM a partir do laudo; se LLM falhar, soma da tabela (família_peça × severidade) + adicional estrutural quando aplicável. Já descontado do Lance Máximo. Audit sinaliza Reforma > 30% do preco_giro como 'lote economicamente questionável' (mesmo viável: capital empatado em reforma é alto vs. revenda) e Reforma R$ 0 com severidade ≥ média como contradição (LLM ignorou laudo).",
                "Custo ANTES de vender. Números grandes aqui = lote com dano material relevante; confirmar no PDF do laudo antes do lance. Operador deve abrir o laudo quando Reforma se aproxima de 30% do preco_giro — surpresa na oficina pode tornar o investimento inviável post-hoc.",
            ],
            [
                "Racional Reforma",
                "EstimadorReformaLLM.justificativa (fallback: sumário do precificador)",
                "Texto descrevendo por que a reforma custou R$X — quais peças/avarias entraram no orçamento. Quando o LLM rodou: justificativa do estimador (ex.: 'Coluna B reparada → solda + pintura; capô amassado'). Quando caiu pro fallback determinístico: sumário gerado pelo precificador a partir dos itens da tabela YAML. '—' em '⚠ LAUDO NÃO CAPTURADO' (estimador não rodou) e em registros pré-workstream O (campo NULL no DB).",
                "Audita o valor da Reforma sem precisar abrir o PDF do laudo — operador lê a justificativa e confere se o LLM/fallback enxergou o que devia. Texto pode ocupar 3-5 linhas wrapped no Sheets; preferimos preservar a info completa a truncar.",
            ],
            [
                "Anúncio",
                "Auto Avaliar",
                "=HYPERLINK do link direto do anúncio no b2b.autoavaliar.com.br, rotulado 'Abrir anúncio'",
                "1 clique pra abrir o anúncio original e fazer checagens manuais ou lance",
            ],
            [
                "Laudo",
                "Scraper detalhe + PDF persistido",
                "Três estados: (1) =HYPERLINK pro PDF do laudo cautelar, rotulado 'Ver laudo' — URLs que não são laudo real (Transparência, listagem) são filtradas pelo `is_laudo_pdf_url`; (2) 'PDF salvo (link expirado)' quando o PDF está em data/laudos_pdfs/ mas a URL pré-assinada do storage já expirou (validade ~1h); (3) '—' quando não há URL nem PDF local.",
                "Evidência material pra confirmar o valor da Reforma e conferir avarias antes do lance. Estado (2) significa que o laudo FOI analisado (severidade/avarias estão certas), só o link clicável morreu — pra abrir, recolete o lote ou consulte data/laudos_pdfs/<lote>.pdf no laptop. Auditoria diária pelo `make auditar-laudos`.",
            ],
        ]

        ws.clear()
        ws.update(glossario, value_input_option="USER_ENTERED")
        ws.freeze(rows=1)

    @property
    def sheet_url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self._spreadsheet_id}"
