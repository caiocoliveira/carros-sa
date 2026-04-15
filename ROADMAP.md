# Carros SA — Roadmap

Documento vivo. Cada sessão atualiza seu workstream ao mergear em `main`.

## Status atual (baseline)

✅ **Fundação + EstimadorReforma** — 35/35 testes passando

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

### A — AvaliadorMercado ✅
- **Branch:** `feat/avaliador-mercado`
- **Arquivos:** [`carros_sa/agents/avaliador_mercado.py`](carros_sa/agents/avaliador_mercado.py), [`carros_sa/tools/fipe.py`](carros_sa/tools/fipe.py)
- **Cobertura:** 4 testes em [`tests/test_avaliador_mercado.py`](tests/test_avaliador_mercado.py) com fixture FIPE real ([`tests/fixtures/fipe_fiesta_2013.json`](tests/fixtures/fipe_fiesta_2013.json)) — Fiesta 2013 FIPE R$ 30.876 + similares reais do lote 21854782, cache persistente em `modelo_fipe_cache` e fallback FIPE-only.
- **Pendente:** trocar fonte de mediana/p25 por Webmotors quando workstream B chegar (contrato `SinalMercado` já preparado).

### B — Webmotors Scraper ✅
- **Branch:** `feat/avaliador-mercado` (worktree `amazing-saha`)
- **Arquivo:** [`carros_sa/tools/webmotors.py`](carros_sa/tools/webmotors.py)
- **Fixture real:** [`data/scrapes/2026-04-14_webmotors_fiesta.json`](data/scrapes/2026-04-14_webmotors_fiesta.json) — 26 cards coletados via Chrome MCP.
- **Cobertura:** 18 testes em [`tests/test_webmotors.py`](tests/test_webmotors.py) — gold com dado real (Fiesta 2013: n=11, mediana=35900, p25=33745), parse de badges/variações, `estatisticas()` com `fetch=` injetável.
- **Coleta ao vivo:** `_fetch_playwright()` é esqueleto que levanta `NotImplementedError` — ligar no workstream G com rate-limit + stealth.

### C — EstimadorReforma ✅ concluído
- **Branch:** `claude/charming-newton`
- **Arquivos:** [`carros_sa/agents/estimador_reforma.py`](carros_sa/agents/estimador_reforma.py) + tabelas em [`config/reforma/`](config/reforma/) (Uberlândia + SP) + [`tests/test_estimador_reforma.py`](tests/test_estimador_reforma.py) (10 testes)
- **Implementação:** determinístico, sem LLM. Avarias são casadas a famílias de peça (longarina, coluna, porta, paralama, capô/tampa, teto, painel) por prefixo do nome; custo = célula `(família × severidade)` da tabela YAML. Adicional fixo de chassi quando severidade_geral é ESTRUTURAL; adicional motor isolado quando motor_ok=False sem ser ESTRUTURAL (sem dupla contagem). Range = ±incerteza_pct.
- **Gold test Fiesta 21854782:** 2 colunas GRAVE + adicional estrutural → R$10.000 em MG (range R$7.500–12.500), R$12.800 em SP (mão de obra metropolitana). Ambos > R$5k confirmando "custo elevado".

### D — ScraperDetalheLote ✅ entregue
- **Branch:** `claude/kind-goodall` (worktree `kind-goodall`)
- **Arquivos:** [`carros_sa/scraping/scraper_detalhe.py`](carros_sa/scraping/scraper_detalhe.py), [`scripts/processar_detalhes.py`](scripts/processar_detalhes.py), cache em [`data/detalhes/<lote_id>.json`](data/detalhes/)
- **Como funciona:** `processar_detalhe(lote_id, body_text, laudo_pdf_url, session, pdf_dir, downloader=...)` aplica `parse_detalhe`, persiste flags em `lote.raw_json["detalhe"]`, baixa o PDF SOMENTE se `DetalheFlags.early_exit is None`. Fetch fica fora do módulo (Auto Avaliar é JS-pesado → coletado via Chrome MCP, salvo em `data/detalhes/`).
- **Cobertura:** 4 testes em [`tests/test_scraper_detalhe.py`](tests/test_scraper_detalhe.py): (a) Fiesta real → `reprovado_estrutural`, downloader nunca chamado; (b) caso feliz → baixa PDF; (c) sem URL → no-op; (d) lote inexistente → ValueError.
- **Critério de aceite atingido:** `make ingest && PYTHONPATH=. .venv/bin/python scripts/processar_detalhes.py` nos 10 lotes de Uberlândia → **9 passaram** + **1 descartado** (Fiesta `reprovado_estrutural`) + **7 PDFs baixados** (Gol e Cruze tinham `pdf=null` no DOM — link em modal lazy). innerText dos 9 coletado via Chrome MCP no padrão `PING + read{clear:true} + emit + read` (driblando `console.clear()` anti-debug do AutoAvaliar).

---

## Workstreams sequenciais

### E — Orquestrador 🔒 bloqueado
Depende de **A + (B ou similares) + C + D**. Vai em `carros_sa/orquestrador.py`. Usa `asyncio` com `Semaphore(8)`, parametriza por `empresa_id`.

### F — CLI 🔒 bloqueado
Depende de E. Vai em `carros_sa/cli.py` com Typer. Comando principal: `carros-sa triagem <url_leilao> --empresa=<id> --top 10`.

### I — Exportador Google Sheets ✅
- **Branch:** `claude/laughing-dewdney`
- **Arquivos:** [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py), [`scripts/exportar_sheets.py`](scripts/exportar_sheets.py)
- **Uso:** `make sheets EMPRESA=uberlandia_mg`
- **O que mudou:** `SheetsExporter` lê `AvaliacaoLote + Lote + LaudoCache` do SQLite e escreve aba `<empresa_id>` no Google Sheets (17 colunas, rankeado por score_roi desc, header congelado). Auth via service account JSON. Sem Orquestrador rodado, exporta 0 linhas — mas módulo e testes estão prontos.
- **Cobertura:** 6 testes em [`tests/test_exportar_sheets.py`](tests/test_exportar_sheets.py) (ROI, ordenação, sem laudo, sem avaliações), todos mockando gspread.
- **Limitações:** depende de `avaliacao_lote` populado (workstream E). Setup one-time: service account JSON + compartilhar Sheet com e-mail do SA.

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
- ✅ **M3** — AvaliadorMercado (FIPE) — _workstream A_
- ✅ **M3-B** — Webmotors parser + estatísticas (coleta ao vivo pendente, workstream G) — _workstream B_
- ✅ **M4** — EstimadorReforma + tabela base — _workstream C_
- ✅ **M5** — ScraperDetalheLote end-to-end (módulo + script + cache de 10 lotes) — _workstream D_
- 🔒 **M6** — Orquestrador paralelo + top-10 ranking — _workstream E_
- 🔒 **M7** — Frete first-class integrado (já temos tabela YAML; falta wire-up)
- 🔒 **M8** — Multi-tenancy rodando (schema pronto; falta rodar na prática)
- 🕐 **M9** — Re-check semanal Webmotors — _workstream G_
- 🕐 **M10** — Calibração isolada por tenant — _workstream H_
