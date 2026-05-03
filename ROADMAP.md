# Carros SA — Roadmap

Documento vivo. Cada sessão atualiza seu workstream ao mergear em `main`.

## Status atual (baseline)

✅ **Fundação + EstimadorReforma** — 328/328 testes passando

### W — Revisão econômica das colunas da planilha (2026-04-29) ✅
- **Branch:** `claude/sleepy-wright-kSiCR`
- **Motivação:** Usuário pediu sanity-check da relação entre as colunas (FIPE × Lance Máximo × Giro FIPE × ROI × Lucro/mês). Simulação com Polo Track real expôs três bugs compostos.
- **Bugs encontrados e corrigidos:**
  1. **`ROI anualizado` na planilha era tautológico.** [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) usava `_calcular_roi_no_maximo(av) → roi_max × 365 / dias_giro` mas, por construção do precificador, `preco_max + reforma + frete + taxas + custo_op = preco_giro × (1 − margem_min)` ⇒ `roi_max ≡ margem_min / (1 − margem_min)` ≈ constante por empresa (~11% em Uberlândia). A coluna só variava por `dias_giro`, virando um `1/dias_giro` disfarçado. Pior: `_calcular_roi_no_maximo` ignorava `custo_op` e dava 15.9% em vez de 11.1%, então o número exibido nem era o "garantido" certo. Fix: usar `score_roi` (caso médio calibrado por risco/liquidez) — bate com a CLI `top` e com o que o docstring de `precificador.py:154-162` recomenda.
  2. **`Lucro/mês` subestimava em ~10%.** [`carros_sa/tools/sheets.py:180`](carros_sa/tools/sheets.py) e [`carros_sa/cli.py:166`](carros_sa/cli.py) usavam `score_roi × preco_alvo` como aproximação do lucro absoluto, mas `score_roi = lucro / capital_alvo` e `capital_alvo > preco_alvo` (engloba reforma/frete/taxas/custo_op). Fórmula exata fechada: `lucro = preco_giro × score_roi / (1 + score_roi)`. Helper canônico `sheets._lucro_absoluto_no_alvo`. No Polo Track real: era R$ 7.419/mês, agora R$ 8.288/mês — gap de R$ 869/mês que afeta diretamente a comparação entre lotes.
  3. **`pdf_dest.exists()` em [`carros_sa/orquestrador.py:609`](carros_sa/orquestrador.py) podia ser `None.exists()`** quando `pdf_url` era truthy mas o download/validação falhava. AttributeError engolido pelo `try/except` de baixo, mas confundia debug. Fix: checar `pdf_dest is not None and pdf_dest.exists()`.
- **Limpeza de docstrings:** [`precificador.py`](carros_sa/precificador.py) declarava `preco_giro_fipe = min(FIPE × 0.95, webmotors_p25)` mas o código faz `webmotors_mediana × f_km` há várias iterações; `webmotors_p25` está exportado em `SinalMercado` mas não é consumido. Docstring agora reflete a fórmula efetiva e nota o legado.
- **Audit reforçado** ([`carros_sa/tools/audit.py`](carros_sa/tools/audit.py)): nova invariante "Lance Máximo > FIPE × 1.05" pra pegar âncora de revenda inflada (f_km saturado em casos onde não deveria, FIPE errada, mediana inflada). ROI anualizado negativo também passa a ser sinalizado.
- **CLAUDE.md** atualizado com 4 padrões aprendidos: identidade econômica do precificador, fórmula fechada do lucro absoluto, naming hint `preco_giro_fipe`, workflow de revisão autônoma (escrever simulação ANTES de fix).
- **Cobertura:** 4 testes novos em `TestLucroAbsolutoNoAlvo` (gold Polo + edge cases score=0/negativo/preco_giro=0); `test_roi_absurdo_reportado` migrado pra trigger via `score_roi=5.0` × dias=30 = 6083% > 1000% (ao invés do mecanismo anterior baseado no roi_max bugado); `test_exportar_roi_anualizado_baseado_em_score_roi` substitui o teste que pinava ~44%/ano (tautologia) por 121.7%/ano (score_roi=0.3, dias=90). Suite total: **328 verde**.
- **Validação real (simulação 3 lotes):**
  - Fiesta 2013 estrutural: `preco_max=R$13.4k vs FIPE R$30.9k = 43.5%` — descartado como inviável (lance atual R$22.9k > p_max).
  - Polo Track 2024: `p_max=R$56.7k vs FIPE R$70k = 81%`, ROI/ano=231.5%, lucro/mês=R$8.236 — saudável.
  - Compass 2019: `p_max=R$82.8k vs FIPE R$100k = 82.8%`, ROI/ano=311.3%, lucro/mês=R$16.418 — alto por giro rápido (60d SUV).
  - Todos com `p_max < FIPE`, como esperado pela construção.
