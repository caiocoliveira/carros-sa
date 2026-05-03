"""LaudoDriveClient — upload idempotente + permissão pública.

Testa o módulo `tools/laudo_drive.py` SEM bater na API do Google. Mockamos
um `session_factory` que devolve um stub com `get/post/put/patch` retornando
respostas pré-fabricadas. Testa:

  - `upload()` idempotente: arquivo já existe na pasta → não sobe de novo.
  - `upload()` novo: faz resumable upload (POST init → PUT bytes) e torna
    público.
  - `web_view_link` segue formato canônico que o Sheets renderiza com preview.
  - `build_default_drive_client()` retorna None quando env vars ausentes
    (fail-soft) e quando o JSON da SA não existe.

Não testa o `_build_authorized_session` porque é só um wrapper sobre a lib
do google-auth — sua corretude é responsabilidade da própria lib. O teste
de borda é a injeção do `session_factory`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from carros_sa.tools.laudo_drive import (
    DRIVE_FILE_ID_KEY,
    DRIVE_URL_KEY,
    DriveUploadResult,
    LaudoDriveClient,
    build_default_drive_client,
)


def _resp(status_code: int, json_data: Any = None, headers: dict | None = None):
    """Resposta http mockada com .raise_for_status no padrão do requests."""
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data or {}
    r.headers = headers or {}
    if status_code >= 400:
        r.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
        r.text = "error"
    return r


@pytest.fixture
def pdf_fake(tmp_path: Path) -> Path:
    p = tmp_path / "21854782.pdf"
    p.write_bytes(b"%PDF-1.4\n" + b"x" * 200_000)
    return p


class TestUpload:
    def test_idempotente_quando_arquivo_ja_existe(self, pdf_fake: Path):
        """Se a pasta já tem `<lote>.pdf`, retorna o ID existente sem subir."""
        sess = MagicMock()
        sess.get.return_value = _resp(
            200, {"files": [{"id": "EXISTING_FILE_ID", "name": "21854782.pdf"}]}
        )
        # Mesmo idempotente, garantimos público (POST permissions). Isso é seguro
        # porque `_tornar_publico` aceita "duplicate" como sucesso.
        sess.post.return_value = _resp(200, {"id": "perm_1"})

        client = LaudoDriveClient(folder_id="FOLDER_X", session_factory=lambda: sess)
        res = client.upload("21854782", pdf_fake)

        assert isinstance(res, DriveUploadResult)
        assert res.file_id == "EXISTING_FILE_ID"
        assert res.criado_agora is False
        assert res.web_view_link == "https://drive.google.com/file/d/EXISTING_FILE_ID/view"
        # Nem tentou subir bytes (PUT) nem POST de upload metadata.
        assert sess.put.call_count == 0
        # Único POST é o de permissions, não o de upload.
        post_urls = [c.args[0] for c in sess.post.call_args_list]
        assert all("upload" not in u for u in post_urls)
        assert any("permissions" in u for u in post_urls)

    def test_arquivo_novo_faz_resumable_e_torna_publico(self, pdf_fake: Path):
        """Sem arquivo na pasta, POST init resumable + PUT bytes + permissions público."""
        sess = MagicMock()
        # 1. GET search → sem resultado
        sess.get.return_value = _resp(200, {"files": []})
        # 2. POST resumable init → retorna Location header
        # 3. POST permissions → ok
        sess.post.side_effect = [
            _resp(200, {}, headers={"Location": "https://upload.googleapis.com/UPLOAD_SESSION_URL"}),
            _resp(200, {"id": "perm_new"}),
        ]
        # 4. PUT bytes → retorna file metadata com id
        sess.put.return_value = _resp(200, {"id": "NEW_FILE_ID"})

        client = LaudoDriveClient(folder_id="FOLDER_X", session_factory=lambda: sess)
        res = client.upload("21854782", pdf_fake)

        assert res.file_id == "NEW_FILE_ID"
        assert res.criado_agora is True
        assert res.web_view_link == "https://drive.google.com/file/d/NEW_FILE_ID/view"
        # Verifica sequência de chamadas: GET search → POST init → PUT bytes → POST perm
        assert sess.get.call_count == 1
        assert sess.put.call_count == 1
        # 2 POSTs: upload init + permissions.
        assert sess.post.call_count == 2

    def test_resumable_sem_location_header_falha(self, pdf_fake: Path):
        sess = MagicMock()
        sess.get.return_value = _resp(200, {"files": []})
        sess.post.return_value = _resp(200, {}, headers={})  # sem Location

        client = LaudoDriveClient(folder_id="FOLDER_X", session_factory=lambda: sess)
        with pytest.raises(RuntimeError, match="Location"):
            client.upload("21854782", pdf_fake)

    def test_pdf_inexistente_levanta(self, tmp_path: Path):
        client = LaudoDriveClient(folder_id="X", session_factory=lambda: MagicMock())
        with pytest.raises(FileNotFoundError):
            client.upload("nope", tmp_path / "nada.pdf")

    def test_permissao_duplicada_e_aceita_como_sucesso(self, pdf_fake: Path):
        """Drive retorna 400 com 'already' quando a permissão já existe — nosso
        código tolera, senão `upload()` falharia em chamadas subsequentes do
        mesmo arquivo (caso de re-run após queda)."""
        sess = MagicMock()
        sess.get.return_value = _resp(
            200, {"files": [{"id": "F1", "name": "21854782.pdf"}]}
        )
        sess.post.return_value = _resp(
            400, {"error": {"message": "Permission already exists"}}
        )

        client = LaudoDriveClient(folder_id="FOLDER_X", session_factory=lambda: sess)
        res = client.upload("21854782", pdf_fake)
        assert res.file_id == "F1"

    def test_folder_id_obrigatorio(self):
        with pytest.raises(ValueError, match="folder_id"):
            LaudoDriveClient(folder_id="", session_factory=lambda: MagicMock())


class TestWebViewLink:
    def test_formato_canonico(self):
        assert LaudoDriveClient.web_view_link("ABC123") == \
            "https://drive.google.com/file/d/ABC123/view"


class TestBuildDefault:
    def test_sem_env_retorna_none(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_DRIVE_LAUDOS_FOLDER_ID", raising=False)
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_PATH", raising=False)
        assert build_default_drive_client() is None

    def test_so_folder_id_sem_credentials_retorna_none(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_DRIVE_LAUDOS_FOLDER_ID", "FOLDER_X")
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_PATH", raising=False)
        assert build_default_drive_client() is None

    def test_arquivo_de_credentials_inexistente_retorna_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GOOGLE_DRIVE_LAUDOS_FOLDER_ID", "FOLDER_X")
        monkeypatch.setenv(
            "GOOGLE_SERVICE_ACCOUNT_PATH", str(tmp_path / "nao-existe.json"),
        )
        assert build_default_drive_client() is None


class TestConstantes:
    def test_chaves_persistencia_estaveis(self):
        # Estes nomes vão pra `raw_json["detalhe"]` e são lidos pelo exporter.
        # Se mudarem sem coordenação, lotes legados perdem o link.
        assert DRIVE_URL_KEY == "laudo_drive_url"
        assert DRIVE_FILE_ID_KEY == "laudo_drive_id"
