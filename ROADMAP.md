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
- **O que mudou:** Playwright scraper com login/senha + cookies persistentes coleta lotes do Auto Avaliar por cidade/UF, filtra por horizonte de 7 dias, roda pipeline completo (laudo→mercado→reforma→frete→precificador) e atualiza Google Sheets. Cron 2x/dia (7h e 13h) via `setup_cron.sh` — batch do AA fecha numa janela única de tarde/noite, a passada do meio-dia pega lotes que entraram depois da 1ª coleta.
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

### N — Marcar leilões encerrados + timestamp visível ✅
- **Branch:** `claude/happy-johnson`
- **Arquivos:**
  - [`carros_sa/scraping/parsers.py`](carros_sa/scraping/parsers.py) — `DetalheFlags.encerrado` + `_detectar_encerrado()` detecta badge "ARREMATADO"/"VENDIDO"/"ENCERRADO"/"FINALIZADO" isolado ou frase "leilão encerrado". `early_exit` passa a priorizar `"leilao_encerrado"` sobre `reprovado_estrutural`.
  - [`carros_sa/scraping/scraper_detalhe.py`](carros_sa/scraping/scraper_detalhe.py) — `_flags_to_dict` persiste `encerrado` em `lote.raw_json["detalhe"]`.
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — exportador agora: (a) insere linha 1 "Última atualização da planilha: DD/MM/YYYY HH:MM" como banner, (b) renomeia coluna final pra "Coletado em" e mostra `Lote.scraped_at` por linha, (c) calcula `encerrado = badge OU fim_em < now()`, (d) ordena encerrados pro FIM da aba, (e) adiciona situação "⚠ Encerrado", (f) `freeze(rows=2)` protege banner+header.
- **Motivação:** Usuário clicou no primeiro lote da planilha e ele já estava arrematado. Duas causas: snapshot do SQLite de 2 dias antes + ausência de qualquer sinal visual de "idade do dado". Agora o banner no topo mostra quando foi a última atualização e cada linha indica quando aquele lote específico foi visto no Auto Avaliar.
- **Cobertura:** 12 testes novos — 4 em [`tests/test_parsers.py`](tests/test_parsers.py) (badge ARREMATADO, frase "leilão encerrado", anti-falso-positivo em texto livre, precedência sobre estrutural), 1 em [`tests/test_scraper_detalhe.py`](tests/test_scraper_detalhe.py) (early_exit + persistência), 6 em [`tests/test_exportar_sheets.py`](tests/test_exportar_sheets.py) (timer vencido marca encerrado, badge no raw_json marca encerrado, encerrados vão pro final, banner na linha 1, coluna Coletado em reflete scraped_at, freeze cobre 2 linhas). 143/143 passando.
- **Limitações conhecidas:**
  - A detecção depende do scraper detalhe **ter rodado** depois do leilão fechar. Se o SQLite só tem o snapshot da listagem, a única defesa é o `fim_em < now()`, que pode dar falso-negativo se o lote foi arrematado **antes** do timer zerar (raro mas possível).
  - Os badges no b2b.autoavaliar foram inferidos das palavras portuguesas mais comuns ("ARREMATADO", "VENDIDO", "ENCERRADO", "FINALIZADO"). Se a plataforma introduzir nova label (ex.: "LOTE FECHADO"), o regex falha silencioso e cai pro fallback do timer.
  - Lotes sem `fim_em` (SHOWROOM, venda direta) nunca são marcados como encerrados pelo timer — precisam do badge pra sair da lista ativa.

### O — Paginação real do scraper de listagem ✅
- **Branch:** `claude/vigorous-lichterman-f70777`
- **Arquivos:**
  - [`carros_sa/scraping/scraper_autoavaliar.py`](carros_sa/scraping/scraper_autoavaliar.py) — `_coletar_listagem_cidade` agora itera `?p=1..N` em vez de scroll infinito. Lê `max(data-page)` do DOM de paginação (`<a class="button" data-page="N">`) e limita a `_MAX_PAGINAS=20` como guard-rail.
  - [`tests/test_scraper_paginacao.py`](tests/test_scraper_paginacao.py) — 5 testes com `FakePage` stub (1 página, N páginas, dedup cross-page, filtro de horizonte pós-agregação, limite de runaway).
- **Diagnóstico (via Chrome MCP na triagem ao vivo):** Uberlândia/MG mostra 148-169 lotes no contador mas o scraper só pegava ~48 (página 1). Causa: site usa paginação real com param `p` (não scroll infinito).
- **Impacto:** triagem passa a cobrir ~3x mais inventário por run. O feeling do usuário de "leilões muito em cima" vinha de duas causas combinadas: (1) planilha magra pela página 1 só, e (2) bug no parser de timer que dropava lotes multi-dia (ver workstream P).
- **Limitações conhecidas:**
  - Com 4 páginas × 1 request = 4 requests extra por cidade (vs 1 antes). Raio de 61 cidades × 4 = 244 requests/run. Se Auto Avaliar apertar rate-limit, talvez precise reduzir raio ou adicionar backoff.
  - A lógica de descoberta de `total_paginas` depende do seletor `a.button[data-page]`. Se o site mudar o DOM de paginação, cai no fallback de 1 página (regressão silenciosa pro comportamento antigo — conservador, não quebra o pipeline).

