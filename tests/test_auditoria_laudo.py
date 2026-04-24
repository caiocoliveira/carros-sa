"""Guards de completude de laudo: todo lote ativo na planilha tem laudo OK.

A planilha exige 3 dimensões preenchidas por lote ativo:
    - link no raw_json.detalhe.laudo_pdf_url (passa em is_laudo_pdf_url)
    - PDF baixado em data/laudos_pdfs/<lote_id>.pdf (≥5KB)
    - LaudoCache.confidence >= 0.6 (extrator rodou de verdade)

Quando qualquer um falta, o lote fica "zumbi" — aparece na planilha como
"⚠ LAUDO NÃO ANALISADO" e suprime numéricos. Historicamente isso ia
acumulando silenciosamente; agora a invariante é coberta por teste.

Estratégia (espelha `test_decoy_laudo_guard.py`):

1. Testes funcionais contra SQLite in-memory + PDF fixture (ou `tmp_path`):
   cobrem cada transição de status.

2. Hygiene check contra DB real: se `carros_sa.db` existe, o test suite
   falha caso qualquer lote ativo esteja zumbi. Skippa em CI/máquinas novas.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.auditoria_laudo import (
    ResultadoAuditoria,
    StatusCompletude,
    auditar_completude,
    classificar_lote,
    reextrair_pendentes_com_pdf_local,
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


def _pdf_valido(path: Path) -> None:
    """Escreve bytes suficientes (>5KB) pra passar no tamanho-mínimo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n" + b"x" * 10_000)


def _lote(
    lote_id: str,
    *,
    laudo_pdf_url: Optional[str] = URL_LAUDO_OK,
    fim_em: Optional[datetime] = None,
    encerrado: bool = False,
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
        fim_em=fim_em or (datetime.now() + timedelta(days=3)),
        origem_cidade="Uberlândia",
        origem_uf="MG",
        raw_json={
            "detalhe": {
                "laudo_pdf_url": laudo_pdf_url,
                "encerrado": encerrado,
            }
        },
        scraped_at=datetime.utcnow(),
    )


def _laudo_cache(lote_id: str, confidence: float = 0.9) -> LaudoCache:
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


def _avaliacao(lote_id: str, empresa_id: str = "carros_uberlandia") -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id=empresa_id,
        lote_id=lote_id,
        preco_alvo=25000, preco_max=20000,
        score_roi=0.15, fator_risco=1.0, fator_liquidez=1.0,
        margem_aplicada=0.2,
        frete_incluso=500, reforma_estimada=1000, taxas_leilao=1600,
        preco_giro=28000, preco_giro_fipe=28000,
        justificativa="teste",
        criado_em=datetime.utcnow(),
    )


