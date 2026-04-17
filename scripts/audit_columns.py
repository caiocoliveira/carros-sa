#!/usr/bin/env python
"""Auditoria de colunas — chamado pelo hook SessionEnd do Claude Code.

Abre o SQLite do projeto, roda `carros_sa.tools.audit.audit`, imprime 0 linhas
quando tudo ok e uma linha por violação quando acha algo fora do racional do
Glossário. Exit code sempre 0 — hook não deve travar a sessão por erro no audit.

Uso:
    PYTHONPATH=. .venv/bin/python scripts/audit_columns.py
    make audit
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    # Imports dentro da função pra que falha de import caia no try-except abaixo
    # em vez de quebrar o hook silenciosamente no carregamento do módulo.
    from carros_sa.db import DEFAULT_DB_PATH, get_engine
    from carros_sa.tools.audit import audit

    db_path = Path(DEFAULT_DB_PATH)
    if not db_path.exists():
        # Sem DB → hook silencioso. Evita poluir quando rodou `make db-reset`
        # ou em máquinas virgens antes do primeiro pipeline.
        return 0

    engine = get_engine(db_path)
    violacoes = audit(engine)

    if not violacoes:
        return 0

    # Usa stderr pra evitar mistura com output de comandos próximos ao fim de
    # sessão, e destaca com banner curto.
    print("", file=sys.stderr)
    print("─── auditoria de colunas ───", file=sys.stderr)
    for linha in violacoes:
        print(linha, file=sys.stderr)
    print("────────────────────────────", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — hook nunca deve travar sessão
        # Falha silenciosa com contexto em stderr pra o usuário saber que rodou
        # mas não conseguiu validar (import quebrado, schema desalinhado, etc.).
        print(f"[audit_columns] auditoria pulada: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(0)
