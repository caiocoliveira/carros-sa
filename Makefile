# Carros SA — atalhos pra comandos comuns
# Uso: `make <target>` (ex: `make test`)

.PHONY: help test test-fast ingest extrair-laudo db-reset sheets triagem triagem-debug top empresas setup-cron worktree-new worktree-remove audit limpar-decoys auditar-laudos auditar-laudos-fix

PY := PYTHONPATH=. .venv/bin/python

help:
	@echo "Alvos disponíveis:"
	@echo "  make test                          # roda toda a suíte (baseline obrigatório)"
	@echo "  make test-fast                     # roda suíte, para no 1o erro"
	@echo "  make audit                         # valida colunas exportadas contra o Glossário"
	@echo "  make ingest [FILE=...]             # persiste JSON de listagem no SQLite"
	@echo "  make extrair-laudo PDF=...         # roda ExtratorLaudo num PDF local"
	@echo "  make db-reset                      # apaga carros_sa.db e recria schema"
	@echo "  make sheets EMPRESA=<id>           # exporta triagem pro Google Sheets"
	@echo "  make triagem [EMPRESA=<id>]        # pipeline completo: scraping→avaliação→sheets"
	@echo "  make triagem-debug [EMPRESA=<id>]  # idem com browser visível"
	@echo "  make top [EMPRESA=<id>] [N=10]     # ranking offline das melhores avaliações"
	@echo "  make empresas                      # lista empresas configuradas"
	@echo "  make setup-cron                    # ativa cron diário (7h e 13h)"
	@echo "  make limpar-decoys                 # remove URLs-decoy de laudo do DB + força retry"
	@echo "  make auditar-laudos                # lista lotes zumbi na planilha (sem laudo completo)"
	@echo "  make auditar-laudos-fix            # auditar-laudos + re-extrai laudos com PDF local"
	@echo "  make worktree-new WS=<nome>        # cria worktree + branch feat/<nome>"
	@echo "  make worktree-remove WS=<nome>     # remove worktree (após merge)"

test:
	$(PY) -m pytest tests/ -v

test-fast:
	$(PY) -m pytest tests/ -x

ingest:
	$(PY) scripts/ingest_listagem.py $(FILE)

extrair-laudo:
ifndef PDF
	@echo "Erro: defina PDF=data/laudos_amostra/<arquivo>.pdf"; exit 1
endif
	$(PY) scripts/extrair_laudo.py $(PDF)

triagem:
	$(PY) -m carros_sa.cli triagem --empresa $(or $(EMPRESA),carros_uberlandia)

triagem-debug:
	$(PY) -m carros_sa.cli triagem --empresa $(or $(EMPRESA),carros_uberlandia) --no-headless

top:
	$(PY) -m carros_sa.cli top --empresa $(or $(EMPRESA),carros_uberlandia) --n $(or $(N),10)

empresas:
	$(PY) -m carros_sa.cli empresas

setup-cron:
	bash scripts/setup_cron.sh

limpar-decoys:
	$(PY) scripts/limpar_decoys_laudo.py

auditar-laudos:
	$(PY) scripts/auditar_laudos.py --detalhes

auditar-laudos-fix:
	$(PY) scripts/auditar_laudos.py --fix --detalhes

sheets:
ifndef EMPRESA
	@echo "Erro: defina EMPRESA=<id> (ex: make sheets EMPRESA=carros_uberlandia)"; exit 1
endif
	$(PY) -m carros_sa.cli sheets --empresa $(EMPRESA)

db-reset:
	rm -f carros_sa.db
	$(PY) -c "from carros_sa.db import init_db; init_db()"
	@echo "carros_sa.db recriado"

audit:
	$(PY) scripts/audit_columns.py

worktree-new:
ifndef WS
	@echo "Erro: defina WS=<nome-workstream>"; exit 1
endif
	git worktree add ../carros-sa-$(WS) -b feat/$(WS)
	@# Compartilha venv e .env (gitignored) com o worktree
	ln -s "$$(pwd)/.venv" ../carros-sa-$(WS)/.venv
	@if [ -f .env ]; then ln -s "$$(pwd)/.env" ../carros-sa-$(WS)/.env; fi
	@echo ""
	@echo "✓ Worktree criado em ../carros-sa-$(WS)"
	@echo "  Próximo passo: cd ../carros-sa-$(WS) && claude"

worktree-remove:
ifndef WS
	@echo "Erro: defina WS=<nome-workstream>"; exit 1
endif
	git worktree remove --force ../carros-sa-$(WS)
	git branch -d feat/$(WS) || git branch -D feat/$(WS) || true
	@echo "✓ Worktree e branch feat/$(WS) removidos"
