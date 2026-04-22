"""Hierarquia de erros do pipeline.

Antes: tudo `except Exception` — logs ausentes ou strings soltas em
`ResultadoLote.erro`. Depurar um lote que falhou virava grep no stderr.

Agora: erros específicos carregam `lote_id` + `motivo` estruturado. Safety-net
genérico (wrapper do pipeline, fallback cascata de LLM) fica, mas com log
explícito via `logging.getLogger(__name__)` em vez de silent-pass.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base pra falhas previstas do pipeline de avaliação de lote."""

    def __init__(self, motivo: str, *, lote_id: str | None = None):
        self.motivo = motivo
        self.lote_id = lote_id
        super().__init__(motivo)


class PDFInvalidoError(PipelineError):
    """PDF baixado não é um laudo de carro (ex.: footer institucional)."""


class PDFDownloadError(PipelineError):
    """Falha ao baixar o PDF do laudo (HTTP, timeout, arquivo zerado)."""


class LaudoExtractionError(PipelineError):
    """Extração de laudo falhou tanto na visão quanto na camada textual."""


class FipeIndisponivel(PipelineError):
    """FIPE não tem catálogo pra essa marca/modelo (motos, carros exóticos)."""
