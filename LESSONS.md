# Aprendizados — Pós-mortem abril 2026

Análise de **45 fix commits** entre 2026-04-07 e 2026-04-24 + notas do ROADMAP.
Fonte de verdade pra próxima sessão não repetir erro já pago.

Use em conjunto com a memória persistente
(`/Users/caiocoliveira/.claude/projects/-Users-caiocoliveira-Carros-SA/memory/`).
Aqui fica o padrão; lá fica o detalhe específico (ex.: "Chery tem marca duplicada na FIPE").

---

## Parte 1 — Padrões de falha recorrentes

### P1. Falhas silenciosas que só surgiram via reclamação do operador

Sintoma: `None`/`[]`/`0` vazando pelo pipeline até virar output visível pro usuário.
Causa imediata: validador ausente na fronteira + fallback "tolerante" no meio.

Casos de referência:
- **Decoy "Relatório de Transparência"** (`1fd8c8d`, workstream R) — 83/85 lotes
  com `laudo_pdf_url` apontando pro PDF institucional do AA. Pipeline rodou
  ExtratorLaudo em texto sobre equidade de gênero e rotulou 111/112 como "limpo".
- **Paginação faltando** (`6bfd7e2`) — scraper só pegava página 1 (~48 de 148).
  Eu assumi scroll infinito sem validar.
- **Timer multi-dia** (`949c91b`) — regex `^\d{1,3}:\d{2}:\d{2}` não casava
  "1 dia, 18:52:23" → `fim_em=None` silencioso → 49/112 lotes sumiram. Racionalizei
  como "ciclo diário do AA" em vez de investigar.
- **Lotes encerrados na planilha** (`e2aadd5`, `42d2494`) — operador clicou no
  top 1 e o lote já tinha sido arrematado.
- **Fallback de reforma zerado** (`4e3ad0e`) — `_laudo_de_textual` retornava
  `avarias=[]` por design; Gemini 503 zerou reforma de todos os lotes silencioso.

### P2. Validação com N=1 (gold test virou prova)

Sintoma: "funciona com o Fiesta, então funciona". Único cenário feliz virou
atestado de correção.

Casos:
- **FIPE marca duplicada** (`ed94d11`) — "Caoa Chery" e "Caoa Chery/Chery"
  empatavam; eu pegava a primeira iterada. Tiggo 2.0 2015 virava R$ 114k em vez
  de R$ 41k (2.7×). Se eu tivesse testado ≥2 marcas com ambiguidade, pego.
- **Tabela de reforma determinística** (`a02db4b`) — "motor não original" era
  R$ 4.000 fixo pra qualquer carro. Gol 2014 e Range Rover 2018 saíam iguais.

### P3. Premissas inventadas em vez de confirmadas

Sintoma: "eu acho que funciona assim". Racionalização antes de investigação.

Casos:
- **"AA tem ciclo diário"** — era só meu regex bugado (ver P1).
- **`tmp_dir` persiste entre runs** — não persiste (`1805c85`). PDFs sumiam.
- **`load_dotenv()` vence o shell** — não vence sem `override=True` (`b681bd6`).
- **`networkidle` dispara em SPA** — não dispara em long-polling (`68b742e`).
- **`grep -v` é idempotente em crontab vazio** — falha com exit 1 (`7a85f02`).
- **`datetime.utcnow()` em uma ponta + `datetime.now()` na outra "se anulam"** —
  não se anulam, descasam pelo offset do timezone. `parse_card_lines` salvava
  `Lote.fim_em` como naive UTC; `sheets`/`audit`/`laudo_audit` comparavam com
  `datetime.now()` naive local. Em Brasil (UTC-3) sobrava 3h de grace silenciosa
  onde lotes encerrados apareciam ativos + horário do leilão exibido 3h
  adiantado. Padrão genérico: **misturar `now()` e `utcnow()` em datetimes
  naive é sempre bug**, independentemente da intuição de "compensam". Fix
  em 2026-05-02: parser default → `datetime.now()` pra colar com a stack
  downstream que já é toda `now()`.

### P4. Single point of failure sem defesa em profundidade

Sintoma: uma camada decidindo sozinha sobre dado de fornecedor instável.

Caso canônico: decoy PDF. Uma regex no JS do scraper decidia se era laudo.
Fix real exigiu **3 camadas**: allowlist no JS + `is_laudo_pdf_url` em Python
(defesa de meio) + `_pdf_eh_laudo_valido` inspecionando o arquivo baixado
(defesa de fim). **E** um script de limpeza dos 75 decoys já persistidos no DB
(workstream R.1) — esqueci de perguntar "o estado já salvo está envenenado?".

### P5b. Mesma métrica implementada duas vezes em arquivos diferentes diverge

Sintoma: dois lugares no código rankeiam/calculam "a mesma coisa" e operador
vê resultados conflitantes. Não é falsa positivo nem falha — é design.

Caso de referência (2026-05-05):
- **Ranking CLI vs Planilha** — `carros-sa top` ranqueava por **ROI anualizado
  desc** (`cli.py:137`); o `SheetsExporter` ranqueava por **folga absoluta
  `preco_max - lance_atual` desc** (`sheets.py:142`). CLI premiava lote
  lucrativo com lance perto do teto; planilha premiava lote barato com folga
  grande, mesmo com ROI muito menor. Operador via duas listas diferentes da
  mesma fonte de dados, sem aviso. Detectado em revisão econômica das colunas
  via simulação algébrica + leitura cruzada (não por reclamação do operador
  desta vez — preventivo).

