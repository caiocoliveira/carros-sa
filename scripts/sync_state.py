"""Sincroniza estado do pipeline (SQLite + cookies + PDFs de laudo) com Cloudflare R2.

Uso:
    PYTHONPATH=. .venv/bin/python scripts/sync_state.py pull   # antes do make triagem
    PYTHONPATH=. .venv/bin/python scripts/sync_state.py push   # depois do make triagem

O que é sincronizado:
    - carros_sa.db          (CARROS_SA_DB, default ./carros_sa.db)
    - autoavaliar_cookies.json (AUTOAVALIAR_COOKIES_PATH, default ~/.secrets/...)
    - data/laudos_pdfs/*.pdf   (hardcoded em carros_sa/orquestrador.py:60)

Layout em R2 (bucket único, configurado via R2_BUCKET):
    carros_sa.db
    autoavaliar_cookies.json
    laudos_pdfs/<lote_id>.pdf

Env vars obrigatórias:
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET

Idempotência:
    - pull: se objeto não existe no bucket, pula em silêncio (primeiro run = DB fresco).
    - push: sobe sempre. Chamar só se pipeline saiu com exit 0.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from rich.console import Console

console = Console()

# Paths que o pipeline usa. Espelham as defaults do código em carros_sa/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = Path(os.environ.get("CARROS_SA_DB", str(_REPO_ROOT / "carros_sa.db")))
_COOKIES_PATH = Path(
    os.environ.get("AUTOAVALIAR_COOKIES_PATH", str(Path.home() / ".secrets" / "autoavaliar_cookies.json"))
)
_PDFS_DIR = _REPO_ROOT / "data" / "laudos_pdfs"

# Keys correspondentes no bucket R2.
_DB_KEY = "carros_sa.db"
_COOKIES_KEY = "autoavaliar_cookies.json"
_PDFS_PREFIX = "laudos_pdfs/"


def _client():
    import boto3
    from botocore.config import Config

    account_id = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
    )


def _bucket() -> str:
    return os.environ["R2_BUCKET"]


def _download_if_exists(s3, key: str, dest: Path) -> bool:
    """Baixa key → dest. Retorna True se baixou, False se não existia."""
    from botocore.exceptions import ClientError

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(_bucket(), key, str(dest))
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def pull() -> None:
    s3 = _client()

    if _download_if_exists(s3, _DB_KEY, _DB_PATH):
        console.print(f"[green]✓[/green] DB baixado: {_DB_PATH} ({_DB_PATH.stat().st_size} bytes)")
    else:
        console.print(f"[yellow]·[/yellow] DB ausente no R2 — primeiro run? init_db() criará novo.")

    if _download_if_exists(s3, _COOKIES_KEY, _COOKIES_PATH):
        console.print(f"[green]✓[/green] Cookies baixados: {_COOKIES_PATH}")
    else:
        console.print(f"[yellow]·[/yellow] Cookies ausentes no R2 — scraper fará login novo.")

    paginator = s3.get_paginator("list_objects_v2")
    _PDFS_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    for page in paginator.paginate(Bucket=_bucket(), Prefix=_PDFS_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            nome = key[len(_PDFS_PREFIX):]
            if not nome:
                continue
            dest = _PDFS_DIR / nome
            s3.download_file(_bucket(), key, str(dest))
            count += 1
    console.print(f"[green]✓[/green] {count} PDFs de laudo baixados em {_PDFS_DIR}")


def push() -> None:
    s3 = _client()

    if _DB_PATH.exists():
        s3.upload_file(str(_DB_PATH), _bucket(), _DB_KEY)
        console.print(f"[green]✓[/green] DB enviado: {_DB_PATH.stat().st_size} bytes")
    else:
        console.print(f"[red]✗[/red] DB não existe em {_DB_PATH} — nada a enviar.")

    if _COOKIES_PATH.exists():
        s3.upload_file(str(_COOKIES_PATH), _bucket(), _COOKIES_KEY)
        console.print(f"[green]✓[/green] Cookies enviados")
    else:
        console.print(f"[yellow]·[/yellow] Cookies ausentes em {_COOKIES_PATH} — nada a enviar.")

    count = 0
    if _PDFS_DIR.exists():
        for pdf in sorted(_PDFS_DIR.glob("*.pdf")):
            s3.upload_file(str(pdf), _bucket(), f"{_PDFS_PREFIX}{pdf.name}")
            count += 1
    console.print(f"[green]✓[/green] {count} PDFs enviados")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("pull", "push"):
        console.print("[red]Uso:[/red] scripts/sync_state.py {pull|push}")
        sys.exit(2)

    if sys.argv[1] == "pull":
        pull()
    else:
        push()


if __name__ == "__main__":
    main()
