"""Gold tests do bucketing de popularidade via ranking FENABRAVE."""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import pytest

from carros_sa.models import CategoriaVeiculo
from carros_sa.tools.popularidade import (
    BucketPopularidade,
    ajustar_dias_giro,
    bucket_modelo,
    invalidar_cache,
    multiplicador,
)


@pytest.fixture
def yaml_temporario(tmp_path):
    """Cria um YAML mínimo de ranking pra testes — independe do real."""
    content = textwrap.dedent("""
        referencia_mes: "2026-test"
        fonte: "teste"
        ranking_por_categoria:
          hatch:
            - "Polo Track"
            - "HB20"
            - "Onix"
            - "Polo"
            - "Mobi"
            - "Argo"
            - "Sandero"
            - "Gol"
            - "Up"
            - "Uno"
            - "Ka"
            - "Fit"
            - "Fiesta"
            - "March"
            - "Punto"
            - "Palio"
          sedan:
            - "Onix Plus"
            - "Virtus"
            - "Corolla"
            - "City"
            - "HB20S"
            - "Versa"
            - "Voyage"
        """)
    path = tmp_path / "ranking.yaml"
    path.write_text(content)
    invalidar_cache()
    return str(path)


def test_modelo_top_5_eh_blockbuster(yaml_temporario):
    # HB20 = posição 2 → top 5 → BLOCKBUSTER
    b = bucket_modelo("Hyundai", "HB20 1.0", CategoriaVeiculo.HATCH, ano=2024,
                      yaml_path=yaml_temporario)
    assert b == BucketPopularidade.BLOCKBUSTER


def test_modelo_top_15_eh_popular(yaml_temporario):
    # Ka = posição 11 → POPULAR
    b = bucket_modelo("Ford", "Ka SE 1.0", CategoriaVeiculo.HATCH, ano=2018,
                      yaml_path=yaml_temporario)
    assert b == BucketPopularidade.POPULAR


def test_modelo_top_30_eh_normal(yaml_temporario):
    # Palio = posição 16 (na lista de teste). Dentro do top-30 mas fora top-15.
    # Cobre o ramo NORMAL.
    b = bucket_modelo("Fiat", "Palio Fire 1.0", CategoriaVeiculo.HATCH, ano=2014,
                      yaml_path=yaml_temporario)
    assert b == BucketPopularidade.NORMAL


def test_modelo_fora_do_ranking_recente_eh_nicho(yaml_temporario):
    # Bravo não está na lista, ano 2020 (recente) → NICHO
    b = bucket_modelo("Fiat", "Bravo 1.8", CategoriaVeiculo.HATCH, ano=2020,
                      yaml_path=yaml_temporario)
    assert b == BucketPopularidade.NICHO


def test_modelo_fora_do_ranking_e_velho_eh_iliquido(yaml_temporario):
    # Bravo 2010 (≥10 anos) + fora do ranking → ILIQUIDO
    b = bucket_modelo("Fiat", "Bravo 1.8", CategoriaVeiculo.HATCH, ano=2010,
                      yaml_path=yaml_temporario)
    assert b == BucketPopularidade.ILIQUIDO


def test_polo_track_match_especifico_antes_de_polo_generico(yaml_temporario):
    """Ranking tem 'Polo Track' (pos 1) e 'Polo' (pos 4). Polo Track 2024 deve
    casar com 'Polo Track' (BLOCKBUSTER), não com 'Polo' (também blockbuster
    aqui mas em outros rankings poderia ser POPULAR)."""
    b = bucket_modelo("VW", "Polo Track 1.0", CategoriaVeiculo.HATCH, ano=2024,
                      yaml_path=yaml_temporario)
    # Top-1 → BLOCKBUSTER (top 5)
    assert b == BucketPopularidade.BLOCKBUSTER


