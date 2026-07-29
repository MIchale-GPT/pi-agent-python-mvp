from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from http.client import HTTPConnection
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import pytest

import tau_coding.web as web_module
from pi_event_helpers import assistant_done, assistant_error, assistant_start, text_delta
from tau_agent.messages import AgentMessage, AssistantMessage, TextContent, UserMessage
from tau_agent.provider import CancellationToken, ModelProvider
from tau_agent.provider_events import AssistantMessageEvent
from tau_agent.session import JsonlSessionStorage, LeafEntry, MessageEntry, entry_to_json_line
from tau_agent.tools import AgentTool
from tau_coding.paths import TauPaths
from tau_coding.provider_config import OpenAICompatibleProviderConfig, ProviderSettings
from tau_coding.resources import TauResourcePaths
from tau_coding.session import CodingSession, CodingSessionConfig
from tau_coding.session_manager import CodingSessionRecord, SessionManager
from tau_coding.web import (
    WebSessionHandle,
    create_web_server,
    main,
    session_detail_payload,
    session_list_payload,
)


def _manager(tmp_path: Path) -> SessionManager:
    return SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))


def _get_json(connection: HTTPConnection, path: str) -> tuple[int, dict[str, Any]]:
    connection.request("GET", path)
    response = connection.getresponse()
    return response.status, json.loads(response.read())


async def _load_test_session(
    selected: CodingSessionRecord,
    selected_manager: SessionManager,
    provider: ModelProvider,
) -> WebSessionHandle:
    session = await CodingSession.load(
        CodingSessionConfig(
            provider=provider,
            model=selected.model,
            system="You are Tau.",
            storage=JsonlSessionStorage(selected.path),
            cwd=selected.cwd,
            resource_paths=TauResourcePaths(
                root=selected_manager.paths.home,
                agents_root=selected_manager.paths.agents_home,
                paths=selected_manager.paths,
            ),
            session_id=selected.id,
            session_manager=selected_manager,
            provider_name="fake",
            extensions_enabled=False,
        )
    )
    return WebSessionHandle(session=session)


def test_session_list_payload_exposes_safe_session_metadata(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="gpt-5.4",
        provider_name="openai",
        title="Build the web workspace",
        session_id="session-1",
    )

    payload = session_list_payload(manager)

    assert payload == {
        "sessions": [
            {
                "id": "session-1",
                "cwd": str(cwd.resolve()),
                "model": "gpt-5.4",
                "providerName": "openai",
                "title": "Build the web workspace",
                "createdAt": record.created_at,
                "updatedAt": record.updated_at,
            }
        ]
    }
    assert "path" not in payload["sessions"][0]


def test_session_detail_payload_returns_only_the_active_branch(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="gpt-5.4",
        provider_name="openai",
        title="Active branch",
        session_id="session-1",
    )
    root = MessageEntry(
        id="root",
        message=UserMessage(content="Build a web page", timestamp=1_000),
    )
    abandoned = MessageEntry(
        id="abandoned",
        parent_id=root.id,
        message=AssistantMessage(
            content=[TextContent(text="Abandoned answer")],
            provider="openai",
            model="gpt-5.4",
            timestamp=2_000,
        ),
    )
    active = MessageEntry(
        id="active",
        parent_id=root.id,
        message=AssistantMessage(
            content=[TextContent(text="Active answer")],
            provider="openai",
            model="gpt-5.4",
            timestamp=3_000,
        ),
    )
    leaf = LeafEntry(parent_id=active.id, entry_id=active.id)
    record.path.write_text(
        "".join(entry_to_json_line(entry) for entry in (root, abandoned, active, leaf)),
        encoding="utf-8",
    )

    payload = session_detail_payload(manager, record.id)

    assert payload is not None
    assert payload["session"]["id"] == "session-1"
    assert [message["text"] for message in payload["messages"]] == [
        "Build a web page",
        "Active answer",
    ]
    assert [message["role"] for message in payload["messages"]] == ["user", "assistant"]
    assert payload["messages"][1]["model"] == "gpt-5.4"


