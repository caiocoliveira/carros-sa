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
| Parser Auto Avaliar (listagem + detalhe + preços AA) | [`carros_sa/scraping/parsers.py`](carros_sa/scraping/parsers.py) | 19 gold tests com dado real |
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

### E — Orquestrador ✅
- **Branch:** entregue dentro do merge do workstream J (`claude/laughing-dewdney`)
- **Arquivo:** [`carros_sa/orquestrador.py`](carros_sa/orquestrador.py)
- **Como funciona:** `orquestrar(empresa_id, session, page, vision_client, horizonte_dias=7)` coleta listagem via Playwright, faz upsert de `Lote`, e para cada lote novo roda pipeline completo: `coletar_detalhe` → `parse_detalhe` (early_exit curto-circuita) → `baixar_pdf` → `extrair_laudo` → `avaliar_mercado` (FIPE + similares) → `estimar_reforma` → `_calcular_frete` (heurística UF + tabela da empresa) → `precificar` → upsert `AvaliacaoLote + LaudoCache`. Retorna `OrquestradorResult` com contagens de coletados/novos/avaliados/descartados/erros + `List[ResultadoLote]`.
- **Cobertura:** 6 testes em [`tests/test_orquestrador.py`](tests/test_orquestrador.py) — frete por UF (mesmo/adjacente/distante), categoria de veículo inferida do modelo, `_laudo_sem_pdf` conservador, idempotência (lote já avaliado não re-processa), upsert de Lote.
- **Limitações:** processamento pós-PDF ainda sequencial (não usa `Semaphore(8)` como previa o plano original) — scraping é naturalmente sequencial por anti-bot, e o resto foi mantido sync por simplicidade no MVP. Destravar paralelismo só quando houver volume que justifique.

### F — CLI ✅
- **Branch:** `claude/awesome-rosalind`
- **Arquivo:** [`carros_sa/cli.py`](carros_sa/cli.py) + entry point `carros-sa` em [`pyproject.toml`](pyproject.toml)
- **Subcomandos:**
  - `carros-sa triagem --empresa <id> [--horizonte-dias 7] [--no-headless] [--sem-sheets] [--top 10]` — pipeline completo (thin wrapper sobre `orquestrador.orquestrar`)
  - `carros-sa top --empresa <id> [--n 10]` — ranking offline via SELECT no SQLite (não depende de scraping)
  - `carros-sa ingest <arquivo.json>` — JSON de listagem → SQLite (upsert)
  - `carros-sa extrair-laudo <pdf>` — ExtratorLaudo standalone
  - `carros-sa sheets --empresa <id>` — exporta avaliações pro Google Sheets
  - `carros-sa empresas` — lista configs em `config/empresas/`
- **Cobertura:** 11 testes em [`tests/test_cli.py`](tests/test_cli.py) via `typer.testing.CliRunner` — help lista subcomandos, `top` ranqueia por ROI desc + filtra por empresa + respeita `--n`, `empresas` lê YAML sem erro, validação de credenciais de `triagem`/`sheets`, ingest real dos 10 lotes de Uberlândia.
- **Makefile:** alvos `make triagem`, `make triagem-debug`, `make top`, `make empresas`, `make sheets` agora usam `python -m carros_sa.cli` (scripts/ mantidos como libs, CLI é a interface pública).

