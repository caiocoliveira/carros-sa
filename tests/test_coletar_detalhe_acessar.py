"""Tests pra `coletar_detalhe` reforçada: trigger 'Acessar' quando o modal lazy
do botão "laudo" não rende a URL.

Cenário documentado em ROADMAP.md (workstream T): grupos kuruma/autus/trivel/
autojapan renderizam o link do laudo abaixo do label "STATUS DO LAUDO" como um
botão "Acessar" cujo texto NÃO contém "laudo" — o `_ABRIR_MODAL_LAUDO_JS` antigo
não pegava. Diagnóstico estático em data/detalhes/ mostrou 2/10 lotes com
status="Laudo Aprovado" mas laudo_pdf_url=None, todos do grupo kuruma.

Esses tests usam um FakePage in-memory que simula o JS do navegador via
strings-marker no código JS — zero dependência de Playwright real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pytest

from carros_sa.scraping import scraper_autoavaliar
from carros_sa.scraping.scraper_autoavaliar import (
    _laudo_existe_no_body,
    coletar_detalhe,
)


# =============================================================================
# Helpers
# =============================================================================


_BODY_TEMPLATE_LAUDO_APROVADO = """\
Volkswagen Gol 1.0 | AutoAvaliar
BYD UBERLANDIA - Uberlândia/MG
Grupo Aguia Branca - ANÚNCIO Nº 21862502
ULTIMA AVALIAÇÃO
27.500,00 / GOL
ANO
2013/2014
KM
112.397
DOCUMENTAÇÃO INFORMADA PELO ANUNCIANTE
STATUS DO LAUDO
Laudo Aprovado
Acessar
STATUS DO DOCUMENTO
Recibo/DOC em processo
"""

_BODY_TEMPLATE_SEM_LAUDO_NO_AA = """\
Carro X
ANÚNCIO Nº 99999999
ANO
2020/2021
DOCUMENTAÇÃO INFORMADA PELO ANUNCIANTE
STATUS DO DOCUMENTO
Pendente
"""

_URL_VALIDA = "https://storage.googleapis.com/doc-b2b/abcd1234.pdf"


@dataclass
class FakePage:
    """Stub mínimo de Playwright Page que simula a sequência de extração.

    `eval_responses` é uma lista de funções que respondem ao próximo evaluate()
    de extração (passada NÃO-body_text) — uma função por chamada.

    `reload_body_texts` é uma lista de body_texts a aplicar em cada `reload()`
    chamado. Default vazio = `reload` é no-op pro body_text (mantém o atual).
    Usado pra simular a retry de body_text curto no `coletar_detalhe`.
    """

    body_text: str
    eval_responses: List[Callable[[str], Any]]
    reload_body_texts: List[str] = field(default_factory=list)
    gotos: List[str] = field(default_factory=list)
    reloads: int = 0
    waits_ms: List[int] = field(default_factory=list)
    _eval_idx: int = 0

    async def goto(self, url: str, wait_until: Optional[str] = None, timeout: Optional[int] = None) -> None:
        self.gotos.append(url)

    async def reload(self, wait_until: Optional[str] = None, timeout: Optional[int] = None) -> None:
        if self.reloads < len(self.reload_body_texts):
            self.body_text = self.reload_body_texts[self.reloads]
        self.reloads += 1

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits_ms.append(ms)

    async def evaluate(self, js: str) -> Any:
        # Toda chamada de body.innerText devolve o body_text ATUAL (pode ter
        # mudado via reload entre chamadas).
        if "document.body.innerText" in js:
            return self.body_text
        # demais: consome eval_responses em ordem
        if self._eval_idx >= len(self.eval_responses):
            return None
        fn = self.eval_responses[self._eval_idx]
        self._eval_idx += 1
        return fn(js)


# =============================================================================
# _laudo_existe_no_body — função pura
# =============================================================================


class TestLaudoExisteNoBody:
    def test_aprovado_simples(self):
        bt = "STATUS DO LAUDO\nLaudo Aprovado\nAcessar\n"
        assert _laudo_existe_no_body(bt) is True

    def test_aprovado_com_apontamento(self):
        bt = "STATUS DO LAUDO\nLaudo aprovado com apontamento\n"
        assert _laudo_existe_no_body(bt) is True

    def test_nao_aprovado_ainda_conta_como_existente(self):
        # Laudo "Não aprovado" significa que TEM laudo (e descreve avaria);
        # early_exit do parse_detalhe descarta o lote, mas a URL é útil.
        bt = "STATUS DO LAUDO\nLaudo não aprovado\n"
        assert _laudo_existe_no_body(bt) is True

    def test_status_ausente(self):
        assert _laudo_existe_no_body("Carro sem status") is False

    def test_pendente_nao_conta(self):
        bt = "STATUS DO LAUDO\nPendente\n"
        assert _laudo_existe_no_body(bt) is False

    def test_body_vazio(self):
        assert _laudo_existe_no_body("") is False
        assert _laudo_existe_no_body(None) is False


# =============================================================================
# coletar_detalhe — fluxo completo
# =============================================================================


class TestColetarDetalhe:
    async def test_passada_1_dom_inicial_acha_url(self):
        """Caso ideal: 1ª evaluate() do _EXTRACT_PDF_URL_JS já retorna URL válida."""
        page = FakePage(
            body_text=_BODY_TEMPLATE_LAUDO_APROVADO,
            eval_responses=[lambda js: _URL_VALIDA],
        )
        body, url = await coletar_detalhe(page, "https://b2b.autoavaliar.com.br/x")
        assert url == _URL_VALIDA
        # Só 2 evaluates: body.innerText + 1ª passada de extração.
        assert page._eval_idx == 1

    async def test_passadas_laudo_falham_acessar_acha_url(self):
        """Cenário kuruma: passadas 1-4 (DOM + trigger "laudo") retornam None,
        passada 5 clica "Acessar" e passada de extração subsequente acha a URL.
        """
        # Sequência de respostas após body_text:
        #  1. _EXTRACT_PDF_URL_JS (passada 1) → None
        #  2. _ABRIR_MODAL_LAUDO_JS (passada 2 click) → False
        #  3. _EXTRACT_PDF_URL_JS (passada 2) → None
        #  4. _ABRIR_MODAL_LAUDO_JS (passada 3 click) → False
        #  5. _EXTRACT_PDF_URL_JS (passada 3) → None  (tentativa>=1 + clicou=False → break)
        #  6. _CLICK_ACESSAR_NEAR_LAUDO_JS (passada 5) → True (clicou)
        #  7. _EXTRACT_PDF_URL_JS (passada 5 extração) → URL!
        responses = [
            lambda js: None,    # 1
            lambda js: False,   # 2
            lambda js: None,    # 3
            lambda js: False,   # 4
            lambda js: None,    # 5
            lambda js: True,    # 6  ← Acessar clicou
            lambda js: _URL_VALIDA,  # 7  ← URL apareceu
        ]
        page = FakePage(
            body_text=_BODY_TEMPLATE_LAUDO_APROVADO,
            eval_responses=responses,
        )
        body, url = await coletar_detalhe(page, "https://b2b.autoavaliar.com.br/x")
        assert url == _URL_VALIDA
        # Todas as 7 evaluates foram consumidas.
        assert page._eval_idx == 7

    async def test_laudo_nao_existe_nao_roda_passadas_acessar(self):
        """Quando body_text NÃO tem STATUS DO LAUDO, não desperdiça passadas 5-7.

        Após passadas 1-4 falharem e nenhum click acontecer, a função retorna
        None sem tentar o trigger 'Acessar'.
        """
        responses = [
            lambda js: None,    # 1: extract → None
            lambda js: False,   # 2: ABRIR_MODAL click → False
            lambda js: None,    # 3: extract → None
            lambda js: False,   # 4: ABRIR_MODAL click → False
            lambda js: None,    # 5: extract → None (tentativa>=1, clicou=False → break)
        ]
        page = FakePage(
            body_text=_BODY_TEMPLATE_SEM_LAUDO_NO_AA,
            eval_responses=responses,
        )
        body, url = await coletar_detalhe(page, "https://b2b.autoavaliar.com.br/x")
        assert url is None
        # Só consumiu até a 5ª — passadas 6-7 (Acessar) NÃO rodaram.
        assert page._eval_idx == 5

    async def test_body_text_vazio_dispara_reload_e_recupera(self):
        """DD5: body_text inicialmente vazio (renderização atrasada / redirect)
        dispara reload, segunda passagem traz conteúdo e fluxo segue normal.

        Sem o retry, _laudo_existe_no_body retornaria False e passadas 5-8
        (Acessar + LLM) NÃO disparariam — lote viraria "⚠ LAUDO NÃO CAPTURADO"
        no próximo cron, e o ciclo se repete indefinidamente.
        """
        page = FakePage(
            body_text="",  # 1ª evaluate: página vazia
            reload_body_texts=[_BODY_TEMPLATE_LAUDO_APROVADO],
            # após reload, body_text vira o template completo, e a 1ª passada
            # de extração já acha a URL (caminho feliz pós-reload).
            eval_responses=[lambda js: _URL_VALIDA],
        )
        body, url = await coletar_detalhe(page, "https://b2b.autoavaliar.com.br/x")
        assert url == _URL_VALIDA
        assert body == _BODY_TEMPLATE_LAUDO_APROVADO
        assert page.reloads == 1, "deveria fazer 1 reload pra puxar body_text"

    async def test_body_text_vazio_em_todas_tentativas_retorna_sem_url(self):
        """Caso pessimista: 1ª passada + 2 reloads continuam com body_text vazio.

        Página deve ter dado throttle / Cloudflare contínuo. Retorna ("", None)
        sem queimar as passadas extras (não tem como `_laudo_existe_no_body`
        passar; passadas 5-8 não disparam — comportamento correto).
        """
        page = FakePage(
            body_text="",
            reload_body_texts=["", ""],  # ambos reloads também vazios
            eval_responses=[
                lambda js: None,    # 1: extract → None
                lambda js: False,   # 2: ABRIR_MODAL → False
                lambda js: None,    # 3: extract → None
                lambda js: False,   # 4: ABRIR_MODAL → False
                lambda js: None,    # 5: extract → None
            ],
        )
        body, url = await coletar_detalhe(page, "https://b2b.autoavaliar.com.br/x")
        assert url is None
        assert body == ""
        assert page.reloads == 2, "esgotou os 2 retries antes de desistir"
        # Passadas 5-8 (Acessar + LLM) NÃO devem rodar — _laudo_existe_no_body
        # devolve False corretamente quando body_text segue vazio.
        assert page._eval_idx <= 5

    async def test_reload_que_levanta_excecao_nao_quebra_fluxo(self):
        """RC3 / DD5: se `page.reload()` levantar (rede caiu, networkidle timeout,
        página fechou pelo throttle), o `except Exception` engole + loga, próxima
        tentativa tem chance, e no fim retorna ('', None) graciosamente — não
        propaga a exception nem corrompe state.

        Sem este guard, qualquer refactor futuro que troque `except: log` por
        `raise` quebraria silenciosamente o pipeline inteiro (cada lote individual
        rola pra exception path do `_pipeline_lote` em `orquestrador.py:609`).
        """
        @dataclass
        class FakePageReloadExplode(FakePage):
            async def reload(self, wait_until: Optional[str] = None, timeout: Optional[int] = None) -> None:
                self.reloads += 1
                raise TimeoutError("networkidle timeout simulado")

        page = FakePageReloadExplode(
            body_text="",
            eval_responses=[
                lambda js: None,    # 1: extract → None
                lambda js: False,   # 2: ABRIR_MODAL → False
                lambda js: None,    # 3: extract → None
                lambda js: False,   # 4: ABRIR_MODAL → False
                lambda js: None,    # 5: extract → None
            ],
        )
        body, url = await coletar_detalhe(page, "https://b2b.autoavaliar.com.br/x")
        assert url is None
        assert body == ""
        # Tentou os 2 reloads (não desistiu na 1ª exception)
        assert page.reloads == 2
        # Passadas 5-8 NÃO devem rodar — body segue vazio, _laudo_existe_no_body False
        assert page._eval_idx <= 5

    async def test_body_text_curto_mas_acima_do_minimo_nao_dispara_reload(self):
        """Body_text >=200 chars (página real renderizou OK) NÃO dispara reload.

        Garantia de que o gate `_MIN_BODY_TEXT_BYTES=200` só intervém em casos
        anômalos — caminho normal (~20-50KB) passa direto.
        """
        body_minimo_aceitavel = "x" * 200 + _BODY_TEMPLATE_LAUDO_APROVADO
        page = FakePage(
            body_text=body_minimo_aceitavel,
            eval_responses=[lambda js: _URL_VALIDA],
        )
        body, url = await coletar_detalhe(page, "https://b2b.autoavaliar.com.br/x")
        assert url == _URL_VALIDA
        assert page.reloads == 0, "body acima do mínimo não deveria reloadar"

    async def test_laudo_existe_mas_acessar_tambem_falha_retorna_none(self):
        """Caso pessimista: laudo existe no AA mas mesmo o trigger 'Acessar' falha.

        Função retorna body_text + None (operador resolve manualmente). Verifica
        que TODAS as passadas extras rodaram antes de desistir.
        """
        # 1-5: passadas com trigger laudo (mesmo padrão do test acima)
        # 6-7: passada 5 (Acessar click + extract)
        # 8-9: passada 6
        # 10-11: passada 7
        responses = [
            lambda js: None,    # 1
            lambda js: False,   # 2
            lambda js: None,    # 3
            lambda js: False,   # 4
            lambda js: None,    # 5
            lambda js: True,    # 6  Acessar clicou
            lambda js: None,    # 7  extract falhou
            lambda js: True,    # 8
            lambda js: None,    # 9
            lambda js: False,   # 10  já não acha botão
            lambda js: None,    # 11
        ]
        page = FakePage(
            body_text=_BODY_TEMPLATE_LAUDO_APROVADO,
            eval_responses=responses,
        )
        body, url = await coletar_detalhe(page, "https://b2b.autoavaliar.com.br/x")
        assert url is None
        assert body == _BODY_TEMPLATE_LAUDO_APROVADO
        # Todas as passadas extras rodaram.
        assert page._eval_idx >= 9
