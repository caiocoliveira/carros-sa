"""Upload de PDFs de laudo pro Google Drive — link permanente na planilha.

Motivação: as URLs pré-assinadas que o Auto Avaliar serve em
`storage.googleapis.com/doc-b2b/...` expiram em ~1h. Após esse prazo, o
=HYPERLINK("Ver laudo") na planilha vira link morto. O exporter já degrada
gracefully pra "PDF salvo (link expirado)" quando detecta o PDF local, mas
isso é só sinalização — o operador continua sem conseguir abrir o laudo
direto da planilha. Subir o PDF pro Drive (mesma service account já usada
pro Sheets) cria um link permanente que sobrevive entre runs.

Design idempotente: antes de subir, busca o arquivo por nome (`<lote_id>.pdf`)
dentro da pasta-alvo. Se já existe, devolve o `webViewLink` existente em vez
de duplicar. Isso permite:
  - rodar a triagem N vezes/dia sem encher a pasta de duplicatas
  - reprocessar com o `sync_laudos_drive.py` sem custo

Pasta-alvo configurada por `GOOGLE_DRIVE_FOLDER_ID` no `.env`. Sem ela, o
uploader vira no-op silencioso (build_default_uploader retorna None) e o
pipeline continua usando a URL pré-assinada como antes.

Permissão: cada arquivo é compartilhado como "anyone with link, reader" pra
que o operador consiga abrir do Sheets sem login extra.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class DriveUploader:
    """Sobe PDF de laudo pro Drive e retorna webViewLink permanente.

    Usa `googleapiclient` (lazy import) com a mesma service account JSON do
    `gspread`. Evita reupload via busca por nome dentro da pasta.
    """

    def __init__(self, folder_id: str, credentials_path: str) -> None:
        self._folder_id = folder_id
        self._credentials_path = credentials_path
        self._service = None  # lazy init pra não exigir dep em quem não usa

    def _client(self):
        if self._service is None:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds = service_account.Credentials.from_service_account_file(
                self._credentials_path, scopes=_SCOPES,
            )
            # cache_discovery=False evita gerar warning chato + arquivo de cache
            # em CWD em ambientes onde isso é proibido (cron com home read-only).
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def _buscar_existente(self, file_name: str) -> Optional[str]:
        """Retorna webViewLink se já existe arquivo com esse nome na pasta."""
        svc = self._client()
        # `q` aceita expressões com escape '\\' pra aspas. file_name vem de
        # lote_id.pdf — só dígitos/letras na prática, mas escapamos por segurança.
        nome_escaped = file_name.replace("'", "\\'")
        q = f"name = '{nome_escaped}' and '{self._folder_id}' in parents and trashed = false"
        resp = svc.files().list(
            q=q,
            fields="files(id, webViewLink)",
            pageSize=1,
        ).execute()
        files = resp.get("files", [])
        if files:
            return files[0].get("webViewLink")
        return None

    def upload_pdf(self, local_path: Path, file_name: Optional[str] = None) -> str:
        """Sobe o PDF (ou retorna link existente) e devolve webViewLink permanente.

        - `file_name` default: `local_path.name`. Recomendado passar
          `<lote_id>.pdf` pra que o sync_laudos_drive consiga deduplicar.
        - Levanta `FileNotFoundError` se o arquivo local não existe.
        - Falhas de rede/API propagam — chamador é responsável por capturar
          (no orquestrador isso vira no-op com log de warning).
        """
        if not local_path.exists():
            raise FileNotFoundError(f"PDF local não encontrado: {local_path}")
        if not file_name:
            file_name = local_path.name

        existente = self._buscar_existente(file_name)
        if existente:
            logger.info("drive_upload: %s já existe, reusando link", file_name)
            return existente

        from googleapiclient.http import MediaFileUpload

        svc = self._client()
        metadata = {"name": file_name, "parents": [self._folder_id]}
        media = MediaFileUpload(str(local_path), mimetype="application/pdf", resumable=False)
        criado = svc.files().create(
            body=metadata,
            media_body=media,
            fields="id, webViewLink",
        ).execute()
        file_id = criado["id"]

        # Permissão pública por link — sem isso o webViewLink dá 403 pra quem
        # abre do Sheets sem estar logado na conta da service account.
        svc.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
        ).execute()

        return criado["webViewLink"]


def build_default_uploader() -> Optional[DriveUploader]:
    """Constrói uploader a partir de env vars; None se config ausente.

    Lê:
      - GOOGLE_DRIVE_FOLDER_ID — ID da pasta no Drive (extraído da URL)
      - GOOGLE_SERVICE_ACCOUNT_PATH — mesmo JSON usado pelo SheetsExporter

    Sem alguma delas, devolve None — chamador decide como lidar (no-op no
    orquestrador, erro claro no script de sync).
    """
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")
    if not folder_id or not creds_path:
        return None
    return DriveUploader(folder_id=folder_id, credentials_path=creds_path)
