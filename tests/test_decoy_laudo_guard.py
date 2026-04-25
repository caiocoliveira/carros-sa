"""Guards contra a regressão do decoy de laudo PDF.

Protege contra o bug de abril/2026: o seletor `_EXTRACT_PDF_URL_JS` do scraper
capturava o link institucional do rodapé ("Relatório de Transparência Salarial")
como se fosse o laudo do lote — contaminando 74+ lotes na base com URL-decoy
persistida em `raw_json["detalhe"]["laudo_pdf_url"]`.

O que esses testes garantem:

1. `is_laudo_pdf_url()` rejeita a família conhecida de decoys — defesa de
   entrada (já coberto em test_parsers.py::TestIsLaudoPdfUrl; importamos
   algumas URLs aqui pra o teste de hygiene bater no mesmo critério).

2. `limpar_decoys()` remove a URL envenenada do raw_json E derruba o
   LaudoCache correspondente — defesa de correção (auto-fix).

3. **Hygiene check do DB real** — se `CARROS_SA_DB` apontar pra um arquivo
   existente (default `carros_sa.db`), o teste falha se encontrar QUALQUER
   decoy persistido. Força o operador a rodar `make limpar-decoys` antes
   de dar um release como verde. Skippa gracefully em CI/máquinas sem DB.

Esse módulo é o que transforma "descobrir de novo daqui 6 meses" em
"pytest vermelho na próxima execução".
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from carros_sa.models import LaudoCache, Lote
from carros_sa.scraping.parsers import is_laudo_pdf_url
from scripts.limpar_decoys_laudo import limpar_decoys

# URL-decoy exata observada em produção (abril/2026, 74 lotes contaminados).
DECOY_URL_TRANSPARENCIA = (
    "https://repo-site-aav-production.storage.googleapis.com/app/uploads/"
    "2025/10/Relatorio-de-Transparencia-e-igualdade-Salarial-"
    "de-Mulheres-e-Homens-2-o-semestre.pdf"
)
URL_LAUDO_OK = "https://storage.googleapis.com/doc-b2b/abc123.pdf"


def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _lote(
    lote_id: str, laudo_pdf_url: Optional[str], *,
    marca: str = "Ford", modelo: str = "Fiesta", ano: int = 2017,
) -> Lote:
    return Lote(
        id=lote_id,
        leilao="auto_avaliar",
        url=f"https://autoavaliar.com.br/lote/{lote_id}",
        marca=marca,
        modelo=modelo,
        ano=ano,
        km=50000,
        lance_atual=15000,
        fim_em=datetime.now() + timedelta(days=3),
        origem_cidade="Uberlândia",
        origem_uf="MG",
        raw_json={
            "detalhe": {
                "laudo_pdf_url": laudo_pdf_url,
                "status_laudo": "Laudo Aprovado" if laudo_pdf_url else None,
            }
        },
        scraped_at=datetime.utcnow(),
    )


def _laudo_cache(lote_id: str, confidence: float = 0.5) -> LaudoCache:
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


class TestLimparDecoys:
    """Funcional: o auto-fix efetivamente limpa decoys e preserva laudos válidos."""

    def test_remove_url_decoy_e_derruba_laudo_cache(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_DECOY", DECOY_URL_TRANSPARENCIA))
            session.add(_laudo_cache("L_DECOY", confidence=0.5))
            session.commit()

            res = limpar_decoys(session)

            assert res.decoys_encontrados == 1
            assert res.decoys_limpos == 1
            assert res.laudos_derrubados == 1
            assert "L_DECOY" in res.lotes_afetados

            lote = session.get(Lote, "L_DECOY")
            assert lote.raw_json["detalhe"]["laudo_pdf_url"] is None
            # Preserva sinais adjacentes (status_laudo fica) pra `_laudo_sem_pdf`.
            assert lote.raw_json["detalhe"]["status_laudo"] == "Laudo Aprovado"
            assert session.get(LaudoCache, "L_DECOY") is None

    def test_preserva_url_valida(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_OK", URL_LAUDO_OK))
            session.add(_laudo_cache("L_OK", confidence=0.95))
            session.commit()

            res = limpar_decoys(session)

            assert res.decoys_encontrados == 0
            assert res.decoys_limpos == 0
            lote = session.get(Lote, "L_OK")
            assert lote.raw_json["detalhe"]["laudo_pdf_url"] == URL_LAUDO_OK
            # LaudoCache válido NÃO é tocado.
            laudo = session.get(LaudoCache, "L_OK")
            assert laudo is not None
            assert laudo.confidence == 0.95

    def test_idempotente(self):
        """Rodar o script 2x seguidas tem o mesmo efeito que rodar 1x."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_DECOY", DECOY_URL_TRANSPARENCIA))
            session.add(_laudo_cache("L_DECOY"))
            session.commit()

            limpar_decoys(session)
            res2 = limpar_decoys(session)

            assert res2.decoys_encontrados == 0
            assert res2.decoys_limpos == 0

    def test_dry_run_nao_persiste(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_DECOY", DECOY_URL_TRANSPARENCIA))
            session.add(_laudo_cache("L_DECOY"))
            session.commit()

            res = limpar_decoys(session, dry_run=True)
            assert res.decoys_encontrados == 1
            assert res.decoys_limpos == 0  # não mexeu
            assert res.laudos_derrubados == 0

            # Verifica que nada mudou no DB
            lote = session.get(Lote, "L_DECOY")
            assert lote.raw_json["detalhe"]["laudo_pdf_url"] == DECOY_URL_TRANSPARENCIA
            assert session.get(LaudoCache, "L_DECOY") is not None

    def test_lote_sem_detalhe_nao_quebra(self):
        """Lotes ingeridos apenas da listagem (sem raspagem de detalhe) não têm
        `detalhe` no raw_json. O script não pode explodir neles."""
        engine = _engine_mem()
        with Session(engine) as session:
            lote = _lote("L_SEM_DET", None)
            lote.raw_json = {}  # sem chave 'detalhe'
            session.add(lote)
            session.commit()

            res = limpar_decoys(session)
            assert res.decoys_encontrados == 0
            assert res.total_lotes == 1

    def test_url_nao_mapeada_tambem_e_limpa(self):
        """Qualquer URL que não passe em `is_laudo_pdf_url()` vira lixo — não
        só a família 'Relatorio-de-Transparencia'. Protege contra decoys
        novos que o scraper ainda não conheça. Sem LaudoCache forte, não há
        evidência empírica de que a URL seja boa."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_LIXO", "https://exemplo.com/arquivo-qualquer.pdf"))
            session.commit()

            res = limpar_decoys(session)
            assert res.decoys_encontrados == 1

    def test_url_fora_da_allowlist_com_cache_forte_e_preservada(self):
        """Evidência empírica vence allowlist: URL que falha `is_laudo_pdf_url`
        mas tem `LaudoCache.confidence ≥ 0.6` é preservada — `extrair_laudo`
        já comprovou que aquela URL apontou pra laudo real. Anular agora
        derrubaria o link "Ver laudo" da planilha sem ganho real e poderia
        entrar em loop com o retry. Era exatamente o sintoma observado:
        coluna Laudo (PDF) com '—' em massa mesmo com lotes '✓ Viável'."""
        engine = _engine_mem()
        url_host_novo = "https://exemplo.com/arquivo-qualquer.pdf"
        assert is_laudo_pdf_url(url_host_novo) is False  # sanity

        with Session(engine) as session:
            session.add(_lote("L_HOST_NOVO", url_host_novo))
            session.add(_laudo_cache("L_HOST_NOVO", confidence=0.7))
            session.commit()

            res = limpar_decoys(session)

            assert res.decoys_encontrados == 0
            assert res.decoys_limpos == 0
            assert res.laudos_derrubados == 0
            assert res.preservados_por_cache == 1

            lote = session.get(Lote, "L_HOST_NOVO")
            assert lote.raw_json["detalhe"]["laudo_pdf_url"] == url_host_novo
            laudo = session.get(LaudoCache, "L_HOST_NOVO")
            assert laudo is not None
            assert laudo.confidence == 0.7

    def test_url_fora_da_allowlist_com_cache_fraco_e_limpa(self):
        """Cache com confidence<0.6 (fallback `_laudo_sem_pdf`) NÃO conta como
        evidência empírica — significa que a extração não viu o PDF. URL
        deve ser limpa normalmente."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_FRACO", "https://exemplo.com/qualquer.pdf"))
            session.add(_laudo_cache("L_FRACO", confidence=0.5))
            session.commit()

            res = limpar_decoys(session)

            assert res.decoys_encontrados == 1
            assert res.decoys_limpos == 1
            assert res.laudos_derrubados == 1
            assert res.preservados_por_cache == 0