- **Limitações conhecidas:**
  - Score_roi em lotes inviáveis (Fiesta) sai 121% pois o fator_risco máximo dispara a margem efetiva. Como o lote é descartado pelo filtro de viabilidade antes de chegar à planilha, não polui a UI — mas o número absoluto fica bizarro em logs. Cap futuro em `margem_aplicada` (ex: ≤0.50) seria razoável; não fiz pra não mudar o ranking de lotes calibrados sem mais dados.



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
- **Cobertura:** 5 testes em [`tests/test_avaliador_mercado.py`](tests/test_avaliador_mercado.py) com fixtures FIPE reais ([`fipe_fiesta_2013.json`](tests/fixtures/fipe_fiesta_2013.json) + [`fipe_chery_tiggo_2015.json`](tests/fixtures/fipe_chery_tiggo_2015.json)) — Fiesta 2013 FIPE R$ 30.876 + similares reais do lote 21854782, regressão Tiggo 2.0 2015 (bug de marca duplicada), cache persistente em `modelo_fipe_cache` e fallback FIPE-only.
- **Pendente:** trocar fonte de mediana/p25 por Webmotors quando workstream B chegar (contrato `SinalMercado` já preparado).
- **2026-04-23 — migração FIPE v1 → v2:** `parallelum.com.br/fipe/api/v1` começou a retornar 503 intermitente + tinha bug de scoring na marca Chery (duas entradas "Caoa Chery" e "Caoa Chery/Chery" empatavam, primeira iterada ganhava, Tiggo 2.0 2015 matchava Tiggo 7 novo com valor ~2.7x errado — R$ 114k em vez de R$ 41.512). Cliente agora usa `fipe.parallelum.com.br/api/v2` (endpoints em inglês, campos `code`/`name`/`price`) e `consultar` testa TODAS as marcas empatadas escolhendo a do melhor match de modelo. Cache em disco migrado pra `fipe_brands_v2.json` (cache v1 antigo é ignorado automaticamente). API pública (`FipeClient.consultar`, `marca_fora_do_escopo_fipe`) inalterada — chamadores não precisam mudar.

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

### O.2 — Gemma 3 local via Ollama (experimental, text-only) 🧪
- **Branch:** `claude/test-gemma-4-local-SmiEV`
- **Arquivos:**
  - [`carros_sa/agents/text_llm_clients.py`](carros_sa/agents/text_llm_clients.py) — +`OllamaTextClient` (espelho do `OllamaVisionClient`), sem campo `images`, endpoint `/api/generate` format=json.
  - [`carros_sa/agents/text_llm_clients.py`](carros_sa/agents/text_llm_clients.py) — `build_default_text_client()` agora aceita `TEXT_LLM_PROVIDER=ollama` (standalone) e `ollama+gemini` (Ollama primário → Gemini fallback via `FallbackTextLLMClient`).
  - [`tests/test_text_llm_clients_ollama.py`](tests/test_text_llm_clients_ollama.py) — 10 testes com `httpx.Client.post` mockado (regra CLAUDE.md: sem LLM real em teste).
  - [`.env.example`](.env.example) — linha comentada documentando `TEXT_LLM_PROVIDER=ollama+gemini`.