class TestClassificarLote:
    """Cada dimensão (link, PDF, laudo) é coberta isoladamente."""

    def test_tudo_ok(self, tmp_path):
        lote = _lote("L_OK")
        laudo = _laudo_cache("L_OK", confidence=0.9)
        _pdf_valido(tmp_path / "L_OK.pdf")

        diag = classificar_lote(lote, laudo, tmp_path)
        assert diag.status == StatusCompletude.OK
        assert diag.razao == "laudo completo"

    def test_sem_url_em_raw_json(self, tmp_path):
        lote = _lote("L_SEM_URL", laudo_pdf_url=None)
        diag = classificar_lote(lote, None, tmp_path)
        assert diag.status == StatusCompletude.SEM_LINK
        assert "ausente" in diag.razao

    def test_url_decoy_tambem_conta_como_sem_link(self, tmp_path):
        lote = _lote("L_DECOY", laudo_pdf_url=URL_DECOY)
        diag = classificar_lote(lote, None, tmp_path)
        assert diag.status == StatusCompletude.SEM_LINK
        assert "decoy" in diag.razao

    def test_url_ok_mas_pdf_ausente(self, tmp_path):
        lote = _lote("L_NO_PDF")
        # Nenhum PDF escrito em tmp_path.
        diag = classificar_lote(lote, None, tmp_path)
        assert diag.status == StatusCompletude.SEM_PDF_LOCAL
        assert "não existe" in diag.razao

    def test_pdf_truncado_conta_como_ausente(self, tmp_path):
        lote = _lote("L_TRUNC")
        (tmp_path / "L_TRUNC.pdf").write_bytes(b"oi")  # 2B << 5KB
        diag = classificar_lote(lote, _laudo_cache("L_TRUNC", 0.9), tmp_path)
        assert diag.status == StatusCompletude.SEM_PDF_LOCAL

    def test_pdf_ok_mas_laudo_ausente(self, tmp_path):
        lote = _lote("L_PEND")
        _pdf_valido(tmp_path / "L_PEND.pdf")
        diag = classificar_lote(lote, None, tmp_path)
        assert diag.status == StatusCompletude.LAUDO_PENDENTE
        assert "ausente" in diag.razao

    def test_pdf_ok_mas_laudo_baixa_confidence(self, tmp_path):
        lote = _lote("L_FALLBACK")
        _pdf_valido(tmp_path / "L_FALLBACK.pdf")
        laudo = _laudo_cache("L_FALLBACK", confidence=0.5)  # fallback _laudo_sem_pdf
        diag = classificar_lote(lote, laudo, tmp_path)
        assert diag.status == StatusCompletude.LAUDO_PENDENTE
        assert "0.50" in diag.razao

    def test_confidence_exatamente_no_limite_e_ok(self, tmp_path):
        """Limite é >= 0.6 (inclusive). 0.6 é laudo textual com avarias
        (fallback de qualidade). Mantemos o mesmo limiar do sheets.py."""
        lote = _lote("L_LIMITE")
        _pdf_valido(tmp_path / "L_LIMITE.pdf")
        diag = classificar_lote(lote, _laudo_cache("L_LIMITE", 0.6), tmp_path)
        assert diag.status == StatusCompletude.OK


