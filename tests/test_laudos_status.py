"""Testes do diagnóstico de laudos pendentes.

Cobre cada motivo de `MotivoPendencia` com um lote sintético + asserção do
detalhe gerado, pra que regressões na ordem de classificação ou na lista de
ações recomendadas quebrem o teste antes de chegar na planilha.

A lógica audita a mesma fronteira que o `SheetsExporter` (lote ativo =
`fim_em > now` e badge não-encerrado), então um lote excluído da planilha
NÃO entra no relatório de pendências — guard contra falso-positivo barulhento.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.laudos_status import (
    MotivoPendencia,
    auditar_laudos_pendentes,
    formatar_relatorio,
)


# URL-decoy real (Relatório de Transparência institucional). Exato fixture do
# bug de abril/2026 — se `is_laudo_pdf_url` for afrouxada, este teste quebra
# antes do dano chegar em produção.
DECOY_URL = (
    "https://repo-site-aav-production.storage.googleapis.com/app/uploads/"
    "2025/10/Relatorio-de-Transparencia.pdf"
)
URL_LAUDO_OK = "https://storage.googleapis.com/doc-b2b/laudos/L_OK/laudo.pdf"


def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


_SENTINELA = object()  # marca "deixa o default" diferente de None explícito.


def _lote(
    lote_id: str,
    *,
    raw_json: Optional[dict] = None,
    fim_em=_SENTINELA,
) -> Lote:
    return Lote(
        id=lote_id,
        leilao="auto_avaliar",
        url=f"https://autoavaliar.com.br/lote/{lote_id}",
        marca="Ford",
        modelo="Fiesta",
        ano=2017,
        km=50000,
        lance_atual=15000,
        # Sentinela permite passar fim_em=None explicitamente sem o `or`
        # substituir por now+2d (caso "lote fora de leilão ativo").
        fim_em=(datetime.now() + timedelta(days=2)) if fim_em is _SENTINELA else fim_em,
        origem_cidade="Uberlândia",
        origem_uf="MG",
        raw_json=raw_json or {},
        scraped_at=datetime.utcnow(),
    )


def _laudo(lote_id: str, confidence: float = 0.95) -> LaudoCache:
    return LaudoCache(
        lote_id=lote_id,
        avarias_json=[],
        severidade_geral="nenhuma",
        motor_ok=True,
        documentacao="ok",
        categoria_veiculo="hatch",
        confidence=confidence,
        modelo_llm="gemini-flash",
        custo_usd=0.0,
        extraido_em=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Cobertura de cada motivo
# ---------------------------------------------------------------------------

class TestCadaMotivo:
    def test_sem_detalhe_raspado(self):
        """raw_json sem chave 'detalhe' → orquestrador nem visitou o lote."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_SEM_DET", raw_json={}))
            session.commit()
            pendentes = auditar_laudos_pendentes(session)

        assert len(pendentes) == 1
        assert pendentes[0].motivo == MotivoPendencia.SEM_DETALHE_RASPADO
        assert "make triagem" in pendentes[0].acao

    def test_scraper_nao_achou_pdf_url(self):
        """detalhe raspado mas laudo_pdf_url=None → modal/seletor falhou."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote(
                "L_SEM_URL",
                raw_json={"detalhe": {"laudo_pdf_url": None, "status_laudo": "Sem laudo"}},
            ))
            session.commit()
            pendentes = auditar_laudos_pendentes(session)

        assert len(pendentes) == 1
        assert pendentes[0].motivo == MotivoPendencia.SCRAPER_NAO_ACHOU_PDF_URL
        assert "Sem laudo" in pendentes[0].detalhe
        assert "--somente-laudo-pendente" in pendentes[0].acao

    def test_url_decoy_persistido(self):
        """laudo_pdf_url no DB mas falha is_laudo_pdf_url() → make limpar-decoys."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote(
                "L_DECOY",
                raw_json={"detalhe": {"laudo_pdf_url": DECOY_URL}},
            ))
            session.commit()
            pendentes = auditar_laudos_pendentes(session)

        assert len(pendentes) == 1
        assert pendentes[0].motivo == MotivoPendencia.URL_DECOY_PERSISTIDO
        assert "limpar-decoys" in pendentes[0].acao

    def test_url_decoy_tem_prioridade_sobre_fallback(self):
        """Lote com decoy + LaudoCache fallback → reporta como URL_DECOY (acionável).

        Se reportasse como FALLBACK_SEM_PDF, o operador rodaria retry e cairia
        no decoy de novo; precisa rodar limpar-decoys antes.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote(
                "L_DECOY_FB",
                raw_json={"detalhe": {"laudo_pdf_url": DECOY_URL}},
            ))
            session.add(_laudo("L_DECOY_FB", confidence=0.5))
            session.commit()
            pendentes = auditar_laudos_pendentes(session)

        assert len(pendentes) == 1
        assert pendentes[0].motivo == MotivoPendencia.URL_DECOY_PERSISTIDO

    def test_fallback_sem_pdf(self):
        """URL ok + LaudoCache conf<0.6 → download/PDF rejeitado, retry."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote(
                "L_FALLBACK",
                raw_json={"detalhe": {"laudo_pdf_url": URL_LAUDO_OK}},
            ))
            session.add(_laudo("L_FALLBACK", confidence=0.5))
            session.commit()
            pendentes = auditar_laudos_pendentes(session)

        assert len(pendentes) == 1
        assert pendentes[0].motivo == MotivoPendencia.FALLBACK_SEM_PDF
        assert "0.50" in pendentes[0].detalhe
        assert "--somente-laudo-pendente" in pendentes[0].acao

    def test_url_ok_sem_laudo_cache_conta_como_fallback(self):
        """URL ok mas LaudoCache ausente → pipeline interrompido após scrape."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote(
                "L_INCOMPLETO",
                raw_json={"detalhe": {"laudo_pdf_url": URL_LAUDO_OK}},
            ))
            session.commit()
            pendentes = auditar_laudos_pendentes(session)

        assert len(pendentes) == 1
        assert pendentes[0].motivo == MotivoPendencia.FALLBACK_SEM_PDF
        assert "ausente" in pendentes[0].detalhe.lower()


# ---------------------------------------------------------------------------
# Lotes que NÃO devem virar pendência
# ---------------------------------------------------------------------------

class TestLotesQueNaoSaoPendencia:
    def test_laudo_com_confidence_alta_nao_e_pendencia(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote(
                "L_OK",
                raw_json={"detalhe": {"laudo_pdf_url": URL_LAUDO_OK}},
            ))
            session.add(_laudo("L_OK", confidence=0.95))
            session.commit()
            pendentes = auditar_laudos_pendentes(session)

        assert pendentes == []

    def test_lote_encerrado_por_timer_nao_aparece(self):
        """Lote com fim_em no passado → fora da planilha → fora do relatório."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote(
                "L_ENCERRADO",
                raw_json={},
                fim_em=datetime.now() - timedelta(hours=1),
            ))
            session.commit()
            pendentes = auditar_laudos_pendentes(session)

        assert pendentes == []

    def test_lote_encerrado_por_badge_nao_aparece(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote(
                "L_ARREMATADO",
                raw_json={"detalhe": {"encerrado": True}},
            ))
            session.commit()
            pendentes = auditar_laudos_pendentes(session)

        assert pendentes == []

    def test_lote_sem_fim_em_nao_aparece(self):
        """Mesma regra do export — sem fim_em o lote nem sai na planilha."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_SEM_FIM", raw_json={}, fim_em=None))
            session.commit()
            pendentes = auditar_laudos_pendentes(session)

        assert pendentes == []


# ---------------------------------------------------------------------------
# Filtro por empresa
# ---------------------------------------------------------------------------

class TestFiltroEmpresa:
    def test_empresa_id_restringe_a_lotes_com_avaliacao(self):
        """Sem empresa_id varre tudo; com empresa_id, só lotes que aparecem
        na planilha daquela empresa (têm AvaliacaoLote)."""
        engine = _engine_mem()
        with Session(engine) as session:
            # L_AVAL: tem avaliação pra empresa X → entra
            session.add(_lote("L_AVAL", raw_json={}))
            session.add(AvaliacaoLote(
                empresa_id="empresa_x",
                lote_id="L_AVAL",
                preco_alvo=10000, preco_max=15000,
                score_roi=0.2, fator_risco=1.0, fator_liquidez=1.0,
                margem_aplicada=0.15, frete_incluso=500,
                reforma_estimada=1000, taxas_leilao=1200,
                preco_giro=20000, preco_giro_fipe=20000,
                justificativa="fixture",
                criado_em=datetime.utcnow(),
            ))
            # L_ORFAO: ativo mas sem AvaliacaoLote → fora do filtro de empresa
            session.add(_lote("L_ORFAO", raw_json={}))
            session.commit()

            sem_filtro = auditar_laudos_pendentes(session)
            com_filtro = auditar_laudos_pendentes(session, empresa_id="empresa_x")

        ids_sem = {p.lote_id for p in sem_filtro}
        ids_com = {p.lote_id for p in com_filtro}
        assert ids_sem == {"L_AVAL", "L_ORFAO"}
        assert ids_com == {"L_AVAL"}


# ---------------------------------------------------------------------------
# Formatação do relatório
# ---------------------------------------------------------------------------

class TestFormatarRelatorio:
    def test_lista_vazia_nao_gera_linha(self):
        assert formatar_relatorio([]) == []

    def test_agrega_por_motivo_com_amostra_de_3_ids(self):
        engine = _engine_mem()
        with Session(engine) as session:
            for i in range(5):
                session.add(_lote(f"L00{i}", raw_json={}))
            session.commit()
            pendentes = auditar_laudos_pendentes(session)

        linhas = formatar_relatorio(pendentes)
        # 5 lotes, todos com mesmo motivo (sem_detalhe_raspado) → 1 linha
        assert len(linhas) == 1
        assert "5 lotes" in linhas[0]
        assert "+2" in linhas[0]  # mostra 3 IDs + " +2"

    def test_motivos_diferentes_geram_linhas_separadas(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_SEM_DET", raw_json={}))
            session.add(_lote(
                "L_DECOY",
                raw_json={"detalhe": {"laudo_pdf_url": DECOY_URL}},
            ))
            session.commit()
            pendentes = auditar_laudos_pendentes(session)

        linhas = formatar_relatorio(pendentes)
        assert len(linhas) == 2
        joined = "\n".join(linhas)
        assert "sem_detalhe_raspado" in joined
        assert "url_decoy_persistido" in joined
