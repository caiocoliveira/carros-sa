"""Testes do audit de laudos travados (carros_sa/audit_laudos.py)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.audit_laudos import (
    LaudoTravado,
    listar_laudos_travados,
    resumo,
)
from carros_sa.models import AvaliacaoLote, LaudoCache, Lote


def _engine():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(e)
    return e


def _lote(
    lote_id: str,
    *,
    dias_atras: int = 0,
    fim_em_dias: int | None = 7,
    leilao: str = "autoavaliar",
) -> Lote:
    scraped = datetime.utcnow() - timedelta(days=dias_atras)
    fim_em = None if fim_em_dias is None else datetime.utcnow() + timedelta(days=fim_em_dias)
    return Lote(
        id=lote_id, leilao=leilao, url=f"https://x/{lote_id}",
        marca="Ford", modelo="Fiesta", ano=2013, km=100_000,
        lance_atual=15_000, fim_em=fim_em,
        origem_cidade="Uberlândia", origem_uf="MG",
        scraped_at=scraped,
    )


def _laudo(lote_id: str, confidence: float, *, dias_desde_extracao: int = 0) -> LaudoCache:
    return LaudoCache(
        lote_id=lote_id,
        avarias_json=[],
        severidade_geral="NENHUMA",
        motor_ok=True,
        documentacao="OK",
        categoria_veiculo="HATCH",
        confidence=confidence,
        modelo_llm="gemini-flash",
        extraido_em=datetime.utcnow() - timedelta(days=dias_desde_extracao),
    )


def test_lote_sem_laudo_cache_aparece_como_travado():
    e = _engine()
    with Session(e) as s:
        s.add(_lote("L1", dias_atras=1))
        s.commit()

        travados = listar_laudos_travados(s, "carros_uberlandia")

    assert len(travados) == 1
    assert travados[0].motivo == "sem_laudo_cache"
    assert travados[0].stuck is False  # 1 dia — ainda não é stuck


def test_lote_com_confidence_baixa_aparece_como_travado():
    e = _engine()
    with Session(e) as s:
        s.add(_lote("L1"))
        s.add(_laudo("L1", confidence=0.5))
        s.commit()

        travados = listar_laudos_travados(s, "carros_uberlandia")

    assert len(travados) == 1
    assert travados[0].motivo == "confidence_baixa"
    assert travados[0].confidence == 0.5


def test_lote_com_confidence_ok_nao_aparece():
    e = _engine()
    with Session(e) as s:
        s.add(_lote("L1"))
        s.add(_laudo("L1", confidence=0.7))
        # Sem AvaliacaoLote, mas vamos desligar esse check
        s.commit()

        travados = listar_laudos_travados(s, "carros_uberlandia", incluir_sem_avaliacao=False)

    assert len(travados) == 0


def test_lote_com_laudo_bom_mas_sem_avaliacao_eh_sinalizado():
    e = _engine()
    with Session(e) as s:
        s.add(_lote("L1"))
        s.add(_laudo("L1", confidence=0.8))
        s.commit()

        travados = listar_laudos_travados(s, "carros_uberlandia")

    assert len(travados) == 1
    assert travados[0].motivo == "sem_avaliacao"
    assert travados[0].tem_avaliacao is False


def test_lote_avaliado_nao_aparece():
    e = _engine()
    with Session(e) as s:
        s.add(_lote("L1"))
        s.add(_laudo("L1", confidence=0.8))
        s.add(AvaliacaoLote(
            empresa_id="carros_uberlandia", lote_id="L1",
            preco_alvo=10_000, preco_max=12_000, score_roi=0.1,
            fator_risco=1.0, fator_liquidez=1.0, margem_aplicada=0.15,
            frete_incluso=500, reforma_estimada=1000, taxas_leilao=300,
            preco_giro=15_000, preco_giro_fipe=14_000, fipe=14_000,
            justificativa="teste",
        ))
        s.commit()

        travados = listar_laudos_travados(s, "carros_uberlandia")

    assert len(travados) == 0


def test_stuck_quando_idade_maior_que_limiar():
    e = _engine()
    with Session(e) as s:
        s.add(_lote("L1", dias_atras=5))  # idade 5 > stuck_dias=3
        s.add(_laudo("L1", confidence=0.5, dias_desde_extracao=1))
        s.commit()

        travados = listar_laudos_travados(s, "carros_uberlandia", stuck_dias=3)

    assert len(travados) == 1
    assert travados[0].stuck is True
    assert travados[0].idade_dias == 5
    assert travados[0].ultima_tentativa_dias == 1


def test_lote_expirado_nao_aparece():
    """Lote com fim_em no passado não precisa mais de laudo — leilão acabou."""
    e = _engine()
    with Session(e) as s:
        s.add(_lote("L1", fim_em_dias=-2))
        s.commit()

        travados = listar_laudos_travados(s, "carros_uberlandia")

    assert len(travados) == 0


def test_lote_sintetico_historico_offline_ignorado():
    """Lotes importados via arrematado-import (leilao='historico_offline') são
    placeholders pra FK do Arrematado — não devem poluir o audit."""
    e = _engine()
    with Session(e) as s:
        s.add(_lote("L1", leilao="historico_offline"))
        s.commit()

        travados = listar_laudos_travados(s, "carros_uberlandia")

    assert len(travados) == 0


def test_ordenacao_prioriza_stuck_e_mais_antigos():
    e = _engine()
    with Session(e) as s:
        s.add(_lote("recente", dias_atras=1))
        s.add(_lote("velho", dias_atras=10))
        s.add(_lote("medio", dias_atras=5))
        s.commit()

        travados = listar_laudos_travados(s, "carros_uberlandia", stuck_dias=3)

    ids = [t.lote_id for t in travados]
    # Stuck primeiro (velho + medio), depois recente. Dentro de stuck, mais velho primeiro.
    assert ids == ["velho", "medio", "recente"]


def test_resumo_agrega_por_motivo():
    travados = [
        LaudoTravado("A", "Ford", "Ka", 2015, None, 5, None, False, True, "sem_laudo_cache"),
        LaudoTravado("B", "VW", "Gol", 2018, 0.5, 2, 1, False, False, "confidence_baixa"),
        LaudoTravado("C", "Fiat", "Argo", 2020, 0.8, 1, 1, False, False, "sem_avaliacao"),
    ]
    r = resumo(travados)
    assert r == {
        "total": 3, "stuck": 1,
        "sem_laudo_cache": 1, "confidence_baixa": 1, "sem_avaliacao": 1,
    }