class TestAuditarCompletude:
    """Integração: filtra lotes ativos + cruza com AvaliacaoLote + agrega contagens."""

    def test_db_vazio_zero_ativos(self, tmp_path):
        engine = _engine_mem()
        with Session(engine) as s:
            result = auditar_completude(s, pdf_dir=tmp_path)
        assert result.total_ativos == 0
        assert result.total_zumbis == 0

    def test_conta_cada_status(self, tmp_path):
        """1 lote OK, 1 sem link, 1 sem pdf, 1 laudo pendente."""
        engine = _engine_mem()
        with Session(engine) as s:
            # OK
            s.add(_lote("OK"))
            s.add(_laudo_cache("OK", 0.9))
            _pdf_valido(tmp_path / "OK.pdf")
            s.add(_avaliacao("OK"))
            # sem link (URL decoy)
            s.add(_lote("SEM_LINK", laudo_pdf_url=URL_DECOY))
            s.add(_avaliacao("SEM_LINK"))
            # sem pdf local (URL ok, nada em disco)
            s.add(_lote("SEM_PDF"))
            s.add(_avaliacao("SEM_PDF"))
            # laudo pendente (tudo ok menos confidence)
            s.add(_lote("PEND"))
            s.add(_laudo_cache("PEND", 0.5))
            _pdf_valido(tmp_path / "PEND.pdf")
            s.add(_avaliacao("PEND"))
            s.commit()

            result = auditar_completude(s, pdf_dir=tmp_path)

        assert result.total_ativos == 4
        assert result.contagens[StatusCompletude.OK] == 1
        assert result.contagens[StatusCompletude.SEM_LINK] == 1
        assert result.contagens[StatusCompletude.SEM_PDF_LOCAL] == 1
        assert result.contagens[StatusCompletude.LAUDO_PENDENTE] == 1
        assert result.total_zumbis == 3
        assert result.total_ok == 1

    def test_lote_ativo_sem_avaliacao_nao_entra_no_audit(self, tmp_path):
        """Lotes que caíram em early_exit (reprovado_estrutural etc) NÃO têm
        AvaliacaoLote. Não devem aparecer no audit — são descarte legítimo.
        """
        engine = _engine_mem()
        with Session(engine) as s:
            s.add(_lote("EARLY_EXIT", laudo_pdf_url=None))
            # Sem AvaliacaoLote pra este lote.
            s.commit()
            result = auditar_completude(s, pdf_dir=tmp_path)
        assert result.total_ativos == 0

    def test_lote_encerrado_por_timer_filtrado(self, tmp_path):
        """Lote com fim_em no passado = encerrado. Não vai pra planilha e
        não entra no audit."""
        engine = _engine_mem()
        with Session(engine) as s:
            s.add(_lote("PASSADO", fim_em=datetime.now() - timedelta(hours=1)))
            s.add(_avaliacao("PASSADO"))
            s.commit()
            result = auditar_completude(s, pdf_dir=tmp_path)
        assert result.total_ativos == 0

    def test_lote_encerrado_por_badge_filtrado(self, tmp_path):
        """Badge ARREMATADO marca encerrado=True em raw_json.detalhe."""
        engine = _engine_mem()
        with Session(engine) as s:
            s.add(_lote("ARREMATADO", encerrado=True))
            s.add(_avaliacao("ARREMATADO"))
            s.commit()
            result = auditar_completude(s, pdf_dir=tmp_path)
        assert result.total_ativos == 0

    def test_filtra_por_empresa_id(self, tmp_path):
        engine = _engine_mem()
        with Session(engine) as s:
            s.add(_lote("A"))
            s.add(_avaliacao("A", empresa_id="carros_uberlandia"))
            s.add(_lote("B"))
            s.add(_avaliacao("B", empresa_id="outra_empresa"))
            s.commit()

            res_uber = auditar_completude(
                s, pdf_dir=tmp_path, empresa_id="carros_uberlandia",
            )
            res_todas = auditar_completude(s, pdf_dir=tmp_path)

        assert res_uber.total_ativos == 1
        assert res_todas.total_ativos == 2

    def test_lotes_ordenados_deterministicamente(self, tmp_path):
        """Relatório deve ser reproduzível — ordenar por lote_id."""
        engine = _engine_mem()
        with Session(engine) as s:
            for lid in ("C", "A", "B"):
                s.add(_lote(lid, laudo_pdf_url=None))
                s.add(_avaliacao(lid))
            s.commit()
            result = auditar_completude(s, pdf_dir=tmp_path)
        assert [d.lote_id for d in result.diagnosticos] == ["A", "B", "C"]


class TestReextrairPendentesComPdfLocal:
    """Auto-fix: pro pendente com PDF local, re-roda extrator sem rede."""

    def test_dry_run_nao_persiste(self, tmp_path):
        engine = _engine_mem()
        with Session(engine) as s:
            s.add(_lote("PEND"))
            s.add(_laudo_cache("PEND", 0.5))
            _pdf_valido(tmp_path / "PEND.pdf")
            s.add(_avaliacao("PEND"))
            s.commit()

            result = reextrair_pendentes_com_pdf_local(
                s, pdf_dir=tmp_path, dry_run=True,
            )
            # 1 tentativa registrada; pode falhar ou suceder dependendo do PDF
            # (fixture é inválido, vai falhar na extração — mas não explode).
            assert result.tentativas == 1
            laudo_apos = s.get(LaudoCache, "PEND")
            # dry_run=True: não persiste nem se sucedesse.
            assert laudo_apos.confidence == 0.5

    def test_sem_pendentes_zero_tentativas(self, tmp_path):
        engine = _engine_mem()
        with Session(engine) as s:
            s.add(_lote("OK"))
            s.add(_laudo_cache("OK", 0.9))
            _pdf_valido(tmp_path / "OK.pdf")
            s.add(_avaliacao("OK"))
            s.commit()

            result = reextrair_pendentes_com_pdf_local(s, pdf_dir=tmp_path)
        assert result.tentativas == 0
        assert result.sucessos == 0

    def test_nao_tenta_quem_esta_sem_pdf_local(self, tmp_path):
        """Auto-fix offline só age quando PDF local existe. SEM_PDF_LOCAL
        fica pro retry com Playwright."""
        engine = _engine_mem()
        with Session(engine) as s:
            s.add(_lote("SEM_PDF"))
            s.add(_laudo_cache("SEM_PDF", 0.5))
            s.add(_avaliacao("SEM_PDF"))
            s.commit()

            result = reextrair_pendentes_com_pdf_local(s, pdf_dir=tmp_path)
        assert result.tentativas == 0