### P — Parser aceita timer multi-dia "N dia, HH:MM:SS" ✅
- **Branch:** `claude/vigorous-lichterman-f70777`
- **Arquivos:**
  - [`carros_sa/scraping/parsers.py`](carros_sa/scraping/parsers.py) — `_TIMER_RE` agora aceita prefixo opcional `N dia[s][,]`; novo `_TIMER_DIAS_RE` extrai dias+h+m+s; `_timer_para_fim_em` tenta multi-dia primeiro, cai no HH:MM:SS puro.
  - [`tests/test_parsers.py`](tests/test_parsers.py) — 4 testes novos: "1 dia", "2 dias" (plural), sem vírgula (robustez), gold do card real da Saveiro SHOWROOM visto em print (2026-04-17).
- **Diagnóstico:** Usuário mostrou print da página 4 de Uberlândia com card "1 dia, 18:52:23". O parser tinha regex `^\d{1,3}:\d{2}:\d{2}` que só casava HH:MM:SS — com "1 dia, ..." na frente, virava `fim_em=None` silenciosamente. Isso explica os 49/112 lotes do DB sem `fim_em` que eu tinha atribuído erroneamente a "ciclo diário" — eram lotes de 2+ dias sumindo.
- **Autocorreção:** memória persistente atualizada — **NÃO presumir ciclo diário no Auto Avaliar**. Lotes multi-dia existem; o que falhou antes foi inspeção superficial (só página 1 de `order=hdt`) + JS do Chrome MCP usando o mesmo regex bugado do Python.
- **Limitações conhecidas:**
  - O regex aceita até `\d+` dias sem teto. Guard-rail implícito vem do filtro `fim_em > limite` em `_coletar_listagem_cidade` com `horizonte_dias=7` — lotes >7d já ficam de fora do pipeline.

### Q — Auditoria automática de colunas (SessionEnd hook) ✅
- **Branch:** `claude/generic-hopping-wadler`
- **Arquivos:**
  - [`carros_sa/tools/audit.py`](carros_sa/tools/audit.py) — `CHECKS` (invariante por coluna HEADER) + `COLUMN_EXTRACTORS` + `audit(engine, sample_size=20)`. Espelha a ordenação de `SheetsExporter.exportar` pra que o Rank auditado coincida com o que o operador veria.
  - [`scripts/audit_columns.py`](scripts/audit_columns.py) — CLI que lê `carros_sa.db`, chama `audit()`, imprime violações em stderr, exit code sempre 0.
  - [`.claude/settings.json`](.claude/settings.json) — registra hook `SessionEnd` que dispara o script ao encerrar qualquer sessão do Claude Code no worktree.
  - [`Makefile`](Makefile) — alvo `make audit` pra rodar sob demanda.
- **Motivação:** operador reclamou que o progresso dependia dele apontar manualmente "essa coluna tá sem sentido". Agora cada sessão termina com uma checagem automática das 24 colunas contra o racional do Glossário (`sheets.py:252-398`).
- **Como funciona:** valida invariantes determinísticas por coluna (ex: `ROI > 500%` = improvável, `dias_giro <=0` = bug no CalibracaoGiro, `fator_risco ∉ [0.5, 1.5]` = fora dos bounds, severidade ∉ domínio do enum, reforma/frete negativos, URL sem HYPERLINK, Coletado em vazio, etc.). Agregação por `(coluna, motivo)` — violações do mesmo tipo viram uma única linha com contagem + exemplo de lote. Silencioso quando tudo ok.
- **Cobertura:** 13 testes em [`tests/test_audit_columns.py`](tests/test_audit_columns.py) — paridade HEADER↔CHECKS (pega coluna nova sem invariante), DB vazio silencioso, linha válida silenciosa, detecção de ROI absurdo / dias_giro zero / severidade fora do domínio / fator_risco fora dos bounds / reforma negativa / KM absurdo / modelo vazio, agregação por coluna, violações em colunas diferentes reportadas separadas.
- **Limitações conhecidas:**
  - MVP sem LLM — só invariantes de domínio. Casos "tecnicamente válidos mas contextualmente absurdos" (ex: ROI 80% num lote com severidade estrutural) não são pegos. Extensão futura plugar Gemini Flash com o Glossário no prompt.
  - Amostra fixa de 20 linhas mais recentes — problemas em percentuais raros podem passar.
  - Invariantes duplicam o Glossário em linguagem imperativa; manutenção exige editar os dois (teste de paridade protege contra colunas novas sem check, mas não contra divergência semântica).

### S — Ajuste da âncora de venda por km do lote ✅
- **Branch:** `claude/distracted-greider-e970bd`
- **Arquivos:**
  - [`carros_sa/ajuste_km.py`](carros_sa/ajuste_km.py) — NOVO. `fator_km(km_lote, km_mediana_mercado) → float ∈ [0.75, 1.15]`; sensibilidade 30% do delta relativo, no-op quando qualquer entrada é ausente/zero.
  - [`carros_sa/tools/webmotors.py`](carros_sa/tools/webmotors.py) — `EstatisticasWM.km_mediana` novo; `estatisticas()` calcula `median([a.km for a in relevantes if a.km > 0])`.
  - [`carros_sa/models.py`](carros_sa/models.py) — `SinalMercado.webmotors_km_mediana: Optional[int] = None` (mudança de contrato retrocompatível, default None preserva fixtures existentes).
  - [`carros_sa/agents/avaliador_mercado.py`](carros_sa/agents/avaliador_mercado.py) — `avaliar()` aceita `webmotors_km_mediana` opcional que é propagado para `SinalMercado`.
  - [`carros_sa/precificador.py`](carros_sa/precificador.py) — aplica `f_km` sobre `webmotors_mediana` e `auto_avaliar_ref` antes de deduzir reforma/frete/taxas; justificativa inclui `f_km=X.XX (km=..., km_mercado=...)` quando o ajuste é aplicado.
