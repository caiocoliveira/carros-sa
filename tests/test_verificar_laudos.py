"""Testes do verificador + auto-healer de completude de laudo.

Protege contra a regressão "carros na lista sem laudo baixado / revisado /
link clicável". Cada caso cobre uma das categorias que `verificar()` classifica,
plus o fluxo de auto-heal (decoy → limpo; PDF podre → removido; cache stale →
derrubado). Um guard final roda contra o DB real quando existir — se sobrar
qualquer lote ativo fora do 'ok', a suíte fica vermelha e o operador roda
`make verificar-laudos` antes de subir.

Fixtures são minimalistas: SQLite em memória + PDF temp com cabeçalho
"LAUDO"/"CHASSI" pra passar em `_pdf_eh_laudo_valido`.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from scripts.verificar_laudos import verificar

URL_LAUDO_OK = "https://storage.googleapis.com/doc-b2b/abc123.pdf"
URL_DECOY = (
    "https://repo-site-aav-production.storage.googleapis.com/app/uploads/"
    "2025/10/Relatorio-de-Transparencia-e-igualdade-Salarial.pdf"
)

EMPRESA_ID = "carros_uberlandia"


# ---------- Fixtures ----------

def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _lote(
    lote_id: str,
    *,
    laudo_pdf_url: Optional[str] = URL_LAUDO_OK,
    fim_em: Optional[datetime] = None,
    encerrado: bool = False,
) -> Lote:
    detalhe = {"laudo_pdf_url": laudo_pdf_url, "status_laudo": "Laudo Aprovado"}
    if encerrado:
        detalhe["encerrado"] = True
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
        raw_json={"detalhe": detalhe},
        scraped_at=datetime.utcnow(),
    )


def _avaliacao(lote_id: str, empresa_id: str = EMPRESA_ID) -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id=empresa_id, lote_id=lote_id,
        preco_alvo=20000, preco_max=18000, score_roi=0.1,
        fator_risco=1.0, fator_liquidez=1.0, margem_aplicada=0.2,
        frete_incluso=500, reforma_estimada=1000, taxas_leilao=1500,
        preco_giro=22000, preco_giro_fipe=22000, preco_giro_aa=None,
        justificativa="teste",
    )


def _laudo(lote_id: str, confidence: float = 0.9) -> LaudoCache:
    return LaudoCache(
        lote_id=lote_id, avarias_json=[], severidade_geral="nenhuma",
        motor_ok=True, documentacao="ok", categoria_veiculo="outro",
        confidence=confidence, modelo_llm="gemini-flash", custo_usd=0.0,
        extraido_em=datetime.utcnow(),
    )


# Mini-PDF válido: header PDF + texto "LAUDO CHASSI" na 1ª página.
# `_pdf_eh_laudo_valido` checa tamanho >=5KB + marcadores positivos, então
# inflamos com espaço vazio. PDFs reais têm ~300KB, nosso mock 6KB serve.
def _escrever_pdf_valido(path: Path) -> None:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "LAUDO DE INSPEÇÃO CHASSI PLACA ABC1234")
    doc.save(str(path))
    doc.close()
    # Garante >=5KB (fitz salva ~1KB minimal); pad com bytes extras no final.
    if path.stat().st_size < 5_000:
        with open(path, "ab") as f:
            f.write(b"\x00" * (5_500 - path.stat().st_size))


def _escrever_pdf_invalido(path: Path) -> None:
    """PDF sem marcadores de laudo (decoy institucional)."""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "RELATÓRIO DE TRANSPARÊNCIA SALARIAL 2025")
    doc.save(str(path))
    doc.close()
    if path.stat().st_size < 5_000:
        with open(path, "ab") as f:
            f.write(b"\x00" * (5_500 - path.stat().st_size))


@pytest.fixture
def pdf_dir(tmp_path):
    d = tmp_path / "laudos_pdfs"
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------- Classificação ----------

class TestClassificacao:
    def test_ok_quando_url_pdf_e_cache_validos(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L1"))
            session.add(_avaliacao("L1"))
            session.add(_laudo("L1", confidence=0.9))
            session.commit()
            _escrever_pdf_valido(pdf_dir / "L1.pdf")

            res = verificar(session, EMPRESA_ID, dry_run=True, pdf_dir=pdf_dir)

            assert res.total_ativos == 1
            assert res.n_ok == 1
            assert res.n_pendentes == 0

    def test_url_decoy_e_classificada(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L2", laudo_pdf_url=URL_DECOY))
            session.add(_avaliacao("L2"))
            session.add(_laudo("L2", confidence=0.5))
            session.commit()

            res = verificar(session, EMPRESA_ID, dry_run=True, pdf_dir=pdf_dir)

            assert res.total_ativos == 1
            assert len(res.por_categoria["url_decoy"]) == 1
            assert res.por_categoria["url_decoy"][0].lote_id == "L2"
            assert res.n_pendentes == 1

    def test_url_ausente(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L3", laudo_pdf_url=None))
            session.add(_avaliacao("L3"))
            session.commit()

            res = verificar(session, EMPRESA_ID, dry_run=True, pdf_dir=pdf_dir)

            assert len(res.por_categoria["url_ausente"]) == 1

    def test_pdf_nao_baixado(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L4"))
            session.add(_avaliacao("L4"))
            session.commit()
            # NÃO escreve PDF em pdf_dir/L4.pdf

            res = verificar(session, EMPRESA_ID, dry_run=True, pdf_dir=pdf_dir)

            assert len(res.por_categoria["pdf_nao_baixado"]) == 1

    def test_pdf_local_invalido(self, pdf_dir):
        """PDF existe no disco mas conteúdo é decoy institucional (sem marcador
        positivo + com 'Transparência Salarial'). `_pdf_eh_laudo_valido` rejeita."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L5"))
            session.add(_avaliacao("L5"))
            session.commit()
            _escrever_pdf_invalido(pdf_dir / "L5.pdf")

            res = verificar(session, EMPRESA_ID, dry_run=True, pdf_dir=pdf_dir)

            assert len(res.por_categoria["pdf_local_invalido"]) == 1

    def test_laudo_ausente(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L6"))
            session.add(_avaliacao("L6"))
            # sem LaudoCache
            session.commit()
            _escrever_pdf_valido(pdf_dir / "L6.pdf")

            res = verificar(session, EMPRESA_ID, dry_run=True, pdf_dir=pdf_dir)

            assert len(res.por_categoria["laudo_ausente"]) == 1

    def test_laudo_nao_analisado_confidence_baixa(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L7"))
            session.add(_avaliacao("L7"))
            session.add(_laudo("L7", confidence=0.5))
            session.commit()
            _escrever_pdf_valido(pdf_dir / "L7.pdf")

            res = verificar(session, EMPRESA_ID, dry_run=True, pdf_dir=pdf_dir)

            assert len(res.por_categoria["laudo_nao_analisado"]) == 1

    def test_encerrado_nao_entra(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            # Active em termos de fim_em, mas marcado encerrado no detalhe.
            session.add(_lote("L8", encerrado=True))
            session.add(_avaliacao("L8"))
            session.commit()

            res = verificar(session, EMPRESA_ID, dry_run=True, pdf_dir=pdf_dir)

            assert res.total_ativos == 0

    def test_lote_passado_nao_entra(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L9", fim_em=datetime.now() - timedelta(days=1)))
            session.add(_avaliacao("L9"))
            session.commit()

            res = verificar(session, EMPRESA_ID, dry_run=True, pdf_dir=pdf_dir)

            assert res.total_ativos == 0

    def test_sem_avaliacao_nao_entra(self, pdf_dir):
        """Lote ativo sem AvaliacaoLote ainda não apareceria na planilha."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L10"))
            # NÃO adiciona AvaliacaoLote
            session.commit()

            res = verificar(session, EMPRESA_ID, dry_run=True, pdf_dir=pdf_dir)

            assert res.total_ativos == 0

    def test_outra_empresa_nao_interfere(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L11"))
            session.add(_avaliacao("L11", empresa_id="empresa_outra"))
            session.commit()

            res = verificar(session, EMPRESA_ID, dry_run=True, pdf_dir=pdf_dir)
            assert res.total_ativos == 0


# ---------- Auto-heal ----------

class TestAutoHeal:
    def test_decoy_e_limpo_e_laudo_cache_derrubado(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_DECOY", laudo_pdf_url=URL_DECOY))
            session.add(_avaliacao("L_DECOY"))
            session.add(_laudo("L_DECOY", confidence=0.5))
            session.commit()

            res = verificar(session, EMPRESA_ID, dry_run=False, pdf_dir=pdf_dir)

            assert res.decoys_limpos >= 1
            # LaudoCache do decoy foi derrubado por limpar_decoys.
            assert session.get(LaudoCache, "L_DECOY") is None
            # URL foi zerada no raw_json.
            lote = session.get(Lote, "L_DECOY")
            assert lote.raw_json["detalhe"]["laudo_pdf_url"] is None

    def test_pdf_local_invalido_e_removido(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_PDF_PODRE"))
            session.add(_avaliacao("L_PDF_PODRE"))
            session.add(_laudo("L_PDF_PODRE", confidence=0.7))
            session.commit()
            _escrever_pdf_invalido(pdf_dir / "L_PDF_PODRE.pdf")

            res = verificar(session, EMPRESA_ID, dry_run=False, pdf_dir=pdf_dir)

            assert res.pdfs_removidos == 1
            assert not (pdf_dir / "L_PDF_PODRE.pdf").exists()
            # LaudoCache associado foi dropado — extração veio do PDF podre.
            assert session.get(LaudoCache, "L_PDF_PODRE") is None

    def test_laudo_nao_analisado_derruba_cache(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_CACHE_BAIXO"))
            session.add(_avaliacao("L_CACHE_BAIXO"))
            session.add(_laudo("L_CACHE_BAIXO", confidence=0.5))
            session.commit()
            _escrever_pdf_valido(pdf_dir / "L_CACHE_BAIXO.pdf")

            res = verificar(session, EMPRESA_ID, dry_run=False, pdf_dir=pdf_dir)

            assert res.laudos_derrubados >= 1
            assert session.get(LaudoCache, "L_CACHE_BAIXO") is None

    def test_dry_run_nao_muta(self, pdf_dir):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_DECOY", laudo_pdf_url=URL_DECOY))
            session.add(_avaliacao("L_DECOY"))
            session.add(_laudo("L_DECOY", confidence=0.5))
            session.commit()
            _escrever_pdf_invalido(pdf_dir / "L_DECOY.pdf")

            res = verificar(session, EMPRESA_ID, dry_run=True, pdf_dir=pdf_dir)

            # Relatou mas não tocou em nada.
            assert res.decoys_limpos == 0
            assert res.pdfs_removidos == 0
            assert res.laudos_derrubados == 0
            lote = session.get(Lote, "L_DECOY")
            assert lote.raw_json["detalhe"]["laudo_pdf_url"] == URL_DECOY
            assert session.get(LaudoCache, "L_DECOY") is not None
            assert (pdf_dir / "L_DECOY.pdf").exists()

    def test_idempotente(self, pdf_dir):
        """Rodar 2x não piora nada — segundo run não tem gaps auto-curáveis."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_DECOY", laudo_pdf_url=URL_DECOY))
            session.add(_avaliacao("L_DECOY"))
            session.add(_laudo("L_DECOY", confidence=0.5))
            session.commit()

            verificar(session, EMPRESA_ID, dry_run=False, pdf_dir=pdf_dir)
            res2 = verificar(session, EMPRESA_ID, dry_run=False, pdf_dir=pdf_dir)

            assert res2.decoys_limpos == 0
            assert res2.pdfs_removidos == 0


# ---------- Guard de DB real ----------

class TestHygieneDBReal:
    """Se o DB de produção existir, falha a suíte caso sobrem lotes ativos
    com laudo incompleto depois do auto-heal. Força o operador a rodar
    `make verificar-laudos` antes de dar release como verde.
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

    def test_sem_lotes_pendentes_no_db_atual(self):
        db_path = self._db_path()
        if db_path is None:
            pytest.skip("sem carros_sa.db — hygiene check só roda com DB presente")

        engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False},
        )
        with Session(engine) as session:
            res = verificar(session, EMPRESA_ID, dry_run=True)

        if res.n_pendentes > 0:
            amostras = []
            for cat, lotes in res.por_categoria.items():
                if cat == "ok" or not lotes:
                    continue
                amostras.append(f"{cat}={len(lotes)} (ex: {lotes[0].lote_id})")
            pytest.fail(
                f"{res.n_pendentes}/{res.total_ativos} lote(s) ativos com laudo "
                f"incompleto. Categorias: {'; '.join(amostras)}. "
                f"Rode `make verificar-laudos` pra auto-curar o que der + pedir retry."
            )