### J — Pipeline Diário Automatizado ✅
- **Branch:** `claude/laughing-dewdney`
- **Arquivos:** [`carros_sa/scraping/scraper_autoavaliar.py`](carros_sa/scraping/scraper_autoavaliar.py), [`carros_sa/orquestrador.py`](carros_sa/orquestrador.py), [`scripts/triagem_diaria.py`](scripts/triagem_diaria.py), [`scripts/setup_cron.sh`](scripts/setup_cron.sh)
- **Uso:** `make triagem` (ou `make triagem-debug` para browser visível). Ativa cron: `make setup-cron`.
- **O que mudou:** Playwright scraper com login/senha + cookies persistentes coleta lotes do Auto Avaliar por cidade/UF, filtra por horizonte de 7 dias, roda pipeline completo (laudo→mercado→reforma→frete→precificador) e atualiza Google Sheets. Cron diário às 7h via `setup_cron.sh`.
- **Cobertura:** 6 testes em [`tests/test_orquestrador.py`](tests/test_orquestrador.py) (frete por UF, early_exit, lote já avaliado, persistência).
- **Limitações:** Seletores JS do scraper precisam ser ajustados na primeira execução real (DOM pode variar). Login deve usar `AUTOAVALIAR_EMAIL` + `AUTOAVALIAR_PASSWORD` no `.env`.

### L — Fix laudo visual + extrator textual de avarias ✅
- **Branch:** `claude/adoring-sinoussi`
- **Arquivos:**
  - [`carros_sa/agents/extrator_laudo.py`](carros_sa/agents/extrator_laudo.py) — nova `extrair_avarias_textuais()` + `_severidade_consolidada()` + `parse_laudo_textual` agora captura só campos (filtro CAIXA ALTA) + `extrair_laudo` sobrevive a visão falhada com fallback textual.
  - [`carros_sa/agents/vision_clients.py`](carros_sa/agents/vision_clients.py) — retry manual 0s/15s/45s em `GeminiVisionClient` + novo `FallbackVisionClient` encadeando clients + `build_default_client` auto-constrói cascata Gemini→Haiku se `ANTHROPIC_API_KEY` setado.
  - [`carros_sa/orquestrador.py`](carros_sa/orquestrador.py) — `_laudo_de_textual` popula avarias e severidade via extrator textual (antes retornava sempre vazio).
  - [`scripts/reprocessar_laudos.py`](scripts/reprocessar_laudos.py) — re-roda laudo+avaliação nos lotes já ingeridos sem rescrape, usando PDFs locais em `data/laudos_amostra/`.
- **Diagnóstico:** Gemini 2.5 Flash vinha retornando `503 UNAVAILABLE` por overload em alguns momentos; o código caía em fallback `_laudo_de_textual` que **por design** retornava `avarias=[]`, zerando a reforma de todos os lotes. Causa raiz: fallback não aproveitava o bloco "Observações" do PDF (texto livre do inspetor com menções concretas a reparos).
- **Como funciona:** O extrator textual procura verbos de reparo (`reparado`, `repintado`, `soldado`, `substituído`, etc.) combinados com peças estruturais (colunas A-D, longarinas, paralamas, portas, etc.) no bloco Observações. Severidade é derivada: coluna/longarina reparada → GRAVE ou ESTRUTURAL; chapa externa → MEDIA. Quando a visão volta a funcionar, ela é a fonte primária e o textual só enriquece (não sobrescreve). Cascata de providers: Gemini grátis como 1º, Haiku ~$0.005/chamada como fallback automático.
- **Validação gold (Fiesta 21854782):**
  - Antes: `severidade=nenhuma, avarias=[], reforma=R$ 0, viável=sim` (ERRADO — Fiesta tem REPROVADO ESTRUTURAL)
  - Depois: `severidade=estrutural, avarias=[coluna_b_esquerda, coluna_c_esquerda], reforma=R$ 10.000, viável=NÃO` (correto — descartado no ranking)
  - No reprocessamento em massa: Gemini voltou a responder (confidence 0.90), então camada visual funcionou, e os 22 lotes sem PDF local continuaram com `avarias=[]` — isso é esperado, precisam rodar pipeline completo com download de PDF pra serem melhorados.
