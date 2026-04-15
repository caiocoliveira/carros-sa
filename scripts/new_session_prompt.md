# Template: primeira mensagem de sessão paralela

Cole o bloco abaixo como **primeira mensagem** em cada nova sessão Claude Code aberta num worktree. Substitua `<WORKSTREAM>` pelo nome (ex: `avaliador-mercado`).

---

```
Sessão paralela do workstream: <WORKSTREAM>

Antes de começar:

1. Leia CLAUDE.md na raiz — stack, convenções, contratos imutáveis, red flags.
2. Leia ROADMAP.md, seção "Workstream: <WORKSTREAM>" — escopo, branch, critério de aceite.
3. Leia a memória persistente em /Users/caiocoliveira/.claude/projects/-Users-caiocoliveira-Carros-SA/memory/MEMORY.md
4. Rode `make test` — baseline deve estar 100% verde antes de qualquer mudança.
5. Implemente SOMENTE o escopo do workstream. Se identificar necessidade de alterar
   carros_sa/models.py ou outro arquivo "não tocar" listado no CLAUDE.md, PARE e
   reporte aqui antes de prosseguir.

Critério de aceite pra considerar "pronto":
- `make test` verde (incluindo ≥1 teste novo cobrindo o caso de uso principal com
  fixture de dado real em tests/fixtures/)
- Entrada do workstream no ROADMAP.md marcada ✅ com link pros arquivos criados
- Descobertas relevantes (quirks de fornecedor, decisões não óbvias) adicionadas
  na memória persistente
- Nenhum segredo commitado; .env intacto
```

---

## Workstreams em aberto

- `avaliador-mercado` — FIPE + similares da plataforma
- `webmotors-scraper` — Playwright com anti-bot
- `estimador-reforma` — tabela YAML + fallback LLM
- `scraper-detalhe` — Chrome MCP / Playwright pra página de detalhe + download PDF laudo

Ver detalhes completos em [`ROADMAP.md`](../ROADMAP.md).
