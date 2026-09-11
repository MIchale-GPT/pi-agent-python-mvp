"""SAG Agent planner contract tests against the captured deployment fixture."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tau_coding.dataquery.backends.sag_agent import SagAgentSqlPlanner
from tau_coding.dataquery.service import KnowledgeError

pytestmark = pytest.mark.anyio

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sag_agent"


class _CancellationSignal:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def is_cancelled(self) -> bool:
        return self.cancelled


def _fixture(name: str) -> dict[str, object]:
    value = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


async def test_plan_uses_captured_chat_contract_and_maps_citations() -> None:
    captured = _fixture("success.json")
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            int(captured["status"]),
            headers={"content-type": str(captured["contentType"])},
            json=captured["body"],
        )

    planner = SagAgentSqlPlanner(
        origin="http://sag.test",
        agent_id="agent_123",
        token="secret-token",
        transport=httpx.MockTransport(handler),
    )

    result = await planner.plan(
        [
            {"role": "user", "content": "完整业务问题"},
            {"role": "assistant", "content": "上一轮 SQL"},
            {"role": "user", "content": "SQL 与脱敏 DWS 错误"},
        ]
    )

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v1/openai/agent_123/chat/completions"
    assert request.headers["authorization"] == "Bearer secret-token"
    assert json.loads(request.content) == {
        "messages": [
            {"role": "user", "content": "完整业务问题"},
            {"role": "assistant", "content": "上一轮 SQL"},
            {"role": "user", "content": "SQL 与脱敏 DWS 错误"},
        ],
        "stream": False,
        "sag_sql_planning": True,
    }
    assert result.answer == "<redacted:content>"
    assert result.request == request.content.decode("utf-8")
    assert result.response == json.dumps(
        captured["body"], ensure_ascii=False, separators=(",", ":")
    )
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation.provider_id == "<redacted:chunk_id>"
    assert citation.source_id == "<redacted:source_id>"
    assert citation.title == "<redacted:heading>"
    assert citation.snippet == "<redacted:snippet>"

    await planner.close()


async def test_plan_cancels_in_flight_http_request() -> None:
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    signal = _CancellationSignal()
    planner = SagAgentSqlPlanner(
        origin="http://sag.test",
        agent_id="agent_123",
        token="secret-token",
        transport=httpx.MockTransport(handler),
    )
    task = asyncio.create_task(
        planner.plan([{"role": "user", "content": "业务问题"}], signal=signal)
    )
    await started.wait()
    signal.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.3)

    await planner.close()


async def test_plan_reports_captured_error_code_without_provider_message() -> None:
    captured = _fixture("error.json")

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            int(captured["status"]),
            headers={"content-type": str(captured["contentType"])},
            json=captured["body"],
        )

    planner = SagAgentSqlPlanner(
        origin="http://sag.test",
        agent_id="agent_123",
        token="expired-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(
        KnowledgeError,
        match=r"^SAG Agent request failed: HTTP 401 \(unauthorized\)$",
    ):
        await planner.plan([{"role": "user", "content": "业务问题"}])

    await planner.close()


async def test_plan_rejects_response_body_over_configured_byte_limit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "x" * 2048}}],
                "sag": {"citations": []},
            },
        )

    planner = SagAgentSqlPlanner(
        origin="http://sag.test",
        agent_id="agent_123",
        token="secret-token",
        max_response_bytes=1024,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(KnowledgeError, match="response exceeds 1024 UTF-8 bytes"):
        await planner.plan([{"role": "user", "content": "业务问题"}])

    await planner.close()


async def test_connection_probe_rejects_missing_answer_and_citations() -> None:
    seen_content = ""

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_content
        payload = json.loads(request.content)
        seen_content = payload["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [], "sag": {"citations": []}})

    planner = SagAgentSqlPlanner(
        origin="http://sag.test",
        agent_id="agent_123",
        token="secret-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(KnowledgeError, match="missing an answer"):
        await planner.test()
    assert "带引用" in seen_content
    assert seen_content != "__probe__"

    await planner.close()


async def test_plan_rejects_missing_citations() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "SELECT amount FROM t"}}
                ],
                "sag": {"citations": []},
            },
        )

    planner = SagAgentSqlPlanner(
        origin="http://sag.test",
        agent_id="agent_123",
        token="secret-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(KnowledgeError, match="missing usable citations"):
        await planner.plan([{"role": "user", "content": "业务问题"}])

    await planner.close()


async def test_plan_sanitizes_timeout_and_invalid_json_failures() -> None:
    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream details", request=request)

    timeout_planner = SagAgentSqlPlanner(
        origin="http://sag.test",
        agent_id="agent_123",
        token="secret-token",
        transport=httpx.MockTransport(timeout_handler),
    )
    with pytest.raises(KnowledgeError, match=r"^SAG Agent request failed: ReadTimeout$"):
        await timeout_planner.plan([{"role": "user", "content": "业务问题"}])
    await timeout_planner.close()

    async def invalid_json_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"not-json")

    invalid_json_planner = SagAgentSqlPlanner(
        origin="http://sag.test",
        agent_id="agent_123",
        token="secret-token",
        transport=httpx.MockTransport(invalid_json_handler),
    )
    with pytest.raises(KnowledgeError, match="returned invalid JSON"):
        await invalid_json_planner.plan([{"role": "user", "content": "业务问题"}])
    await invalid_json_planner.close()
    await invalid_json_planner.close()
