"""Tests para paginação de `_coletar_listagem_cidade`.

Auto Avaliar usa paginação real via `?p=N` (não scroll infinito). O scraper
precisa iterar por todas as páginas detectadas no DOM de paginação pra pegar
o inventário completo — caso contrário só vemos ~48 de 148 lotes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

from carros_sa.scraping import scraper_autoavaliar


@dataclass
class FakePage:
    """Page stub com goto/evaluate/wait_for_timeout assíncronos.

    `pagina_to_cards` mapeia número da página (1, 2, ...) → lista de cards
    que `_EXTRACT_CARDS_JS` retornaria. `total_paginas` é o que o DOM de
    paginação reporta.
    """

    pagina_to_cards: Dict[int, List[Dict[str, Any]]]
    total_paginas: int
    _pagina_atual: int = 1
    gotos: List[str] = field(default_factory=list)

    async def goto(self, url: str, wait_until: Optional[str] = None, timeout: Optional[int] = None) -> None:
        self.gotos.append(url)
        # Extrai o número de página da URL (default 1)
        import re
        m = re.search(r"[?&]p=(\d+)", url)
        self._pagina_atual = int(m.group(1)) if m else 1

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def evaluate(self, js: str) -> Any:
        # Chama uma ou outra função pré-programada baseado em conteúdo do JS.
        if "data-page" in js:
            return self.total_paginas
        # Default: retorna cards da página atual.
        return self.pagina_to_cards.get(self._pagina_atual, [])


def _card(lote_id: str, timer: Optional[str] = "00:20:00:00") -> Dict[str, Any]:
    lines = [
        "Uberlândia/MG",
        "25%",
        "MARCA",
        "MODELO",
        "10.000,00",
        "VERSAO",
        "2020/2020",
        "FLEX",
        "120.000",
    ]
    if timer:
        lines.append(timer)
    return {"loteId": lote_id, "href": f"/avaliacoes/x/{lote_id}/slug", "lines": lines}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestPaginacao:
    def test_uma_pagina_so_navega_uma_vez(self):
        page = FakePage(
            pagina_to_cards={1: [_card("A"), _card("B")]},
            total_paginas=1,
        )
        cards = _run(scraper_autoavaliar._coletar_listagem_cidade(page, "Uberlandia", "MG", 7))
        assert len(cards) == 2
        assert {c["loteId"] for c in cards} == {"A", "B"}
        # Só navegou pra página 1 (sem ?p=)
        gotos_com_p = [u for u in page.gotos if "&p=" in u or "?p=" in u]
        assert gotos_com_p == [], f"Não devia navegar ?p= com 1 página só: {page.gotos}"

    def test_multiplas_paginas_navega_todas(self):
        page = FakePage(
            pagina_to_cards={
                1: [_card("A"), _card("B")],
                2: [_card("C"), _card("D")],
                3: [_card("E")],
            },
            total_paginas=3,
        )
        cards = _run(scraper_autoavaliar._coletar_listagem_cidade(page, "Uberlandia", "MG", 7))
        assert {c["loteId"] for c in cards} == {"A", "B", "C", "D", "E"}
        # Navegou em ?p=2 e ?p=3 (p=1 é implícito)
        gotos_com_p = [u for u in page.gotos if "&p=" in u]
        assert any("&p=2" in u for u in gotos_com_p)
        assert any("&p=3" in u for u in gotos_com_p)

    def test_dedup_por_lote_id_entre_paginas(self):
        # Caso defensivo: se a mesma ID aparecer em 2 páginas (raro, mas o site
        # pode fazer isso em refreshes), a gente só conta 1 vez.
        page = FakePage(
            pagina_to_cards={
                1: [_card("A"), _card("B")],
                2: [_card("B"), _card("C")],  # B duplicado
            },
            total_paginas=2,
        )
        cards = _run(scraper_autoavaliar._coletar_listagem_cidade(page, "Uberlandia", "MG", 7))
        assert {c["loteId"] for c in cards} == {"A", "B", "C"}
        assert len(cards) == 3

    def test_horizonte_dias_filtra_apos_paginar(self):
        # Card com timer absurdo (1000h = 41d) deve ser filtrado pelo horizonte de 7d.
        page = FakePage(
            pagina_to_cards={
                1: [_card("DENTRO", timer="20:00:00:00"), _card("FORA", timer="999:00:00:00")],
            },
            total_paginas=1,
        )
        cards = _run(scraper_autoavaliar._coletar_listagem_cidade(page, "Uberlandia", "MG", 7))
        ids = {c["loteId"] for c in cards}
        assert "DENTRO" in ids
        assert "FORA" not in ids

    def test_limite_maximo_paginas_protege_contra_runaway(self):
        # Se o site reportar 999 páginas por bug/mudança, a gente não roda
        # 999 requisições — limita a um teto razoável.
        page = FakePage(
            pagina_to_cards={i: [_card(f"L{i}")] for i in range(1, 30)},
            total_paginas=999,
        )
        _run(scraper_autoavaliar._coletar_listagem_cidade(page, "Uberlandia", "MG", 7))
        gotos_com_p = [u for u in page.gotos if "&p=" in u]
        # Hoje Uberlândia tem 4 páginas. 20 é um teto generoso que pega
        # casos reais e corta qualquer coisa anômala.
        assert len(gotos_com_p) <= 20
