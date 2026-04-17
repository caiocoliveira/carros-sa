"""Testes do filtro de marcas fora do catálogo FIPE (motos, principalmente).

Impacto no pipeline: quando Auto Avaliar mistura motos na listagem (Triumph,
Harley etc), a FIPE /carros/ não tem catálogo → `_resolve_marca` levantava
LookupError que virava erro no `reprocessar_laudos.py` ou motivo_descarte
genérico no orquestrador. Agora detectamos ANTES de gastar LLM com laudo.
"""

from __future__ import annotations

import pytest

from carros_sa.tools.fipe import MARCAS_NON_FIPE, marca_fora_do_escopo_fipe


class TestMarcaForaDoEscopoFipe:
    @pytest.mark.parametrize("marca", [
        "Triumph", "triumph", "TRIUMPH",
        "Harley-Davidson", "Harley Davidson", "Harley",
        "Ducati", "Kawasaki", "Dafra", "Kasinski",
        "Royal Enfield", "Royal-Enfield",
        "MV Agusta", "KTM", "Piaggio", "Vespa",
    ])
    def test_marcas_de_moto_exclusivas_sao_detectadas(self, marca):
        assert marca_fora_do_escopo_fipe(marca), f"{marca!r} deveria ser non-FIPE"

    @pytest.mark.parametrize("marca", [
        # Fabricantes com carro E moto — não bloqueamos por marca; deixamos
        # o LookupError do FIPE tratar modelos de moto dessas marcas.
        "Honda", "Yamaha", "Suzuki", "BMW",
        # Fabricantes de carro puros.
        "Ford", "Volkswagen", "Chevrolet", "Fiat", "Toyota",
        "Renault", "Nissan", "Hyundai", "Jeep",
    ])
    def test_fabricantes_de_carro_nao_sao_bloqueados(self, marca):
        assert not marca_fora_do_escopo_fipe(marca), f"{marca!r} NÃO deveria ser non-FIPE"

    def test_string_vazia_ou_none_nao_quebra(self):
        assert marca_fora_do_escopo_fipe("") is False
        assert marca_fora_do_escopo_fipe(None) is False  # type: ignore[arg-type]

    def test_set_tem_ao_menos_as_motos_conhecidas_do_mercado_br(self):
        # Sanity: conjunto não pode estar vazio nem faltando entries críticas.
        essenciais = {"triumph", "harley", "ducati", "kawasaki"}
        assert essenciais.issubset(MARCAS_NON_FIPE)
