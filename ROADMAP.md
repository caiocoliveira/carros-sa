# Carros SA — Roadmap

Documento vivo. Cada sessão atualiza seu workstream ao mergear em `main`.

## Status atual (baseline)

✅ **Fundação da PoC rodando** — 25/25 testes passando

| Componente | Arquivo | Cobertura |
|---|---|---|
| Contratos Pydantic + SQLModel (8 tabelas) | [`carros_sa/models.py`](carros_sa/models.py) | schema estável |
| DB engine SQLite + init idempotente | [`carros_sa/db.py`](carros_sa/db.py) | smoke test |
| Tenancy (EmpresaConfig YAML + frete lookup) | [`carros_sa/tenancy.py`](carros_sa/tenancy.py) | 2 gold tests |
| Precificador (Python puro, risco + liquidez) | [`carros_sa/precificador.py`](carros_sa/precificador.py) | 9 gold tests + multi-empresa |
| Parser Auto Avaliar (listagem + detalhe) | [`carros_sa/scraping/parsers.py`](carros_sa/scraping/parsers.py) | 11 gold tests com dado real |
| ExtratorLaudo textual (PyMuPDF) | [`carros_sa/agents/extrator_laudo.py`](carros_sa/agents/extrator_laudo.py) | 4 testes c/ PDF real |
| VisionClient abstrato + 3 impls | [`carros_sa/agents/vision_clients.py`](carros_sa/agents/vision_clients.py) | validado c/ Fiesta real |
| ExtratorLaudo consolidação (textual+visão) | idem | gold test com fixture Gemini |
| Config empresas (Uberlândia + SP fake) | [`config/empresas/*.yaml`](config/empresas/) | estáveis |
| Script ingest listagem → SQLite | [`scripts/ingest_listagem.py`](scripts/ingest_listagem.py) | 10 lotes persistidos |
| Script extrair laudo standalone | [`scripts/extrair_laudo.py`](scripts/extrair_laudo.py) | funciona c/ Gemini Flash |

## Dados reais já coletados

- **10 lotes** de Uberlândia/MG em `data/scrapes/2026-04-14_uberlandia_listagem.json` (Fiesta, Saveiro, Compass ×2, Gol, Cruze, Haval H6, S10, Range Rover Evoque, Grand Vitara)
- **1 PDF de laudo** em `data/laudos_amostra/21854782_fiesta.pdf` (Fiesta 2013 REPROVADO ESTRUTURAL — colunas B/C esq. reparadas)
- **1 fixture de visão** em `tests/fixtures/21854782_visual_gemini.json`

---

## Workstreams paralelos (podem rodar em sessões separadas JÁ)

Cada um vai em seu próprio worktree git. Independentes entre si até o Orquestrador (E).

### A — AvaliadorMercado 📋 pendente
- **Branch:** `feat/avaliador-mercado`
- **Escopo:** `carros_sa/agents/avaliador_mercado.py` — `avaliar(marca, modelo, ano, km) -> SinalMercado`
- **Componentes:** cliente FIPE (`fipe.parallelum.com.br`) em `carros_sa/tools/fipe.py` + uso dos similares da própria plataforma Auto Avaliar (parser `parse_detalhe` já expõe em `similares_precos`)
- **Não escopo:** Webmotors (workstream B).
- **Critério de aceite:** gold test comparando FIPE de Fiesta 2013 vs. preços de similares que a plataforma mostrou (45k, 23.2k, 27k, etc). Cache em tabela `modelo_fipe_cache`.

### B — Webmotors Scraper 📋 pendente
- **Branch:** `feat/webmotors-scraper`
- **Escopo:** `carros_sa/tools/webmotors.py` com Playwright + stealth. Função `estatisticas(marca, modelo, ano) -> (p25, mediana, n_anuncios)` persistindo em `anuncio_webmotors`.
- **Riscos:** anti-bot agressivo. Antes de atacar, ler CLAUDE.md section "Red flags". Começar com 1 busca manual, cache 24h.
- **Critério de aceite:** integração marcada `@pytest.mark.slow` roda e popula ≥5 anúncios; unitário cobre parser dos resultados offline.