def test_web_server_serves_live_a_theme_and_session_api(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="gpt-5.4",
        provider_name="openai",
        title="Session from API",
        session_id="session-1",
    )
    server = create_web_server(host="127.0.0.1", port=0, session_manager=manager)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)

    try:
        connection.request("GET", "/")
        page = connection.getresponse()
        page_body = page.read().decode()
        assert page.status == 200
        assert page.getheader("Content-Security-Policy") is not None
        assert "Tau Web" in page_body
        assert "Trace Workbench" in page_body
        assert "prototype-switcher" not in page_body
        assert "variant-b" not in page_body
        assert 'id="composer"' in page_body
        assert 'class="mode-pill">LIVE' in page_body
        assert "READ ONLY" not in page_body
        assert 'id="new-session-dialog"' in page_body
        assert 'id="rename-session-dialog"' in page_body
        assert 'id="delete-session-dialog"' in page_body
        assert 'data-confirmation="DELETE"' in page_body
        assert "新建会话将在后续阶段接入" not in page_body

        connection.request("GET", "/app.js")
        script = connection.getresponse()
        script_body = script.read().decode()
        assert script.status == 200
        assert "new EventSource" in script_body
        assert '"X-Tau-Web": "1"' in script_body
        assert "/cancel" in script_body
        assert "/api/session-options" in script_body
        assert "/rename" in script_body
        assert 'commandJson(path, body, "DELETE")' in script_body
        assert "/export?format=" in script_body

        connection.request("GET", "/session-actions.js")
        controller = connection.getresponse()
        controller_body = controller.read().decode()
        assert controller.status == 200
        assert "createAndEnterSession" in controller_body

        status, payload = _get_json(connection, "/api/sessions")
        assert status == 200
        assert payload["sessions"][0]["id"] == "session-1"

        status, payload = _get_json(connection, "/api/health")
        assert status == 200
        assert payload["status"] == "ok"

        status, payload = _get_json(connection, "/api/sessions/missing")
        assert status == 404
        assert payload == {"error": "session_not_found"}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_session_options_and_create_api_open_a_configured_project_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "selected-project"
    cwd.mkdir()
    settings = ProviderSettings(
        default_provider="fake-provider",
        providers=(
            OpenAICompatibleProviderConfig(
                name="fake-provider",
                models=("fake-small", "fake-large"),
                default_model="fake-large",
            ),
        ),
    )
    monkeypatch.setattr(web_module, "load_provider_settings", lambda paths: settings)
    server = create_web_server(host="127.0.0.1", port=0, session_manager=manager)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)

    try:
        status, options = _get_json(connection, "/api/session-options")

        assert status == 200
        assert options["defaultProvider"] == "fake-provider"
        assert options["providers"] == [
            {
                "name": "fake-provider",
                "models": ["fake-small", "fake-large"],
                "defaultModel": "fake-large",
            }
        ]

        status, created = _post_json(
            connection,
            "/api/sessions",
            {
                "cwd": str(cwd),
                "providerName": "fake-provider",
                "model": "fake-small",
            },
        )

        assert status == 201
        session = created["session"]
        assert session["cwd"] == str(cwd.resolve())
        assert session["providerName"] == "fake-provider"
        assert session["model"] == "fake-small"
        assert session["title"] is None
        assert manager.get_session(session["id"]) is not None

        status, detail = _get_json(connection, f"/api/sessions/{session['id']}")
        assert status == 200
        assert detail == {"session": session, "messages": []}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_browser_creation_controller_registers_and_enters_the_created_session() -> None:
    controller_path = Path(web_module.__file__).parent / "data" / "web" / "session-actions.js"
    script = """
require(process.argv[1]);
const calls = [];
const options = {
  cwd: "/workspace/tau",
  providerName: "fake-provider",
  model: "fake-model",
};
const created = {
  id: "session-new",
  cwd: "/workspace/tau",
  providerName: "fake-provider",
  model: "fake-model",
};
globalThis.TauSessionActions.createAndEnterSession(options, {
  createSession: async (received) => {
    calls.push(["create", received]);
    return { session: created };
  },
  registerSession: (session) => calls.push(["register", session.id]),
  enterSession: async (sessionId) => calls.push(["enter", sessionId]),
}).then((session) => {
  console.log(JSON.stringify({ calls, returnedSessionId: session.id }));
});
"""

    completed = subprocess.run(
        ["node", "-e", script, str(controller_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "calls": [
            [
                "create",
                {
                    "cwd": "/workspace/tau",
                    "providerName": "fake-provider",
                    "model": "fake-model",
                },
            ],
            ["register", "session-new"],
            ["enter", "session-new"],
        ],
        "returnedSessionId": "session-new",
    }


def test_create_api_rejects_unknown_directories_and_provider_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    settings = ProviderSettings(
        default_provider="fake-provider",
        providers=(
            OpenAICompatibleProviderConfig(
                name="fake-provider",
                models=("fake-model",),
                default_model="fake-model",
            ),
        ),
    )
    monkeypatch.setattr(web_module, "load_provider_settings", lambda paths: settings)
    server = create_web_server(host="127.0.0.1", port=0, session_manager=manager)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)

    try:
        status, missing_directory = _post_json(
            connection,
            "/api/sessions",
            {
                "cwd": str(tmp_path / "missing"),
                "providerName": "fake-provider",
                "model": "fake-model",
            },
        )
        assert status == 422
        assert missing_directory["error"] == "project_directory_not_found"

        status, unknown_model = _post_json(
            connection,
            "/api/sessions",
            {
                "cwd": str(cwd),
                "providerName": "fake-provider",
                "model": "not-configured",
            },
        )
        assert status == 422
        assert unknown_model["error"] == "provider_selection_invalid"
        assert manager.list_sessions() == []
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_rename_api_updates_the_active_session_title(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake-provider",
        title="Old title",
        session_id="session-1",
    )
    server = create_web_server(host="127.0.0.1", port=0, session_manager=manager)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)

    try:
        status, renamed = _post_json(
            connection,
            "/api/sessions/session-1/rename",
            {"title": "  Web launch plan  "},
        )

        assert status == 200
        assert renamed["session"]["title"] == "Web launch plan"
        assert renamed["session"]["updatedAt"] >= record.updated_at

        status, detail = _get_json(connection, "/api/sessions/session-1")
        assert status == 200
        assert detail["session"] == renamed["session"]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_delete_api_requires_danger_confirmation_and_removes_session_data(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        title="Delete this session",
        session_id="session-1",
    )
    record.path.write_text('{"type":"leaf","entry_id":null}\n', encoding="utf-8")
    server = create_web_server(host="127.0.0.1", port=0, session_manager=manager)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)

    try:
        status, rejected = _delete_json(
            connection,
            "/api/sessions/session-1",
            {"confirmation": "delete"},
        )

        assert status == 422
        assert rejected == {"error": "delete_confirmation_required"}
        assert manager.get_session(record.id) is not None
        assert record.path.exists()

        status, deleted = _delete_json(
            connection,
            "/api/sessions/session-1",
            {"confirmation": "DELETE"},
        )

        assert status == 200
        assert deleted == {"status": "deleted", "sessionId": "session-1"}
        assert manager.get_session(record.id) is None
        assert not record.path.exists()

        status, missing = _get_json(connection, "/api/sessions/session-1")
        assert status == 404
        assert missing == {"error": "session_not_found"}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_export_api_downloads_the_full_session_as_html_and_jsonl(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        title="Exported work",
        session_id="session-1",
    )
    message = MessageEntry(
        id="message-1",
        message=UserMessage(content="Keep every branch", timestamp=1_000),
    )
    leaf = LeafEntry(parent_id=message.id, entry_id=message.id)
    record.path.write_text(
        "".join(entry_to_json_line(entry) for entry in (message, leaf)),
        encoding="utf-8",
    )
    server = create_web_server(host="127.0.0.1", port=0, session_manager=manager)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)

    try:
        connection.request("GET", "/api/sessions/session-1/export?format=html")
        html_response = connection.getresponse()
        html_body = html_response.read().decode()

        assert html_response.status == 200
        assert html_response.getheader("Content-Type") == "text/html; charset=utf-8"
        assert html_response.getheader("Content-Disposition") == (
            'attachment; filename="tau-session.html"'
        )
        assert "Exported work" in html_body
        assert "Keep every branch" in html_body

        connection.request("GET", "/api/sessions/session-1/export?format=jsonl")
        jsonl_response = connection.getresponse()
        jsonl_body = jsonl_response.read().decode()

        assert jsonl_response.status == 200
        assert jsonl_response.getheader("Content-Type") == ("application/x-ndjson; charset=utf-8")
        assert jsonl_response.getheader("Content-Disposition") == (
            'attachment; filename="tau-session.jsonl"'
        )
        assert [json.loads(line)["type"] for line in jsonl_body.splitlines()] == [
            "message",
            "leaf",
        ]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_server_rejects_unknown_paths(tmp_path: Path) -> None:
    server = create_web_server(host="127.0.0.1", port=0, session_manager=_manager(tmp_path))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)

    try:
        status, payload = _get_json(connection, "/not-a-route")
        assert status == 404
        assert payload == {"error": "not_found"}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_server_requires_command_header_for_mutations(tmp_path: Path) -> None:
    server = create_web_server(host="127.0.0.1", port=0, session_manager=_manager(tmp_path))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)
    body = json.dumps({"message": "This request did not come from Tau Web"})

    try:
        connection.request(
            "POST",
            "/api/sessions/session-1/messages",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        response = connection.getresponse()

        assert response.status == 403
        assert json.loads(response.read()) == {"error": "command_header_required"}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_timed_out_message_submission_cancels_slow_session_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Slow session",
        session_id="session-1",
    )
    loader_started = Event()
    loader_cancelled = Event()

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        del selected, selected_manager
        loader_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            loader_cancelled.set()
            raise
        raise AssertionError("unreachable")

    monkeypatch.setattr(web_module, "_RUNTIME_CALL_TIMEOUT_SECONDS", 0.05)
    server = create_web_server(
        host="127.0.0.1",
        port=0,
        session_manager=manager,
        session_loader=load_session,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)

    try:
        status, payload = _post_json(
            connection,
            "/api/sessions/session-1/messages",
            {"message": "Do not run after the timeout"},
        )

        assert status == 503
        assert payload["error"] == "session_unavailable"
        assert loader_started.is_set()
        assert loader_cancelled.wait(timeout=1)
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_web_cli_rejects_remote_access_until_authentication_exists(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--host", "0.0.0.0", "--no-open"])

    assert error.value.code == 2
    assert "only supports loopback hosts" in capsys.readouterr().err


def test_message_api_streams_coding_session_events_and_persists_turn(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Live session",
        session_id="session-1",
    )
    provider = _StreamingFakeProvider()

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(selected, selected_manager, provider)

    server = create_web_server(
        host="127.0.0.1",
        port=0,
        session_manager=manager,
        session_loader=load_session,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    events_connection = HTTPConnection(host, port, timeout=2)
    command_connection = HTTPConnection(host, port, timeout=2)

    try:
        events_connection.request("GET", "/api/sessions/session-1/events")
        events_response = events_connection.getresponse()
        assert events_response.status == 200
        assert events_response.getheader("Content-Type") == "text/event-stream; charset=utf-8"

        request_body = json.dumps({"message": "Connect the A theme"})
        command_connection.request(
            "POST",
            "/api/sessions/session-1/messages",
            body=request_body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(request_body)),
                "X-Tau-Web": "1",
            },
        )
        command_response = command_connection.getresponse()
        command_payload = json.loads(command_response.read())

        assert command_response.status == 202
        assert command_payload["status"] == "accepted"
        assert command_payload["runId"]

        events = _read_sse_events(events_response, until="run_finished")
        event_types = [event["type"] for event in events]
        assert event_types[:2] == ["web_connected", "run_started"]
        assert "message_start" in event_types
        assert "message_update" in event_types
        assert "agent_settled" in event_types
        assert event_types[-1] == "run_finished"
        update = next(event for event in events if event["type"] == "message_update")
        assert update["assistantMessageEvent"]["delta"] == "A theme is live."

        detail = session_detail_payload(manager, record.id)
        assert detail is not None
        assert [(message["role"], message["text"]) for message in detail["messages"]] == [
            ("user", "Connect the A theme"),
            ("assistant", "A theme is live."),
        ]
    finally:
        events_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cancel_api_stops_the_active_coding_session_run(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Cancellable session",
        session_id="session-1",
    )
    provider = _CancellableFakeProvider()

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(selected, selected_manager, provider)

    server = create_web_server(
        host="127.0.0.1",
        port=0,
        session_manager=manager,
        session_loader=load_session,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    events_connection = HTTPConnection(host, port, timeout=2)
    command_connection = HTTPConnection(host, port, timeout=2)

    try:
        events_connection.request("GET", "/api/sessions/session-1/events")
        events_response = events_connection.getresponse()
        assert events_response.status == 200

        _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Wait for cancellation"},
        )
        assert provider.started.wait(timeout=1)

        cancel_response, cancel_payload = _post_json(
            command_connection,
            "/api/sessions/session-1/cancel",
            {},
        )

        assert cancel_response == 202
        assert cancel_payload == {"status": "cancel_requested"}
        events = _read_sse_events(events_response, until="run_finished")
        assert "cancel_requested" in [event["type"] for event in events]
        assert events[-1]["status"] == "cancelled"
    finally:
        events_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_delete_api_refuses_to_remove_a_running_session(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Running session",
        session_id="session-1",
    )
    provider = _CancellableFakeProvider()

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(selected, selected_manager, provider)

    server = create_web_server(
        host="127.0.0.1",
        port=0,
        session_manager=manager,
        session_loader=load_session,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    command_connection = HTTPConnection(host, port, timeout=2)

    try:
        status, _ = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Keep running"},
        )
        assert status == 202
        assert provider.started.wait(timeout=1)

        status, rejected = _delete_json(
            command_connection,
            "/api/sessions/session-1",
            {"confirmation": "DELETE"},
        )

        assert status == 409
        assert rejected["error"] == "session_busy"
        assert manager.get_session(record.id) is not None
        assert record.path.exists()

        status, _ = _post_json(
            command_connection,
            "/api/sessions/session-1/cancel",
            {},
        )
        assert status == 202
    finally:
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_provider_error_is_streamed_and_finishes_the_run_as_failed(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Failing session",
        session_id="session-1",
    )
    provider = _ErrorFakeProvider()

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(selected, selected_manager, provider)

    server = create_web_server(
        host="127.0.0.1",
        port=0,
        session_manager=manager,
        session_loader=load_session,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    events_connection = HTTPConnection(host, port, timeout=2)
    command_connection = HTTPConnection(host, port, timeout=2)

    try:
        events_connection.request("GET", "/api/sessions/session-1/events")
        events_response = events_connection.getresponse()
        assert events_response.status == 200

        response_status, _payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Trigger the provider error"},
        )
        events = _read_sse_events(events_response, until="run_finished")

        assert response_status == 202
        error = next(event for event in events if event["type"] == "run_error")
        assert error["message"] == "provider unavailable"
        assert events[-1]["status"] == "failed"

        detail = session_detail_payload(manager, record.id)
        assert detail is not None
        assistant = detail["messages"][-1]
        assert assistant["role"] == "assistant"
        assert assistant["stopReason"] == "error"
        assert assistant["errorMessage"] == "provider unavailable"
    finally:
        events_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_sse_disconnect_keeps_run_alive_and_server_close_releases_provider(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Disconnect session",
        session_id="session-1",
    )
    provider = _DisconnectFakeProvider()
    unsubscribed = Event()

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        handle = await _load_test_session(selected, selected_manager, provider)
        return WebSessionHandle(session=handle.session, provider=provider)

    server = create_web_server(
        host="127.0.0.1",
        port=0,
        session_manager=manager,
        session_loader=load_session,
    )
    unsubscribe = server.web_runtime.unsubscribe

    def track_unsubscribe(session_id: str, subscriber_id: int) -> None:
        unsubscribe(session_id, subscriber_id)
        unsubscribed.set()

    server.web_runtime.unsubscribe = track_unsubscribe  # type: ignore[method-assign]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    events_connection = HTTPConnection(host, port, timeout=2)
    reconnect_connection = HTTPConnection(host, port, timeout=2)
    command_connection = HTTPConnection(host, port, timeout=2)

    try:
        events_connection.request("GET", "/api/sessions/session-1/events")
        events_response = events_connection.getresponse()
        assert events_response.status == 200
        assert _read_sse_events(events_response, until="web_connected")[0]["running"] is False

        response_status, _payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Finish even if the browser disconnects"},
        )
        assert response_status == 202
        assert provider.started.wait(timeout=1)

        events_response.close()
        events_connection.close()

        reconnect_connection.request("GET", "/api/sessions/session-1/events")
        reconnect_response = reconnect_connection.getresponse()
        assert reconnect_response.status == 200
        connected = _read_sse_events(reconnect_response, until="web_connected")
        assert connected[0]["running"] is True

        provider.release.set()
        assert provider.finished.wait(timeout=1)
        reconnected_events = _read_sse_events(reconnect_response, until="run_finished")
        assert reconnected_events[-1]["status"] == "completed"

        deadline = monotonic() + 1
        detail = session_detail_payload(manager, record.id)
        while detail is not None and len(detail["messages"]) < 2 and monotonic() < deadline:
            sleep(0.01)
            detail = session_detail_payload(manager, record.id)

        assert provider.cancelled.is_set() is False
        assert detail is not None
        assert detail["messages"][-1]["text"] == "Run survived disconnect."
        assert unsubscribed.wait(timeout=1)
    finally:
        events_connection.close()
        reconnect_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert provider.closed.is_set()


