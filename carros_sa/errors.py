"""Hierarquia de erros do pipeline.

Antes: tudo `except Exception` — logs ausentes ou strings soltas em
`ResultadoLote.erro`. Depurar um lote que falhou virava grep no stderr.

Agora: `PipelineError` base carrega `lote_id` + `motivo` estruturado. Callers
externos (scripts, testes) podem capturar especificamente em vez de pegar
qualquer coisa. Safety-net do orquestrador continua logando via
`logging.getLogger(__name__)`.

Nota: a primeira versão deste módulo declarava PDFInvalidoError,
PDFDownloadError e LaudoExtractionError — removidas porque nunca foram
levantadas. O pipeline degrada silenciosamente com logger.warning quando
essas falhas acontecem (comportamento correto: lote com PDF ruim ganha
`_laudo_sem_pdf(flags)` e reprocessa na próxima run). Se um dia quisermos
virar exceções, é fácil adicionar.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base pra falhas previstas do pipeline de avaliação de lote."""

    def __init__(self, motivo: str, *, lote_id: str | None = None):
        self.motivo = motivo
        self.lote_id = lote_id
        super().__init__(motivo)


class FipeIndisponivel(PipelineError):
    """FIPE não tem catálogo pra essa marca/modelo (motos, carros exóticos).

    Levantada pelo `_estagio_mercado` do orquestrador (indiretamente, via
    `_DescarteLote`) quando `avaliar_mercado` devolve LookupError. Sem FIPE
    o precificador perde a âncora — lote vai pro descarte com motivo claro.
    """
