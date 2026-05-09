"""Testes do CLI unificado (carros-sa).

Sem rede, sem Playwright, sem LLM. Usa DB SQLite temporário por teste.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlmodel import SQLModel, create_engine
from typer.testing import CliRunner

import carros_sa.db as db_module
from carros_sa.cli import app
from carros_sa.models import AvaliacaoLote, Lote

runner = CliRunner()


@pytest.fixture
def db_tmp(tmp_path, monkeypatch):
    """DB SQLite em arquivo temporário, isolado por teste."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DEFAULT_DB_PATH", db_path)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return db_path


def _seed_avaliacoes(db_path: Path, empresa: str = "carros_uberlandia", n: int = 3, prefix: str = "LOT"):
    """Insere N lotes + avaliações com ROI decrescente. Usa prefix para evitar colisão de IDs entre empresas."""
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    from sqlmodel import Session

    with Session(engine) as session:
        for i in range(n):
            lote = Lote(
                id=f"{prefix}{i:03d}",
                leilao="Uberlândia",
                url=f"https://example/lot/{i}",
                marca="Fiat",
                modelo=f"Uno {i}",
                ano=2015 + i,
                km=50000 + i * 1000,
                lance_atual=20000 + i * 1000,
                fim_em=datetime(2026, 4, 20, 14, 0),
                origem_cidade="Uberlândia",
                origem_uf="MG",
                raw_json={},
            )
            av = AvaliacaoLote(
                empresa_id=empresa,
                lote_id=lote.id,
                preco_alvo=25000 + i * 500,
                preco_max=27000,
                score_roi=0.30 - i * 0.05,  # ROI decrescente: 0.30, 0.25, 0.20
                fator_risco=1.0,
                fator_liquidez=1.0,
                margem_aplicada=0.20,
                frete_incluso=500,
                reforma_estimada=1000,
                taxas_leilao=500,
                preco_giro=26000,
                preco_giro_fipe=26000,
                preco_giro_aa=None,
                justificativa="teste",
                criado_em=datetime.utcnow(),
            )
            session.add(lote)
            session.add(av)
        session.commit()


# ---------------------------------------------------------------------------
# Ajuda geral — `carros-sa --help` lista os subcomandos principais
# ---------------------------------------------------------------------------

def test_help_lista_subcomandos():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for sub in ("triagem", "top", "ingest", "extrair-laudo", "sheets", "empresas"):
        assert sub in result.stdout


# ---------------------------------------------------------------------------
# top — caso feliz + caso vazio
# ---------------------------------------------------------------------------

def test_top_sem_dados_emite_aviso(db_tmp):
    result = runner.invoke(app, ["top", "--empresa", "carros_uberlandia"])
    assert result.exit_code == 0
    assert "Nenhuma avaliação" in result.stdout


def test_top_ranqueia_por_roi_desc(db_tmp):
    _seed_avaliacoes(db_tmp, empresa="carros_uberlandia", n=3)
    # COLUMNS alto evita Rich truncar IDs com "…" quando há muitas colunas
    result = runner.invoke(app, ["top", "--empresa", "carros_uberlandia", "--n", "10"],
                           env={"COLUMNS": "200"})
    assert result.exit_code == 0
    # Primeiro lote (LOT000) tem ROI 30%, segundo 25%, terceiro 20% — ordem descendente
    idx_lot0 = result.stdout.index("LOT000")
    idx_lot1 = result.stdout.index("LOT001")
    idx_lot2 = result.stdout.index("LOT002")
    assert idx_lot0 < idx_lot1 < idx_lot2


