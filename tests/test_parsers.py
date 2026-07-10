"""Testes dos parsers de Auto Avaliar, usando fixtures reais coletadas via Chrome MCP."""

from datetime import datetime, timedelta

import pytest

from carros_sa.scraping.parsers import (
    _timer_para_fim_em,
    extrair_loja_do_card,
    is_laudo_pdf_url,
    parse_card_lines,
    parse_detalhe,
)


# =============================================================================
# Fixtures com dados REAIS coletados de b2b.autoavaliar.com.br em 2026-04-14
# =============================================================================

CARD_FIESTA_LINES = [
    "Uberlandia/MG",
    "33%",
    "anúncio destaque",
    "FORD",
    "FIESTA",
    "22.900,00",
    "1.6 SE HATCH 16V FLEX 4P MANUAL",
    "2012/2013",
    "FLEX",
    "MANUAL",
    "171.053",
    "12:32:19:10",
    "SN VW UDI MATRIZ",
    "GRUPO SAGA SEMINOVOS",
    "4.2",
    "AVALIE AGORA",
]

CARD_COMPASS_LINES = [
    "Uberlandia/MG",
    "24%",
    # sem "anúncio destaque"
    "JEEP",
    "COMPASS",
    "72.000,00",
    "2.0 16V FLEX LONGITUDE AUTOMATICO",
    "2018/2019",
    "FLEX",
    "AUTOMATICO",
    "137.577",
    "14:21:16:10",
    "SN VW UDI MATRIZ",
    "GRUPO SAGA SEMINOVOS",
    "4.2",
    "AVALIE AGORA",
]


def test_parse_card_fiesta_com_anuncio_destaque():
    lote = parse_card_lines(
        CARD_FIESTA_LINES,
        lote_id="21854782",
        href="https://b2b.autoavaliar.com.br/avaliacoes/saga/21854782/ford-fiesta",
    )
    assert lote.lote_id == "21854782"
    assert lote.marca == "Ford"
    assert "Fiesta" in lote.modelo
    assert lote.ano == 2013
    assert lote.km == 171_053
    assert lote.lance_atual == 22_900
    assert lote.origem_cidade == "Uberlandia"
    assert lote.origem_uf == "MG"
    assert lote.fim_em is not None


# =============================================================================
# Timer do card: HH:MM:SS:cs (não DD:HH:MM:SS)
# =============================================================================
# Gold real: data/detalhes/21867780.json coletado 2026-04-14T22:19:00Z mostra
# "FINALIZA EM 15/04/2026 as 16:00:00" com timer "17:40:36 :78".
# 22:19 UTC + 17h40m36s ≈ 2026-04-15 15:59 UTC, bate com 16:00 do site.


def test_timer_hh_mm_ss_com_centesimos_ignora_centesimos():
    """Timer `01:46:45:17` é 1h46m45s (centésimos descartados), não 1 dia + 46h."""
    agora = datetime(2026, 4, 16, 17, 13, 15)
    fim = _timer_para_fim_em(agora, "01:46:45:17")
    assert fim == agora + timedelta(hours=1, minutes=46, seconds=45)


def test_timer_hh_mm_ss_tres_partes():
    """Timer `17:40:36` é 17h40m36s (caso do detalhe do Haval)."""
    agora = datetime(2026, 4, 14, 22, 19, 0)
    fim = _timer_para_fim_em(agora, "17:40:36")
    # 14/04 22:19 + 17h40m36s = 15/04 15:59:36 — bate com "FINALIZA EM 15/04 as 16:00"
    assert fim == datetime(2026, 4, 15, 15, 59, 36)


def test_timer_longo_20h_nao_vira_20_dias():
    """`20:15:13:92` no card do Haval é 20h15m13s, não 20 dias (regressão)."""
    agora = datetime(2026, 4, 14, 22, 0, 0)
    fim = _timer_para_fim_em(agora, "20:15:13:92")
    delta = fim - agora
    # Deve estar no mesmo dia ou no dia seguinte — não uma semana+ no futuro.
    assert delta < timedelta(days=1, hours=1)