- **Motivação:** `webmotors_mediana` reflete um carro "típico" com km típica do modelo/ano. Lote com km muito acima da mediana de mercado → sistema superestimava o preço de venda → preço-alvo inflado → risco de lance ruim. Sem esse ajuste, `LoteRaw.km` era capturado mas inerte no precificador.
- **Como funciona:** delta relativo `(km_mediana_mercado - km_lote) / km_mediana_mercado` × sensibilidade 0.30, clamp em `[0.75, 1.15]`. Lote com km 50% acima da mediana → f_km ≈ 0.85 → 15% de desconto na âncora. Bounds evitam resultados absurdos em outliers (ex.: lote 5k km num mercado mediana 150k).
- **Cobertura:** 14 testes novos — 9 em [`tests/test_ajuste_km.py`](tests/test_ajuste_km.py) (neutro, cap superior/inferior, desconto/bônus moderado, dados faltantes), 3 em [`tests/test_webmotors.py`](tests/test_webmotors.py) (`km_mediana` real da fixture Fiesta, zeros ignorados), 2 em [`tests/test_precificador.py`](tests/test_precificador.py) (km alta reduz preço-alvo, km baixa eleva). Suíte completa **297 passando**.
- **Limitações conhecidas:**
  - O orquestrador/pipeline real ainda NÃO popula `webmotors_km_mediana` — `avaliar_mercado` é chamado com `similares_precos` (Auto Avaliar) e não recebe anúncios Webmotors. O campo fica `None` em produção → `f_km=1.0` (no-op) até o workstream que integrar Webmotors ao pipeline ligar `estatisticas(...).km_mediana` como parâmetro. Infra e testes prontos; falta o wire-up.
  - Sensibilidade 0.30 e bounds [0.75, 1.15] são calibração de primeira passada. Com dados reais do pipeline vale revisitar (idealmente regredir contra Arrematado).
  - Quando `webmotors_km_mediana` tem amostra rala (<3 anúncios), a mediana é ruidosa e pode enviesar o ajuste. Guard-rail atual é só o clamp; revisitar se aparecerem falsos positivos.

### R — Fix do decoy Relatório de Transparência + coluna Laudo na planilha ✅
- **Branch:** `claude/musing-jemison-ba0f06`
- **Arquivos:**
  - [`carros_sa/scraping/parsers.py`](carros_sa/scraping/parsers.py) — nova `is_laudo_pdf_url()` com allowlist (hosts `storage.googleapis.com/doc-b2b`, `cdn-aav.autoavaliar.com.br`, ou `.pdf` com "laudo" no path) e blocklist explícita de decoys (`relatorio-de-transparencia`, `/app/uploads/`, `/avaliacoes?`).
  - [`carros_sa/scraping/scraper_autoavaliar.py`](carros_sa/scraping/scraper_autoavaliar.py) — `_EXTRACT_PDF_URL_JS` refeito com função `pareceLaudo()` que usa a mesma allowlist. `coletar_detalhe` também aplica `is_laudo_pdf_url` em Python como defesa em profundidade caso o JS encontre padrão novo.
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — nova coluna "Laudo (PDF)" com `=HYPERLINK(url, "Ver laudo")`, filtrando decoys já persistidos em `raw_json["detalhe"]["laudo_pdf_url"]` pra não exibir link clicável pro PDF errado. Glossário atualizado.
  - [`scripts/reprocessar_lotes_do_db.py`](scripts/reprocessar_lotes_do_db.py) — novo flag `--somente-ativos` filtra lotes com `fim_em > now()` (evita gastar LLM em lotes já encerrados).
- **Diagnóstico:** 83 de 85 lotes no DB tinham `laudo_pdf_url` apontando pro rodapé institucional do Auto Avaliar ("Relatório de Transparência e Igualdade Salarial" hospedado em `storage.googleapis.com/app/uploads/...`). Causa: `_EXTRACT_PDF_URL_JS` aceitava QUALQUER link com `.pdf` ou `storage.googleapis` sem validar que o conteúdo era mesmo de laudo de lote. Resultado: ExtratorLaudo rodava em PDF institucional (texto sobre equidade de gênero), zerava avarias e toda pipeline rotulava como "limpo". 111/112 laudos ficaram com `severidade=nenhuma, avarias=[]`.
- **Validação pós-fix:** 9 lotes ativos foram reprocessados. **7/9 pegaram URL de laudo correta** (hosts `doc-b2b` e `sa-laudo`). **1 detectou avaria real** (Fiat Toro — paralamas reparados, severidade=media, confidence 0.95). Os 2 sem URL (`pdf_ok=False`) têm status "Laudo Aprovado" mas PDF não renderiza no DOM inicial — fallback do modal lazy também não achou; limitação conhecida.
- **Cobertura:** 10 testes novos — 7 em [`tests/test_parsers.py`](tests/test_parsers.py) (classe `TestIsLaudoPdfUrl`: decoy transparência, decoy listing, doc-b2b ok, cdn-aav ok, pdf com "laudo" ok, pdf sem "laudo" rejeitado, None/vazio) + 3 em [`tests/test_exportar_sheets.py`](tests/test_exportar_sheets.py) (URL válida vira hyperlink, decoy vira "—", URL ausente vira "—"). 196/196 passando.
- **Limitações conhecidas:**
  - Fix só ajuda coletas **futuras**. Os 103 lotes encerrados/sem fim_em continuam com `severidade=nenhuma` e `laudo_pdf_url` decoy — mas não são afetados operacionalmente porque já estão filtrados do export (`fim_em=None` → descartado) ou ordenados pro final (`⚠ Encerrado`).
  - Gemini 2.5 Flash teve overload (`ServerError`) durante o reprocessamento — 5/9 lotes caíram em fallback textual (confidence 0.5). Rodar de novo quando API normalizar, ou garantir `ANTHROPIC_API_KEY` ativa pra cascatear pro Haiku.
  - Allowlist cobre os hosts observados até hoje. Se Auto Avaliar mudar pra CDN novo (ex.: Cloudflare próprio), `pareceLaudo()` rejeita silenciosamente e cai no fallback do modal — monitorar taxa de `pdf_ok=False` na triagem pra detectar.