**Antídoto operacional:** quando uma métrica é ranking-defining, definir
**uma função canônica** num módulo neutro (`carros_sa/agents/calibracao_giro.py`
já hospeda `roi_anualizado`) e fazer todos os consumidores importarem dela.
Cuidado especial com helpers ad-hoc embutidos em `cli.py`, `sheets.py`,
`audit.py` — tendem a reimplementar variantes silenciosas.

**Antídoto estrutural:** se o repo tiver mais de uma "view" sobre o mesmo
ranking (CLI + planilha + audit), adicionar em `audit.py` ou em teste de
integração uma checagem de paridade — ex.: top-3 do `cli.top` é um subset do
top-N da planilha, ordem preservada. Falha vira sinal antes do operador ver.

### P5g. Callers paralelos do mesmo entrypoint com defaults divergentes silenciam camadas de defesa

Sintoma: defesa em camadas funciona em alguns caminhos, falha silenciosa em
outros. Audit reporta o sintoma; investigação rastreia até "o cliente X foi
passado em A, não foi passado em B."

Caso de referência (2026-05-22, DD6):
- `extrair_laudo(pdf, vision_client, text_llm_client=None)` foi extendido em
  DD4 (2026-05-15) com uma 4ª camada que dispara só quando `text_llm_client`
  é passado. Triagem inicial (`triagem_diaria.py` → `orquestrar` → `_pipeline_lote`)
  passava o cliente. Retry diário (`scripts/reprocessar_lotes_do_db.py` →
  `_pipeline_lote` direto) NÃO passava — mesma assinatura, default None.
- Lote de vendor fora do template Auto Avaliar entrando pela 1ª vez via
  triagem: camada 4 dispara, extrai OK. Lote entrando via retry: camada 4
  não dispara, visual responde "0.0 + listas vazias" honestamente, persiste,
  `tentativas++`, 3 ciclos, circuit-breaker (II) congela. Nunca rodou a
  defesa nele. Audit `--strict` reporta `cache_confianca_baixa` em loop.

**Antídoto operacional:** quando uma feature opcional (kwarg default `None`)
é gateway de uma camada de defesa, **todos os callers do entrypoint precisam
passar o kwarg** — sem exceção. Adicionar grep guard cobrindo cada caller
paralelo:

```python
# tests/test_paridade_callers.py
def test_retry_passa_text_llm_client():
    src = Path("scripts/reprocessar_lotes_do_db.py").read_text()
    assert "text_llm_client=" in src.split("_pipeline_lote(")[1][:400]
```

**Antídoto estrutural:** se o kwarg é defesa-em-profundidade obrigatória,
considere tornar o default *opinativo* (auto-instanciar via factory dentro
do entrypoint) em vez de `None`. Aí o caller que não quer paga o custo
explícito (`text_llm_client=False` ou similar). Hoje em DD4 escolhemos o
caminho conservador (não auto-instanciar pra não quebrar testes) — mas a
opção fica em cima da mesa se P5g aparecer de novo.

**Conexão com P5b:** P5b é "mesma métrica em dois arquivos diverge no que
calcula"; P5g é "mesma chamada em dois lugares com defaults divergentes
silencia uma camada". Sintomas externos parecidos (paridade quebrada);
causas e fixes diferentes.

### P5. Invariantes adicionadas reativamente, nunca preventivamente

Sintoma: operador aponta "essa coluna tá sem sentido", e só depois vira teste.

O workstream Q (`carros_sa/tools/audit.py` + hook SessionEnd) foi criado depois
de várias rodadas de "operador vê, operador reclama, eu conserto". Muitos dos
fixes acima teriam sido pegos em `make test` se o `audit.py` existisse em
2026-04-10 (ex.: `fim_em=None` na planilha, ROI > 500%, severidade fora do enum).

### P5c. Paridade audit ↔ display: filtragem **e** substituição

Sintoma: audit reporta violação que o operador NÃO consegue confirmar abrindo
a planilha — porque o display substitui o valor problemático por placeholder
(`—`), mas o audit valida o valor cru.

Caso de referência (2026-05-07, revisão preventiva):
- **ROI anualizado negativo em lote inviável** — `SheetsExporter._write_sheet:422-429`
  substitui ROI/Lucro/Tese por `"—"` quando `viavel=False` (cenário "comprar
  pelo alvo é fantasia se lance > preco_max"). `audit.COLUMN_EXTRACTORS`
  retornava o número cru, então `_score_roi_efetivo` com `capital_ef > preco_giro`
  (Fiesta ESTRUTURAL real: -53.9%) disparava "ROI anualizado negativo —
  score_roi negativo". Operador olhava a planilha e via "—". Falso alarme.

**Antídoto operacional:** paridade audit ↔ display vai além de "filtrar mesmas
linhas" (P5 original). Toda **substituição** de display (campo X vira `"—"`
em condição Y) precisa do mesmo no `COLUMN_EXTRACTORS` do audit. Audit é uma
view sobre o display, não sobre o DB cru.

### P5e. Paridade audit ↔ display: TODA dimensão de supressão, não só inviabilidade

Sintoma: display oculta um campo em CONDIÇÃO Y (não só `viavel=False`),
mas audit lê o valor cru e dispara warning. Operador olha planilha e vê
"—" no campo, mas o relatório do audit reclama dele. Falso alarme estrutural.

Caso de referência (2026-05-09):
- **`laudo_analisado=False` (confidence < 0.6 ou ausente)** — `_write_sheet`
  já oculta Lance Máximo / Lucro / ROI / Reforma / Tese e mostra "⚠ LAUDO
  NÃO CAPTURADO". Audit cobria só `viavel=False` (paridade P5c). Lotes com
  laudo fallback `_laudo_sem_pdf` (confidence 0.55) marcam severidade=ESTRUTURAL
  sem peça → audit disparava "Reforma R$ 0 com severidade estrutural" mesmo
  quando display oculta toda a linha.
