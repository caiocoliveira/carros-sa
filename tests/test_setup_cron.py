"""Guard contra rename de scripts referenciados no cron.

`scripts/setup_cron.sh` define o pipeline diário (triagem → limpar-decoys →
retry de laudos pendentes) hardcodando paths de scripts. Se alguém renomear
ou mover um desses arquivos, o cron quebra silenciosamente até alguém abrir
o log — e nesse meio tempo a planilha acumula "⚠ LAUDO NÃO ANALISADO".

Este teste varre o setup_cron.sh procurando referências a `scripts/*.py` e
falha se algum não existir. Catch barato pra regressão de refactor.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CRON_SH = REPO_ROOT / "scripts" / "setup_cron.sh"
# Comandos com flags que o cron passa pros scripts — se uma flag for removida
# do script e mantida no cron (ex.: `--somente-laudo-pendente`), o retry vira
# no-op silencioso. Validamos a presença literal da flag no arquivo do script.
FLAGS_OBRIGATORIAS = {
    "scripts/reprocessar_lotes_do_db.py": ["--somente-ativos", "--somente-laudo-pendente"],
}


def test_cron_so_referencia_scripts_existentes():
    assert CRON_SH.exists(), "setup_cron.sh sumiu"
    conteudo = CRON_SH.read_text()
    referenciados = set(re.findall(r'scripts/[A-Za-z0-9_\-]+\.py', conteudo))
    assert referenciados, "Nenhum script referenciado em setup_cron.sh — algo está muito errado"

    faltando = [s for s in referenciados if not (REPO_ROOT / s).exists()]
    assert not faltando, (
        f"setup_cron.sh referencia scripts que não existem: {faltando}. "
        "Se renomeou, atualize setup_cron.sh; se removeu, ajuste o pipeline diário."
    )


def test_flags_do_cron_existem_nos_scripts():
    for script_rel, flags in FLAGS_OBRIGATORIAS.items():
        script = REPO_ROOT / script_rel
        assert script.exists(), f"{script_rel} sumiu — atualize FLAGS_OBRIGATORIAS"
        conteudo = script.read_text()
        faltando = [f for f in flags if f not in conteudo]
        assert not faltando, (
            f"setup_cron.sh chama {script_rel} com flags {faltando} que o "
            f"script não declara mais — cron viraria no-op silencioso."
        )