### R.1 — Auto-fix de decoys persistidos + teste obrigatório ✅
- **Arquivos:**
  - [`scripts/limpar_decoys_laudo.py`](scripts/limpar_decoys_laudo.py) — varre o DB, usa `is_laudo_pdf_url()` como fonte de verdade, zera `raw_json.detalhe.laudo_pdf_url` e derruba o `LaudoCache` correspondente pra forçar re-extração. Importável como `limpar_decoys(session, dry_run=True)` pra uso em teste.
  - [`tests/test_decoy_laudo_guard.py`](tests/test_decoy_laudo_guard.py) — 10 testes: funcionais (limpa decoy, preserva URL válida, idempotente, dry-run, lote sem detalhe, URL não mapeada) + guard de DB real (se `carros_sa.db` existe, `make test` falha caso sobre QUALQUER decoy). Skippa gracefully sem DB.
  - [`Makefile`](Makefile) — novo alvo `make limpar-decoys`.
  - [`scripts/setup_cron.sh`](scripts/setup_cron.sh) — cron diário agora é `triagem → limpar_decoys → retry-laudo-pendente`. Auto-heal em cada ciclo (7h/13h).
- **Motivação:** R fechou a porta de entrada do decoy via `is_laudo_pdf_url()` no scraper, mas 75 lotes legados continuavam com URL envenenada persistida em `raw_json.detalhe.laudo_pdf_url` — e cada retry diário caía no decoy, baixava PDF errado, `_pdf_eh_laudo_valido` rejeitava, e o lote ficava travado em `confidence=0.5` ("LAUDO NÃO ANALISADO" no export). Triagem 2026-04-18: 215/342 lotes com `confidence<0.6`, dos quais 71 por essa causa exata.
- **Validação:** rodado no DB de produção → 75 decoys limpos, 75 `LaudoCache` derrubados. `--dry-run` seguinte retornou 0. Suite: **308/308 verde**.
- **Limitações conhecidas:**
  - Guard do DB real faz `dry_run` mas não auto-corrige durante o `pytest` — exige intervenção (`make limpar-decoys`). Intencional: teste não deve mutar estado de produção silenciosamente.
  - Se um novo padrão de decoy aparecer no Auto Avaliar e o `is_laudo_pdf_url()` aceitar incorretamente, ambos scraper E limpeza deixam passar. Defesa secundária fica com `_pdf_eh_laudo_valido()` no orquestrador (inspeciona o PDF baixado).

### R.2 — Guard 'todo carro na planilha tem laudo baixado + revisado + linkado' ✅
- **Branch:** `claude/great-turing-6MUIL`
- **Arquivos:**
  - [`carros_sa/tools/lista_laudo_audit.py`](carros_sa/tools/lista_laudo_audit.py) — NOVO. `auditar_lista_laudos(session, empresa_id)` cruza `AvaliacaoLote` × `LaudoCache` × arquivo PDF persistente × `is_laudo_pdf_url()`. Retorna `ResultadoAuditoria` com 4 causas-raiz (`URL_AUSENTE`, `URL_DECOY`, `PDF_NAO_BAIXADO`, `LAUDO_NAO_REVISADO`) ordenadas por gate primário.
  - [`scripts/auditar_lista_laudos.py`](scripts/auditar_lista_laudos.py) — NOVO. CLI Typer com `--empresa`, `--max-listar`, `--quiet`. Imprime tabela agregada por causa + dica de resolução, lista os primeiros N gaps individualmente. Exit code != 0 quando há gaps (cron-friendly).
  - [`tests/test_lista_laudo_guard.py`](tests/test_lista_laudo_guard.py) — NOVO. 13 testes funcionais (1 por causa, ordem de causa raiz, espelhamento de filtros do export, agregação) + hygiene check de DB real espelhando `test_decoy_laudo_guard.py`.
  - [`Makefile`](Makefile) — alvo `make auditar-lista-laudos`.
  - [`scripts/setup_cron.sh`](scripts/setup_cron.sh) — cron diário agora encadeia 4 passos: `triagem → limpar_decoys → retry-laudo-pendente → auditar_lista_laudos --quiet`. Termômetro pós-cura no log; `--quiet` evita poluição no caminho feliz.