- **`lance_atual == preco_max` (boundary inviável)** — `viavel = preco_max >
  lance` é estrito; zona apertada usava `<=` no max. Audit reportava "zona
  apertada" enquanto display mostrava "✗ Caro demais". Sinais contraditórios
  na mesma linha.

Generalização (P5c → P5e): **cada `if condicao_X: cell = "—"` no exporter
precisa de paridade explícita no audit, NÃO importa qual é a condição**.
Lista de dimensões cresce com o produto (em 2026-05-09 já são ≥3: `fim_em
is None`, `viavel`, `laudo_analisado`). Ler `_write_sheet` end-to-end antes
de adicionar novo extractor — caso contrário o audit eventualmente cobre só
algumas dimensões e gera falsos alarmes silenciosos no resto.

**Antídoto operacional:** ao adicionar nova condição de supressão no display:
1. listar todos os campos que ficam `"—"` nessa condição
2. atualizar `_build_rows` pra calcular o flag (`laudo_analisado`, etc.)
3. atualizar `COLUMN_EXTRACTORS` pra cada campo retornar `"—"` quando o flag for `False`
4. atualizar cross-checks (`_check_*`) pra retornar `[]` quando o flag for `False`
5. validators de `CHECKS` que comparam `v <= 0` precisam guarda `isinstance(v, (int, float))` pra tolerar `"—"` sem `TypeError`
6. teste guard do tipo `test_FOO_em_X_falsa_nao_dispara` pra cada cross-check.

### P5f. Upsert que reconstrói "container" parcial perde subkeys de outras camadas

Sintoma: operação de upsert (UPDATE/INSERT idempotente) recebe um payload
parcial e reconstrói o campo "container" (`raw_json` dict, JSONB, etc.) a
partir desse payload. Subkeys escritas por OUTROS passos do pipeline somem
silenciosamente. O bug é latente: só aparece quando outro mecanismo (cache,
short-circuit) começa a depender dessas subkeys.

Caso de referência (2026-05-10, `_upsert_lote`):
- `_upsert_lote(lote_raw, ...)` reconstruía `raw_json` a partir do
  `LoteRaw.model_dump()` (listagem — sem `detalhe`). Apenas `loja` era
  preservada da `raw_json` existente. `detalhe.laudo_pdf_url` e
  `body_text_sample` (escritos por `_persistir_flags_no_lote` após o
  scraper de detalhe) eram ZERADOS em todo cron diário.
- Por meses ninguém percebeu: o pipeline rodava completo a cada run e
  `coletar_detalhe` repopulava a URL. Bug latente.
- Após DD2 (2026-05-09) — state/db persistiu PDFs e o short-circuit ficou
  estrito (`ja_avaliado AND laudo_ok AND pdf_ok`) — o pipeline passou a
  PULAR esses lotes (cache + PDF OK). A URL nunca mais voltava. **Bug
  latente virou ativo em 95/187 lotes ativos** = 51% de perda da coluna
  "Ver laudo" em produção.

**Antídoto:**
1. Quando uma operação de upsert recebe payload parcial, **enumere todas
   as subkeys que outras camadas do pipeline escrevem nesse container** e
   preserve uma a uma — não confie em "lembrar" das que existem agora.
2. Teste guard explícito: simular o ciclo (camada A escreve subkey X →
   camada B faz upsert com payload sem X → assert subkey X ainda lá).
3. Defesa em profundidade: critério de short-circuit / "já feito" do
   pipeline deve cobrir TODAS as condições que a auditoria valida — não
   um subset. Caso contrário, qualquer regressão futura no upsert volta
   a virar bug latente sem detecção.
4. Filtros de retry também precisam de paridade total com a auditoria
   (mesmas condições, mesma fonte de verdade). Auditoria reportar X mas
   retry não pegar X = laço aberto, lotes presos.

Padrão genérico: **upsert idempotente + payload parcial = lista explícita
de subkeys preservadas, idealmente com teste guard por subkey**. Se o
container tem N subkeys e o upsert preserva K<N, é bug em estado
"esperando alguma camada começar a depender da N-K-ésima".

### P5d. If/elif encadeado num check esconde red flags atrás de yellow flags

Sintoma: validador de coluna usa `if cond_A else (if cond_B else (if cond_C ...))`
e só retorna o **primeiro** motivo encontrado. Quando uma linha tem mais de
um sintoma simultâneo (caso patológico), o operador só vê o sintoma mais
superficial.

Caso de referência (2026-05-08, `carros_sa/tools/audit.py`): `CHECKS["Lance
Máximo (R$)"]` aninhava 4 condições — (1) preco_max ≤ 0 num viável,
(2) preco_alvo > preco_max, (3) zona apertada, (4) preco_max > FIPE × 1.05.
Cenário simulado: lote com mediana inflada (similares Webmotors n≥5 sem cap)
+ lance acima do alvo. Disparava SÓ "zona apertada" (yellow) — o "Lance Máximo
> FIPE × 1.05" (red flag, indica dado economicamente quebrado) ficava
silenciado pelo encadeamento. Operador via amarelo e ignorava; o vermelho
escondido sinalizava que ele estava prestes a dar lance acima da FIPE.

Fix: cross-checks vivem em `ALL_CHECKS` como funções independentes que
retornam `List[CheckResult]` (cada uma 0-1 motivos), não em `CHECKS` dict
que força lambda 1-motivo. Múltiplos sintomas na mesma linha emergem
simultaneamente. Padrão genérico: **se um check tem 3+ ramos, separar em
funções independentes — encadeamento vira ponto cego garantido em casos
patológicos**.

### P5f. Colunas derivadas dimensionalmente acopladas precisam do mesmo basis