# =============================================================================
# Timer multi-dia: "1 dia, HH:MM:SS" / "2 dias, HH:MM:SS"
# =============================================================================
# Gold real: print do usuário em 2026-04-17 na página 4 de Uberlândia/MG
# mostra card com "1 dia, 18:52:23" — formato que o parser antigo ignorava,
# virando fim_em=None e sumindo da planilha.


def test_timer_um_dia_mais_horas():
    """`1 dia, 18:52:23` = 1d18h52m23s no futuro (gold da página 4 real)."""
    agora = datetime(2026, 4, 17, 12, 0, 0)
    fim = _timer_para_fim_em(agora, "1 dia, 18:52:23")
    assert fim == agora + timedelta(days=1, hours=18, minutes=52, seconds=23)


def test_timer_multiplos_dias_plural():
    """`2 dias, 05:10:00` = 2d5h10m — plural com 's'."""
    agora = datetime(2026, 4, 17, 0, 0, 0)
    fim = _timer_para_fim_em(agora, "2 dias, 05:10:00")
    assert fim == agora + timedelta(days=2, hours=5, minutes=10)


def test_timer_dias_sem_virgula_tambem_aceito():
    """Robustez: se o site variar e remover a vírgula, ainda casa."""
    agora = datetime(2026, 4, 17, 0, 0, 0)
    fim = _timer_para_fim_em(agora, "3 dias 12:00:00")
    assert fim == agora + timedelta(days=3, hours=12)


def test_parse_card_com_timer_multi_dia_extrai_fim_em():
    """Gold: card da página 4 com '1 dia, 18:52:23' deve popular fim_em."""
    lines = [
        "Uberlandia/MG",
        "VOLKSWAGEN",
        "SAVEIRO",
        "75.000,00",
        "1.6 MSI ROBUST CS 16V FLEX 2P MANUAL",
        "2024/2025",
        "FLEX",
        "MANUAL",
        "77.595",
        "1 dia, 18:52:23",
    ]
    agora = datetime(2026, 4, 17, 12, 0, 0)
    lote = parse_card_lines(lines, lote_id="99999", href="/x/99999/y", agora=agora)
    assert lote.fim_em is not None
    assert lote.fim_em == agora + timedelta(days=1, hours=18, minutes=52, seconds=23)


CARD_HAVAL_SHOWROOM = [
    "Uberlândia/MG", "SHOWROOM", "GWM", "HAVAL H6", "162.000,00",
    "1.5 HEV PREMIUM E-TRACTION", "2023/2024", "HIBRIDO", "AUTOMATICO",
    "39.731", "20:15:13:92", "EUROVILLE GWM UBERLÂNDIA", "Grupo Auto Japan", "4.2", "AVALIE AGORA",
]

CARD_EVOQUE_DOIS_PRECOS = [
    "Uberlândia/MG", "28%", "LAND ROVER", "RANGE ROVER", "EVOQUE",
    "93.000,00",        # preço original
    "82.000,00",        # preço atual (menor → é o que conta)
    "2.0 DYNAMIC 4WD 16V GASOLINA 4P AUTOMATICO",
    "2014/2015", "GASOLINA", "AUTOMATICO", "97.403",
    "12:15:13:92", "QUITCAR", "Revendedores Auto Avaliar", "AVALIE AGORA",
]

CARD_COMPASS_SEM_BADGE = [
    # Sem linha de % nem "SHOWROOM"
    "Uberlândia/MG", "JEEP", "COMPASS", "89.000,00",
    "2.0 16V FLEX LIMITED AUTOMATICO", "2019/2020", "FLEX", "AUTOMATICO",
    "70.891", "21:15:13:92", "BYD UBERLANDIA", "Grupo Aguia Branca", "3.6", "AVALIE AGORA",
]


