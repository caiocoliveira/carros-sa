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

### P5. Invariantes adicionadas reativamente, nunca preventivamente

Sintoma: operador aponta "essa coluna tá sem sentido", e só depois vira teste.

O workstream Q (`carros_sa/tools/audit.py` + hook SessionEnd) foi criado depois
de várias rodadas de "operador vê, operador reclama, eu conserto". Muitos dos
fixes acima teriam sido pegos em `make test` se o `audit.py` existisse em
2026-04-10 (ex.: `fim_em=None` na planilha, ROI > 500%, severidade fora do enum).

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
