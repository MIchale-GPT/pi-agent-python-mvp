"""Deterministic in-memory backends for workflow tests."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence

from tau_agent.tools import ToolCancellationToken
from tau_coding.dataquery.backends.base import (
    EvidenceContent,
    KnowledgeBackend,
    KnowledgeEvidence,
    KnowledgeExchange,
    KnowledgeExchangeCallback,
    QueryBackend,
    QueryColumn,
    QueryResult,
    truncate_result_rows,
)

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "what",
        "which",
        "where",
        "when",
        "how",
        "are",
        "was",
        "were",
        "show",
        "give",
        "list",
        "using",
        "based",
        "data",
    }
)


class FakeKnowledgeBackend(KnowledgeBackend):
    """In-memory knowledge backend seeded with evidence documents."""

    def __init__(
        self,
        documents: dict[str, str] | None = None,
        *,
        search_delay: float = 0.0,
    ) -> None:
        self.documents = dict(documents or {})
        self.search_delay = search_delay
        self.search_calls: list[str] = []
        self.search_retry_contexts: list[str] = []
        self.read_calls: list[str] = []
        self.read_source_ids: list[str | None] = []
        self.closed = False

    async def search(
        self,
        question: str,
        *,
        limit: int = 5,
        retry_context: str | None = None,
        on_exchange: KnowledgeExchangeCallback | None = None,
    ) -> list[KnowledgeEvidence]:
        await self._maybe_delay()
        self.search_calls.append(question)
        if retry_context:
            self.search_retry_contexts.append(retry_context)
        keywords = {
            word.lower()
            for word in re.findall(r"[a-zA-Z0-9_]+", question)
            if len(word) >= 3 and word.lower() not in _STOPWORDS
        }
        results: list[KnowledgeEvidence] = []
        for evidence_id, content in self.documents.items():
            title = content.splitlines()[0] if content else evidence_id
            haystack = f"{title}\n{content}".lower()
            if not keywords or any(keyword in haystack for keyword in keywords):
                results.append(
                    KnowledgeEvidence(
                        evidence_id=evidence_id,
                        title=title[:200],
                        summary=content[: 8 * 1024],
                    )
                )
        results = results[:limit]
        if on_exchange is not None:
            on_exchange(
                KnowledgeExchange(
                    request=question,
                    response="\n\n".join(item.summary for item in results),
                )
            )
        return results

    async def read(
        self,
        evidence_id: str,
        *,
        source_id: str | None = None,
        max_bytes: int = 64 * 1024,
    ) -> EvidenceContent:
        await self._maybe_delay()
        self.read_calls.append(evidence_id)
        self.read_source_ids.append(source_id)
        content = self.documents.get(evidence_id)
        if content is None:
            raise KeyError(f"unknown evidence: {evidence_id}")
        return EvidenceContent(evidence_id=evidence_id, content=content[:max_bytes])

    async def test(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def _maybe_delay(self) -> None:
        if self.search_delay:
            await asyncio.sleep(self.search_delay)


class FakeQueryBackend(QueryBackend):
    """In-memory query backend serving a canned table."""

    def __init__(
        self,
        table_rows: list[list[object]] | None = None,
        *,
        columns: list[QueryColumn] | None = None,
        fail_with: Exception | None = None,
        row_factory: Sequence[object] | None = None,
    ) -> None:
        self.columns = columns or [QueryColumn("id", "integer"), QueryColumn("name", "text")]
        self.rows = table_rows if table_rows is not None else [[1, "alice"], [2, "bob"]]
        self.fail_with = fail_with
        self.row_factory = row_factory
        self.statements: list[str] = []
        self.param_sets: list[tuple[object, ...]] = []
        self.cancelled = False
        self.closed = False

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
        self.statements.append(sql)
        self.param_sets.append(tuple(params))
        if self.fail_with is not None:
            raise self.fail_with
        if signal is not None:
            for _ in range(10):
                if signal.is_cancelled():
                    self.cancelled = True
                    raise TimeoutError("query cancelled")
                await asyncio.sleep(0)
        rows = self.rows
        if self.row_factory is not None:
            rows = [list(self.row_factory)]
        outcome = truncate_result_rows(
            rows[: max_rows + 1],
            max_rows=max_rows,
            max_result_bytes=max_result_bytes,
            max_cell_bytes=max_cell_bytes,
        )
        return QueryResult(
            columns=list(self.columns),
            rows=outcome.rows,
            truncated=outcome.truncated,
            truncation_reasons=list(outcome.reasons),
            previewed_cells=outcome.previewed_cells,
            elapsed_ms=5,
            statement=sql,
        )

    async def test_connection(self) -> tuple[int, str | None]:
        if self.fail_with is not None:
            return 3, "connection refused (fake)"
        return 4, None

    async def close(self) -> None:
        self.closed = True