### C — EstimadorReforma 📋 pendente
- **Branch:** `feat/estimador-reforma`
- **Escopo:** `carros_sa/agents/estimador_reforma.py` — `estimar(laudo: LaudoEstruturado, empresa: EmpresaConfig) -> CustoReforma`
- **Componentes:** tabela por empresa `config/reforma/<empresa>.yaml` com preços por (peça, severidade). Default determinístico (sem LLM). Fallback pra `VisionClient` só quando laudo tiver texto livre útil.
- **Critério de aceite:** gold test com o Fiesta 21854782 → 2 colunas GRAVE → custo total elevado com range (min/max).

### D — ScraperDetalheLote 📋 pendente
- **Branch:** `feat/scraper-detalhe`
- **Escopo:** módulo + script que, dada URL de um lote, usa Chrome MCP (ou Playwright) pra: (1) extrair `innerText` do body, (2) passar pelo `parse_detalhe` existente, (3) baixar PDF do laudo, (4) persistir `laudo` + enriquecer `lote.raw_json`.
- **Early-exit obrigatório:** respeitar `DetalheFlags.early_exit` — se retornar valor, pular PDF e LLM.
- **Critério de aceite:** rodar nos 10 lotes já em SQLite e imprimir tabela de quantos passaram vs. quantos foram descartados (esperado: ≥1 reprovado estrutural na amostra real).

---

## Workstreams sequenciais

### E — Orquestrador 🔒 bloqueado
Depende de **A + (B ou similares) + C + D**. Vai em `carros_sa/orquestrador.py`. Usa `asyncio` com `Semaphore(8)`, parametriza por `empresa_id`.

### F — CLI 🔒 bloqueado
Depende de E. Vai em `carros_sa/cli.py` com Typer. Comando principal: `carros-sa triagem <url_leilao> --empresa=<id> --top 10`.

### G — Tracking longitudinal Webmotors 🕐 futuro
Cron semanal que popula `anuncio_webmotors.sumiu_em`. Só faz sentido depois de ≥2 semanas de coleta contínua.

### H — Calibração coeficientes 🕐 futuro
Relatório DuckDB `preco_alvo vs arrematado.preco_real`. Só ativa após 5+ linhas em `arrematado` (dados reais de compras).

---

## Convenções de coordenação

### Quem mexe em `carros_sa/models.py`?
**Ninguém sem discussão.** Se workstream precisa de campo novo: (a) comenta aqui no ROADMAP na seção "Pendências de schema", (b) abre issue/mensagem, (c) espera merge coordenado antes de começar.

Pendências de schema:
- _(nenhuma ainda)_

### Como mergear?
1. Rodar `make test` no worktree — 100% verde, incluindo teste novo
2. Marcar workstream aqui como ✅ com link pro arquivo principal
3. `git checkout main && git merge --no-ff feat/<workstream>`
4. Se conflito em `models.py` → **pare**, não resolva sozinho

### Onde logar descobertas?
Memória persistente: `/Users/caiocoliveira/.claude/projects/-Users-caiocoliveira-Carros-SA/memory/`

Já registrado:
- `project_carros_sa_overview.md` — escopo + stack + decisões fixadas
- `project_autoavaliar_estrutura.md` — DOM + campos + red flags de Auto Avaliar

---

## Marcos (do plano arquitetural original)

- ✅ **M1** — Scraper listagem + 10 LoteRaw no SQLite
- ✅ **M2** — ExtratorLaudo validado em lote real (Gemini Flash, custo zero)
- 🔄 **M3** — AvaliadorMercado (FIPE) — _workstream A_
- 🔄 **M4** — EstimadorReforma + tabela base — _workstream C_
- 🔄 **M5** — ScraperDetalheLote end-to-end — _workstream D_
- 🔒 **M6** — Orquestrador paralelo + top-10 ranking — _workstream E_
- 🔒 **M7** — Frete first-class integrado (já temos tabela YAML; falta wire-up)
- 🔒 **M8** — Multi-tenancy rodando (schema pronto; falta rodar na prática)
- 🕐 **M9** — Re-check semanal Webmotors — _workstream G_
- 🕐 **M10** — Calibração isolada por tenant — _workstream H_
