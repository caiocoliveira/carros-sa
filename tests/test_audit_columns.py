"""Testes da auditoria automática de colunas (carros_sa.tools.audit).

Invariantes declarativas por coluna: cruza o valor exportado contra o propósito
declarado no Glossário da planilha (sheets.py). O hook SessionEnd roda isto
silenciosamente — só imprime algo quando acha anomalia.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pytest
from sqlmodel import Session, SQLModel, create_engine

from carros_sa.models import AvaliacaoLote, LaudoCache, Lote
from carros_sa.tools.audit import CHECKS, audit
from carros_sa.tools.sheets import HEADER


def _engine_mem():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


def _lote(
    lote_id: str = "L001",
    marca: str = "Ford",
    modelo: str = "Fiesta",
    ano: int = 2013,
    km: Optional[int] = 45000,
    lance_atual: int = 20000,
    fim_em: Optional[datetime] = None,
) -> Lote:
    return Lote(
        id=lote_id,
        leilao="auto_arremate",
        url=f"https://autoavaliar.com.br/lote/{lote_id}",
        marca=marca,
        modelo=modelo,
        ano=ano,
        km=km,
        lance_atual=lance_atual,
        fim_em=fim_em or (datetime.now() + timedelta(days=3)),
        scraped_at=datetime.utcnow(),
    )


def _avaliacao(
    lote_id: str = "L001",
    empresa_id: str = "uberlandia_mg",
    preco_max: int = 30000,
    # preco_giro <= fipe × 1.10 — invariante cruzada no audit alerta acima.
    # 35k vs 40k = 87.5% FIPE (caso realista: webmotors mediana < FIPE).
    preco_giro: int = 35000,
    reforma_estimada: int = 3000,
    frete_incluso: int = 1500,
    fator_risco: float = 0.8,
    dias_giro_estimado: Optional[int] = 90,
    fipe: Optional[int] = 40000,
    webmotors_mediana: Optional[int] = 35000,
    # n=10 default = "tem amostra Webmotors" (display mostra mediana, audit checa).
    # Testes podem override pra 0 pra simular "sem amostra" (display vira "—").
    webmotors_n_anuncios: Optional[int] = 10,
    preco_giro_fipe: int = 35000,
    preco_giro_aa: Optional[int] = None,
    score_roi: float = 0.3,
    justificativa: str = "Laudo leve, FIPE R$40k, giro estimado 90 dias.",
    reforma_racional: Optional[str] = "Coluna B reparada (R$3000)",
) -> AvaliacaoLote:
    return AvaliacaoLote(
        empresa_id=empresa_id,
        lote_id=lote_id,
        preco_alvo=25000,
        preco_max=preco_max,
        score_roi=score_roi,
        fator_risco=fator_risco,
        fator_liquidez=1.0,
        margem_aplicada=0.15,
        frete_incluso=frete_incluso,
        reforma_estimada=reforma_estimada,
        taxas_leilao=int(preco_max * 0.08),
        preco_giro=preco_giro,
        preco_giro_fipe=preco_giro_fipe,
        preco_giro_aa=preco_giro_aa,
        fipe=fipe,
        webmotors_mediana=webmotors_mediana,
        webmotors_n_anuncios=webmotors_n_anuncios,
        dias_giro_estimado=dias_giro_estimado,
        justificativa=justificativa,
        reforma_racional=reforma_racional,
        criado_em=datetime.utcnow(),
    )


def _laudo(
    lote_id: str = "L001",
    severidade: str = "leve",
    motor_ok: bool = True,
) -> LaudoCache:
    return LaudoCache(
        lote_id=lote_id,
        avarias_json=[],
        severidade_geral=severidade,
        motor_ok=motor_ok,
        documentacao="ok",
        categoria_veiculo="hatch",
        confidence=0.95,
        modelo_llm="gemini-flash",
        custo_usd=0.001,
        extraido_em=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Paridade HEADER ↔ CHECKS: toda coluna nova da planilha precisa ter uma
# entrada correspondente (mesmo que permissiva) senão a auditoria vira ponto cego.
# ---------------------------------------------------------------------------

class TestParidadeHeaderChecks:
    def test_todas_colunas_do_header_estao_em_checks(self):
        faltando = set(HEADER) - set(CHECKS.keys())
        assert not faltando, f"Colunas sem invariante declarada: {faltando}"

    def test_checks_nao_tem_colunas_fantasma(self):
        """CHECKS não deve ter colunas que não existem em HEADER."""
        fantasma = set(CHECKS.keys()) - set(HEADER)
        assert not fantasma, f"CHECKS referencia coluna inexistente em HEADER: {fantasma}"


# ---------------------------------------------------------------------------
# Silêncio em caminho feliz
# ---------------------------------------------------------------------------

class TestAuditHappyPath:
    def test_db_vazio_retorna_lista_vazia(self):
        engine = _engine_mem()
        violacoes = audit(engine)
        assert violacoes == []

    def test_linha_valida_nao_gera_violacoes(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert violacoes == []


# ---------------------------------------------------------------------------
# Detecção de valores fora do racional do Glossário
# ---------------------------------------------------------------------------

class TestAuditDeteccao:
    def test_roi_absurdo_reportado(self):
        """ROI alvo > 100% sinaliza bug de cálculo (margem cap em 50% deveria
        limitar score_roi a ≤100%). Antes (até 2026-05-08) a coluna era ROI
        anualizado com threshold 500% — calibrado pra giro otimista.

        Lote precisa ser VIÁVEL (preco_max > lance_atual) E ter laudo analisado
        (confidence ≥ 0.6) — em inviáveis ou laudo NÃO CAPTURADO o ROI vai pra
        "—" no display e o audit espelha (LESSONS.md/P5c).
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=1000))
            # score_roi=2.0 × 100 = 200% > 100 (matematicamente impossível com cap)
            session.add(_avaliacao(
                "L001",
                preco_max=2000,  # > lance_atual → viável
                preco_giro=100_000,
                reforma_estimada=0,
                frete_incluso=0,
                dias_giro_estimado=30,
                score_roi=2.0,
            ))
            session.add(_laudo("L001"))  # confidence=0.95 → laudo_analisado=True
            session.commit()
        violacoes = audit(engine)
        assert any("ROI" in v for v in violacoes), f"Esperava violação de ROI em {violacoes}"

    def test_roi_50_pct_nao_reportado(self):
        """ROI alvo 50% (lote viável típico) NÃO deve ser flagged — está dentro
        do range plausível pré-cap de margem (margem 33% → score_roi 50%).
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            # score_roi=0.50 → ROI alvo = 50%
            session.add(_avaliacao(
                "L001",
                dias_giro_estimado=60,
                score_roi=0.50,
            ))
            session.commit()
        violacoes = audit(engine)
        assert not any("ROI" in v for v in violacoes), (
            f"ROI 50% não deveria flag (dentro da zona aceita): {violacoes}"
        )

    def test_reforma_negativa_reportada(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001", reforma_estimada=-100))
            # Reforma só é validada quando laudo_analisado=True (paridade display).
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("Reforma" in v for v in violacoes), f"Violacoes: {violacoes}"

    def test_km_absurdo_reportado(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", km=5_000_000))
            session.add(_avaliacao("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("KM" in v for v in violacoes), f"Violacoes: {violacoes}"

    def test_modelo_string_vazia_reportado(self):
        engine = _engine_mem()
        with Session(engine) as session:
            lote = _lote("L001", marca="", modelo="", ano=2013)
            session.add(lote)
            session.add(_avaliacao("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("Modelo" in v for v in violacoes), f"Violacoes: {violacoes}"


# ---------------------------------------------------------------------------
# Coerência cross-field por linha (sintomas detectados em revisão 2026-05-02)
# ---------------------------------------------------------------------------

class TestAuditCoerenciaRow:
    def test_severidade_grave_com_reforma_zero_reportado(self):
        """Reforma R$ 0 num lote estrutural é contradição: indica que LLM não
        leu o laudo direito ou caiu em fallback errado. Operador veria
        '✓ Viável + reforma R$ 0' num carro batido.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001", reforma_estimada=0))
            session.add(_laudo("L001", severidade="estrutural"))
            session.commit()
        violacoes = audit(engine)
        assert any("Reforma R$ 0" in v and "estrutural" in v for v in violacoes), (
            f"Esperava violação 'Reforma R$ 0 com severidade estrutural', obtive: {violacoes}"
        )

    def test_severidade_leve_com_reforma_zero_e_aceito(self):
        """Reforma 0 com severidade leve/nenhuma é normal — não dispara warning."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001", reforma_estimada=0))
            session.add(_laudo("L001", severidade="leve"))
            session.commit()
        violacoes = audit(engine)
        assert not any("Reforma R$ 0" in v for v in violacoes), (
            f"Não deveria flagar reforma 0 em severidade leve: {violacoes}"
        )

    def test_preco_giro_fipe_muito_acima_da_fipe_reportado(self):
        """Anchor de revenda 30%+ acima da FIPE retail sinaliza:
           - similares poluídos (regex pegando R$ de outras seções)
           - cache FIPE stale ou marca/modelo errado
           - f_km saturado no teto sem motivo
        Threshold é `_PRECO_GIRO_FIPE_RATIO_MAX = 1.13` (max natural 1.0925 +
        margem ergonômica de 3.5pp).
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            # FIPE 32k mas preco_giro_fipe 50k = +56% acima — bug em algum lugar.
            session.add(_avaliacao(
                "L001", fipe=32_000, preco_giro_fipe=50_000, preco_giro=50_000,
            ))
            session.add(_laudo("L001"))  # paridade laudo_analisado=True
            session.commit()
        violacoes = audit(engine)
        assert any("preco_giro_fipe" in v and "FIPE × 1.13" in v for v in violacoes), (
            f"Esperava violação de divergência preco_giro_fipe vs FIPE: {violacoes}"
        )

    def test_preco_giro_fipe_dentro_da_faixa_aceito(self):
        """preco_giro_fipe = FIPE × 0.97 (fallback sem similares) deve passar."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao(
                "L001", fipe=32_000, preco_giro_fipe=31_040, preco_giro=31_040,
            ))
            session.commit()
        violacoes = audit(engine)
        assert not any("preco_giro_fipe" in v for v in violacoes), (
            f"Não deveria flagar 0.97×FIPE: {violacoes}"
        )


# ---------------------------------------------------------------------------
# Agregação por coluna (várias linhas → uma única violação com contagem)
# ---------------------------------------------------------------------------

class TestAuditCrossCheck:
    """Invariantes que cruzam mais de uma coluna (preco_alvo vs preco_max,
    preco_giro_fipe vs FIPE, lance_atual vs preco_alvo).
    """

    def test_preco_giro_fipe_acima_de_fipe_x_113_sinalizado(self):
        """Combinação patológica de mediana inflada + f_km saturado pode
        empurrar preco_giro_fipe acima de 1.13×FIPE. Audit avisa pra checar
        Webmotors live e km mediana de mercado. Threshold 1.13 deixa ~3.5pp
        de margem ergonômica sobre o max natural (1.0925).
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao(
                "L001",
                fipe=70_000,
                preco_giro_fipe=82_000,  # 82k / 70k = 1.171 > 1.13
            ))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("preco_giro_fipe" in v and "1.13" in v for v in violacoes), (
            f"Esperava violação preco_giro_fipe > FIPE×1.13 em {violacoes}"
        )

    def test_preco_giro_fipe_dentro_da_tolerancia_nao_sinaliza(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao(
                "L001",
                fipe=70_000,
                preco_giro_fipe=75_000,  # 75k / 70k = 1.071 < 1.13
            ))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert not any("preco_giro_fipe" in v for v in violacoes), (
            f"Não esperava violação dentro da tolerância: {violacoes}"
        )

    def test_zona_apertada_lance_acima_do_alvo_sinalizada(self):
        """`lance_atual > preco_alvo` mas ainda `< preco_max` é zona apertada:
        operador pode dar lance, mas o ROI exibido deve usar score_efetivo
        (não o intrinsic). Audit avisa pra confirmar que o ROI realista bate.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=27_000))  # alvo=25k, max=30k
            session.add(_avaliacao("L001", preco_max=30_000))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("Zona apertada" in v for v in violacoes), (
            f"Esperava aviso de zona apertada em {violacoes}"
        )

    def test_preco_alvo_zerado_em_viavel_sinalizado(self):
        """preco_alvo=0 num lote viável = margem-alvo da empresa inalcançável.

        Cenário patológico: FIPE baixa onde reforma+frete+custo_op+margem-alvo
        excedem o preco_giro. Precificador capa preco_alvo em 0 mas o teto
        (preco_max) ainda permite lance pela margem MÍNIMA. Display mostra
        "✓ Viável + ROI positivo + lance baixo" — operador desavisado pode
        dar lance sem perceber que entra estritamente no piso da margem.
        Detectado por audit pra sinalizar lote economicamente patológico.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=1_000))
            # preco_alvo zerado + preco_max positivo (margem mínima só)
            av = _avaliacao("L001", preco_max=1_500)
            av.preco_alvo = 0
            session.add(av)
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("preco_alvo zerado" in v for v in violacoes), (
            f"Esperava aviso preco_alvo zerado em {violacoes}"
        )

    def test_preco_alvo_positivo_nao_dispara_zerado(self):
        """preco_alvo > 0 num lote viável é o caso normal — não sinaliza."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=20_000))
            session.add(_avaliacao("L001", preco_max=30_000))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert not any("preco_alvo zerado" in v for v in violacoes), (
            f"Não esperava preco_alvo zerado em caso normal: {violacoes}"
        )

    def test_preco_alvo_zerado_em_inviavel_nao_sinaliza(self):
        """Lote inviável (lance > preco_max) NÃO dispara — display já oculta
        números, paridade P5c."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=10_000))  # > preco_max=1500
            av = _avaliacao("L001", preco_max=1_500)
            av.preco_alvo = 0
            session.add(av)
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert not any("preco_alvo zerado" in v for v in violacoes), (
            f"Não esperava preco_alvo zerado em lote inviável (display oculta): {violacoes}"
        )

    def test_lance_abaixo_do_alvo_nao_dispara_zona_apertada(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=20_000))  # alvo=25k, max=30k
            session.add(_avaliacao("L001", preco_max=30_000))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert not any("Zona apertada" in v for v in violacoes), (
            f"Não esperava zona apertada quando lance < alvo: {violacoes}"
        )


