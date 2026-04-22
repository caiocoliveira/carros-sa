"""Contrato `Scraper` — interface que todo scraper de leilão deve respeitar.

Motivação: hoje só temos Auto Avaliar (510 linhas em scraper_autoavaliar.py) e
implementar Webmotors significaria descobrir por tentativa e erro quais
funções o orquestrador chama. Este Protocol documenta o contrato mínimo.

Nota: é um `typing.Protocol` (duck typing), não uma ABC — quem já satisfaz a
assinatura (ex.: módulo `scraper_autoavaliar` usado via wrapper) não precisa
herdar. Também não obrigamos ninguém a migrar ainda; o orquestrador segue
usando as funções soltas.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from carros_sa.tenancy import EmpresaConfig


@runtime_checkable
class Scraper(Protocol):
    """Contrato mínimo que o orquestrador espera de um scraper de leilão.

    `page` é um `playwright.async_api.Page` já autenticado, deixado como Any
    pra não forçar import do Playwright em módulos que só leem o protocol.
    """

    async def coletar_listagem(
        self, page: Any, empresa: EmpresaConfig, horizonte_dias: int,
    ) -> list[dict]:
        """Retorna cards da listagem com ao menos `loteId`, `href`, `lines`."""

    async def coletar_detalhe(
        self, page: Any, url: str,
    ) -> tuple[str, str | None]:
        """Retorna `(body_text, pdf_url | None)` da página de detalhe do lote."""

    async def baixar_pdf(
        self, pdf_url: str, dest: Path, cookies: list[dict],
    ) -> None:
        """Persiste o PDF do laudo em `dest` usando cookies autenticados."""


class AutoAvaliarScraper:
    """Adapter fino sobre `scraper_autoavaliar` — expõe as funções como métodos.

    Existe pra destravar implementação de novos scrapers (Webmotors, Leilão123)
    sem obrigar ninguém a refatorar o módulo legado. Quem implementar um novo
    herda/imita este shape e injeta no orquestrador.
    """

    async def coletar_listagem(
        self, page: Any, empresa: EmpresaConfig, horizonte_dias: int,
    ) -> list[dict]:
        from carros_sa.scraping.scraper_autoavaliar import coletar_listagem
        return await coletar_listagem(page, empresa, horizonte_dias)

    async def coletar_detalhe(
        self, page: Any, url: str,
    ) -> tuple[str, str | None]:
        from carros_sa.scraping.scraper_autoavaliar import coletar_detalhe
        return await coletar_detalhe(page, url)

    async def baixar_pdf(
        self, pdf_url: str, dest: Path, cookies: list[dict],
    ) -> None:
        from carros_sa.scraping.scraper_autoavaliar import baixar_pdf
        await baixar_pdf(pdf_url, dest, cookies)