class TestHygieneDBReal:
    """Falha o `make test` se o operador deixou zumbi em produção.

    Skippa gracefully em CI/máquinas sem DB. Usa sempre `dry_run=True` —
    hygiene check NÃO deve mutar estado de produção silenciosamente. Se
    falhar, o operador roda `python scripts/auditar_laudos.py --fix` ou
    `make triagem` pra corrigir.
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

    def test_db_atual_nao_tem_zumbi(self):
        db_path = self._db_path()
        if db_path is None:
            pytest.skip("sem carros_sa.db — hygiene check só roda com DB presente")

        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        with Session(engine) as session:
            result = auditar_completude(session)

        if result.total_zumbis > 0:
            # Agrega por status pra mensagem ser acionável sem flood de IDs.
            resumo = ", ".join(
                f"{status.value}={n}"
                for status, n in result.contagens.items()
                if status != StatusCompletude.OK and n > 0
            )
            # Amostra um lote por status pro operador identificar rapidamente.
            amostras = []
            vistos: set = set()
            for diag in result.diagnosticos:
                if diag.status == StatusCompletude.OK or diag.status in vistos:
                    continue
                vistos.add(diag.status)
                amostras.append(f"{diag.lote_id} ({diag.status.value}): {diag.razao}")
            pytest.fail(
                f"Encontrados {result.total_zumbis} lote(s) zumbi na planilha "
                f"(total ativo: {result.total_ativos}). "
                f"Contagem: {resumo}. "
                f"Amostra:\n  " + "\n  ".join(amostras) + "\n"
                f"Rode `python scripts/auditar_laudos.py --detalhes` pra lista completa, "
                f"ou `python scripts/auditar_laudos.py --fix` pra auto-fix dos laudos com PDF local."
            )


class TestInvariantesDeImpl:
    """Garante que o módulo não regride em jeito silencioso."""

    def test_todos_status_tem_emoji_no_cli(self):
        """Se alguém adiciona novo StatusCompletude, _EMOJI do CLI precisa
        acompanhar — senão print() quebra em runtime. Teste de paridade.
        """
        from scripts.auditar_laudos import _EMOJI
        for status in StatusCompletude:
            assert status in _EMOJI, (
                f"{status.value} sem emoji em scripts/auditar_laudos.py _EMOJI — "
                "atualizar o dict."
            )

    def test_resultado_auditoria_propriedades(self):
        """total_zumbis + total_ok = total_ativos, e ambas propriedades
        derivam de `contagens` sem estado próprio."""
        r = ResultadoAuditoria(
            total_ativos=10,
            contagens={
                StatusCompletude.OK: 7,
                StatusCompletude.LAUDO_PENDENTE: 2,
                StatusCompletude.SEM_LINK: 1,
            },
        )
        assert r.total_ok == 7
        assert r.total_zumbis == 3
        assert r.total_ok + r.total_zumbis == r.total_ativos
