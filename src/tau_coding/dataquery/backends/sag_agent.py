"""SAG Claude-compatible Agent adapter for SQL planning."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Mapping, Sequence
from contextlib import suppress
from typing import Any, cast

import httpx

from tau_agent.tools import ToolCancellationToken
from tau_coding.dataquery.planner import (
    PlannerAnswer,
    PlannerCitation,
    PlannerMessage,
)
from tau_coding.dataquery.service import KnowledgeError

_CHAT_PATH = "/api/v1/openai/{agent_id}/chat/completions"
_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_CONNECTION_PROBE = (
    "请基于当前 Agent 已绑定的知识源返回一个简短、带引用的回答，用于连接测试。"
)


class SagAgentSqlPlanner:
    """Call one configured SAG Agent through its non-streaming chat endpoint."""

    def __init__(
        self,
        *,
        origin: str,
        agent_id: str,
        token: str,
        timeout_seconds: float = 60.0,
        max_response_bytes: int = 256 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = (
            f"{origin.rstrip('/')}"
            f"{_CHAT_PATH.format(agent_id=agent_id)}"
        )
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def plan(
        self,
        messages: Sequence[PlannerMessage],
        *,
        signal: ToolCancellationToken | None = None,
    ) -> PlannerAnswer:
        body: dict[str, object] = {
            "messages": [dict(message) for message in messages],
            "stream": False,
        }
        request_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        client = self._client_or_create()
        try:
            response = await _request_with_cancellation(
                _post_bounded(
                    client,
                    self._endpoint,
                    token=self._token,
                    body=request_text.encode("utf-8"),
                    max_response_bytes=self._max_response_bytes,
                ),
                signal,
            )
        except httpx.HTTPError as exc:
            raise KnowledgeError(f"SAG Agent request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            code = _response_error_code(response)
            suffix = f" ({code})" if code else ""
            raise KnowledgeError(
                f"SAG Agent request failed: HTTP {response.status_code}{suffix}"
            )
        try:
            raw = response.json()
        except ValueError as exc:
            raise KnowledgeError("SAG Agent returned invalid JSON") from exc
        if not isinstance(raw, dict):
            raise KnowledgeError("SAG Agent returned an invalid response object")
        answer = _answer(raw)
        citations = _citations(raw)
        if not answer.strip():
            raise KnowledgeError("SAG Agent response is missing an answer")
        if not any(citation.snippet.strip() for citation in citations):
            raise KnowledgeError("SAG Agent response is missing usable citations")
        return PlannerAnswer(
            answer=answer,
            citations=citations,
            request=request_text,
            response=json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
        )

    async def test(self) -> None:
        await self.plan([{"role": "user", "content": _CONNECTION_PROBE}])

    async def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            )
        return self._client


def _answer(raw: Mapping[str, Any]) -> str:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    message = choice.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _citations(raw: Mapping[str, Any]) -> tuple[PlannerCitation, ...]:
    sag = raw.get("sag")
    if not isinstance(sag, dict):
        return ()
    values = sag.get("citations")
    if not isinstance(values, list):
        return ()
    citations: list[PlannerCitation] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        citations.append(
            PlannerCitation(
                provider_id=_optional_text(value.get("chunk_id")),
                source_id=_optional_text(value.get("source_id")),
                title=(
                    _optional_text(value.get("heading"))
                    or _optional_text(value.get("source_name"))
                    or f"SAG citation {index + 1}"
                ),
                snippet=_optional_text(value.get("snippet")) or "",
            )
        )
    return tuple(citations)


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _response_error_code(response: httpx.Response) -> str | None:
    try:
        value = response.json()
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    error = value.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else None


async def _request_with_cancellation(
    request: Awaitable[httpx.Response],
    signal: ToolCancellationToken | None,
) -> httpx.Response:
    if signal is None:
        return await request

    request_task: asyncio.Future[httpx.Response] = asyncio.ensure_future(request)
    if signal.is_cancelled():
        request_task.cancel()
        with suppress(asyncio.CancelledError):
            await request_task
        raise asyncio.CancelledError
    watcher = asyncio.create_task(_wait_until_cancelled(signal))
    try:
        wait_set = {
            cast(asyncio.Future[Any], request_task),
            cast(asyncio.Future[Any], watcher),
        }
        done, _pending = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            request_task.cancel()
            with suppress(asyncio.CancelledError):
                await request_task
            raise asyncio.CancelledError
        return await request_task
    except asyncio.CancelledError:
        request_task.cancel()
        with suppress(asyncio.CancelledError):
            await request_task
        raise
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher


async def _wait_until_cancelled(signal: ToolCancellationToken) -> None:
    while not signal.is_cancelled():
        await asyncio.sleep(0.05)


async def _post_bounded(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    token: str,
    body: bytes,
    max_response_bytes: int,
) -> httpx.Response:
    async with client.stream(
        "POST",
        endpoint,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        content=body,
    ) as response:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > max_response_bytes:
                raise KnowledgeError(
                    f"SAG Agent response exceeds {max_response_bytes} UTF-8 bytes"
                )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=bytes(content),
            request=response.request,
        )


__all__ = ["SagAgentSqlPlanner"]
