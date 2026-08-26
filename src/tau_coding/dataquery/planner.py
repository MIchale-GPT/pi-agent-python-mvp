"""Application-layer SQL planner port for SAG Agent-backed data queries.

The port owns no HTTP, MCP, DWS, Rich, or Textual behavior.  It accepts an
explicit bounded chat history and returns an untrusted cited planning answer;
the data-query service turns citations into run-scoped evidence and Tau still
authors, validates, freezes, and authorizes the executable SQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from tau_agent.tools import ToolCancellationToken


class PlannerMessage(TypedDict):
    """One portable message sent to the configured SQL planner."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class PlannerCitation:
    """One provider citation before Tau issues a run-scoped evidence id."""

    provider_id: str | None
    source_id: str | None
    title: str
    snippet: str


@dataclass(frozen=True, slots=True)
class PlannerAnswer:
    """One bounded, untrusted planner response and its observable exchange."""

    answer: str
    citations: tuple[PlannerCitation, ...]
    request: str
    response: str


class SqlPlanner(Protocol):
    """Port implemented by SAG Agent chat and deterministic test fakes."""

    async def plan(
        self,
        messages: Sequence[PlannerMessage],
        *,
        signal: ToolCancellationToken | None = None,
    ) -> PlannerAnswer:
        """Return one cited answer without executing SQL."""
        ...

    async def test(self) -> None:
        """Verify planner connectivity and authentication."""
        ...

    async def close(self) -> None:
        """Release HTTP or provider resources."""
        ...


def rewrite_planner_question(question: str, template: str, *, max_bytes: int) -> str:
    """Apply the trusted Tau template and cap the final UTF-8 request."""
    stripped = question.strip()
    if template and "{question}" in template:
        rewritten = template.replace("{question}", stripped)
    elif template:
        rewritten = f"{template.rstrip()}\n\n{stripped}"
    else:
        rewritten = stripped
    return bound_utf8(rewritten, max_bytes)


def bound_utf8(value: str, max_bytes: int) -> str:
    """Return a valid UTF-8 prefix with an explicit truncation marker."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker = "…[truncated]"
    marker_bytes = marker.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore")
    return f"{prefix}{marker}"


__all__ = [
    "PlannerAnswer",
    "PlannerCitation",
    "PlannerMessage",
    "SqlPlanner",
    "bound_utf8",
    "rewrite_planner_question",
]