- **Motivação:** Usuário (2026-04-23) pediu garantia "todos carros na lista têm laudo baixado + revisado + link na planilha; se não, identificar razão e resolver pra nunca mais acontecer". O cron já rodava cura (decoy + retry), mas sem termômetro pós-cura ninguém sabia se ainda sobravam gaps. Pior: se o pipeline silenciosamente regredisse (ex.: scraper deixar passar padrão novo de decoy, vision client mudar comportamento), só apareceria como "LAUDO NÃO ANALISADO" no Sheets — usuário só descobriria abrindo a planilha.
- **Como funciona:** O auditor espelha exatamente os filtros do `SheetsExporter._query` + `exportar` (lote sem `fim_em` ou encerrado por badge/timer não conta). Pra cada lote ativo, checa em ordem: (1) `raw_json.detalhe.laudo_pdf_url` presente → (2) URL passa em `is_laudo_pdf_url()` → (3) `data/laudos_pdfs/<lote>.pdf` existe → (4) `LaudoCache.confidence >= 0.6`. Reporta SÓ a primeira causa por lote (ordem de causa raiz: resolver URL destrava o resto em cascata; reportar sintomas derivados infla o output).
- **Cobertura:** 13 testes novos. Suíte completa **324 passando** (+ 3 skipped — incluindo o hygiene check de DB real, que ativa só com `carros_sa.db` presente).
- **Limitações conhecidas:**
  - Auditor NÃO auto-corrige — ele só identifica. A cura é responsabilidade do cron (`limpar_decoys` + `reprocessar_lotes_do_db --somente-laudo-pendente`). Se o cron está parado, o auditor só pinta vermelho mais alto.
  - `URL_AUSENTE` é o gap mais difícil de fechar automaticamente: se o scraper não acha `laudo_pdf_url` no DOM (modal lento, layout diferente, status "SEM LAUDO"), retry visita a URL de novo mas pode falhar do mesmo jeito. Mitigação atual: relatório aponta a quantidade pra operador investigar manualmente. Próximo passo (não neste workstream): retry com `wait_for_selector` mais paciente + fallback pra clicar no botão "Ver laudo".
  - Hygiene check só roda pra `carros_uberlandia` (única empresa em produção). Multi-tenant precisa expandir o teste.

### I — Exportador Google Sheets ✅
- **Branch:** `claude/laughing-dewdney`
- **Arquivos:** [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py), [`scripts/exportar_sheets.py`](scripts/exportar_sheets.py)
- **Uso:** `make sheets EMPRESA=uberlandia_mg`
- **O que mudou:** `SheetsExporter` lê `AvaliacaoLote + Lote + LaudoCache` do SQLite e escreve aba `<empresa_id>` no Google Sheets (17 colunas, rankeado por score_roi desc, header congelado). Auth via service account JSON. Sem Orquestrador rodado, exporta 0 linhas — mas módulo e testes estão prontos.
- **Cobertura:** 6 testes em [`tests/test_exportar_sheets.py`](tests/test_exportar_sheets.py) (ROI, ordenação, sem laudo, sem avaliações), todos mockando gspread.
- **Limitações:** depende de `avaliacao_lote` populado (workstream E). Setup one-time: service account JSON + compartilhar Sheet com e-mail do SA.

### G — Tracking longitudinal Webmotors 🕐 futuro
Cron semanal que popula `anuncio_webmotors.sumiu_em`. Só faz sentido depois de ≥2 semanas de coleta contínua.

### H — Calibração coeficientes 🔓 destravado (dados disponíveis)
- **Pré-requisito atendido:** [data/historico/uberlandia_arrematado.csv](data/historico/uberlandia_arrematado.csv) com **32 vendas reais** importáveis via `carros-sa arrematado-import`. Carregadas em `arrematado` + `lote` (sintético com `leilao="historico_offline"`) — preserva FK sem mexer em `models.py`.
- **Já disponível:** 1 venda completa Auto Avaliar (Polo Track), 11 consignação (no pátio), 19 vendidos do Reinaldo com prazo médio 3,07 meses.
- **Próximo passo:** relatório DuckDB `preco_alvo (AvaliacaoLote) vs preco_real (Arrematado)` quando houver overlap (lotes que entraram pelo pipeline AA E foram arrematados). Hoje overlap = 0 — precisa rodar triagem real e arrematar de fato.
- **Bloco C deste plano** já consome esse dado pra calibrar `dias_giro_estimado` por categoria (sem esperar overlap, usa só `vendido_em - data` dos históricos).

### Bloco A — Calibração econômica do precificador ✅
- **Branch:** `claude/exciting-pascal` (worktree `exciting-pascal`)
- **Arquivos:**
  - [carros_sa/tenancy.py](carros_sa/tenancy.py) — novo `CustosOperacionais` (despachante/higienização/marketing/laudo/combustível) + `taxa_leilao_fixa`
  - [carros_sa/precificador.py](carros_sa/precificador.py) — fórmula combina taxa pct + fixa
  - [config/empresas/carros_uberlandia.yaml](config/empresas/carros_uberlandia.yaml) — calibrado: margem.base 0.18→0.25, taxa Auto Avaliar R$ 999 fixo, custos op R$ 2.523 decompostos
  - [config/empresas/empresa_fake_sp.yaml](config/empresas/empresa_fake_sp.yaml) — bump margem 0.22→0.30 (mantém semântica "mais agressivo que Uberlândia")
- **Calibração baseada em:** Polo Track 2024 real (R$ 52.200 → R$ 69.400, FIPE cheia, R$ 4.735 em custos recorrentes), histórico Reinaldo 21 carros (R$ 161k lucro / R$ 1,08M invest = 14,9% absoluto / 3 meses médios = ~5x CDI).
- **Cobertura:** 18 testes em [tests/test_precificador.py](tests/test_precificador.py) (14 ajustados + 4 novos: `test_taxa_leilao_fixa_auto_avaliar_polo_track_real`, `test_taxa_leilao_pct_e_fixa_combinadas`, `test_custos_op_decompostos_somam_total`, `test_yaml_antigo_so_custo_op_fixo_funciona`).

### Bloco B — Importador histórico → Arrematado ✅
- **Branch:** `claude/exciting-pascal`
- **Arquivos:**
  - [carros_sa/tools/historico_import.py](carros_sa/tools/historico_import.py) — `HistoricoRow`, `parse_csv`, `importar_historico`, `lote_id_sintetico`. Idempotente (matching por marca+modelo+ano+valor_compra).
  - [carros_sa/cli.py](carros_sa/cli.py) — subcomando `arrematado-import <csv> --empresa <id>`
  - [data/historico/uberlandia_arrematado.csv](data/historico/uberlandia_arrematado.csv) — 32 carros do operador
