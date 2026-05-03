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
BACKFILL_DRIVE="$REPO_DIR/scripts/backfill_laudos_drive.py"
AUDIT_SCRIPT="$REPO_DIR/scripts/auditar_laudos.py"
LOG="/tmp/carros_sa_triagem.log"
CRON_MARK="carros-sa-triagem"
# Pipeline diário (5 passos):
#   (1) triagem completa
#   (2) limpa decoys de laudo
#   (3) retry automático de laudos pendentes
#   (4) backfill Drive — sobe PDFs locais que ainda não têm link permanente
#   (5) auditoria --strict — exit code != 0 quando ainda há lotes incompletos,
#       deixando rastro óbvio no LOG pro operador investigar
#
# (2) limpar_decoys: até abril/2026, um seletor JS frouxo do scraper pegava o
# link do "Relatório de Transparência Salarial" (rodapé institucional) como se
# fosse o PDF do laudo e persistia essa URL-decoy em raw_json.detalhe.laudo_pdf_url.
# O gate `is_laudo_pdf_url()` hoje filtra no scraping, mas lotes legados ainda
# carregam decoy no raw_json e envenenam o retry.
#
# (3) retry: quando o scraper não acha o `laudo_pdf_url` no 1º passe (modal
# lento, rede instável, layout diferente do grupo), o orquestrador cai em
# `_laudo_sem_pdf` com confidence=0.5. Cheap — só visita a URL dos pendentes.
#
# (4) backfill Drive: URLs do storage AA expiram em ~1h, então o link na
# planilha morre ao longo do dia. Subindo o PDF pro Drive (idempotente, no-op
# se já existe), o link vira permanente. Sem GOOGLE_DRIVE_LAUDOS_FOLDER_ID
# configurado o script sai com mensagem clara — não derruba o cron.
#
# (5) auditoria --strict: garante que qualquer lote ativo com laudo incompleto
# vire erro não-silencioso. O cron continua rodando depois (não é crítico),
# mas o exit code != 0 facilita alarmes se o operador integrar com monitoring.
CRON_LINE="0 7,13 * * * cd \"$REPO_DIR\" && PYTHONPATH=. \"$PYTHON\" \"$SCRIPT\" --empresa carros_uberlandia >> \"$LOG\" 2>&1; PYTHONPATH=. \"$PYTHON\" \"$DECOY_SCRIPT\" >> \"$LOG\" 2>&1; PYTHONPATH=. \"$PYTHON\" \"$RETRY_SCRIPT\" --empresa carros_uberlandia --somente-ativos --somente-laudo-pendente >> \"$LOG\" 2>&1; PYTHONPATH=. \"$PYTHON\" \"$BACKFILL_DRIVE\" --empresa carros_uberlandia >> \"$LOG\" 2>&1 || true; PYTHONPATH=. \"$PYTHON\" \"$AUDIT_SCRIPT\" --empresa carros_uberlandia --strict >> \"$LOG\" 2>&1 || true # $CRON_MARK"

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
echo "  Comando: triagem → limpar-decoys → retry → backfill-drive → auditar --strict"
echo "  Log:     $LOG"
echo ""
echo "Para verificar: crontab -l | grep carros-sa"
echo "Para remover:   bash scripts/setup_cron.sh --remove"
echo "Para rodar agora: make triagem"
