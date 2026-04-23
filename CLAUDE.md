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

## Checklist de revisão de código (auto-review periódico)

Quando o usuário pedir "revisa o código" ou "encontra bugs", seguir nessa ordem:

1. **Rodar o test suite antes de qualquer edição** — precisa de baseline pra saber o que é regressão. Se faltar dep local, instalar mínimo via pip (venv do projeto pode estar em diretório que não existe no ambiente atual).
2. **Docstring vs implementação**: procurar drift. Nomes enganosos (ex.: `preco_giro_fipe` calculado de `webmotors_mediana`) são pista forte de que a intenção diverge do código — ou docstring desatualizado, ou bug.
3. **Sanity bounds quando eles são esperados**: se o domínio tem "teto" humano (FIPE como teto de lance, reforma ≥ 0, ROI ≤ X%), checar se o código respeita. A pergunta "faz sentido que X seja maior que Y?" do usuário vira CHECK no código.
4. **Sinal faltante ≠ sinal neutro ≠ sinal ótimo**: cada `None`/`0`/vazio tem semântica própria. Tratar "não sei" como "mercado perfeito" vira viés positivo (faz o sistema recomendar com confiança indevida). Valida caso a caso antes de achatar.
5. **Guards em objetos optional**: `x.exists()`, `x.attr`, `x[0]` exigem `x is not None` quando `x` é atribuído em branches (ex.: `pdf_dest = None` no `except`). Linter passa, runtime crasha.
6. **Toda correção precisa de teste de regressão** — caso contrário volta no próximo refactor.
7. **Atualizar glossário da planilha + audit quando muda coluna** — o glossário documenta pro usuário, o audit detecta regressões futuras. Esquecer um dos dois = dívida técnica silenciosa.

## Aprendizados acumulados

- A planilha enxuta (14 → 15 colunas em 2026-04-23) agora inclui FIPE explícita — usuário precisa comparar Lance Máximo com FIPE pra sanity check antes de dar lance. Sem isso, qualquer bug de ancoragem de preço vira lance ruim invisível.
- Quando for adicionar coluna nova à planilha: `HEADER` + `COLUMN_FORMATS` + `_query` (dict) + `_write_sheet` (lista) + `_write_glossario_sheet` + `audit.CHECKS` + `audit.COLUMN_EXTRACTORS`. São 6 lugares — testes de regressão cobrem 2 deles.