- **Como funciona:** cada linha vira `Lote` sintético (`leilao="historico_offline"`, `lote_id` determinístico) + `Arrematado`. Preserva FK pra `lote.id` sem mexer em [models.py](carros_sa/models.py). Suporta linhas "no pátio" (sem `data_venda` → `vendido_em` e `vendido_por` ficam NULL).
- **Cobertura:** 4 testes em [tests/test_historico_import.py](tests/test_historico_import.py) — id sintético determinístico, gold do Polo real, "no pátio" parcial, idempotência.

### Bloco C — Calibração `dias_giro` + ROI anualizado ✅
- **Branch:** `claude/exciting-pascal`
- **Arquivos:**
  - [carros_sa/models.py](carros_sa/models.py) — +1 campo `dias_giro_estimado: Optional[int] = None` em `Avaliacao` e `AvaliacaoLote` (mudança coordenada, aprovada explicitamente)
  - [carros_sa/db.py](carros_sa/db.py) — `_aplicar_migracoes_leves` ALTER TABLE idempotente pra DBs existentes (substituir por Alembic quando sair de PoC)
  - [carros_sa/agents/calibracao_giro.py](carros_sa/agents/calibracao_giro.py) — NOVO. `calibrar_dias_giro()` lê `Arrematado` por categoria (≥3 vendas → média; <3 → fallback prior). `roi_anualizado(score, dias)` com floor de 30d e fallback de 90d quando NULL.
  - [carros_sa/agents/avaliador_mercado.py](carros_sa/agents/avaliador_mercado.py) — aceita `empresa_id` opcional pra ativar calibração (retrocompat: sem empresa = prior hardcoded)
  - [carros_sa/precificador.py](carros_sa/precificador.py) — propaga `dias_giro_estimado` da SinalMercado pra Avaliacao
  - [carros_sa/orquestrador.py](carros_sa/orquestrador.py) — passa `empresa_id` ao avaliar mercado e persiste o campo no upsert
  - [carros_sa/cli.py](carros_sa/cli.py) — `top` agora ranqueia por **ROI anualizado** por default; `--absoluto` volta pro score_roi puro
  - [carros_sa/tools/sheets.py](carros_sa/tools/sheets.py) — 2 colunas novas (Dias até venda, ROI anualizado)
- **Calibração real (32 vendas importadas):** sedan calibrado 98d (vs prior 30d), hatch 128d (vs 25d), SUV 79d, picape 64d. **Sistema estava otimista demais** — operador real opera com mix de carros antigos e nichos que demoram muito mais que o prior categórico assumia.
- **Cobertura:** 13 testes novos — 12 em [tests/test_calibracao_giro.py](tests/test_calibracao_giro.py) (inferência de categoria, calibração ≥3 vs fallback, idempotência por empresa, ROI anualizado com floor) + 1 em [tests/test_cli.py](tests/test_cli.py) (`test_top_ranqueia_por_roi_anualizado_default` — lote rápido com ROI menor passa lento com ROI maior).

#### Pendência identificada por Caio (2026-04-16): granularidade da calibração
A categoria genérica é grosseira demais. O hatch 128d agrupa Polo Track 2024 (227d, preço-cheio) com Onix Joy 1.0 2018 (278d) e Gol 2014 (22d) — perfis muito diferentes.

Caminhos pra refinar (em ordem de simplicidade):
1. **Sub-bucket por idade** (`hatch_novo` ≤3 anos / `hatch_velho` >3 anos). Implementação: ~1h. Custo: precisa rodar mais lotes pra ter ≥3 amostras por sub-bucket.
2. **Calibração por modelo** quando há ≥2 amostras do MESMO modelo (cai pra categoria quando não). Mais granular, mas requer histórico denso.
3. **Filtrar outliers**: usar mediana em vez de média; ou peso decrescente por idade da amostra.
4. **Distinguir "demanda intrínseca" de "política de preço"**: o Polo demorou 227d porque vendeu na FIPE cheia. Dividir `dias_giro` em `dias_se_FIPE` vs `dias_se_FIPE-5%` exigiria histórico com info de quanto desconto deu (não temos hoje).
- **Limitações:** calibração de qualidade modesta com 32 vendas (~poucas por categoria). Workstream H futuro vai melhorar com séries temporais e overlap real entre AA + Arrematado.

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

### P — Defesa em profundidade do download de PDF de laudo ✅
- **Branch:** `claude/upbeat-faraday`
- **Arquivos:**
  - [`carros_sa/orquestrador.py`](carros_sa/orquestrador.py) — `_pdf_persistente_path` (PDFs em `data/laudos_pdfs/<lote>.pdf`, não mais `tmp_dir` que somia entre runs) + `_pdf_eh_laudo_valido` (lê 1ª página com PyMuPDF, exige marcador positivo LAUDO/CHASSI/PLACA e nega decoy TRANSPARÊNCIA SALARIAL/IGUALDADE SALARIAL).
  - [`scripts/reprocessar_laudos.py`](scripts/reprocessar_laudos.py) — busca PDF tanto em `data/laudos_pdfs/` (produção) quanto `data/laudos_amostra/` (fixtures).
  - [`scripts/diagnosticar_pdf_laudo.py`](scripts/diagnosticar_pdf_laudo.py) — ferramenta de debug: pega N lotes ativos, re-visita URLs e reporta quais baixaram laudo real.
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — `COLUMN_FORMATS` + `_reaplicar_formato_numerico` reaplicam NUMBER nas colunas R$ após `ws.clear()` (clear preserva formato → inteiros viravam datas).
  - [`tests/test_orquestrador.py`](tests/test_orquestrador.py) + [`tests/test_exportar_sheets.py`](tests/test_exportar_sheets.py) — 7 testes novos cobrindo validador de PDF (gold Fiesta, denylist, min size) e numberFormat (reaplica em Reforma/Frete, col_letter, cadeado de sync HEADER×FORMATS).
