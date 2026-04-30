#!/usr/bin/env bash
# Configura cron para rodar a triagem de leilões 2x por dia (7h e 13h).
# O leilão do Auto Avaliar fecha numa janela única de tarde/noite; rodar
# cedo dá o batch do dia, e a passada do meio-dia pega lotes que entraram
# depois da 1ª coleta.
# Uso: bash scripts/setup_cron.sh
# Remove: bash scripts/setup_cron.sh --remove

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO_DIR/.venv/bin/python3"
SCRIPT="$REPO_DIR/scripts/triagem_diaria.py"
RETRY_SCRIPT="$REPO_DIR/scripts/reprocessar_lotes_do_db.py"
DECOY_SCRIPT="$REPO_DIR/scripts/limpar_decoys_laudo.py"
AUDIT_SCRIPT="$REPO_DIR/scripts/auditar_laudos.py"
LOG="/tmp/carros_sa_triagem.log"
CRON_MARK="carros-sa-triagem"
# Pipeline diário, fechando o laço "todo lote ativo na planilha tem laudo
# baixado + revisado + link clicável" (workstream U + V):
#
# (1) triagem completa — coleta listagem multi-cidade, roda pipeline em
#     lotes novos, exporta planilha.
#
# (2) limpar_decoys — até abril/2026, um seletor JS frouxo do scraper pegava
#     o link do "Relatório de Transparência Salarial" (rodapé institucional)
#     como se fosse o PDF do laudo e persistia em raw_json.detalhe.laudo_pdf_url.
#     O gate `is_laudo_pdf_url()` hoje filtra no scraping, mas lotes legados
#     ainda carregam decoy. Rodar antes do retry zera URL envenenada e derruba
#     o LaudoCache pra forçar re-extração no passo (3). Cache forte (≥0.6) é
#     preservado mesmo com URL fora da allowlist.
#
# (3) retry com max-tentativas=3 — quando o scraper não acha o `laudo_pdf_url`
#     no 1º passe (modal lazy AA, Gemini 503 transitivo, 429 no download),
#     orquestrador cai em `_laudo_sem_pdf` com confidence=0.5 e o lote vira
#     "⚠ LAUDO NÃO CAPTURADO" na planilha. 1ª tentativa cobria caso simples.
#     Loop com max-tentativas=3 dá 3 oportunidades antes de declarar que ele
#     ficou stuck — Playwright session warm + re-consulta de pendentes a cada
#     iteração shrinka conforme lotes ganham confidence>=0.6.
#
# (4) auditoria final --strict — gate observável: exit 1 se sobrar lote ativo
#     sem laudo completo (PDF + cache forte + URL). Cron registra o erro e o
#     próximo `;` ainda roda (chained, não &&), então a falha NÃO derruba o
#     pipeline; só vira sinal claro no log "$LOG" pro operador. Sem este passo
#     ninguém percebia que sobrava 12-30 lotes presos por ciclo.
CRON_LINE="0 7,13 * * * cd \"$REPO_DIR\" && PYTHONPATH=. \"$PYTHON\" \"$SCRIPT\" --empresa carros_uberlandia >> \"$LOG\" 2>&1; PYTHONPATH=. \"$PYTHON\" \"$DECOY_SCRIPT\" >> \"$LOG\" 2>&1; PYTHONPATH=. \"$PYTHON\" \"$RETRY_SCRIPT\" --empresa carros_uberlandia --somente-ativos --somente-laudo-pendente --max-tentativas 3 >> \"$LOG\" 2>&1; PYTHONPATH=. \"$PYTHON\" \"$AUDIT_SCRIPT\" --empresa carros_uberlandia --strict >> \"$LOG\" 2>&1 # $CRON_MARK"

if [[ "${1:-}" == "--remove" ]]; then
    echo "Removendo entrada do cron..."
    crontab -l 2>/dev/null | grep -v "$CRON_MARK" | crontab -
    echo "✓ Entrada removida."
    exit 0
fi

# Verifica pré-requisitos
if [[ ! -f "$PYTHON" ]]; then
    echo "Erro: Python não encontrado em $PYTHON"
    echo "Verifique se o venv foi criado em $REPO_DIR/.venv"
    exit 1
fi

if [[ ! -f "$REPO_DIR/.env" ]]; then
    echo "Aviso: .env não encontrado. Copie .env.example e preencha antes de rodar."
fi

# Adiciona ao cron (idempotente — remove entrada anterior antes).
# `grep -v` retorna 1 quando crontab está vazio ou não tem a marca; com
# `set -e` + pipefail isso matava o script. `|| true` neutraliza.
(crontab -l 2>/dev/null | grep -v "$CRON_MARK" || true; echo "$CRON_LINE") | crontab -

echo "✓ Cron configurado:"
echo "  Horário: todo dia às 07:00 e 13:00"
echo "  Comando: triagem → limpar_decoys → retry (max 3 tentativas) → auditar_laudos --strict"
echo "  Log:     $LOG"
echo ""
echo "Para verificar: crontab -l | grep carros-sa"
echo "Para remover:   bash scripts/setup_cron.sh --remove"
echo "Para rodar agora: make triagem"
