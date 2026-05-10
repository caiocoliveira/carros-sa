# Carros SA — Instruções pro Claude Code

Este projeto é uma PoC multi-agente, multi-empresa que ranqueia lotes de leilão online (Auto Avaliar) por ROI potencial. Pra contexto completo do produto ver **`ROADMAP.md`** (status + workstreams) e `/Users/caiocoliveira/.claude/plans/bubbly-noodling-glade.md` (arquitetura).

## Stack

- Python **3.12** (instalado via `uv` em `~/.local/share/uv/python/`; o quirk antigo de evitar `X | None` e `list[...]` em runtime foi removido com o upgrade de 2026-04-18 — 3.12 suporta nativamente, mas o código existente ainda usa `Optional[X]` / `typing.List` em vários lugares; pode ir migrando à medida que tocar)
- Pydantic v2 + SQLModel + FastAPI/Typer
- SQLite (default `./carros_sa.db`)
- VisionClient pluggable: Gemini Flash (default, grátis), Anthropic Haiku, Ollama local

## Setup

Venv já criado em `.venv/` (Python 3.12, gerenciado por `uv`). Pra recriar do zero:

```bash
uv venv --python 3.12 .venv
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -e ".[dev]"
.venv/bin/playwright install chromium
```

Backup do venv antigo (3.9) em `.venv.py39.bak` — pode apagar quando confiar no novo.