Sintoma: duas colunas exibidas refletem dimensões da MESMA decisão econômica
(ex.: `Lucro = capital × ROI`), mas são calculadas com bases diferentes —
operador faz a aritmética mentalmente, não bate, e suspeita do sistema. Caso
simétrico ao P5b (mesma métrica em dois lugares diverge), mas aqui são
DUAS métricas que **deveriam** se compor.

Caso de referência (2026-05-10, revisão preventiva):
- **`ROI alvo (%)` × `Lucro (R$)` em zona apertada** — `Lucro (R$)` usava
  `score_efetivo` (realista, reduzido em zona apertada onde `lance_atual >
  preco_alvo`) enquanto `ROI alvo (%)` usava `score_roi` intrinsic. Em
  Cenário 6 (Gol em zona apertada): ROI exibido 64.3%, Lucro R$ 7,167.
  Capital implícito = 7167 / 0.643 = R$ 11,148 — sem correspondência em
  nenhuma coluna da linha (preco_alvo R$ 19,721, lance R$ 27,500). Mental
  math do operador `capital × ROI = Lucro` falhava. Detectado por simulação
  canônica + leitura cruzada das colunas, não por reclamação.

**Antídoto operacional:** quando duas colunas derivadas formam relação
algébrica explícita (`Lucro = capital × ROI`), adicionar teste de coerência
do tipo `assert lucro / (roi/100) + lucro ≈ preco_giro` — mental math do
operador passa. Esse teste flagra mudança de basis em qualquer das colunas.

**Antídoto estrutural:** documentar a INTENÇÃO de basis (intrinsic vs.
efetivo) tanto no validator do audit quanto no glossário. Se uma coluna
muda de basis, automaticamente revisar TODAS as colunas dimensionalmente
acopladas a ela. Lista canônica em sheets.py: `Lucro` ↔ `ROI alvo` ↔
`Lance Máximo` ↔ `FIPE` (todos R$ ou % e correlatos).

### P6. Audit por coluna isolada não pega contradição cross-field

Sintoma: cada célula passa no validador individual, mas a combinação é
inconsistente. "✓ Viável + reforma R$ 0" num lote estrutural valida cada
campo isoladamente (situação ok, reforma 0 é ≥ 0, severidade está no enum)
mas é contradição evidente quando vista em linha. Mesmo padrão para
`preco_giro_fipe` 50% acima da FIPE — cada um é positivo, nenhum estoura
o cap individual, mas a relação entre eles indica `_extrai_precos_similares`
poluído ou cache FIPE stale.

Fix em 2026-05-02 (`carros_sa/tools/audit.py`): CHECKS recebem `row` dict com
campos vizinhos; checks de "Reforma" e "FIPE" agora cruzam severidade e
`preco_giro_fipe` respectivamente. Padrão genérico: **invariante não é só
"valor está no domínio", é "esse valor é coerente com seus pares na mesma
linha"**.

---

## Parte 2 — Causa raiz: o que está por baixo dos padrões

Os cinco padrões acima têm origens comuns. Se atacar só os sintomas, volto a
produzir padrões parecidos em domínios diferentes. O que segue é desagradável
de ler, mas é onde mora a alavanca.

### RC1. Viés de confirmação em vez de ceticismo ativo

Quando um teste passa ou um output "parece certo", eu paro de interrogar.
O Fiesta gold test virou "o teste" — e tratar passar nele como prova de
correção é o mesmo erro do pesquisador que ajusta o experimento pra bater com
a hipótese. Isso alimenta P2 direto, e P3 indiretamente (se não duvido,
racionalizo).

**Antídoto operacional:** antes de marcar `✅`, escrever 3 perguntas do tipo
"o que quebraria isso?" e só fechar depois de respondê-las com teste ou
evidência. Gold test é piso, nunca teto.

### RC2. Fornecedor instável tratado como API estável

AA, Gemini, FIPE são fontes adversariais ou instáveis: DOM muda, 503 aparece,
marcas duplicadas existem, campos desaparecem. Codifiquei como se o contrato
fosse estável. Isso gera P1 (falha silenciosa no fornecedor) e P4 (SPOF no
ponto de contato).

**Antídoto operacional:** toda fronteira com fornecedor externo tem 2 coisas
obrigatórias: (a) validador de domínio com allowlist explícita (não heurística);
(b) métrica contada e exposta (% de `None`, % de fallback, distribuição de
confidence). Drift vira alerta, não surpresa do operador.

### RC3. Silêncio como default em vez de alarme como default

Hábitos de "código defensivo" (try/except → return None, dado ausente → pula)
são corretos em biblioteca genérica mas catastróficos num pipeline onde `None`
flui por 5 estágios e vira "lote limpo pra licitar". Fallback existe pra
**continuar operando**, não pra **esconder que algo quebrou**. Isso é o motor
principal de P1.

**Antídoto operacional:** todo fallback emite log estruturado com contador
(`logger.warning("fallback_acionado", reason=..., lote_id=...)`). Triagem
diária reporta % de cada tipo. Threshold de alerta configurável. Se cair em
fallback pra >20% dos lotes, isso é bug, não resiliência.

### RC4. Debug reativo em camadas, sem curar a raiz

Quando algo quebra, minha tendência é adicionar flag/retry/fallback. Isso
conserta o sintoma, mas deixa o bug real vivo embaixo e cria complexidade que
vai mascarar o próximo bug. Os fixes `02ad964` (short-circuit respeitar laudo
pendente) e `a142bcd` (motor_ok default True) são casos assim — camada nova
pra contornar estado inconsistente em vez de investigar por que o estado ficou
inconsistente.