- **Cobertura:** 8 testes novos em [`tests/test_avarias_textuais.py`](tests/test_avarias_textuais.py) — gold Fiesta real + casos sintéticos (vazio, plural "COLUNAS B e C", lados separados, capô/tampa, múltiplos reparos numa frase, observações sem reparo).
- **Custo operacional:** Com fallback ativo, pior caso é ~$0.005 por laudo no Haiku (100 lotes/dia = $15/mês; 300/dia = $45/mês). Gemini permanece primário grátis.
- **Limitações conhecidas:**
  - `ANTHROPIC_API_KEY` precisa ser setada no `.env` pra ativar o fallback pago — sem ela, pipeline ainda funciona mas Gemini 503 derruba o laudo visual (cai pro textual).
  - Textual só detecta peças mencionadas em português no bloco Observações — PDFs com convenção diferente podem não ser cobertos (mitigável adicionando padrões).
  - Reprocessar lotes antigos depende de PDFs locais. Lotes sem PDF ficam na zona cinza (avarias=0) até a próxima triagem completa.

### M — Expansão geográfica por raio ✅
- **Branch:** `claude/adoring-sinoussi`
- **Arquivos:**
  - [`carros_sa/tools/geo.py`](carros_sa/tools/geo.py) — `Municipio`, `carregar_municipios`, `distancia_haversine_km`, `buscar_municipio`, `cidades_no_raio`
  - [`data/geo/municipios.csv`](data/geo/municipios.csv) — 5570 municípios BR (IBGE, MIT, 390KB) com lat/lon, cacheado em memória
  - [`data/geo/estados.csv`](data/geo/estados.csv) — 27 UFs com códigos IBGE
  - [`carros_sa/tenancy.py`](carros_sa/tenancy.py) — `EmpresaConfig.raio_operacao_km` + `cidades_de_busca()`, `frete_para(0)` retorna R$ 0
  - [`carros_sa/orquestrador.py`](carros_sa/orquestrador.py) — `_calcular_frete` usa distância haversine real (fallback heurístico UF se cidade fora do dataset)
  - [`carros_sa/scraping/scraper_autoavaliar.py`](carros_sa/scraping/scraper_autoavaliar.py) — `coletar_listagem` itera cidades do raio, deduplica por `loteId`
  - [`config/empresas/carros_uberlandia.yaml`](config/empresas/carros_uberlandia.yaml) — `raio_operacao_km: 150`
- **Como funciona:** A empresa declara um raio operacional no YAML (150km em Uberlândia → 61 cidades, incluindo Triângulo Mineiro + fronteiras de GO e SP). O scraper itera cada cidade do raio fazendo 1 request à listagem do Auto Avaliar e deduplica lotes que aparecem em múltiplas cidades. Entre requests, sleep aleatório 0.8–1.5s. No precificador, o frete usa distância haversine real entre origem do lote e pátio, não mais a heurística grosseira de UF. **Lote na mesma cidade do pátio → frete R$ 0** (comprador busca pessoalmente), conforme briefing do usuário.
- **Cobertura:** 12 testes novos — 11 em [`tests/test_geo.py`](tests/test_geo.py) (haversine, busca por nome/UF, raio com ordenação por distância, cross-UF), +1 em [`tests/test_precificador.py`](tests/test_precificador.py) (frete 0km = R$ 0), e atualização de 4 testes em [`tests/test_orquestrador.py`](tests/test_orquestrador.py) pra validar distância real em vez da heurística antiga.
- **Limitações conhecidas:**
  - 1 request por cidade = 61 requests por run no raio padrão. Auto Avaliar pode rate-limitar; se necessário, reduzir `raio_operacao_km` ou adicionar backoff exponencial.
  - Haversine é linha reta — rodovia no Brasil costuma ser ~20% maior. Tabela de frete já absorve isso nas faixas largas (0-300km, 300-600km).
  - Cidades com grafia "esquisita" (ex.: "São Gotardo" vs "Sao Gotardo") são normalizadas (lowercase + sem acento), mas se o Auto Avaliar usar nome que não bate com o IBGE, o frete cai no fallback heurístico UF.

