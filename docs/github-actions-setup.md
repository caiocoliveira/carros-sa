# Setup do triagem-diaria (GitHub Actions + Cloudflare R2)

Checklist pra ativar `.github/workflows/triagem-diaria.yml`. Esses passos são feitos **uma vez**, na UI dos provedores — não há automação via código.

## 1. Cloudflare R2 (estado persistente)

1. Criar conta em https://dash.cloudflare.com → **R2** (habilitar billing, mesmo no free tier).
2. **Create bucket** → nome: `carros-sa-state` (ou outro, só guarde).
3. Painel R2 → **Manage R2 API Tokens** → **Create API Token**:
   - Permissions: `Object Read & Write`
   - Specify bucket: `carros-sa-state`
   - TTL: `Forever` (ou rotação manual)
4. Guardar:
   - **Account ID** (aparece no dashboard R2)
   - **Access Key ID** + **Secret Access Key** (mostrados uma única vez)

Custo esperado: ~$0.003/mês (200MB × $0.015/GB). Egress é grátis no R2.

## 2. GitHub Secrets

`caiocoliveira/carros-sa` → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

Cadastrar cada um:

| Secret | De onde vem |
|---|---|
| `AUTOAVALIAR_EMAIL` | `.env` local |
| `AUTOAVALIAR_PASSWORD` | `.env` local |
| `GEMINI_API_KEY` | `.env` local |
| `ANTHROPIC_API_KEY` | `.env` local (opcional — fallback) |
| `GOOGLE_SHEETS_ID` | `.env` local |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | conteúdo inteiro do JSON da service account (o arquivo que `GOOGLE_SERVICE_ACCOUNT_PATH` aponta) — colar tudo, multilinha |
| `R2_ACCOUNT_ID` | painel Cloudflare R2 |
| `R2_ACCESS_KEY_ID` | passo 1.4 |
| `R2_SECRET_ACCESS_KEY` | passo 1.4 |
| `R2_BUCKET` | `carros-sa-state` (ou o nome escolhido) |

## 3. Semear o bucket com estado atual (recomendado)

Senão o primeiro run parte de DB zero — re-login no Auto Avaliar, re-extração de todos os laudos ativos (custo de vision API), perda de `Arrematado`/`CalibracaoCoeficiente` acumulados.

No laptop, com `.env` preenchido com os mesmos valores de R2_* acima:

```bash
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -e ".[cloud]"
PYTHONPATH=. .venv/bin/python scripts/sync_state.py push
```

Confere no dashboard do R2 que aparecem `carros_sa.db`, `autoavaliar_cookies.json`, `laudos_pdfs/*.pdf`.

## 4. Dry-run supervisionado

**Importante:** primeiro run em IP de datacenter pode ser bloqueado pelo Auto Avaliar. Não deixar o cron disparar sozinho antes de validar.

1. GitHub → **Actions** → `triagem-diaria` → **Run workflow** (branch `main`).
2. Acompanhar os steps em tempo real. Sinais de problema:
   - Step "Run triagem pipeline" trava no login (aguardando captcha / 2FA).
   - Logs cheios de HTTP 429 no scraper de detalhe.
3. Após o run, checar:
   - Google Sheet atualizada com timestamp de hoje.
   - Objeto `carros_sa.db` no R2 com `LastModified` recente.
   - Artifact `carros-sa-db-<run_id>` disponível pra download (backup).

## 5. Ativar o cron

Se o dry-run passou, o cron `0 10 * * *` (7h BRT) já está ativo — sem ação adicional. Amanhã de manhã o GitHub dispara automaticamente.

## 6. Parar de rodar localmente

Enquanto o workflow estiver ativo, **não rodar `make triagem` no laptop**. Dois writers no mesmo DB (um local, outro via R2 pull/push) pode corromper estado. Se precisar testar algo local: `CARROS_SA_DB=./test.db make triagem`.

Se quiser desabilitar o cron antigo do laptop: `crontab -e` e comentar as linhas de `scripts/triagem_diaria.py`.

## Troubleshooting

- **HTTP 429 em cascata no Auto Avaliar** → o IP da Azure foi fichado. Plano B é migrar pra VPS (Hetzner ~$5/mês, IP fixo). Código fica quase igual — basta rodar cron na VPS e esquecer o workflow.
- **Cookie expirou (re-login a cada run)** → normal depois de ~30 dias; o scraper re-loga automaticamente. Se acontecer todo dia, `AUTOAVALIAR_COOKIES_PATH` não está persistindo — conferir `sync_state.py push` no fim do run.
- **`init_db()` falha com schema antigo** → rodar `scripts/migrar_schema_workstream_k.py` localmente contra o DB baixado do R2, depois `push`.
- **Run falhou no meio, estado em R2 ficou antigo** → esperado (intencional: `if: success()` no push). Artifact `carros-sa-db-<run_id>` tem o DB parcial se quiser debugar.