def test_parse_card_default_agora_e_local_naive_nao_utc(monkeypatch):
    """`parse_card_lines(..., agora=None)` deve usar `datetime.now()` (LOCAL),
    não `datetime.utcnow()`. Toda a stack downstream (sheets/audit/laudo_audit)
    compara `lote.fim_em` contra `datetime.now()` — UTC default vazava lotes
    encerrados há até |offset| horas como ativos no Brasil (UTC-3 = 3h de gap).

    Estratégia do teste: patcheia `now` e `utcnow` na classe `datetime` que o
    parser usa, com valores deslocados por 3h. Se o parser chamou `now`, fim_em
    cai relativo ao "agora local"; se chamou `utcnow`, cai 3h adiantado.
    """
    from carros_sa.scraping import parsers as parsers_mod

    fake_local = datetime(2026, 5, 2, 14, 0, 0)   # "agora local" (BRT)
    fake_utc = datetime(2026, 5, 2, 17, 0, 0)     # "agora UTC" (= local + 3h)

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fake_local
        @classmethod
        def utcnow(cls):
            return fake_utc

    monkeypatch.setattr(parsers_mod, "datetime", _FakeDatetime)

    lote = parse_card_lines(
        CARD_HAVAL_SHOWROOM, "21867780",
        "https://b2b.autoavaliar.com.br/avaliacoes/autojapan/21867780/gwm-haval-h6",
    )
    # Timer "20:15:13:92" → +20h15m13s
    esperado_local = fake_local + timedelta(hours=20, minutes=15, seconds=13)
    assert lote.fim_em == esperado_local, (
        f"fim_em={lote.fim_em} esperava {esperado_local} (relativo a now() local). "
        "Se veio com 3h a mais, parser regrediu pra utcnow()."
    )


def test_parse_card_haval_com_badge_showroom():
    lote = parse_card_lines(CARD_HAVAL_SHOWROOM, "21867780",
                            "https://b2b.autoavaliar.com.br/avaliacoes/autojapan/21867780/gwm-haval-h6")
    assert lote.marca == "Gwm"
    assert "Haval" in lote.modelo
    assert lote.ano == 2024
    assert lote.km == 39_731
    assert lote.lance_atual == 162_000


def test_parse_card_evoque_dois_precos_pega_o_menor():
    lote = parse_card_lines(CARD_EVOQUE_DOIS_PRECOS, "21860924",
                            "https://b2b.autoavaliar.com.br/avaliacoes/revendedoresautoavaliar/21860924")
    # Marca com 2 palavras (LAND ROVER) + modelo de 2 palavras (RANGE ROVER EVOQUE)
    assert "Land" in lote.marca or "Land" in lote.modelo
    assert lote.ano == 2015
    assert lote.km == 97_403
    # Dois preços → usa o menor (82k)
    assert lote.lance_atual == 82_000
    # Versão não deve capturar o segundo preço
    assert "82.000" not in lote.modelo
    assert "DYNAMIC" in lote.modelo.upper()


def test_parse_card_compass_sem_badge_nem_fipe_pct():
    lote = parse_card_lines(CARD_COMPASS_SEM_BADGE, "21863111",
                            "https://b2b.autoavaliar.com.br/avaliacoes/kuruma/21863111")
    assert lote.marca == "Jeep"
    assert "Compass" in lote.modelo
    assert lote.ano == 2020
    assert lote.km == 70_891
    assert lote.lance_atual == 89_000


def test_parse_card_compass_sem_anuncio_destaque():
    lote = parse_card_lines(
        CARD_COMPASS_LINES,
        lote_id="21866082",
        href="https://b2b.autoavaliar.com.br/avaliacoes/saga/21866082/jeep-compass",
    )
    assert lote.marca == "Jeep"
    assert "Compass" in lote.modelo
    assert lote.ano == 2019
    assert lote.km == 137_577
    assert lote.lance_atual == 72_000


# =============================================================================
# parse_detalhe — testa o innerText real do Fiesta 21854782
# =============================================================================

