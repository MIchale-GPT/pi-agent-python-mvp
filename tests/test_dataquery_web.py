"""Data-query Web configuration API tests (PRD testing decision 5).

Exercises the runtime-level API (the HTTP routes are thin wrappers) with a
temporary Tau home: defaults, save/read round-trip, password write-only rules,
env-override read-only marking, credential-store permissions, sanitized test
connection and password-leak regression across the serialized payloads.
"""

from __future__ import annotations

import asyncio
import queue
from pathlib import Path

import pytest

from tau_coding.credentials import FileCredentialStore, credentials_path
from tau_coding.dataquery.config import (
    CREDENTIAL_DWS_PASSWORD,
    CREDENTIAL_SAG_TOKEN,
    ENV_DWS_HOST,
)
from tau_coding.paths import TauPaths
from tau_coding.session import SessionManager
from tau_coding.web import TauWebRuntime, WebSessionValidationError


@pytest.fixture
def runtime(tmp_path: Path) -> tuple[TauWebRuntime, SessionManager]:
    manager = SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))
    web_runtime = TauWebRuntime(manager, lambda *args: None)  # type: ignore[arg-type]
    yield web_runtime, manager
    web_runtime.close()


def _store(paths: TauPaths) -> FileCredentialStore:
    return FileCredentialStore(credentials_path(paths))


def test_config_read_returns_defaults_without_secrets(runtime) -> None:
    web_runtime, manager = runtime
    payload = web_runtime.dataquery_config()

    assert payload["fields"]["sslmode"]["value"] == "prefer"
    assert payload["fields"]["max_rows"]["value"] == 1000
    assert payload["fields"]["host"]["source"] == "default"
    assert payload["passwordConfigured"] is False
    assert payload["sagTokenConfigured"] is False
    assert payload["sagSourceId"] == "19d09d3733c34716bcdf906738d10b03"
    assert payload["planningMode"] == "unconfigured"
    assert payload["configurationDiagnostics"][0]["code"] == "planning_mode_unconfigured"
    assert "password" not in str(payload) or payload.get("password") is None


def test_config_update_saves_fields_and_secrets(runtime) -> None:
    web_runtime, manager = runtime
    payload = web_runtime.dataquery_update(
        {
            "host": "dws.example.com",
            "port": 8000,
            "database": "exchange",
            "username": "reader",
            "sslmode": "require",
            "password": "top-secret-pw",
            "sagToken": "sag-secret-token",
        }
    )
    assert payload["fields"]["host"]["value"] == "dws.example.com"
    assert payload["fields"]["host"]["source"] == "config"
    assert payload["fields"]["port"]["value"] == 8000
    assert payload["passwordConfigured"] is True
    assert payload["sagTokenConfigured"] is True

    store = _store(manager.paths)
    assert store.get(CREDENTIAL_DWS_PASSWORD) == "top-secret-pw"
    assert store.get(CREDENTIAL_SAG_TOKEN) == "sag-secret-token"

    serialized = str(payload)
    assert "top-secret-pw" not in serialized
    assert "sag-secret-token" not in serialized


def test_config_payload_never_contains_secret_values(runtime) -> None:
    web_runtime, manager = runtime
    store = _store(manager.paths)
    store.set(CREDENTIAL_DWS_PASSWORD, "leaky-pw")
    store.set(CREDENTIAL_SAG_TOKEN, "leaky-token")

    payload = web_runtime.dataquery_config()
    for value in (str(payload), payload):
        assert "leaky-pw" not in str(value)
        assert "leaky-token" not in str(value)
    assert payload["passwordConfigured"] is True


def test_empty_password_preserves_existing(runtime) -> None:
    web_runtime, manager = runtime
    store = _store(manager.paths)
    store.set(CREDENTIAL_DWS_PASSWORD, "original-pw")

    payload = web_runtime.dataquery_update({"password": ""})
    assert payload["passwordConfigured"] is True
    assert store.get(CREDENTIAL_DWS_PASSWORD) == "original-pw"