**Antídoto operacional:** antes de adicionar flag/fallback/try-except, escrever
uma frase explícita: "a causa raiz é X, e a mitigação é Y porque Z". Se não
consigo nomear X, não tenho direito de escrever Y ainda.

### RC5. Falta de "adversário" no loop mental

Pensamento de segurança/confiabilidade exige perguntar "o que faria isso
quebrar de forma maliciosa ou casual?". Meu default é happy path → ship. Sem
simular adversário (AA muda DOM, LLM alucina R$ 200k de reforma, usuário cola
PDF errado), construo pro caso de demo. Isso alimenta P1 e P4 simultaneamente.

**Antídoto operacional:** em todo feature novo, escrever explicitamente 3
cenários adversariais na descrição do PR: (1) fornecedor muda schema; (2)
LLM/parser retorna lixo plausível; (3) estado antigo no DB é incompatível.
Cobrir ao menos os 2 primeiros com teste.

### RC6. Memória de armadilhas não persiste bem entre sessões paralelas

Worktrees separados = sessões invisíveis entre si até o merge. ROADMAP cobre
**o que** foi feito, mas não sistematicamente **por que o erro anterior
aconteceu**. Quirks de fornecedor (Chery duplicado, decoy do AA, `networkidle`
não dispara) viraram nota dispersa. A próxima sessão redescobre.

**Antídoto operacional:** este arquivo. Toda sessão que fechar workstream com
fix commit acrescenta 2-3 linhas aqui no padrão correspondente, referenciando
o hash. E em casos de quirk específico de fornecedor, entrada em
`~/.claude/projects/.../memory/` com tag `quirk_fornecedor`.

### RC8. Tests de comportamento de flag são âncoras de intenção semântica

Sintoma: ao "alinhar" duas peças do código (ex.: ranking de CLI vs. display
de planilha), eu mudo a base de ranking de um flag (ex.: `--absoluto`)
sem perguntar se aquele flag tinha intenção semântica DIFERENTE. Teste
falha. Em vez de tratar a falha como sinal, tendência é "atualizar o teste
pra refletir o novo comportamento" — que destrói a intenção codificada.

Caso de referência (2026-05-10):
- **`--absoluto` no `carros-sa top`** — calibrado pra ranquear por
  `score_roi` intrinsic ("sniff-test de potencial econômico no alvo
  teórico"). Quando mudei display de intrinsic pra efetivo (fix P5f), tentei
  alinhar `--absoluto` também → `test_top_ranqueia_por_roi_anualizado_default`
  quebrou (fixture inviável em zona apertada flipa ranking entre intrinsic
  e efetivo). Tive a tentação de "atualizar o teste". Errado: o teste
  estava me dizendo que `--absoluto` tem semântica DIFERENTE do default.
  Reverter mantém ambos sensatos: default = realista (efetivo), `--absoluto`
  = potencial alvo (intrinsic).

**Antídoto operacional:** quando um teste falha por mudança comportamental,
ler a docstring/nome do teste antes de "atualizar pra passar". Se o teste
ancora intenção semântica explícita ("--flag X faz Y"), ele está
documentando uma decisão de produto — perguntar se você está REVERTENDO
a decisão de propósito ou só por descuido. "Atualizar o teste" é o erro
default; ler o teste primeiro é a defesa.

**Antídoto estrutural:** flags com semântica não-óbvia (que diferem do
comportamento default em casos sutis) precisam de teste-âncora dedicado +
docstring que vincule "este flag responde a pergunta X, default responde
a pergunta Y". Tests-âncora são MAIS valiosos que tests de "happy path"
porque restringem o que você pode mudar sem pensar.

### RC10. Extrator devolvendo "vazio confiável" persistido como-está vira loop perpétuo

Sintoma: uma camada de extração devolve resposta válida mas com sinal nulo
— modelo dizendo "olhei e não tem nada útil aqui" (confidence baixa + listas
vazias) em vez de inventar. Persistir esse resultado como-está parece honesto
mas, se a próxima rodada do pipeline aplicar o MESMO extrator no MESMO input,
o resultado é o mesmo, indefinidamente. O lote fica num bucket de retry
perpétuo, audit reclama, cron exit-1, ninguém olha.

Caso de referência (2026-05-15, DD4):
- **Gemini visual sobre página 2 do PDF** — extrator hardcoded pro template
  Auto Avaliar (diagrama colorido na página 2). Vendors observados em
  produção (DEKRA, Procemax, SA-Laudo, Vistoria Cautelar genérica) NÃO têm o
  diagrama nessa página. Gemini corretamente devolvia `confidence=0.0,
  pecas=[]`. Persistido. Próximo cron rodava no mesmo lote → mesmo resultado.
  22/47 lotes ativos travados em `cache_confianca_baixa` perpetuamente.

**Antídoto operacional:** detectar "extrator respondeu mas inútil"
(confidence < threshold + listas vazias) como caso DIFERENTE de "extrator
respondeu com sinal forte". Disparar uma camada de fallback que olha pra
input DIFERENTE — mesmo PDF, mas usando texto completo em vez de página 2;
ou mesmo lote, mas refazendo o scrape; ou mesmo dado, mas via LLM diferente.
"Mesmo extrator, mesmo input, próxima rodada" é definição de loop infinito.

**Antídoto estrutural:** quando uma camada de extração tem premissa de
formato hardcoded (template do vendor X, página N do PDF, regex calibrada
em fixture Y), prever que vendors NOVOS vão chegar e ou (a) construir o
extrator pra ser vendor-agnostic desde o início (LLM com prompt sobre input
diverso), ou (b) ter uma camada de fallback explícita pronta. "Vendor X é o
único que existe" é premissa não-validada — viola RC2 (fornecedor instável
tratado como API estável).

### RC7. Pressa em fechar o workstream

