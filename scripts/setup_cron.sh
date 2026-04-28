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
AUDIT_LAUDOS_SCRIPT="$REPO_DIR/scripts/auditar_laudos.py"
LOG="/tmp/carros_sa_triagem.log"
CRON_MARK="carros-sa-triagem"
# Pipeline diário: (1) triagem completa → (2) limpeza de decoys de laudo →
# (3) retry automático de laudos pendentes.
#
# (2) limpar_decoys: até abril/2026, um seletor JS frouxo do scraper pegava o
# link do "Relatório de Transparência Salarial" (rodapé institucional) como se
# fosse o PDF do laudo e persistia essa URL-decoy em raw_json.detalhe.laudo_pdf_url.
# O gate `is_laudo_pdf_url()` hoje filtra no scraping, mas lotes legados ainda
# carregam decoy no raw_json e envenenam o retry. Rodar sempre antes do retry
# garante que qualquer decoy que vaze (padrão novo, regressão no scraper) seja
# neutralizado em ciclo único — e derruba o LaudoCache pra forçar re-extração.
#
# (3) retry: quando o scraper não acha o `laudo_pdf_url` no 1º passe (modal
# lento, rede instável, layout diferente do grupo), o orquestrador cai em
# `_laudo_sem_pdf` com confidence=0.5. Sem esse passe, o lote ia pra planilha
# como "LAUDO NÃO ANALISADO" até a próxima coleta. Cheap — pula listagem e
# só visita a URL dos lotes pendentes (inclui os que o limpar_decoys marcou).
#
# (4) auditar_laudos --auto-heal: relatório final dos 3 eixos (PDF baixado /
# revisado / link na planilha). Re-extrai laudo offline de PDFs locais válidos
# que ainda têm confidence baixa (ex.: visão Gemini caiu no run anterior, mas
# o PDF está bom no disco). Imprime contagem por motivo no log — operador
# enxerga "X lotes sem URL, Y sem PDF, Z com extração falhada" sem precisar
# entrar no SQLite.
CRON_LINE="0 7,13 * * * cd \"$REPO_DIR\" && PYTHONPATH=. \"$PYTHON\" \"$SCRIPT\" --empresa carros_uberlandia >> \"$LOG\" 2>&1; PYTHONPATH=. \"$PYTHON\" \"$DECOY_SCRIPT\" >> \"$LOG\" 2>&1; PYTHONPATH=. \"$PYTHON\" \"$RETRY_SCRIPT\" --empresa carros_uberlandia --somente-ativos --somente-laudo-pendente >> \"$LOG\" 2>&1; PYTHONPATH=. \"$PYTHON\" \"$AUDIT_LAUDOS_SCRIPT\" --empresa carros_uberlandia --auto-heal --no-fail >> \"$LOG\" 2>&1 # $CRON_MARK"

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
echo "  Comando: triagem + limpar_decoys + retry de laudos pendentes + auditar_laudos --auto-heal"
echo "  Log:     $LOG"
echo ""
echo "Para verificar: crontab -l | grep carros-sa"
echo "Para remover:   bash scripts/setup_cron.sh --remove"
echo "Para rodar agora: make triagem"
