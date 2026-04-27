"""Guards do auditor de laudos.

Protege contra a regressão de abril/2026: 2/10 lotes da listagem de Uberlândia
ficaram com `laudo_pdf_url=None` mesmo com `STATUS DO LAUDO: Laudo Aprovado`
no DOM. Causa: o JS `_ABRIR_MODAL_LAUDO_JS` só clicava elementos com texto
contendo "laudo", e o trigger real ("Acessar") não tinha essa palavra.

Esses testes garantem:

1. **Detecção** — `auditar()` identifica os 3 modos de defeito:
   `laudo_aprovado_sem_url`, `url_invalida_no_db`, `laudo_cache_baixa_confianca`.

2. **Não-falso-positivo** — lotes legitimamente sem laudo ("Laudo não aprovado",
   "Sem laudo", ou só ingeridos da listagem sem detalhe) NÃO viram defeito.

3. **Hygiene check do DB real** — se `carros_sa.db` existir, falha o
   `make test` se houver QUALQUER defeito tipo `laudo_aprovado_sem_url`.
   Skippa em ambientes sem DB.

4. **Body-text fixture** — usando os 10 lotes reais raspados na triagem de
   2026-04-14, parse_detalhe + auditar reproduz exatamente o gap observado
   (2 defeitos: 21862502 e 21865772). Esse teste é o que faz a próxima
   versão do scraper provar que o fix funciona quando reprocessar a listagem.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from carros_sa.models import LaudoCache, Lote
from carros_sa.scraping.parsers import parse_detalhe
from scripts.auditar_laudos import (
    DefeitoLaudo,
    ResultadoAuditoria,
    _status_indica_laudo_existente,
    auditar,
)


URL_LAUDO_OK = "https://storage.googleapis.com/doc-b2b/abc123.pdf"
URL_DECOY = (
    "https://repo-site-aav-production.storage.googleapis.com/app/uploads/"
    "2025/10/Relatorio-de-Transparencia.pdf"
)


def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _lote(
    lote_id: str,
    *,
    laudo_pdf_url: Optional[str] = None,
    status_laudo: Optional[str] = "Laudo Aprovado",
    com_detalhe: bool = True,
) -> Lote:
    raw_json: dict = {}
    if com_detalhe:
        raw_json["detalhe"] = {
            "laudo_pdf_url": laudo_pdf_url,
            "status_laudo": status_laudo,
        }
    return Lote(
        id=lote_id,
        leilao="auto_avaliar",
        url=f"https://b2b.autoavaliar.com.br/avaliacoes/saga/{lote_id}/foo",
        marca="Ford",
        modelo="Fiesta",
        ano=2017,
        km=50000,
        lance_atual=15000,
        fim_em=datetime.now() + timedelta(days=3),
        origem_cidade="Uberlândia",
        origem_uf="MG",
        raw_json=raw_json,
        scraped_at=datetime.utcnow(),
    )


def _laudo_cache(lote_id: str, confidence: float) -> LaudoCache:
    return LaudoCache(
        lote_id=lote_id,
        avarias_json=[],
        severidade_geral="nenhuma",
        motor_ok=True,
        documentacao="ok",
        categoria_veiculo="outro",
        confidence=confidence,
        modelo_llm="gemini-flash",
        custo_usd=0.0,
        extraido_em=datetime.utcnow(),
    )


class TestStatusIndicaLaudoExistente:
    """Heurística que classifica `status_laudo` em 'tem laudo' vs 'não tem'."""

    @pytest.mark.parametrize("status", [
        "Laudo Aprovado",
        "Laudo aprovado",
        "Laudo aprovado com apontamento",
        "LAUDO APROVADO",
    ])
    def test_aprovado_indica_existente(self, status):
        assert _status_indica_laudo_existente(status) is True

    @pytest.mark.parametrize("status", [
        "Laudo não aprovado",
        "Laudo nao aprovado",
        "SEM LAUDO",
        "Sem laudo",
        None,
        "",
    ])
    def test_nao_aprovado_ou_ausente_nao_e_existente(self, status):
        assert _status_indica_laudo_existente(status) is False


class TestAuditarDeteccao:
    """Detecção dos três modos de defeito."""

    def test_laudo_aprovado_sem_url_detectado(self):
        """Caso central: status diz aprovado mas URL ausente — defeito do scraper."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L1", laudo_pdf_url=None, status_laudo="Laudo Aprovado"))
            session.commit()

            res = auditar(session)

        assert res.total_lotes == 1
        defeitos = res.por_motivo("laudo_aprovado_sem_url")
        assert len(defeitos) == 1
        assert defeitos[0].lote_id == "L1"
        assert defeitos[0].status_laudo == "Laudo Aprovado"
        assert "Acessar" in defeitos[0].detalhe or "scraper" in defeitos[0].detalhe.lower()

    def test_url_decoy_detectada(self):
        """URL preenchida mas inválida (Relatório de Transparência) → defeito tipo decoy."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L2", laudo_pdf_url=URL_DECOY, status_laudo="Laudo Aprovado"))
            session.commit()

            res = auditar(session)

        defeitos = res.por_motivo("url_invalida_no_db")
        assert len(defeitos) == 1
        assert defeitos[0].lote_id == "L2"
        assert defeitos[0].url_atual == URL_DECOY

    def test_laudo_cache_baixa_confianca_detectada(self):
        """LaudoCache stale (confidence < 0.6) com URL OK → reportado pra reprocessar."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L3", laudo_pdf_url=URL_LAUDO_OK, status_laudo="Laudo Aprovado"))
            session.add(_laudo_cache("L3", confidence=0.5))
            session.commit()

            res = auditar(session)

        defeitos = res.por_motivo("laudo_cache_baixa_confianca")
        assert len(defeitos) == 1
        assert defeitos[0].lote_id == "L3"
        assert defeitos[0].confidence == 0.5

    def test_lote_completo_nao_e_defeito(self):
        """Status aprovado + URL válida + LaudoCache forte → zero defeito."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L4", laudo_pdf_url=URL_LAUDO_OK, status_laudo="Laudo Aprovado"))
            session.add(_laudo_cache("L4", confidence=0.92))
            session.commit()

            res = auditar(session)

        assert res.tem_defeito is False
        assert res.defeitos == []

    def test_laudo_nao_aprovado_nao_e_defeito_se_sem_url(self):
        """Lote legitimamente reprovado/sem laudo NÃO entra na lista de defeitos."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L5", laudo_pdf_url=None, status_laudo="Laudo não aprovado"))
            session.add(_lote("L6", laudo_pdf_url=None, status_laudo="SEM LAUDO"))
            session.add(_lote("L7", laudo_pdf_url=None, status_laudo=None))
            session.commit()

            res = auditar(session)

        assert res.tem_defeito is False, f"Esperado zero defeito, achou: {res.defeitos}"

    def test_lote_sem_detalhe_e_ignorado(self):
        """Lote só ingerido da listagem (sem raw_json.detalhe) NÃO vira defeito."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L8", com_detalhe=False))
            session.commit()

            res = auditar(session)

        assert res.total_lotes == 1
        assert res.defeitos == []

    def test_um_lote_so_aparece_uma_vez_mesmo_com_multiplos_defeitos(self):
        """Lote com URL decoy + LaudoCache fraco aparece só em 1 motivo (o 1o detectado)."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L9", laudo_pdf_url=URL_DECOY, status_laudo="Laudo Aprovado"))
            session.add(_laudo_cache("L9", confidence=0.5))
            session.commit()

            res = auditar(session)

        # url_invalida_no_db é detectado primeiro; cache fraco fica suprimido
        # pra não duplicar o mesmo lote no report.
        ids_reportados = {d.lote_id for d in res.defeitos}
        assert ids_reportados == {"L9"}
        # Mas pelo menos um motivo foi flag-ado
        assert len(res.defeitos) == 1