DETALHE_FIESTA_BODY = """Ford Fiesta 1.6 Se Hatch 16v Flex 4p Manual | AutoAvaliar
SN VW UDI MATRIZ - Uberlandia/MG
GRUPO SAGA SEMINOVOS - ANÚNCIO Nº 21854782
fipe
33%
Veículo de Repasse
ULTIMA AVALIAÇÃO
22.900,00 / FIESTA
R$
AVALIE AGORA
400,00
800,00
1.200,00
Ative lances automáticos para este anúncio
R$
ATIVAR
LAUDO DO VEÍCULO
SIMULAR FRETE
FINALIZA EM 15/04/2026 as 08:11:03

12:30:56 :86

ANO
2012/2013
COMBUSTÍVEL
FLEX
KM
171.053
PORTAS
4P
COR
BRANCO
MOTOR
1596
PLACA
O**-***8
CÂMBIO
MANUAL
ORIGEM
IMPORTADO
ANUNCIANTE
DOCUMENTAÇÃO INFORMADA PELO ANUNCIANTE
STATUS DO LAUDO
Laudo não aprovado
Acessar
STATUS DO DOCUMENTO
Recibo/DOC em processo de transferência para a concessionária
PRAZO ESTIMADO DE TRANSFERÊNCIA
16 dias
PRAZO DE LIBERAÇÃO DO VEÍCULO
7 dias
OPCIONAIS
AIR BAG DUPLO
VIDRO ELÉTRICO
AR QUENTE
AR CONDICIONADO
VIDRO ELETRICO TRASEIRO
ALARME
VIDRO ELETRICO DIANTEIRO
ITENS AVALIADOS
Está ciente dos itens avaliados e das condições de venda?
Estou Ciente
REPROVADO ESTRUTURAL

1 PARCELA IPVA 2026 PAGA
IPVA 2025 PAGO
LEIA O ANÚNCIO COM ATENÇÃO, POIS NÃO TEMOS ACESSO PARA EFETUAR CANCELAMENTO
ATENÇÃO TODOS OS NOSSOS VEICULOS É UTILIZADO PROCURAÇÃO PUBLICA OUTORGADA A PESSOA JURIDICA
Talvez se interesse por
FIESTA 1.6 TITANIUM HATCH 16V FLEX 4P POWERSHIFT
2015/2016
FLEX
15:19:17
45.000,00
FIESTA 1.0 ROCAM 8V FLEX 4P MANUAL
2012/2013
FLEX
15:19:17
23.200,00
"""


def test_parse_detalhe_extrai_specs():
    flags = parse_detalhe(DETALHE_FIESTA_BODY, laudo_pdf_url="https://storage.googleapis.com/doc-b2b/8c9fbea96f.pdf")
    assert flags.specs["ANO"] == "2012/2013"
    assert flags.specs["KM"] == "171.053"
    assert flags.specs["COR"] == "BRANCO"
    assert flags.specs["CÂMBIO"] == "MANUAL"
    assert flags.specs["COMBUSTÍVEL"] == "FLEX"
    assert flags.specs["ORIGEM"] == "IMPORTADO"


def test_parse_detalhe_status_e_prazos():
    flags = parse_detalhe(DETALHE_FIESTA_BODY)
    assert flags.status_laudo == "Laudo não aprovado"
    assert "transferência" in (flags.status_documento or "").lower()
    assert flags.prazo_transferencia_dias == 16
    assert flags.prazo_liberacao_dias == 7


def test_parse_detalhe_reprovado_estrutural_early_exit():
    flags = parse_detalhe(DETALHE_FIESTA_BODY)
    assert flags.reprovado_estrutural is True
    assert flags.laudo_aprovado is False
    # Early exit primário é o reprovado estrutural (aparece antes)
    assert flags.early_exit == "reprovado_estrutural"


