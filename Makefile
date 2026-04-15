# Carros SA — atalhos pra comandos comuns
# Uso: `make <target>` (ex: `make test`)

.PHONY: help test test-fast ingest extrair-laudo db-reset sheets triagem triagem-debug setup-cron worktree-new worktree-remove

PY := PYTHONPATH=. .venv/bin/python

help:
	@echo "Alvos disponíveis:"
	@echo "  make test                          # roda toda a suíte (baseline obrigatório)"
	@echo "  make test-fast                     # roda suíte, para no 1o erro"
	@echo "  make ingest [FILE=...]             # persiste JSON de listagem no SQLite"
	@echo "  make extrair-laudo PDF=...         # roda ExtratorLaudo num PDF local"
	@echo "  make db-reset                      # apaga carros_sa.db e recria schema"
	@echo "  make sheets EMPRESA=<id>           # exporta triagem pro Google Sheets"
	@echo "  make triagem [EMPRESA=<id>]        # pipeline completo: scraping→avaliação→sheets"
	@echo "  make triagem-debug [EMPRESA=<id>]  # idem com browser visível"
	@echo "  make setup-cron                    # ativa cron diário às 7h"
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
	$(PY) scripts/triagem_diaria.py --empresa $(or $(EMPRESA),carros_uberlandia)

triagem-debug:
	$(PY) scripts/triagem_diaria.py --empresa $(or $(EMPRESA),carros_uberlandia) --no-headless

setup-cron:
	bash scripts/setup_cron.sh

sheets:
ifndef EMPRESA
	@echo "Erro: defina EMPRESA=<id> (ex: make sheets EMPRESA=uberlandia_mg)"; exit 1
endif
	$(PY) scripts/exportar_sheets.py --empresa $(EMPRESA)

db-reset:
	rm -f carros_sa.db
	$(PY) -c "from carros_sa.db import init_db; init_db()"
	@echo "carros_sa.db recriado"

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