class TestAuditarFixturesReais:
    """Reproduz o gap exato observado em 2026-04-14 com os 10 lotes da listagem."""

    LOTES_DIR = Path("data/detalhes")

    def test_dois_lotes_da_listagem_uberlandia_caem_em_laudo_aprovado_sem_url(self):
        """Gold test: dos 10 lotes raspados em 2026-04-14, 21862502 e 21865772
        têm `STATUS DO LAUDO: Laudo Aprovado` no DOM mas laudo_pdf_url=None.

        Se o scraper for consertado e re-rodar contra esses lotes, a fixture
        em `data/detalhes/` será atualizada e este teste falha — sinal verde
        de que o fix pegou. Quando isso acontecer, atualizar a fixture e o
        contador esperado pra 0.
        """
        if not self.LOTES_DIR.exists():
            pytest.skip("data/detalhes/ ausente — fixture só roda no repo principal")

        engine = _engine_mem()
        with Session(engine) as session:
            for cache_path in sorted(self.LOTES_DIR.glob("*.json")):
                d = json.loads(cache_path.read_text())
                lote_id = d["lote_id"]
                body_text = d.get("body_text", "")
                pdf_url = d.get("laudo_pdf_url")
                flags = parse_detalhe(body_text, laudo_pdf_url=pdf_url)
                lote = Lote(
                    id=lote_id,
                    leilao="auto_avaliar",
                    url=d.get("url", ""),
                    marca="Desconhecida",
                    modelo="Desconhecido",
                    ano=2015,
                    km=0,
                    lance_atual=0,
                    fim_em=datetime.now() + timedelta(days=3),
                    origem_cidade="Uberlândia",
                    origem_uf="MG",
                    raw_json={"detalhe": {
                        "laudo_pdf_url": flags.laudo_pdf_url,
                        "status_laudo": flags.status_laudo,
                    }},
                    scraped_at=datetime.utcnow(),
                )
                session.add(lote)
            session.commit()

            res = auditar(session)

        defeitos = res.por_motivo("laudo_aprovado_sem_url")
        ids_defeituosos = sorted(d.lote_id for d in defeitos)
        # Snapshot conhecido — quando o scraper for consertado e a fixture
        # atualizada, atualizar este assertion (idealmente pra `[]`).
        assert ids_defeituosos == ["21862502", "21865772"], (
            f"Mudou o conjunto de lotes com laudo_aprovado_sem_url: {ids_defeituosos}. "
            "Se o scraper foi consertado, atualize a fixture data/detalhes/ rodando "
            "o scraper com login real e atualize este teste."
        )