def test_parse_detalhe_itens_reprovados_case_insensitive():
    """Variações fora de caixa alta também são capturadas (parser tolerante)."""
    body = (
        "STATUS DO LAUDO\nAprovado com ressalvas\n"
        "Coluna B reprovada\n"
        "Porta dianteira direita Reparada\n"
        "Airbag substituído em 2020\n"
        "Paralama com dano estrutural\n"
        "LAUDO COMPLETO\n"
    )
    flags = parse_detalhe(body)
    itens = flags.itens_reprovados
    assert len(itens) == 4
    assert any("coluna b reprovada" in i.lower() for i in itens)
    assert any("reparada" in i.lower() for i in itens)
    assert any("substituído" in i.lower() for i in itens)
    assert any("dano estrutural" in i.lower() for i in itens)


def test_parse_detalhe_itens_reprovados_ignora_linhas_muito_longas():
    """Parágrafos longos contendo 'reprovado' no meio não são itens — só linhas compactas."""
    body = (
        "STATUS DO LAUDO\nAprovado\n"
        "Coluna A reprovada\n"  # curta → captura
        "Este parágrafo de 200+ caracteres descreve em detalhes a situação do veículo que foi reprovado em testes anteriores mas depois aprovado novamente após reparo completo e revisão.\n"  # longa → ignora
    )
    flags = parse_detalhe(body)
    assert len(flags.itens_reprovados) == 1
    assert "Coluna A reprovada" in flags.itens_reprovados[0]


def test_parse_detalhe_itens_reprovados_ignora_linhas_vazias_e_curtas():
    body = (
        "STATUS DO LAUDO\nAprovado\n"
        "\n\n\n"
        "ok\n"                 # muito curta < 5 chars, ignora
        "NÃO APROVADO\n"       # válida, 12 chars
    )
    flags = parse_detalhe(body)
    assert len(flags.itens_reprovados) == 1
    assert "NÃO APROVADO" in flags.itens_reprovados[0]


def test_parse_detalhe_opcionais_e_ipva():
    flags = parse_detalhe(DETALHE_FIESTA_BODY)
    assert "AIR BAG DUPLO" in flags.opcionais
    assert "AR CONDICIONADO" in flags.opcionais
    assert flags.ipva_pago is True


def test_parse_detalhe_similares_precos():
    flags = parse_detalhe(DETALHE_FIESTA_BODY)
    assert 45_000 in flags.similares_precos
    assert 23_200 in flags.similares_precos


def test_parse_detalhe_laudo_aprovado_sem_early_exit():
    """Caso feliz: laudo aprovado, sem reprovações, prazos curtos."""
    body = DETALHE_FIESTA_BODY.replace("Laudo não aprovado", "Laudo aprovado").replace(
        "REPROVADO ESTRUTURAL", "APROVADO ESTRUTURAL"
    )
    flags = parse_detalhe(body)
    assert flags.laudo_aprovado is True
    assert flags.reprovado_estrutural is False
    assert flags.early_exit is None
    assert flags.encerrado is False


def test_parse_detalhe_badge_arrematado_marca_encerrado():
    """Auto Avaliar coloca 'ARREMATADO' como badge isolado quando o lote fecha."""
    body = (
        "Ford Fiesta 1.6\n"
        "ARREMATADO\n"
        "KM\n171.053\n"
        "Laudo aprovado\n"
        "APROVADO ESTRUTURAL\n"
    )
    flags = parse_detalhe(body)
    assert flags.encerrado is True
    # early_exit prioriza "leilao_encerrado" pra economizar download de PDF
    assert flags.early_exit == "leilao_encerrado"


def test_parse_detalhe_frase_leilao_encerrado():
    """'Leilão encerrado' em texto livre também deve disparar a flag."""
    body = "Ford Fiesta\nEste leilão encerrado em 10/04/2026\nKM\n171.053\n"
    flags = parse_detalhe(body)
    assert flags.encerrado is True