### K — Referência de preço Tabela Auto Avaliar ✅
- **Branch:** `claude/adoring-sinoussi`
- **Arquivos:**
  - [`carros_sa/scraping/parsers.py`](carros_sa/scraping/parsers.py) — `extrair_precos_aa()` + `PrecosAA` + integração no `parse_detalhe`
  - [`carros_sa/scraping/scraper_detalhe.py`](carros_sa/scraping/scraper_detalhe.py) — promove preços pro `Lote` + grava histórico em `PrecoReferenciaAA`
  - [`carros_sa/models.py`](carros_sa/models.py) — 2 campos novos em `LoteRaw`/`Lote`, 1 em `SinalMercado`, 2 em `Avaliacao`/`AvaliacaoLote`, nova tabela `PrecoReferenciaAA`
  - [`carros_sa/precificador.py`](carros_sa/precificador.py) — decompõe em `preco_giro_fipe` + `preco_giro_aa`, consolida usando o menor
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — 3 colunas novas (Giro FIPE, Giro Auto Avaliar, FIPE %)
- **Fixture real:** [`tests/fixtures/21893414_voyage_precos_aa.html`](tests/fixtures/21893414_voyage_precos_aa.html) — DOM do anúncio VW Voyage 2019 (lote 21893414), capturado autenticado em 2026-04-16.
- **Como funciona:** No `b2b.autoavaliar.com.br`, a "Tabela Auto Avaliar" não existe como consulta livre — o preço-referência (`ULTIMA AVALIAÇÃO`) e o percentual sobre FIPE (`.tag-percent-value`) vêm **embedded SSR** na página de cada anúncio. O parser extrai esses dois sinais do próprio HTML que a gente já raspa, sem request extra. `Precificador` agora calcula `preco_giro_fipe = min(fipe*0.95, wm_p25)` e `preco_giro_aa = min(auto_avaliar_ref, wm_p25)` e consolida o preço-giro usando o **menor** dos dois (mais conservador). Quando um lote não traz o dado, fallback pra FIPE-only preserva o comportamento anterior.
- **Cobertura:** 15 testes novos — 8 em [`tests/test_precos_aa.py`](tests/test_precos_aa.py) (gold fixture Voyage + robustez), 4 em [`tests/test_precificador.py`](tests/test_precificador.py) (comportamento híbrido), 3 em [`tests/test_scraper_detalhe.py`](tests/test_scraper_detalhe.py) (propagação + histórico).
- **Limitações conhecidas:**
  - Só popula `PrecoReferenciaAA` pra modelos que já apareceram em leilão — a consulta cross-lote ("tenho um lote de modelo X, já vi ref pra ele?") precisa ser cabeada no AvaliadorMercado (próxima iteração, não nesse workstream).
  - A label visual "fipe" do badge é renderizada via CSS/imagem ao lado do número, não aparece em innerText. Confiamos que `.tag-percent-value` significa semanticamente "% sobre FIPE do lance mín" — confirmado em inspeção manual 2026-04-16. Se Auto Avaliar mudar o HTML, o parser falha silencioso (retorna `None`), não errado.
  - SinalMercado.auto_avaliar_ref precisa ser populado pelo AvaliadorMercado (hoje continua None até o wire-up).

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
- ✅ **M6** — Orquestrador end-to-end + ranking (paralelismo parcial — scraping sequencial por anti-bot) — _workstream E_
- ✅ **M6-B** — CLI unificada (`carros-sa` com 6 subcomandos + 11 testes) — _workstream F_
- 🔒 **M7** — Frete first-class integrado (já temos tabela YAML; falta wire-up)
- 🔒 **M8** — Multi-tenancy rodando (schema pronto; falta rodar na prática)
- 🕐 **M9** — Re-check semanal Webmotors — _workstream G_
- 🕐 **M10** — Calibração isolada por tenant — _workstream H_
