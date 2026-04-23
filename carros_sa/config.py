"""AppSettings — configuração de comportamento do pipeline em um lugar só.

Antes: magic numbers espalhados em 5 arquivos (sleep anti-bot, piso de
imprevistos, retry delays de LLM, thresholds de confidence, etc). Editar um
valor operacional virava caça ao tesouro.

Agora: tudo aqui, tipado, override via env var opcional, carregado uma vez no
processo via `get_settings()`. Segredos (API keys, senhas) NÃO ficam aqui —
seguem lidos ad-hoc pelos clientes que os precisam, já que o lazy loading
deles depende do provider selecionado.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class AppSettings(BaseSettings):
    """Config imutável do processo. Override via env `CARROS_SA_<CAMPO>`."""

    model_config = SettingsConfigDict(
        env_prefix="CARROS_SA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ----- Armazenamento -----
    # NOTA: o path do SQLite vive em `carros_sa/db.py` via env CARROS_SA_DB,
    # não aqui — evita divergência de fonte da verdade. Mantido esse módulo
    # pra comportamento (sleep, retry, thresholds).
    pdf_storage_dir: Path = _PROJECT_ROOT / "data" / "laudos_pdfs"

    # ----- Scraping / anti-bot -----
    # Sleep entre requests da Auto Avaliar. Subido de 2-4s → 5-8s após 49/55
    # requests caírem com HTTP 429 na triagem de 2026-04-16.
    scrape_sleep_min_s: float = 5.0
    scrape_sleep_max_s: float = 8.0
    # Amostra do HTML persistida em Lote.raw_json pra debug offline do parser.
    body_text_sample_bytes: int = 8000

    # ----- Laudo / PDF -----
    # Tamanho mínimo pra aceitar como PDF de laudo (footer institucional tem ~2KB).
    pdf_min_bytes: int = 5_000
    pdf_markers_positivos: tuple[str, ...] = (
        "LAUDO", "INSPEÇÃO", "INSPECAO", "AVALIAÇÃO", "AVALIACAO",
        "VEÍCULO", "VEICULO", "CHASSI", "PLACA",
    )
    pdf_markers_negativos: tuple[str, ...] = (
        "TRANSPARÊNCIA SALARIAL", "TRANSPARENCIA SALARIAL",
        "IGUALDADE SALARIAL", "RELATÓRIO DE TRANSPARÊNCIA",
    )
    # Threshold de confidence do LaudoCache pra considerar "já avaliado" e
    # pular o pipeline. Abaixo disso o retry reprocessa.
    laudo_confidence_ok_threshold: float = 0.6

    # ----- LLM retry -----
    # Retry manual com backoff pra absorver 503 UNAVAILABLE do Gemini Flash.
    llm_retry_delays_s: tuple[int, ...] = (0, 15, 45)
    llm_retry_http_codes: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    # ----- EstimadorReforma -----
    # Premissa operacional: qualquer carro passa pelo pátio gerando uns R$ 1.000
    # de "coisinhas" (retoque pontual, borracha de porta, limpeza profunda).
    reforma_reserva_imprevistos_brl: int = 1_000
    # Incerteza default quando a tabela YAML não especifica.
    reforma_incerteza_pct_default: float = 0.25

    # ----- Frete heurístico (fallback quando haversine não acha cidade) -----
    # UFs adjacentes ao pátio MG (origem do projeto — único pátio real hoje).
    # Quando/se plugar segundo pátio em outro estado, promover pra mapping.
    ufs_adjacentes_mg: frozenset[str] = frozenset(
        {"SP", "RJ", "ES", "BA", "GO", "MS", "DF"}
    )
    frete_km_mesma_uf: int = 150
    frete_km_uf_adjacente: int = 400
    frete_km_uf_distante: int = 700


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Retorna a instância única de AppSettings (cacheada)."""
    return AppSettings()


def reset_settings_cache() -> None:
    """Limpa o cache de settings (uso: testes que mexem em env vars)."""
    get_settings.cache_clear()