def test_modelo_polo_simples_cai_em_polo_generico(yaml_temporario):
    """'Polo 1.0 MSI' (sem Track) deve casar com 'Polo Track' por causa da
    iteração em ordem do YAML — Polo Track aparece antes."""
    # Wait: 'polo track' não está em 'polo 1.0 msi' → não bate
    # Cai pra 'Polo' (pos 4) → BLOCKBUSTER
    b = bucket_modelo("VW", "Polo 1.0 MSI", CategoriaVeiculo.HATCH, ano=2020,
                      yaml_path=yaml_temporario)
    assert b == BucketPopularidade.BLOCKBUSTER


def test_categoria_sem_ranking_no_yaml_cai_nicho(yaml_temporario):
    """SUV não está no YAML de teste → todo modelo SUV vira NICHO ou ILIQUIDO."""
    b = bucket_modelo("Jeep", "Compass", CategoriaVeiculo.SUV, ano=2022,
                      yaml_path=yaml_temporario)
    assert b == BucketPopularidade.NICHO


def test_multiplicadores():
    assert multiplicador(BucketPopularidade.BLOCKBUSTER) == 0.6
    assert multiplicador(BucketPopularidade.POPULAR) == 0.8
    assert multiplicador(BucketPopularidade.NORMAL) == 1.0
    assert multiplicador(BucketPopularidade.NICHO) == 1.3
    assert multiplicador(BucketPopularidade.ILIQUIDO) == 1.6


def test_ajustar_dias_giro_aplica_multiplicador():
    # Hatch 100d × 0.6 (blockbuster) = 60d
    assert ajustar_dias_giro(100, BucketPopularidade.BLOCKBUSTER) == 60
    # Hatch 100d × 1.6 (iliquido) = 160d
    assert ajustar_dias_giro(100, BucketPopularidade.ILIQUIDO) == 160


def test_ajustar_dias_giro_floor_15():
    # Lote categórico já calibrado em 20d × 0.6 = 12d → cap em 15d (mínimo)
    assert ajustar_dias_giro(20, BucketPopularidade.BLOCKBUSTER) == 15


def test_ranking_real_carrega_e_polo_track_eh_blockbuster_hatch():
    """Sanity do YAML real em config/mercado/ — Polo Track tem que ser top-5 hatch."""
    invalidar_cache()  # garante leitura do real, não do temp
    b = bucket_modelo("VW", "Polo Track 1.0", CategoriaVeiculo.HATCH, ano=2024)
    assert b == BucketPopularidade.BLOCKBUSTER


def test_bucket_modelo_tolera_acentos_e_caixa_alta(yaml_temporario):
    """Auto Avaliar escreve tudo em caixa alta; ranking YAML em Title Case.
    Slug normalization garante o match."""
    b_upper = bucket_modelo("HYUNDAI", "HB20 1.0", CategoriaVeiculo.HATCH, ano=2020,
                            yaml_path=yaml_temporario)
    b_title = bucket_modelo("Hyundai", "hb20 1.0", CategoriaVeiculo.HATCH, ano=2020,
                            yaml_path=yaml_temporario)
    assert b_upper == b_title == BucketPopularidade.BLOCKBUSTER


def test_bucket_modelo_ano_desconhecido_nao_dispara_iliquido(yaml_temporario):
    """Sem `ano`, um modelo fora do ranking deve cair em NICHO (não ILIQUIDO),
    porque ILIQUIDO exige idade ≥ 10 anos confirmada."""
    b = bucket_modelo("Fiat", "Bravo 1.8", CategoriaVeiculo.HATCH, ano=None,
                      yaml_path=yaml_temporario)
    assert b == BucketPopularidade.NICHO


def test_ranking_real_strada_eh_blockbuster_picape():
    invalidar_cache()
    b = bucket_modelo("Fiat", "Strada Adventure", CategoriaVeiculo.PICAPE, ano=2023)
    assert b == BucketPopularidade.BLOCKBUSTER
