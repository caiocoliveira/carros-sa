"""Gold tests do importador de histórico → Arrematado.

Cobre os 3 cenários do plano (splendid-dancing-alpaca):
  1. Importar Polo Track real do CSV — Lote sintético + Arrematado completos
  2. Linha "no pátio" (sem data_venda) — Arrematado parcial
  3. Idempotência — re-rodar não duplica
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from carros_sa.models import Arrematado, Empresa, Lote
from carros_sa.tools.historico_import import (
    HistoricoRow,
    importar_historico,
    lote_id_sintetico,
    parse_csv,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def session_isolada() -> Iterator[Session]:
    """SQLite em memória isolado por teste — não toca carros_sa.db."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def csv_polo_track() -> Iterator[Path]:
    """CSV com 1 linha do Polo Track real (vendido, dados completos)."""
    content = (
        "marca,modelo,ano,km,valor_compra,data_compra,custos_extras,valor_venda,data_venda,observacoes\n"
        "VW,Polo Track 1.0,2024,80000,52200,2025-11-13,4735,69400,2026-03-31,Auto Avaliar - vendeu na FIPE cheia\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(content)
        path = Path(f.name)
    yield path
    path.unlink()


@pytest.fixture
def csv_misto() -> Iterator[Path]:
    """CSV com 2 linhas: 1 vendida + 1 'no pátio' (sem data_venda)."""
    content = (
        "marca,modelo,ano,km,valor_compra,data_compra,custos_extras,valor_venda,data_venda,observacoes\n"
        "VW,Polo Track 1.0,2024,80000,52200,2025-11-13,4735,69400,2026-03-31,vendido\n"
        "BMW,750i,2015,,114890,2026-03-01,,188000,,no patio sugerido\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(content)
        path = Path(f.name)
    yield path
    path.unlink()


# =============================================================================
# Helper id sintético
# =============================================================================

def test_lote_id_sintetico_eh_deterministico_e_normaliza_acentos():
    a = lote_id_sintetico("carros_uberlandia", "VW", "Polo Track 1.0", 2024, 1)
    b = lote_id_sintetico("carros_uberlandia", "vw", "polo track 1.0", 2024, 1)
    assert a == b == "hist_carros_uberlandia_vw_polo_track_1_0_2024_001"

    # Acento normalizado
    c = lote_id_sintetico("carros_uberlandia", "Citroën", "C3", 2020, 1)
    assert "citroen" in c
    assert "ë" not in c


# =============================================================================
# Cenário 1 — Importar Polo Track real do CSV
# =============================================================================

def test_importar_polo_track_real(csv_polo_track, session_isolada):
    """Gold: linha do Polo cria Lote + Arrematado completos."""
    rows, erros = parse_csv(csv_polo_track)
    assert len(rows) == 1
    assert erros == []

    polo = rows[0]
    assert polo.marca == "VW"
    assert polo.modelo == "Polo Track 1.0"
    assert polo.ano == 2024
    assert polo.km == 80_000
    assert polo.valor_compra == 52_200
    assert polo.data_compra == datetime(2025, 11, 13)
    assert polo.custos_extras == 4_735
    assert polo.valor_venda == 69_400
    assert polo.data_venda == datetime(2026, 3, 31)

    # Cria empresa fake direto pra evitar carregar config
    session_isolada.add(Empresa(id="carros_uberlandia", nome="Test", config_yaml_path="x"))
    session_isolada.commit()

    # Hack: força o garantir_empresa a ser no-op
    from carros_sa.tools import historico_import
    monkey_orig = historico_import._garantir_empresa
    historico_import._garantir_empresa = lambda *a, **k: None
    try:
        result = importar_historico(rows, "carros_uberlandia", session_isolada)
    finally:
        historico_import._garantir_empresa = monkey_orig

    assert result.criados == 1
    assert result.atualizados == 0
    assert result.erros == []

    # Lote sintético criado com id determinístico
    lote = session_isolada.get(Lote, "hist_carros_uberlandia_vw_polo_track_1_0_2024_001")
    assert lote is not None
    assert lote.leilao == "historico_offline"
    assert lote.lance_atual == 52_200
    assert lote.km == 80_000
    assert lote.raw_json["origem"] == "import_historico"

    # Arrematado correspondente
    arrs = session_isolada.exec(select(Arrematado)).all()
    assert len(arrs) == 1
    arr = arrs[0]
    assert arr.lote_id == lote.id
    assert arr.empresa_id == "carros_uberlandia"
    assert arr.preco_real == 52_200
    assert arr.data == datetime(2025, 11, 13)
    assert arr.gastos_reforma_real == 4_735
    assert arr.vendido_por == 69_400
    assert arr.vendido_em == datetime(2026, 3, 31)


# =============================================================================
# Cenário 2 — Linha "no pátio" (sem data_venda)
# =============================================================================

def test_importar_no_patio_sem_data_venda(csv_misto, session_isolada):
    """Linha sem data_venda: Arrematado fica sem vendido_em e vendido_por."""
    rows, _ = parse_csv(csv_misto)
    assert len(rows) == 2

    session_isolada.add(Empresa(id="carros_uberlandia", nome="Test", config_yaml_path="x"))
    session_isolada.commit()
    from carros_sa.tools import historico_import
    historico_import._garantir_empresa = lambda *a, **k: None

    result = importar_historico(rows, "carros_uberlandia", session_isolada)
    assert result.criados == 2

    arrs = sorted(
        session_isolada.exec(select(Arrematado)).all(),
        key=lambda a: a.preco_real,
    )

    # Polo (vendido): tem vendido_em + vendido_por
    polo = arrs[0]
    assert polo.preco_real == 52_200
    assert polo.vendido_em is not None
    assert polo.vendido_por == 69_400

    # BMW (no pátio): sem vendido_em nem vendido_por (apesar do valor_venda existir como sugestão)
    bmw = arrs[1]
    assert bmw.preco_real == 114_890
    assert bmw.vendido_em is None
    assert bmw.vendido_por is None  # não preenchido sem data_venda real


# =============================================================================
# Cenário 3 — Idempotência
# =============================================================================

def test_idempotencia_nao_duplica(csv_polo_track, session_isolada):
    """Re-rodar a mesma importação atualiza linhas em vez de adicionar."""
    rows, _ = parse_csv(csv_polo_track)
    session_isolada.add(Empresa(id="carros_uberlandia", nome="Test", config_yaml_path="x"))
    session_isolada.commit()
    from carros_sa.tools import historico_import
    historico_import._garantir_empresa = lambda *a, **k: None

    # Primeira passada: 1 criado
    r1 = importar_historico(rows, "carros_uberlandia", session_isolada)
    assert r1.criados == 1

    # Segunda passada (mesmo CSV): 0 criados, 1 atualizado
    r2 = importar_historico(rows, "carros_uberlandia", session_isolada)
    assert r2.criados == 0
    assert r2.atualizados == 1

    # Total no DB continua 1 Lote + 1 Arrematado
    assert len(session_isolada.exec(select(Lote)).all()) == 1
    assert len(session_isolada.exec(select(Arrematado)).all()) == 1


# =============================================================================
# Cenário 4 — Formato decomposto (workstream HH-2, 2026-05-11+)
# =============================================================================
# CSV agora suporta 6 colunas decompostas pra custos pós-arremate:
# taxa_leilao_real, frete_real, transferencia_real, higienizacao_real,
# outros_extras_real, gastos_reforma_real. Permite calibrar cada bucket
# separadamente (HH-4, HH-5) em vez de usar o agregado `custos_extras` poluído.

@pytest.fixture
def csv_decomposto() -> Iterator[Path]:
    """CSV com 1 linha em formato decomposto (Fusion real, PR HH-2).

    Soma das decompostas = 4.397 (mesmo total do legacy `custos_extras`, mas
    agora separado: taxa AA 867 + frete 1200 + transf 580 + higi 550 + outros 0
    + reforma 1200).
    """
    content = (
        "marca,modelo,ano,km,valor_compra,data_compra,custos_extras,valor_venda,data_venda,"
        "taxa_leilao_real,frete_real,transferencia_real,higienizacao_real,outros_extras_real,gastos_reforma_real,observacoes\n"
        "Ford,Fusion 2.0 GTDI AWD,2014,,55500,2026-05-11,,,,"
        "867,1200,580,550,0,1200,Auto Arremate decomposto\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(content)
        path = Path(f.name)
    yield path
    path.unlink()


@pytest.fixture
def csv_misto_decomposto_e_legacy() -> Iterator[Path]:
    """CSV com 2 linhas: 1 legacy (Polo) + 1 decomposta (Fusion).

    Garante que ambos os formatos coexistem na mesma importação — operador
    pode migrar linha por linha sem precisar refazer tudo de uma vez.
    """
    content = (
        "marca,modelo,ano,km,valor_compra,data_compra,custos_extras,valor_venda,data_venda,"
        "taxa_leilao_real,frete_real,transferencia_real,higienizacao_real,outros_extras_real,gastos_reforma_real,observacoes\n"
        "VW,Polo Track 1.0,2024,80000,52200,2025-11-13,4735,69400,2026-03-31,,,,,,,legacy\n"
        "Ford,Fusion 2.0 GTDI AWD,2014,,55500,2026-05-11,,,,867,1200,580,550,0,1200,decomposto\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write(content)
        path = Path(f.name)
    yield path
    path.unlink()


class TestFormatoDecomposto:
    """Cobre o caminho novo de decomposição (HH-2)."""

    def test_extras_decompostos_property_true_quando_qualquer_bucket_preenchido(self):
        """Property `extras_decompostos` detecta migração parcial."""
        # Só reforma preenchida → ainda é decomposto
        r = HistoricoRow(marca="X", modelo="Y", ano=2020, valor_compra=10_000,
                          gastos_reforma_real=1_500)
        assert r.extras_decompostos is True

        # Nada decomposto, só legacy → não é decomposto
        r2 = HistoricoRow(marca="X", modelo="Y", ano=2020, valor_compra=10_000,
                           custos_extras=2_000)
        assert r2.extras_decompostos is False

        # Tudo zero/None → não é decomposto
        r3 = HistoricoRow(marca="X", modelo="Y", ano=2020, valor_compra=10_000)
        assert r3.extras_decompostos is False

    def test_total_extras_soma_decompostos(self):
        """`total_extras` soma os buckets quando decomposto, ignora custos_extras."""
        r = HistoricoRow(
            marca="Ford", modelo="Fusion", ano=2014, valor_compra=55_500,
            custos_extras=9_999,  # deveria ser IGNORADO (decomposto vence)
            taxa_leilao_real=867, frete_real=1_200, transferencia_real=580,
            higienizacao_real=550, outros_extras_real=0, gastos_reforma_real=1_200,
        )
        assert r.total_extras == 4_397  # 867+1200+580+550+0+1200

    def test_total_extras_fallback_legacy_quando_nada_decomposto(self):
        """Linha legacy: `total_extras` devolve `custos_extras` direto."""
        r = HistoricoRow(marca="VW", modelo="Polo", ano=2024,
                          valor_compra=52_200, custos_extras=4_735)
        assert r.total_extras == 4_735

    def test_total_extras_none_quando_compra_sem_despesa(self):
        """Snapshot do pátio (PR #84): sem custos_extras nem decomposto → None."""
        r = HistoricoRow(marca="BMW", modelo="750i", ano=2015, valor_compra=114_890)
        assert r.total_extras is None

    def test_reforma_real_efetiva_isola_reforma_quando_decomposto(self):
        """O ponto crítico do HH-2: calibrador de reforma deixa de receber
        número poluído (taxa+frete+higi+...) e passa a receber só reforma."""
        r_decomposto = HistoricoRow(
            marca="Ford", modelo="Fusion", ano=2014, valor_compra=55_500,
            taxa_leilao_real=867, frete_real=1_200, transferencia_real=580,
            higienizacao_real=550, gastos_reforma_real=1_200,
        )
        assert r_decomposto.reforma_real_efetiva == 1_200

        # Legacy: continua com o agregado (back-compat — irrecuperável retroativo)
        r_legacy = HistoricoRow(marca="VW", modelo="Polo", ano=2024,
                                 valor_compra=52_200, custos_extras=4_735)
        assert r_legacy.reforma_real_efetiva == 4_735

    def test_parse_csv_decomposto_popula_buckets(self, csv_decomposto):
        """parse_csv lê as 6 novas colunas e popula o HistoricoRow correto."""
        rows, erros = parse_csv(csv_decomposto)
        assert erros == []
        assert len(rows) == 1

        f = rows[0]
        assert f.marca == "Ford"
        assert f.valor_compra == 55_500
        assert f.taxa_leilao_real == 867
        assert f.frete_real == 1_200
        assert f.transferencia_real == 580
        assert f.higienizacao_real == 550
        assert f.outros_extras_real == 0
        assert f.gastos_reforma_real == 1_200
        assert f.custos_extras is None  # vazio no decomposto
        assert f.extras_decompostos is True

    def test_importar_decomposto_popula_arrematado_so_com_reforma(
        self, csv_decomposto, session_isolada
    ):
        """Linha decomposta: Arrematado.gastos_reforma_real recebe APENAS 1200
        (a coluna `gastos_reforma_real`), não 4397 (soma poluída)."""
        rows, _ = parse_csv(csv_decomposto)
        session_isolada.add(Empresa(id="carros_uberlandia", nome="Test", config_yaml_path="x"))
        session_isolada.commit()
        from carros_sa.tools import historico_import
        historico_import._garantir_empresa = lambda *a, **k: None

        importar_historico(rows, "carros_uberlandia", session_isolada)

        arrs = session_isolada.exec(select(Arrematado)).all()
        assert len(arrs) == 1
        assert arrs[0].gastos_reforma_real == 1_200  # SÓ reforma, não 4397

    def test_importar_csv_misto_legacy_e_decomposto_funcionam_juntos(
        self, csv_misto_decomposto_e_legacy, session_isolada
    ):
        """Operador pode migrar 1 linha por vez — ambos formatos coexistem."""
        rows, erros = parse_csv(csv_misto_decomposto_e_legacy)
        assert erros == []
        assert len(rows) == 2

        polo, fusion = rows
        assert polo.extras_decompostos is False
        assert polo.reforma_real_efetiva == 4_735  # legacy agregado
        assert fusion.extras_decompostos is True
        assert fusion.reforma_real_efetiva == 1_200  # só reforma

        session_isolada.add(Empresa(id="carros_uberlandia", nome="Test", config_yaml_path="x"))
        session_isolada.commit()
        from carros_sa.tools import historico_import
        historico_import._garantir_empresa = lambda *a, **k: None

        importar_historico(rows, "carros_uberlandia", session_isolada)

        arrs = sorted(
            session_isolada.exec(select(Arrematado)).all(),
            key=lambda a: a.preco_real,
        )
        assert arrs[0].gastos_reforma_real == 4_735  # Polo legacy (poluído, irrecuperável)
        assert arrs[1].gastos_reforma_real == 1_200  # Fusion decomposto (limpo)
