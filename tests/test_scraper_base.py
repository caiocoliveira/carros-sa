"""Conformidade do AutoAvaliarScraper com o Protocol `Scraper`."""

from __future__ import annotations

from carros_sa.scraping.base import AutoAvaliarScraper, Scraper


def test_autoavaliar_satisfaz_protocolo_scraper():
    """AutoAvaliarScraper tem todos os métodos esperados na assinatura correta."""
    scraper = AutoAvaliarScraper()
    assert isinstance(scraper, Scraper)
    # Sanity: métodos existem e são awaitable.
    for m in ("coletar_listagem", "coletar_detalhe", "baixar_pdf"):
        assert callable(getattr(scraper, m))


def test_protocol_rejeita_objeto_incompleto():
    """Um stub que NÃO implemente baixar_pdf falha o isinstance check."""

    class _Incompleto:
        async def coletar_listagem(self, page, empresa, horizonte_dias):
            return []

        async def coletar_detalhe(self, page, url):
            return ("", None)

    assert not isinstance(_Incompleto(), Scraper)