- **Motivação:** Complementar ao fix do seletor JS (workstream paralelo em `parsers.is_laudo_pdf_url` e `_EXTRACT_PDF_URL_JS`). Dois problemas que só dá pra atacar aqui: (1) PDFs sumiam entre runs do orquestrador porque ficavam em `tmp_dir`, impossibilitando reprocessamento offline; (2) raw_json antigos coletados ANTES do fix do seletor ainda têm URLs erradas, e sem validação no ARQUIVO baixado o extrator processaria PDF institucional como se fosse laudo.
- **Validação real (16/04):** scraper com seletor frouxo + meu validador → 0 PDFs errados processados (validador rejeitou todos os "Transparência Salarial"). Diagnóstico em 10 lotes ativos: 8/10 PDFs de laudo corretos baixados e persistidos.
- **Limitações conhecidas:** validador só lê 1ª página (suficiente pros decoys observados, mas pode escapar PDF institucional com cabeçalho disfarçado). `tmp_dir` ainda é criado no orquestrador mas não é mais usado — limpar em workstream seguinte.

### O — EstimadorReformaLLM (substitui tabela determinística) ✅
- **Branch:** `claude/gifted-bassi-a24b51`
- **Arquivos:**
  - [`carros_sa/agents/text_llm_clients.py`](carros_sa/agents/text_llm_clients.py) — `TextLLMClient` ABC + `GeminiTextClient` + `AnthropicTextClient` + `FallbackTextLLMClient` + `build_default_text_client`. Espelha `vision_clients.py` mas text-only.
  - [`carros_sa/agents/estimador_reforma_llm.py`](carros_sa/agents/estimador_reforma_llm.py) — `estimar_llm(laudo, lote_info, empresa, llm_client, observacoes_pdf)` com prompt estruturado (carro + severidade + região + avarias + observações livres) e parsing robusto (JSON malformado, custo_total mentiroso, range ausente → fallbacks internos).
  - [`carros_sa/orquestrador.py`](carros_sa/orquestrador.py) — `_pipeline_lote` e `orquestrar` aceitam `text_llm_client=None`; quando presente, usa LLM + extrai `observacoes` via `parse_laudo_textual`. Qualquer falha no LLM cai pro determinístico internamente (em `estimar_llm`), pipeline não quebra.
  - [`carros_sa/cli.py`](carros_sa/cli.py) + [`scripts/triagem_diaria.py`](scripts/triagem_diaria.py) + [`scripts/reprocessar_laudos.py`](scripts/reprocessar_laudos.py) — instanciam `build_default_text_client` e injetam no orquestrador.
  - [`tests/test_estimador_reforma_llm.py`](tests/test_estimador_reforma_llm.py) — 8 testes novos: gold Fiesta, diferenciação Gol × Range Rover Evoque com motor suspeito, 2× fallback pro determinístico, parsing robusto (soma itens, range derivado), prompt contém campos mínimos.
  - [`tests/fixtures/21854782_reforma_llm.json`](tests/fixtures/21854782_reforma_llm.json) — fixture gold da resposta esperada do LLM.
- **Motivação:** Tabela determinística YAML zerava todos os "motor não original" em R$ 4.000 fixo independente do carro — um Gol 2014 com peças baratas e um Range Rover 2018 com peças importadas saíam com o mesmo número. Usuário pediu: LLM que lê laudo + carro + região e estima caso-a-caso.
- **Como funciona:** Prompt inclui marca/modelo/ano/km, pátio da empresa (Uberlândia/MG × São Paulo capital muda mão-de-obra ~25%), severidade geral, lista de avarias específicas do laudo, status do motor + documentação, e bloco "Observações" livre do inspetor (capado em 2000 chars). LLM responde JSON com 1-8 itens + custo_total + range + justificativa + confidence. Parser recalcula `custo_total = sum(itens)` (LLM às vezes erra a soma) e deriva range se LLM omitir. Em qualquer erro (timeout, JSON inválido, shape ruim), `estimar_llm` cai transparentemente pro `estimar` determinístico — pipeline nunca fica sem custo.
- **Cascata de providers:** Gemini Flash primário (grátis) → Haiku fallback (~$0.001/chamada, ativa com `ANTHROPIC_API_KEY`). Validado em cima do Fiesta real: quando Gemini entrou em 503 UNAVAILABLE nos 3 retries, o determinístico absorveu e devolveu R$ 10.000 como antes.
- **Validação gold (Fiesta 21854782):** LLM com fixture → 3 itens, R$ ~10.400 com range R$ 8.300–13.000. Determinístico (fallback) → R$ 10.000. Patamares compatíveis, mas LLM detalha os itens (solda + pintura + calibração de airbag separadas). Teste Gol × Evoque: mesma `motor_ok=False, sem avarias`, Gol → R$ 1.800, Evoque → R$ 12.000 (multiplicador 6×, objetivo central do workstream).
- **Custo operacional:** Gemini grátis no caso feliz; pior caso Haiku a ~$0.001/lote. Para 100 lotes/dia e 30% fallback pago, ~$0,03/dia → ~$1/mês. Operacionalmente negligível.
- **Limitações conhecidas:**
  - Variância do LLM: dois runs do mesmo laudo podem variar ±10% no custo (temperature=0 já). Aceitável porque o precificador usa `custo_total` como uma componente entre várias (FIPE, Webmotors, frete) — erro no reforma não inverte ranking por conta própria.
  - Custo-teto: hoje não há limite superior. Se LLM alucinar "R$ 200k pra trocar motor", isso mataria ROI mas não quebra pipeline. Mitigação futura: comparar com determinístico e, se LLM >3× determinístico, usar max dos dois + log de auditoria.
  - Prompt pede regra "SEMPRE incluir alinhamento de chassi quando estrutural" — se LLM ignorar, o custo fica baixo demais. Nos testes de fixture o LLM respeitou, mas em produção pode falhar silenciosamente. Validar com 20 lotes reais quando Gemini voltar ao ar.
  - Tabela determinística em `config/reforma/*.yaml` **fica como safety net** — usada no fallback e pode ser auditada em paralelo. Não remover até termos confiança em 50+ lotes reais.

