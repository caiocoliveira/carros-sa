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

**Regra adicional (2026-04-30):** a simulação deve cobrir ≥3 cenários DIVERSOS, não só Polo Track. Adicionar um lote "extremo" (km ultra-baixa pra forçar `f_km` no teto) + um "fallback" (sem `webmotors_km_mediana`) + um "estrutural" (Fiesta) revela bugs invisíveis no caminho feliz. Foi assim que apareceu `preco_giro > FIPE × 1.15` no Onix sintético da revisão X.

### Benchmark operacional vs limite matemático
Toda invariante numérica do audit precisa de threshold calibrado contra **benchmark operacional**, não contra o limite teórico do cálculo. Exemplo (workstream X, 2026-04-30): ROI > 1000% é matematicamente possível, mas o operador real (Reinaldo, 21 carros / 3 meses médios) rende ~60-75% ao ano. Threshold de 1000% deixava passar lotes mostrando "526% ROI" — número correto pelo cálculo, irreal pelo negócio. Apertei pra 500% e mensagem explícita aponta a causa raiz provável (dias_giro otimista ou margem×fator inflado).

Antes de fechar workstream que toca métrica derivada, anotar no código (comentário no audit.py por exemplo) o benchmark operacional usado pra calibrar o threshold. Se o número não está documentado, não está calibrado.

### Audit deve espelhar o exporter
Regra de ouro (workstream X, 2026-04-30): se `SheetsExporter._query` filtra alguma coisa, `audit._build_rows` filtra o MESMO predicado. Sem isso o audit reporta lote que o operador não consegue confirmar abrindo a UI — ruído puro. Teste de paridade obrigatório (ver `TestAuditParidadeSheets` em `tests/test_audit_columns.py`).
