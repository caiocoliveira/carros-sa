"""Importador de histórico Compra/Venda → tabela Arrematado.

Ground-truth real do operador (planilhas físicas, vendas pré-pipeline) entra aqui
como CSV. O schema do `Arrematado` exige FK pra `lote.id`, então cada linha
gera um `Lote` sintético (`leilao="historico_offline"`) pra preservar a
integridade referencial sem mexer em [carros_sa/models.py](carros_sa/models.py).

Arquivos consumidos: `data/historico/<empresa_id>_arrematado.csv`.

Fluxo:
    parse_csv(path) → List[HistoricoRow]
    importar_historico(rows, empresa_id, session) → ImportResult

Idempotência: re-importar o mesmo CSV não duplica linhas. Matching por
(marca, modelo_normalizado, ano, valor_compra, data_compra) — atualiza linha
existente em vez de adicionar.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session, select

from carros_sa.models import Arrematado, Empresa, Lote
from carros_sa.tenancy import carregar_empresa


# =============================================================================
# Pydantic — uma linha do CSV
# =============================================================================

class HistoricoRow(BaseModel):
    """Uma linha de venda (ou compra-no-pátio) importada do CSV.

    `data_venda` opcional: vazio = "no pátio, ainda não vendido". Nesses casos
    `valor_venda` é o preço sugerido de anúncio, não realizado.
    """

    marca: str
    modelo: str
    ano: int
    km: Optional[int] = None
    valor_compra: int = Field(gt=0)
    data_compra: Optional[datetime] = None
    custos_extras: Optional[int] = None
    valor_venda: Optional[int] = None
    data_venda: Optional[datetime] = None
    observacoes: str = ""

    @field_validator("marca", "modelo")
    @classmethod
    def _strip_obrigatorio(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("marca/modelo não pode ser vazio")
        return v


# =============================================================================
# Resultado da importação
# =============================================================================

@dataclass
class ImportResult:
    criados: int = 0
    atualizados: int = 0
    erros: List[Tuple[int, str]] = field(default_factory=list)  # (linha_csv, mensagem)

    @property
    def total_processados(self) -> int:
        return self.criados + self.atualizados


# =============================================================================
# Helpers
# =============================================================================

_NAO_ALFANUM = re.compile(r"[^a-z0-9]+")


def _slug(s: str) -> str:
    """Normaliza string pra slug ASCII lower-case (matching de modelo)."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().strip()
    return _NAO_ALFANUM.sub("_", s).strip("_")


def lote_id_sintetico(empresa_id: str, marca: str, modelo: str, ano: int, idx: int) -> str:
    """ID determinístico pra Lote sintético — permite upsert idempotente.

    Formato: hist_<empresa>_<marca>_<modelo>_<ano>_<idx>
    Ex: hist_carros_uberlandia_vw_polo_track_2024_001
    """
    return f"hist_{empresa_id}_{_slug(marca)}_{_slug(modelo)}_{ano}_{idx:03d}"


def _parse_data(s: Optional[str]) -> Optional[datetime]:
    """Parse data ISO (YYYY-MM-DD) ou BR (DD/MM/YYYY). Vazio → None."""
    if s is None or not s.strip():
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"data inválida: {s!r} (esperado YYYY-MM-DD ou DD/MM/YYYY)")


def _parse_int_opcional(s: Optional[str]) -> Optional[int]:
    """Parse int. Vazio → None. Aceita '1.500,00', '1500', '1500.00'."""
    if s is None or not s.strip():
        return None
    # Remove separadores BR e decimais
    cleaned = s.strip().replace("R$", "").replace(" ", "")
    cleaned = re.sub(r"[.,]\d{2}$", "", cleaned)  # remove decimais
    cleaned = cleaned.replace(".", "").replace(",", "")
    return int(cleaned)


# =============================================================================
# Parse CSV
# =============================================================================

def parse_csv(path: Path) -> Tuple[List[HistoricoRow], List[Tuple[int, str]]]:
    """Lê CSV → (rows válidas, erros [(linha, msg)]).

    Linha 1 é cabeçalho; primeira linha de dado é linha 2 no log de erro.
    """
    rows: List[HistoricoRow] = []
    erros: List[Tuple[int, str]] = []

    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for i, raw in enumerate(reader, start=2):
            try:
                row = HistoricoRow(
                    marca=raw["marca"],
                    modelo=raw["modelo"],
                    ano=int(raw["ano"]),
                    km=_parse_int_opcional(raw.get("km")),
                    valor_compra=_parse_int_opcional(raw.get("valor_compra")) or 0,
                    data_compra=_parse_data(raw.get("data_compra")),
                    custos_extras=_parse_int_opcional(raw.get("custos_extras")),
                    valor_venda=_parse_int_opcional(raw.get("valor_venda")),
                    data_venda=_parse_data(raw.get("data_venda")),
                    observacoes=(raw.get("observacoes") or "").strip(),
                )
                rows.append(row)
            except Exception as exc:
                erros.append((i, str(exc)))

    return rows, erros