- **Motivação:** usuário (2026-04-22) quer testar rodar o estimador de reforma local no MacBook Air pra eliminar rate limit do Gemini free tier e dependência de rede.
- **Como usar:** `brew install ollama && ollama pull gemma3:4b && ollama serve`, depois `TEXT_LLM_PROVIDER=ollama+gemini` no `.env`. Nenhum código chamador muda — `estimador_reforma_llm.estimar_llm`, `scripts/comparar_reforma_tabela_vs_llm.py` e `scripts/triagem_diaria.py` já usam `build_default_text_client()`.
- **Limitações conhecidas:**
  - **Vision ainda em Gemini** — extrator de laudo NÃO migrado, risco de perda de qualidade no diagrama Auto Avaliar (Gemini 2.5 Flash tem conf. 0.95 na fixture Fiesta). Decidir depois do smoke test do texto.
  - **Default `auto` não mudou** — Ollama só entra com opt-in explícito pra não quebrar produção em servidor sem Ollama.
  - **Sem benchmark de qualidade vs Gemini** ainda — user vai rodar `scripts/comparar_reforma_tabela_vs_llm.py --lote 21854782` manualmente.
  - **Latência esperada** 5-15s/chamada em Gemma 3 4B no M-series vs <2s do Gemini Flash.

