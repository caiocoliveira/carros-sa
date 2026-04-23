"""Audit de laudos travados — fecha o gap de visibilidade.

Pergunta que responde: "estamos deixando carro nenhum sem avaliação de laudo?".

Hoje o pipeline auto-reprocessa lotes com `LaudoCache.confidence < threshold`
na próxima run. Se o mesmo erro se repete (PDF corrompido, marca fora de
catálogo, visão consistentemente confusa), o lote fica num loop silencioso
— ninguém é avisado.

Este módulo lista:
  - Lotes ATIVOS (fim_em > agora) com LaudoCache conf < threshold
  - Quantos dias desde scraped_at (= "idade do lote na base")
  - Última tentativa de extração (= LaudoCache.extraido_em)
  - Classificação STUCK quando idade > `stuck_dias` — sinaliza revisão humana

Não toca `carros_sa/models.py` (contrato imutável). Só consulta campos existentes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, select

from carros_sa.config import get_settings
from carros_sa.models import AvaliacaoLote, LaudoCache, Lote


@dataclass(frozen=True)
class LaudoTravado:
    lote_id: str
    marca: str
    modelo: str
    ano: int
    confidence: float | None
    idade_dias: int  # desde scraped_at
    ultima_tentativa_dias: int | None  # desde extraido_em; None se LaudoCache ausente
    tem_avaliacao: bool
    stuck: bool  # idade > stuck_dias sem melhorar
    motivo: str  # "sem_laudo_cache" | "confidence_baixa" | "sem_avaliacao"


def _agora() -> datetime:
    # Wrapper pra permitir mock em testes sem freezegun.
    return datetime.utcnow()


def listar_laudos_travados(
    session: Session,
    empresa_id: str,
    *,
    stuck_dias: int = 3,
    incluir_sem_avaliacao: bool = True,
) -> list[LaudoTravado]:
    """Lista lotes ativos que precisam de atenção.

    Critério "ativo": `fim_em` futuro OU ausente (sinal de coleta recente).
    Critério "travado": uma das três condições:
      1. Tem Lote mas não tem LaudoCache — extração nunca rodou
      2. Tem LaudoCache mas `confidence < laudo_confidence_ok_threshold`
      3. Tem Lote + LaudoCache bom mas sem AvaliacaoLote (erro pós-laudo) —
         só quando `incluir_sem_avaliacao=True`

    `stuck=True` quando `idade_dias > stuck_dias` — lote já teve N chances de
    reprocessar e continua ruim. Sinal forte pra revisão manual.
    """
    threshold = get_settings().laudo_confidence_ok_threshold
    agora = _agora()

    ids_avaliados = {
        r.lote_id for r in session.exec(
            select(AvaliacaoLote).where(AvaliacaoLote.empresa_id == empresa_id)
        ).all()
    }

    # Pula lotes sintéticos do importador histórico — não têm URL real.
    lotes = [
        lote for lote in session.exec(select(Lote)).all()
        if lote.leilao != "historico_offline"
        and (lote.fim_em is None or lote.fim_em >= agora)
    ]

    travados: list[LaudoTravado] = []
    for lote in lotes:
        laudo = session.get(LaudoCache, lote.id)
        idade_dias = (agora - lote.scraped_at).days if lote.scraped_at else 0

        motivo: str | None = None
        confidence = None
        ultima_tentativa_dias = None

        if laudo is None:
            motivo = "sem_laudo_cache"
        else:
            confidence = laudo.confidence
            ultima_tentativa_dias = (
                (agora - laudo.extraido_em).days if laudo.extraido_em else None
            )
            if (confidence or 0) < threshold:
                motivo = "confidence_baixa"
            elif incluir_sem_avaliacao and lote.id not in ids_avaliados:
                motivo = "sem_avaliacao"

        if motivo is None:
            continue

        travados.append(LaudoTravado(
            lote_id=lote.id,
            marca=lote.marca or "?",
            modelo=lote.modelo or "?",
            ano=lote.ano or 0,
            confidence=confidence,
            idade_dias=idade_dias,
            ultima_tentativa_dias=ultima_tentativa_dias,
            tem_avaliacao=(lote.id in ids_avaliados),
            stuck=(idade_dias > stuck_dias),
            motivo=motivo,
        ))

    # Ordena: stuck primeiro, mais antigo primeiro.
    return sorted(travados, key=lambda t: (not t.stuck, -t.idade_dias))


def resumo(travados: list[LaudoTravado]) -> dict[str, int]:
    """Contagem agregada pra log/planilha."""
    return {
        "total": len(travados),
        "stuck": sum(1 for t in travados if t.stuck),
        "sem_laudo_cache": sum(1 for t in travados if t.motivo == "sem_laudo_cache"),
        "confidence_baixa": sum(1 for t in travados if t.motivo == "confidence_baixa"),
        "sem_avaliacao": sum(1 for t in travados if t.motivo == "sem_avaliacao"),
    }
