#!/usr/bin/env bash
# Configura cron diário para rodar a triagem de leilões às 7h.
# Uso: bash scripts/setup_cron.sh
# Remove: bash scripts/setup_cron.sh --remove

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$REPO_DIR/.venv/bin/python3"
SCRIPT="$REPO_DIR/scripts/triagem_diaria.py"
LOG="/tmp/carros_sa_triagem.log"
CRON_MARK="carros-sa-triagem"
CRON_LINE="0 7 * * * cd \"$REPO_DIR\" && PYTHONPATH=. \"$PYTHON\" \"$SCRIPT\" --empresa carros_uberlandia >> \"$LOG\" 2>&1 # $CRON_MARK"

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

# Adiciona ao cron (idempotente — remove entrada anterior antes)
(crontab -l 2>/dev/null | grep -v "$CRON_MARK"; echo "$CRON_LINE") | crontab -

echo "✓ Cron configurado:"
echo "  Horário: todo dia às 07:00"
echo "  Comando: triagem_diaria.py --empresa carros_uberlandia"
echo "  Log:     $LOG"
echo ""
echo "Para verificar: crontab -l | grep carros-sa"
echo "Para remover:   bash scripts/setup_cron.sh --remove"
echo "Para rodar agora: make triagem"