class TestAuditAgregacao:
    def test_multiplas_linhas_problema_mesma_coluna_agrupadas(self):
        engine = _engine_mem()
        with Session(engine) as session:
            for i in range(3):
                lid = f"L00{i+1}"
                session.add(_lote(lid))
                # 3 lotes com reforma_estimada negativa
                session.add(_avaliacao(lid, reforma_estimada=-100))
                # Laudo com confidence ≥ 0.6 → display mostra Reforma → audit valida.
                session.add(_laudo(lid))
            session.commit()
        violacoes = audit(engine)
        reforma = [v for v in violacoes if "Reforma" in v]
        assert len(reforma) == 1, f"Esperava uma única linha agregada, obtive: {reforma}"
        assert "3" in reforma[0], f"Linha deveria reportar contagem de 3 linhas: {reforma[0]}"

    def test_problemas_em_colunas_diferentes_reportados_separados(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", km=5_000_000))  # KM absurdo
            session.add(_avaliacao("L001", reforma_estimada=-50))  # Reforma negativa
            session.add(_laudo("L001"))

            lote2 = _lote("L002", ano=1900)  # Ano fora da faixa
            session.add(lote2)
            session.add(_avaliacao("L002"))
            session.add(_laudo("L002"))
            session.commit()
        violacoes = audit(engine)
        texto = "\n".join(violacoes)
        assert "KM" in texto
        assert "Reforma" in texto
        assert "Ano" in texto


# ---------------------------------------------------------------------------
# Paridade audit ↔ sheets (mesmo set de lotes auditado vs exibido)
# ---------------------------------------------------------------------------

class TestAuditParidadeSheets:
    """Audit deve auditar o que o operador vê. SheetsExporter filtra:
      1. fim_em is None (sumiu do leilão ativo do AA)
      2. encerrado (timer vencido OU badge ARREMATADO)

    Antes da paridade ficar explícita, audit reportava violações em lotes que
    nunca apareciam na planilha — alarme falso que o operador não conseguia
    confirmar abrindo a UI.
    """

    def test_lote_sem_fim_em_nao_e_auditado(self):
        engine = _engine_mem()
        with Session(engine) as session:
            # Lote sem fim_em mas com dado claramente bugado (KM absurdo).
            # Deveria ser ignorado pelo audit (não aparece na planilha).
            lote = _lote("L_SEM_FIM", km=5_000_000)
            lote.fim_em = None
            session.add(lote)
            session.add(_avaliacao("L_SEM_FIM"))
            session.commit()
        violacoes = audit(engine)
        assert violacoes == [], (
            f"Audit não deveria reportar lotes sem fim_em (filtrados da planilha): {violacoes}"
        )

    def test_lote_encerrado_por_timer_nao_e_auditado(self):
        engine = _engine_mem()
        with Session(engine) as session:
            # Lote com timer já passado + KM absurdo.
            lote = _lote("L_ENCERRADO", km=5_000_000)
            lote.fim_em = datetime.now() - timedelta(days=1)
            session.add(lote)
            session.add(_avaliacao("L_ENCERRADO"))
            session.commit()
        violacoes = audit(engine)
        assert violacoes == []

    def test_lote_ativo_continua_sendo_auditado(self):
        """Sanidade: se o filtro novo não derruba o lote ativo, KM absurdo continua flag."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_ATIVO", km=5_000_000))
            session.add(_avaliacao("L_ATIVO"))
            session.commit()
        violacoes = audit(engine)
        assert any("KM" in v for v in violacoes), f"Lote ativo perdeu auditoria: {violacoes}"


# ---------------------------------------------------------------------------
# Invariantes que cruzam colunas (não pertencem a uma coluna individual)
# ---------------------------------------------------------------------------

class TestAuditInvariantesCruzadas:
    """Validações que comparam DOIS campos da avaliação. Não cabem em CHECKS
    (que validam coluna por coluna) — vivem em CROSS_CHECKS.
    """

    def test_preco_giro_acima_de_fipe_x_110_e_reportado(self):
        """preco_giro > FIPE × 1.10 sinaliza f_km saturando ou mediana inflada.

        Por construção, webmotors_mediana × f_km tem teto teórico FIPE×0.97×1.15
        ≈ 111.5% — ultrapassar 110% é red flag.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            # FIPE 50k, preco_giro 60k = 120% FIPE → flag
            session.add(_avaliacao(
                "L001",
                fipe=50_000,
                preco_giro=60_000,
                preco_giro_fipe=60_000,
            ))
            session.commit()
        violacoes = audit(engine)
        assert any("Preço-Giro" in v or "preco_giro" in v.lower() for v in violacoes), (
            f"Esperava violação de preco_giro vs FIPE: {violacoes}"
        )

    def test_preco_giro_dentro_de_fipe_x_110_nao_reporta(self):
        """preco_giro = FIPE × 1.05 (dentro do esperado) não deve disparar."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao(
                "L001",
                fipe=50_000,
                preco_giro=int(50_000 * 1.05),
                preco_giro_fipe=int(50_000 * 1.05),
            ))
            session.commit()
        violacoes = audit(engine)
        assert not any("Preço-Giro" in v for v in violacoes), (
            f"105% FIPE não deveria flag: {violacoes}"
        )

    def test_preco_alvo_maior_que_preco_max_e_reportado(self):
        """preco_alvo > preco_max viola invariante do precificador (margem
        aplicada deveria ser >= margem mínima absoluta)."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            av = _avaliacao("L001", preco_max=20_000)
            av.preco_alvo = 25_000  # > preco_max — bug
            session.add(av)
            session.commit()
        violacoes = audit(engine)
        assert any("Preço-Alvo" in v or "preco_alvo" in v.lower() for v in violacoes), (
            f"Esperava violação preco_alvo > preco_max: {violacoes}"
        )


# ---------------------------------------------------------------------------
# Indicador agregado de cobertura de reforma — % de lotes com laudo analisado
# que saem com reforma=0 (suspeita de pipeline quebrado).
# ---------------------------------------------------------------------------

class TestCoberturaReforma:
    """Premissa operacional: a maior parte dos carros precisa de alguma reforma.
    Quando >=30% dos lotes com laudo analisado saem com reforma=0, o audit
    aponta pro `make diagnose-cobertura` em vez de só descrever o problema.

    Filtra lotes SEM laudo analisado (confidence<0.6) — esses têm reforma=0
    por construção e poluiriam o denominador.
    """

    def _setup_batch(self, session: Session, n_total: int, n_sem_reforma: int) -> None:
        """N lotes com laudo válido (confidence=0.95). Os primeiros `n_sem_reforma`
        têm reforma_estimada=0; o resto tem reforma=3000 (default do _avaliacao).
        """
        for i in range(n_total):
            lid = f"L{i:03d}"
            session.add(_lote(lid))
            reforma = 0 if i < n_sem_reforma else 3000
            session.add(_avaliacao(lid, reforma_estimada=reforma))
            session.add(_laudo(lid))

    def test_cobertura_reforma_dispara_acima_30pct(self):
        """40% sem reforma (4/10) → flag, mensagem aponta pro diagnose-cobertura."""
        engine = _engine_mem()
        with Session(engine) as session:
            self._setup_batch(session, n_total=10, n_sem_reforma=4)
            session.commit()
        violacoes = audit(engine)
        cobertura = [v for v in violacoes if "Cobertura de reforma" in v]
        assert len(cobertura) == 1, f"Esperava 1 violação de cobertura: {violacoes}"
        msg = cobertura[0]
        assert "4/10" in msg
        assert "40%" in msg
        # Aponta pro script de diagnóstico
        assert "diagnose-cobertura" in msg

    def test_cobertura_reforma_silencia_abaixo_30pct(self):
        """20% sem reforma (2/10) → silêncio."""
        engine = _engine_mem()
        with Session(engine) as session:
            self._setup_batch(session, n_total=10, n_sem_reforma=2)
            session.commit()
        violacoes = audit(engine)
        assert not any("Cobertura de reforma" in v for v in violacoes), (
            f"Não deveria flagar 20%: {violacoes}"
        )

    def test_cobertura_reforma_no_limite_30_dispara(self):
        """Exatamente 30% (3/10) → dispara (>= no threshold)."""
        engine = _engine_mem()
        with Session(engine) as session:
            self._setup_batch(session, n_total=10, n_sem_reforma=3)
            session.commit()
        violacoes = audit(engine)
        cobertura = [v for v in violacoes if "Cobertura de reforma" in v]
        assert len(cobertura) == 1, f"30% deveria disparar: {violacoes}"

    def test_cobertura_reforma_ignora_amostra_pequena(self):
        """4 lotes (todos sem reforma) → silêncio porque amostra<5."""
        engine = _engine_mem()
        with Session(engine) as session:
            self._setup_batch(session, n_total=4, n_sem_reforma=4)
            session.commit()
        violacoes = audit(engine)
        assert not any("Cobertura de reforma" in v for v in violacoes), (
            f"Amostra pequena não deveria disparar: {violacoes}"
        )

    def test_cobertura_reforma_exclui_laudos_nao_analisados(self):
        """10 lotes mas só 3 com laudo válido (todos sem reforma). Amostra
        elegível = 3 → silêncio porque <5, mesmo com 100% sem reforma.
        Garante que o denominador filtra lotes sem laudo (confidence<0.6)."""
        engine = _engine_mem()
        with Session(engine) as session:
            for i in range(3):
                lid = f"L_OK_{i}"
                session.add(_lote(lid))
                session.add(_avaliacao(lid, reforma_estimada=0))
                session.add(_laudo(lid))
            for i in range(7):
                lid = f"L_FALLBACK_{i}"
                session.add(_lote(lid))
                session.add(_avaliacao(lid, reforma_estimada=0))
                laudo = _laudo(lid)
                # Confidence 0.5 = fallback `_laudo_sem_pdf`, NÃO conta como
                # analisado pelo verificar_laudo_completo (limite 0.6).
                laudo.confidence = 0.5
                session.add(laudo)
            session.commit()
        violacoes = audit(engine)
        assert not any("Cobertura de reforma" in v for v in violacoes), (
            f"Lotes sem laudo válido não deveriam contar: {violacoes}"
        )


# ---------------------------------------------------------------------------
# Invariantes derivadas — cruzam mais de um campo. Testam o pipeline
# `_DERIVED_CHECKS` introduzido junto com o cap de margem.
# ---------------------------------------------------------------------------

class TestAuditDerivado:
    def _av_com_margem(self, lote_id: str, margem: float) -> AvaliacaoLote:
        # Avaliacao normal SO que com margem_aplicada explícita.
        av = _avaliacao(lote_id)
        av.margem_aplicada = margem
        return av

    def test_margem_no_teto_reportada(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(self._av_com_margem("L001", margem=0.50))  # bate no cap
            session.commit()
        violacoes = audit(engine)
        derivada = [v for v in violacoes if "margem" in v]
        assert len(derivada) == 1, f"Esperava 1 violação de margem: {derivada}"
        assert "50.0%" in derivada[0] or "50%" in derivada[0]

    def test_margem_normal_nao_reportada(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(self._av_com_margem("L001", margem=0.30))  # típico
            session.commit()
        violacoes = audit(engine)
        derivada = [v for v in violacoes if "margem" in v]
        assert derivada == []


# ---------------------------------------------------------------------------
# Display ↔ Audit (paridade de supressão pra colunas que viram "—" em
# lotes inviáveis).  Sem essa paridade, audit reportava "ROI/Lucro/Tese"
# pra lotes onde o operador NÃO vê o número — alarme falso operacional.
# ---------------------------------------------------------------------------

class TestAuditChecksIndependentes:
    """Refator 2026-05-08: Lance Máximo (R$) deixou de ser if/elif encadeado
    no `CHECKS` (que só permite 1 motivo por linha) e virou conjunto de
    funções independentes em `ALL_CHECKS`. Múltiplos sintomas na MESMA linha
    coexistem — antes o red flag (preco_max > FIPE × 1.05) ficava escondido
    atrás do yellow (zona apertada).
    """

    def test_zona_apertada_E_preco_max_acima_fipe_disparam_juntos(self):
        """Cenário patológico: lote com lance acima do alvo (zona apertada) +
        preco_max acima da FIPE × 1.05 (red flag). Ambos devem aparecer.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            # FIPE 50k, preco_max 60k = 120% FIPE → red flag
            # alvo 25k (default), lance_atual 30k → zona apertada (30 > 25, 30 < 60)
            session.add(_lote("L001", lance_atual=30_000))
            session.add(_avaliacao(
                "L001",
                preco_max=60_000,
                fipe=50_000,
                preco_giro=55_000,
                preco_giro_fipe=55_000,  # mantido < FIPE×1.13 pra não duplicar com check de preco_giro
            ))
            # Laudo com confidence>=0.6 → display mostra valores → audit valida.
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        zona = [v for v in violacoes if "Zona apertada" in v]
        acima = [v for v in violacoes if "FIPE × 1.05" in v]
        assert zona, f"Esperava 'Zona apertada': {violacoes}"
        assert acima, f"Esperava 'Lance Máximo > FIPE × 1.05': {violacoes}"

    def test_preco_max_acima_fipe_em_lote_sem_zona_apertada(self):
        """Sanidade: red flag dispara mesmo quando não há zona apertada."""
        engine = _engine_mem()
        with Session(engine) as session:
            # lance abaixo do alvo (sem zona apertada) + preco_max acima da FIPE
            session.add(_lote("L001", lance_atual=10_000))
            session.add(_avaliacao(
                "L001",
                preco_max=60_000,
                fipe=50_000,
                preco_giro=55_000,
                preco_giro_fipe=55_000,
            ))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("FIPE × 1.05" in v for v in violacoes), (
            f"Esperava red flag preco_max > FIPE × 1.05: {violacoes}"
        )

    def test_preco_max_dentro_de_fipe_nao_dispara(self):
        """Caso normal: preco_max < FIPE × 1.05 não dispara warning."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            # preco_max 30k, FIPE 40k → 75% FIPE, ok
            session.add(_avaliacao("L001", preco_max=30_000, fipe=40_000))
            session.commit()
        violacoes = audit(engine)
        assert not any("FIPE × 1.05" in v for v in violacoes), (
            f"75% FIPE não deveria flag: {violacoes}"
        )


class TestAuditReformaPesada:
    """Reforma > 30% do preco_giro indica lote economicamente questionável,
    mesmo quando passa pela margem do precificador. Capital empatado em
    reforma alto = risco operacional (surpresa na oficina, revenda mais lenta).
    """

    def test_reforma_acima_30pct_preco_giro_em_lote_viavel_dispara(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=10_000))
            # preco_giro 30k, reforma 12k → 40% > 30% → flag
            session.add(_avaliacao(
                "L001", preco_max=20_000, preco_giro=30_000, reforma_estimada=12_000,
            ))
            session.add(_laudo("L001"))  # paridade laudo_analisado=True
            session.commit()
        violacoes = audit(engine)
        assert any("economicamente questionável" in v for v in violacoes), (
            f"Esperava flag de reforma pesada: {violacoes}"
        )

    def test_reforma_abaixo_30pct_nao_dispara(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            # preco_giro 35k, reforma 3k → 8.5% — caso normal
            session.add(_avaliacao("L001"))  # default reforma=3000, preco_giro=35000
            session.commit()
        violacoes = audit(engine)
        assert not any("economicamente questionável" in v for v in violacoes), (
            f"Reforma 8% não deveria flag: {violacoes}"
        )

    def test_reforma_pesada_em_lote_inviavel_nao_dispara(self):
        """Em lotes inviáveis o display suprime tudo — audit acompanha."""
        engine = _engine_mem()
        with Session(engine) as session:
            # lance > preco_max → inviável; reforma 50% do giro mas display oculta
            session.add(_lote("L001", lance_atual=25_000))
            session.add(_avaliacao(
                "L001", preco_max=20_000, preco_giro=30_000, reforma_estimada=15_000,
            ))
            session.commit()
        violacoes = audit(engine)
        assert not any("economicamente questionável" in v for v in violacoes), (
            f"Lote inviável não deveria disparar reforma pesada (display oculta): {violacoes}"
        )

    def test_threshold_reforma_pesada_paridade_sheets_audit(self):
        """Guarda P5c: `_REFORMA_PESADA_PCT_GIRO` idêntico em sheets.py e audit.py.

        Antes de 2026-07-04 audit tinha `0.30` hardcoded no corpo de
        `_check_reforma_pesada` enquanto sheets tinha a constante nomeada
        `_REFORMA_PESADA_PCT_GIRO`. Drift silencioso: alguém calibrando com
        Arrematado poderia mudar sheets sem tocar audit (ou vice-versa) e o
        display marcaria "⚠ reforma pesada" enquanto o audit reportaria outro
        threshold — mensagens contraditórias na mesma linha. Constante em
        ambos + este teste guard fecha o vetor. LESSONS.md/P5b.
        """
        from carros_sa.tools.audit import _REFORMA_PESADA_PCT_GIRO as audit_pct
        from carros_sa.tools.sheets import _REFORMA_PESADA_PCT_GIRO as sheets_pct
        assert audit_pct == sheets_pct, (
            f"Threshold `_REFORMA_PESADA_PCT_GIRO` divergiu: audit={audit_pct} "
            f"vs sheets={sheets_pct} — recalibrar AMBOS os arquivos juntos "
            "(paridade explícita display↔audit, LESSONS.md/P5c)."
        )


class TestAuditEspelhaDisplay:
    """Lotes inviáveis (lance_atual > preco_max) viram '—' em ROI/Lucro/Tese
    no `SheetsExporter._write_sheet`. Audit deve espelhar — caso contrário
    aparece disparado "ROI alvo negativo" em lotes que o operador NÃO
    consegue confirmar abrindo a planilha. Padrão geral em LESSONS.md/P5b:
    audit é uma view sobre o display; toda supressão de display vale aqui também.
    """

    def test_lote_inviavel_com_score_efetivo_negativo_nao_dispara_roi(self):
        """Cenário Fiesta ESTRUTURAL real: lance_atual=22.9k > preco_max=13k →
        lote inviável → display vira '—' nas colunas ROI/Lucro/Tese. Audit
        espelha — não deve disparar nem ROI negativo nem ROI absurdo.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_INV", lance_atual=22_900))
            # preco_max < lance_atual → inviável; score_roi alto pra forçar
            # cálculo de score_efetivo negativo no _build_rows.
            session.add(_avaliacao(
                "L_INV",
                preco_max=13_000,    # < 22_900 → inviável
                score_roi=1.0,
                preco_giro=27_300,
                fipe=30_900,
            ))
            session.commit()
        violacoes = audit(engine)
        roi_negativos = [v for v in violacoes if "ROI" in v and "negativ" in v]
        assert roi_negativos == [], (
            f"Audit não deve flag ROI negativo em lote inviável (display mostra '—'): {violacoes}"
        )

    def test_lote_viavel_com_roi_negativo_continua_disparando(self):
        """Sanidade: lote VIÁVEL com laudo analisado e score_roi forçadamente
        negativo (cenário sintético, não deveria acontecer) ainda flag.
        Inviabilidade e laudo NÃO CAPTURADO são as únicas exclusões.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L_VIA", lance_atual=10_000))
            # Lote viável (preco_max > lance) mas score_roi < 0 (bug sintético)
            session.add(_avaliacao(
                "L_VIA",
                preco_max=30_000,
                score_roi=-0.5,
                preco_giro=35_000,
            ))
            session.add(_laudo("L_VIA"))  # paridade laudo_analisado=True
            session.commit()
        violacoes = audit(engine)
        assert any("ROI" in v and "negativ" in v for v in violacoes), (
            f"Lote viável com ROI negativo deve continuar disparando: {violacoes}"
        )


# ---------------------------------------------------------------------------
# Cross-checks novos (revisão preventiva 2026-05-09)
# ---------------------------------------------------------------------------

class TestAuditMotorProblema:
    """`motor_ok=False` em lote viável + laudo analisado = red flag operacional.

    Antes do check: lote com motor problemático passava como '✓ Viável' sem
    qualquer alerta visual além da Reforma elevada (LLM frequentemente
    subestima retífica). Operador focado em ROI alto podia dar lance.
    """

    def test_motor_ok_false_em_viavel_dispara(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=10_000))
            session.add(_avaliacao("L001", preco_max=30_000))
            session.add(_laudo("L001", motor_ok=False))
            session.commit()
        violacoes = audit(engine)
        assert any("motor_ok=False" in v for v in violacoes), (
            f"Esperava flag de motor com problema: {violacoes}"
        )

    def test_motor_ok_true_nao_dispara(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
            session.add(_laudo("L001", motor_ok=True))
            session.commit()
        violacoes = audit(engine)
        assert not any("motor_ok=False" in v for v in violacoes), (
            f"motor_ok=True não deveria disparar: {violacoes}"
        )

    def test_motor_ok_false_em_laudo_nao_analisado_nao_dispara(self):
        """Paridade display: laudo NÃO CAPTURADO oculta tudo, audit acompanha."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=10_000))
            session.add(_avaliacao("L001", preco_max=30_000))
            # Laudo com confidence baixa (fallback _laudo_sem_pdf) → laudo_analisado=False
            laudo_baixo = _laudo("L001", motor_ok=False)
            laudo_baixo.confidence = 0.5  # < 0.6 → fallback
            session.add(laudo_baixo)
            session.commit()
        violacoes = audit(engine)
        assert not any("motor_ok=False" in v for v in violacoes), (
            f"Laudo NÃO CAPTURADO deveria suprimir o check: {violacoes}"
        )


class TestSeveridadeStrCoerce:
    """`_severidade_str` coerce enum + string pra mesma forma lowercase.

    `LaudoCache.severidade_geral` é `str` no schema (orquestrador grava `.value`),
    então o caminho de produção sempre passa string. Mas o helper é chamado por
    `_check_severidade_estrutural_em_viavel` (audit) e `_sufixo_warning_operacional`
    (sheets) — duas defesas em camadas. Se um dia algum caller passar enum direto
    (vindo de `LaudoEstruturado`), `str(SeveridadeAvaria.ESTRUTURAL)` devolve
    `"SeveridadeAvaria.ESTRUTURAL"`, NÃO bate `"estrutural"`, e o warning some
    silenciosamente. Test guard contra esse bug latente.
    """

    def test_check_estrutural_aceita_enum(self):
        from carros_sa.models import SeveridadeAvaria
        from carros_sa.tools.audit import _check_severidade_estrutural_em_viavel
        row = {
            "viavel": True, "laudo_analisado": True,
            "severidade": SeveridadeAvaria.ESTRUTURAL,
            "reforma_estimada": 1000,
        }
        out = _check_severidade_estrutural_em_viavel(row)
        assert len(out) == 1
        assert "ESTRUTURAL" in out[0][1]

    def test_check_reforma_zero_aceita_enum(self):
        """CHECKS["Reforma (R$)"] cross-check: reforma=0 + severidade ≥ média
        é contradição. Quando severidade vem como enum, helper coerce pra string
        e o set lookup continua funcionando.
        """
        from carros_sa.models import SeveridadeAvaria
        from carros_sa.tools.audit import CHECKS
        row = {
            "severidade": SeveridadeAvaria.ESTRUTURAL,
            "laudo_analisado": True,
            "reforma_estimada": 0,
        }
        motivo = CHECKS["Reforma (R$)"](0, row)
        assert motivo is not None
        assert "severidade" in motivo.lower()


class TestAuditEstruturalEmViavel:
    """Lote viável + laudo analisado + severidade=ESTRUTURAL = red flag explícito.

    Operador real (Reinaldo) descarta lotes estruturais categoricamente. Mesmo
    quando o sistema deixa passar (lance muito baixo + fator_risco no teto
    diminuindo o teto), display deve sinalizar EXPLICITAMENTE. Antes do check,
    estrutural com lance baixo aparecia como '✓ Viável' sem nada além de
    Reforma alta.
    """

    def test_severidade_estrutural_em_viavel_dispara(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=5_000))
            session.add(_avaliacao("L001", preco_max=20_000))
            session.add(_laudo("L001", severidade="estrutural"))
            session.commit()
        violacoes = audit(engine)
        assert any("ESTRUTURAL" in v for v in violacoes), (
            f"Esperava flag de estrutural em lote viável: {violacoes}"
        )

    def test_severidade_leve_em_viavel_nao_dispara(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001"))
            session.add(_laudo("L001", severidade="leve"))
            session.commit()
        violacoes = audit(engine)
        # Esse motor_ok=True default + severidade leve não deveria disparar nada novo.
        assert not any("ESTRUTURAL" in v for v in violacoes), (
            f"Severidade leve não deveria flag estrutural: {violacoes}"
        )


class TestAuditMedianaDistanteFipe:
    """Mediana Webmotors muito distante da FIPE = sinal informativo de
    amostra com outlier / defasagem da FIPE (>1.20×FIPE) ou anúncios antigos
    no cache / depreciação real (<0.70×FIPE). Não afeta cálculo desde
    refactor FIPE-only de 2026-05-08, só sinaliza.

    Fonte da mediana mudou em 2026-05-12 (workstream G): vem do Webmotors
    live (cache `anuncio_webmotors`), não mais dos similares do Auto Avaliar.
    Mensagens do check refletem essa mudança.
    """

    def test_mediana_acima_de_fipe_x_120_dispara(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            # FIPE 50k, mediana 65k = 130% > 120% → amostra com outlier ou defasagem FIPE
            session.add(_avaliacao("L001", fipe=50_000, webmotors_mediana=65_000))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("Mediana" in v and "outlier" in v for v in violacoes), (
            f"Esperava flag de mediana >120% FIPE: {violacoes}"
        )

    def test_mediana_abaixo_de_fipe_x_070_dispara(self):
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            # FIPE 50k, mediana 30k = 60% < 70% → anúncios antigos / sample ruidosa
            session.add(_avaliacao("L001", fipe=50_000, webmotors_mediana=30_000))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("Mediana" in v and ("antigos" in v or "ruidosa" in v) for v in violacoes), (
            f"Esperava flag de mediana <70% FIPE: {violacoes}"
        )

    def test_mediana_proxima_da_fipe_nao_dispara(self):
        """Caso normal: mediana = FIPE × 0.97 (fallback) — não dispara."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001", fipe=50_000, webmotors_mediana=48_500))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert not any("Mediana" in v and ("outlier" in v or "antigos" in v or "ruidosa" in v) for v in violacoes), (
            f"Mediana 97% FIPE não deveria flag: {violacoes}"
        )


class TestAuditZonaApertadaBoundaryViabilidade:
    """Quando `lance_atual == preco_max` (boundary), `viavel = preco_max > lance`
    é False (estrito), mas o check antigo de zona apertada usava `<=` no max
    e disparava 'zona apertada' ao mesmo tempo que display mostrava '✗ Caro
    demais'. Audit agora usa `<` estrito pra alinhar com a viabilidade.
    """

    def test_lance_igual_a_preco_max_nao_dispara_zona_apertada(self):
        engine = _engine_mem()
        with Session(engine) as session:
            # alvo=25k (default), max=30k, lance=30k → boundary inviável.
            session.add(_lote("L001", lance_atual=30_000))
            session.add(_avaliacao("L001", preco_max=30_000))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        zona = [v for v in violacoes if "Zona apertada" in v]
        assert not zona, (
            f"Lance igual ao Lance Máximo é inviável (display oculta) — "
            f"audit não deveria reportar zona apertada: {violacoes}"
        )


class TestAuditLaudoNaoAnalisadoSuprime:
    """Paridade ampla audit ↔ display pra `laudo_analisado=False`. Display
    oculta Lance Máximo / ROI / Lucro / Reforma / Tese; audit espelha.
    Sem essa paridade, audit dispararia falsos alarmes em lotes onde o
    operador NÃO consegue confirmar abrindo a planilha (LESSONS.md/P5c).
    """

    def test_lance_maximo_acima_fipe_em_laudo_nao_analisado_nao_dispara(self):
        """Sintético: preco_max > FIPE × 1.05, mas laudo_analisado=False
        (display mostra '—' em Lance Máximo)."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001", lance_atual=10_000))
            session.add(_avaliacao(
                "L001",
                preco_max=60_000,
                fipe=50_000,
                preco_giro=55_000,
            ))
            laudo_baixo = _laudo("L001")
            laudo_baixo.confidence = 0.5  # < 0.6 → laudo_analisado=False
            session.add(laudo_baixo)
            session.commit()
        violacoes = audit(engine)
        assert not any("FIPE × 1.05" in v for v in violacoes), (
            f"Laudo não analisado deveria suprimir Lance Máximo > FIPE × 1.05: {violacoes}"
        )

    def test_reforma_zero_em_estrutural_com_laudo_nao_analisado_nao_dispara(self):
        """Reforma 0 + severidade ESTRUTURAL é contradição — MAS quando o
        laudo é fallback (confidence<0.6, _laudo_sem_pdf marca ESTRUTURAL
        sem peça), display oculta Reforma e o check de contradição deve
        ser silenciado."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001", reforma_estimada=0))
            laudo_fallback = _laudo("L001", severidade="estrutural")
            laudo_fallback.confidence = 0.55  # _laudo_sem_pdf típico
            session.add(laudo_fallback)
            session.commit()
        violacoes = audit(engine)
        assert not any("Reforma R$ 0" in v for v in violacoes), (
            f"Laudo NÃO CAPTURADO deveria suprimir contradição reforma 0: {violacoes}"
        )

    def test_lote_sem_laudo_nao_dispara_reforma_negativa(self):
        """Reforma negativa em lote sem laudo (LaudoCache ausente) — display
        mostra '⚠ LAUDO NÃO CAPTURADO' e oculta Reforma. Audit espelha.
        """
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001", reforma_estimada=-100))
            # SEM _laudo() → laudo_analisado=False
            session.commit()
        violacoes = audit(engine)
        assert not any("Reforma negativa" in v for v in violacoes), (
            f"Lote sem laudo não deveria flag reforma negativa (display oculta): {violacoes}"
        )


# ---------------------------------------------------------------------------
# Cross-field per-row check: Racional Reforma vazio com reforma>0
# ---------------------------------------------------------------------------

class TestRacionalReformaVazio:
    def test_racional_vazio_com_reforma_positiva_dispara(self):
        """Lote com laudo analisado + reforma_estimada=3000 + reforma_racional=None
        → flag. Sinaliza precificador que não montou sumário fallback (bug futuro
        ou DB pré-workstream O envenenado)."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001", reforma_estimada=3000, reforma_racional=None))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert any("Racional Reforma" in v for v in violacoes), (
            f"Esperava flag de racional vazio: {violacoes}"
        )

    def test_racional_vazio_sem_reforma_silencia(self):
        """Reforma=0 + racional None é coerente — laudo sem avarias relevantes."""
        engine = _engine_mem()
        with Session(engine) as session:
            session.add(_lote("L001"))
            session.add(_avaliacao("L001", reforma_estimada=0, reforma_racional=None))
            session.add(_laudo("L001"))
            session.commit()
        violacoes = audit(engine)
        assert not any("Racional Reforma" in v for v in violacoes), (
            f"Reforma 0 + racional vazio NÃO deveria disparar: {violacoes}"
        )
