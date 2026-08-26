"""Capture and sanitize the target SAG Agent HTTP contract.

This module exists solely for the readiness gate in
``docs/PRD-sag-agent-sql-planner.md``. It makes real requests to the configured
deployment, but persists only structural fields and stable redaction markers.
It is not the runtime planner adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

AGENT_PATH_TEMPLATE = "/api/v1/openai/{agent_id}/chat/completions"
DEFAULT_CAPTURE_QUESTION = "请返回一个带引用的简短回答，用于接口契约验证。"
type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
_AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_STRUCTURAL_STRING_KEYS = frozenset(
    {
        "code",
        "$ref",
        "finish_reason",
        "format",
        "in",
        "kind",
        "layer",
        "object",
        "role",
        "scheme",
        "stage",
        "type",
    }
)


@dataclass(frozen=True, slots=True)
class SagAgentCaptureConfig:
    """Trusted inputs for one target-deployment contract capture."""

    origin: str
    agent_id: str
    token: str
    question: str = DEFAULT_CAPTURE_QUESTION
    timeout_seconds: float = 60.0
    cancel_after_seconds: float = 0.05

    def __post_init__(self) -> None:
        normalized = self.origin.rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("origin must be an HTTP(S) origin without path or credentials")
        if _AGENT_ID_PATTERN.fullmatch(self.agent_id) is None:
            raise ValueError("agent_id contains unsupported characters")
        if not self.token:
            raise ValueError("token must be configured")
        if not self.question.strip():
            raise ValueError("question must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.cancel_after_seconds < 0:
            raise ValueError("cancel_after_seconds must not be negative")
        object.__setattr__(self, "origin", normalized)


CaptureArtifacts = dict[str, JsonValue]


async def capture_sag_agent_contract(
    config: SagAgentCaptureConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CaptureArtifacts:
    """Probe OpenAPI, success, auth error and cancellation, returning safe artifacts."""
    endpoint = f"{config.origin}{AGENT_PATH_TEMPLATE.format(agent_id=config.agent_id)}"
    headers = {"Authorization": f"Bearer {config.token}"}
    body: dict[str, JsonValue] = {
        "messages": [{"role": "user", "content": config.question.strip()}],
        "stream": False,
    }
    timeout = httpx.Timeout(config.timeout_seconds)
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        openapi_response = await _with_deadline(
            client.get(f"{config.origin}/openapi.json"),
            label="OpenAPI request",
            timeout_seconds=config.timeout_seconds,
        )
        openapi_response.raise_for_status()
        openapi = _json_object(openapi_response, label="OpenAPI")
        operation = _openapi_operation(openapi)

        success_response = await _with_deadline(
            client.post(endpoint, headers=headers, json=body),
            label="success request",
            timeout_seconds=config.timeout_seconds,
        )
        success_response.raise_for_status()
        success_body = _json_object(success_response, label="success response")

        error_response = await _with_deadline(
            client.post(
                endpoint,
                headers={"Authorization": "Bearer invalid-contract-capture-token"},
                json=body,
            ),
            label="error request",
            timeout_seconds=config.timeout_seconds,
        )
        if error_response.status_code < 400:
            raise RuntimeError("invalid-token request did not produce an HTTP error")

        cancellation_task = asyncio.create_task(client.post(endpoint, headers=headers, json=body))
        await asyncio.sleep(config.cancel_after_seconds)
        cancellation: dict[str, JsonValue]
        if cancellation_task.done():
            cancellation_response = await cancellation_task
            raise RuntimeError(
                "cancellation probe completed before cancellation; use a shorter "
                f"--cancel-after-seconds value (status {cancellation_response.status_code})"
            )
        else:
            cancellation_task.cancel()
            try:
                await cancellation_task
            except asyncio.CancelledError:
                cancellation = {"outcome": "client_task_cancelled"}
            else:  # pragma: no cover - task state cannot become successful after cancellation
                cancellation = {"outcome": "completed_during_cancel"}

    return {
        "openapi-operation.json": {
            "openapi": _safe_structural_string(openapi.get("openapi"), "unknown"),
            "method": "POST",
            "path": AGENT_PATH_TEMPLATE,
            "operation": _sanitize_json(operation),
        },
        "request.json": {
            "method": "POST",
            "path": AGENT_PATH_TEMPLATE,
            "authorization": "Bearer <redacted>",
            "body": _sanitize_json(body),
        },
        "success.json": _response_artifact(success_response, success_body),
        "error.json": _response_artifact(error_response, _response_body(error_response)),
        "cancellation.json": cancellation,
    }


def write_sag_agent_contract_capture(
    directory: Path,
    artifacts: Mapping[str, JsonValue],
    *,
    overwrite: bool = False,
) -> tuple[Path, ...]:
    """Write sanitized artifacts, refusing accidental replacement by default."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = tuple(directory / name for name in sorted(artifacts))
    existing = [path for path in paths if path.exists()]
    if existing and not overwrite:
        names = ", ".join(path.name for path in existing)
        raise FileExistsError(f"capture artifacts already exist: {names}")
    for path in paths:
        path.write_text(
            json.dumps(artifacts[path.name], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    """Run the readiness capture using Tau config/env without exposing the token in argv."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", help="SAG Agent API origin; defaults to Tau config")
    parser.add_argument("--agent-id", help="SAG Agent id; defaults to Tau config")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tests/fixtures/sag_agent"),
        help="Artifact directory (default: tests/fixtures/sag_agent)",
    )
    parser.add_argument("--question", default=DEFAULT_CAPTURE_QUESTION)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--cancel-after-seconds", type=float, default=0.05)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    from tau_coding.dataquery.config import resolve_data_query_config
    from tau_coding.env import load_env_file

    load_env_file()
    resolved = resolve_data_query_config()
    origin = args.origin or resolved.sag_agent_origin
    agent_id = args.agent_id or resolved.sag_agent_id
    token = os.environ.get("TAU_SAG_TOKEN") or resolved.secrets.sag_token or ""
    try:
        config = SagAgentCaptureConfig(
            origin=origin,
            agent_id=agent_id,
            token=token,
            question=args.question,
            timeout_seconds=args.timeout_seconds,
            cancel_after_seconds=args.cancel_after_seconds,
        )
        artifacts = asyncio.run(capture_sag_agent_contract(config))
        paths = write_sag_agent_contract_capture(
            args.output,
            artifacts,
            overwrite=args.overwrite,
        )
    except (FileExistsError, RuntimeError, ValueError, httpx.HTTPError) as exc:
        parser.error(str(exc))
    for path in paths:
        print(path)
    return 0


def _openapi_operation(openapi: Mapping[str, Any]) -> Mapping[str, Any]:
    paths = openapi.get("paths")
    operation: object = None
    if isinstance(paths, dict):
        path_item = paths.get(AGENT_PATH_TEMPLATE)
        if isinstance(path_item, dict):
            operation = path_item.get("post")
    if not isinstance(operation, dict):
        raise RuntimeError(
            f"OpenAPI does not expose POST {AGENT_PATH_TEMPLATE}"
        )
    return cast(Mapping[str, Any], operation)


async def _with_deadline[ResultT](
    awaitable: Awaitable[ResultT],
    *,
    label: str,
    timeout_seconds: float,
) -> ResultT:
    try:
        async with asyncio.timeout(timeout_seconds):
            return await awaitable
    except TimeoutError as exc:
        raise RuntimeError(f"{label} timed out") from exc


def _json_object(response: httpx.Response, *, label: str) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return cast(dict[str, Any], value)


def _response_body(response: httpx.Response) -> JsonValue:
    try:
        value = response.json()
    except ValueError:
        return "<redacted:text>"
    return _sanitize_json(cast(Any, value))


def _response_artifact(response: httpx.Response, body: object) -> dict[str, JsonValue]:
    return {
        "status": response.status_code,
        "contentType": response.headers.get("content-type", "").partition(";")[0],
        "body": _sanitize_json(body),
    }


def _sanitize_json(value: object, *, key: str = "value") -> JsonValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if key in _STRUCTURAL_STRING_KEYS:
            return value
        return f"<redacted:{key}>"
    if isinstance(value, (int, float)):
        return 0
    if isinstance(value, list):
        return [_sanitize_json(item, key=key) for item in value]
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_json(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    return f"<redacted:{key}>"


def _safe_structural_string(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and len(value) <= 32 else fallback


if __name__ == "__main__":  # pragma: no cover - exercised via the public functions
    raise SystemExit(main())


__all__ = [
    "SagAgentCaptureConfig",
    "capture_sag_agent_contract",
    "main",
    "write_sag_agent_contract_capture",
]