def test_parse_detalhe_palavra_vendido_em_observacao_nao_falso_positiva():
    """'vendido' dentro de frase longa não é badge — não deve marcar encerrado."""
    body = (
        "Ford Fiesta\n"
        "O carro foi vendido anteriormente sem garantia segundo o relato do antigo proprietário.\n"
        "Laudo aprovado\n"
        "APROVADO ESTRUTURAL\n"
        "KM\n171.053\n"
    )
    flags = parse_detalhe(body)
    assert flags.encerrado is False


def test_parse_detalhe_redirect_veiculo_ja_vendido_marca_encerrado():
    """Quando um lote é arrematado mid-cron o AA redireciona pra tela vazia
    com "Este veículo já foi vendido...". Sem detectar, o pipeline salva
    `_laudo_sem_pdf` (conf 0.50) e o lote apodrece como "⚠ LAUDO NÃO
    CAPTURADO" na planilha — audit --strict quebra o cron.

    Guarda o fix estrutural do snapshot 2026-07-02 (3/17 incompletos).
    """
    body_redirect = (
        "14\n"
        "Com a Auto Avaliar os bons negócios não param.\n"
        "\n"
        "Este veículo já foi vendido mas confira as milhares de outras boas ofertas"
    )
    assert len(body_redirect) < 200  # abaixo do threshold DD5 → sem retry salva
    flags = parse_detalhe(body_redirect)
    assert flags.encerrado is True
    assert flags.early_exit == "leilao_encerrado"


def test_parse_detalhe_frase_ja_foi_vendido_pega_variacoes_de_acento():
    """O redirect vem com "Este veículo já foi vendido" mas queremos
    tolerar variações sem acento ("veiculo ja foi vendido")."""
    body = "cabecalho\nEste veiculo ja foi vendido antes.\n"
    flags = parse_detalhe(body)
    assert flags.encerrado is True


class TestDD11RedirectAaHeader:
    """DD11 (2026-07-10): AA tem MAIS variantes de página-vazia (~186 chars)
    além do "vendido" que DD10 pega. Todas compartilham o header "Com a
    Auto Avaliar os bons negócios não param.". Cron 2026-07-06 acumulou
    56/138 (40%) incompletos + cron 07-09 timeout 4h com dezenas dessas
    URLs presas em `_laudo_sem_pdf`/circuit-breaker.

    Guarda a combinação length<400 + header AA como âncora — bulletproof
    contra falso-positivo em página real de lote (20-50KB).
    """

    def test_body_curto_com_header_aa_sem_frase_vendido_marca_encerrado(self):
        """Variante "genérica" (~186 chars): header AA + convite pra listagem
        mas sem a frase específica "já foi vendido". DD10 não pegava; DD11 sim.
        """
        body = (
            "14\n"
            "Com a Auto Avaliar os bons negócios não param.\n"
            "\n"
            "Volte para a listagem e encontre novos veículos disponíveis"
        )
        assert len(body) < 400
        # DD10 (frase "já foi vendido") NÃO bate aqui — mas DD11 sim.
        from carros_sa.scraping.parsers import _RE_VEICULO_VENDIDO_REDIRECT
        assert _RE_VEICULO_VENDIDO_REDIRECT.search(body) is None
        flags = parse_detalhe(body)
        assert flags.encerrado is True
        assert flags.early_exit == "leilao_encerrado"

    def test_body_curto_com_header_aa_sem_acento_marca_encerrado(self):
        """Robustez a texto sem acento — mesmo padrão de tolerância do DD10."""
        body = (
            "5\n"
            "Com a Auto Avaliar os bons negocios nao param.\n"
            "Confira outros lotes"
        )
        flags = parse_detalhe(body)
        assert flags.encerrado is True

    def test_body_longo_com_header_aa_no_banner_nao_marca_encerrado(self):
        """Página real de lote pode ter o marketing text num banner de rodapé.
        Não deve marcar encerrado: length >400 falha o gate DD11 e nenhuma
        outra regra dispara. Isolamento length+header é a proteção.
        """
        body = (
            "Ford Fiesta\nANO\n2013\nKM\n80.000\nCOR\nBranco\nPLACA\nABC1234\n"
            "STATUS DO LAUDO\nLaudo aprovado com apontamento\n"
            + ("descrição detalhada do veículo com muitos detalhes e observações. " * 20)
            + "\nRodapé: Com a Auto Avaliar os bons negócios não param.\n"
        )
        assert len(body) > 400
        flags = parse_detalhe(body)
        assert flags.encerrado is False

    def test_body_curto_sem_header_aa_nao_marca_encerrado(self):
        """Body curto genérico (parser sendo exercido com fixture mínima)
        NÃO deve marcar encerrado — DD11 exige o header AA como âncora."""
        body = "Ford Fiesta\nANO\n2013\nKM\n80.000\n"
        assert len(body) < 400
        flags = parse_detalhe(body)
        assert flags.encerrado is False

    def test_e_pagina_redirect_aa_vazia_helper(self):
        """Cobertura direta do helper — length e header separados."""
        from carros_sa.scraping.parsers import _e_pagina_redirect_aa_vazia
        assert _e_pagina_redirect_aa_vazia("") is False
        assert _e_pagina_redirect_aa_vazia(None) is False  # type: ignore[arg-type]
        # Header sem length OK
        curto_ok = "Com a Auto Avaliar os bons negócios não param."
        assert _e_pagina_redirect_aa_vazia(curto_ok) is True
        # Length OK sem header
        curto_sem_header = "Alguma frase curta qualquer sem marketing text"
        assert _e_pagina_redirect_aa_vazia(curto_sem_header) is False
        # Length no threshold exato (400 = limite superior inclusivo)
        header_frag = "Com a Auto Avaliar os bons negócios não param. "
        pad = "x" * (400 - len(header_frag))
        assert len(header_frag + pad) == 400
        assert _e_pagina_redirect_aa_vazia(header_frag + pad) is True
        # Length > threshold → False mesmo com header
        assert _e_pagina_redirect_aa_vazia(header_frag + "x" * 500) is False