Toda chamada precisa de `PYTHONPATH=.`:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v
```

Use o `Makefile` em vez de decorar:
- `make test` — roda toda a suíte (baseline obrigatório antes de "pronto")
- `make ingest` — persiste listagem JSON salva em SQLite
- `make extrair-laudo PDF=data/laudos_amostra/<arquivo>.pdf` — roda ExtratorLaudo
- `make db-reset` — apaga `carros_sa.db` e recria

## Contratos imutáveis — fronteira entre workstreams

**`carros_sa/models.py`** define todos os tipos (LoteRaw, LaudoEstruturado, SinalMercado, CustoReforma, CustoLogistico, Avaliacao, + 8 tabelas SQLModel). **Nunca altere sem coordenação com as outras sessões.** Se precisar de campo novo: pare, documente a necessidade no ROADMAP.md e pergunte antes.

Outros arquivos "não tocar sem motivo":
- `carros_sa/precificador.py` — fórmula do preço-alvo (coberta por 9 testes)
- `carros_sa/tenancy.py` — loader YAML das empresas
- `carros_sa/db.py` — engine
- `carros_sa/scraping/parsers.py` — parser de Auto Avaliar
- `carros_sa/agents/extrator_laudo.py` + `vision_clients.py`
- `config/empresas/*.yaml`

## Segredos

- **NUNCA** cole chaves, tokens ou senhas no chat — fica no histórico da conversa e vaza.
- Segredos vão em `.env` (já no `.gitignore`). Template em `.env.example`.
- Código lê via `python-dotenv`: `load_dotenv(); os.environ["GEMINI_API_KEY"]`
- Se alguém colar segredo em qualquer mensagem: pare, peça pra revogar e rotacionar.

## Env vars relevantes

- `GEMINI_API_KEY` — Gemini (default)
- `ANTHROPIC_API_KEY` — Anthropic (fallback)
- `VISION_PROVIDER` — `gemini` | `anthropic` | `ollama` (default: `gemini`)
- `CARROS_SA_DB` — override do caminho do SQLite

## Workflow autônomo — auto-push em main

Usuário trabalha laptop + celular e delegou autonomia. Após qualquer commit ou merge em `main`, **rodar `git push` automaticamente** sem pedir confirmação. Sempre mostrar resumo curto pro usuário (hash + mensagem + arquivos principais) pra ele ter visibilidade post-hoc.

Continuam exigindo aprovação explícita: `git push --force`, `reset --hard`, `--no-verify` em hooks, mudanças em `carros_sa/models.py` (contratos imutáveis), operações em outros remotes.

## Workflow autônomo — auto-review + auto-merge de PRs

Usuário NÃO revisa PRs manualmente. Toda PR aberta por mim deve seguir o ciclo:

1. **Criar PR** (draft ou ready, qualquer um).
2. **Disparar agente especializado em arquitetura de software** pra revisar — usar `Agent` com `subagent_type=Plan` (ou `general-purpose` com prompt de revisão arquitetural). Briefing inclui: link/diff do PR, contexto do que mudou, pergunta direta "vale mergear como está?".
3. **Se review aprova:** marcar como ready (sair do draft) + `merge_method=squash` + push em `main`. Não esperar humano.
4. **Se review aponta issues:** corrigir os blockers, reaplicar review, mergear. Issues "nice-to-have" registrar como follow-up no `ROADMAP.md` mas não bloquear merge.
5. **Se review aponta risco arquitetural sério (quebra contratos imutáveis em `models.py`, regressão de teste, mudança de fluxo crítico):** parar e perguntar antes de mergear.

Continua valendo: nunca mergear sem `make test` verde, nunca pular hooks, nunca alterar `models.py` sem coordenação.

## ROADMAP.md é fonte de verdade entre sessões paralelas

Sessões em worktrees separados **não enxergam** umas às outras até mergearem em `main`. O `ROADMAP.md` é a única fonte compartilhada de "o que está acontecendo agora" — sem ele, sessões duplicam trabalho ou pisam em premissas erradas.

**Regras obrigatórias em toda sessão de workstream:**

1. **No início:** `git pull` + ler `ROADMAP.md` integralmente. Se o seu workstream já estiver marcado ✅ ou se outro workstream tiver alterado os contratos que você usa, parar e pedir orientação.
2. **No meio:** descobertas valiosas (bugs em fornecedor, mudança de premissa, decisão arquitetural) viram entrada na memória persistente E nota curta no `ROADMAP.md` na seção do seu workstream.
3. **No fim, antes do merge:** atualizar `ROADMAP.md` marcando o workstream como ✅ com link pros arquivos criados + 1-2 linhas de "o que mudou" + "limitações conhecidas". O commit do merge inclui essa atualização.
4. **Sessão de integração (esta, em main):** após cada merge, rebalancear o `ROADMAP.md` — destravar workstreams sequenciais que dependiam do que acabou de mergear, ajustar prioridades, push.

## Workflow de sessão paralela

Cada workstream (ver `ROADMAP.md`) roda no seu próprio git worktree:

```bash
git worktree add ../carros-sa-<workstream> -b feat/<workstream>
cd ../carros-sa-<workstream>
claude
```

Primeira mensagem da sessão: cole `scripts/new_session_prompt.md` preenchido com o nome do workstream. O prompt te manda ler CLAUDE.md + ROADMAP.md + rodar `make test` antes de começar.

Critério de aceite pra merge em `main`:
1. `make test` verde (incluindo teste novo do workstream)
2. Novo teste cobre o caso de uso principal com fixture de dado real (modelo: `tests/test_extrator_laudo_consolidacao.py`)
3. `ROADMAP.md` atualizado marcando o workstream como ✅

## Memória persistente (compartilhada entre sessões)

Leitura obrigatória ao começar algo novo:
- `/Users/caiocoliveira/.claude/projects/-Users-caiocoliveira-Carros-SA/memory/MEMORY.md` — quirks específicos de fornecedor, flags de negócio, decisões fixadas
- [`LESSONS.md`](LESSONS.md) — padrões de falha recorrentes + causa raiz + checklist pré-merge. Ler antes de declarar `✅` qualquer workstream.

Descobertas valiosas (flags de negócio, decisões fixadas, quirks de fornecedor) viram entrada em `memory/`. Padrões repetidos (falha silenciosa, validação N=1, premissa inventada) viram entrada em `LESSONS.md`.

## Red flags

- **NÃO** iniciar scraping agressivo de Webmotors (ou outro) sem discutir estratégia anti-bot — pode queimar IP.
- **NÃO** fazer lance real em lote nenhum. Sistema só recomenda; humano executa.
- **NÃO** commitar `.env`, `*.db`, `data/scrapes/*.json`, `data/laudos_amostra/*.pdf` (já no `.gitignore`).
- **NÃO** alterar `carros_sa/models.py` sem discutir — quebra outras sessões em paralelo.
- **NÃO** rodar chamadas de LLM dentro de testes — usar fixture salva em `tests/fixtures/`.

## Dado real de referência

- Lote real com dano estrutural: `data/laudos_amostra/21854782_fiesta.pdf` (Fiesta 2013, colunas B/C esquerdas reparadas). Use como gold test.
- Listagem real de Uberlândia/MG: `data/scrapes/2026-04-14_uberlandia_listagem.json` (10 lotes variados).
- Fixture de resposta Gemini: `tests/fixtures/21854782_visual_gemini.json`.

## Princípios gerais de codagem

Adaptado de https://github.com/forrestchang/andrej-karpathy-skills. Reforça o que já está no system prompt do Claude Code; em conflito, regras específicas deste arquivo prevalecem.

1. **Pensar antes de codar.** Explicitar premissas. Se houver mais de uma interpretação, apresentar — não escolher silenciosamente. Se algo está confuso, parar e perguntar em vez de chutar.
2. **Simplicidade primeiro.** Mínimo de código que resolve o pedido. Sem feature além do que foi pedido, sem abstração de uso único, sem "flexibilidade" não solicitada, sem error handling pra cenário impossível. Se escreveu 200 linhas e dava em 50, reescreve.
3. **Mudanças cirúrgicas.** Tocar só o que precisa. Não "melhorar" código adjacente, formatação ou comentários. Match no estilo existente mesmo que você fizesse diferente. Código morto pré-existente: comenta, não apaga (a menos que peçam). Cada linha alterada precisa rastrear até o pedido do usuário.
4. **Critério de sucesso verificável.** Transformar a tarefa em loop fechado: "adicionar validação" → "escrever testes pros inputs inválidos e fazer passar"; "consertar bug" → "escrever teste que reproduz, depois fazer passar"; "refatorar X" → "garantir que os testes passam antes e depois". Pra tarefa multi-step, listar plano curto com verificação por passo.

## Padrões aprendidos (revisar antes de tocar nessas áreas)

### Identidade econômica do precificador
Por construção: `preco_max + reforma + frete + taxas_max + custo_op = preco_giro × (1 − margem_min)`. Equivalentemente: `capital_total_no_max = preco_giro × (1 − margem_min)`. Antes de inventar uma "ROI no máximo" derivada de `(preco_giro − capital) / capital`, lembre que o resultado vira `margem_min / (1 − margem_min)` — quase-constante por empresa, ~11% em Uberlândia. **Pra ranqueamento, sempre use `score_roi` (caso médio no preço-alvo).**

### Lucro absoluto exato — fórmula fechada
`score_roi = lucro / capital_alvo` ⇒ `capital_alvo = preco_giro / (1 + score_roi)` ⇒ `lucro_absoluto = preco_giro × score_roi / (1 + score_roi)`. Não use `score_roi × preco_alvo` como aproximação — subestima ~10% porque `capital_alvo > preco_alvo` (engloba reforma/frete/taxas/custo_op). Helper canônico em `sheets._lucro_absoluto_no_alvo`.

### Refactor FIPE-only no precificador (2026-05-08)
`preco_giro_fipe = FIPE × f_km × 0.95` desde 2026-05-08. Antes era `webmotors_mediana × f_km` com 3 caps em série (n<5 no avaliador → 1.20×FIPE no precificador → 1.05×FIPE no audit) tentando consertar similares poluídos do Auto Avaliar (Tiggo 7 vs Tiggo 2, Airtrek vs Outlander, Ka descontinuado). Como Webmotors live não está conectado (workstream G), `webmotors_mediana` era `FIPE × 0.97` na prática — sistema já era FIPE-driven mascarado por camadas de incerteza. Refactor matou os 2 caps redundantes; cap n<5 no `avaliador_mercado.py` mantido só pra limpar display da mediana (não afeta cálculo).

`preco_giro_aa` agora é sempre `None`. `webmotors_mediana` continua persistido em `Avaliacao`/`AvaliacaoLote` pra display na nova coluna **"Mediana mercado (R$)"** da planilha (entre FIPE e Lucro/mês). Operador vê FIPE × Mediana × Lance Atual lado a lado pra contextualizar.

**Por construção `preco_max < FIPE`** em qualquer cenário (max teórico = `FIPE × 1.15 × 0.95 × 0.90 ≈ 0.98 × FIPE`). Audit threshold 1.05×FIPE mantido como guard de regressão. **Não renomear campos persistidos** — contratos (models.py).

Quando workstream G ligar Webmotors live, redesenhar como `FIPE × β + mediana × (1−β)` com `β` variando por `n_anuncios_competidores` (sample size).

### Antes de mexer em precificador / sheets / cli, rodar `make test` com olhos abertos
Os testes `test_exportar_sheets.py::TestLucroAbsolutoNoAlvo` e `test_audit_columns.py::test_roi_absurdo_reportado` são guard-rails das fórmulas — qualquer mudança que quebrá-los provavelmente está reintroduzindo um dos bugs anteriores (ROI tautológico ou lucro subestimado).

### Workflow de revisão autônoma
Quando o usuário pedir "revise e corrija" sem direcionar, o caminho que funcionou foi: (1) ler ROADMAP + precificador + sheets + tests existentes, (2) ESCREVER UM SCRIPT DE SIMULAÇÃO (`/tmp/sim.py`) com lote real conhecido (Polo Track 2024 do YAML) e validar identidades algébricas comparando docstring vs implementação, (3) só depois confirmar bugs e refatorar. Pular a simulação leva a "fixes" baseados em leitura — frequentemente errados.

Cobrir **≥3 cenários** na simulação, não só o feliz: (a) caso real conhecido (gold Polo Track), (b) edge case do `f_km` no teto/piso (km do lote << ou >> mediana de mercado), (c) lote inviável (severidade ESTRUTURAL ou confidence baixa). Cada um expõe um modo de falha diferente — gold sozinho não pega ranking distorcido nem `score_roi` inflado em laudo ruim. Validar nos casos: relação `preco_max ↔ FIPE`, sinal de `score_roi`, monotonicidade do ranking entre lotes do mesmo perfil.

### "Mesma métrica em dois arquivos = duas métricas diferentes" (2026-05-05)
Padrão encontrado em revisão preventiva: helpers ad-hoc embutidos em `cli.py`, `sheets.py`, `audit.py` reimplementando variantes silenciosas do mesmo cálculo. CLI ranqueava por `roi_anualizado` desc; planilha ranqueava por folga absoluta `preco_max - lance_atual` desc; audit ordenava por folga; lucro absoluto era reescrito em dois lugares. Operador via duas ordens conflitantes da mesma fonte. Antes de adicionar uma métrica nova num arquivo, **procurar se ela já existe em outro** e importar — módulo neutro: `carros_sa.agents.calibracao_giro` pra `roi_anualizado` / `lucro_reais_por_mes`; `carros_sa.tools.sheets._lucro_absoluto_no_alvo` pro lucro absoluto. Quando o ranking é o produto principal, fazer **paridade explícita** entre as views (CLI top, planilha, audit) — divergência aqui não é refactor, é bug operacional.

### Análise da relação entre colunas (2026-05-05, revisão econômica)
Pergunta do usuário: faz sentido `Lance Máximo > FIPE`? `preco_giro_fipe > FIPE`? Resposta após 5 cenários simulados:
- **`Lance Máximo > FIPE`**: NÃO em condições normais. Por construção `preco_max ≤ preco_giro × (1−margem_min)`. Pra estourar FIPE precisaria f_km no teto 1.15 + custos zero — caso teórico, não real (max observado: 95.36% da FIPE). Audit já alerta `> FIPE × 1.05`.
- **`preco_giro_fipe > FIPE`**: PODE em até ~12% quando `f_km > 1` (lote km baixa). É econômico: carro mais bem cuidado vale mais que mediana de mercado. O nome "preco_giro_fipe" é enganoso porque é `mediana × f_km`, não `min(FIPE × 0.95, p25)` (versão antiga). Não renomear (contrato em models.py) — anotar mentalmente.
- **`score_roi` cresce com `fator_risco × fator_liquidez`**: lote pior conhecido (confidence 0.5) sai com ROI MAIOR que lote bem auditado (confidence 0.95). É design (mais incerteza → exige mais margem → score maior). Mascarado na planilha porque `confidence < 0.6` vira "—" e cai pro fim. Mas em logs/audit aparece. Não é bug, é contraintuitivo — não confundir com sintoma.

### `Lote.fim_em` é naive LOCAL — nunca UTC
Toda comparação contra `lote.fim_em` em `sheets.py`, `audit.py`, `laudo_audit.py`, `scraper_autoavaliar.py` e o filtro SQL `Lote.fim_em > now()` usa `datetime.now()` (naive local). Por isso `parse_card_lines` default agora é `datetime.now()` (não `utcnow()`) — em fusos != UTC, mistura gerava grace silenciosa de |offset|h onde lotes encerrados apareciam ativos e o horário exibido ficava |offset|h adiantado. Se for adicionar nova fonte de `fim_em` (re-scraper, importador, fixture), **NÃO use `utcnow()`** — ressuscita o bug. Os outros campos default-utcnow (`scraped_at`, `criado_em`, `extraido_em`) são timestamps internos sem comparação cross-tz, ficam como estão. Teste guard: `tests/test_parsers.py::test_parse_card_default_agora_e_local_naive_nao_utc`.

### Invariante de auditoria é coerência **da linha inteira**, não só da célula
`carros_sa/tools/audit.py::CHECKS` agora valida cada coluna por sanidade individual MAIS relação cross-field (Reforma R$ 0 com severidade ≥ média = contradição; `preco_giro_fipe` divergente >25% de FIPE = mediana de similares poluída ou cache FIPE stale). Antes de adicionar uma check nova, perguntar: "esse valor faz sentido sozinho, OU em relação aos outros campos da mesma linha?". Quase sempre é a 2ª — uma reforma de R$ 0 é válida em absoluto mas absurda num lote ESTRUTURAL. Padrão refletido em LESSONS.md/P6.

### Cap em margem_aplicada (`_MARGEM_TETO=0.50` no precificador)
`margem_aplicada = max(min(base × fator_risco × fator_liquidez, 0.50), minima_absoluta)`. Sem o cap, fatores saturados (laudo estrutural + mercado ilíquido) podiam levar margem a 90% (Uberlândia: 0.25 × 2.0 × 1.8) — `score_roi` explorava acima de 1.0 e lotes péssimos exibiam Lucro/mês alto na planilha. Cap é freio de emergência: lotes calibrados (margem 25-45%) intocados, extremos honestos. Não tirar sem revisar `sheets._write_sheet` (suprime Lucro/mês e ROI em inviáveis) — defesa em camadas.

### Cap mediana similares Auto Avaliar (`FIPE × 1.20` quando n<5)
AA pode trazer "similares" de outro modelo/versão (Tiggo 7 entre Tiggo 2, Cherokee entre Compass). Se a amostra é pequena (n<5), 1 outlier puxa `statistics.median` e o sistema recomendava lance ACIMA da FIPE. Cap em `FIPE × 1.20` em `agents/avaliador_mercado.avaliar()` quando `n<5` — Civic e Corolla legitimamente vendem ~110% FIPE em alta, 120% é teto generoso. Mediana isolada NÃO defende contra outlier categórico — precisa cap declarado.

### `_score_roi_efetivo` em sheets.py — ROI honesto em zona apertada
Quando `lance_atual > preco_alvo` mas `≤ preco_max`, o operador real entra acima do alvo: capital empatado cresce e ROI cai. `_score_roi_efetivo(av, lance_atual)` recalcula com `capital_ef = capital_alvo + (lance_atual - preco_alvo)` e devolve `(preco_giro - capital_ef) / capital_ef`. Quando `lance_atual ≤ preco_alvo`, devolve o `score_roi` original (intrinsic). **Sempre que for exibir ROI/Lucro mensal pro operador, passar `lance_atual` — nunca usar `score_roi` puro do DB pra display.** Persistido em `AvaliacaoLote.score_roi` continua sendo intrinsic (bom pra ranking algorítmico).

### Floor `dias_giro` 60d em `lucro_reais_por_mes` e `roi_anualizado`
Defaults categóricos otimistas (HATCH NOVO=25d) faziam `lucro_reais_por_mes = lucro_abs × 30 / 30 = lucro_abs` — operador via "Lucro/mês = Lucro total". Floor 60d em `_FLOOR_DIAS_GIRO_DISPLAY` corrige sem zerar o sinal de lotes onde calibração via Arrematado já deu giro <60d.

### Categoria do veículo é canônica em `calibracao_giro._categoria_de_modelo`
`_calcular_frete(lote, empresa, categoria=None)` no orquestrador aceita categoria pré-resolvida (laudo + fallback do pipeline). Quando `None`, usa `_categoria_de_modelo` da MESMA tabela do calibrador. Antes do fix, o frete tinha lista reduzida (5 marcas) enquanto o calibrador tinha 50 — Toro virava OUTRO no frete mas PICAPE no giro. Não criar terceira lista de keywords pra qualquer feature nova: passa pela função canônica.

### Threshold ROI absurdo — 500%, não 1000%
`audit.CHECKS["ROI anualizado (%)"]` flaga >500% (era 1000%). Calibração: operação real Reinaldo 21 carros = ~60-75% ano linear; Polo Track real = 21% em 7 meses. ROI >500% num leilão de carros é matematicamente possível mas operacionalmente irreal — quase sempre indica `dias_giro` otimista colidindo com fatores no teto. Mensagem aponta a causa raiz provável.

### Audit deve espelhar o exporter
`audit._build_rows` filtra `lote.fim_em is None` igual ao `SheetsExporter._query` — sem isso reportava violações em lotes invisíveis na UI (alarme falso). Teste guarda: `TestAuditParidadeSheets`. Quando `SheetsExporter` filtrar coisa nova, replicar no audit.

### Audit espelha TODAS as supressões do display, não só `fim_em is None` (2026-05-07)
Lotes inviáveis (`lance_atual > preco_max`) substituem ROI/Lucro/Tese por `—` em `SheetsExporter._write_sheet:422-429`. Antes do fix, `audit.COLUMN_EXTRACTORS` retornava o valor numérico cru — então `_score_roi_efetivo` recalculado com `capital_ef > preco_giro` dava ROI anualizado negativo (Fiesta ESTRUTURAL real: -53.9%) e o validator "ROI anualizado negativo" disparava em lotes que o operador NUNCA via na planilha. Falso alarme operacional. Fix: `COLUMN_EXTRACTORS["ROI anualizado (%)"]/["Lucro/mês (R$)"]/["Tese"]` retornam `"—"` quando `r["viavel"] is False`. Teste guard: `TestAuditEspelhaDisplay` em `tests/test_audit_columns.py`. **Padrão genérico:** quando uma view do display SUBSTITUI um valor por placeholder (não só filtra a linha), o audit precisa fazer o mesmo — caso contrário sinaliza "violação" invisível ao operador.

### Cap defensivo no precificador `preco_giro_fipe ≤ FIPE × 1.20` (2026-05-07)
Cap mediana similares Auto Avaliar (`FIPE × 1.20` quando n<5) + f_km saturado (1.15) podiam multiplicar pra `1.38 × FIPE` no `preco_giro_fipe` — lance proposto saía acima da FIPE em cenários adversariais (similares premium dominando + lote km baixa). Cenário 10 da simulação: preco_max ia a 111.4% FIPE (audit alertava `Lance Máximo > FIPE × 1.05`). Cap final em 1.20×FIPE no precificador (`_PRECO_GIRO_FIPE_TETO_PCT_FIPE`) preserva Civic/Corolla ~110% em alta sem permitir combinação patológica. Audit threshold `_PRECO_GIRO_FIPE_RATIO_MAX = 1.10` continua avisando como dado fraco. Teste guard: `test_preco_giro_fipe_capado_em_120pct_fipe`. **Defesa em camadas: 3 caps com propósitos distintos** — entrada (avaliador_mercado, n<5 outlier), saída (precificador, qualquer caminho de mediana inflada), alarme (audit, ainda dentro do cap mas dado fraco). Não compartilham constante; relaxar/apertar um lado não devia mover o outro automaticamente.

### `_score_roi_efetivo` defensivo contra `preco_alvo=None`
`AvaliacaoLote.preco_alvo` é non-nullable hoje, mas migrações antigas podem deixar NULL. Antes do fix de 2026-05-07, `lance_atual - av.preco_alvo` levantava `TypeError` silencioso (engolido em layers superiores) que quebrava a planilha inteira porque `_score_roi_efetivo` é chamado por linha. Fix: `alvo = av.preco_alvo or 0` antes de comparar/subtrair. Padrão genérico: helper que recebe SQLModel "non-nullable" deve coalescer mesmo assim — schema atual ≠ histórico do DB. Teste guard: `test_preco_alvo_none_nao_quebra`.

### Audit checks com 3+ ramos viram if/elif encadeado, escondendo red flags (2026-05-08)
Revisão preventiva expôs `CHECKS["Lance Máximo (R$)"]` aninhando 4 condições — só o 1º motivo emergia. Cenário simulado: lote com **zona apertada** (yellow, lance > alvo) + **preco_max > FIPE × 1.05** (red flag) disparava SÓ o yellow; operador ignorava amarelo e se aproximava de dar lance acima da FIPE. Refatorado em 3 funções independentes em `ALL_CHECKS` que retornam `List[CheckResult]` — múltiplos sintomas coexistem. **Padrão genérico:** quando um lambda em CHECKS tem `else (if ...)` aninhado 2+ níveis, mover pra função independente em ALL_CHECKS antes de adicionar mais ramos. Encadeamento vira ponto cego garantido em casos patológicos. Padrão registrado em LESSONS.md/P5d.

### Antes de adicionar audit check novo, perguntar "ele coexiste com checks existentes na mesma linha?"
Se SIM (caso típico: cross-check de coluna multi-faceta como Lance Máximo, Reforma, FIPE), criar função em `ALL_CHECKS`. Se NÃO (validação de domínio simples como "Ano em [1980, agora+1]"), pode ficar em `CHECKS` dict. Erro recorrente: empurrar tudo pra `CHECKS` lambda porque "é a coluna X" — esquece que lambda só retorna 1 motivo.

### Reforma > 30% do preco_giro = lote economicamente questionável (2026-05-08)
Mesmo passando pela margem do precificador, reforma pesada significa capital empatado num gasto NÃO recuperável diretamente (oficina vs. revenda) + risco de surpresa de oficina (estimativa inicial subestima o real) + revenda mais lenta (histórico de avaria assusta comprador). Audit `_check_reforma_pesada` flaga a partir de 30% — só pra lote viável (em inviável display oculta tudo, audit acompanha por paridade P5c). Threshold é heurístico; calibrar com Arrematado quando tiver ≥10 vendas com `gastos_reforma_real`.

### Workflow de revisão diária — confiar no que a simulação mostra
Quando o usuário pede "revise e corrija autonomamente" sem direcionar, o caminho que voltou a funcionar (4ª vez consecutiva) foi: (1) `make test` baseline → (2) ler precificador + sheets + audit + agentes principais → (3) ESCREVER UM SCRIPT DE SIMULAÇÃO (`/tmp/sim.py`) com **8-10 cenários reais** (gold + edge cases f_km no teto/piso + ESTRUTURAL conf baixa + mediana inflada + zona apertada + lote inviável + dias_giro otimista + km=None) → (4) imprimir todas as colunas da planilha pra cada cenário e validar identidades algébricas + relação cross-field → (5) só DEPOIS confirmar bugs e refatorar. Cobertura mínima de cenários: gold + saturação de cada bound + caso patológico onde MÚLTIPLOS sinais coexistem (esses revelam P5d). Pular a simulação leva a "fixes" baseados em leitura — frequentemente errados.

### Audit deve espelhar TODA dimensão de supressão do display, não só `viavel` (2026-05-09)
`SheetsExporter._write_sheet` suprime colunas em pelo menos 2 condições:
1. `viavel=False` (lance > preco_max) → ROI / Lucro / Tese viram "—"
2. `laudo_analisado=False` (confidence < 0.6 ou laudo ausente) → "⚠ LAUDO NÃO CAPTURADO" + Lance Máximo / Lucro / ROI / Reforma / Tese viram "—"

Cada dimensão exige paridade no audit — caso contrário sobram falsos alarmes. Em 2026-05-09 o audit cobria só (1); lotes com laudo fallback `_laudo_sem_pdf` (confidence 0.55, marca ESTRUTURAL sem peça) disparavam "Reforma R$ 0 com severidade estrutural" enquanto display oculta tudo. Fix: `_build_rows` calcula `laudo_analisado` e `COLUMN_EXTRACTORS` + cross-checks (`_check_zona_apertada`, `_check_lance_maximo_acima_fipe`, `_check_reforma_pesada`, `_check_motor_problema`, `_check_severidade_estrutural`) respeitam ambas as dimensões. **Padrão genérico: cada `if condicao_X: cell = "—"` no exporter precisa de paridade explícita no audit. Lista cresce; ler `_write_sheet` end-to-end antes de adicionar novo extractor.**

### Cross-checks operacionais que o precificador NÃO modela explicitamente (2026-05-09)
O precificador penaliza `motor_ok=False` e `severidade=ESTRUTURAL` via `fator_risco` (peso 0.3 em motor; severidade ESTRUTURAL → 1.0 saturando). Mas o teto saturado SÓ não basta pra descartar — com lance suficientemente baixo, o lote passa como "✓ Viável" sem qualquer alerta visual além de Reforma elevada. Operador focado em ROI alto pode dar lance num carro com motor problemático ou estrutural reparado. Fix: `_check_motor_problema_em_viavel` e `_check_severidade_estrutural_em_viavel` em `audit.py` disparam warning explícito mesmo quando o sistema deixa passar. **Padrão genérico: condição econômica = "passa pelo precificador", condição operacional = "deve gerar warning visível pro operador". Modelar AS DUAS, não só a primeira.**

### Thresholds com baixa margem do max natural viram bombas-relógio (2026-05-09)
`_PRECO_GIRO_FIPE_RATIO_MAX = 1.10` foi escolhido com referência ao max natural `_FATOR_MAX × 0.95 = 1.15 × 0.95 = 1.0925`. Gap de SÓ 0.75pp. Qualquer aumento futuro de `_FATOR_MAX` (ex.: 1.15 → 1.20 pra acomodar lotes super-baixa-quilometragem) dispararia falso positivo automático sem warning. Fix: 1.10 → 1.13 (margem ergonômica de ~3.5pp), comentário cruzando ref com `_FATOR_MAX` em `ajuste_km.py`. **Padrão genérico: ao definir um threshold de "guard de regressão" calibrado pelo max teórico de uma constante em outro arquivo, deixar margem ≥ 2-3pp E referenciar a constante no comentário. Quem altera a constante futuramente vai ver o threshold cruzando — caso contrário o gap silenciosamente fecha.**

### Validators de `CHECKS` precisam tolerar "—" sem TypeError (2026-05-09)
`COLUMN_EXTRACTORS` retorna `"—"` (string) quando o display oculta o número. Validators que comparam `v <= 0` ou `v < 0` levantam `TypeError` em string. Fix: cada validator de coluna numérica que pode receber "—" precisa de guarda `if not isinstance(v, (int, float)): return None` ANTES de qualquer comparação. **Padrão genérico: ao adicionar uma supressão de display nova (campo X vira "—" em condição Y), validar que TODOS os validators de X têm guard isinstance — caso contrário audit quebra silenciosamente em runtime quando o caminho da supressão é exercitado.**

### Upsert que reconstrói "container" parcial perde subkeys de outras camadas (2026-05-10)
Bug DD3: `_upsert_lote(lote_raw, ...)` reconstruía `raw_json` a partir do `LoteRaw.model_dump()` (listagem — sem `detalhe`) e só preservava `loja` da `raw_json` existente. `detalhe.laudo_pdf_url` e `body_text_sample` (escritos por `_persistir_flags_no_lote` após o scraper de detalhe) eram ZERADOS em todo cron diário. Bug latente por meses — pipeline rodava completo a cada run e `coletar_detalhe` repopulava. Após DD2 (2026-05-09 — short-circuit estrito + state/db persiste PDF), o pipeline passou a PULAR esses lotes (cache + PDF OK no DB) e a URL nunca mais voltava: 95/187 lotes ativos = 51% perderam coluna "Ver laudo". **Padrão genérico: quando uma operação de upsert reconstrói um container (raw_json dict, JSONB) a partir de payload parcial, enumere TODAS as subkeys que outras camadas escrevem e preserve uma a uma — não confie em "lembrar". Defesa em profundidade: critério de short-circuit + filtros de retry devem cobrir TODAS as condições que a auditoria valida (paridade total). Auditoria reportar X mas retry/short-circuit não pegar X = laço aberto.** Padrão registrado em LESSONS.md/P5f.
