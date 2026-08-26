"""SAG knowledge backend over Streamable HTTP MCP (decision 4/5).

Minimal MCP client: ``initialize`` handshake, Bearer token injection, one
JSON-RPC ``tools/call`` per knowledge operation, JSON or SSE response handling,
bounded result extraction and clean shutdown. The search/read tool mapping is
the SAG integration point: the tool names and argument keys default to the
original SAG contract and can be overridden per deployment.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping

import httpx

from tau_coding.dataquery.backends.base import (
    EvidenceContent,
    KnowledgeBackend,
    KnowledgeEvidence,
    KnowledgeExchange,
    KnowledgeExchangeCallback,
)
from tau_coding.dataquery.config import DEFAULT_SAG_QUESTION_TEMPLATE
from tau_coding.dataquery.service import KnowledgeError

# SAG tool names and argument keys (integration point, overridable per instance).
_SAG_TOOL_SEARCH = "search"
_SAG_TOOL_READ = "get_chunk"
_SAG_ARG_QUERY = "query"
_SAG_ARG_SOURCE = "source_id"
_SAG_ARG_DOCUMENT = "chunk_id"
_SAG_TEXT_HIT_HEADER = re.compile(
    r"^\[(\d+)\]\s(.+?)(?:\(|（)(chunk_id|document_id)=([^)）]+)(?:\)|）)\s*$",
    re.MULTILINE,
)
# Appended after the rewritten question when a retry carries a failed SQL +
# database error log, so SAG's Q&A interface can correct the SQL.
_SAG_RETRY_FEEDBACK_PREFIX = (
    "\n\n上一轮根据知识库生成的SQL执行失败，请参考知识库修正后重新给出完整SQL。"
    "只输出与用户提问相关的字段，不要输出多余字段。错误上下文如下：\n"
)
# Cap for the retry context so a model-supplied error log cannot bloat the
# SAG request or the audit trail.
_MAX_RETRY_CONTEXT_BYTES = 8 * 1024
_MAX_DIRECT_ANSWER_BYTES = 64 * 1024


class SagMcpKnowledgeBackend(KnowledgeBackend):
    """Streamable HTTP MCP client pinned to the SAG knowledge source."""

    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        source_id: str,
        timeout_seconds: float = 60.0,
        search_tool: str = _SAG_TOOL_SEARCH,
        read_tool: str = _SAG_TOOL_READ,
        arg_query: str = _SAG_ARG_QUERY,
        arg_source: str = _SAG_ARG_SOURCE,
        arg_document: str = _SAG_ARG_DOCUMENT,
        protocol_version: str = "2025-03-26",
        probe_query: str = "__probe__",
        search_summary_max_bytes: int = 8 * 1024,
        question_template: str = DEFAULT_SAG_QUESTION_TEMPLATE,
    ) -> None:
        self._endpoint = endpoint
        self._token = token
        self._source_id = source_id
        self._search_tool = search_tool
        self._read_tool = read_tool
        self._arg_query = arg_query
        self._arg_source = arg_source
        self._arg_document = arg_document
        self._timeout_seconds = timeout_seconds
        self._protocol_version = protocol_version
        self._probe_query = probe_query
        self._search_summary_max_bytes = search_summary_max_bytes
        self._question_template = question_template
        self._client: httpx.AsyncClient | None = None
        self._initialized = False
        self._direct_answers: dict[str, str] = {}

    async def search(
        self,
        question: str,
        *,
        limit: int = 5,
        retry_context: str | None = None,
        on_exchange: KnowledgeExchangeCallback | None = None,
    ) -> list[KnowledgeEvidence]:
        rewritten_question = _rewrite_question(
            question,
            self._question_template,
            retry_context,
        )
        raw = await self._call(
            self._search_tool,
            {
                self._arg_query: rewritten_question,
                self._arg_source: self._source_id,
                "top_k": limit,
            },
        )
        if on_exchange is not None:
            on_exchange(
                KnowledgeExchange(
                    request=_bounded(rewritten_question, _MAX_DIRECT_ANSWER_BYTES),
                    response=_bounded(_extract_text(raw).strip(), _MAX_DIRECT_ANSWER_BYTES),
                )
            )
        hits = _extract_hits(raw)
        if not hits:
            direct_answer = _bounded(_extract_text(raw).strip(), _MAX_DIRECT_ANSWER_BYTES)
            if direct_answer:
                evidence_id = _direct_answer_id(direct_answer)
                self._direct_answers[evidence_id] = direct_answer
                hits = [
                    {
                        "id": evidence_id,
                        "title": "SAG 问答结果",
                        "summary": direct_answer,
                    }
                ]
        results: list[KnowledgeEvidence] = []
        for hit in hits[:limit]:
            results.append(
                KnowledgeEvidence(
                    evidence_id=_hit_id(hit),
                    title=_hit_title(hit),
                    summary=_bounded(_hit_summary(hit), self._search_summary_max_bytes),
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
        direct_answer = self._direct_answers.get(evidence_id)
        if direct_answer is not None:
            return EvidenceContent(
                evidence_id=evidence_id,
                content=_bounded(direct_answer, max_bytes),
            )
        raw = await self._call(
            self._read_tool,
            {self._arg_document: evidence_id, self._arg_source: source_id or self._source_id},
        )
        return EvidenceContent(
            evidence_id=evidence_id,
            content=_bounded(_extract_text(raw), max_bytes),
        )

    async def test(self) -> None:
        await self._call(
            self._search_tool,
            {self._arg_query: self._probe_query, self._arg_source: self._source_id},
        )

    async def close(self) -> None:
        client = self._client
        self._client = None
        self._initialized = False
        self._direct_answers.clear()
        if client is not None:
            await client.aclose()

    # -- transport -----------------------------------------------------------

    async def _call(self, tool: str, arguments: Mapping[str, object]) -> object:
        client = await self._client_or_raise()
        await self._ensure_initialized(client)
        request_id = f"tau-{int(time.monotonic() * 1000)}"
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": dict(arguments)},
        }
        started = time.monotonic()
        try:
            response = await client.post(
                self._endpoint,
                json=payload,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._token}",
                },
            )
        except httpx.HTTPError as exc:
            raise KnowledgeError(f"knowledge search failed: {type(exc).__name__}") from exc
        elapsed = time.monotonic() - started
        if elapsed > self._timeout_seconds:
            raise KnowledgeError("knowledge search timed out")
        if response.status_code != 200:
            raise KnowledgeError(f"knowledge search failed: HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "")
        body: object
        if "text/event-stream" in content_type:
            body = _parse_sse(response.text)
        else:
            try:
                body = response.json()
            except ValueError as exc:
                raise KnowledgeError("knowledge search returned invalid JSON") from exc
        error = _rpc_error(body)
        if error is not None:
            raise KnowledgeError(f"knowledge search failed: {_bounded(str(error), 300)}")
        result = _rpc_result(body)
        if result is None:
            raise KnowledgeError("knowledge search returned no result")
        return result

    async def _client_or_raise(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_seconds)
        return self._client

    async def _ensure_initialized(self, client: httpx.AsyncClient) -> None:
        if self._initialized:
            return
        try:
            response = await client.post(
                self._endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": "tau-initialize",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": self._protocol_version,
                        "capabilities": {},
                        "clientInfo": {"name": "tau-dataquery", "version": "0.1"},
                    },
                },
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._token}",
                },
            )
        except httpx.HTTPError as exc:
            raise KnowledgeError(f"knowledge connection failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise KnowledgeError(f"knowledge connection failed: HTTP {response.status_code}")
        self._initialized = True


# ---------------------------------------------------------------------------
# Question rewriting (SAG integration point).
# ---------------------------------------------------------------------------


def _rewrite_question(
    question: str,
    template: str,
    retry_context: str | None = None,
) -> str:
    """Rewrite the raw user question into a SAG-targeted instruction.

    The default template asks SAG's Q&A interface to reference the knowledge
    base's SQL templates and directly produce SQL for the exact period and
    indicator asked, resolving entity code/caliber itself and emitting only the
    fields the user's question needs. A ``retry_context`` (failed SQL + database
    error) is appended so the knowledge source can correct the SQL. An empty or
    placeholder-only template passes the question through unchanged.
    """
    rewritten = question.strip()
    if template and "{question}" in template:
        rewritten = template.replace("{question}", question.strip())
    elif template:
        rewritten = f"{template.rstrip()}\n\n{question.strip()}"
    if retry_context:
        bounded_error = _bounded(retry_context.strip(), _MAX_RETRY_CONTEXT_BYTES)
        rewritten = f"{rewritten}{_SAG_RETRY_FEEDBACK_PREFIX}{bounded_error}"
    return rewritten


def _direct_answer_id(answer: str) -> str:
    """Return an opaque stable id for a direct answer cached by this backend."""
    digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()[:32]
    return f"sag_answer_{digest}"


def _parse_sse(text: str) -> object:
    """Parse an SSE stream and return the first event's JSON payload."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data = line.removeprefix("data:").strip()
            if data and data != "[DONE]":
                try:
                    return json.loads(data)
                except ValueError:
                    continue
    raise KnowledgeError("knowledge search returned an invalid event stream")