class TestIsLaudoPdfUrl:
    """Defesa contra decoys observados no DOM do Auto Avaliar.

    Contexto: antes da blindagem, 83/85 lotes ingeridos tinham `laudo_pdf_url`
    apontando pro "Relatório de Transparência e Igualdade Salarial" (link de
    rodapé institucional) hospedado em storage.googleapis.com — contaminando
    111/112 laudos extraídos para severidade=nenhuma/avarias=[]. A allowlist
    aqui garante que só URLs de laudo real passam, e que decoys conhecidos
    são rejeitados mesmo que a estrutura do site mude.
    """

    def test_url_relatorio_transparencia_e_rejeitada(self):
        """Link de rodapé do Auto Avaliar pro PDF de RH — não é laudo."""
        decoy = (
            "https://repo-site-aav-production.storage.googleapis.com/app/uploads/"
            "2025/10/Relatorio-de-Transparencia-e-igualdade-Salarial-"
            "de-Mulheres-e-Homens-2-o-semestre.pdf"
        )
        assert is_laudo_pdf_url(decoy) is False

    def test_url_listagem_com_query_entity_e_rejeitada(self):
        """URL da página de listagem (?entity=...) não é um PDF."""
        decoy = (
            "https://b2b.autoavaliar.com.br/avaliacoes?entity=hazul,allegro"
            "&models=hb20&report=yes"
        )
        assert is_laudo_pdf_url(decoy) is False

    def test_url_doc_b2b_googleapis_e_aceita(self):
        """Storage oficial dos laudos de lote — principal host observado."""
        ok = (
            "https://storage.googleapis.com/doc-b2b/laudos/12345/laudo.pdf"
            "?X-Goog-Signature=abc"
        )
        assert is_laudo_pdf_url(ok) is True

    def test_url_cdn_aav_e_aceita(self):
        """CDN do Auto Avaliar, host alternativo de laudos."""
        ok = "https://cdn-aav.autoavaliar.com.br/laudos/2026/abc.pdf"
        assert is_laudo_pdf_url(ok) is True

    def test_url_pdf_generico_com_laudo_no_path_e_aceito(self):
        """PDF em host não mapeado mas com 'laudo' no path — aceito."""
        ok = "https://exemplo.com/laudos/laudo-12345.pdf"
        assert is_laudo_pdf_url(ok) is True

    def test_url_pdf_generico_sem_laudo_e_rejeitada(self):
        """PDF qualquer sem indício de ser laudo — rejeita."""
        assert is_laudo_pdf_url("https://exemplo.com/manual.pdf") is False

    def test_url_sistemaprocemax_carbel_e_aceita(self):
        """Grupo carbel (apareceu 2026-05) usa ProceMax como sistema de laudos.

        URL real coletada dos lotes 22161767 e 22161768. Sem .pdf, sem 'laudo'
        no path — só passa pela allowlist explícita do host.
        """
        ok = "https://app.sistemaprocemax.com.br/files/report/7aa5c4aa-5bf8-463e-93ac-3302876e9698"
        assert is_laudo_pdf_url(ok) is True

    def test_url_vazia_ou_none(self):
        assert is_laudo_pdf_url(None) is False
        assert is_laudo_pdf_url("") is False


