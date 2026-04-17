"""Testes unitários de `carros_sa.tenancy`.

Foco em `EmpresaConfig.frete_para()` — lookup na tabela YAML — que só era
coberta indiretamente por testes de integração antes. Se a tabela quebrar,
qualquer teste aqui falha na hora.
"""

from __future__ import annotations

import pytest

from carros_sa.models import CategoriaVeiculo
from carros_sa.tenancy import (
    EmpresaConfig,
    PatioConfig,
    MargemConfig,
    carregar_empresa,
    listar_empresas,
)


def _empresa_base(**overrides) -> EmpresaConfig:
    base = dict(
        empresa_id="test",
        nome="Test Co",
        patio=PatioConfig(cidade="Uberlândia", uf="MG"),
        margem=MargemConfig(base=0.25, minima_absoluta=0.10),
        tabela_frete={
            "0-300": {"hatch": 800, "sedan": 800, "suv": 1200, "utilitario": 1500,
                      "picape": 1500, "outro": 1200},
            "300-600": {"hatch": 1400, "sedan": 1400, "suv": 1900, "utilitario": 2300,
                        "picape": 2300, "outro": 1900},
            "600-1000": {"hatch": 2200, "sedan": 2200, "suv": 2800, "utilitario": 3400,
                         "picape": 3400, "outro": 2800},
        },
        categorias_aceitas=[CategoriaVeiculo.HATCH, CategoriaVeiculo.SEDAN,
                            CategoriaVeiculo.SUV],
    )
    base.update(overrides)
    return EmpresaConfig(**base)


def test_frete_para_distancia_zero_e_gratis():
    """Lote na mesma cidade do pátio → comprador busca → frete R$ 0."""
    e = _empresa_base()
    assert e.frete_para(0, CategoriaVeiculo.HATCH) == 0
    assert e.frete_para(0, CategoriaVeiculo.SUV) == 0


def test_frete_para_faixas_basicas():
    e = _empresa_base()
    # 150km cai em 0-300
    assert e.frete_para(150, CategoriaVeiculo.HATCH) == 800
    assert e.frete_para(150, CategoriaVeiculo.SUV) == 1200
    # 400km cai em 300-600
    assert e.frete_para(400, CategoriaVeiculo.HATCH) == 1400
    # 700km cai em 600-1000
    assert e.frete_para(700, CategoriaVeiculo.PICAPE) == 3400


def test_frete_para_borda_inferior_inclusiva():
    """lo ≤ dist < hi — 0 entra em 0-300 se não for caso especial, 300 entra em 300-600."""
    e = _empresa_base()
    # Dist=300 cai na próxima faixa, não na anterior
    assert e.frete_para(300, CategoriaVeiculo.HATCH) == 1400
    # Dist=1 (não zero) cai em 0-300
    assert e.frete_para(1, CategoriaVeiculo.HATCH) == 800


def test_frete_para_alem_da_maior_faixa_extrapola_30pct():
    e = _empresa_base()
    # 2000km além da maior faixa (600-1000) → maiores × 1.3
    # hatch maior: 2200 × 1.3 = 2860
    assert e.frete_para(2000, CategoriaVeiculo.HATCH) == 2860


def test_carregar_empresa_uberlandia():
    """Smoke: o YAML real de Uberlândia carrega sem erro e expõe campos chave."""
    e = carregar_empresa("carros_uberlandia")
    assert e.empresa_id == "carros_uberlandia"
    assert e.patio.uf == "MG"
    assert e.raio_operacao_km == 150
    # Taxa Auto Avaliar = R$999 fixo
    assert e.taxa_leilao_fixa == 999
    # Custos operacionais decompostos existem e somam
    assert e.custos_operacionais is not None
    assert e.custos_operacionais.total == e.custo_op_fixo


def test_carregar_empresa_inexistente_falha():
    with pytest.raises(FileNotFoundError):
        carregar_empresa("nao_existe_xyz")


def test_listar_empresas_inclui_uberlandia_e_sp():
    empresas = listar_empresas()
    assert "carros_uberlandia" in empresas
    assert "empresa_fake_sp" in empresas
