"""Testes do sinalizador Tese — classificação prescritiva baseada em Arrematado.

Usa SQLite in-memory com um histórico sintético pequeno (mas representativo)
pra não depender do carros_sa.db do worktree.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import Arrematado, Lote
from carros_sa.tools.tese import (
    HistoricoStat,
    SinalRuimConfig,
    Tese,
    TeseConfig,
    _chave_modelo,
    calcular_tese,
    carregar_historico_stat,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config_padrao() -> TeseConfig:
    """Config equivalente à do config/tese.yaml — mantido em sync manualmente."""
    return TeseConfig(
        ticket_min=12000,
        ticket_max=85000,
        km_min=30000,
        km_max=260000,
        compras_minimas_modelo=2,
        sinais_ruins=[
            SinalRuimConfig(
                nome="v6_gasolina_km_alto",
                motivo="V6 gasolina + km alto",
                patterns=["v6", "3.5", "santa fe 3.5", "cadenza"],
                excludes=["diesel"],
                km_minimo=150000,
            ),
            SinalRuimConfig(
                nome="diesel_km_alto",
                motivo="diesel + km muito alto",
                patterns=["diesel", "sd4"],
                km_minimo=200000,
            ),
            SinalRuimConfig(
                nome="nicho_sem_repeticao",
                motivo="modelo sem histórico de compra",
            ),
            SinalRuimConfig(
                nome="ticket_acima_teto",
                motivo="ticket acima do teto histórico",
            ),
            SinalRuimConfig(
                nome="eletrico_sem_revenda",
                motivo="elétrico sem revenda conhecida",
                patterns=["eletrico", "e-js1"],
                compras_minimas_modelo=5,
            ),
        ],
    )


@pytest.fixture
def hist_sintetico() -> HistoricoStat:
    """Histórico reduzido que espelha o real: Focus/Renegade/Onix recorrentes,
    Cadenza/Fortwo/Santa Fe nicho raro."""
    return HistoricoStat(
        modelos_contagem={
            "ford|focus": 7,
            "jeep|renegade": 7,
            "chevrolet|onix": 5,
            "fiat|toro": 5,
            "jeep|compass": 4,
            "nissan|sentra": 4,
            "hyundai|santa": 2,      # Santa Fe comprado 2x mas é V6
            "jac|e": 2,              # E-JS1 elétrico 2x (abaixo do mínimo 5)
            "kia|cadenza": 1,        # 1 só — entra em nicho
            "smart|fortwo": 1,       # 1 só — entra em nicho
        },
        ticket_min=12000,
        ticket_max=84000,
        total_compras=94,
    )


# ---------------------------------------------------------------------------
# Helpers de DB sintético (pra testar carregar_historico_stat)
# ---------------------------------------------------------------------------

def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _add_arr(sess: Session, marca: str, modelo: str, valor: int, i: int) -> None:
    lote = Lote(
        id=f"hist_{i:03d}",
        leilao="historico_offline",
        url="",
        marca=marca,
        modelo=modelo,
        ano=2020,
        km=100000,
        lance_atual=valor,
    )
    sess.add(lote)
    sess.add(Arrematado(
        empresa_id="uberlandia_mg",
        lote_id=lote.id,
        preco_real=valor,
        data=datetime(2025, 1, 1),
    ))


# ---------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------

class TestChaveModelo:
    def test_agrupa_variacoes_do_mesmo_modelo(self) -> None:
        assert _chave_modelo("Ford", "Focus 2.0 Titanium Plus") == "ford|focus"
        assert _chave_modelo("Ford", "FOCUS 2.0 TITANIUM FASTBACK") == "ford|focus"
        assert _chave_modelo("Ford", "Focus Sedan 2.0 SE") == "ford|focus"

    def test_marca_slug_normalizado(self) -> None:
        assert _chave_modelo("Land Rover", "Freelander 2") == "land_rover|freelander"

    def test_modelo_vazio_tolerado(self) -> None:
        assert _chave_modelo("VW", "") == "vw|"


class TestCarregarHistoricoStat:
    def test_agrega_do_banco(self) -> None:
        sess = _make_session()
        _add_arr(sess, "Ford", "Focus 2.0", 40000, 1)
        _add_arr(sess, "Ford", "Focus Titanium Plus", 42000, 2)
        _add_arr(sess, "Jeep", "Renegade 1.8 Longitude", 63000, 3)
        sess.commit()

        stat = carregar_historico_stat(sess)
        assert stat.total_compras == 3
        assert stat.contagem_modelo("Ford", "Focus 2.0 SE") == 2
        assert stat.contagem_modelo("Jeep", "Renegade Sport") == 1
        assert stat.ticket_min == 40000
        assert stat.ticket_max == 63000


class TestCalcularTeseNivelTipica:
    def test_modelo_recorrente_ticket_ok_km_ok(self, hist_sintetico, config_padrao) -> None:
        """Focus ×7, R$ 40k, 150k km — todos os 3 eixos batem."""
        tese = calcular_tese(
            marca="Ford",
            modelo="Focus 2.0 Titanium Plus",
            km=150000,
            lance_max=40000,
            historico=hist_sintetico,
            config=config_padrao,
        )
        assert tese.nivel == "tipica"
        assert "Focus" in tese.texto
        assert "×7" in tese.texto
        assert tese.texto.startswith("🟢")

    def test_onix_1_turbo_tipica(self, hist_sintetico, config_padrao) -> None:
        tese = calcular_tese(
            marca="Chevrolet", modelo="Onix 1.0 Turbo", km=80000, lance_max=60000,
            historico=hist_sintetico, config=config_padrao,
        )
        assert tese.nivel == "tipica"


class TestCalcularTeseNivelAtipica:
    def test_cadenza_v6_nicho_e_km_alto(self, hist_sintetico, config_padrao) -> None:
        """Cadenza 3.5 V6 + 1 compra só + km alta = 2 sinais → atípica."""
        tese = calcular_tese(
            marca="Kia", modelo="Cadenza 3.5 V6", km=170000, lance_max=62000,
            historico=hist_sintetico, config=config_padrao,
        )
        assert tese.nivel == "atipica"
        assert tese.texto.startswith("🔴")
        assert "V6" in tese.texto or "nicho" in tese.texto.lower() or "histórico" in tese.texto

    def test_range_rover_diesel_km_ultra_alto(self, hist_sintetico, config_padrao) -> None:
        """Diesel + km 290k + modelo sem histórico + ticket R$ 90k = 3+ sinais."""
        tese = calcular_tese(
            marca="Land Rover", modelo="Range Rover Vogue 3.0 Diesel",
            km=290000, lance_max=90000,
            historico=hist_sintetico, config=config_padrao,
        )
        assert tese.nivel == "atipica"
        assert "diesel" in tese.texto.lower()

    def test_ticket_acima_teto_mais_nicho(self, hist_sintetico, config_padrao) -> None:
        """Honda Civic R$ 95k: nicho (0 compras) + ticket acima teto = atípica."""
        tese = calcular_tese(
            marca="Honda", modelo="Civic 2.0", km=80000, lance_max=95000,
            historico=hist_sintetico, config=config_padrao,
        )
        assert tese.nivel == "atipica"


class TestCalcularTeseForaDaCurva:
    def test_santa_fe_v6_com_2_compras(self, hist_sintetico, config_padrao) -> None:
        """Santa Fe V6 + km alta = 1 sinal ruim. Modelo tem 2 compras = não dispara nicho.
        Resultado: fora_da_curva (não escalona pra atípica)."""
        tese = calcular_tese(
            marca="Hyundai", modelo="Santa Fe 3.5 V6", km=180000, lance_max=32000,
            historico=hist_sintetico, config=config_padrao,
        )
        assert tese.nivel == "fora_da_curva"
        assert tese.texto.startswith("🟡")

    def test_eletrico_abaixo_minimo_compras(self, hist_sintetico, config_padrao) -> None:
        """E-JS1 com 2 compras no histórico < 5 exigidas → sinal elétrico dispara."""
        tese = calcular_tese(
            marca="JAC", modelo="E-JS1 eletrico", km=80000, lance_max=61000,
            historico=hist_sintetico, config=config_padrao,
        )
        assert tese.nivel == "fora_da_curva"
        assert "elétrico" in tese.texto.lower()

    def test_modelo_novo_sem_historico(self, hist_sintetico, config_padrao) -> None:
        """Civic 0 compras, ticket/km OK: só nicho dispara → fora_da_curva."""
        tese = calcular_tese(
            marca="Honda", modelo="Civic 2.0", km=80000, lance_max=55000,
            historico=hist_sintetico, config=config_padrao,
        )
        assert tese.nivel == "fora_da_curva"
        # Verifica desduplicação — não deve ter "modelo novo (X)" duplicado com "sem histórico"
        assert tese.texto.count("histórico") == 1

    def test_km_ausente_nao_quebra(self, hist_sintetico, config_padrao) -> None:
        tese = calcular_tese(
            marca="Ford", modelo="Focus 2.0", km=None, lance_max=40000,
            historico=hist_sintetico, config=config_padrao,
        )
        assert tese.nivel == "fora_da_curva"
        assert "km ausente" in tese.texto


class TestDeteccaoExcludes:
    def test_diesel_nao_dispara_v6_gasolina(self, hist_sintetico, config_padrao) -> None:
        """Carro que casa 'v6' mas também 'diesel' não pode disparar v6_gasolina.
        Se disparasse, duplicaria com 'diesel_km_alto'."""
        tese = calcular_tese(
            marca="Land Rover", modelo="Range Rover V6 3.0 Diesel",
            km=250000, lance_max=90000,
            historico=hist_sintetico, config=config_padrao,
        )
        # Diesel sim, V6 não (porque 'diesel' está no excludes do v6_gasolina)
        assert tese.nivel == "atipica"
        motivos = " ".join(tese.razoes)
        # Conta quantas vezes aparece — o v6 isolado não pode aparecer
        assert motivos.lower().count("v6 gasolina") == 0