class TestHygieneDBReal:
    """Guard de DB real — falha o `make test` se o operador deixou decoys
    em produção. Skippa se não há DB (ex.: CI, máquina nova).
    """

    def _db_path(self) -> Optional[Path]:
        """Resolve o caminho do DB igual ao `carros_sa.db.DEFAULT_DB_PATH`,
        mas sem importar o módulo — assim o skip não depende de init_db().
        """
        explicit = os.environ.get("CARROS_SA_DB")
        if explicit:
            p = Path(explicit)
            return p if p.exists() else None
        # Default: procura no cwd e na raiz do repo (relativo a este teste).
        for candidate in (Path("carros_sa.db"), Path(__file__).resolve().parents[1] / "carros_sa.db"):
            if candidate.exists():
                return candidate
        return None

    def test_db_atual_nao_tem_decoys(self):
        db_path = self._db_path()
        if db_path is None:
            pytest.skip("sem carros_sa.db — hygiene check só roda com DB presente")

        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        with Session(engine) as session:
            res = limpar_decoys(session, dry_run=True)

        if res.decoys_encontrados > 0:
            amostra = res.lotes_afetados[:5]
            pytest.fail(
                f"Encontrados {res.decoys_encontrados} lotes com URL-decoy persistida "
                f"em raw_json.detalhe.laudo_pdf_url (total no DB: {res.total_lotes}). "
                f"Amostra: {amostra}. "
                f"Rode `make limpar-decoys` pra corrigir."
            )


class TestIsLaudoPdfUrlGuard:
    """Regressão: se alguém afrouxar `is_laudo_pdf_url()` pra deixar passar o
    decoy principal, o teste falha. Duplicado do test_parsers propositalmente —
    aqui vive junto do resto da proteção contra o decoy, pra um grep por
    'decoy' achar tudo junto.
    """

    def test_relatorio_transparencia_rejeitado(self):
        assert is_laudo_pdf_url(DECOY_URL_TRANSPARENCIA) is False

    def test_doc_b2b_aceito(self):
        assert is_laudo_pdf_url(URL_LAUDO_OK) is True