### O.1 — Coluna "Racional Reforma" na planilha ✅
- **Branch:** `claude/gifted-bassi-a24b51`
- **Arquivos:**
  - [`carros_sa/models.py`](carros_sa/models.py) — +campo `racional: Optional[str]` em `CustoReforma`; +campo `reforma_racional: Optional[str]` em `Avaliacao` e `AvaliacaoLote` (aditivos, nullable, aprovados explicitamente).
  - [`carros_sa/db.py`](carros_sa/db.py) — migração leve `ALTER TABLE avaliacao_lote ADD COLUMN reforma_racional TEXT` pros DBs existentes.
  - [`carros_sa/agents/estimador_reforma_llm.py`](carros_sa/agents/estimador_reforma_llm.py) — `_parse_resposta` agora preserva `justificativa` do LLM em `CustoReforma.racional`.
  - [`carros_sa/precificador.py`](carros_sa/precificador.py) — popula `Avaliacao.reforma_racional`: prioriza LLM (`reforma.racional`), fallback é sumário "descrição (R$valor)" dos itens do determinístico, None quando reforma vazia.
  - [`carros_sa/orquestrador.py`](carros_sa/orquestrador.py) — `_upsert_avaliacao` passa o novo campo pra SQLite.
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — nova coluna "Racional Reforma" entre "Reforma Estimada" e "Frete" + entrada no Glossário explicando fonte LLM vs fallback + lotes sem racional mostram "—".
  - [`tests/test_precificador.py`](tests/test_precificador.py) — 3 testes (montagem de itens, justificativa LLM prioritária, reforma vazia → None).
  - [`tests/test_estimador_reforma_llm.py`](tests/test_estimador_reforma_llm.py) — 2 testes (LLM justificativa vira `CustoReforma.racional`, fallback determinístico deixa None).
  - [`tests/test_exportar_sheets.py`](tests/test_exportar_sheets.py) — 3 testes (header inclui a coluna, valor renderiza correto, None mostra "—").
- **Motivação:** Usuário (2026-04-17) pediu pra ver NA PLANILHA o racional do valor da reforma, pra operador auditar rapidamente sem abrir laudo ou código.
- **Como funciona:** LLM devolve "justificativa" → sobrevive em `CustoReforma.racional` → `precificar()` propaga pra `Avaliacao.reforma_racional` → `_upsert_avaliacao` salva → `SheetsExporter` renderiza na coluna dedicada. Quando LLM não responde (cascata caiu no determinístico), precificador monta sumário curto ("Coluna B (R$3500) · Coluna C (R$3500) · adicional estrutural (R$3000)") dos itens. Nulo renderiza "—".
- **Cobertura:** 8 testes novos (202 total verde). Migração SQLite validada: campo aparece tanto em DB fresco quanto em DB existente sem perda de dados.
- **Limitações:** coluna tem largura livre — se LLM retornar justificativa muito longa (>500 chars) vai ficar feia no Google Sheets. Prompt hoje pede "uma frase", mas não enforce. Mitigável com truncagem se virar problema.

### S — Aba Cidades & Frete na planilha ✅
- **Branch:** `claude/festive-tesla-6c18ae`
- **Arquivos:** [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — novo `_write_cidades_frete_sheet(empresa_id, session)` engatado no `exportar()`. Aba `cidades_<empresa_id>` com 1 linha por município no raio operacional + frete por categoria + contagem de lotes ativos (fim_em > now) com origem naquela cidade.
- **Motivação:** usuário pediu visibilidade direta de "de onde estamos olhando carros + custo logístico de cada cidade". Antes a info ficava espalhada (raio no YAML + tabela de frete em outro lugar + origem dos lotes só na coluna do listing).
- **Como funciona:** Reusa `empresa.cidades_de_busca()` (já ordenado por distância haversine) e `empresa.frete_para(d, categoria)` para cada categoria do enum. Conta lotes ativos por (cidade, UF) com normalização case/accent-insensitive (espelha `_normaliza` do `geo.py`) pra colar mesmo quando AA grava "ARAGUARI" e o IBGE tem "Araguari". Pátio aparece com distância 0 e frete R$ 0 (comprador busca pessoalmente).
- **Cobertura:** 4 testes em [`tests/test_exportar_sheets.py`](tests/test_exportar_sheets.py) (`TestCidadesFreteSheet`) — aba criada com nome correto, pátio com 0/0, contagem com normalização, fallback silencioso quando YAML não existe (caminho atingido só nos testes pré-existentes que usam `empresa_id` mockado).
- **Limitações conhecidas:**
  - Lotes sem `origem_cidade`/`origem_uf` populados não entram na contagem (o scraper costuma popular, mas snapshots antigos podem ter NULL).
  - `lru_cache(32)` em `carregar_empresa` significa que mudanças no YAML em runtime do mesmo processo só refletem após reload — não impacta operação batch (script encerra entre runs).

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
