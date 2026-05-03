"""Upload de PDFs de laudo para o Google Drive — link permanente na planilha.

Motivação: URLs do Auto Avaliar (`storage.googleapis.com/doc-b2b/...`) são
pré-assinadas com validade ~1h. O operador abre a planilha algumas horas (ou
dias) depois da triagem e o link "Ver laudo" volta 403/expired. Workstream U
sinalizou explicitamente o caminho: hospedar o PDF em local que NÃO expire,
guardar o `drive_file_id` e usá-lo como link primário na planilha.

Reutilizamos o mesmo `service-account.json` já configurado pra `gspread` —
basta dar acesso de Editor numa pasta do Drive e exportar
`GOOGLE_DRIVE_LAUDOS_FOLDER_ID` no `.env`. Sem env var, a feature fica off
e o pipeline continua funcionando como antes (fail-soft).

Drive API v3 endpoints usados (raw HTTP via `AuthorizedSession`, sem
`google-api-python-client` pra não inflar deps):

  GET  /drive/v3/files?q=...           → procura por nome na pasta (idempotência)
  POST /upload/drive/v3/files          → upload multipart inicial
  PATCH /upload/drive/v3/files/{id}    → substitui conteúdo se mudou
  POST /drive/v3/files/{id}/permissions → torna público com link

Idempotência é primeira classe: chamar `upload(lote_id, path)` 2x não cria
duplicata — procura `<lote_id>.pdf` na pasta primeiro. Combinado com o
`_pdf_eh_laudo_valido` do orquestrador, garante que só PDFs validados sobem.

Tests usam `session_factory` injetável pra mockar HTTP — nenhum teste sobe
nada de verdade (regra do CLAUDE.md: nada de chamada externa em testes).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

# Hosts da API. Variáveis pra facilitar mock em teste.
_DRIVE_API = "https://www.googleapis.com/drive/v3"
_DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"

# Escopo mínimo: drive.file dá acesso só a arquivos que a SA criou — bem mais
# restritivo que `drive` (acesso completo). Suficiente porque a SA é dona dos
# uploads. Combinar com sheets pra reaproveitar a SA existente.
_SCOPES = (
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/spreadsheets",
)


@dataclass
class DriveUploadResult:
    file_id: str
    web_view_link: str       # URL clicável no formato drive.google.com/file/d/<id>/view
    criado_agora: bool       # False se já existia (idempotência)


class LaudoDriveClient:
    """Wrapper fino sobre Drive API v3 com idempotência por nome.

    Pode ser construído por `build_default_drive_client()` que lê env vars
    ou direto pra teste. `session_factory` é um callable que retorna um
    objeto compatível com `requests.Session` — em produção, uma
    `AuthorizedSession` autenticada pelo service account. Em teste, um mock.
    """

    def __init__(
        self,
        folder_id: str,
        session_factory: Callable[[], Any],
    ) -> None:
        if not folder_id:
            raise ValueError("folder_id é obrigatório")
        self._folder_id = folder_id
        self._session_factory = session_factory
        self._sess: Optional[Any] = None

    def _session(self):
        if self._sess is None:
            self._sess = self._session_factory()
        return self._sess

    @staticmethod
    def web_view_link(file_id: str) -> str:
        # Formato canônico que o Sheets renderiza bonitinho (preview inline).
        return f"https://drive.google.com/file/d/{file_id}/view"

    def _buscar_por_nome(self, nome: str) -> Optional[str]:
        """Procura `<nome>` (ex.: '12345.pdf') na pasta. Retorna file_id ou None."""
        # `q` na Drive API: name='X' and 'FOLDER' in parents and trashed=false.
        # Aspas simples no valor precisam virar \\'.
        nome_esc = nome.replace("'", "\\'")
        q = f"name='{nome_esc}' and '{self._folder_id}' in parents and trashed=false"
        resp = self._session().get(
            f"{_DRIVE_API}/files",
            params={"q": q, "fields": "files(id,name)", "pageSize": 1},
            timeout=30,
        )
        resp.raise_for_status()
        files = resp.json().get("files") or []
        return files[0]["id"] if files else None

    def _upload_novo(self, nome: str, pdf_path: Path) -> str:
        """Multipart upload: metadata + bytes numa requisição."""
        # Drive multipart usa related/multipart com boundary explícito. Em vez
        # de montar à mão, usamos o pattern simples: 1ª request com metadata
        # → resumable URL → upload bytes. Mais robusto pra arquivos grandes
        # (laudos podem ter 1-2MB).
        metadata = {
            "name": nome,
            "parents": [self._folder_id],
            "mimeType": "application/pdf",
        }
        # Resumable upload — uploadType=resumable retorna Location header com URL pra PUT bytes.
        resp = self._session().post(
            _DRIVE_UPLOAD,
            params={"uploadType": "resumable"},
            json=metadata,
            headers={
                "X-Upload-Content-Type": "application/pdf",
                "X-Upload-Content-Length": str(pdf_path.stat().st_size),
            },
            timeout=30,
        )
        resp.raise_for_status()
        upload_url = resp.headers.get("Location")
        if not upload_url:
            raise RuntimeError("Drive não retornou Location header pro upload resumable")

        with pdf_path.open("rb") as f:
            put = self._session().put(
                upload_url,
                data=f.read(),
                headers={"Content-Type": "application/pdf"},
                timeout=120,
            )
        put.raise_for_status()
        return put.json()["id"]

    def _atualizar_conteudo(self, file_id: str, pdf_path: Path) -> None:
        """Substitui bytes de um arquivo existente sem mudar metadata/permissões."""
        with pdf_path.open("rb") as f:
            resp = self._session().patch(
                f"{_DRIVE_UPLOAD}/{file_id}",
                params={"uploadType": "media"},
                data=f.read(),
                headers={"Content-Type": "application/pdf"},
                timeout=120,
            )
        resp.raise_for_status()

    def _tornar_publico(self, file_id: str) -> None:
        """Permissão 'anyone with the link can view'. Idempotente — Drive não
        cria 2 permissões iguais; se já existir, retorna 200 com a existente.
        """
        resp = self._session().post(
            f"{_DRIVE_API}/files/{file_id}/permissions",
            json={"role": "reader", "type": "anyone"},
            timeout=30,
        )
        # 200 (criado) ou 400 com 'duplicate' são ambos aceitáveis. Outros levantam.
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {}).get("message", "")
            except Exception:
                err = resp.text
            if "already" not in err.lower() and "duplicate" not in err.lower():
                resp.raise_for_status()

    def upload(self, lote_id: str, pdf_path: Path) -> DriveUploadResult:
        """Idempotente: se `<lote_id>.pdf` já existe na pasta, retorna o ID
        existente. Se não, faz upload + torna público.

        Nota: NÃO substitui conteúdo se o arquivo já existir — assumimos que
        o PDF do laudo do Auto Avaliar pra um lote_id é imutável (o lote em si
        é). Se o operador quiser forçar re-upload, use `forcar=True`.
        """
        return self._upload_interno(lote_id, pdf_path, forcar=False)

    def _upload_interno(
        self, lote_id: str, pdf_path: Path, *, forcar: bool
    ) -> DriveUploadResult:
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF não encontrado: {pdf_path}")
        nome = f"{lote_id}.pdf"
        existing = self._buscar_por_nome(nome)

        if existing and forcar:
            self._atualizar_conteudo(existing, pdf_path)
            file_id = existing
        elif existing:
            file_id = existing
        else:
            file_id = self._upload_novo(nome, pdf_path)

        # Sempre garante público — idempotente. Faz self-heal se uma permissão
        # foi removida manualmente; custo extra é 1 POST barato.
        self._tornar_publico(file_id)

        return DriveUploadResult(
            file_id=file_id,
            web_view_link=self.web_view_link(file_id),
            criado_agora=not existing,
        )


def _build_authorized_session(creds_path: str):
    """Factory padrão: AuthorizedSession com creds da service account.

    Importa lazy pra não falhar em ambientes sem `google-auth`. As deps
    transitivas via `google-genai` + `gspread` cobrem isso em produção.
    """
    from google.oauth2.service_account import Credentials  # type: ignore
    from google.auth.transport.requests import AuthorizedSession  # type: ignore

    creds = Credentials.from_service_account_file(creds_path, scopes=list(_SCOPES))
    return AuthorizedSession(creds)


def build_default_drive_client() -> Optional[LaudoDriveClient]:
    """Constrói o client a partir do .env. Retorna None quando não configurado.

    Vars necessárias:
      - GOOGLE_DRIVE_LAUDOS_FOLDER_ID — ID da pasta no Drive (achar na URL)
      - GOOGLE_SERVICE_ACCOUNT_PATH   — mesmo JSON usado pelo gspread

    Sem essas duas, retorna None e o pipeline segue normal — link da planilha
    fica no estado "pré-Drive" (URL AA enquanto fresca, depois 'PDF salvo').
    """
    folder_id = os.environ.get("GOOGLE_DRIVE_LAUDOS_FOLDER_ID", "").strip()
    creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH", "").strip()
    if not folder_id or not creds_path:
        return None
    if not Path(creds_path).exists():
        # Var setada mas arquivo sumiu — deixa explícito em vez de falhar
        # silenciosamente no primeiro upload.
        import sys
        print(
            f"[laudo_drive] GOOGLE_SERVICE_ACCOUNT_PATH={creds_path} "
            f"não existe — uploads pro Drive desativados",
            file=sys.stderr, flush=True,
        )
        return None
    return LaudoDriveClient(
        folder_id=folder_id,
        session_factory=lambda: _build_authorized_session(creds_path),
    )


# Constante usada pelo exporter pra ler o link permanente do raw_json.
DRIVE_URL_KEY = "laudo_drive_url"
DRIVE_FILE_ID_KEY = "laudo_drive_id"