def test_top_respeita_limite_n(db_tmp):
    _seed_avaliacoes(db_tmp, empresa="carros_uberlandia", n=3)
    result = runner.invoke(app, ["top", "--empresa", "carros_uberlandia", "--n", "2"],
                           env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "LOT000" in result.stdout
    assert "LOT001" in result.stdout
    assert "LOT002" not in result.stdout


def test_top_filtra_inviaveis_por_default(db_tmp):
    """lance_atual > preco_max → lote inviável → some do top default."""
    engine = create_engine(f"sqlite:///{db_tmp}", connect_args={"check_same_thread": False})
    from sqlmodel import Session
    with Session(engine) as session:
        # Viável: lance 20k, preco_max 25k
        session.add(Lote(id="VIAVEL", leilao="x", url="x", marca="VW", modelo="Polo",
                         ano=2024, lance_atual=20000, raw_json={}))
        session.add(AvaliacaoLote(
            empresa_id="carros_uberlandia", lote_id="VIAVEL",
            preco_alvo=18000, preco_max=25000, score_roi=0.20,
            fator_risco=1.0, fator_liquidez=1.0, margem_aplicada=0.20,
            frete_incluso=0, reforma_estimada=0, taxas_leilao=999,
            preco_giro=24000, preco_giro_fipe=24000, preco_giro_aa=None,
            dias_giro_estimado=60, justificativa="ok",
            criado_em=datetime.utcnow(),
        ))
        # Inviável: lance 50k, preco_max 30k (lance ja passou)
        session.add(Lote(id="CARO", leilao="x", url="x", marca="VW", modelo="Polo",
                        ano=2024, lance_atual=50000, raw_json={}))
        session.add(AvaliacaoLote(
            empresa_id="carros_uberlandia", lote_id="CARO",
            preco_alvo=28000, preco_max=30000, score_roi=0.99,  # ROI altíssimo, mas inviável
            fator_risco=1.5, fator_liquidez=1.5, margem_aplicada=0.45,
            frete_incluso=0, reforma_estimada=0, taxas_leilao=999,
            preco_giro=40000, preco_giro_fipe=40000, preco_giro_aa=None,
            dias_giro_estimado=30, justificativa="caro",
            criado_em=datetime.utcnow(),
        ))
        session.commit()

    # Default: só VIAVEL aparece, CARO some apesar do ROI maior
    result = runner.invoke(app, ["top", "--empresa", "carros_uberlandia"],
                           env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "VIAVEL" in result.stdout
    assert "CARO" not in result.stdout
    assert "1 inviável" in result.stdout  # contador no título

    # Com --incluir-inviaveis: ambos aparecem, CARO no topo (ROI maior)
    result_all = runner.invoke(
        app, ["top", "--empresa", "carros_uberlandia", "--incluir-inviaveis", "--absoluto"],
        env={"COLUMNS": "200"},
    )
    assert result_all.exit_code == 0
    assert "VIAVEL" in result_all.stdout
    assert "CARO" in result_all.stdout
    assert result_all.stdout.index("CARO") < result_all.stdout.index("VIAVEL")


def test_top_zero_viaveis_emite_aviso(db_tmp):
    """Quando todos os lotes são inviáveis, default mostra aviso e não tabela."""
    engine = create_engine(f"sqlite:///{db_tmp}", connect_args={"check_same_thread": False})
    from sqlmodel import Session
    with Session(engine) as session:
        session.add(Lote(id="CARO", leilao="x", url="x", marca="VW", modelo="Gol",
                        ano=2014, lance_atual=50000, raw_json={}))
        session.add(AvaliacaoLote(
            empresa_id="carros_uberlandia", lote_id="CARO",
            preco_alvo=20000, preco_max=25000, score_roi=0.30,
            fator_risco=1.0, fator_liquidez=1.0, margem_aplicada=0.20,
            frete_incluso=0, reforma_estimada=0, taxas_leilao=999,
            preco_giro=30000, preco_giro_fipe=30000, preco_giro_aa=None,
            dias_giro_estimado=30, justificativa="x",
            criado_em=datetime.utcnow(),
        ))
        session.commit()

    result = runner.invoke(app, ["top", "--empresa", "carros_uberlandia"],
                           env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "Nenhum lote viável" in result.stdout
    assert "--incluir-inviaveis" in result.stdout


def test_top_filtra_por_empresa(db_tmp):
    _seed_avaliacoes(db_tmp, empresa="carros_uberlandia", n=2, prefix="UBE")
    _seed_avaliacoes(db_tmp, empresa="outra_empresa", n=2, prefix="OUT")
    result = runner.invoke(app, ["top", "--empresa", "outra_empresa"],
                           env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert "outra_empresa" in result.stdout
    assert "OUT000" in result.stdout
    assert "UBE000" not in result.stdout


def test_top_ranqueia_por_roi_anualizado_default(db_tmp):
    """Default rankeia por ROI/ano: lote rápido (30d) com ROI menor passa lento (180d) maior."""
    engine = create_engine(f"sqlite:///{db_tmp}", connect_args={"check_same_thread": False})
    from sqlmodel import Session
    with Session(engine) as session:
        # Lote LENTO: ROI 30% mas dias_giro 180 → anualizado = 0.30 * 365/180 = 0.608
        lote_lento = Lote(
            id="LENTO", leilao="x", url="x", marca="Land Rover", modelo="Freelander",
            ano=2012, lance_atual=30000, raw_json={},
        )
        av_lento = AvaliacaoLote(
            empresa_id="carros_uberlandia", lote_id="LENTO",
            preco_alvo=25000, preco_max=27000,
            score_roi=0.30, fator_risco=1.0, fator_liquidez=1.0,
            margem_aplicada=0.20, frete_incluso=500, reforma_estimada=1000,
            taxas_leilao=500, preco_giro=26000, preco_giro_fipe=26000,
            preco_giro_aa=None, dias_giro_estimado=180,
            justificativa="lento",
            criado_em=datetime.utcnow(),
        )
        # Lote RAPIDO: ROI 20% mas dias_giro 30 → anualizado = 0.20 * 365/30 = 2.43
        lote_rapido = Lote(
            id="RAPIDO", leilao="x", url="x", marca="VW", modelo="Polo",
            ano=2024, lance_atual=50000, raw_json={},
        )
        av_rapido = AvaliacaoLote(
            empresa_id="carros_uberlandia", lote_id="RAPIDO",
            preco_alvo=45000, preco_max=48000,
            score_roi=0.20, fator_risco=1.0, fator_liquidez=1.0,
            margem_aplicada=0.20, frete_incluso=500, reforma_estimada=1000,
            taxas_leilao=500, preco_giro=46000, preco_giro_fipe=46000,
            preco_giro_aa=None, dias_giro_estimado=30,
            justificativa="rapido",
            criado_em=datetime.utcnow(),
        )
        for r in [lote_lento, av_lento, lote_rapido, av_rapido]:
            session.add(r)
        session.commit()

    # Default: ranking interno por ROI anualizado → RAPIDO vem antes.
    # --incluir-inviaveis pq fixture tem lance > preco_max (escolha intencional
    # pra isolar a regra de ranking do filtro de viabilidade).
    result = runner.invoke(app, ["top", "--empresa", "carros_uberlandia", "--incluir-inviaveis"],
                           env={"COLUMNS": "200"})
    assert result.exit_code == 0
    assert result.stdout.index("RAPIDO") < result.stdout.index("LENTO")
    assert "ROI anualizado interno" in result.stdout

    # --absoluto inverte: por score_roi puro → LENTO (30%) vem antes de RAPIDO (20%)
    result_abs = runner.invoke(app, ["top", "--empresa", "carros_uberlandia", "--absoluto",
                                     "--incluir-inviaveis"],
                               env={"COLUMNS": "200"})
    assert result_abs.exit_code == 0
    assert result_abs.stdout.index("LENTO") < result_abs.stdout.index("RAPIDO")
    assert "ROI alvo" in result_abs.stdout


# ---------------------------------------------------------------------------
# empresas — lista configs do diretório
# ---------------------------------------------------------------------------

def test_empresas_lista_configs():
    result = runner.invoke(app, ["empresas"])
    assert result.exit_code == 0
    assert "carros_uberlandia" in result.stdout
    # sanity: não deve ter erro de atributo em nenhuma linha
    assert "erro" not in result.stdout.lower()


# ---------------------------------------------------------------------------
# triagem — falha cedo sem credenciais
# ---------------------------------------------------------------------------

def test_triagem_sem_credenciais_falha(monkeypatch):
    monkeypatch.delenv("AUTOAVALIAR_EMAIL", raising=False)
    monkeypatch.delenv("AUTOAVALIAR_PASSWORD", raising=False)
    result = runner.invoke(app, ["triagem", "--empresa", "carros_uberlandia"])
    assert result.exit_code == 1
    assert "AUTOAVALIAR_EMAIL" in result.stdout


# ---------------------------------------------------------------------------
# sheets — valida env vars antes de falar com gspread
# ---------------------------------------------------------------------------

def test_sheets_sem_credenciais_falha(monkeypatch):
    monkeypatch.delenv("GOOGLE_SHEETS_ID", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_PATH", raising=False)
    result = runner.invoke(app, ["sheets", "--empresa", "carros_uberlandia"])
    assert result.exit_code == 1
    assert "GOOGLE_SHEETS_ID" in result.stdout


def test_sheets_credencial_inexistente_falha(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_SHEETS_ID", "abc123")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_PATH", str(tmp_path / "fake.json"))
    result = runner.invoke(app, ["sheets", "--empresa", "carros_uberlandia"])
    assert result.exit_code == 1
    assert "não encontrado" in result.stdout


# ---------------------------------------------------------------------------
# ingest — arquivo inexistente
# ---------------------------------------------------------------------------

def test_ingest_arquivo_inexistente_falha(db_tmp, tmp_path):
    result = runner.invoke(app, ["ingest", str(tmp_path / "nao_existe.json")])
    assert result.exit_code == 1
    assert "não encontrado" in result.stdout


def test_ingest_lote_real_persiste(db_tmp):
    """Ingest com o JSON real de Uberlândia (10 lotes)."""
    fixture = Path("data/scrapes/2026-04-14_uberlandia_listagem.json")
    if not fixture.exists():
        pytest.skip("fixture de listagem não disponível")
    result = runner.invoke(app, ["ingest", str(fixture)])
    assert result.exit_code == 0
    assert "10" in result.stdout  # 10 lotes parseados


# ---------------------------------------------------------------------------
# _auditar_apos_triagem — gate final da triagem
#
# Garante que QUALQUER lote ativo na planilha tem laudo baixado, revisado
# (LaudoCache.confidence ≥ 0.6) e linkado (URL clicável). É o invariante
# pedido em "todos carros têm laudo" — quando algo escapar do retry, retorna
# > 0 e a CLI pode sair com exit 1, deixando rastro no log do cron.
# ---------------------------------------------------------------------------


def _seed_lote_completo(db_path: Path, lote_id: str, pdf_dir: Path):
    """Lote com PDF persistido + LaudoCache forte + URL válida."""
    from datetime import timedelta

    from sqlmodel import Session

    from carros_sa.models import LaudoCache

    pdf_dir.mkdir(parents=True, exist_ok=True)
    (pdf_dir / f"{lote_id}.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 200_000)

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    url_ok = "https://storage.googleapis.com/doc-b2b/laudo-abc.pdf"
    with Session(engine) as session:
        session.add(Lote(
            id=lote_id, leilao="auto_avaliar", url=f"https://x/{lote_id}",
            marca="Ford", modelo="Fiesta", ano=2017, km=50_000, lance_atual=15_000,
            fim_em=datetime.now() + timedelta(days=3),
            origem_cidade="Uberlândia", origem_uf="MG",
            raw_json={"detalhe": {"laudo_pdf_url": url_ok}},
        ))
        session.add(AvaliacaoLote(
            empresa_id="carros_uberlandia", lote_id=lote_id,
            preco_alvo=22000, preco_max=25000, score_roi=0.2, fator_risco=0.8,
            fator_liquidez=1.0, margem_aplicada=0.15, frete_incluso=1500,
            reforma_estimada=2000, taxas_leilao=2000, preco_giro=30000,
            preco_giro_fipe=30000, justificativa="ok", criado_em=datetime.utcnow(),
        ))
        session.add(LaudoCache(
            lote_id=lote_id, avarias_json=[], severidade_geral="leve",
            motor_ok=True, documentacao="ok", categoria_veiculo="hatch",
            confidence=0.9, modelo_llm="gemini-flash", custo_usd=0.001,
            extraido_em=datetime.utcnow(),
        ))
        session.commit()


def _seed_lote_incompleto(db_path: Path, lote_id: str):
    """Lote sem PDF + LaudoCache fraco + URL ausente — todos os 3 sintomas."""
    from datetime import timedelta

    from sqlmodel import Session

    from carros_sa.models import LaudoCache

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    with Session(engine) as session:
        session.add(Lote(
            id=lote_id, leilao="auto_avaliar", url=f"https://x/{lote_id}",
            marca="Fiat", modelo="Uno", ano=2014, km=80_000, lance_atual=10_000,
            fim_em=datetime.now() + timedelta(days=3),
            origem_cidade="Uberlândia", origem_uf="MG",
            raw_json={"detalhe": {"laudo_pdf_url": None}},
        ))
        session.add(AvaliacaoLote(
            empresa_id="carros_uberlandia", lote_id=lote_id,
            preco_alvo=15000, preco_max=18000, score_roi=0.2, fator_risco=0.8,
            fator_liquidez=1.0, margem_aplicada=0.15, frete_incluso=500,
            reforma_estimada=500, taxas_leilao=999, preco_giro=20000,
            preco_giro_fipe=20000, justificativa="ok", criado_em=datetime.utcnow(),
        ))
        session.add(LaudoCache(
            lote_id=lote_id, avarias_json=[], severidade_geral="leve",
            motor_ok=True, documentacao="ok", categoria_veiculo="hatch",
            confidence=0.5,  # fallback `_laudo_sem_pdf` → não conta como analisado
            modelo_llm="gemini-flash", custo_usd=0.001,
            extraido_em=datetime.utcnow(),
        ))
        session.commit()


def test_auditar_apos_triagem_zero_quando_tudo_completo(db_tmp, tmp_path, monkeypatch):
    """Quando todos os lotes ativos têm PDF + cache forte + URL válida, retorna 0."""
    pdf_dir = tmp_path / "pdfs"
    monkeypatch.setattr("carros_sa.tools.laudo_audit.PDF_DIR_DEFAULT", pdf_dir)
    _seed_lote_completo(db_tmp, "L_OK", pdf_dir)

    from carros_sa.cli import _auditar_apos_triagem

    n = _auditar_apos_triagem("carros_uberlandia")
    assert n == 0


def test_auditar_apos_triagem_conta_incompletos(db_tmp, tmp_path, monkeypatch, capsys):
    """Lotes sem laudo completo fazem a função retornar > 0 e printar motivo + remediação."""
    pdf_dir = tmp_path / "pdfs"
    monkeypatch.setattr("carros_sa.tools.laudo_audit.PDF_DIR_DEFAULT", pdf_dir)
    _seed_lote_completo(db_tmp, "L_OK", pdf_dir)
    _seed_lote_incompleto(db_tmp, "L_FAIL_1")
    _seed_lote_incompleto(db_tmp, "L_FAIL_2")

    from carros_sa.cli import _auditar_apos_triagem

    n = _auditar_apos_triagem("carros_uberlandia")
    assert n == 2

    # Captura output do Rich console (escreve em stdout). O ID e a remediação
    # precisam aparecer pra que o operador saiba o que rodar pra destravar.
    out = capsys.readouterr().out
    assert "L_FAIL_1" in out
    assert "reprocessar_lotes_do_db" in out


def test_setup_cron_inclui_audit_estrito():
    """Cron diário precisa rodar `auditar_laudos --strict` no fim do pipeline.

    Sem esse passo, lotes que escapam do retry ficam silenciosamente como
    "⚠ LAUDO NÃO CAPTURADO" e o operador só descobre olhando a aba —
    exatamente o sintoma que o gate se propõe a eliminar.
    """
    cron = Path("scripts/setup_cron.sh").read_text()
    assert "auditar_laudos.py" in cron
    assert "--strict" in cron
    # Deve vir depois do retry (último elo da cadeia) — sanity de ordem.
    assert cron.index("AUDIT_SCRIPT=") > cron.index("RETRY_SCRIPT=")