ROADMAP trata `✅` como métrica de sucesso visível. Isso cria incentivo
implícito pra declarar pronto cedo. "Tests green + gold test passou" virou
critério, quando o critério certo exige também "rodou em amostra diversa +
invariante no audit + fallback loga".

**Antídoto operacional:** elevar o critério de aceite em `CLAUDE.md` (já
existe — só precisa ser mais rigoroso). Ver checklist na Parte 3.

---

## Parte 3 — Disciplinas a adotar

### Checklist pré-merge de workstream (novo critério de `✅`)

Adicionar ao critério atual em `CLAUDE.md`:

- [ ] `make test` verde, **incluindo teste novo do workstream**
- [ ] Gold test com fixture de dado real
- [ ] **Teste com ≥3 cenários variados** (não só o feliz; ao menos 1 adversarial)
- [ ] **Invariante correspondente em `carros_sa/tools/audit.py`** se tocou
      coluna da planilha ou campo de `AvaliacaoLote`
- [ ] **Fallback loga warning estruturado com contador**, não silencioso
- [ ] **Validador de domínio** nas fronteiras com AA/FIPE/Gemini (allowlist,
      não heurística)
- [ ] **Se o fix mudou critério de validação:** considerei que estado antigo
      no DB pode estar envenenado? Precisa script de limpeza?
- [ ] `ROADMAP.md` atualizado + entrada nova em `LESSONS.md` se o workstream
      fechou um dos 5 padrões

### Em código novo

1. **Sem fallback silencioso.** Se cair em fallback, `logger.warning(...)`.
2. **Validador antes de confiar.** Dado de fonte externa passa por
   `is_X_valid()` explícita antes de ser propagado.
3. **Contador em métricas de triagem.** % de `None`, % de fallback, distribuição
   de confidence expostos em todo run.
4. **Premissa vira comentário só se não-óbvia** (regra de CLAUDE.md). Premissa
   de fornecedor vira entrada em `memory/`.

### Em ops/debug

1. **Antes de adicionar flag/retry:** nomear a causa raiz explicitamente.
2. **Quando fornecedor mudar:** antes de consertar, investigar se há outros
   lugares no código com a mesma premissa.
3. **Quando o operador apontar algo estranho:** invariante em `audit.py` antes
   de fechar a issue. "Manual review" não é defesa.

---

## Apêndice — Index dos fixes citados