class TestExtrairLojaDoCard:
    """Últimas linhas do card (depois do timer) trazem loja e grupo anunciante.
    Gold com o Fiesta real + variações observadas nos 10 lotes de Uberlândia."""

    def test_gold_fiesta_real_retorna_loja_e_grupo(self):
        # CARD_FIESTA_LINES termina em 'SN VW UDI MATRIZ' / 'GRUPO SAGA SEMINOVOS' / '4.2' / 'AVALIE AGORA'
        loja = extrair_loja_do_card(CARD_FIESTA_LINES)
        assert loja == "SN VW UDI MATRIZ · GRUPO SAGA SEMINOVOS"

    def test_card_sem_rating_ainda_pega_loja_e_grupo(self):
        # Observado no Range Rover Evoque (21860924): sem rating antes do CTA
        lines = [
            "Uberlândia/MG", "28%", "LAND ROVER", "RANGE ROVER", "EVOQUE",
            "93.000,00", "82.000,00", "2.0 DYNAMIC 4WD 16V GASOLINA 4P AUTOMATICO",
            "2014/2015", "GASOLINA", "AUTOMATICO", "97.403", "12:15:13:92",
            "QUITCAR", "Revendedores Auto Avaliar", "AVALIE AGORA",
        ]
        assert extrair_loja_do_card(lines) == "QUITCAR · Revendedores Auto Avaliar"

    def test_card_sem_timer_retorna_none(self):
        # Defensivo: card malformado sem timer não deve explodir nem adivinhar
        assert extrair_loja_do_card(["FORD", "FIESTA", "AVALIE AGORA"]) is None

    def test_card_sem_nada_apos_timer_retorna_none(self):
        assert extrair_loja_do_card(["12:00:00", "AVALIE AGORA"]) is None

    def test_rating_e_cta_sao_ignorados(self):
        lines = ["12:00:00", "LOJA X", "GRUPO Y", "4.5", "AVALIE AGORA"]
        assert extrair_loja_do_card(lines) == "LOJA X · GRUPO Y"


def test_parse_detalhe_encerrado_tem_precedencia_sobre_estrutural():
    """Lote arrematado + reprovado estrutural → early_exit vira 'leilao_encerrado'.

    Faz diferença pro Orquestrador: encerrado é mais informativo pro usuário
    ('não vale mais pensar') que 'reprovado_estrutural' (que ainda sugere
    possibilidade de lance).
    """
    body = DETALHE_FIESTA_BODY + "\nARREMATADO\n"
    flags = parse_detalhe(body)
    assert flags.encerrado is True
    assert flags.reprovado_estrutural is True
    assert flags.early_exit == "leilao_encerrado"