def _rpc_error(body: object) -> object | None:
    if isinstance(body, Mapping) and body.get("error") is not None:
        return body.get("error")
    return None


def _rpc_result(body: object) -> object | None:
    if not isinstance(body, Mapping):
        return None
    return body.get("result")


def _extract_hits(raw: object) -> list[Mapping[str, object]]:
    """Extract search hits from the SAG tool result (tolerant parsing)."""
    structured = _structured(raw)
    if isinstance(structured, list):
        items = [item for item in structured if isinstance(item, Mapping)]
        if items:
            return items
    if isinstance(structured, Mapping):
        for key in ("hits", "results", "documents", "items", "evidence"):
            value = structured.get(key)
            if isinstance(value, list):
                items = [item for item in value if isinstance(item, Mapping)]
                if items:
                    return items
    text = _extract_text(raw)
    parsed = _try_parse_json(text)
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, Mapping)]
    if isinstance(parsed, Mapping):
        for key in ("hits", "results", "documents", "items"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
    text_hits = _parse_text_hits(text)
    if text_hits:
        return text_hits
    return []


def _parse_text_hits(text: str) -> list[Mapping[str, object]]:
    """Parse SAG MCP search/grep text blocks into hit mappings.

    Real SAG deployments return ranked evidence as plain text:

    ``[1] Section title (chunk_id=uuid)`` followed by the chunk body.
    """
    if not text.strip():
        return []
    matches = list(_SAG_TEXT_HIT_HEADER.finditer(text))
    if not matches:
        return []
    hits: list[Mapping[str, object]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        evidence_id = match.group(4).strip()
        title = match.group(2).strip()
        hits.append(
            {
                "id": evidence_id,
                "chunk_id": evidence_id,
                "title": title,
                "summary": body,
                "text": body,
            }
        )
    return hits


def _structured(raw: object) -> object:
    if isinstance(raw, Mapping):
        value = raw.get("structuredContent")
        if value is not None:
            return value
    return None


def _extract_text(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, Mapping):
        return ""
    structured = raw.get("structuredContent")
    if isinstance(structured, str):
        return structured
    content = raw.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        if parts:
            return "\n".join(parts)
    return _try_json_text(structured)


def _hit_id(hit: Mapping[str, object]) -> str:
    for key in ("id", "chunk_id", "evidenceId", "documentId", "docId", "uri"):
        value = hit.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return str(hash(tuple(sorted((str(k), str(v)) for k, v in hit.items()))))


def _hit_title(hit: Mapping[str, object]) -> str:
    for key in ("title", "name", "heading"):
        value = hit.get(key)
        if isinstance(value, str) and value.strip():
            return _bounded(value, 500)
    return _hit_id(hit)


def _hit_summary(hit: Mapping[str, object]) -> str:
    for key in ("summary", "snippet", "excerpt", "text", "content"):
        value = hit.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _bounded(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _try_parse_json(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _try_json_text(value: object) -> str:
    if isinstance(value, (Mapping, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return ""