| Hash | Padrão | Descrição curta |
|---|---|---|
| `1fd8c8d` | P1, P4 | Allowlist de pdf_url (decoy Transparência) |
| `6bfd7e2` | P1, P3 | Paginação real `?p=N` (3x inventário) |
| `949c91b` | P1, P3 | Timer multi-dia "N dia, HH:MM:SS" |
| `e2aadd5` | P1, P5 | Filtrar lotes encerrados do export |
| `42d2494` | P1, P5 | Filtrar lotes sem `fim_em` |
| `4e3ad0e` | P1 | Remover fallback silencioso de reforma |
| `ed94d11` | P2 | FIPE v2 + desambiguação Chery duplicado |
| `a02db4b` | P2 | EstimadorReformaLLM (tabela fixa era grosseira) |
| `1805c85` | P3, P4 | Defesa em profundidade download PDF |
| `b681bd6` | P3 | `load_dotenv(override=True)` |
| `68b742e` | P3 | `domcontentloaded` em vez de `networkidle` |
| `7a85f02` | P3 | `grep -v ... || true` em crontab vazio |
| `02ad964` | RC4 | Short-circuit respeita laudo pendente |
| `a142bcd` | RC4 | `motor_ok` default True quando ausente |
| `e57bb3a` | P5 | Auditoria automática de colunas (hook) |
| (R.3) | P5, RC3 | `auditar_laudos --strict` no cron + CLI triagem fecha o laço "todo lote ativo tem laudo completo" — antes, `make auditar-laudos` existia mas era manual e a planilha ficava com "⚠ LAUDO NÃO CAPTURADO" silencioso. Lição secundária: `pdf_dir: Path = PDF_DIR_DEFAULT` capturado em def-time impedia monkeypatch — sempre resolver default mutável em runtime quando o teste vai patchá-lo. |
| (consolidação cluster precificador 2026-05-07) | P6 | Cap `_MARGEM_TETO=0.50` no precificador + supressão Lucro/ROI em lotes inviáveis em sheets.py. Identidade matemática consistente mas semanticamente errada (lote estrutural com margem 90% → score_roi 1.0+ → Lucro/mês R$5k+ exibido em "✗ Caro demais") = primeiro caso registrado de bug não-algébrico. |
| (consolidação cluster precificador 2026-05-07) | P6 | Cap mediana similares Auto Avaliar em `FIPE × 1.20` quando n<5. Mediana isolada não defende contra outlier categórico (Tiggo 7 entre Tiggo 2 com n=2 → mediana 153% FIPE → lance MAIOR que FIPE). Statisticamente inocente, operacionalmente catastrófico. |
| (consolidação cluster precificador 2026-05-07) | RC3 | `_score_roi_efetivo` em sheets — ROI honesto em zona apertada (lance_atual > preco_alvo). Antes a planilha exibia ROI baseado no alvo mesmo quando o operador real entraria acima dele, criando impressão otimista (caso real Polo: 273% → 122%). Métrica intrinsic (DB) ≠ métrica realista (display). |
| (consolidação cluster precificador 2026-05-07) | P3, P4 | DRY de `_categoria_de_modelo` no frete (orquestrador). Toro era PICAPE no calibrador mas OUTRO no frete antigo — drift entre listas semi-paralelas. Resolução: `_calcular_frete` aceita categoria pré-resolvida do pipeline; fallback usa a tabela canônica de `calibracao_giro`. |
| (consolidação cluster precificador 2026-05-07) | P5 | Audit espelha o exporter (filtra `fim_em is None`) + cross-checks (`preco_giro_fipe > FIPE × 1.10`, `preco_alvo > preco_max`) + derived check (margem ≥ 49% no teto) + threshold ROI 1000→500% calibrado contra benchmark operacional. |
| (consolidação cluster precificador 2026-05-07) | P6 | Floor `dias_giro` 30d → 60d em `lucro_reais_por_mes` e `roi_anualizado`. Defaults categóricos chegavam a 25d (HATCH NOVO) e fazia `Lucro/mês = Lucro absoluto` (lucro_abs × 30/30 = lucro_abs). Floor maior = magnitude honesta sem destruir o sinal. |
| (revisão preventiva 2026-05-07, post-cluster) | P5b/P6 | Audit espelha TODAS as supressões do display (não só filtros de linha). Lotes inviáveis substituem ROI/Lucro/Tese por `—` em `_write_sheet:422-429`; antes o `COLUMN_EXTRACTORS` retornava o número cru e `_score_roi_efetivo` com `capital_ef > preco_giro` (Fiesta ESTRUTURAL real, -53.9%) disparava "ROI anualizado negativo" em lotes que o operador NUNCA via. Falso alarme operacional. Padrão genérico: paridade de SUBSTITUIÇÃO, não só de filtragem. |
| (revisão preventiva 2026-05-07, post-cluster) | P4/RC2 | Cap defensivo `preco_giro_fipe ≤ FIPE × 1.20` no precificador. Cap mediana (entrada, em avaliador_mercado, n<5) + f_km saturado (1.15) podiam multiplicar pra 1.38×FIPE — combinação patológica de duas otimizações. Defesa em camadas: 3 caps com propósitos distintos (entrada, saída, alarme). Não compartilham constante por design — propósitos diferentes pedem ajuste independente. |
| (revisão preventiva 2026-05-07, post-cluster) | RC3 | `_score_roi_efetivo` coalesce `av.preco_alvo or 0`. Schema diz non-nullable hoje, mas migrações antigas podem ter deixado NULL; sem coalescência o `lance - None` levantava TypeError silencioso que quebrava a planilha inteira. Helper que recebe SQLModel "non-nullable" deve coalescer — schema atual ≠ histórico do DB. |
| (DD2 2026-05-09) | P3, P5c | **Persistência seletiva quebra invariante de auditoria.** Workflow CI persistia DB+cookies em `state/db` mas descartava `data/laudos_pdfs/` entre runs ("PDFs são só cache pra UX"). Audit demandava PDF on-disk como uma das 3 condições; com DB restaurado e PDFs zerados, `auditar_laudos --strict` falhava cronicamente por `pdf_ausente`. Padrão genérico: **se a auditoria valida (A, B, C) ∧, persistência tem que cobrir (A, B, C) também — persistir 2 dos 3 cria contradição garantida pelo próprio gate**. Fix: persistir `laudos_pdfs/` na subárvore de `state/db` + curto-circuito do orquestrador exigir PDF on-disk + retry filter pegar PDF ausente como pendente. Defesa em profundidade. |
| (revisão preventiva 2026-05-09) | P5e | Paridade audit ↔ display pra `laudo_analisado=False` (confidence < 0.6 ou laudo ausente). `_build_rows` agora calcula o flag e `COLUMN_EXTRACTORS` retorna `"—"` em Lance Máximo / Lucro / ROI / Reforma / Tese. Cross-checks (`_check_zona_apertada`, `_check_lance_maximo_acima_fipe`, `_check_reforma_pesada`, novos `_check_motor_problema`, `_check_severidade_estrutural`) respeitam o flag. Antes: lote `_laudo_sem_pdf` (confidence 0.55) marcava ESTRUTURAL sem peça → "Reforma R$ 0 com severidade estrutural" disparava enquanto display oculta tudo. Falso alarme estrutural. |
| (revisão preventiva 2026-05-09) | P5e | `_check_zona_apertada` usa `<` estrito no `preco_max` (era `<=`). Boundary `lance_atual == preco_max` é INVIÁVEL (`viavel = preco_max > lance`); display oculta tudo, audit não deve reportar zona apertada. Antes: planilha mostrava "✗ Caro demais" e audit reportava "zona apertada" — sinais contraditórios na mesma linha. |
| (revisão preventiva 2026-05-09) | P6 | Cross-checks operacionais novos: `_check_motor_problema_em_viavel` (motor_ok=False em lote viável + laudo analisado) e `_check_severidade_estrutural_em_viavel`. Precificador penaliza ambos via fator_risco mas o teto saturado SÓ não basta — com lance baixo, lote passa como "✓ Viável" sem warning visível. Padrão: condição econômica = "passa pelo precificador"; condição operacional = "warning visível pro operador". Modelar AS DUAS. |
| (revisão preventiva 2026-05-09) | P5/RC2 | `_check_mediana_distante_fipe` em audit — flag informativo quando webmotors_mediana > FIPE × 1.20 (similares poluídos do AA: Tiggo 7 entre Tiggo 2) ou < FIPE × 0.70 (sample fraca). Refactor FIPE-only de 2026-05-08 tornou a coluna "Mediana mercado" puramente informativa, mas operador olhando "mediana 130% FIPE" pode achar que é "carro premium em alta" — falso. Audit fecha o vetor sem afetar cálculo. |
| (revisão preventiva 2026-05-09) | RC4 | `_PRECO_GIRO_FIPE_RATIO_MAX` 1.10 → 1.13. Threshold antigo tinha gap de só 0.75pp do max natural (1.0925 = `_FATOR_MAX × 0.95`); qualquer aumento futuro de `_FATOR_MAX` em ajuste_km.py disparava falso positivo automático. Padrão: threshold de "guard de regressão" calibrado pelo max teórico de constante em outro arquivo precisa de **margem ≥ 2-3pp** + comentário cruzado referenciando a constante. Quem altera a constante futuramente vê a referência — caso contrário o gap silenciosamente fecha. |
| (revisão preventiva 2026-05-09) | RC3 | Validators de `CHECKS` com `v <= 0` precisam de guarda `isinstance(v, (int, float))` quando o coluna pode receber `"—"` da supressão do display. Sem guarda, `"—" <= 0` levanta TypeError em runtime — só dispara quando o caminho da supressão é exercitado em produção (laudo NÃO CAPTURADO num lote real). Audit silenciosamente quebrava na linha em vez de pular o validator. |
| (revisão preventiva 2026-05-10) | P5f | **Coerência aritmética entre colunas dimensionalmente acopladas** — `Lucro (R$)` usava `score_efetivo` mas `ROI alvo (%)` usava `score_roi` intrinsic. Em zona apertada (Cenário 6 simulação: Gol 64% / R$ 7k), capital implícito do operator math `lucro/(roi/100)` não correspondia a nada na linha. Fix: `roi_alvo = score_efetivo × 100` em sheets.py + cli.py + audit.py (paridade) + glossário + teste guard `test_coerencia_roi_lucro_zona_apertada`. Padrão genérico: quando duas colunas formam relação algébrica explícita, adicionar teste `lucro/(roi/100) + lucro ≈ preco_giro` impede regressão. |
| (revisão preventiva 2026-05-16) | P5b/RC4 | **Mudança de basis de ranking deixa dead code nos arquivos com paridade obrigatória.** PR #94 trocou `key=lambda r: -roi_anualizado` por `-(r["lucro"] or 0)` em sheets/cli/audit (workstream II), mas o cálculo+import+entry de dict de `roi_anualizado` ficou nos 3 arquivos — escrito como `r["roi_anualizado"]` que nenhuma view lê. Comentários de bloco ao redor (sheets.py "mantemos roi_anualizado no key= do sorted"; audit.py "chave de DESEMPATE"; glossário "ranking por ROI ANUALIZADO") documentavam o comportamento antigo enquanto o código abaixo já usava o novo. Padrão genérico: quando troca métrica de view com paridade exigida (P5b — sheets+cli+audit), **grep `métrica_antiga` em CADA arquivo da lista** antes de fechar PR. Tendência é remover só o cálculo da função onde foi MUDADO `key=`, não os outros 2; e comentários longos justificando o comportamento antigo viram parágrafos órfãos. Fix: extirpar cálculo+import+entry+comentário+glossário nos 3 arquivos + teste guard `test_query_nao_carrega_roi_anualizado_dead_key` que assert `"roi_anualizado" not in rows[0]`. Complementa P5b: lá era "mesma métrica em 2 lugares diverge"; aqui é "métrica antiga removida de 1/N lugares com paridade vira fantasma em N-1". |
| (revisão preventiva 2026-05-10) | RC8 | **`--absoluto` no CLI top mantido em score_roi intrinsic** mesmo após display passar a usar score_efetivo. Tentativa de "alinhar" o flag quebrou test-âncora (`test_top_ranqueia_por_roi_anualizado_default`) que codifica intenção semântica diferente: `--absoluto` responde "qual o potencial econômico no alvo teórico?" enquanto default responde "qual o ROI realista dado lance atual?". Lição: tests de comportamento de flag são **âncoras de intenção semântica** — quando falham por mudança comportamental, ler docstring antes de "atualizar pra passar". |
| (revisão preventiva 2026-05-23) | P3/RC4 | **Defaults com ano hardcoded são bombas-relógio silenciosas.** `faixa_de_idade(ano_veiculo, ano_referencia=2026)` + `calibrar_dias_giro(..., ano_referencia=2026)` + `bucket_modelo(..., ano_referencia=2026)` calibravam idade contra o literal 2026. Em jan/2027, carro 2023 calcularia idade=3 (NOVO) mesmo tendo idade real 4 (MEDIO) — silenciosamente miscalibrando prior de dias_giro e classificação ILIQUIDO (≥10 anos) em todos os 4 callers (avaliador_mercado, cli.top, audit, calibracao_giro interno). Fix: default lazy resolvido em runtime via `datetime.now().year`. Padrão genérico: **literal de ano como default de função é sempre bug latente**. Mesma classe do P3 ("`datetime.utcnow()` na ponta vs `datetime.now()` na outra"). Quando vir literal de ano em function signature, mover pra runtime resolution + teste guard que use ano atual.|
| (revisão preventiva 2026-05-23) | P5/P6 | **Sufixo de warning operacional em Situação** — lote viável com severidade=ESTRUTURAL ou motor_ok=False ganhava só warning no audit; operador focado em ROI alto raramente roda audit antes do lance. Display agora antecipa visualmente: "✓ Viável ⚠ ESTRUTURAL", "✓ Viável ⚠ motor", "✓ Viável ⚠ ESTRUTURAL + motor". Cross-checks audit (`_check_severidade_estrutural_em_viavel`, `_check_motor_problema_em_viavel`) continuam existindo (defesa em camadas), mas agora o sinal chega no fluxo natural do operador. Padrão genérico: **cross-checks operacionais do audit devem propagar visualmente pra Situação quando o lote PASSA pelo precificador mas operador não deveria comprar**. Aplicar mesmo quando display NÃO suprime números (laudo confiável, decisão é dele) — basta sinalizar pra ele LER o laudo antes do lance. |
