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
`/Users/caiocoliveira/.claude/projects/-Users-caiocoliveira-Carros-SA/memory/MEMORY.md`

Descobertas valiosas (flags de negócio, decisões fixadas, quirks de fornecedor) viram entrada nessa pasta — e a próxima sessão já lê sem precisar redescobrir.

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

## Aprendizados acumulados (sempre relevante)

### Auditoria de colunas — colunas exportadas são contratos numéricos, não "valores mágicos"

Sempre que uma coluna da planilha mudar de fórmula, **rodar o pipeline inteiro na cabeça**:
1. O que essa fórmula é algebricamente equivalente a?
2. A nova fórmula usa um campo persistido, ou reconstrói algo que já foi calculado uma vez pelo precificador?
3. Se já existe no `AvaliacaoLote`, use direto — NÃO recalcule derivando a partir do `preco_max`, pq `preco_max` é o teto (já embute margem mínima) e vai virar tautologia.

Exemplos concretos já cometidos:
- `ROI anualizado` inicialmente usava `_calcular_roi_no_maximo` que por construção resulta em ~margem_mínima (~11%) → ranking virou insensível ao fator de risco. Trocado por `av.score_roi × 365/dias_giro`.
- `Lucro/mês` usava `score_roi × preco_alvo`, que subestima em 15-40% porque `capital_alvo = preco_alvo + reforma + frete + taxas + custo_op` é sempre > `preco_alvo`. Identidade correta: `retorno_alvo = preco_giro × score_roi / (1 + score_roi) = score_roi × capital_alvo`.

### Fixture de teste: defaults têm que ser self-consistent

Fixture `_avaliacao` com `preco_alvo=25000` hardcoded é uma bomba-relógio — qualquer teste que passar `preco_giro` ou `score_roi` arbitrários pode gerar combinações impossíveis (ex: `preco_giro=30k, score_roi=0.25` implica `capital_alvo=24k < preco_alvo=25k`). Regra: se um teste passa `preco_giro` e `score_roi`, passe também `preco_alvo` coerente — senão o teste vai medir coisa errada. Ideal: fixture deriva valores dependentes ao invés de hardcodear.

### Auditoria cruzada é mais valiosa que por-coluna

Checagens por coluna pegam erros óbvios (reforma negativa, KM absurdo). Checagens CRUZADAS pegam os sutis e mais caros — aqueles que batem no bolso:
- `preco_max > FIPE × 1.05` → capital bruto excede âncora de revenda
- `preco_giro_fipe` muito longe da FIPE → webmotors outlier ou parsing errado
- `preco_max` "viável" mas `score_roi < 0.05` → teto é formal, não econômico

Mantenha `INVARIANTES_INTERNAS` em `carros_sa/tools/audit.py` separado de `CHECKS` (que é alinhado com HEADER) — paridade HEADER↔CHECKS é teste importante e não deve ser poluída com campos internos.

### Dependências de teste: use sempre PYTHONPATH=. + pytest

Repo tem `pyproject.toml` com `configfile`. Comandos que funcionam em qualquer env:
```bash
PYTHONPATH=. python -m pytest tests/ -v
PYTHONPATH=. python -m pytest tests/<arquivo>.py::ClasseX::test_y -v
```

Se o venv `.venv/` não existir (ex: sessão em container CI), use `python` do sistema — normalmente já tem as deps de `.[dev]` via instalação de base. Não tente recriar venv sem necessidade.

### Naming honesto > naming aspiracional

Se um campo se chama `preco_giro_fipe` mas na verdade é `webmotors_mediana × f_km` (ajuste de KM), isso é um bug de nome que atrasa todo mundo que ler o código depois. Quando encontrar esse padrão: ou renomeie no DB/migração, ou pelo menos coloque docstring gritando a discrepância. Nomes que mentem são débito técnico silencioso.
