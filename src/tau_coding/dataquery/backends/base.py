"""Backend protocols and value types for the data-query extension.

``KnowledgeBackend`` is the SAG knowledge surface (search + read). ``QueryBackend``
is the DWS read-only query surface. Both are narrow ports so the workflow can be
tested against deterministic fakes (testing decision 1/3/4).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from tau_agent.tools import ToolCancellationToken

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

# --- knowledge backend -----------------------------------------------------

MAX_SEARCH_RESULTS = 5
MAX_SEARCH_SUMMARY_BYTES = 8 * 1024
MAX_READ_BYTES = 64 * 1024
MAX_BUNDLE_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class KnowledgeEvidence:
    """One search hit returned to the model (bounded, decision 4)."""

    evidence_id: str
    title: str
    summary: str


@dataclass(frozen=True, slots=True)
class EvidenceContent:
    """Expanded content for one evidence item (bounded)."""

    evidence_id: str
    content: str


@dataclass(frozen=True, slots=True)
class KnowledgeExchange:
    """One bounded request/response exchange with the knowledge provider."""

    request: str
    response: str


KnowledgeExchangeCallback = Callable[[KnowledgeExchange], None]


class KnowledgeBackend(Protocol):
    """Read-only SAG knowledge surface."""

    async def search(
        self,
        question: str,
        *,
        limit: int = MAX_SEARCH_RESULTS,
        retry_context: str | None = None,
        on_exchange: KnowledgeExchangeCallback | None = None,
    ) -> list[KnowledgeEvidence]:
        """Semantic + exact search over the fixed knowledge source.

        ``retry_context`` optionally carries the previously generated SQL and the
        database error log so the knowledge source can correct the SQL.
        ``on_exchange`` receives bounded display-only observability data.
        """
        ...

    async def read(
        self,
        evidence_id: str,
        *,
        source_id: str | None = None,
        max_bytes: int = MAX_READ_BYTES,
    ) -> EvidenceContent:
        """Expand one evidence item's full content."""
        ...

    async def test(self) -> None:
        """Verify connectivity (used by the Web test-connection flow)."""
        ...

    async def close(self) -> None:
        """Release any client resources."""
        ...


# --- query backend ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueryColumn:
    name: str
    type: str = "text"


@dataclass(slots=True)
class QueryResult:
    """A completed read-only query result (bounded, decision 10)."""

    columns: list[QueryColumn]
    rows: list[list[str | None]]
    truncated: bool
    truncation_reasons: list[str]
    previewed_cells: int = 0
    elapsed_ms: int = 0
    statement: str = ""

    def to_model_dict(self) -> dict[str, object]:
        """Return the model-visible bounded payload (decision 11)."""
        return {
            "columns": [column.name for column in self.columns],
            "rows": self.rows,
            "rowCount": len(self.rows),
            "truncated": self.truncated,
            "truncationReasons": list(self.truncation_reasons),
            "previewedCells": self.previewed_cells,
            "elapsedMs": self.elapsed_ms,
        }


class QueryBackend(Protocol):
    """Read-only DWS query surface."""

    async def execute(
        self,
        sql: str,
        params: Sequence[object],
        *,
        timeout_seconds: int,
        max_rows: int,
        max_result_bytes: int,
        max_cell_bytes: int,
        signal: ToolCancellationToken | None = None,
    ) -> QueryResult:
        """Run one statement in a read-only transaction and return bounded rows."""
        ...

    async def test_connection(self) -> tuple[int, str | None]:
        """Connect, open a read-only transaction and probe; return (elapsed_ms, error)."""
        ...

    async def close(self) -> None:
        """Close the pool and cancel any running statements."""
        ...


# --- truncation (decision 10) ----------------------------------------------


@dataclass(frozen=True, slots=True)
class TruncationOutcome:
    rows: list[list[str | None]]
    truncated: bool
    reasons: tuple[str, ...]
    previewed_cells: int


def serialize_cell(value: object) -> str:
    """Serialize one result cell to the display/model text form."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def truncate_result_rows(
    rows: Sequence[Sequence[object]],
    *,
    max_rows: int,
    max_result_bytes: int,
    max_cell_bytes: int,
) -> TruncationOutcome:
    """Apply cell preview, row and byte limits to fetched rows.

    Order follows the PRD: first cap each cell (``cell_limit`` preview), then
    drop a row whose complete previewed size would exceed the total byte budget
    (``byte_limit``, never half rows), and finally stop at ``max_rows``
    (``row_limit``). Any applied limit sets ``truncated``.
    """
    kept: list[list[str | None]] = []
    reasons: list[str] = []
    previewed_cells = 0
    total_bytes = 0

    for index, row in enumerate(rows):
        if index >= max_rows:
            if "row_limit" not in reasons:
                reasons.append("row_limit")
            break

        previewed: list[str | None] = []
        for cell in row:
            if cell is None:
                previewed.append(None)
                continue
            text = serialize_cell(cell)
            size = len(text.encode("utf-8"))
            if size > max_cell_bytes:
                previewed_cells += 1
                if "cell_limit" not in reasons:
                    reasons.append("cell_limit")
                preview = f"{text[:max_cell_bytes]}…[+{size - max_cell_bytes} bytes omitted]"
                previewed.append(preview)
            else:
                previewed.append(text)

        row_bytes = (
            sum(4 if cell is None else len(cell.encode("utf-8")) for cell in previewed)
            + len(previewed)
            - 1
        )
        if total_bytes + row_bytes > max_result_bytes:
            if "byte_limit" not in reasons:
                reasons.append("byte_limit")
            break
        kept.append(previewed)
        total_bytes += row_bytes

    truncated = bool(reasons)
    return TruncationOutcome(
        rows=kept,
        truncated=truncated,
        reasons=tuple(reasons),
        previewed_cells=previewed_cells,
    )


# --- configuration sources -------------------------------------------------


class ConnectionFactory(Protocol):
    """Deferred real-backend construction (kept out of the workflow)."""

    def __call__(self) -> QueryBackend: ...


class KnowledgeFactory(Protocol):
    def __call__(self) -> KnowledgeBackend: ...
