"""Safe target-deployment capture for the SAG Agent adapter readiness gate."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tau_coding.dataquery.capture_contract import (
    SagAgentCaptureConfig,
    capture_sag_agent_contract,
    write_sag_agent_contract_capture,
)

pytestmark = pytest.mark.anyio


async def test_capture_records_contract_shape_without_secrets_or_business_text(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    cancellation_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "openapi": "3.1.0",
                    "paths": {
                        "/api/v1/openai/{agent_id}/chat/completions": {
                            "post": {
                                "security": [{"HTTPBearer": []}],
                                "requestBody": {
                                    "content": {
                                        "application/json": {
                                            "schema": {"$ref": "#/components/schemas/ChatRequest"}
                                        }
                                    }
                                },
                                "responses": {
                                    "200": {
                                        "content": {
                                            "application/json": {
                                                "schema": {"type": "object"}
                                            }
                                        }
                                    }
                                },
                            }
                        }
                    },
                },
            )
        if request.headers["authorization"] == "Bearer invalid-contract-capture-token":
            return httpx.Response(
                401,
                headers={"content-type": "application/json", "x-request-id": "private-id"},
                json={
                    "error": {
                        "code": "unauthorized",
                        "message": "token for private-user@example.com is invalid",
                        "layer": "api",
                        "stage": "auth",
                        "retryable": False,
                        "request_id": "private-id",
                    }
                },
            )
        if len(requests) == 4:
            cancellation_started.set()
            await asyncio.Event().wait()
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "x-request-id": "private-id"},
            json={
                "id": "chatcmpl-secret-agent-123",
                "object": "chat.completion",
                "created": 1780000000,
                "model": "private-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "SELECT private_business_value FROM private_table",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 31,
                    "completion_tokens": 12,
                    "total_tokens": 43,
                },
                "sag": {
                    "citations": [
                        {
                            "kind": "internal",
                            "chunk_id": "private-chunk",
                            "heading": "Private document",
                            "snippet": "Private business definition",
                            "score": 0.98,
                            "source_id": "private-source",
                            "source_name": "Private source name",
                        }
                    ],
                    "sources": 1,
                },
            },
        )

    config = SagAgentCaptureConfig(
        origin="http://sag.test",
        agent_id="private-agent-id",
        token="super-secret-token",
        question="private capture question",
        cancel_after_seconds=0.001,
    )
    artifacts = await capture_sag_agent_contract(
        config,
        transport=httpx.MockTransport(handler),
    )
    await asyncio.wait_for(cancellation_started.wait(), timeout=1)

    serialized = json.dumps(artifacts, ensure_ascii=False)
    for secret in (
        "super-secret-token",
        "private-agent-id",
        "private capture question",
        "private_business_value",
        "private_table",
        "private-chunk",
        "private-source",
        "Private document",
        "private-user@example.com",
        "private-id",
    ):
        assert secret not in serialized

    assert artifacts["request.json"] == {
        "method": "POST",
        "path": "/api/v1/openai/{agent_id}/chat/completions",
        "authorization": "Bearer <redacted>",
        "body": {
            "messages": [{"role": "user", "content": "<redacted:content>"}],
            "stream": False,
        },
    }
    success = artifacts["success.json"]
    assert success["status"] == 200
    assert success["body"]["object"] == "chat.completion"
    assert success["body"]["choices"][0]["message"] == {
        "role": "assistant",
        "content": "<redacted:content>",
    }
    assert success["body"]["sag"]["citations"][0] == {
        "kind": "internal",
        "chunk_id": "<redacted:chunk_id>",
        "heading": "<redacted:heading>",
        "snippet": "<redacted:snippet>",
        "score": 0,
        "source_id": "<redacted:source_id>",
        "source_name": "<redacted:source_name>",
    }
    assert artifacts["error.json"] == {
        "status": 401,
        "contentType": "application/json",
        "body": {
            "error": {
                "code": "unauthorized",
                "message": "<redacted:message>",
                "layer": "api",
                "stage": "auth",
                "retryable": False,
                "request_id": "<redacted:request_id>",
            }
        },
    }
    assert artifacts["cancellation.json"]["outcome"] == "client_task_cancelled"

    written = write_sag_agent_contract_capture(tmp_path, artifacts)
    assert {path.name for path in written} == set(artifacts)
    assert json.loads((tmp_path / "success.json").read_text(encoding="utf-8")) == success


async def test_capture_rejects_openapi_without_the_agent_operation() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"openapi": "3.1.0", "paths": {}})

    with pytest.raises(RuntimeError, match="OpenAPI does not expose"):
        await capture_sag_agent_contract(
            SagAgentCaptureConfig(
                origin="http://sag.test",
                agent_id="agent-01",
                token="token",
            ),
            transport=httpx.MockTransport(handler),
        )


async def test_capture_applies_its_own_deadline_to_a_hanging_success_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "openapi": "3.1.0",
                    "paths": {
                        "/api/v1/openai/{agent_id}/chat/completions": {
                            "post": {"responses": {"200": {}}}
                        }
                    },
                },
            )
        await asyncio.Event().wait()

    with pytest.raises(RuntimeError, match="success request timed out"):
        await capture_sag_agent_contract(
            SagAgentCaptureConfig(
                origin="http://sag.test",
                agent_id="agent-01",
                token="token",
                timeout_seconds=0.001,
            ),
            transport=httpx.MockTransport(handler),
        )
