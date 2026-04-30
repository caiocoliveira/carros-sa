"""`selecionar_pendentes` — filtro de candidatos a retry de laudo.

Este helper foi extraído do `scripts/reprocessar_lotes_do_db.py` pra ficar
testável sem Playwright. A garantia central que esses testes blindam:

  Quando o `--max-tentativas` do retry script faz N iterações, **a Nª
  iteração re-consulta o DB e só retorna lotes que AINDA estão pendentes**.
  Sem isso, o loop reprocessaria os mesmos lotes que já tinham ganho
  confidence>=0.6 na iteração anterior — desperdício de Playwright + risco
  de UNIQUE constraint no `LaudoCache`.

A combinação `somente_ativos + somente_laudo_pendente` é o uso de produção
(cron diário). Outras combinações são testadas isoladamente pra cobrir o
script de reprocessamento ad-hoc.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.laudo_reconciliacao import selecionar_pendentes


def _engine_mem():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _lote(lote_id: str, *, fim_em: Optional[datetime] = None) -> Lote:
    if fim_em is None:
        fim_em = datetime.now() + timedelta(days=2)
    return Lote(
        id=lote_id,
        leilao="auto_avaliar",
        url=f"https://autoavaliar.com.br/lote/{lote_id}",
        marca="Ford",
        modelo="Fiesta",
        ano=2017,
        km=50_000,
        lance_atual=15_000,
        fim_em=fim_em,
        origem_cidade="Uberlândia",
        origem_uf="MG",
        raw_json={},
        scraped_at=datetime.utcnow(),
    )


def _avaliacao(lote_id: str, empresa_id: str = "carros_uberlandia") -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id=empresa_id,
        lote_id=lote_id,
        preco_alvo=22_000,
        preco_max=25_000,
        score_roi=0.2,
        fator_risco=0.8,
        fator_liquidez=1.0,
        margem_aplicada=0.15,
        frete_incluso=1500,
        reforma_estimada=2000,
        taxas_leilao=2000,
        preco_giro=30_000,
        preco_giro_fipe=30_000,
        justificativa="ok",
        criado_em=datetime.utcnow(),
    )


def _laudo(lote_id: str, confidence: float) -> LaudoCache:
    return LaudoCache(
        lote_id=lote_id,
        avarias_json=[],
        severidade_geral="leve",
        motor_ok=True,
        documentacao="ok",
        categoria_veiculo="hatch",
        confidence=confidence,
        modelo_llm="gemini-flash",
        custo_usd=0.001,
        extraido_em=datetime.utcnow(),
    )


class TestSelecionarPendentes:
    def test_sem_filtros_retorna_todos_lotes(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L1"))
            session.add(_lote("L2"))
            session.commit()
            lotes = selecionar_pendentes(session, empresa_id="carros_uberlandia")
        ids = sorted(l.id for l in lotes)
        assert ids == ["L1", "L2"]

    def test_somente_ativos_descarta_lotes_com_fim_passado(self):
        engine = _engine_mem()
        passado = datetime.now() - timedelta(hours=2)
        futuro = datetime.now() + timedelta(days=1)
        with Session(engine) as session:
            session.add(_lote("L_VIVO", fim_em=futuro))
            session.add(_lote("L_MORTO", fim_em=passado))
            session.commit()
            lotes = selecionar_pendentes(
                session, empresa_id="carros_uberlandia", somente_ativos=True
            )
        assert [l.id for l in lotes] == ["L_VIVO"]

    def test_somente_ativos_descarta_lotes_sem_fim_em(self):
        """Lote sem `fim_em` pode ser SHOWROOM ou snapshot velho — não dá pra
        validar timer, então não considera ativo."""
        engine = _engine_mem()
        l = _lote("L_SEM_TIMER")
        l.fim_em = None
        with Session(engine) as session:
            session.add(l)
            session.commit()
            lotes = selecionar_pendentes(
                session, empresa_id="carros_uberlandia", somente_ativos=True
            )
        assert lotes == []

    def test_somente_laudo_pendente_filtra_confidence_baixa(self):
        """confidence<0.6 = `_laudo_sem_pdf` ou textual sem avarias = pendente."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_OK"))
            session.add(_laudo("L_OK", confidence=0.9))
            session.add(_lote("L_PENDENTE_FALLBACK"))
            session.add(_laudo("L_PENDENTE_FALLBACK", confidence=0.5))
            session.add(_lote("L_PENDENTE_SEM_LAUDO"))  # sem LaudoCache
            session.commit()
            lotes = selecionar_pendentes(
                session,
                empresa_id="carros_uberlandia",
                somente_laudo_pendente=True,
            )
        ids = sorted(l.id for l in lotes)
        assert ids == ["L_PENDENTE_FALLBACK", "L_PENDENTE_SEM_LAUDO"]

    def test_loop_de_retry_shrinka_ate_zero(self):
        """Garantia core: re-consultar entre iterações reflete confidence atualizado.

        Simula o cenário do `--max-tentativas`: na 1ª iteração, lote vem como
        pendente. Após o retry "subir" o confidence pra 0.9, a 2ª chamada de
        `selecionar_pendentes` não retorna mais o lote — loop encerra cedo
        sem reprocessar redundante.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L1"))
            session.add(_laudo("L1", confidence=0.5))
            session.commit()

            primeira = selecionar_pendentes(
                session, empresa_id="carros_uberlandia",
                somente_laudo_pendente=True,
            )
            assert [l.id for l in primeira] == ["L1"]

            # Simula o `_pipeline_lote` subindo confidence (caso feliz)
            laudo = session.get(LaudoCache, "L1")
            laudo.confidence = 0.9
            session.add(laudo)
            session.commit()

            segunda = selecionar_pendentes(
                session, empresa_id="carros_uberlandia",
                somente_laudo_pendente=True,
            )
            assert segunda == []

    def test_somente_sem_avaliacao_filtra_lotes_ja_avaliados(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_NOVO"))
            session.add(_lote("L_AVALIADO"))
            session.add(_avaliacao("L_AVALIADO"))
            session.commit()
            lotes = selecionar_pendentes(
                session,
                empresa_id="carros_uberlandia",
                somente_sem_avaliacao=True,
            )
        assert [l.id for l in lotes] == ["L_NOVO"]

    def test_somente_sem_avaliacao_respeita_empresa_id(self):
        """Lote avaliado pra empresa A continua pendente pra empresa B."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L1"))
            session.add(_avaliacao("L1", empresa_id="empresa_a"))
            session.commit()
            pendente_b = selecionar_pendentes(
                session, empresa_id="empresa_b", somente_sem_avaliacao=True,
            )
            avaliado_a = selecionar_pendentes(
                session, empresa_id="empresa_a", somente_sem_avaliacao=True,
            )
        assert [l.id for l in pendente_b] == ["L1"]
        assert avaliado_a == []

    def test_filtros_combinados_uso_de_producao(self):
        """`somente_ativos + somente_laudo_pendente`: o uso real do cron."""
        engine = _engine_mem()
        passado = datetime.now() - timedelta(hours=2)
        futuro = datetime.now() + timedelta(days=2)
        with Session(engine) as session:
            # Pendente E ativo → entra
            session.add(_lote("L_TARGET", fim_em=futuro))
            session.add(_laudo("L_TARGET", confidence=0.5))
            # Pendente mas encerrado → fora (timer passou)
            session.add(_lote("L_ENCERRADO", fim_em=passado))
            session.add(_laudo("L_ENCERRADO", confidence=0.5))
            # Ativo mas com laudo OK → fora
            session.add(_lote("L_OK", fim_em=futuro))
            session.add(_laudo("L_OK", confidence=0.9))
            session.commit()
            lotes = selecionar_pendentes(
                session,
                empresa_id="carros_uberlandia",
                somente_ativos=True,
                somente_laudo_pendente=True,
            )
        assert [l.id for l in lotes] == ["L_TARGET"]

    def test_max_lotes_trunca_resultado(self):
        engine = _engine_mem()
        with Session(engine) as session:
            for i in range(5):
                session.add(_lote(f"L{i}"))
            session.commit()
            lotes = selecionar_pendentes(
                session, empresa_id="carros_uberlandia", max_lotes=2,
            )
        assert len(lotes) == 2
