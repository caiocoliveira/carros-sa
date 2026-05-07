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

### Naming hint — `preco_giro_fipe`
O campo `preco_giro_fipe` em `Avaliacao`/`AvaliacaoLote` é literalmente `webmotors_mediana × f_km`, NÃO `min(FIPE × 0.95, webmotors_p25)`. Quando Webmotors live ainda não está conectado, `webmotors_mediana` é populado pelo `AvaliadorMercado` como `FIPE × 0.97` (ver `agents/avaliador_mercado.py:134`). Daí o nome ser inerte: a fonte primária é a mediana de mercado, com fallback indireto pra FIPE. `webmotors_p25` é exposto em `SinalMercado` mas hoje **não é consumido** pelo precificador (versão antiga usava). Não renomear o campo persistido sem coordenar — é contrato (models.py).

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