# =============================================================================
# Importação principal
# =============================================================================

def _garantir_empresa(empresa_id: str, session: Session) -> None:
    """Cria linha Empresa se não existir (FK do Arrematado depende dela)."""
    if session.get(Empresa, empresa_id) is not None:
        return
    cfg = carregar_empresa(empresa_id)
    session.add(Empresa(
        id=empresa_id,
        nome=cfg.nome,
        config_yaml_path=str(Path("config/empresas") / f"{empresa_id}.yaml"),
    ))


def importar_historico(
    rows: List[HistoricoRow],
    empresa_id: str,
    session: Session,
) -> ImportResult:
    """Persiste rows como Lote sintético + Arrematado. Idempotente.

    Identidade lógica de uma linha = (marca, modelo, ano, valor_compra, data_compra).
    Re-rodar atualiza em vez de duplicar.
    """
    result = ImportResult()
    _garantir_empresa(empresa_id, session)

    # Carrega lotes históricos existentes pra essa empresa (cache de lookup)
    existentes = {
        l.id: l for l in session.exec(
            select(Lote).where(Lote.leilao == "historico_offline")
        ).all()
    }

    # Próximo idx por (marca, modelo, ano) — sequencial determinístico
    contadores: dict = {}
    for lote_id in existentes:
        match = re.match(
            rf"hist_{re.escape(empresa_id)}_(.+)_(\d{{4}})_(\d{{3}})$", lote_id
        )
        if match:
            chave = (match.group(1), int(match.group(2)))
            contadores[chave] = max(contadores.get(chave, 0), int(match.group(3)))

    for i, row in enumerate(rows, start=1):
        try:
            chave_modelo = (f"{_slug(row.marca)}_{_slug(row.modelo)}", row.ano)

            # Tenta achar Lote existente pelo padrão (idempotência)
            # Match secundário por (marca/modelo/ano/valor_compra) pra detectar mesmo carro
            lote_existente = None
            for lid, lote_obj in existentes.items():
                if (
                    lote_obj.marca == row.marca
                    and lote_obj.modelo == row.modelo
                    and lote_obj.ano == row.ano
                    and lote_obj.lance_atual == row.valor_compra
                ):
                    lote_existente = lote_obj
                    break

            if lote_existente is None:
                contadores[chave_modelo] = contadores.get(chave_modelo, 0) + 1
                lote_id = lote_id_sintetico(
                    empresa_id, row.marca, row.modelo, row.ano,
                    contadores[chave_modelo],
                )
                lote = Lote(
                    id=lote_id,
                    leilao="historico_offline",
                    url="",
                    marca=row.marca,
                    modelo=row.modelo,
                    ano=row.ano,
                    km=row.km,
                    lance_atual=row.valor_compra,
                    origem_cidade=None,
                    origem_uf=None,
                    raw_json={
                        "origem": "import_historico",
                        "fonte": "planilha_caio_2026_04_16",
                        "observacoes": row.observacoes,
                    },
                )
                session.add(lote)
                existentes[lote_id] = lote
                criado_lote = True
            else:
                lote_id = lote_existente.id
                criado_lote = False

            # Upsert Arrematado
            arr_existente = session.exec(
                select(Arrematado).where(
                    Arrematado.lote_id == lote_id,
                    Arrematado.empresa_id == empresa_id,
                )
            ).first()

            data_compra = row.data_compra or datetime(2026, 1, 1)  # placeholder se vazio

            if arr_existente:
                arr_existente.preco_real = row.valor_compra
                arr_existente.data = data_compra
                arr_existente.gastos_reforma_real = row.custos_extras
                arr_existente.vendido_por = row.valor_venda if row.data_venda else None
                arr_existente.vendido_em = row.data_venda
                result.atualizados += 1
            else:
                session.add(Arrematado(
                    empresa_id=empresa_id,
                    lote_id=lote_id,
                    preco_real=row.valor_compra,
                    data=data_compra,
                    gastos_reforma_real=row.custos_extras,
                    # vendido_por só preenche se a venda já aconteceu (data_venda real)
                    vendido_por=row.valor_venda if row.data_venda else None,
                    vendido_em=row.data_venda,
                ))
                if criado_lote:
                    result.criados += 1
                else:
                    result.atualizados += 1
        except Exception as exc:
            result.erros.append((i, str(exc)))

    session.commit()
    return result
