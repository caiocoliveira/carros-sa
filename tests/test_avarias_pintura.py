"""Gold tests do parser da seção PINTURA V1 do cautelar Auto Avaliar.

Caso real de referência: Mitsubishi L200 Triton 2022 (lote ranqueado #1 em
2026-04-25) com estrutura toda OK mas pintura ruim — 12 peças listadas na
seção PINTURA V1 com avarias (11 na faixa 5-20cm + 1 pequena <5cm).

Antes desse fix: extrator só lia o diagrama estrutural (página 2) + bloco
"Observações". Como o Triton tinha estrutura limpa e Observações vazias, a
pipeline retornava `avarias=[]`, severidade='nenhuma' e custo R$ 1.000 (piso
de imprevistos). O carro subia indevidamente no topo do ranking.

Depois do fix: 12 avarias detectadas, severidade MEDIA, custo > R$ 5k em MG.
"""

from pathlib import Path

import pytest

from carros_sa.agents.estimador_reforma import estimar
from carros_sa.agents.extrator_laudo import (
    _peca_pintura_para_parte,
    _severidade_pintura,
    extrair_avarias_pintura,
)
from carros_sa.models import (
    Avaria,
    CategoriaVeiculo,
    LaudoEstruturado,
    SeveridadeAvaria,
    StatusDocumentacao,
)
from carros_sa.tenancy import carregar_empresa

FIXTURE_TRITON = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "laudo_triton_pintura_5a383023d7.txt"
)


# =============================================================================
# Mapeamento de peças e severidades
# =============================================================================


def test_peca_coluna_dianteira_vira_coluna_a():
    assert _peca_pintura_para_parte("COLUNA DIANTEIRA DIREITA") == "coluna_a_direita"
    assert _peca_pintura_para_parte("COLUNA DIANTEIRA ESQUERDA") == "coluna_a_esquerda"


def test_peca_coluna_central_vira_coluna_b():
    assert _peca_pintura_para_parte("COLUNA CENTRAL DIREITA") == "coluna_b_direita"
    assert _peca_pintura_para_parte("COLUNA CENTRAL ESQUERDA") == "coluna_b_esquerda"


def test_peca_coluna_traseira_vira_coluna_c():
    assert _peca_pintura_para_parte("COLUNA TRASEIRA DIREITA") == "coluna_c_direita"
    assert _peca_pintura_para_parte("COLUNA TRASEIRA ESQUERDA") == "coluna_c_esquerda"


def test_peca_para_choque_sem_lado():
    assert _peca_pintura_para_parte("PARA-CHOQUE DIANTEIRO") == "para_choque_dianteiro"
    assert _peca_pintura_para_parte("PARA-CHOQUE TRASEIRO") == "para_choque_traseiro"
    # Variação de grafia sem hífen
    assert _peca_pintura_para_parte("PARA CHOQUE TRASEIRO") == "para_choque_traseiro"


def test_peca_paralama_com_posicao_e_lado():
    assert _peca_pintura_para_parte("PARA-LAMA DIANTEIRO ESQUERDO") == "paralama_dianteiro_esquerdo"
    assert _peca_pintura_para_parte("PARA-LAMA DIANTEIRO DIREITO") == "paralama_dianteiro_direito"


def test_peca_porta_com_posicao_e_lado():
    assert _peca_pintura_para_parte("PORTA DIANTEIRA ESQUERDA") == "porta_dianteira_esquerda"
    assert _peca_pintura_para_parte("PORTA TRASEIRA DIREITA") == "porta_traseira_direita"


def test_peca_lateral_traseira():
    assert _peca_pintura_para_parte("LATERAL TRASEIRA ESQUERDA") == "lateral_traseira_esquerda"
    assert _peca_pintura_para_parte("LATERAL TRASEIRA DIREITA") == "lateral_traseira_direita"


def test_peca_capo_e_tampa_e_teto():
    assert _peca_pintura_para_parte("CAPÔ DIANTEIRO") == "capo_tampa_motor"
    assert _peca_pintura_para_parte("TAMPA TRASEIRA") == "tampa_traseira"
    assert _peca_pintura_para_parte("TETO") == "teto"


def test_peca_desconhecida_retorna_none():
    assert _peca_pintura_para_parte("ITEM ESQUISITO XYZ") is None