### S — Aba Cidades & Frete na planilha ✅
- **Branch:** `claude/festive-tesla-6c18ae`
- **Arquivos:** [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — novo `_write_cidades_frete_sheet(empresa_id, session)` engatado no `exportar()`. Aba `cidades_<empresa_id>` com 1 linha por município no raio operacional + frete por categoria + contagem de lotes ativos (fim_em > now) com origem naquela cidade.
- **Motivação:** usuário pediu visibilidade direta de "de onde estamos olhando carros + custo logístico de cada cidade". Antes a info ficava espalhada (raio no YAML + tabela de frete em outro lugar + origem dos lotes só na coluna do listing).
- **Como funciona:** Reusa `empresa.cidades_de_busca()` (já ordenado por distância haversine) e `empresa.frete_para(d, categoria)` para cada categoria do enum. Conta lotes ativos por (cidade, UF) com normalização case/accent-insensitive (espelha `_normaliza` do `geo.py`) pra colar mesmo quando AA grava "ARAGUARI" e o IBGE tem "Araguari". Pátio aparece com distância 0 e frete R$ 0 (comprador busca pessoalmente).
- **Cobertura:** 4 testes em [`tests/test_exportar_sheets.py`](tests/test_exportar_sheets.py) (`TestCidadesFreteSheet`) — aba criada com nome correto, pátio com 0/0, contagem com normalização, fallback silencioso quando YAML não existe (caminho atingido só nos testes pré-existentes que usam `empresa_id` mockado).
- **Limitações conhecidas:**
  - Lotes sem `origem_cidade`/`origem_uf` populados não entram na contagem (o scraper costuma popular, mas snapshots antigos podem ter NULL).
  - `lru_cache(32)` em `carregar_empresa` significa que mudanças no YAML em runtime do mesmo processo só refletem após reload — não impacta operação batch (script encerra entre runs).

### U — Leilões futuros na planilha (decoupling coleta × exibição) ✅
- **Branch:** `claude/add-future-auctions-ITtsf`
- **Arquivos:**
  - [`carros_sa/scraping/scraper_autoavaliar.py`](carros_sa/scraping/scraper_autoavaliar.py) — `_coletar_listagem_cidade` e `coletar_listagem` passam a aceitar `horizonte_dias: Optional[int] = None` (default None = coleta full). `_MAX_PAGINAS` subiu de 20 → 50 + log quando o site reporta mais páginas do que o teto.
  - [`carros_sa/orquestrador.py`](carros_sa/orquestrador.py) — `orquestrar` default `horizonte_dias=None`.
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — `SheetsExporter.exportar(horizonte_exibicao_dias: Optional[int] = None)` — filtra lotes com `fim_em > agora + N dias` da planilha.
  - [`carros_sa/cli.py`](carros_sa/cli.py) — `triagem --horizonte-dias` agora é **janela de exibição**, default 30d (era 7d de coleta). Coleta passa `None` pro scraper (pega o pipeline inteiro).
  - [`tests/test_scraper_paginacao.py`](tests/test_scraper_paginacao.py) — teste novo `test_sem_horizonte_deixa_passar_lotes_futuros` + `test_horizonte_dias_explicito_filtra_apos_paginar` (opt-in) + cap ajustado pra 50.
  - [`tests/test_exportar_sheets.py`](tests/test_exportar_sheets.py) — 2 testes novos: `horizonte_exibicao_dias=30` corta lotes 45d; `None` mantém tudo.
- **Motivação:** Usuário reclamou (2026-04-23) que a planilha só mostra leilões de hoje, apesar do site ter muitos lotes agendados pra dias futuros. Causa raiz: filtro `fim_em > agora + 7d` rodava **no scraper** (`_coletar_listagem_cidade`), descartando tudo antes do DB ver. Se a janela precisava crescer, tinha que re-scrape. Combinado com `_MAX_PAGINAS=20`, raios maiores podiam ter cidades truncadas silenciosamente.
- **Como funciona:** Scraper agora coleta tudo que aparece na listagem (inclui leilões agendados). DB guarda o pipeline futuro cheio. Usuário decide a janela de exibição via `--horizonte-dias N` (passa pro exporter, não pro scraper) sem precisar re-coletar. Subir `N` = ver mais dias à frente instantaneamente.
- **Limitações conhecidas:**
  - Mais lotes coletados = mais chamadas de detalhe/LLM no primeiro run contra DB novo. Sistema já tem dedup por lote_id (orquestrador short-circuita lotes já avaliados) então passadas subsequentes ficam finas.
  - Se a listagem do Auto Avaliar NÃO mostrar leilões agendados por padrão (pode haver filtro/status que precisa ser passado na URL), esse fix não os traz sozinho — pode precisar de investigação adicional na URL `?status=agendado` ou aba separada. A coleta hoje usa `&order=recforyou`; se o usuário ainda ver só leilões de hoje depois disso, é próximo passo.

### V — Fix coluna Laudo (PDF) "—" em massa + grade fantasma na planilha ✅
- **Branch:** `claude/fix-report-download-analysis-3WSXM`
- **Sintoma:** operador apontou em 25/abr planilha com 19 lotes "✓ Viável" mas coluna `Laudo (PDF)` em Z toda "—". Como `laudo_analisado=True` exige `LaudoCache.confidence ≥ 0.6` (PDF baixado e extraído com sucesso), a URL deveria estar em `detalhe.laudo_pdf_url` — mas chegava como `None` no exporter.
- **Causa raiz #1 (URL sumindo):** `scripts/limpar_decoys_laudo.py` rodava entre triagem e retry e anulava qualquer URL que não passasse em `is_laudo_pdf_url()` — incluindo URLs que JÁ tinham gerado `LaudoCache.confidence ≥ 0.6`. A allowlist (`storage.googleapis.com/doc-b2b`, `cdn-aav.autoavaliar.com.br`, `*.pdf+laudo`) é heurística; quando o AA serve laudo de host fora da lista, o gate rejeitava mesmo o PDF tendo sido baixado e validado por `_pdf_eh_laudo_valido()`. Resultado: planilha mostrava "—" em todos esses lotes e o retry ficava em loop tentando re-extrair.
- **Causa raiz #2 (grade fantasma):** slim-down do HEADER (commit `51ccc59`, 27→15 colunas) deixou as colunas P→AA órfãs em abas criadas em versões anteriores. `ws.clear()` esvazia o range ativo mas não derruba colunas no servidor — então rótulos antigos ("Reforma Estimada", "Racional Reforma", "Frete (R$)", "Justificativa", "URL", "Laudo (PDF)") congelavam em Z e mascaravam o "Laudo" novo em O.
- **Arquivos tocados:**
  - [`scripts/limpar_decoys_laudo.py`](scripts/limpar_decoys_laudo.py) — `limpar_decoys()` agora preserva URL+cache quando `LaudoCache.confidence ≥ 0.6` (evidência empírica vence allowlist). Novo contador `preservados_por_cache` no `ResultadoLimpeza`.
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — `_write_sheet` chama `ws.resize(cols=len(HEADER))` antes de `clear()+update()`, derrubando no servidor qualquer coluna além do HEADER atual. Fail-soft em versões de gspread que reclamem de no-op.
  - [`tests/test_decoy_laudo_guard.py`](tests/test_decoy_laudo_guard.py) — 2 testes novos: `test_url_fora_da_allowlist_com_cache_forte_e_preservada` (cobre o sintoma de produção) + `test_url_fora_da_allowlist_com_cache_fraco_e_limpa` (cache fraco continua sendo limpo normalmente).
  - [`tests/test_exportar_sheets.py`](tests/test_exportar_sheets.py) — `test_resize_encolhe_grade_pra_len_header` valida ordem `resize → clear → update`.
- **Cobertura:** 329/329 testes verdes localmente (3 novos). Hygiene check do DB real continua falhando em decoys legítimos.
- **Limitações conhecidas:**
  - O fix do limpar_decoys é retroativo só pra lotes em que o cache forte sobreviveu até hoje — quem já teve cache derrubado em ciclos anteriores precisa de 1 rodada de retry pra recriar.
  - O resize no exporter requer 1 export bem-sucedido pra limpar a aba; até lá, operador pode apagar manualmente as colunas P→AA na UI sem perda de dado real.

### T — Coluna "Tese" na planilha (sinalização baseada em histórico) ✅
- **Branch:** `claude/adoring-black-d891b8`
- **Arquivos:**
  - [`carros_sa/tools/tese.py`](carros_sa/tools/tese.py) — NOVO. Módulo puro com `TeseConfig`, `HistoricoStat`, `carregar_historico_stat(session)`, `calcular_tese(marca, modelo, km, lance_max, historico, config)`. Classifica lote em 3 níveis: `tipica` 🟢 / `fora_da_curva` 🟡 / `atipica` 🔴.
  - [`config/tese.yaml`](config/tese.yaml) — NOVO. Thresholds editáveis (ticket R$ 12k-85k, km 30k-260k, compras_min=2 por modelo) + 5 sinais ruins (V6 gasolina + km alto, diesel + km muito alto, nicho sem repetição, ticket acima do teto, elétrico sem revenda).
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — nova coluna "Tese" entre "Reforma (R$)" e "Anúncio"; pré-carrega `HistoricoStat` uma vez por export; fail-soft pra "—" se config quebra ou laudo pendente. Glossário atualizado.
  - [`carros_sa/tools/audit.py`](carros_sa/tools/audit.py) — invariante "string não vazia" pra Tese (mantém paridade HEADER↔CHECKS obrigatória do workstream Q).
  - [`data/historico/uberlandia_arrematado.csv`](data/historico/uberlandia_arrematado.csv) — +62 linhas das 68 compras Auto Avaliar dos PDFs "Minhas compras" (jul/2024 → mar/2026). 5 saltados por dados cortados nas quebras de página; Polo Track 2024 já estava no CSV e não foi duplicado. Total histórico: 94 Arrematados.
  - [`tests/test_tese.py`](tests/test_tese.py) — 14 testes: `_chave_modelo` normaliza variações (Focus Titanium ≡ Focus Sedan), agrega do DB real, típica/atípica/fora da curva com casos clássicos do histórico, desduplicação nicho×modelo_novo no render, `excludes` (V6+diesel não dupla-conta), km ausente tolerado.
- **Motivação:** usuário analisou 6 PDFs "Minhas compras" (68 lotes, `data/laudos_amostra/Compras _ AutoAvaliar[1-6].pdf`) e pediu sinalização **prescritiva mas não filtrante**: "dentro do padrão de compras antigas". Nome escolhido: **Tese** (como "tese de compra" em M&A/PE). Ranking por ROI fica intocado — operador lê a Tese lado-a-lado e decide.
- **Como funciona:** `calcular_tese` devolve célula pronta:
  - `🟢 típica — Focus ×7, R$ 40k, 150k km` (modelo ≥ 2 compras + ticket + km na faixa)
  - `🟡 fora da curva — V6 gasolina + km alto` (1 sinal ruim isolado OU um eixo fora)
  - `🔴 atípica — diesel + km muito alto · modelo sem histórico · ticket acima do teto` (2+ sinais)
  - Chave de agrupamento = `slug(marca) + "|" + primeira_palavra_slug(modelo)`. "Focus Titanium Plus", "FOCUS 2.0 SE" e "Focus Sedan Titanium" caem no bucket `ford|focus`.
  - Granularidade futura (separar Renegade Sport de Longitude) = mudança em `_chave_modelo` só; o resto continua igual.
- **Validação (banco real com 94 Arrematados):** Focus 2.0 Titanium (7 compras) → típica; Cadenza V6 + km 170k → atípica (V6 + nicho); Santa Fe V6 com 2 compras → fora da curva (não escala pra atípica porque nicho não dispara); JAC E-JS1 com 2 compras (< 5 exigidas pelo sinal) → fora da curva; Range Rover Vogue diesel 290k km R$ 90k → atípica (3 sinais: diesel + nicho + ticket).
- **Cobertura:** 14 testes novos em [`tests/test_tese.py`](tests/test_tese.py).
- **Limitações conhecidas:**
  - Chave modelo = primeira palavra. "Compass Sport" e "Compass Night Eagle" entram juntos em `jeep|compass`. Se precisar distinguir trim, quebrar `_chave_modelo` em 2 níveis (marca+primeira+segunda) — isolado a 1 função.
  - Sinais ruins são listas de substrings simples no nome do modelo — falso positivo possível (ex: "Santa Fe 3.5 Híbrido" casaria V6 se tivesse o pattern). Mitigado com `excludes`, mas o domínio é restrito à checagem por substring.
  - `HistoricoStat` é cached por export — múltiplos exports na mesma sessão não veem Arrematados novos sem re-chamar `carregar_historico_stat`. Aceitável (export roda 1x por run).
  - Coluna descritiva — NÃO afeta ranking, NÃO filtra. Lotes com laudo pendente mostram Tese "—" pra evitar sinal enganoso (lance_max está "—" nesses casos).

### U — Auditoria de completude de laudo (PDF + cache + URL) ✅
- **Branch:** `claude/great-turing-Nfdh4`
- **Arquivos:**
  - [`carros_sa/tools/laudo_audit.py`](carros_sa/tools/laudo_audit.py) — NOVO. `verificar_laudo_completo(lote, laudo, pdf_dir)` checa simultaneamente: (1) PDF persistido em `data/laudos_pdfs/<id>.pdf` (>5KB), (2) `LaudoCache.confidence ≥ 0.6`, (3) `raw_json.detalhe.laudo_pdf_url` passa em `is_laudo_pdf_url`. `auditar(session, empresa_id)` agrega para todos os lotes ativos espelhando o filtro do exporter (avaliados + fim_em futuro + não-encerrados).
  - [`scripts/auditar_laudos.py`](scripts/auditar_laudos.py) — NOVO. CLI que imprime relatório com lotes incompletos, motivo agregado e instruções de remediação. `--strict` retorna exit 1 quando há incompletos (pra travar cron).
  - [`carros_sa/orquestrador.py`](carros_sa/orquestrador.py) — fix preventivo: quando `_pdf_eh_laudo_valido` rejeita o arquivo baixado, agora também zera `raw_json.detalhe.laudo_pdf_url` no commit granular do mesmo passo. Antes, a URL "envenenada" continuava persistida → exporter renderia HYPERLINK clicável pra um PDF que o validador já tinha rejeitado.
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — coluna "Laudo" passa a ter 3 estados em vez de 2: HYPERLINK (URL válida), `"PDF salvo (link expirado)"` (PDF local existe mas URL pré-assinada já morreu), ou "—" (sem URL e sem PDF). URLs do `storage.googleapis.com/doc-b2b/` expiram em ~1h; o estado intermediário sinaliza que o laudo FOI analisado.
  - [`Makefile`](Makefile) — novo target `make auditar-laudos [EMPRESA=<id>]`.
  - [`tests/test_laudo_audit.py`](tests/test_laudo_audit.py) — 16 testes cobrindo: matriz das 3 condições, agregação, filtro de ativos, multi-tenant, e os 3 estados da célula "Laudo" no exporter.
- **Motivação:** usuário pediu garantia de que todo carro na lista tem laudo baixado, revisado e link na planilha — e identificar/resolver razão quando não tiver. As camadas defensivas existentes (`is_laudo_pdf_url` no scraper, `_pdf_eh_laudo_valido` no orquestrador, `limpar_decoys` no cron, retry diário) cobrem cada sintoma isoladamente, mas ninguém auditava o resultado integrado. Resultado: lotes "vazavam" pra planilha com 1 das 3 condições falhando sem ninguém perceber.
- **Como funciona o ciclo completo:**
  1. **Pipeline** baixa PDF → valida → extrai laudo → persiste URL no raw_json. Quando valida rejeita, agora zera URL (defesa contemporânea).
  2. **Cron** roda diariamente: `triagem` → `limpar_decoys` (defesa retroativa em URL legada) → retry de pendentes.
  3. **Exporter** renderiza 3 estados — link clicável, "PDF salvo", ou "—" — operador sabe exatamente o que pode fazer.
  4. **Auditoria** (`make auditar-laudos`) é a fonte única da verdade que cruza os 3 sinais e reporta gaps acionáveis.
- **Cobertura:** 16 testes em `tests/test_laudo_audit.py`. Suite total: **342 passed, 2 skipped** (skip do guard de DB e do orquestrador-async sem playwright).
- **Limitações conhecidas:**
  - URLs pré-assinadas do Google Storage expiram em ~1h. O estado "PDF salvo (link expirado)" é o melhor que dá pra fazer sem hospedar o PDF em outro lugar. Pra ter link permanente, o próximo passo é subir os PDFs no Google Drive (via service account já configurada) e armazenar `drive_file_id` no `LaudoCache` — mudança maior, fora deste workstream.
  - `auditar_laudos.py` não integra com o cron ainda — operador precisa rodar manualmente. Adicionar ao `setup_cron.sh` quando o estado típico for ≥99% completo (hoje, com URLs expiradas frequentes, ia spammar log).

### V — Link permanente do laudo via Google Drive ✅
- **Branch:** `claude/great-turing-vivoI`
- **Arquivos:**
  - [`carros_sa/tools/laudo_drive.py`](carros_sa/tools/laudo_drive.py) — NOVO. `LaudoDriveClient` com upload idempotente (procura `<lote>.pdf` na pasta antes de subir), tornar público com link, raw HTTP via `AuthorizedSession` (zero deps novas — `google-auth` e `requests` já estavam transitivamente). `build_default_drive_client()` lê env vars e fail-soft retorna None quando não configurado.
  - [`carros_sa/orquestrador.py`](carros_sa/orquestrador.py) — `_pipeline_lote` recebe `drive_client` e, após `_pdf_eh_laudo_valido` aprovar o PDF, sobe pro Drive. Erro no Drive não derruba pipeline (cai pro estado anterior). Persiste `laudo_drive_id` + `laudo_drive_url` em `raw_json["detalhe"]` (sem mexer em `models.py` — segue o padrão já usado pelo `laudo_pdf_url`).
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — coluna "Laudo" passa a ter 4 estados em ordem de preferência: Drive URL → URL AA fresca → "PDF salvo (link expirado)" → "—". Glossário atualizado.
  - [`carros_sa/tools/laudo_audit.py`](carros_sa/tools/laudo_audit.py) — `verificar_laudo_completo` aceita Drive URL como satisfação da condição "URL clicável" (sem precisar passar `is_laudo_pdf_url`). Novo campo `drive_persistente: bool` no `StatusLaudo` + `com_drive` no `RelatorioLaudos` distinguem completos com link permanente dos que dependem da URL AA-fresca.
  - [`scripts/backfill_laudos_drive.py`](scripts/backfill_laudos_drive.py) — NOVO. Sobe PDFs de `data/laudos_pdfs/` que ainda não têm Drive URL. Filtra por `confidence ≥ 0.6` (não desperdiça quota), prioriza ATIVOS, `--dry-run` e `--limite N` pra runs controlados, idempotente.
  - [`scripts/setup_cron.sh`](scripts/setup_cron.sh) — pipeline diário virou 5 passos: triagem → limpar-decoys → retry → backfill-drive → auditar `--strict`. Fail-soft no backfill e na auditoria pra não derrubar o cron, mas exit code != 0 deixa rastro óbvio no log.
  - [`carros_sa/cli.py`](carros_sa/cli.py) + [`scripts/triagem_diaria.py`](scripts/triagem_diaria.py) — instanciam `build_default_drive_client()` e injetam no `orquestrar`. Logam habilitado/desabilitado.
  - [`carros_sa/cli.py:sheets`](carros_sa/cli.py) — após o export chama `auditar()` e printa warning + instruções (`make limpar-decoys && make backfill-drive`) quando há gaps. Sem gaps, mostra resumo "✓ N/N completos · M com link Drive permanente".
  - [`Makefile`](Makefile) — alvo `make backfill-drive [EMPRESA=<id>]`.
  - [`.env.example`](.env.example) — documenta `GOOGLE_DRIVE_LAUDOS_FOLDER_ID`.
- **Motivação:** usuário pediu garantir que todo carro tem laudo baixado, revisado E LINK NA PLANILHA, com diagnóstico+fix pra "nunca mais acontecer". URLs do `storage.googleapis.com/doc-b2b/...` (pré-assinadas pelo Auto Avaliar) expiram em ~1h, então a planilha publicada de manhã ficava com links mortos à tarde. Workstream U havia identificado isso como next-step explícito.
- **Como funciona o ciclo "nunca mais":**
  1. **Triagem** valida PDF (`_pdf_eh_laudo_valido`), sobe pro Drive, persiste `laudo_drive_url` em `raw_json`. Pipeline robusto a falha do Drive (continua sem ele).
  2. **Cron diário** roda backfill pra promover lotes legados (PDF local existente + cache forte + sem Drive URL ainda) e fecha com `auditar --strict`.
  3. **Exporter** prefere Drive URL > URL AA fresca > "PDF salvo (link expirado)" > "—" — operador sempre vê o estado mais útil.
  4. **`make sheets`** mostra warning na hora se algo está incompleto, com receita pra resolver.
  5. **`auditar_laudos`** distingue agora "completo com Drive" de "completo só com URL AA" — operador pode rodar `make backfill-drive` se a 2ª contagem for grande.
- **Setup necessário (one-time):** criar pasta no Drive → compartilhar com o e-mail do service-account → setar `GOOGLE_DRIVE_LAUDOS_FOLDER_ID` no `.env`. Sem isso, sistema fail-soft volta ao comportamento anterior (URL AA-fresca + "PDF salvo (link expirado)").
- **Cobertura:** 24 testes novos — 11 em [`tests/test_laudo_drive.py`](tests/test_laudo_drive.py) (upload idempotente, resumable, permissão público com tolerância a duplicate, fail-soft sem env), 8 em [`tests/test_backfill_laudos_drive.py`](tests/test_backfill_laudos_drive.py) (filtros PDF/cache/já-tem-Drive/empresa, dry-run, idempotência, erro num lote não derruba o loop), 5 em [`tests/test_laudo_audit.py`](tests/test_laudo_audit.py) (Drive satisfaz `url_persistida_ok` mesmo com AA decoy, `com_drive` separado de `completos`, prioridade Drive>AA no exporter). Suite total: **396 passed, 2 skipped**.
- **Limitações conhecidas:**
  - Mock-heavy nos testes (regra do CLAUDE.md: nada de chamada externa em testes). Validação ao vivo requer rodar 1x com a folder ID setada — recomendo backfill `--limite 5 --dry-run` primeiro.
  - Cota da Drive API: free-tier permite ~1B requests/dia, então as ~5 chamadas/lote (search+upload+permission) folgam mesmo com 600 lotes/mês. Se rampar muito, monitorar.
  - PDF do laudo é imutável por construção (mesmo lote → mesmo conteúdo). `upload()` não substitui — se um dia precisar (re-extração com novo seletor), expor `forcar=True` no backfill.

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
