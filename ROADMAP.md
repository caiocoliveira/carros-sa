# Carros SA — Roadmap

Documento vivo. Cada sessão atualiza seu workstream ao mergear em `main`.

## Status atual (baseline)

✅ **Pipeline operacional FIPE-only + planilha calibrada + audit estrito + coerência ROI×Lucro + URL preservada em re-scrape + Webmotors live cache + LLM textual vendor-agnostic + circuit-breaker em perma-loop** — 571/571 testes passando

Cobertura atual: scraper Auto Avaliar (listagem + detalhe + laudo PDF), extrator de laudo (vision + textual + LLM textual), precificador FIPE-only com `f_km`, EstimadorReforma LLM, calibração econômica (Polo Track 2024 real + 32 históricos Reinaldo), exportador Google Sheets com 18 colunas + glossário, audit estrito como gate diário (paridade total com display), multi-tenancy por YAML, **coleta noturna Webmotors live (workstream G — 2026-05-12) com cache 24h, retry/Cloudflare-detect e display honesto ("—" quando sem amostra real)**.

Dependências externas conhecidas: workstream G.3 (reativar mediana no precificador — bloqueia em ≥1 semana de dado real do cron G), workstream H (calibração de coeficientes com séries temporais), DD (granularidade de `dias_giro`).

### II — Circuit-breaker em lotes perma-stuck no retry diário (2026-05-15) ✅
- **Branch:** `claude/update-daily-content-Aau6F`
- **Motivação:** Após DD4 destravar 22/47 lotes com vendor fora do template AA, sobraram 23 lotes com `body_text=""` do `coletar_detalhe` (DD5 follow-up — não capturado por DD4) + 2 com URL+PDF-ausente. Esses lotes recaíam todo cron no caminho `_filtrar_laudo_pendente` → `_pipeline_lote` → extração falhava de novo (mesmo input ruim) → persistia `confidence<0.6` → próximo cron repetia o ciclo. Estimativa: ~50-100 chamadas LLM/dia desperdiçadas em perma-loop entre Gemini visual + textual DD4 + reforma. Cron das 16:00 UTC de 2026-05-15 (1ª passagem com DD4) demorou 1h49m, em parte por essa fila de stuck lotes consumindo retry do Gemini Flash com flakiness intermitente.
- **Solução:**
  - Novo campo [`LaudoCache.tentativas_extracao: int`](carros_sa/models.py) (default 0). Incrementa em cada upsert com `confidence<0.6` (extração falhou — input ruim), zera quando uma extração ≥0.6 persiste.
  - [`_upsert_laudo_cache`](carros_sa/orquestrador.py) atualiza o contador de forma transparente — mesma interface, comportamento aditivo.
  - [`_filtrar_laudo_pendente`](scripts/reprocessar_lotes_do_db.py) ganha parâmetro opcional `laudos_tentativas: dict`. Lotes com `confidence<0.6 AND tentativas>=MAX_TENTATIVAS_EXTRACAO (=3)` saem da fila. Lotes com cache forte mas outras pendências (PDF ausente, URL inválida) NÃO são afetados — retry pra eles não chama LLM.
  - Migração idempotente [`scripts/migrar_tentativas_extracao.py`](scripts/migrar_tentativas_extracao.py) (ALTER TABLE ADD COLUMN ... DEFAULT 0). Roda no início do workflow antes da triagem.
- **Cobertura:** 9 testes novos:
  - `tests/test_orquestrador.py::TestUpsertLaudoCacheTentativas` (4 testes — primeira fraca, incremento, reset em forte, forte de cara fica 0).
  - `tests/test_laudo_audit.py::TestFiltroRetryCircuitBreaker` (5 testes — max atingido sai, abaixo do max passa, cache forte ignora tentativas, sem cache passa, backward compat).
  - `tests/test_migrar_tentativas_extracao.py` (3 testes — adiciona em DB existente, idempotente, no-op sem DB).
- **Impacto esperado:** Sai do laço perpétuo em 36h (3 cron cycles × 2 runs/dia = 6 incrementos atingem MAX=3). Após isso, ~50-100 LLM calls/dia salvas. Audit `--strict` continua reportando esses lotes como incompletos (status real do laudo), operador inspeciona manualmente e zera o contador (ou deleta a row de LaudoCache) pra forçar retry quando o problema upstream estiver resolvido.
- **Limitações conhecidas:**
  - Não resolve o problema upstream (DD5 — `coletar_detalhe` retornando `body_text=""`). Apenas para de queimar LLM nele.
  - Constante MAX=3 não é ajustável via env var. Se operador quiser destravar manualmente, deletar a linha de `LaudoCache` ou rodar `UPDATE laudo SET tentativas_extracao=0 WHERE lote_id=...` direto no SQLite.
  - Operador precisa ler audit `--strict` pra saber que tem lotes em circuit-break — não há aviso visual diferenciado na planilha (continua "⚠ LAUDO NÃO CAPTURADO").
