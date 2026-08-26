"""Fail-closed knowledge backend used when Agent citation expansion is absent."""

from __future__ import annotations

from tau_coding.dataquery.backends.base import (
    MAX_READ_BYTES,
    MAX_SEARCH_RESULTS,
    EvidenceContent,
    KnowledgeEvidence,
    KnowledgeExchangeCallback,
)
from tau_coding.dataquery.service import KnowledgeError


class UnavailableKnowledgeBackend:
    """Keep the workflow port complete without pretending MCP is configured."""

    citation_expansion_available = False

    async def search(
        self,
        question: str,
        *,
        limit: int = MAX_SEARCH_RESULTS,
        retry_context: str | None = None,
        on_exchange: KnowledgeExchangeCallback | None = None,
    ) -> list[KnowledgeEvidence]:
        del question, limit, retry_context, on_exchange
        raise KnowledgeError("legacy SAG MCP search is not configured")

    async def read(
        self,
        evidence_id: str,
        *,
        source_id: str | None = None,
        max_bytes: int = MAX_READ_BYTES,
    ) -> EvidenceContent:
        del evidence_id, source_id, max_bytes
        raise KnowledgeError("citation_expansion_unavailable")

    async def test(self) -> None:
        raise KnowledgeError("citation expansion is not configured")

    async def close(self) -> None:
        return None


__all__ = ["UnavailableKnowledgeBackend"]