def test_severidade_boas_condicoes_e_nenhuma():
    assert _severidade_pintura("PINTURA EM BOAS CONDIÇÕES") is None
    assert _severidade_pintura("PINTURA ORIGINAL") is None


def test_severidade_pequeno_ate_5cm_e_leve():
    assert _severidade_pintura("PEQUENO AMASSADO, RISCADO OU RALADO (ATÉ 5CM)") == SeveridadeAvaria.LEVE


def test_severidade_5_a_20cm_e_media():
    assert _severidade_pintura("AMASSADO, RISCADO OU RALADO (5CM A 20CM)") == SeveridadeAvaria.MEDIA


def test_severidade_acima_20cm_continua_media_nao_estrutural():
    """Pintura com risco grande continua sendo cosmético (não estrutural)."""
    assert _severidade_pintura("AMASSADO, RISCADO OU RALADO (ACIMA DE 20CM)") == SeveridadeAvaria.MEDIA


def test_severidade_repintada_e_media():
    assert _severidade_pintura("PEÇA REPINTADA") == SeveridadeAvaria.MEDIA


# =============================================================================
# Gold test — Triton com 12 peças na seção PINTURA V1
# =============================================================================


@pytest.mark.skipif(not FIXTURE_TRITON.exists(), reason="fixture do Triton ausente")
def test_extrair_avarias_pintura_triton_detecta_12_avarias():
    """Triton 2022 com estrutura limpa mas pintura ruim em 12 peças."""
    texto = FIXTURE_TRITON.read_text()
    avarias = extrair_avarias_pintura(texto)

    assert len(avarias) == 12, (
        f"Esperava 12 avarias (11 MEDIA + 1 LEVE), achou {len(avarias)}: "
        f"{[(a.parte, a.severidade.value) for a in avarias]}"
    )

    leves = [a for a in avarias if a.severidade == SeveridadeAvaria.LEVE]
    medias = [a for a in avarias if a.severidade == SeveridadeAvaria.MEDIA]
    assert len(leves) == 1, "esperava só o para-choque traseiro como LEVE (<5cm)"
    assert leves[0].parte == "para_choque_traseiro"
    assert len(medias) == 11

    # Peças esperadas (todas com damage 5-20cm exceto para-choque traseiro)
    partes = {a.parte for a in avarias}
    assert "tampa_traseira" in partes
    assert "coluna_a_esquerda" in partes
    assert "para_choque_dianteiro" in partes
    assert "paralama_dianteiro_esquerdo" in partes
    assert "paralama_dianteiro_direito" in partes
    assert "porta_dianteira_esquerda" in partes
    assert "porta_dianteira_direita" in partes
    assert "porta_traseira_esquerda" in partes
    assert "porta_traseira_direita" in partes
    assert "lateral_traseira_esquerda" in partes
    assert "lateral_traseira_direita" in partes


def test_pintura_em_boas_condicoes_nao_gera_avaria():
    """Carro sem nenhum defeito de pintura → lista vazia."""
    texto = """CAUTELAR V2 + PINTURA
PINTURA V1
• VISTA SUPERIOR
1 - CAPÔ DIANTEIRO:
PINTURA EM BOAS CONDIÇÕES
2 - TETO:
PINTURA EM BOAS CONDIÇÕES
3 - TAMPA TRASEIRA:
PINTURA EM BOAS CONDIÇÕES
OBSERVAÇÃO
.
"""
    assert extrair_avarias_pintura(texto) == []


def test_texto_sem_secao_pintura_retorna_vazio():
    """Laudo antigo sem módulo PINTURA V1 (só estrutural) → lista vazia."""
    texto = "CAUTELAR V2\nESTRUTURA VEICULAR\nLONGARINA DIANTEI. ESQ. : SEM AVARIAS APARENTES"
    assert extrair_avarias_pintura(texto) == []


def test_input_none_ou_vazio_retorna_vazio():
    assert extrair_avarias_pintura(None) == []
    assert extrair_avarias_pintura("") == []


# =============================================================================
# End-to-end — pipeline completo do Triton (estimador determinístico)
# =============================================================================


