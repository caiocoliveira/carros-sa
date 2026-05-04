"""DriveUploader — sobe PDFs de laudo pro Google Drive (link permanente).

Todos os testes mockam `googleapiclient.discovery.build` e
`google.oauth2.service_account.Credentials`. Sem chamadas reais à API.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from carros_sa.tools.drive_uploader import DriveUploader, build_default_uploader


@pytest.fixture
def fake_pdf(tmp_path):
    p = tmp_path / "L001.pdf"
    # Tamanho >5KB pro audit não rejeitar (replica o threshold do laudo_audit).
    p.write_bytes(b"%PDF-1.4\n" + b"x" * 10_000)
    return p


def _mock_drive_service(existing_files=None, created_id="file-id-novo",
                        created_link="https://drive.google.com/file/d/file-id-novo/view"):
    """Constrói um mock do `service` do Drive com .files() e .permissions()."""
    files_mock = MagicMock()
    files_mock.list.return_value.execute.return_value = {
        "files": existing_files or []
    }
    files_mock.create.return_value.execute.return_value = {
        "id": created_id,
        "webViewLink": created_link,
    }
    permissions_mock = MagicMock()
    permissions_mock.create.return_value.execute.return_value = {"id": "perm-id"}

    service = MagicMock()
    service.files.return_value = files_mock
    service.permissions.return_value = permissions_mock
    return service, files_mock, permissions_mock


class TestDriveUploaderUpload:
    def test_upload_novo_arquivo_cria_e_libera_permissao(self, fake_pdf):
        service, files_mock, permissions_mock = _mock_drive_service()

        with patch("carros_sa.tools.drive_uploader.DriveUploader._client", return_value=service):
            up = DriveUploader(folder_id="folder-abc", credentials_path="/fake.json")
            link = up.upload_pdf(fake_pdf, file_name="L001.pdf")

        assert link == "https://drive.google.com/file/d/file-id-novo/view"
        # files().create chamado 1x
        assert files_mock.create.called
        create_kwargs = files_mock.create.call_args.kwargs
        assert create_kwargs["body"]["name"] == "L001.pdf"
        assert create_kwargs["body"]["parents"] == ["folder-abc"]
        # permissions().create chamado pra liberar "anyone with link"
        assert permissions_mock.create.called
        perm_body = permissions_mock.create.call_args.kwargs["body"]
        assert perm_body == {"type": "anyone", "role": "reader"}

    def test_upload_idempotente_arquivo_existente_reusa_link(self, fake_pdf):
        existing = [{"id": "file-velho", "webViewLink": "https://drive.google.com/file/d/file-velho/view"}]
        service, files_mock, permissions_mock = _mock_drive_service(existing_files=existing)

        with patch("carros_sa.tools.drive_uploader.DriveUploader._client", return_value=service):
            up = DriveUploader(folder_id="folder-abc", credentials_path="/fake.json")
            link = up.upload_pdf(fake_pdf, file_name="L001.pdf")

        assert link == "https://drive.google.com/file/d/file-velho/view"
        # NÃO criou arquivo novo (idempotência)
        assert not files_mock.create.called
        assert not permissions_mock.create.called

    def test_upload_arquivo_inexistente_levanta(self, tmp_path):
        up = DriveUploader(folder_id="folder-abc", credentials_path="/fake.json")
        with pytest.raises(FileNotFoundError):
            up.upload_pdf(tmp_path / "nao_existe.pdf", file_name="x.pdf")

    def test_upload_default_file_name_usa_local_path_name(self, fake_pdf):
        service, files_mock, _ = _mock_drive_service()

        with patch("carros_sa.tools.drive_uploader.DriveUploader._client", return_value=service):
            up = DriveUploader(folder_id="folder-abc", credentials_path="/fake.json")
            up.upload_pdf(fake_pdf)  # file_name omitido

        # Deve usar fake_pdf.name (= "L001.pdf")
        body = files_mock.create.call_args.kwargs["body"]
        assert body["name"] == fake_pdf.name

    def test_busca_existente_filtra_por_pasta_e_trashed_false(self, fake_pdf):
        """Confirma que a query do Drive isola pela pasta correta.

        Sem `'<folder_id>' in parents`, qualquer arquivo com nome `<lote>.pdf`
        em outras pastas do Drive seria considerado duplicata — mesmo que não
        tenhamos permissão de leitura. `trashed = false` evita ressuscitar
        link de arquivo que o operador já apagou intencionalmente."""
        service, files_mock, _ = _mock_drive_service()

        with patch("carros_sa.tools.drive_uploader.DriveUploader._client", return_value=service):
            up = DriveUploader(folder_id="folder-abc", credentials_path="/fake.json")
            up.upload_pdf(fake_pdf, file_name="L001.pdf")

        q_arg = files_mock.list.call_args.kwargs["q"]
        assert "name = 'L001.pdf'" in q_arg
        assert "'folder-abc' in parents" in q_arg
        assert "trashed = false" in q_arg


class TestBuildDefaultUploader:
    def test_sem_env_vars_retorna_none(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_DRIVE_FOLDER_ID", raising=False)
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_PATH", raising=False)
        assert build_default_uploader() is None

    def test_so_folder_id_sem_creds_retorna_none(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "abc")
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_PATH", raising=False)
        assert build_default_uploader() is None

    def test_ambas_env_vars_setadas_retorna_uploader(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_DRIVE_FOLDER_ID", "abc")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_PATH", "/fake.json")
        up = build_default_uploader()
        assert up is not None
        assert isinstance(up, DriveUploader)