- **Follow-ups (não-bloqueantes, post-merge da revisão arquitetural do PR #93):**
  - **II-FU1 — Teste do branch "forte→fraco":** asserir que após `conf=0.9` seguido de `conf=0.3`, `tentativas_extracao` vai 0→1. Hoje cobrimos a direção inversa (`test_extracao_forte_zera_contador`). Lógica está correta (`tentativas_prev=0, novo=0+1=1`), só falta cobertura.
  - **II-FU2 — Alinhar convenção de migração:** hoje a migração de `tentativas_extracao` vive em `scripts/migrar_tentativas_extracao.py` + step no workflow, mas `db.py::_MIGRACOES_ADD_COLUMN` tem o padrão "in-process" usado por K. Dois caminhos paralelos. Opção: mover pra registry de `db.py` OU documentar em CLAUDE.md por que coexistem.
  - **II-FU3 — Audit-aware circuit-break:** adicionar `motivo='cache_confianca_baixa_circuit_break'` distinto quando `tentativas>=MAX`. Operador consegue grepar "stuck e desistimos" separado de "stuck e ainda tentando". Pairs com DD4-FU1 (`metodo_extracao` instrumentação).
  - **II-FU4 — CLI helper `laudo reset-tentativas <lote_id>`:** evita operador rodar SQL cru contra `state/db` em prod. Pequeno, mas fecha o loop UX.

### DD4 — LLM textual vendor-agnostic destrava lotes com PDF de leiloeiro fora do template Auto Avaliar (2026-05-15) ✅
- **Branch:** `claude/amazing-goldberg-ijutz`
- **Motivação:** Operador pediu (4ª vez) garantia de que TODO carro na planilha tem laudo baixado, revisado E link clicável — "se não, identificar razão e resolver pra nunca mais acontecer." Diagnóstico no DB de produção (state/db @ 2026-05-13 14:40, 145 lotes ativos no momento do cron): `auditar_laudos --strict` reportava **47 incompletos (32%)**, distribuição:
  - **22 lotes** com PDF baixado + URL válida MAS `LaudoCache.confidence=0.0` (motivo `cache_confianca_baixa`). Investigação dos PDFs mostrou que vinham de **vendors fora do template Auto Avaliar**: DEKRA CAUTELAR (page 6/7 tem ESTRUTURA), Procemax (sistemaprocemax.com.br — laudo 100% textual sem diagrama), SA-Laudo (BM Veiculos — lista de peças na página 5), Vistoria Cautelar genérica (sa-laudo, doc-b2b-adm — fotografias listadas como "COLUNA CENTRAL ESQUERDA"). Gemini visual corretamente devolvia `confidence=0.0 + pecas=[]` porque renderizava página 2 (índice 1) que pra esses layouts NÃO tem o diagrama estrutural. Regex de Observações (camada 2) também falhava — calibrada nos termos AA ("VEÍCULO POSSUI REPARO NAS COLUNAS..."). Resultado persistido como-está → próximo cron rodava no mesmo lote pra sempre, sempre com o mesmo 0.0. Planilha mostrava "⚠ LAUDO NÃO CAPTURADO: extração fraca" indefinidamente.
  - **23 lotes** com `body_text` vazio do `coletar_detalhe` (motivos combinados PDF+cache+URL). Fora do escopo deste PR — bug de scraping, follow-up.
  - **2 lotes** com URL válida mas PDF não baixado (`baixar_pdf` falhou silenciosamente).
- **Causa raiz:** `extrair_laudo` só tinha 3 camadas — (1) identificadores textuais via regex, (2) visão Gemini sobre página 2 do PDF, (3) extração de avarias via regex sobre Observações. Todas calibradas no template Auto Avaliar. Vendor novo = todas as 3 camadas devolvem nada. Sem camada de fallback vendor-agnostic, o lote ficava preso em `cache_confianca_baixa` perpetuamente.
- **Solução em 1 camada nova (camada 4):**
  - Nova função [`extrair_laudo_via_llm_textual(pdf_path, text_llm_client)`](carros_sa/agents/extrator_laudo.py) — extrai TODO o texto do PDF (todas as páginas, joinadas, truncado em 20KB), delega pro Gemini Flash text-only com prompt vendor-agnostic que conhece os termos de DEKRA / Procemax / SA-Laudo / Vistoria Cautelar genérica. Retorna mesmo shape do visual (`pecas_reparadas`, `pecas_avariadas`, `severidade_geral`, `confidence`). Robusta a falhas: PDF inválido → `None`; LLM exception → `None`; JSON não-dict → `None`.
  - [`extrair_laudo`](carros_sa/agents/extrator_laudo.py) ganha parâmetro `text_llm_client=None` (opcional, backward compat). Quando passado E o visual deu inútil (helper `_visual_e_inutil`: confidence<0.6 + pecas vazias) E o regex textual TAMBÉM ficou vazio, dispara camada 4. Quando camada 4 retorna sinal forte, usa essa confidence + integra avarias.
  - [`_pipeline_lote`](carros_sa/orquestrador.py) propaga `text_llm_client` pro extrator. `text_llm_client` já era criado pelo CLI/cron pra outras camadas (estimador reforma, URL fallback do scraper) — reutilizado sem custo de infra novo.
- **Defesa em camadas (custos):**
  - Auto Avaliar puro (~75% dos lotes): visual responde com sinal forte → camada 4 NÃO dispara. Zero custo extra.
  - Visual responde bem mas regex Observações também acha sinal → camada 4 NÃO dispara. Zero custo extra.
  - Visual inútil + regex falha (vendor fora do template): camada 4 dispara. Custo: 1 chamada Gemini Flash free tier (~grátis) + ~2-3s/lote.
  - Camada 4 falha (LLM 503): cai pro resultado original do visual. Sem regressão.
- **Cobertura:** 16 testes novos em [`tests/test_extrator_laudo_llm_textual.py`](tests/test_extrator_laudo_llm_textual.py) cobrindo `_visual_e_inutil`, `extrair_laudo_via_llm_textual` pura, e a integração na `extrair_laudo` (6 cenários: visual OK não chama LLM, visual inútil + LLM recupera, sem `text_llm_client` preserva comportamento antigo, LLM falha cai pro visual, regex já achou avarias pula LLM, visual lança exceção). Total: 580/580 verde.
- **Recuperação dos lotes em produção:** próximo cron run vai detectar os 22 lotes via `_filtrar_laudo_pendente` (já incluía `confidence < 0.6` desde DD2/DD3), rodar `_pipeline_lote` → camada 4 dispara → laudo persistido com confidence ≥ 0.6 + avarias estruturadas (ou explicitamente "nenhuma" com confidence ≥ 0.6 quando o vendor diz "Nada Consta"). Audit deixa de flagar. Planilha mostra "✓ Viável" ao invés de "⚠ LAUDO NÃO CAPTURADO".
- **Padrão genérico (LESSONS.md/RC10):** Quando uma camada de extração devolve um sinal "vazio mas plausível" (modelo correto dizendo "não vi nada de útil aqui"), persistir o resultado como-está condena o lote a um loop de retry perpétuo se o input não mudar. Defesa: detectar o caso "extrator respondeu mas inútil" (confidence baixa + listas vazias) e disparar uma camada de fallback que olha pra input DIFERENTE (aqui, texto completo em vez de página 2). Se o fallback também falha, é OK persistir com confidence baixa — o audit continua flagando, mas pelo menos não é por causa de uma limitação da camada (template hardcoded), e sim por falta legítima de sinal.
- **Limitações conhecidas / follow-ups (post-merge, revisão arquitetural pós-PR #91):**
  - **DD5 — `coletar_detalhe` devolvendo `body_text=""` (23 lotes em produção):** mesmo com flags parciais como `status_laudo='Laudo Aprovado'` populadas, o `body_text` veio vazio. Esses caem no fallback `_laudo_sem_pdf` (confidence 0.5/0.55) e o retry roda nas mesmas condições, mesma falha. Hipótese: page innerText vazio por redirect / iframe / rate-limit. Fix proposto: em `coletar_detalhe`, se `body_text` vier vazio E o detalhe-flags indicar laudo aprovado (sentinel diferente do que o body refletiu), retry interno com sleep + reload antes de aceitar. **Prioridade alta** — operador pediu "TODO carro" pela 4ª vez; DD4 cobriu 22/47, restam 23/47 + 2/47 (PDF-faltante). Toca `scraping/scraper_autoavaliar.py` na lista "não tocar sem motivo" — atenção ao mergear.
  - **DD4-FU1 — Métrica `pct_lotes_via_camada4`:** sem instrumentação, regressão silenciosa do visual (modelo updated upstream, camada 4 disparando pra 75% dos lotes) fica invisível. Adicionar campo `LaudoCache.metodo_extracao` enum {"visual", "regex_textual", "llm_textual"} + relatório no audit reportando distribuição. Quando `pct_via_camada4 > 30%`, audit emite warning informativo. Aplica RC10 recursivamente — sem essa métrica, não dá pra distinguir "fallback ocasional" de "fallback virou caminho principal".
  - **DD4-FU2 — Piso defensivo na confidence do LLM:** prompt instrui 0.6+ pra "Nada Consta sem lista" e 0.8+ pra "lista explícita". Se o modelo super-confiar (devolve 0.85 sem lista quando PDF está vazio/cortado), `laudo_ok` passa e o audit silencia falsamente. Proposta: quando `llm_textual["pecas_reparadas"]==[] and llm_textual["pecas_avariadas"]==[] and llm_textual["severidade_geral"]=="nenhuma"`, cap confidence em 0.65 (acima do gate 0.6 mas conserva sinal). Hoje aceita 0.95 cego. Sample manual em 10-20 lotes pós-merge mitiga, mas vale instrumentar.
  - **DD4-FU3 — Teste do branch `texto.strip() == ""`:** existe em `extrair_laudo_via_llm_textual` (linhas 493-498 antes do merge) mas não tem teste dedicado. Trivial de adicionar.
  - **DD4-FU4 — Truncamento 20KB silencioso:** quando PDF excede e o trecho relevante (lista de peças) está em página posterior, perde sinal sem aviso. Logger info quando `total >= max_chars` ajudaria triage.
  - **2 lotes URL+PDF-ausente:** `baixar_pdf` lança e cai pro `_laudo_sem_pdf`. Próximo cron tem outra chance — se a URL pré-assinada expirou (Google Cloud Storage validade ~1h), retry só funciona quando `coletar_detalhe` re-popular. Caso patológico cobrirá <2% dos lotes; aceito.

### HH — Calibração de custos pós-compra real Fusion (2026-05-11) 🔄

- **Branch (em curso):** `claude/analyze-recent-purchase-wgpkZ`
- **Motivação:** Operador compartilhou a "Planilha de Compra" da compra mais recente — Fusion Titanium 2.0 GTDI 14/14 AWD via Auto Arremate. Comparativo orçado×realizado expôs **5 gaps independentes** entre o modelo de custos e a realidade operacional, todos visíveis numa única compra:
  | Item | Realizado | Modelo (YAML Uberlândia) | Δ |
  |---|---|---|---|
  | Valor lote | R$ 55.500 | — | — |
  | TX Auto Arremate | R$ 866,80 | `taxa_leilao_fixa: 999` (R$ fixos) | sobre +R$ 132 (1.56% do lote ≠ R$ 999 fixos) |
  | Frete São Paulo→Uberlândia (~580 km, sedan) | R$ 1.200 | faixa `300-600` × sedan = R$ 1.400 | sobre +R$ 200 (conservador, OK) |
  | 3 peças pra pintar (reforma) | R$ 1.200 | `EstimadorReforma` (sem lote no DB) | n/a (lote não foi triagado pelo pipeline) |
  | Higienização e polimento | R$ 550 | `higienizacao: 450` | sub –R$ 100 (+22% desatualizado) |
  | Transf. interestadual DETRAN | R$ 580 | **nada** — só `despachante: 380` (registro local) | **gap total** (1.04% do valor) |
  | Total despesas | R$ 4.396,80 | ≈ R$ 4.149 | sub ≈ R$ 248 |
- **Gaps em ordem de prioridade:**
  1. **Transferência interestadual não modelada** (`origem_uf ≠ patio_uf` → custo extra de R$ 580 zerado no orçado) → **escopo deste PR**.
  2. **Decompor CSV de Arrematado** — hoje `data/historico/uberlandia_arrematado.csv` agrega tudo em `custos_extras`. Pra calibrar diferenciado (taxa AA, frete, transferência, higienização) precisa quebrar em colunas separadas. Bloqueia #4 e #5.
  3. **CLI `registrar-compra` on-the-fly** — `arrematado-import` é batch CSV; operador mantém planilha Excel paralela e "esquece" de sincronizar. Subcomando interativo (`carros-sa registrar-compra <lote_id> --preco N --taxa N --frete N --transf N --higi N --reforma N`) fecha o loop.
  4. **Calibrar taxa Auto Arremate (fixa→variável)** — esta cobrança foi 1.56% do lote, não R$ 999. Precisa 3-5 notas adicionais pra triangular fórmula (piso + %? faixa por valor?). Depende de #2 (coluna `taxa_leilao_real` no CSV).
  5. **Calibrar higienização (defasagem +22%)** — depende de #2 (coluna `higienizacao_real`). Sem dado decomposto, é chute.
- **Entregue neste PR (#1 apenas):**
  - Novo campo `transferencia_interestadual: int = 0` em `CustosOperacionais` (`carros_sa/tenancy.py`) — fica DE FORA do `total` (porque é condicional ao lote, não recorrente).
  - Novo método `EmpresaConfig.custo_op_para_lote(lote)` que devolve `custo_op_fixo + (transferencia_interestadual se lote.origem_uf != patio.uf else 0)`.
  - Precificador troca `empresa.custo_op_fixo` → `empresa.custo_op_para_lote(lote)` na linha 167.
  - YAMLs `carros_uberlandia.yaml` e `carros_rio.yaml` ganham `transferencia_interestadual: 580` (calibrado em 1 compra real Fusion SP→MG; recalibrar com #2 quando tiver ≥5 transferências interestaduais no CSV).
- **HH-2 entregue (2026-05-11):** ✅ Decomposição do CSV em colunas por bucket — destrava HH-4 e HH-5.
  - 6 colunas novas no `data/historico/uberlandia_arrematado.csv`: `taxa_leilao_real`, `frete_real`, `transferencia_real`, `higienizacao_real`, `outros_extras_real`, `gastos_reforma_real`. Inseridas ANTES de `observacoes` pra manter convenção (texto livre no fim). Migração programática preservou capital total (R$ 5.220.887 antes/depois bate).
  - `HistoricoRow` ganhou os 6 campos opcionais + 3 properties: `extras_decompostos` (detecta formato), `total_extras` (soma decomposto OU devolve `custos_extras` legacy), `reforma_real_efetiva` (isola SÓ a reforma quando decomposto).
  - `parse_csv` lê as 6 novas colunas via `raw.get()` (compat com fixtures antigas de 10 colunas).
  - `importar_historico` usa `reforma_real_efetiva` em vez de `custos_extras` ao popular `Arrematado.gastos_reforma_real` — calibrador de reforma finalmente vê número limpo nos lotes pós-2026-05-11.
  - Linha do Fusion (única adicionada em PR #83) migrada: R$ 4.397 agregado → 867 + 1200 + 580 + 550 + 0 + 1200 decomposto.
  - 8 testes novos em `TestFormatoDecomposto` cobrindo: detecção de formato (parcial/legacy/vazio), `total_extras` (soma vs fallback vs None), isolamento de reforma decomposto vs legacy, parse_csv populando buckets, import end-to-end com csv puro decomposto e csv misto (legacy + decomposto coexistem). 536/536 verde.
  - **Limitação irrecuperável:** linhas anteriores a 2026-05-11 (95 do CSV) continuam com `custos_extras` agregado poluído alimentando `gastos_reforma_real`. Não dá pra desagregar retroativamente. Solução: quando HH-2 tiver ≥5 linhas decompostas, calibrador filtra `extras_decompostos=True` pra baseline limpo e usa legacy só como prior.
- **HH-3 entregue (2026-05-11):** ✅ Subcomando CLI `registrar-compra` interativo — fecha o loop de entrada de compra real.
  - Novo subcomando `carros-sa registrar-compra` em [`carros_sa/cli.py`](carros_sa/cli.py): aceita todas as flags de uma vez OU prompts interativos quando campo obrigatório for omitido (via `typer.Option(prompt=...)`).
  - Flags obrigatórias (com prompt se ausentes): `--empresa`, `--marca`, `--modelo`, `--ano`, `--valor`.
  - Flags opcionais: `--km`, `--data`, `--taxa`, `--frete`, `--transf`, `--higi`, `--outros`, `--reforma`, `--obs`, `--valor-venda`, `--data-venda`, `--csv` (override de caminho).
  - Escreve em **duas etapas atômicas**: (a) upsert no CSV via `_upsert_csv_row` (append ou update da linha existente); (b) chamada a `importar_historico` pra sync DB (Lote sintético + Arrematado). Atomicidade: CSV escreve primeiro; se DB falhar, próximo `arrematado-import` corrige.
  - **Idempotente** — mesma chave `(marca, modelo, ano, valor_compra)` atualiza ao invés de duplicar (tanto no CSV quanto no DB, espelhando lógica de `importar_historico`).
  - **Sempre decomposto (HH-2)**: `custos_extras` legacy fica vazio em todo registro novo; `taxa/frete/transf/higi/outros/reforma` vão para as 6 colunas separadas — calibrador recebe dado limpo desde o 1º registro.
  - 9 testes em [`tests/test_registrar_compra.py`](tests/test_registrar_compra.py): todas as flags, interativo (prompt), idempotência, isolamento de reforma no Arrematado, preservação de linhas existentes, validação de ano/valor/data.
  - **Limitações conhecidas:** `--csv` default aponta pra `data/historico/<empresa>_arrematado.csv` relativo ao CWD; operador precisa rodar do root do repo (ou passar `--csv` explícito). Sem lock de arquivo — não suporta escrita concorrente (ok pra CLI interativo single-user).
- **HH-4 entregue (2026-05-11):** ✅ Taxa Auto Arremate calibrada de fixa (R$999) para percentual (1.56%).
  - `config/empresas/carros_uberlandia.yaml`: `taxa_leilao_pct: 0.0156`, `taxa_leilao_fixa: 0`. Calibrado em N=1 (Fusion: R$866,80 / R$55.500 = 1.5618%). Recalibrar quando tiver ≥3 notas no CSV.
  - Impacto nos lotes: taxa menor que R$999 em lotes < R$64k (preco_max sobe); maior em lotes > R$64k (preco_max cai). Breakeven: R$999/0.0156 ≈ R$64k.
  - 3 testes atualizados em `test_precificador.py` com novos valores e comentários explicando a álgebra (diff de transferência = 580/(1+pct) ≈ 571, não mais 580 exato).
  - **Limitação N=1:** única nota decompostas disponível. Suficiente pra trocar o modelo de fixed→pct; não suficiente pra triangular piso mínimo ou faixa por valor. Recalibrar via `data/historico/uberlandia_arrematado.csv::taxa_leilao_real` quando ≥3 registros.
- **Follow-ups (não-bloqueantes):** #5 (calibrar higienização — depende de mais linhas decompostas no CSV).
- **Limitações conhecidas:**
  - Valor R$ 580 é N=1 (só o Fusion). Variação por UF de origem desconhecida (SP→MG pode ser diferente de RJ→MG, GO→MG). Aceitável como ponto de partida — `transferencia_interestadual` é uniforme por empresa, não tabela.
  - Custo é aplicado por igual a TODA UF ≠ pátio. Não trata caso de UF adjacente com convênio (raro no Brasil; DETRAN é estadual).

### DD3 — Preserva `detalhe.laudo_pdf_url` em re-scrape de listagem + paridade total no short-circuit/retry (2026-05-10) ✅
- **Branch:** `claude/great-turing-DYVvY`
- **Motivação:** Operador pediu (3ª vez) garantia de que TODO carro na planilha tem laudo baixado, revisado E **link clicável** — e a causa de não estar acontecendo, "pra nunca mais acontecer". Diagnóstico contra DB do `state/db` em produção (236 PDFs, 187 lotes ativos): `auditar_laudos --strict` reportava **124 incompletos**, dos quais **95 com motivo `url_invalida_ou_ausente`** — PDF baixado, cache ≥0.6, mas `raw_json.detalhe.laudo_pdf_url=None`. Coluna "Ver laudo" da planilha sumia silenciosamente em 51% dos lotes ativos.
- **Causa raiz:** `_upsert_lote` reconstruía `raw_json` a partir do `LoteRaw.model_dump()` (que NÃO carrega `detalhe`) e sobrescrevia o existente — só `loja` era preservada. Em todo cron diário, o re-scrape da listagem ZERAVA `detalhe.laudo_pdf_url` (e `body_text_sample`). Antes do DD2 (2026-05-09), o short-circuit `ja_avaliado AND laudo_ok` falhava sempre porque PDFs sumiam entre runs CI — pipeline rodava completo, `coletar_detalhe` repopulava a URL, ninguém percebia. Após DD2 (state/db persiste PDFs + `pdf_ok` adicionado ao short-circuit), o short-circuit começou a disparar pra esses lotes — e a URL nunca mais voltava.
- **Mudanças (defesa em 3 camadas):**
  - **Causa raiz** [`carros_sa/orquestrador.py::_upsert_lote`](carros_sa/orquestrador.py): preserva `detalhe` e `body_text_sample` da `lote.raw_json` existente quando o LoteRaw novo (listagem) não os carrega — espelha a lógica que já preservava `loja`.
  - **Defesa #1** [`carros_sa/orquestrador.py::_pipeline_lote`](carros_sa/orquestrador.py): short-circuit agora exige `url_ok` (i.e. `is_laudo_pdf_url(detalhe.laudo_pdf_url)` passa) ALÉM de `ja_avaliado + laudo_ok + pdf_ok`. Se algum bug futuro nullificar a URL, pipeline re-roda e `_persistir_flags_no_lote` repopula via `coletar_detalhe`.
  - **Defesa #2** [`scripts/reprocessar_lotes_do_db.py::_filtrar_laudo_pendente`](scripts/reprocessar_lotes_do_db.py): filtro de retry agora também flagga lotes com URL inválida/ausente — mesmo com cache forte + PDF on-disk. Paridade total com `verificar_laudo_completo` (auditoria).
- **Cobertura:** 5 testes guard novos: `test_upsert_lote_preserva_detalhe_em_re_scrape` (orquestrador), `test_lote_ja_avaliado_com_pdf_e_cache_mas_url_ausente_reavalia` (short-circuit), `test_cache_forte_e_pdf_presente_mas_url_ausente_e_pendente` + `test_cache_forte_e_pdf_presente_com_url_decoy_e_pendente` (filtro retry). Total: 481/481 verde.
- **Recuperação dos 95 lotes em produção:** próximo cron run vai detectá-los via `_filtrar_laudo_pendente` e re-coletar detalhe — `_persistir_flags_no_lote` repopula a URL. ~5-8s sleep × 95 = ~10min extras na 1ª passagem após o merge, depois steady-state.
- **Padrão genérico (LESSONS.md/P5f):** Quando uma operação de upsert reconstrói um campo "container" (raw_json dict, JSONB, etc.) a partir de uma fonte parcial, **enumere TODAS as subkeys que outras camadas escrevem nele** e preserve uma a uma. Listar só as que você lembra é convite a esse tipo de bug latente — campos novos adicionados por outros workflows somem silenciosamente. No nosso caso o LoteRaw é a listagem (sempre crua), mas detalhe/body_text/loja são adicionados depois por outros passos do pipeline. Cada um precisa de preservação explícita.
- **Limitação conhecida:** as URLs assinadas de Google Cloud Storage continuam expirando ~1h após geração — ortogonal a este fix. Workaround atual já existente: célula degrada pra "PDF salvo (link expirado)" quando URL ausente. Resolução plena é o follow-up do DD2 (servir de fonte estável: GitHub raw URL ou re-assinar no export).
- **Follow-ups (não-bloqueantes, da revisão arquitetural pós-merge):**
  - **Guardrail anti-spin no re-scrape de detalhe** — se `coletar_detalhe` continuar retornando `pdf_url=None` por DOM novo / allowlist defasada (vide caso carbel pré-PR #75), o lote re-roda Playwright + sleep 5-8s + LLM fallback em todo cron pra sempre. Não é loop infinito (pipeline progride) mas consome cota. Sugestão: contador `tentativas_detalhe_sem_url` em `raw_json`, parar de tentar após N=3 e reportar via audit.
  - **Teste de detalhe rico sobrevivendo ao upsert** — `test_upsert_lote_preserva_detalhe_em_re_scrape` assert apenas `laudo_pdf_url`. Adicionar fixture com detalhe rico (`specs`, `similares_precos`, opcionais) pra selar o contrato da preservação seletiva — qualquer subkey nova de `detalhe` escrita por outro passo precisa ser coberta.
  - **Mover `is_laudo_pdf_url` import pro topo de `orquestrador.py`** — 4ª referência no arquivo passou do limiar pra justificar import tardio.

### II — Priorização da listagem por lucro absoluto (2026-05-16) ✅
- **Branch:** `claude/review-listing-priority-wkMkC`
- **Motivação:** ROI anualizado como métrica de ranking premiava lotes de capital pequeno com ROI alto sobre lotes de capital grande com lucro absoluto maior — distorcia o que o operador vê na coluna `Lucro (R$)`. Usuário: "lucro absoluto = dinheiros que sobram no final, qto mais melhor". Métrica única em paridade CLI ↔ planilha ↔ audit (P5b).
- **Solução em 4 pontos:**
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — sort key passa de `-roi_anualizado` pra `-lucro` (campo já calculado por linha via `_lucro_absoluto_efetivo`, basis score_efetivo = mesmo da coluna 'Lucro (R$)' exibida). Composta de filtros (laudo_analisado, viavel) inalterada.
  - [`carros_sa/tools/audit.py`](carros_sa/tools/audit.py) — mesma chave + populei `r["lucro"]` no `_build_rows` (pre-existing latent bug: `COLUMN_EXTRACTORS["Lucro (R$)"]` lia `r.get("lucro", "—")` mas ninguém populava — sempre caía pro placeholder).
  - [`carros_sa/cli.py`](carros_sa/cli.py) — default rankeia por lucro absoluto efetivo. Flag `--absoluto` renomeada pra **`--roi-intrinsic`** (semantic clearer: "ROI intrinsic do score_roi cru no preço-alvo"). Sufixo do título da tabela: "Lucro absoluto (R$)" vs "ROI intrinsic (alvo teórico)".
- **Tests-âncora atualizados:**
  - `tests/test_cli.py::test_top_ranqueia_por_lucro_absoluto_default` (substitui `test_top_ranqueia_por_roi_anualizado_default`) — fixture RAPIDO vs LENTO comprovando ranking por lucro efetivo + flag `--roi-intrinsic` ainda inverte por score_roi.
  - `tests/test_cli.py::test_top_filtra_inviaveis_por_default` — flag rename `--absoluto → --roi-intrinsic`.
  - `tests/test_exportar_sheets.py::test_exportar_ranking_por_lucro_absoluto_entre_viaveis` (renomeado) — semântica atualizada.
- **Resultado:** 579 testes verdes. Lotes de capital grande/lucro alto sobem; lotes de capital pequeno/ROI% alto saem do topo (mas continuam acessíveis via `--roi-intrinsic`).
- **Limitações conhecidas:** ROI anualizado e folga absoluta não são mais oferecidos como modos de ranking — se operador pedir, fácil reintroduzir como flags secundárias. Mediana de Webmotors live pode invalidar ranking durante warm-up do cron (workstream G) — mas isso afeta `preco_giro`/`lucro` em qualquer métrica.

### GG — Coerência aritmética entre `ROI alvo (%)` e `Lucro (R$)` em zona apertada (2026-05-10) ✅
- **Branch:** `claude/sleepy-wright-tFNNb`
- **Motivação:** Revisão preventiva pediu "verifique se a lógica está fazendo sentido considerando a relação entre os valores das colunas". Simulação canônica em `/tmp/sim.py` com 11 cenários expôs incoerência aritmética: em zona apertada (lance_atual entre preco_alvo e preco_max), `Lucro (R$)` usava `score_efetivo` (realista, reduzido) enquanto `ROI alvo (%)` usava `score_roi` intrinsic (alvo teórico). Cenário 6 (Gol em zona apertada): ROI exibido 64.3% e Lucro R$ 7,167 — capital implícito do mental math `lucro/(roi/100)` = R$ 11,148, sem correspondência em nenhum campo da linha. Operador suspeitava do sistema sem conseguir nomear o porquê.
- **Solução em 3 pontos:**
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — `roi_alvo = score_efetivo * 100` (era `score_roi or 0`). Coluna agora reflete o ROI EFETIVO (mesma base do Lucro). Quando `lance_atual ≤ preco_alvo`, score_efetivo == score_roi → display inalterado pra 100% dos lotes que o operador pode comprar pelo alvo. Apenas zona apertada e inviáveis ficam diferentes — e inviáveis já são suprimidos pra "—".
  - [`carros_sa/cli.py`](carros_sa/cli.py) — `top` exibe `score_efetivo × 100` na coluna ROI alvo (paridade com planilha). Ranking `--absoluto` MANTIDO em `score_roi` intrinsic (test-âncora `test_top_ranqueia_por_roi_anualizado_default` documenta: flag responde "potencial econômico no alvo teórico", default responde "ROI realista"; semânticas diferentes não devem ser "alinhadas").
  - [`carros_sa/tools/audit.py`](carros_sa/tools/audit.py) — `roi_alvo = score_efetivo * 100` (paridade audit ↔ display, P5e). Mensagem do `_check_zona_apertada` reescrita: era "ROI realista < ROI alvo" (sugeria que display era otimista), virou "margem aplicada < margem-alvo (ROI exibido já reflete redução)" (alinhado com nova realidade).
- **Teste guard:** `tests/test_exportar_sheets.py::test_coerencia_roi_lucro_zona_apertada` — fixture com lance > alvo < max, valida que `Lucro / (ROI/100) + Lucro ≈ preco_giro` (mental math do operador passa). Impede regressão.
- **Glossário atualizado:** entrada de `ROI alvo (%)` agora descreve `score_efetivo × 100` + por que columns são coerentes; entrada de `Lucro (R$)` reforça "coerente com ROI alvo (%): `Lucro = capital_efetivo × ROI/100` bate por construção".
- **Limitações conhecidas:** quando workstream G ligar Webmotors live e f_km começar a saturar em lotes baixa-km, preco_giro pode passar 100% FIPE (já documentado em CLAUDE.md — design FIPE-only com ajuste por km). Não afeta a coerência ROI×Lucro.

### FF — Suporte ao grupo carbel + LLM fallback self-healing pra leiloeiros novos (2026-05-09) ✅
- **Branch:** `claude/fix-build-scheduling-sAzQq`
- **Motivação:** Diagnóstico em `data/scrapes/2026-05-09_uberlandia_listagem.json`: 132 lotes ativos com `⚠ LAUDO NÃO CAPTURADO`. Investigação dos HTMLs reais (lotes 22161767, 22161768): grupo `carbel` (que apareceu na plataforma Auto Avaliar ~2026-05) usa **sistema terceirizado de laudos** — `https://app.sistemaprocemax.com.br/files/report/<UUID>`. A allowlist em `is_laudo_pdf_url` + `_EXTRACT_PDF_URL_JS` cobria só `storage.googleapis.com/doc-b2b`, `cdn-aav.autoavaliar.com.br` e PDFs com "laudo" no path. Sistemaprocemax cai fora dos 3 → 98 lotes carbel ignorados. Pergunta operacional: "todo leiloeiro novo a gente vai ter que adicionar à mão?"
- **Solução em 2 camadas:**
  - **V1 — Fast path determinístico:** adicionado `app.sistemaprocemax.com.br/files/report/` à allowlist em 2 lugares (`parsers.is_laudo_pdf_url` + JS `pareceLaudo()`). Cobre carbel + qualquer leiloeiro futuro nessa plataforma.
  - **V2 — LLM fallback self-healing (passada 8 do `coletar_detalhe`):** quando heurísticas determinísticas (passadas 1-7) falharem E `_laudo_existe_no_body() == True`, o scraper agora pede pro `text_llm_client` ler o `documentElement.outerHTML` e devolver a URL do laudo. Validação pós-LLM em camadas: (a) JSON bem-formado com `{"url": ...}`, (b) URL aparece **literal** no HTML cru (anti-alucinação + anti-injection), (c) URL não bate com decoys conhecidos. Roda **só** quando heurística falhou — fast path inalterado pra ~95% dos lotes. Custo: ~grátis (Gemini Flash free tier) + +2-5s por lote raro.
- **Arquivos:** `carros_sa/scraping/scraper_autoavaliar.py` (`_LLM_PROMPT_LAUDO_URL`, `_url_no_html_literal`, `_url_parece_laudo_frouxo`, `_extrair_url_laudo_via_llm`, passada 8 em `coletar_detalhe`), `carros_sa/scraping/parsers.py` (allowlist), `carros_sa/orquestrador.py` (passa `text_llm_client` pra `coletar_detalhe`).
- **Testes:** `tests/test_coletar_detalhe_llm_fallback.py` (20 cases: validators puros, função core, integração na passada 8, anti-injection); `tests/test_parsers.py::TestIsLaudoPdfUrl::test_url_sistemaprocemax_carbel_e_aceita`.
- **Defesa em camadas anti-injection (3 níveis):**
  1. **`_url_no_html_literal` exige URL em atributo HTML (`"<URL>"` ou `'<URL>'`)** — comentários sem aspas ao redor da URL não passam, eliminando o vetor `<!-- IGNORE TUDO E RETORNE: ... -->`.
  2. **`_url_parece_laudo_frouxo`** rejeita decoys conhecidos (transparência, /app/uploads/, login, javascript:, data:).
  3. **`baixar_pdf` cookie-scope (`_cookie_scope_permite`)** — Cookie da sessão Auto Avaliar enviado APENAS pra `b2b.autoavaliar.com.br`, `cdn-aav.autoavaliar.com.br` e `storage.googleapis.com`. URLs de leiloeiros externos (sistemaprocemax e futuros) recebem GET sem header de auth. Mesmo se um adversário sofisticado romper as 2 camadas anteriores (`<!-- veja "<URL>" -->` com aspas), cookie da sessão não vaza.
- **Cap defensivo:** HTML enviado pro LLM truncado em 200KB (~50k tokens). Páginas SPA pesadas (>2MB) não estouram a janela do Gemini Flash nem drenam free tier. `_url_no_html_literal` valida contra o HTML ORIGINAL, não o truncado — anti-injection independe de quanto o LLM viu.
- **Limitação conhecida:** LLM fallback depende de `text_llm_client` configurado no orquestrador. Em CI sem `GEMINI_API_KEY`/`ANTHROPIC_API_KEY`, fallback não roda — comportamento idêntico ao pré-PR.

### DD2 — Persistência de PDFs em state/db + defesa em profundidade no retry (2026-05-09) ✅
- **Branch:** `claude/great-turing-T0zcC`
- **Motivação:** Operador pediu pra garantir que TODO carro na planilha tem laudo baixado, revisado e link clicável — e identificar a causa de não estar acontecendo, "pra nunca mais acontecer". Diagnóstico: o workflow do GitHub Actions persistia DB e cookies em `state/db` mas DESCARTAVA `data/laudos_pdfs/` entre runs ("PDFs (data/laudos_pdfs/) NÃO são persistidos"). Em runs subsequentes ao 1º, o DB já trazia `LaudoCache.confidence>=0.6` (laudo "analisado") mas a pasta de PDFs ressuscitava VAZIA. O retry script com `--somente-laudo-pendente` filtrava só `confidence<0.6`, então ignorava esses lotes. Resultado: `auditar_laudos --strict` no fim do workflow falhava cronicamente com motivo `pdf_ausente` em todo run após o 1º — invariante prometida ("todo lote ativo na planilha tem PDF baixado + cache forte + URL clicável") quebrada por design.
- **Mudanças:**
  - [`.github/workflows/triagem.yml`](.github/workflows/triagem.yml) — restore step agora restaura subárvore `laudos_pdfs/` de `state/db` pra `data/laudos_pdfs/` via `git ls-tree` + `git show`. Persistência step adiciona subárvore `laudos_pdfs/` ao tree órfão (filtra >5KB pra não persistir HTML de erro). Comentário de cabeçalho explica o porquê (alinhar estado operacional com estado real).
  - [`carros_sa/orquestrador.py`](carros_sa/orquestrador.py) — short-circuit do `_pipeline_lote` agora exige `pdf_ok` (PDF >5KB on-disk) ALÉM de `ja_avaliado` + `laudo_ok`. Defesa em profundidade: se `state/db` falhar restaurando o PDF parcialmente, o pipeline re-roda e re-baixa em vez de pular silenciosamente.
  - [`scripts/reprocessar_lotes_do_db.py`](scripts/reprocessar_lotes_do_db.py) — `--somente-laudo-pendente` agora também pega lotes com cache forte mas PDF ausente em disco. Filtro extraído pra helper testável `_filtrar_laudo_pendente`.
- **Cobertura:** 5 testes novos:
  - [`tests/test_orquestrador.py`](tests/test_orquestrador.py) — atualiza `test_lote_ja_avaliado_com_laudo_ok_nao_reavalia` (agora exige fake PDF on-disk pra short-circuit) + novo `test_lote_ja_avaliado_com_laudo_ok_mas_pdf_ausente_reavalia` (cenário CI: cache forte + PDF sumiu → pipeline re-roda).
  - [`tests/test_laudo_audit.py::TestFiltroRetryPdfAusente`](tests/test_laudo_audit.py) — 4 testes do filtro `_filtrar_laudo_pendente` (cache forte + PDF presente: nada; cache forte + PDF ausente: pendente; cache fraco: pendente; cache ausente: pendente).
- **Limitações conhecidas:**
  - URLs assinadas do Google Cloud Storage continuam expirando ~1h após geração; a planilha pode mostrar "Ver laudo" clicável que retorna 403 entre runs. Workaround atual: a célula degrada pra "PDF salvo (link expirado)" quando a URL ausente, e agora os PDFs persistidos em `state/db` garantem que esse fallback é honesto. Resolução plena exige servir os PDFs de fonte estável (workstream futuro — GitHub raw URL, S3 público, etc.).
  - Branch `state/db` cresce em volume (~180MB/600 lotes × 300KB cada). GitHub fará GC dos blobs não referenciados quando o tree mais recente não os listar; até lá, fica nos objects.
- **Follow-ups (não-bloqueantes, registrados no review arquitetural):**
  - **Rotação/GC de `state/db`** — cada run faz `commit-tree -p $PARENT`, então blobs antigos ficam acumulados na cadeia. Limite soft de 5GB do GitHub = ~6 meses de runway no ritmo atual. Considerar (a) `git push --force` periódico resetando histórico, ou (b) script de cleanup que reescreve sem parent quando atingir N commits. Não bloqueia agora.
  - **Re-assinatura de URL "Ver laudo" no export** — substituir as URLs assinadas expirantes (Google Cloud Storage, ~1h) por links estáveis quando o exporter rodar. Caminhos possíveis: GitHub raw URL apontando pra `state/db/laudos_pdfs/<lote>.pdf` (se repo público), ou re-assinar via service account na hora do export. Resolve a mitigação atual ("PDF salvo (link expirado)") deixando link sempre vivo.

### DD — Audit cross-checks operacionais + paridade laudo_analisado (2026-05-09) ✅
- **Branch:** `claude/sleepy-wright-Ocj2h`
- **Motivação:** Revisão preventiva diária. Simulação algébrica de 10 cenários (gold Polo Track, f_km saturado teto/piso, ESTRUTURAL conf 0.5, mediana inflada 1.20×FIPE, zona apertada, lote inviável, dias_giro otimista, km=None) confirmou identidades MAS expôs 6 falhas residuais — todas relacionadas ao audit não cobrir contradições cross-field específicas E não espelhar todas as supressões do display.
- **Bugs encontrados e corrigidos:**
  1. **`_check_zona_apertada` disparava em lance == preco_max (boundary inviável)** — `viavel = preco_max > lance` é estrito, mas zona apertada usava `<=` no max. Resultado: planilha mostrava "✗ Caro demais" e audit reportava "zona apertada" simultaneamente — sinais contraditórios pro operador. Fix: `<` estrito no `preco_max`.
  2. **Audit não espelhava `laudo_analisado=False`** — quando display oculta Lance Máximo / Lucro / ROI / Reforma / Tese (laudo extraído com confidence < 0.6 ou ausente), audit lia valores crus de `AvaliacaoLote`. Resultado: lotes "⚠ LAUDO NÃO CAPTURADO" disparavam falsos alarmes (ex.: reforma 0 + severidade ESTRUTURAL via `_laudo_sem_pdf` confidence 0.55). Fix: `_build_rows` calcula `laudo_analisado = laudo is not None and confidence ≥ 0.6` e `COLUMN_EXTRACTORS` retorna "—" pra Lance Máximo / Lucro / ROI / Reforma / Tese quando False. Cross-checks (`_check_zona_apertada`, `_check_lance_maximo_acima_fipe`, `_check_reforma_pesada`) também respeitam.
  3. **Audit não checava `motor_ok=False` em lote viável** — laudo indica motor não-original ou com problema, mas o lote passa como "✓ Viável" (precificador penaliza via fator_risco mas o teto pode ainda ficar acima do lance). Operador focado em ROI alto podia dar lance sem ver o sinal. Fix: novo `_check_motor_problema_em_viavel` (paridade `viavel + laudo_analisado`).
  4. **Audit não checava `severidade=ESTRUTURAL` em lote viável** — operador real (Reinaldo) descarta lotes estruturais categoricamente, mas com lance baixo + fator_risco no teto o sistema pode deixar passar. Fix: novo `_check_severidade_estrutural_em_viavel`.
  5. **Audit não checava `Mediana mercado >> FIPE`** — desde refactor FIPE-only a coluna é informativa, mas mediana >120% FIPE indica similares poluídos do AA (Tiggo 7 entre Tiggo 2). Operador olhando "mediana alta" pode achar que é "carro premium em alta" — falso. Fix: novo `_check_mediana_distante_fipe` (>1.20 ou <0.70 dispara informativo).
  6. **`_PRECO_GIRO_FIPE_RATIO_MAX = 1.10` apertado contra max natural 1.0925** — gap de só 0.75pp; qualquer aumento futuro de `_FATOR_MAX` em ajuste_km.py disparava falso positivo automático. Fix: 1.10 → 1.13 (margem ergonômica de ~3.5pp), comentário cruzando ref com `_FATOR_MAX`. Validators do `Lance Máximo / Reforma` também ganharam guarda `isinstance(v, (int, float))` pra tolerar "—" sem `TypeError`.
- **Análise das colunas (resposta direta às perguntas do usuário, 10 cenários):**
  - **Lance Máximo > FIPE?** Não em condições normais. Por construção FIPE-only, max teórico = `FIPE × 1.15 × 0.95 × 0.90 = 0.984×FIPE`. Cenário 2 (f_km saturado teto, custos zero) chega a 91.9% FIPE. Audit threshold 1.05 mantido como guard de regressão (matematicamente inviável bater FIPE no design atual).
  - **`preco_giro_fipe` muito diferente da FIPE?** Pode em ±9% naturalmente (`f_km` ∈ [0.75, 1.15] × 0.95). Cenário 2 produz 109.2% FIPE, cenário 3 produz 71.2% FIPE. Audit threshold antigo 1.10 era apertado — agora 1.13 com ~3.5pp de margem ergonômica.
  - **Linha-a-linha (10 cenários):** identidades algébricas confirmadas em todos. `lucro_alvo = preco_giro × score_roi / (1+score_roi)` exato; `score_efetivo ≤ score_intrinsic` sempre; `preco_alvo ≤ preco_max` sempre.
- **Cobertura:** 12 testes guard novos (3 motor problema + 2 estrutural viável + 3 mediana distante + 1 zona apertada boundary + 3 paridade laudo_analisado). **Total: 444/444 verde** (eram 432).
- **Limitações conhecidas:**
  - Threshold da mediana (>1.20 ou <0.70) é heurístico — calibrar quando Webmotors live conectar (workstream G).
  - `_check_motor_problema` e `_check_severidade_estrutural` usam `motor_ok` e `severidade` do `LaudoCache` (global). Em lotes onde o LLM categorizou errado, audit segue o LLM — não há sanity check sobre o laudo em si.
  - Threshold 1.13 para `preco_giro_fipe` ainda dispara se alguém aumentar `_FATOR_MAX` para 1.20+ (raro, mas possível com lotes super-baixa-quilometragem). Comentário cruzado deixa explícita a relação.
- **Updates persistentes:**
  - `CLAUDE.md` ganhou 4 entradas em "Padrões aprendidos": (a) audit deve cruzar `laudo_analisado` em TODOS os checks dependentes do display, não só em `viavel`; (b) cross-checks operacionais (motor_ok, severidade ESTRUTURAL) que o precificador NÃO modela explicitamente; (c) thresholds com baixa margem do max natural (gap < 1pp) viram bombas-relógio quando alguém ajusta o max — sempre deixar margem ergonômica de 2-3pp; (d) validators de `CHECKS` com `v <= 0` têm que ter guarda `isinstance` pra tolerar "—" da supressão.
  - `LESSONS.md` ganhou padrão **P5e** ("Paridade audit ↔ display: TODA dimensão de supressão, não só inviabilidade") + 4 entradas no apêndice.

### EE — Coluna "ROI alvo (%)" cru (sem anualizar) (2026-05-08) ✅
- **Branch:** `claude/roi-intrinsic-na-planilha` (PR #72 mergeado em `be2e587`)
- **Motivação:** Operador pediu ROI da operação no preço-alvo sem extrapolar pra ano. Anualização (`× 365 / dias_giro`) dependia de `dias_giro_estimado` calibrado, frequentemente otimista por categoria genérica (workstream DD pendente). ROI cru ("30% sobre o capital empatado") é mais legível pro operador.
- **Mudanças:**
  - `sheets.py`: HEADER `'ROI anualizado (%)'` → `'ROI alvo (%)'`. Valor = `score_roi × 100`. Glossário reescrito.
  - `audit.py`: threshold `500%` → `100%` (max teórico com cap margem 50% é `0.50/0.50 = 100%`). Após merge com #69, COLUMN_EXTRACTORS combina rename + paridade `laudo_analisado`.
  - `cli.py` (`top`): coluna `ROI%` → `ROI alvo`; coluna `ROI/ano%` removida pra paridade com a planilha.
- **Decisão arquitetural:** ranking interno PRESERVADO por `roi_anualizado` (no `key=` do `sorted`) mas COLUNA exibe `roi_alvo`. Sem isso, Polo Track 2024 (227d, 21% intrinsic) empataria com Gol 2014 (22d, 21%) — invertido. Divergência documentada em 3 lugares (docstring `_score_roi_efetivo`, comentário `_query`, glossário).
- **Cobertura:** 449/449 verde (subiu de 432 ao reconciliar com tests novos do #69 sobre paridade `laudo_analisado`).
- **Review arquitetural** (Plan agent): aprovou com 3 FUs não-bloqueantes (abaixo).
- **Follow-ups (não-bloqueantes):**
  - **Padrão "ranking key ≠ display column"** — registrar entrada em LESSONS.md formalizando que essa divergência é OK desde que UNIVERSAL (mesmo tratamento em CLI + planilha + audit) e DOCUMENTADA. Próximo refactor pode ressuscitar a divergência sem perceber.
  - **UX da CLI top**: operador perdeu a coluna `ROI/ano%` que ajudava a entender quando dois ROIs alvo iguais ranqueiam em ordens diferentes. Sugestão: ressuscitar atrás de flag `--anualizado` OU readicionar como coluna informativa secundária. A coluna `Dias` sozinha (já existe) ajuda mas não desambigua tudo.
  - **Threshold do audit acoplado ao cap do precificador**: `_MARGEM_TETO=0.50` em `precificador.py` define o max teórico do `score_roi=1.0` que o threshold `>100%` no audit assume. Se cap subir pra 0.60 sem audit acompanhar, audit vira mute silencioso. Sugestão: exportar constante `MAX_SCORE_ROI` de `precificador.py` e o audit consumir.

### CC — Coluna "Lucro (R$)" total absoluto (sem quebra mensal) (2026-05-08) ✅
- **Branch:** `claude/lucro-total-sem-quebra-mensal` (PR #65 mergeado em `d2906cc`)
- **Motivação:** Operador pediu pra ver o lucro TOTAL projetado da revenda em vez de normalizado por mês. Divisão por `dias_giro_estimado` confundia: defaults categóricos otimistas (HATCH NOVO=25d sem floor) faziam `Lucro/mês = Lucro absoluto`, levando operador a achar que entrava aquele valor todo mês.
- **Mudanças:**
  - `sheets.py`: header `"Lucro/mês (R$)"` → `"Lucro (R$)"`. Valor passa a ser `_lucro_absoluto_efetivo(av, lance_atual)` direto.
  - `audit.py`: `CHECKS` + `COLUMN_EXTRACTORS` atualizados.
  - `cli.py` (`top` command): coluna `R$/mês` → `Lucro` pra paridade com a planilha (princípio CLAUDE.md "mesma métrica em dois arquivos").
  - Glossário reescrito explicando que ROI anualizado lado a lado carrega o sinal de ritmo.
- **Cobertura:** 432/432 verde, sem regressão.
- **Review arquitetural** (Plan agent): aprovou com 1 nit não-bloqueante.
- **Follow-ups (não-bloqueantes):**
  - **`lucro_reais_por_mes` em `carros_sa/agents/calibracao_giro.py:211` virou dead code** — único caller restante removido. Manter por enquanto (CLAUDE.md/3 "código morto pré-existente: comenta, não apaga"), mas em varredura de housekeeping considerar deletar/renomear pra `_lucro_diario_legacy` e atualizar referências em CLAUDE.md/Padrões aprendidos + LESSONS.md/P6.

### BB — Refactor FIPE-only no precificador + coluna "Mediana mercado" (2026-05-08) ✅
- **Branch:** `claude/add-fipe-price-column-9mvUa`
- **Motivação:** Operador relatou 4 carros do screenshot (Airtrek 2008, Tiggo 2.0 2015, Ka 1.0 2020, Argo 1.0 2019) com `Lance Máximo > FIPE` na planilha. Simulação algébrica confirmou: 3 caps em série (n<5 no avaliador → 1.20×FIPE no precificador → 1.05×FIPE no audit) tentavam consertar similares poluídos do Auto Avaliar (Tiggo 7 vs Tiggo 2, Airtrek vs Outlander, Ka descontinuado vs seminovos europeus) — band-aids reativos sobre fonte de ruído estrutural. Como Webmotors live ainda não está conectado (workstream G), `webmotors_mediana` era na prática `FIPE × 0.97` com ruído — sistema já era FIPE-driven mascarado.
- **Decisão arquitetural:** simplificar pra **FIPE-only**:
  - `preco_giro_fipe = FIPE × f_km × 0.95` (era `webmotors_mediana × f_km` com cap 1.20×FIPE)
  - `preco_giro_aa = None` sempre (campo de referência apenas, não no cálculo)
  - `webmotors_mediana` continua persistido em `Avaliacao` pra display
  - Removidos os 2 caps no precificador (`_PRECO_GIRO_FIPE_TETO_PCT_FIPE` e o cap n<5 redundância) — fórmula nova torna `preco_max > FIPE` matematicamente inviável (max teórico = `FIPE × 1.15 × 0.95 × 0.90 ≈ 0.98 × FIPE`)
  - Cap n<5 no `avaliador_mercado.py` mantido (limpa o display da mediana)
- **Display:** nova coluna `Mediana mercado (R$)` na planilha entre FIPE e Lucro — operador vê FIPE × Mediana × Lance Atual lado a lado pra contextualizar a decisão da máquina. Glossário atualizado.
- **Trade-off conhecido:** modelos premium (Civic/Corolla) que de fato vendem ~108% FIPE em alta perdem o uplift que `webmotors_mediana` daria. Calibração mais conservadora em troca de eliminação categórica do bug "Lance Máximo > FIPE". Quando Webmotors live conectar (workstream G), reativar mediana com cap mais apertado.
- **Cobertura:** 4 testes guard novos (`test_lance_maximo_nunca_excede_fipe_em_uberlandia_sem_dano` parametrizado nos 4 carros do screenshot) + atualização de 7 testes que assumiam fórmula antiga + nova suite `test_preco_giro_fipe_independe_de_mediana_inflada` e `test_preco_giro_fipe_eh_fipe_vezes_fkm_vezes_095`. **Total: 432/432 verde** (eram 429).
- **Limitações conhecidas:**
  - `auto_avaliar_ref` em `SinalMercado` continua exposto no schema mas não usado em nenhum cálculo. Considerado contrato (models.py) — mantido pra future re-ativação.
  - Coluna "Mediana mercado" mostra `FIPE × 0.97` (fallback) na maioria das linhas até Webmotors live ligar. Operador precisa contextualizar.
- **Follow-ups:**
  - Quando workstream G ligar Webmotors live, redesenhar a ponderação `FIPE × β + mediana × (1−β)` com `β` variando por `n_anuncios_competidores` (sample size).
  - Considerar deprecar `preco_giro_aa` no schema (atualmente sempre None) — exige coordenação multi-sessão (contrato em models.py).

### AA — Audit cross-checks independentes + reforma pesada (2026-05-08) ✅
- **Branch:** `claude/sleepy-wright-0eJhG`
- **Motivação:** Revisão preventiva diária. Simulação algébrica de 6 cenários (gold Polo Track, km saturando teto/piso, ESTRUTURAL conf 0.5, mediana inflada 1.20×FIPE, zona apertada) expôs duas falhas residuais.
- **Bugs encontrados e corrigidos:**
  1. **Audit `Lance Máximo (R$)` usava if/elif encadeado** — só o primeiro motivo aparecia. Cenário patológico: lote com `lance_atual > preco_alvo` (zona apertada, yellow) + `preco_max > FIPE × 1.05` (red flag, indica dado quebrado) disparava SÓ o yellow — operador via amarelo e ignorava enquanto se preparava pra dar lance acima da FIPE. Refatorado em 3 funções independentes em `ALL_CHECKS` (`_check_zona_apertada`, `_check_lance_maximo_acima_fipe`, `_check_preco_alvo_gt_preco_max` — esta já existia). Múltiplos sintomas na mesma linha emergem juntos. Padrão registrado em LESSONS.md/P5d.
  2. **Audit não tinha check pra reforma pesada** — `reforma_estimada > 30% × preco_giro` em lote viável é sinal de lote economicamente questionável (capital empatado em reforma alto vs. revenda; surpresa na oficina pode tornar o investimento inviável post-hoc). Mesmo com margem aprovada pelo precificador, é red flag operacional. Adicionado `_check_reforma_pesada` em `ALL_CHECKS`. Suprimido em lotes inviáveis (paridade com display).
- **Análise das colunas (resposta direta às perguntas do usuário, 6 cenários):**
  - **Lance Máximo > FIPE?** Não em condições normais. Cenário 5 simulado (mediana=1.20×FIPE, f_km=1.0) produz preco_max em 101.9% FIPE — dentro do cap defensivo do precificador (1.20) MAS audit agora avisa explicitamente como red flag separado.
  - **`preco_giro_fipe` muito diferente de FIPE?** Cenário 2 (km baixíssima, f_km=1.15) produz 111.7% FIPE → audit dispara `> FIPE × 1.10`. Cenário 3 (km alta, f_km=0.75) produz 73.3% FIPE → ok.
  - **Linha-a-linha (6 cenários):** identidades algébricas confirmadas. Lucro absoluto exato bate com helper `_lucro_absoluto_no_alvo` em todos os 6. `_score_roi_efetivo` cai pra negativo em lote inviável (cenário 4 ESTRUTURAL: -6%) e display suprime corretamente.
- **Cobertura:** 6 testes novos (`TestAuditChecksIndependentes` com 3 + `TestAuditReformaPesada` com 3). **Total: 429/429 verde** (eram 423).
- **Limitações conhecidas:**
  - Threshold 30% pra reforma pesada é heurístico; calibrar com histórico real Arrematado quando tiver ≥10 vendas com `gastos_reforma_real`.
  - Audit ainda NÃO espelha `laudo_analisado=False` (confidence < 0.6) — display oculta Reforma/Lance Máximo nesses lotes mas audit lê valores persistidos. Paridade incompleta. Follow-up registrado.
- **Follow-ups:**
  - Espelhar `laudo_analisado` no `audit._build_rows` — paridade total com sheets (toda supressão de display deve refletir no audit, padrão LESSONS.md/P5c).
  - Avaliar threshold reforma pesada com histórico real (calibrar com Arrematado).

### Z — Hardening operacional + refactor audit (2026-05-07) ✅
- **PRs:** #55, #56, #57, #58, #59, #60 (sequência consolidada num único dia)
- **Motivação:** revisão preventiva do workflow CI + caça a silent failures (RC3 do LESSONS) após primeiro run real chegar a 3h+ e quebrar no `Persiste DB`.
- **Mudanças:**
  1. **`#55 — CI artifacts on failure`** ([`.github/workflows/triagem.yml`](.github/workflows/triagem.yml)): upload do `carros_sa.db` parcial como artifact (7 dias) quando o pipeline quebra. Cookies e PDFs ficam de fora deliberadamente.
  2. **`#56 — refactor audit`** ([`carros_sa/tools/audit.py`](carros_sa/tools/audit.py)): unifica `CHECKS`/`CROSS_CHECKS`/`_DERIVED_CHECKS` num único contrato `(row) -> List[CheckResult]`. `audit()` colapsa 3 loops em 1. Endereça FU#3 da revisão arquitetural do PR #52.
  3. **`#57, #59 — fail-loud em Sheets export`** ([`scripts/triagem_diaria.py`](scripts/triagem_diaria.py), [`carros_sa/cli.py`](carros_sa/cli.py)): `except Exception: print(); pass` engolia falha do export — operador abria planilha desatualizada sem saber. Agora `raise typer.Exit(1)` em ambos os caminhos.
  4. **`#58 — audit gate no workflow`** ([`.github/workflows/triagem.yml`](.github/workflows/triagem.yml)): paridade com o `setup_cron.sh` local — adiciona `auditar_laudos --strict` como 4ª chamada no pipeline.
  5. **`#60 — log cards descartados no orquestrador`** ([`carros_sa/orquestrador.py`](carros_sa/orquestrador.py)): loop de upsert de cards do scraping tinha `except Exception: pass`. Agora imprime stderr por card descartado + sumário.
- **Cobertura:** **423 passed, 2 skipped** (eram 416 antes do batch).
- **Follow-ups (não bloqueantes, deixados pra próximo ciclo):**
  - `_PRECO_GIRO_FIPE_TETO_PCT_FIPE` em `precificador.py` declarada dentro da função em vez do topo (review do #54 considera intencional, não vale).
  - Adicionar `n_cards_dropped` em `ResultadoOrquestracao` pra ficar visível na CLI table do `triagem_diaria.py` (review do #60).
  - Migrar prints stderr do orquestrador pra `logging` estruturado quando o projeto tiver pipeline de logs (review do #60).
  - Mover `import sys` pro topo de `orquestrador.py` (review do #60, estilístico).
  - Extrair bloco "Sheets export + fail-loud" num helper compartilhado entre `triagem_diaria.py` e `cli.py::triagem` pra eliminar drift (review do #59).
  - Considerar audit cross-check "delta entre `_score_roi_efetivo` e ROI exato com taxas" pra cobrir leilão judicial (irrelevante hoje em AA com taxa fixa).

### Y — Revisão preventiva pós-cluster: paridade audit↔display + cap defensivo (2026-05-07) ✅
- **Branch:** `claude/sleepy-wright-1UNO3`
- **Motivação:** Pedido de revisão geral pra encontrar bugs/oportunidades. Simulação algébrica em 10 cenários (gold real Polo Track, Fiesta ESTRUTURAL, zona apertada, mediana inflada, lote inviável, etc.) expôs 3 falhas residuais que sobreviveram à consolidação do cluster.
- **Bugs encontrados e corrigidos:**
  1. **Audit reportava "ROI anualizado negativo" em lotes INVIÁVEIS** — `SheetsExporter._write_sheet:422-429` substitui ROI/Lucro/Tese por `"—"` quando `viavel=False` (caso "comprar pelo alvo é fantasioso se lance > preco_max"). Audit `COLUMN_EXTRACTORS` retornava o valor numérico cru, então `_score_roi_efetivo` com `capital_ef > preco_giro` (Fiesta ESTRUTURAL real: ROI -53.9%) disparava o validator "ROI negativo" em lotes que o operador NUNCA viu. Falso alarme operacional. Fix: extractors de "ROI anualizado (%)", "Lucro/mês (R$)" e "Tese" no audit retornam `"—"` quando `r["viavel"] is False` — espelhando o display.
  2. **Cap defensivo no precificador `preco_giro_fipe ≤ FIPE × 1.20`** — Cap mediana similares (`FIPE × 1.20` quando n<5) + `f_km` saturado (1.15) podiam multiplicar pra `1.38 × FIPE` no `preco_giro_fipe`. Cenário simulado (mediana 168% FIPE, f_km neutro): preco_max ia a 111.4% FIPE — audit alertava `Lance Máximo > FIPE × 1.05`, mas só após o estrago já estar persistido. Fix: cap final em 1.20×FIPE no precificador (`_PRECO_GIRO_FIPE_TETO_PCT_FIPE`). Preserva caso legítimo Civic/Corolla ~110% em alta sem permitir combinação patológica em série. Audit threshold 1.10 continua avisando "dado fraco" como sinal cedo.
  3. **`_score_roi_efetivo` quebrava com `preco_alvo=None`** — `AvaliacaoLote.preco_alvo` é non-nullable hoje, mas migrações antigas podem ter deixado NULL. `lance_atual - None` levantava `TypeError` silencioso que quebrava a planilha inteira (helper é chamado por linha). Fix: `alvo = av.preco_alvo or 0` antes de subtrair.
- **Análise das colunas (resposta direta às perguntas do usuário):**
  - **Lance Máximo > FIPE?** Não em condições normais. Antes do cap defensivo, podia chegar a 111% FIPE em adversarial; agora limitado a ~97% mesmo nos piores casos simulados. Audit já avisa em 1.05.
  - **Giro FIPE muito diferente da FIPE?** Pode em até 1.20 (cap defensivo); audit avisa entre 1.10-1.20 como dado fraco. Acima de 1.20 é matematicamente impossível agora.
  - **Linha-a-linha (10 cenários):** identidades algébricas confirmadas (`preco_alvo ≤ preco_max ≤ preco_giro × (1−margem_min)`). Lote estrutural com confidence 0.55 mascarado pela supressão `laudo_analisado=False` (vira "⚠ LAUDO NÃO CAPTURADO"). Lote inviável agora produz `—` em ROI/Lucro/Tese tanto na planilha QUANTO na auditoria — paridade garantida.
- **Cobertura:** 4 testes novos: `test_inviavel_nao_dispara_roi_negativo`, `test_viavel_com_roi_negativo_continua_disparando`, `test_preco_alvo_none_nao_quebra`, `test_preco_giro_fipe_capado_em_120pct_fipe` + `test_preco_giro_fipe_caso_normal_nao_e_afetado_pelo_cap`. **Total: 416/416 verde** (eram 411).
- **Limitações conhecidas:**
  - Cap defensivo do precificador é dura (1.20×FIPE) e não tem override por modelo. Se um modelo de fato vender 130% FIPE (raríssimo), seria capado. Em produção real, calibrar via `auto_avaliar_ref` se aparecer.
  - Audit pode ainda ter outras "substituições silenciosas" do display sem paridade — vale auditar `_write_sheet` de tempos em tempos. Esta sessão cobriu as 3 do `viavel=False`.
- **Updates persistentes:**
  - `CLAUDE.md` ganhou 3 entradas em "Padrões aprendidos": (a) audit espelha SUBSTITUIÇÃO, não só filtro; (b) cap defensivo `preco_giro_fipe ≤ FIPE × 1.20`; (c) `_score_roi_efetivo` defensivo contra `preco_alvo=None`.
  - `LESSONS.md` ganhou padrão **P5c** ("Paridade audit ↔ display: filtragem **e** substituição") + 3 entradas no apêndice de fixes.

### X — Consolidação do cluster precificador (2026-05-07) ✅
- **Branch:** `claude/consolidate-precificador-cluster`
- **Motivação:** 4 PRs paralelos (#38, #43, #46, #49) tocando precificador/sheets/audit com sobreposição forte. Em vez de mergear sequencialmente (cada um conflitando com o anterior), foi feita uma consolidação cirúrgica: cherry-pick em ordem cronológica + resolução manual de conflitos pra extrair o melhor de cada.
- **11 fixes integrados:**
  1. **Cap `_MARGEM_TETO=0.50` no precificador** — evita score_roi explorar em lotes péssimos (margem teórica 90%+ em Uberlândia).
  2. **Cap mediana similares Auto Avaliar (`FIPE × 1.20` quando n<5)** em `agents/avaliador_mercado.py` — outlier categórico (Tiggo 7 entre Tiggo 2) podia gerar lance ACIMA da FIPE.
  3. **DRY `_categoria_de_modelo` no frete** — `_calcular_frete(lote, empresa, categoria=None)` aceita categoria pré-resolvida; fallback usa a tabela canônica do calibrador. Toro virava OUTRO no frete mas PICAPE no giro.
  4. **Floor `dias_giro` 30d → 60d** em `lucro_reais_por_mes` e `roi_anualizado` (`_FLOOR_DIAS_GIRO_DISPLAY=60`) — defaults categóricos otimistas (HATCH NOVO=25d) faziam `Lucro/mês = Lucro absoluto`.
  5. **Threshold ROI 1000% → 500%** em `audit.CHECKS` — calibrado contra benchmark operacional (Reinaldo 21 carros = 60-75% ano).
  6. **Audit espelha exporter (filtra `fim_em is None`)** — sem isso reportava violações em lotes invisíveis na UI.
  7. **CROSS_CHECKS** em audit: `preco_giro > FIPE × 1.10` (f_km saturando) + `preco_alvo > preco_max` (sanity).
  8. **`_score_roi_efetivo` + `_lucro_absoluto_efetivo`** em `sheets.py` — ROI honesto quando `lance_atual > preco_alvo` (zona apertada). Caso real Polo: 273% → 122%.
  9. **Supressão de Lucro/mês + ROI + Tese em lotes "✗ Caro demais"** em `_write_sheet` — comprar pelo preço-alvo é cenário fantasioso quando lance_atual > preco_max.
  10. **`_DERIVED_CHECKS` (margem no teto)** — flag agregado quando margem ≥ 49% (sintoma forte de fatores explodidos).
  11. **Defesa contra None** em `_lucro_absoluto_no_alvo` (registros antigos com NULL).
- **Cobertura:** **411 passed, 2 skipped** (eram 380 antes da consolidação; +31 testes novos do cluster).
- **PRs originais fechados:** #38, #43, #46, #49 (link pra este consolidado).
- **Limitações conhecidas:**
  - Cap mediana ativa só `n<5`. Se AA enviar 5+ similares todos de outro modelo (extremo), cap não atua. Mitigação futura: filtrar por similaridade de string com modelo do lote.
  - `_score_roi_efetivo` ignora `taxa_leilao_pct × delta_lance` no capital incremental (≈zero em AA com taxa fixa; até 8% em judicial).
  - Threshold ROI 500% ainda é frouxo pro benchmark real (60-75%/ano). Solução de raiz: elevar priors `dias_giro` após calibração com Arrematado.
- **Follow-ups da revisão arquitetural (não bloqueiam merge):**
  - Convergir CHECKS (Dict) / CROSS_CHECKS (List) / `_DERIVED_CHECKS` (List) num único modelo de registry pra reduzir custo cognitivo no `audit.py`.
  - Audit cross-check "delta entre `_score_roi_efetivo` e ROI exato com taxas" pra cobrir leilão judicial (taxa pct até 8%) — irrelevante hoje (AA tem taxa fixa), reabrir quando o sistema rodar fora do AA.
  - Atualizar tag `(consolidação cluster precificador 2026-05-07)` no `LESSONS.md` apêndice pro hash final pós-merge (depois do squash).

### X — Revisão de coerência entre linhas + fix TZ (2026-05-02) ✅
- **Branch:** `claude/sleepy-wright-J7Brw`
- **Motivação:** Usuário pediu revisão autônoma "da lógica entre os valores das colunas" + dúvidas se Lance Máximo > FIPE ou Giro FIPE muito diferente da FIPE faziam sentido.
- **Bugs encontrados e corrigidos:**
  1. **Mistura naive UTC ↔ naive LOCAL em `Lote.fim_em`.** [`carros_sa/scraping/parsers.py:150`](carros_sa/scraping/parsers.py) computava `fim_em = datetime.utcnow() + delta_timer` (default), mas [`sheets.py`](carros_sa/tools/sheets.py), [`audit.py`](carros_sa/tools/audit.py), [`laudo_audit.py`](carros_sa/tools/laudo_audit.py), [`scraper_autoavaliar.py`](carros_sa/scraping/scraper_autoavaliar.py) e o filtro SQL `Lote.fim_em > now()` comparavam contra `datetime.now()` (LOCAL). Em Brasil (UTC-3) sobravam 3h de grace silenciosa onde lotes encerrados apareciam ativos na planilha + `strftime` exibia horário 3h adiantado. Conecta diretamente ao sintoma do workstream N ("usuário clicou no top 1 e o lote já tinha sido arrematado") que só foi resolvido parcialmente via badge ARREMATADO. Fix: parser default → `datetime.now()` pra colar com o resto da stack. Guard test em `tests/test_parsers.py::test_parse_card_default_agora_e_local_naive_nao_utc` patcheia `now`/`utcnow` com 3h de offset pra travar regressão em runner UTC.
  2. **Auditoria de colunas era cega a contradições cross-field.** [`carros_sa/tools/audit.py`](carros_sa/tools/audit.py) validava cada célula em isolamento — "✓ Viável + reforma R$ 0" num lote ESTRUTURAL passava (cada campo individual válido). Adicionada coerência:
     - **Reforma R$ 0 com severidade ∈ {média, grave, estrutural}** vira violação. Captura LLM mal-interpretando laudo ou fallback errado.
     - **`preco_giro_fipe` divergente >25% de FIPE** vira violação. Por construção `f_km` contribui no máx ±15%; gap >25% sinaliza `_extrai_precos_similares` poluído (regex pegando R$ de outras seções), cache FIPE stale, ou marca/modelo errado na consulta.
- **Análise da pergunta original (Lance Máximo > FIPE? Giro FIPE ≠ FIPE?):**
  - Por construção `preco_max ≤ preco_giro × (1 − margem_min)` e `preco_giro = webmotors_mediana × f_km` com `f_km ∈ [0.75, 1.15]`. Ceiling teórico de `preco_max` ≈ FIPE × 1.05 (cenário extremo: f_km saturado + similares no topo). Audit já flagava `Lance Máximo > FIPE × 1.05`; agora cobre o vetor de entrada via `preco_giro_fipe vs FIPE`.
  - "Giro FIPE muito diferente da FIPE" é o sintoma de dados ruins entrando — não é esperado em condições normais. A nova invariante captura.
- **Cobertura:** 5 testes novos (1 em parsers, 4 em audit `TestAuditCoerenciaRow`). Suite total: **377 verde** (era 372).
- **Limitações conhecidas:**
  - O fix de TZ só corrige novos `fim_em` — entradas pré-fix com 3h de offset continuam até o lote sair do horizonte (timer expira). Sem migração porque o churn natural (~7d) resolve sozinho e migração retroativa em UTC naive sem TZ-info confiável é frágil.
  - Threshold de divergência `preco_giro_fipe vs FIPE` está em ±25%. Pode disparar falso positivo em cenários legítimos onde `auto_avaliar_ref` vem MUITO mais baixo (atacado real abaixo da FIPE retail) — mas nesse caso `preco_giro` consolidado usa o mínimo, então `preco_giro_fipe` em si fica perto da FIPE (não é o `preco_giro`). Se aparecer falso positivo em produção, calibrar threshold ou trocar pra `preco_giro_aa`.

### X — Revisão econômica + alinhamento de ranking CLI ↔ Planilha (2026-05-05) ✅
- **Branch:** `claude/sleepy-wright-C2HzV`
- **Motivação:** Usuário pediu nova revisão das colunas (FIPE × Lance Máximo × Giro FIPE × ROI × Lucro/mês). Simulação algébrica em 5 cenários (Polo Track real, Polo+AA_ref, Fiesta ESTRUTURAL, Polo zero-bala km saturado, Polo confidence 0.5) confirmou identidades, mas expôs uma divergência operacional não pega antes.
- **Bug encontrado:**
  1. **Ranking divergente CLI vs Planilha.** [`carros-sa top`](carros_sa/cli.py:137) ranqueava por **ROI anualizado desc**; [`SheetsExporter`](carros_sa/tools/sheets.py:142) ranqueava por **folga absoluta `preco_max - lance_atual` desc**. Na prática: lote barato com folga grande mas ROI baixo subia na planilha; lote lucrativo com lance perto do teto descia. Operador via duas ordens conflitantes da mesma fonte. CLI estava certo (segue ROADMAP/CLAUDE: "score_roi calibrado por risco/liquidez é a métrica de ranking"). Planilha agora bate com a CLI: `(laudo_analisado, viavel, -roi_anualizado)`. Audit também alinhado.
- **Análise da relação entre colunas (resposta direta às perguntas do usuário):**
  - **`Lance Máximo > FIPE`?** Não em condições normais. `preco_max ≤ preco_giro × (1−margem_min)` por construção. Pra estourar FIPE precisaria f_km no teto 1.15 + custos zero — caso teórico, max observado nos 5 cenários: 95.36% da FIPE. Audit já alerta `> FIPE × 1.05`.
  - **`preco_giro_fipe > FIPE`?** Pode em até ~12% quando `f_km > 1` (lote km baixa). Faz sentido econômico: carro mais bem cuidado vale mais que mediana. Nome enganoso (é `mediana × f_km`, não `min(FIPE×0.95, p25)`) — anotado em CLAUDE.md, contrato em models.py preserva.
  - **`score_roi` cresce com fator de risco**: lote pior conhecido (confidence 0.5) sai com ROI MAIOR que lote bem auditado. Design intencional (mais incerteza → exige mais margem). Mascarado na planilha (confidence < 0.6 vira "—") mas aparece em logs/audit.
- **Cobertura:** +1 teste em [`tests/test_exportar_sheets.py`](tests/test_exportar_sheets.py): `test_exportar_ranking_por_roi_anualizado_entre_viaveis` (lote barato com ROI baixo deve vir DEPOIS de lote lucrativo com ROI alto). Suite total: **373/375 verde** (2 skips esperados).
- **CLAUDE.md** acrescentado com 2 entradas: "Mesma métrica em dois arquivos = duas métricas diferentes" + "Análise da relação entre colunas". **LESSONS.md** ganhou padrão P5b ("ranking duplicado") com antídoto operacional (importar de módulo neutro + paridade explícita entre views).

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

### G — Webmotors live + tracking longitudinal ✅ (G.1 + G.2 infra, 2026-05-12)
- **Branch:** `claude/investigate-median-source-AKH3H`
- **Arquivos novos:**
  - [`carros_sa/tools/webmotors_live.py`](carros_sa/tools/webmotors_live.py) — fetch async com Playwright + `playwright-stealth`, retry exponencial, detecção de Cloudflare challenge, `StealthBrowser` context manager.
  - [`carros_sa/tools/webmotors_cache.py`](carros_sa/tools/webmotors_cache.py) — wrapper sobre tabela `anuncio_webmotors` (TTL 24h, match de ano em faixa, `marcar_anuncios_sumidos` pra G.2, `obter_estatisticas_cacheadas` integrado).
  - [`tests/test_webmotors_live.py`](tests/test_webmotors_live.py) — 8 testes, mocka `page` Playwright, exercita parse, Cloudflare, retry/backoff.
  - [`tests/test_webmotors_cache.py`](tests/test_webmotors_cache.py) — 9 testes, gold pra upsert/TTL/match-ano/sumiu.
- **Arquivos modificados:**
  - [`carros_sa/models.py`](carros_sa/models.py) — `AvaliacaoLote.webmotors_n_anuncios: Optional[int] = None` (mudança coordenada, aprovada explicitamente; migração leve em [`db.py`](carros_sa/db.py)).
  - [`carros_sa/agents/avaliador_mercado.py`](carros_sa/agents/avaliador_mercado.py) — fonte de mediana mudou de `similares_precos` (AA) pra `webmotors_anuncios` (cache Webmotors via `session`). Cap n<5 → FIPE×1.20 **removido** (band-aid pra AA, não mais necessário). Sem amostra → `mediana = fipe` placeholder, `n=0`.
  - [`carros_sa/orquestrador.py`](carros_sa/orquestrador.py) — para de passar `similares_precos`; propaga `mercado.n_anuncios_competidores` → `AvaliacaoLote.webmotors_n_anuncios`.
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) + [`carros_sa/tools/audit.py`](carros_sa/tools/audit.py) — display da coluna "Mediana mercado (R$)" mostra "—" quando `webmotors_n_anuncios < 1` (paridade explícita). Audit `_check_mediana_distante_fipe` ignora quando `n=0`.
  - [`carros_sa/cli.py`](carros_sa/cli.py) — novo subcommand `webmotors-coletar` pra cron noturno.

**Estratégia anti-bot acordada (CLAUDE.md red flag respeitada):**
- Cron noturno único, 60s/req (rate-limit configurável, rejeita <30s), batch 3-4h da manhã.
- Playwright + stealth, 1 contexto sequencial sem paralelismo.
- Cache 24h: skip `(marca,modelo,ano)` já coletado nas últimas 24h.
- Retry exponencial em 403/Cloudflare/zero cards (30s → 60s → 120s).
- Alerta de fail-rate >30% sugere pausar e investigar.

**Workflow operacional:**
1. Validar manualmente uma vez: `carros-sa webmotors-coletar --marca Ford --modelo Fiesta --ano 2013 --debug`
2. Confirmar URL/seletor JS no site real (Webmotors muda CSS-Modules com hash volátil; nosso seletor ancora em `a[href*="/comprar/"]` + innerText).
3. Agendar cron 3h da manhã rodando `carros-sa webmotors-coletar` sem args (itera lotes ativos sem cache fresh).

**Mudanças no design da mediana:**
- Antes: `webmotors_mediana = mediana(similares_AA)` ou `FIPE × 0.97` fallback. Cap n<5 em FIPE×1.20 mascarava similares poluídos.
- Depois: `webmotors_mediana = mediana(Webmotors cache)` ou `FIPE` (placeholder neutro quando cache vazio). Sem cap — Webmotors tem amostra precisa por (marca,modelo,ano) sem mistura categórica.
- **Display honesto:** coluna "Mediana mercado" vira "—" quando `n=0`, evitando passar impressão de "sinal real" pra placeholder. Quando workstream G estabilizar (≥1 semana de cron rodando), coluna passa a ter dado de mercado de revenda real.

**Limitações conhecidas:**
- `_build_search_url` usa template configurável via `WEBMOTORS_SEARCH_URL_TEMPLATE` env var — primeira validação manual pode requerer ajuste fino do template.
- Precificador ainda é FIPE-only. **NÃO** redesenhamos como `FIPE × β + mediana × (1−β)` — isso depende de ≥1-2 semanas de cron acumulando amostra suficiente pra calibrar β por `n_anuncios_competidores`. Fica como **G.3** abaixo.
- `_fetch_playwright` síncrono em `webmotors.py` continua bloqueado (NotImplementedError) — entry point é só o CLI.
- Critério "≥5 amostras/modelo, captura >70%/semana" ainda não validado em produção — só após operador rodar cron por 1 semana e revisar.

**Próximas iterações (G.3 e G.2 ativos):**
- **G.3** (futuro): redesenhar precificador como `preco_giro = FIPE × β + mediana × (1−β)` com `β` variando por sample size. Bloqueia em ≥1 semana de coleta real estável.
- **G.2** (infra pronta, ativação operacional): `marcar_anuncios_sumidos` já popula `sumiu_em` no cron noturno; basta criar relatório DuckDB `vendido_em (proxy) - primeiro_visto` pra calibrar `dias_giro_estimado` (workstream H).

**Follow-ups (não-bloqueantes, da auto-review arquitetural do PR #90):**
- **G.1a** — Validação manual do template de URL `_build_search_url`. Rodar `carros-sa webmotors-coletar --marca Ford --modelo Fiesta --ano 2013 --debug` em ambiente real antes de agendar cron. Se layout mudou, ajustar `WEBMOTORS_SEARCH_URL_TEMPLATE` ou seletor JS. Documentar resultado em `data/scrapes/`.
- **G.1b** — Smoke test do subcommand `webmotors-coletar` (CliRunner com `StealthBrowser` mockado). Hoje só exercitado manualmente. Casos: alvos vazios → "nenhum precisa coletar"; `--rate-limit <30` → exit 2.
- **G.1c** — Range de ano `[ano-1, ano, ano+1]` em `obter_anuncios_cacheados` pode misturar modelos em transição de geração (ex.: novo Onix 2020 vs antigo 2019). Mitigação futura: persistir `ano_fab` E `ano_mod` separados em `AnuncioWebmotors` e fazer match exato em `ano_mod` com fallback pra range. Não urgente — ruído residual absorvido pela mediana.
- **G.1d** — Outlier defensivo opcional pós-fetch: clip percentil 5-95 dentro de `webmotors_cache.obter_anuncios_cacheados` quando `n>=10`. Substitui o cap n<5 removido por algo estatisticamente sadio. `_check_mediana_distante_fipe` continua avisando.
- **G.1e** — Configuração de cron (crontab/systemd timer/launchd) pra rodar `carros-sa webmotors-coletar` 3-4h da manhã todo dia. Default top 10 do ranking ROI anualizado.

### G.3 — Reativar mediana no precificador 🕐 bloqueado (≥1 semana de dado real do G)
- Precificador hoje: `preco_giro = FIPE × f_km × 0.95` (FIPE-only desde 2026-05-08).
- Próximo: `preco_giro = (FIPE × β + mediana × (1−β)) × f_km × 0.95` com `β = f(n_anuncios)`.
  - `β = 1.0` quando `n=0` (cai pra FIPE puro, mantém comportamento atual).
  - `β = 0.3` quando `n ≥ 10` (confia em 70% no mercado real).
  - Curva intermediária (sigmoid?) entre os dois.
- **Bloqueia em:** ≥1 semana de cron rodando + auditoria de qualidade da amostra (lotes da Uberlândia precisam ter pelo menos n=5 médio).
- **Risco:** mexer na fórmula central, exige simulação canônica de 10 cenários (CLAUDE.md/LESSONS.md). Não fazer junto com o G.1.

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

#### Pendência identificada por Caio (2026-04-16): granularidade da calibração — promovida pra **DD** (ver abaixo)
- **Limitações:** calibração de qualidade modesta com 32 vendas (~poucas por categoria). Workstream H futuro vai melhorar com séries temporais e overlap real entre AA + Arrematado.

### DD — Calibração granular de `dias_giro` 🔓 destravado
**Promovido em 2026-05-08 a partir da pendência inline do Bloco C (linha histórica 451)** — fica visível como item de trabalho nomeado em vez de prosa enterrada num "####".

**Problema:** A categoria genérica é grosseira demais. O hatch 128d agrupa Polo Track 2024 (227d, preço-cheio) com Onix Joy 1.0 2018 (278d) e Gol 2014 (22d) — perfis muito diferentes. Defaults categóricos otimistas eram a causa do bug "Lucro/mês = Lucro absoluto" que motivou o workstream CC (mascarado por floor 60d, não resolvido na raiz).

**Caminhos pra refinar (em ordem de simplicidade):**
1. **Sub-bucket por idade** (`hatch_novo` ≤3 anos / `hatch_velho` >3 anos). Implementação: ~1h. Custo: precisa rodar mais lotes pra ter ≥3 amostras por sub-bucket.
2. **Calibração por modelo** quando há ≥2 amostras do MESMO modelo (cai pra categoria quando não). Mais granular, mas requer histórico denso.
3. **Filtrar outliers**: usar mediana em vez de média; ou peso decrescente por idade da amostra.
4. **Distinguir "demanda intrínseca" de "política de preço"**: o Polo demorou 227d porque vendeu na FIPE cheia. Dividir `dias_giro` em `dias_se_FIPE` vs `dias_se_FIPE-5%` exigiria histórico com info de quanto desconto deu (não temos hoje).

**Critério de aceite:** `dias_giro_estimado` calibrado em granularidade que faz Polo Track 2024 e Gol 2014 saírem em buckets distintos. Cobertura: novo teste em `tests/test_calibracao_giro.py` validando `dias_giro` divergente entre `(hatch, novo)` e `(hatch, velho)` com fixture real.

**Pré-requisito:** ≥3 amostras por sub-bucket (cobrir com Bloco C importer + dados Reinaldo + AA real).

---

## Convenções de coordenação

### Quem mexe em `carros_sa/models.py`?
**Ninguém sem discussão.** Se workstream precisa de campo novo: (a) comenta aqui no ROADMAP na seção "Pendências de schema", (b) abre issue/mensagem, (c) espera merge coordenado antes de começar.

Pendências de schema (dead-fields aguardando reativação OU deprecação coordenada):
- **`Avaliacao.preco_giro_aa` / `AvaliacaoLote.preco_giro_aa`** — sempre `None` desde o refactor FIPE-only (BB, 2026-05-08). Mantido no schema pra futura reativação quando workstream G ligar Webmotors live com ponderação `FIPE × β + mediana × (1−β)`. Se workstream G não ressuscitar a mediana no precificador, deprecar formalmente o campo.
- **`SinalMercado.auto_avaliar_ref`** — coletado pelo scraper mas não consumido em nenhum cálculo desde BB. Idem `preco_giro_aa`: sobrevive aguardando G. Considerar usar como sinal secundário no futuro (preço atacado real da plataforma).
- **`SinalMercado.webmotors_p25`** — dead read antigo (versões anteriores do precificador usavam `min(FIPE×0.95, p25)`). Continua exposto mas nunca é lido. Candidato a deprecar quando a estratégia de Webmotors live for definida.

Pendências de cleanup (não-schema):
- **`carros_sa.agents.calibracao_giro.lucro_reais_por_mes`** — dead code desde CC (2026-05-08). Mantido por CLAUDE.md/3 ("código morto pré-existente: comenta, não apaga"). Em varredura de housekeeping considerar deletar/renomear pra `_lucro_diario_legacy` e atualizar referências em CLAUDE.md/Padrões aprendidos + LESSONS.md/P6.

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

### O.1.1 — Cabeamento do frontend "Racional Reforma" + indicador de cobertura (2026-05-08) ✅
- **Branch:** `claude/investigate-missing-report-Wvzuh`
- **Motivação:** O.1 marcou ✅ em 2026-04-17 mas o **frontend nunca foi cabeado** — `AvaliacaoLote.reforma_racional` era populado pelo precificador e persistido pelo orquestrador, porém nem `HEADER` nem `_write_sheet` em [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) referenciavam o campo, então a coluna não aparecia na planilha. O usuário pediu hoje (2026-05-08) pra finalmente expor. Aproveitei pra adicionar o indicador de saúde da coleta que ficou pendente como follow-up.
- **Arquivos:**
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — `HEADER` ganha "Racional Reforma" entre "Reforma (R$)" e "Tese"; `_query` propaga `av.reforma_racional`; `_write_sheet` renderiza com supressão em "⚠ LAUDO NÃO CAPTURADO"; nova entrada no Glossário.
  - [`carros_sa/tools/audit.py`](carros_sa/tools/audit.py) — `COLUMN_EXTRACTORS` + `CHECKS` cobrem a coluna nova (sinaliza racional vazio com reforma>0); `_build_rows` enriquece com `reforma_racional` e `laudo_analisado`; nova função `_check_cobertura_reforma` + abstração `BATCH_CHECKS` (extensível pra próximos checks agregados).
  - [`scripts/diagnose_cobertura.py`](scripts/diagnose_cobertura.py) — script novo invocado por `make diagnose-cobertura`. Read-only: identifica causa provável (LLM em fallback, lotes pendentes, possível bug no estimador) e sugere comando exato pra retry direcionado.
  - [`Makefile`](Makefile) — target `diagnose-cobertura` + entry no `make help`.
  - [`tests/test_exportar_sheets.py`](tests/test_exportar_sheets.py) — `TestRacionalReforma` (5 testes: posição da coluna, render com texto, render com None, persistência em "✗ Caro demais", supressão em "⚠ LAUDO NÃO CAPTURADO").
  - [`tests/test_audit_columns.py`](tests/test_audit_columns.py) — `TestCoberturaReforma` (5 testes do indicador agregado: dispara em 40%, silencia em 20%, dispara no limite 30%, ignora amostra<5, exclui laudos não-analisados) + `TestRacionalReformaVazio` (2 testes do per-row check).
  - [`tests/test_diagnose_cobertura.py`](tests/test_diagnose_cobertura.py) — 4 testes do script (sugestão muda conforme dominância: lotes sem laudo → retry; todos com laudo + reforma=0 → bug no estimador; sem GEMINI_API_KEY → setar config primeiro; DB vazio não crasha).
- **Como funciona o indicador:** premissa operacional "a maior parte dos carros precisa de alguma reforma" — quando ≥30% dos lotes com laudo analisado saem com `reforma=0`, audit aponta pro `make diagnose-cobertura`. Threshold em 30% por preferência do usuário ("30% já é muito"); single-tier (alarme ou silêncio, sem warning intermediário); amostra mínima 5 lotes pra evitar ruído.
- **Limitações conhecidas:**
  - Threshold 30% pode ser ruidoso em batches pequenos (5-10 lotes) onde 1-2 lotes legítimos sem reforma viram alta porcentagem. Calibrar pra 40-50% se a primeira run mostrar falso positivo.
  - Script de diagnóstico é read-only: não dispara retry automático nem chama LLM. Operador (ou workflow) decide o que fazer com base na sugestão. `--auto-retry` pode ser adicionado depois se virar atrito.
  - Healthcheck do Gemini é raso (só checa env var presente + tamanho mínimo). Ping real custaria cota; pode ser melhorado quando tiver retry budget pra gastar.

### O.1 — Coluna "Racional Reforma" na planilha ✅
- **Branch:** `claude/gifted-bassi-a24b51`
- **Status (2026-05-08):** backend marcado ✅ em 2026-04-17 mas o **frontend nunca foi cabeado** (HEADER + render no Sheets ficaram de fora). Gap fechado em O.1.1 acima.
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

### Workstream R.4 — Motivo de laudo incompleto na planilha (2026-05-07) ✅

- **Branch:** `claude/great-turing-eKFE0`
- **Problema:** desde R.1/R.3, lotes incompletos viravam "⚠ LAUDO NÃO CAPTURADO"
  genérico — operador via 100+ avisos iguais, abrir o cron log pra descobrir se
  faltou PDF, extração ou URL é fricção que mata a auditoria visual. RC3 do
  `LESSONS.md` em ação ("aviso vira ruído quando todos parecem iguais").
- **Mudança cirúrgica:**
  - [`carros_sa/tools/sheets.py`](carros_sa/tools/sheets.py) — `_query` chama
    `verificar_laudo_completo` (passando `PDF_DIR_DEFAULT` da indireção do
    módulo pra preservar monkeypatch) e enriquece a row com `laudo_completo` +
    `laudo_motivo`. `_write_sheet` sufixa o motivo legível na Situação:
    `⚠ LAUDO NÃO CAPTURADO: PDF ausente`, `... extração fraca`, `... URL inválida`,
    ou combinação `... PDF ausente + URL inválida`. Quando cache está forte mas
    sinais laterais falham (PDF some / URL stale), aparece como
    `✓ Viável (laudo: <motivo>)` — numéricos seguem válidos, operador é avisado
    sem bloquear o ranking.
  - [`tests/test_laudo_audit.py`](tests/test_laudo_audit.py) — nova classe
    `TestSituacaoCarregaMotivoLaudo` com 6 testes cobrindo cada motivo isolado,
    combinações de 2/3 motivos e o caso completo (Situação "✓ Viável" puro).
- **Cobertura:** 22 testes em `tests/test_laudo_audit.py` (16 originais + 6
  novos). Suite total: **417 passed, 2 skipped**.
- **Loop fechado com R.3:** `--strict` ainda alerta o cron quando há gaps; o
  novo sufixo na Situação alerta o **operador humano** que abre a planilha. Se
  qualquer um dos 3 sinais falhar, está agora visível em DOIS canais (cron log
  + célula da planilha) e o motivo é o MESMO código (`verificar_laudo_completo`
  é fonte única).
- **Limitações conhecidas:**
  - O sufixo cresce até ~3 motivos concatenados. Se a auditoria expandir para
    4ª condição (ex: PDF assinado por chave revogada), revisar o `_LAUDO_MOTIVO_LEGIVEL`
    em `sheets.py` pra manter o texto curto.
- **Follow-ups (não-bloqueantes, da revisão arquitetural):**
  - `_LAUDO_MOTIVO_LEGIVEL.get(p, p)` em `sheets.py` deixa código desconhecido
    vazar pro operador se `laudo_audit` adicionar motivo novo sem espelhar.
    Pequeno risco; opção: trocar pra `"motivo desconhecido"` ou logar warning.
    Não vale fix preventivo — ajusta junto quando a 4ª condição aparecer.

### Workstream R.3 — Audit estrito como gate final (2026-05-05)

- **Problema:** o auditor `make auditar-laudos` existia desde R.1 mas operador
  precisava lembrar de rodar. Sem alarme automático, lotes que escapavam do
  retry diário ficavam silenciosamente como "⚠ LAUDO NÃO CAPTURADO" na
  planilha — exatamente o sintoma de RC3 ("silêncio como default") do
  `LESSONS.md`.
- **Mudança cirúrgica:**
  - [`carros_sa/cli.py`](carros_sa/cli.py) — `_auditar_apos_triagem(empresa_id)` roda no fim de `triagem` (manual e via `make triagem`). Imprime relatório curto com motivo + remediação. Se há incompletos, `typer.Exit(1)`.
  - [`scripts/setup_cron.sh`](scripts/setup_cron.sh) — pipeline diário ganhou 4ª etapa: `auditar_laudos.py --strict` depois do retry. Exit 1 deixa rastro no `/tmp/carros_sa_triagem.log` quando algo persistiu.
  - [`carros_sa/tools/laudo_audit.py`](carros_sa/tools/laudo_audit.py) — `pdf_dir` resolve `PDF_DIR_DEFAULT` em runtime em vez de capturar em def-time, blindando contra a armadilha clássica de monkeypatch que ofuscou a regressão até este workstream.
  - [`tests/test_cli.py`](tests/test_cli.py) — 3 testes novos: `_auditar_apos_triagem` retorna 0 quando completo, conta incompletos + imprime remediação, e o `setup_cron.sh` referencia `auditar_laudos.py --strict` na ordem certa.
- **Loop fechado:** triagem (manual ou cron) só termina ✅ quando todo lote ativo na planilha tem PDF baixado + LaudoCache forte + URL clicável. Senão, exit ≠ 0 + log explícito + ponteiro pra ação corretiva.

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