def test_env_overridden_fields_marked_read_only_and_ignored_on_write(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_runtime, manager = runtime
    monkeypatch.setenv(ENV_DWS_HOST, "env.example.com")

    read_payload = web_runtime.dataquery_config()
    assert read_payload["fields"]["host"]["value"] == "env.example.com"
    assert read_payload["fields"]["host"]["source"] == "env"

    written = web_runtime.dataquery_update({"host": "should-be-ignored", "database": "newdb"})
    assert written["fields"]["host"]["value"] == "env.example.com"
    assert written["fields"]["database"]["value"] == "newdb"


def test_test_connection_without_configuration_is_sanitized(runtime) -> None:
    web_runtime, _manager = runtime
    result = web_runtime.dataquery_test_connection()

    assert result["complete"] is False
    assert result["planningMode"] == "unconfigured"
    assert result["dws"]["ok"] is False
    assert result["dws"]["error"] == "not configured"
    assert result["planner"] == {
        "mode": "unconfigured",
        "ok": False,
        "elapsedMs": 0,
        "error": "not configured",
    }
    assert result["citationExpansion"] == {
        "configured": False,
        "ok": False,
        "elapsedMs": 0,
        "error": "not configured",
    }
    assert "sag" not in result
    # No secret *values* leak into the payload.
    assert "top-secret" not in str(result)
    assert "Bearer" not in str(result)


def test_agent_connection_probe_uses_runtime_adapter_without_requiring_mcp(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    web_runtime, _manager = runtime
    web_runtime.dataquery_update(
        {
            "username": "reader",
            "password": "dws-secret",
            "planning_mode": "agent",
            "sag_agent_origin": "http://sag.test",
            "sag_agent_id": "agent_123",
            "sag_endpoint": "",
            "sagToken": "sag-secret",
        }
    )

    async def dws_ok(self) -> tuple[int, None]:
        del self
        return 7, None

    async def planner_ok(self) -> None:
        del self

    monkeypatch.setattr(
        "tau_coding.dataquery.backends.dws.DwsPostgresQueryBackend.test_connection",
        dws_ok,
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.backends.sag_agent.SagAgentSqlPlanner.test",
        planner_ok,
    )

    result = web_runtime.dataquery_test_connection()

    assert result["complete"] is True
    assert result["planner"]["mode"] == "agent"
    assert result["planner"]["ok"] is True
    assert result["citationExpansion"] == {
        "configured": False,
        "ok": False,
        "elapsedMs": 0,
        "error": "not configured",
    }


def test_credential_store_permissions(runtime) -> None:
    _web_runtime, manager = runtime
    store = _store(manager.paths)
    store.set(CREDENTIAL_DWS_PASSWORD, "pw")
    path = credentials_path(manager.paths)
    assert (path.stat().st_mode & 0o077) == 0


def test_invalid_update_is_rejected(runtime) -> None:
    web_runtime, _manager = runtime
    with pytest.raises(WebSessionValidationError):
        web_runtime.dataquery_update({"port": "not-a-number", "password": "x"})


# -- Web authorization classification (decision 16) --------------------------


class _FakeAuthorizationRuntime:
    def authorization_view(self, tool_name: str, arguments: dict[str, object]) -> None:
        del tool_name, arguments
        return None


class _FakeAuthorizationSession:
    def __init__(self) -> None:
        self.extension_runtime = _FakeAuthorizationRuntime()


class _FakeAuthorizationHandle:
    def __init__(self) -> None:
        self.session = _FakeAuthorizationSession()


def _auth_session(manager) -> None:
    cwd = manager.paths.home.parent / "project"
    cwd.mkdir(exist_ok=True)
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="auth",
        session_id="auth-session",
    )


def test_web_auto_approves_knowledge_and_prepare_tools(runtime) -> None:
    """search/read/prepare never prompt a browser dialog (decision 16)."""
    from tau_agent.messages import ToolCall
    from tau_coding.web import _WebSessionSlot

    web_runtime, manager = runtime
    _auth_session(manager)

    for name in ("data_knowledge_search", "data_knowledge_read", "data_query_prepare"):
        slot = _WebSessionSlot()
        slot.subscribers[1] = object()
        slot.handle = _FakeAuthorizationHandle()
        future = asyncio.run_coroutine_threadsafe(
            web_runtime._authorize_tool_call(  # noqa: SLF001
                "auth-session", slot, ToolCall(id="1", name=name, arguments={})
            ),
            web_runtime._loop,  # noqa: SLF001
        )
        assert future.result(timeout=2) == (False, None)


def test_web_execute_requires_browser_confirmation(runtime) -> None:
    """data_query_execute goes through the host dialog and honors a deny."""
    from tau_agent.messages import ToolCall
    from tau_coding.web import _WebSessionSlot

    web_runtime, manager = runtime
    _auth_session(manager)
    slot = _WebSessionSlot()
    slot.subscribers[1] = queue.Queue()
    slot.handle = _FakeAuthorizationHandle()
    call = ToolCall(id="1", name="data_query_execute", arguments={})

    future = asyncio.run_coroutine_threadsafe(
        web_runtime._authorize_tool_call("auth-session", slot, call),  # noqa: SLF001
        web_runtime._loop,  # noqa: SLF001
    )
    for _ in range(50):
        if slot.pending_tool_authorizations:
            break
        import time

        time.sleep(0.02)
    assert slot.pending_tool_authorizations, "expected a pending authorization"
    pending = next(iter(slot.pending_tool_authorizations.values()))
    web_runtime._loop.call_soon_threadsafe(  # noqa: SLF001
        lambda: pending.decision.set_result("deny")
    )

    assert future.result(timeout=2) == (True, "Tool execution denied by the user")
