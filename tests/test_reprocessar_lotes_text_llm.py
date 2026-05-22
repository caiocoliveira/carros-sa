"""Guard contra regressão: `reprocessar_lotes_do_db._run` precisa passar
`text_llm_client` pro `_pipeline_lote` igual `triagem_diaria.py` faz.

Histórico (2026-05-22):
  Triagem inicial chamava `orquestrar(..., text_llm_client=...)` → `_pipeline_lote`
  recebia text_llm_client → camada 4 (DD4 — LLM textual vendor-agnostic) ativa.
  Retry diário (`scripts/reprocessar_lotes_do_db.py`) chamava `_pipeline_lote`
  diretamente SEM `text_llm_client` → camada 4 nunca disparava no retry.
  Resultado: lotes de vendor fora do Auto Avaliar (Zachi/Procemax/Terceira
  Visão/etc) que entram pelo retry ficavam presos em confidence=0.0 — visual
  devolvia 0.0+listas vazias (correto pra vendor fora do template), persistia
  como-está, tentativa++, 3 ciclos → circuit-breaker (II) congelava sem nunca
  ter rodado camada 4 sobre o PDF. Audit `--strict` reportava `cache_confianca_baixa`
  indefinidamente.

  Mesmo padrão **P5b** ("mesma operação em dois lugares diverge") aplicado a
  CHAMADAS do mesmo entrypoint com defaults divergentes. Cobertura por
  inspeção do source — testar `_run` end-to-end exigiria mock de Playwright
  + auth + tudo mais, e o que importa é exatamente que a linha de chamada
  pase o kwarg.
"""

from __future__ import annotations

from pathlib import Path


def _ler_script():
    return (
        Path(__file__).resolve().parent.parent
        / "scripts" / "reprocessar_lotes_do_db.py"
    ).read_text(encoding="utf-8")


def test_reprocessar_importa_build_default_text_client():
    """Sem o import, text_llm_client nem existe no escopo do retry."""
    src = _ler_script()
    assert "build_default_text_client" in src, (
        "reprocessar_lotes_do_db precisa importar build_default_text_client "
        "pra ativar camada 4 do extrator (DD4) no retry — paridade com triagem_diaria."
    )


def test_reprocessar_chama_pipeline_lote_com_text_llm_client():
    """Linha de chamada de `_pipeline_lote` precisa incluir text_llm_client.

    Sem isso, defesa em camadas do extrator (DD4) só vale na 1ª triagem; lote
    que entra pelo retry (cron diário 10:00/16:00 UTC) nunca recebe a camada
    LLM textual e fica preso pra sempre.
    """
    src = _ler_script()
    # Aceita formato multi-linha — assertiva sobre presença do kwarg na chamada
    # de `_pipeline_lote`, não sobre formatting exato.
    assert "_pipeline_lote(" in src
    chamada_inicio = src.index("_pipeline_lote(")
    # Janela generosa pra cobrir formatting multi-linha.
    trecho = src[chamada_inicio:chamada_inicio + 400]
    assert "text_llm_client=" in trecho, (
        "_pipeline_lote no reprocessar precisa receber `text_llm_client=...` "
        f"(camada 4 do DD4). Trecho atual:\n{trecho}"
    )


def test_triagem_diaria_continua_passando_text_llm_client():
    """Espelho de paridade: triagem é a referência. Se ela parar de passar,
    a comparação acima vira teatro."""
    src = (
        Path(__file__).resolve().parent.parent
        / "scripts" / "triagem_diaria.py"
    ).read_text(encoding="utf-8")
    assert "text_llm_client=text_llm_client" in src, (
        "triagem_diaria.py é a referência canônica de passagem do text_llm_client. "
        "Se mudou aqui, atualize tests/test_reprocessar_lotes_text_llm.py e o "
        "comentário em reprocessar_lotes_do_db.py."
    )