def _post_json(
    connection: HTTPConnection,
    path: str,
    payload: dict[str, object],
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload)
    connection.request(
        "POST",
        path,
        body=body,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-Tau-Web": "1",
        },
    )
    response = connection.getresponse()
    return response.status, json.loads(response.read())


def _delete_json(
    connection: HTTPConnection,
    path: str,
    payload: dict[str, object],
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload)
    connection.request(
        "DELETE",
        path,
        body=body,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            "X-Tau-Web": "1",
        },
    )
    response = connection.getresponse()
    return response.status, json.loads(response.read())


def _read_sse_events(response: Any, *, until: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        line = response.readline().decode("utf-8").strip()
        if not line.startswith("data: "):
            continue
        event = json.loads(line.removeprefix("data: "))
        events.append(event)
        if event["type"] == until:
            return events


class _StreamingFakeProvider:
    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del model, system, messages, tools, signal

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            yield assistant_start(model="fake")
            yield text_delta("A theme is live.")
            yield assistant_done(message=AssistantMessage(content="A theme is live."))

        return iterator()


class _CancellableFakeProvider:
    def __init__(self) -> None:
        self.started = Event()

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del model, system, messages, tools

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            yield assistant_start(model="fake")
            self.started.set()
            while signal is None or not signal.is_cancelled():
                await asyncio.sleep(0)

        return iterator()


class _ErrorFakeProvider:
    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del model, system, messages, tools, signal

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            yield assistant_error(message="provider unavailable")

        return iterator()


class _DisconnectFakeProvider:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.finished = Event()
        self.cancelled = Event()
        self.closed = Event()

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        del model, system, messages, tools

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            yield assistant_start(model="fake")
            self.started.set()
            while not self.release.is_set():
                if signal is not None and signal.is_cancelled():
                    self.cancelled.set()
                    return
                await asyncio.sleep(0)
            self.finished.set()
            yield assistant_done(message=AssistantMessage(content="Run survived disconnect."))

        return iterator()

    async def aclose(self) -> None:
        self.closed.set()
