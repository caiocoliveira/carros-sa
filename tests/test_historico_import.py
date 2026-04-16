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