class TestHygieneDBRealLaudo:
    """Guard de DB real: falha o `make test` se houver lote com `laudo_aprovado_sem_url`.

    Skippa graciosamente em CI/máquinas sem DB. O ponto é: se o operador
    rodar a triagem em produção e algum lote escapar do scraper, o próximo
    `make test` acende o vermelho.

    Defeitos tipo `url_invalida_no_db` já têm guard próprio em
    `test_decoy_laudo_guard.py::TestHygieneDBReal`. `laudo_cache_baixa_confianca`
    é tolerado aqui pq pode ser temporário (entre triagens — o orquestrador
    reprocessa naturalmente via short-circuit de confidence < 0.6).
    """

    def _db_path(self) -> Optional[Path]:
        explicit = os.environ.get("CARROS_SA_DB")
        if explicit:
            p = Path(explicit)
            return p if p.exists() else None
        for candidate in (
            Path("carros_sa.db"),
            Path(__file__).resolve().parents[1] / "carros_sa.db",
        ):
            if candidate.exists():
                return candidate
        return None

    def test_db_atual_nao_tem_laudo_aprovado_sem_url(self):
        db_path = self._db_path()
        if db_path is None:
            pytest.skip("sem carros_sa.db — hygiene check só roda com DB presente")

        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        with Session(engine) as session:
            res = auditar(session)

        defeitos = res.por_motivo("laudo_aprovado_sem_url")
        if defeitos:
            amostra = [d.lote_id for d in defeitos[:5]]
            pytest.fail(
                f"Encontrados {len(defeitos)} lotes com STATUS DO LAUDO=Aprovado "
                f"mas sem laudo_pdf_url no DB (total: {res.total_lotes}). "
                f"Amostra: {amostra}. "
                f"Re-rode `make triagem` — o pipeline tem short-circuit pra reprocessar "
                f"automaticamente lotes com LaudoCache.confidence < 0.6."
            )