@pytest.mark.skipif(not FIXTURE_TRITON.exists(), reason="fixture do Triton ausente")
def test_estimar_triton_uberlandia_custo_alto_nao_piso_de_1k():
    """Bug regression: Triton com 12 avarias de pintura tem que ter custo
    bem maior que o piso de R$ 1.000 (que era o resultado antes do fix).
    """
    texto = FIXTURE_TRITON.read_text()
    avarias = extrair_avarias_pintura(texto)

    # Severidade consolidada das avarias só de pintura: MEDIA (sem ESTRUTURAL).
    laudo = LaudoEstruturado(
        avarias=avarias,
        severidade_geral=SeveridadeAvaria.MEDIA,
        motor_ok=True,
        documentacao=StatusDocumentacao.OK,
        categoria_veiculo=CategoriaVeiculo.PICAPE,
        confidence=0.7,
    )
    custo = estimar(laudo, carregar_empresa("carros_uberlandia"))

    # Bug regression: ANTES do fix, custo_total era R$ 1.000 (piso). Agora,
    # com 11 peças MEDIA + 1 LEVE, custo deve ficar bem maior.
    assert custo.custo_total > 5000, (
        f"Custo R${custo.custo_total} ainda baixo demais — fix do bug do Triton "
        f"deveria gerar > R$ 5k. Itens: {[(i.descricao, i.custo) for i in custo.itens]}"
    )

    # Severidade NÃO deve ser estrutural — coluna_a_esquerda foi marcada
    # MEDIA (pintura), não GRAVE/ESTRUTURAL, então adicional estrutural NÃO entra.
    descricoes = [i.descricao for i in custo.itens]
    assert not any("adicional estrutural" in d for d in descricoes)


@pytest.mark.skipif(not FIXTURE_TRITON.exists(), reason="fixture do Triton ausente")
def test_estimar_triton_inclui_pecas_novas_para_choque_lateral():
    """Confirma que as famílias novas (para_choque, lateral_traseira) entram
    no estimador determinístico em vez de cair no `default`."""
    texto = FIXTURE_TRITON.read_text()
    avarias = extrair_avarias_pintura(texto)
    laudo = LaudoEstruturado(
        avarias=avarias,
        severidade_geral=SeveridadeAvaria.MEDIA,
        motor_ok=True,
        documentacao=StatusDocumentacao.OK,
        categoria_veiculo=CategoriaVeiculo.PICAPE,
        confidence=0.7,
    )
    custo = estimar(laudo, carregar_empresa("carros_uberlandia"))

    descricoes_partes = " ".join(i.descricao for i in custo.itens)
    assert "para_choque_dianteiro" in descricoes_partes
    assert "para_choque_traseiro" in descricoes_partes
    assert "lateral_traseira_esquerda" in descricoes_partes
    assert "lateral_traseira_direita" in descricoes_partes


# =============================================================================
# Severidade combinada — visão "nenhuma" + pintura MEDIA → MEDIA (não ignora pintura)
# =============================================================================


def test_severidade_combinada_visao_nenhuma_nao_zera_avarias_de_pintura():
    """Bug do Triton: visão dizia 'nenhuma' (estrutura OK) e o código antigo
    sobrescrevia a severidade consolidada. Agora pegamos o MAIOR — então
    pintura MEDIA prevalece sobre visão NENHUMA.
    """
    from carros_sa.agents.extrator_laudo import (
        _RANK_SEVERIDADE,
        _severidade_consolidada,
        _severidade_rank,
    )

    avarias = [
        Avaria(parte="porta_dianteira_esquerda", severidade=SeveridadeAvaria.MEDIA),
        Avaria(parte="para_choque_traseiro", severidade=SeveridadeAvaria.LEVE),
    ]
    consolidada = _severidade_consolidada(avarias)
    visao = SeveridadeAvaria.NENHUMA

    final = max(visao, consolidada, key=_severidade_rank)
    assert final == SeveridadeAvaria.MEDIA


def test_severidade_combinada_visao_estrutural_domina_pintura_media():
    """Caso oposto: estrutura tem coluna reparada (visão diz ESTRUTURAL),
    pintura tem amassados (MEDIA). Final deve ser ESTRUTURAL — visão domina."""
    from carros_sa.agents.extrator_laudo import _severidade_rank

    avarias = [
        Avaria(parte="porta_dianteira_esquerda", severidade=SeveridadeAvaria.MEDIA),
    ]
    visao = SeveridadeAvaria.ESTRUTURAL
    consolidada = SeveridadeAvaria.MEDIA  # só uma porta MEDIA, não estrutural

    final = max(visao, consolidada, key=_severidade_rank)
    assert final == SeveridadeAvaria.ESTRUTURAL
