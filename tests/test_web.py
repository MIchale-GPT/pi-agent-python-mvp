from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import AsyncIterator, Mapping
from http.client import HTTPConnection
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import pytest

import tau_coding.web as web_module
from pi_event_helpers import (
    assistant_done,
    assistant_error,
    assistant_start,
    text_delta,
    tool_call_end,
)
from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ToolCall,
    UserMessage,
)
from tau_agent.provider import CancellationToken, ModelProvider
from tau_agent.provider_events import AssistantMessageEvent
from tau_agent.session import (
    JsonlSessionStorage,
    LeafEntry,
    MessageEntry,
    ThinkingLevelChangeEntry,
    entry_to_json_line,
)
from tau_agent.tools import AgentTool, AgentToolResult
from tau_coding import session as coding_session_module
from tau_coding.credentials import FileCredentialStore, OAuthCredential, credentials_path
from tau_coding.paths import TauPaths
from tau_coding.provider_config import (
    AnthropicProviderConfig,
    OpenAICompatibleProviderConfig,
    ProviderModelMetadata,
    ProviderSettings,
)
from tau_coding.resources import TauResourcePaths
from tau_coding.session import CodingSession, CodingSessionConfig
from tau_coding.session_manager import CodingSessionRecord, SessionManager
from tau_coding.web import (
    TauWebRuntime,
    WebSessionHandle,
    _WebSessionSlot,
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


def _get_html(connection: HTTPConnection, path: str) -> tuple[int, str]:
    connection.request("GET", path)
    response = connection.getresponse()
    return response.status, response.read().decode("utf-8")


async def _load_test_session(
    selected: CodingSessionRecord,
    selected_manager: SessionManager,
    provider: ModelProvider,
    *,
    tools: list[AgentTool] | None = None,
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
            tools=tools,
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
                "temperature": None,
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
        assert 'id="new-session-temperature-mode"' in page_body
        assert 'id="new-session-temperature"' in page_body
        assert 'id="new-session-provider-url"' in page_body
        assert 'id="new-session-api-key"' in page_body
        assert 'id="new-session-model"' in page_body
        assert 'id="new-session-thinking"' in page_body
        assert 'step="any"' in page_body
        assert 'id="rename-session-dialog"' in page_body
        assert 'id="delete-session-dialog"' in page_body
        assert 'data-confirmation="DELETE"' in page_body
        assert 'id="session-settings-button"' in page_body
        assert 'id="session-settings-dialog"' in page_body
        assert 'id="session-thinking"' in page_body
        assert 'id="steer-button"' in page_body
        assert 'id="follow-up-button"' in page_body
        assert 'id="queue-panel"' in page_body
        assert 'id="clear-queue-button"' in page_body
        assert 'id="tool-authorization-dialog"' in page_body
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
        assert "/configuration" in script_body
        assert "/queue/clear" in script_body
        assert "/tool-authorizations/" in script_body
        assert "tool_authorization_requested" in script_body
        assert "command_result" in script_body

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


def test_index_html_scripts_are_cache_busted(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    server = create_web_server(host="127.0.0.1", port=0, session_manager=manager)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)
    try:
        status, body = _get_html(connection, "/")
        assert status == 200
        for name in ("trace-timeline.js", "session-actions.js", "app.js"):
            pattern = f'src="/{name}?v='
            assert pattern in body, f"missing cache bust for {name}"
        version = server.asset_versions["app.js"]
        assert len(version) == 8
        assert f'src="/app.js?v={version}"' in body
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
                credential_name="fake-provider",
                models=("fake-small", "fake-large"),
                default_model="fake-large",
                thinking_levels=("off", "low"),
                thinking_default="low",
                thinking_parameter="reasoning_effort",
            ),
            OpenAICompatibleProviderConfig(
                name="unused-provider",
                credential_name="unused-provider",
                models=("unused-model",),
                default_model="unused-model",
            ),
        ),
    )
    FileCredentialStore(credentials_path(manager.paths)).set("fake-provider", "secret")
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
        assert options["provider"] == {
            "name": "fake-provider",
            "baseUrl": "https://api.openai.com/v1",
            "model": "fake-large",
            "apiKeyConfigured": True,
            "thinkingLevels": ["off", "low"],
            "defaultThinkingLevel": "low",
            "temperatureSupported": True,
            "temperatureRange": {"min": 0.0, "max": 2.0, "step": "any"},
        }
        assert options["providers"] == [
            {
                "name": "fake-provider",
                "models": ["fake-small", "fake-large"],
                "defaultModel": "fake-large",
                "thinkingLevels": {
                    "fake-small": ["off", "low"],
                    "fake-large": ["off", "low"],
                },
                "temperatureModels": ["fake-small", "fake-large"],
                "temperatureRange": {"min": 0.0, "max": 2.0, "step": "any"},
            }
        ]

        status, created = _post_json(
            connection,
            "/api/sessions",
            {
                "cwd": str(cwd),
                "providerName": "fake-provider",
                "model": "fake-small",
                "temperature": 0.2,
            },
        )

        assert status == 201
        session = created["session"]
        assert session["cwd"] == str(cwd.resolve())
        assert session["providerName"] == "fake-provider"
        assert session["model"] == "fake-small"
        assert session["temperature"] == 0.2
        assert session["title"] is None
        stored = manager.get_session(session["id"])
        assert stored is not None
        assert stored.temperature == 0.2

        status, detail = _get_json(connection, f"/api/sessions/{session['id']}")
        assert status == 200
        assert detail["session"] == session
        assert detail["messages"] == []
        assert detail["configuration"]["providerName"] == "fake-provider"
        assert detail["configuration"]["model"] == "fake-small"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_session_options_offer_setup_when_no_openai_compatible_provider_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    settings = ProviderSettings(
        default_provider="anthropic",
        providers=(AnthropicProviderConfig(),),
    )
    saved: list[ProviderSettings] = []
    monkeypatch.setattr(
        web_module,
        "load_provider_settings",
        lambda paths: saved[-1] if saved else settings,
    )
    monkeypatch.setattr(
        web_module,
        "save_provider_settings",
        lambda updated, paths: saved.append(updated) or paths.home / "providers.json",
    )
    server = create_web_server(host="127.0.0.1", port=0, session_manager=manager)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)

    try:
        status, options = _get_json(connection, "/api/session-options")

        assert status == 200
        assert options["defaultProvider"] == "tau-web"
        assert options["provider"] == {
            "name": "tau-web",
            "baseUrl": "https://api.openai.com/v1",
            "model": "gpt-5.4",
            "apiKeyConfigured": False,
            "thinkingLevels": [],
            "defaultThinkingLevel": None,
            "temperatureSupported": False,
            "temperatureRange": {"min": 0.0, "max": 2.0, "step": "any"},
        }
        assert [provider["name"] for provider in options["providers"]] == ["tau-web"]

        status, configured = _post_json(
            connection,
            "/api/provider",
            {
                "baseUrl": "http://localhost:8000/v1",
                "apiKey": "local-secret",
                "model": "local-model",
            },
        )

        assert status == 200
        assert configured["provider"]["name"] == "tau-web"
        assert configured["provider"]["baseUrl"] == "http://localhost:8000/v1"
        assert configured["provider"]["model"] == "local-model"
        assert saved[-1].default_provider == "anthropic"
        assert saved[-1].get_provider("anthropic") == settings.get_provider("anthropic")
        assert saved[-1].get_provider("tau-web").api == "openai-completions"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_session_options_do_not_report_oauth_as_an_editable_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    monkeypatch.delenv("COPILOT_TEST_API_KEY", raising=False)
    provider = OpenAICompatibleProviderConfig(
        name="github-copilot",
        api_key_env="COPILOT_TEST_API_KEY",
        credential_name="github-copilot",
        models=("copilot-model",),
        default_model="copilot-model",
    )
    settings = ProviderSettings(
        default_provider=provider.name,
        providers=(provider,),
    )
    FileCredentialStore(credentials_path(manager.paths)).set_oauth(
        "github-copilot",
        OAuthCredential(
            access="oauth-access",
            refresh="oauth-refresh",
            expires=4_000_000_000_000,
        ),
    )
    monkeypatch.setattr(web_module, "load_provider_settings", lambda paths: settings)

    options = web_module.session_options_payload(manager)

    assert options["provider"]["name"] == "github-copilot"
    assert options["provider"]["apiKeyConfigured"] is False


def test_provider_api_updates_the_single_web_connection_without_returning_the_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    current = ProviderSettings(
        default_provider="custom",
        providers=(
            OpenAICompatibleProviderConfig(
                name="custom",
                base_url="http://old.example/v1",
                api_key_env="CUSTOM_API_KEY",
                credential_name="custom",
                models=("old-model",),
                default_model="old-model",
                thinking_levels=("off", "medium", "high"),
                thinking_default="medium",
                thinking_parameter="reasoning_effort",
            ),
        ),
    )
    saved: list[ProviderSettings] = []

    def load_settings(paths: TauPaths) -> ProviderSettings:
        del paths
        return saved[-1] if saved else current

    def save_settings(settings: ProviderSettings, paths: TauPaths) -> Path:
        saved.append(settings)
        return paths.home / "providers.json"

    monkeypatch.setattr(web_module, "load_provider_settings", load_settings)
    monkeypatch.setattr(web_module, "save_provider_settings", save_settings)
    server = create_web_server(host="127.0.0.1", port=0, session_manager=manager)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    connection = HTTPConnection(host, port, timeout=2)

    try:
        status, missing_key = _post_json(
            connection,
            "/api/provider",
            {
                "baseUrl": "http://127.0.0.1:9000/v1/",
                "model": "new-model",
            },
        )
        assert status == 422
        assert missing_key["error"] == "provider_api_key_required"
        assert saved == []

        credential_store = FileCredentialStore(credentials_path(manager.paths))
        credential_store.set("custom", "existing-secret")
        status, copied = _post_json(
            connection,
            "/api/provider",
            {
                "baseUrl": "http://127.0.0.1:9000/v1/",
                "model": "new-model",
            },
        )
        assert status == 200
        assert copied["provider"]["name"] == "tau-web"
        assert copied["provider"]["apiKeyConfigured"] is True
        assert credential_store.get("tau-web") == "existing-secret"

        status, payload = _post_json(
            connection,
            "/api/provider",
            {
                "baseUrl": "http://127.0.0.1:9000/v1/",
                "apiKey": "replacement-secret",
                "model": "new-model",
            },
        )

        assert status == 200
        assert payload == {
            "provider": {
                "name": "tau-web",
                "baseUrl": "http://127.0.0.1:9000/v1",
                "model": "new-model",
                "apiKeyConfigured": True,
                "thinkingLevels": [],
                "defaultThinkingLevel": None,
                "temperatureSupported": True,
                "temperatureRange": {"min": 0.0, "max": 2.0, "step": "any"},
            }
        }
        assert "replacement-secret" not in json.dumps(payload)
        assert credential_store.get("tau-web") == "replacement-secret"
        assert saved[-1].default_provider == "custom"
        assert saved[-1].get_provider("custom") == current.get_provider("custom")
        configured = saved[-1].get_provider("tau-web")
        assert configured.base_url == "http://127.0.0.1:9000/v1"
        assert configured.models == ("new-model",)
        assert configured.default_model == "new-model"

        status, options = _get_json(connection, "/api/session-options")
        assert status == 200
        assert options["defaultProvider"] == "tau-web"
        assert options["provider"]["name"] == "tau-web"
        assert [provider["name"] for provider in options["providers"]] == ["tau-web"]
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_create_api_persists_the_selected_session_thinking_level(
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
                thinking_levels=("off", "medium", "high"),
                thinking_default="medium",
                thinking_parameter="reasoning_effort",
            ),
        ),
    )
    monkeypatch.setattr(web_module, "load_provider_settings", lambda paths: settings)
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
    connection = HTTPConnection(host, port, timeout=2)

    try:
        status, created = _post_json(
            connection,
            "/api/sessions",
            {
                "cwd": str(cwd),
                "providerName": "fake-provider",
                "model": "fake-model",
                "thinkingLevel": "medium",
            },
        )

        assert status == 201
        record = manager.get_session(created["session"]["id"])
        assert record is not None
        entries = asyncio.run(JsonlSessionStorage(record.path).read_all())
        thinking_entries = [
            entry for entry in entries if isinstance(entry, ThinkingLevelChangeEntry)
        ]
        assert thinking_entries[-1].thinking_level == "medium"
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
  temperature: 0.2,
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
                    "temperature": 0.2,
                },
            ],
            ["register", "session-new"],
            ["enter", "session-new"],
        ],
        "returnedSessionId": "session-new",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_browser_creation_controller_saves_the_connection_before_creating() -> None:
    controller_path = Path(web_module.__file__).parent / "data" / "web" / "session-actions.js"
    script = """
require(process.argv[1]);
const calls = [];
const options = {
  cwd: "/workspace/tau",
  connection: {
    baseUrl: "http://127.0.0.1:9000/v1",
    apiKey: "secret",
    model: "reasoner",
  },
  thinkingLevel: "high",
  temperature: null,
};
globalThis.TauSessionActions.configureAndCreateSession(options, {
  updateProvider: async (connection) => {
    calls.push(["provider", connection]);
    return {
      provider: {
        name: "custom",
        model: "reasoner",
        thinkingLevels: ["off", "medium", "high"],
        defaultThinkingLevel: "medium",
      },
    };
  },
  createSession: async (session) => {
    calls.push(["create", session]);
    return { session: { id: "session-new" } };
  },
  registerSession: (session) => calls.push(["register", session.id]),
  enterSession: async (sessionId) => calls.push(["enter", sessionId]),
}).then(() => console.log(JSON.stringify(calls)));
"""

    completed = subprocess.run(
        ["node", "-e", script, str(controller_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [
        [
            "provider",
            {
                "baseUrl": "http://127.0.0.1:9000/v1",
                "apiKey": "secret",
                "model": "reasoner",
            },
        ],
        [
            "create",
            {
                "cwd": "/workspace/tau",
                "providerName": "custom",
                "model": "reasoner",
                "thinkingLevel": "high",
                "temperature": None,
            },
        ],
        ["register", "session-new"],
        ["enter", "session-new"],
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_browser_creation_controller_rejects_stale_thinking_after_model_update() -> None:
    controller_path = Path(web_module.__file__).parent / "data" / "web" / "session-actions.js"
    script = """
require(process.argv[1]);
let createCalled = false;
globalThis.TauSessionActions.configureAndCreateSession({
  cwd: "/workspace/tau",
  connection: { baseUrl: "http://localhost/v1", apiKey: "", model: "plain-model" },
  thinkingLevel: "high",
  temperature: null,
}, {
  updateProvider: async () => ({
    provider: {
      name: "tau-web",
      model: "plain-model",
      thinkingLevels: [],
      defaultThinkingLevel: null,
    },
  }),
  createSession: async () => {
    createCalled = true;
    return { session: { id: "unexpected" } };
  },
  registerSession: () => {},
  enterSession: async () => {},
}).catch((error) => {
  console.log(JSON.stringify({ message: error.message, createCalled }));
});
"""

    completed = subprocess.run(
        ["node", "-e", script, str(controller_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "message": "Thinking level high is not available for plain-model",
        "createCalled": False,
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_browser_temperature_controller_resolves_auto_precise_and_custom_modes() -> None:
    controller_path = Path(web_module.__file__).parent / "data" / "web" / "session-actions.js"
    script = """
require(process.argv[1]);
const resolve = globalThis.TauSessionActions.resolveTemperature;
const capability = { supported: true, min: 0, max: 2 };
const result = {
  automatic: resolve("auto", "", capability),
  precise: resolve("precise", "", capability),
  custom: resolve("custom", "0.35", capability),
  unsupported: resolve("precise", "", { ...capability, supported: false }),
};
let invalid;
try {
  resolve("custom", "2.1", capability);
} catch (error) {
  invalid = error.message;
}
console.log(JSON.stringify({ result, invalid }));
"""

    completed = subprocess.run(
        ["node", "-e", script, str(controller_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "result": {
            "automatic": None,
            "precise": 0,
            "custom": 0.35,
            "unsupported": None,
        },
        "invalid": "Temperature must be between 0 and 2",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_browser_ignores_an_accepted_run_response_after_sse_already_finished() -> None:
    controller_path = Path(web_module.__file__).parent / "data" / "web" / "session-actions.js"
    script = """
require(process.argv[1]);
const shouldActivate = globalThis.TauSessionActions.shouldActivateAcceptedRun;
const settled = new Set(["fast-run"]);
console.log(JSON.stringify({
  fastRun: shouldActivate("fast-run", settled),
  normalRun: shouldActivate("normal-run", settled),
  remaining: [...settled],
}));
"""

    completed = subprocess.run(
        ["node", "-e", script, str(controller_path)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "fastRun": False,
        "normalRun": True,
        "remaining": [],
    }


@pytest.mark.parametrize(
    ("model_api", "expected_temperature"),
    [(None, 0.2), ("openai-responses", None)],
)
@pytest.mark.anyio
async def test_web_load_reconciles_stored_temperature_with_model_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model_api: str | None,
    expected_temperature: float | None,
) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="changed-model",
        provider_name="changed",
        temperature=0.2,
    )
    provider_config = OpenAICompatibleProviderConfig(
        name="changed",
        models=("changed-model",),
        default_model="changed-model",
        model_metadata=(
            {"changed-model": ProviderModelMetadata(api=model_api)} if model_api else {}
        ),
    )
    created_temperatures: list[float | None] = []

    class FakeProvider:
        async def aclose(self) -> None:
            return None

    def create_provider(provider: object, **kwargs: object) -> FakeProvider:
        del provider
        temperature = kwargs.get("temperature")
        created_temperatures.append(temperature if isinstance(temperature, float) else None)
        return FakeProvider()

    monkeypatch.setattr(
        web_module,
        "load_provider_settings",
        lambda paths: ProviderSettings(providers=(provider_config,)),
    )
    monkeypatch.setattr(web_module, "create_model_provider", create_provider)
    monkeypatch.setattr(coding_session_module, "create_model_provider", create_provider)

    handle = await web_module._load_web_session(record, manager)

    assert handle.session.temperature == expected_temperature
    assert created_temperatures == [expected_temperature, expected_temperature]
    updated = manager.get_session(record.id)
    assert updated is not None
    assert updated.temperature == expected_temperature

    await handle.aclose()


def test_session_configuration_api_lists_available_choices_and_persists_switches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="alpha-small",
        provider_name="alpha",
        title="Configurable session",
        session_id="session-1",
    )
    settings = ProviderSettings(
        default_provider="alpha",
        providers=(
            OpenAICompatibleProviderConfig(
                name="alpha",
                models=("alpha-small", "alpha-large"),
                default_model="alpha-small",
                thinking_levels=("off", "low", "high"),
                thinking_default="low",
            ),
            OpenAICompatibleProviderConfig(
                name="beta",
                models=("beta-reasoner",),
                default_model="beta-reasoner",
                thinking_levels=("low", "high"),
                thinking_default="high",
            ),
        ),
    )

    class SwitchableProvider:
        async def aclose(self) -> None:
            return None

    def create_provider(provider: object, **kwargs: object) -> SwitchableProvider:
        del provider, kwargs
        return SwitchableProvider()

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        session = await CodingSession.load(
            CodingSessionConfig(
                provider=SwitchableProvider(),
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
                provider_name=selected.provider_name or "alpha",
                provider_settings=settings,
                runtime_provider_config=settings.get_provider(selected.provider_name or "alpha"),
                extensions_enabled=False,
            )
        )
        return WebSessionHandle(session=session)

    monkeypatch.setattr(web_module, "load_provider_settings", lambda paths: settings)
    monkeypatch.setattr(coding_session_module, "create_model_provider", create_provider)
    monkeypatch.setattr(
        coding_session_module,
        "save_provider_thinking_level",
        lambda **kwargs: pytest.fail(
            f"Web session switch changed the global thinking default: {kwargs}"
        ),
    )
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
        status, detail = _get_json(connection, "/api/sessions/session-1")

        assert status == 200
        assert detail["configuration"] == {
            "providerName": "alpha",
            "model": "alpha-small",
            "thinkingLevel": "low",
            "availableThinkingLevels": ["off", "low", "high"],
            "thinkingUnavailableReason": None,
            "providers": [
                {
                    "name": "alpha",
                    "models": ["alpha-small", "alpha-large"],
                    "defaultModel": "alpha-small",
                    "thinkingLevels": {
                        "alpha-small": ["off", "low", "high"],
                        "alpha-large": ["off", "low", "high"],
                    },
                },
            ],
        }

        status, updated = _post_json(
            connection,
            "/api/sessions/session-1/configuration",
            {
                "providerName": "alpha",
                "model": "alpha-large",
                "thinkingLevel": "high",
            },
        )

        assert status == 200
        assert updated["session"]["providerName"] == "alpha"
        assert updated["session"]["model"] == "alpha-large"
        assert updated["configuration"]["thinkingLevel"] == "high"
        stored = manager.get_session(record.id)
        assert stored is not None
        assert stored.provider_name == "alpha"
        assert stored.model == "alpha-large"
        entry_types = [
            json.loads(line)["type"]
            for line in record.path.read_text(encoding="utf-8").splitlines()
        ]
        assert "model_change" in entry_types
        assert "thinking_level_change" in entry_types

        status, updated = _post_json(
            connection,
            "/api/sessions/session-1/configuration",
            {
                "providerName": "alpha",
                "model": "alpha-large",
                "thinkingLevel": "low",
            },
        )
        assert status == 200
        assert updated["configuration"]["thinkingLevel"] == "low"

        status, rejected = _post_json(
            connection,
            "/api/sessions/session-1/configuration",
            {
                "providerName": "beta",
                "model": "beta-reasoner",
                "thinkingLevel": "high",
            },
        )
        assert status == 422
        assert rejected["error"] == "provider_selection_invalid"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
                models=("fake-model", "gpt-5.4"),
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

        status, invalid_temperature = _post_json(
            connection,
            "/api/sessions",
            {
                "cwd": str(cwd),
                "providerName": "fake-provider",
                "model": "fake-model",
                "temperature": 2.1,
            },
        )
        assert status == 422
        assert invalid_temperature["error"] == "temperature_invalid"

        status, unsupported_temperature = _post_json(
            connection,
            "/api/sessions",
            {
                "cwd": str(cwd),
                "providerName": "fake-provider",
                "model": "gpt-5.4",
                "temperature": 0,
            },
        )
        assert status == 422
        assert unsupported_temperature["error"] == "temperature_unsupported"
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


def test_message_api_tags_trace_events_with_run_id_and_finish_summary(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Trace session",
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
        assert command_payload["status"] == "accepted"
        run_id = command_payload["runId"]

        events = _read_sse_events(events_response, until="run_finished")

        streamed_types = {"message_start", "message_update", "message_end", "agent_settled"}
        for event in events:
            if event["type"] in streamed_types:
                assert event["runId"] == run_id, event
                assert isinstance(event["timestamp"], int), event

        finished = events[-1]
        assert finished["type"] == "run_finished"
        assert finished["status"] == "completed"
        assert finished["turnCount"] == 1
        assert finished["eventCount"] >= 4
        assert isinstance(finished["durationMs"], int)
        assert finished["durationMs"] >= 0
    finally:
        events_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_sse_subscribe_replays_buffered_trace_events(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Replay session",
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
    runner_connection = HTTPConnection(host, port, timeout=2)
    late_connection = HTTPConnection(host, port, timeout=2)

    try:
        runner_connection.request("GET", "/api/sessions/session-1/events")
        runner_response = runner_connection.getresponse()
        assert runner_response.status == 200

        status, payload = _post_json(
            runner_connection,
            "/api/sessions/session-1/messages",
            {"message": "Connect the A theme"},
        )
        assert status == 202
        _read_sse_events(runner_response, until="run_finished")

        late_connection.request("GET", "/api/sessions/session-1/events")
        late_response = late_connection.getresponse()
        assert late_response.status == 200

        events = _read_sse_events(late_response, until="run_finished")
        assert events[0]["type"] == "web_connected"
        replayed = events[1:]
        assert replayed[0]["type"] == "run_started"
        assert replayed[-1]["type"] == "run_finished"
        assert all(event.get("replay") is True for event in replayed)
        assert "replay" not in events[0]
        assert [event["type"] for event in replayed].count("message_update") >= 1
    finally:
        runner_connection.close()
        late_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_trace_buffer_drops_oldest_events_beyond_limit(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Buffer session",
        session_id="session-1",
    )
    runtime = TauWebRuntime(manager, lambda *args: None)  # type: ignore[arg-type]

    async def publish_many() -> None:
        slot = runtime._slots.setdefault(record.id, _WebSessionSlot())
        for index in range(web_module.TRACE_BUFFER_LIMIT + 100):
            runtime._publish(
                slot,
                {"type": "message_end", "sessionId": record.id, "index": index},
            )

    asyncio.run(publish_many())
    slot = runtime._slots[record.id]
    assert len(slot.trace_buffer) == web_module.TRACE_BUFFER_LIMIT
    assert slot.trace_buffer[0]["index"] == 100
    assert slot.trace_buffer[-1]["index"] == web_module.TRACE_BUFFER_LIMIT + 99
    runtime.close()


def test_trace_events_are_persisted_and_backfilled_across_restart(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Persisted trace session",
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
    connection = HTTPConnection(host, port, timeout=2)
    webtrace_path = record.path.with_name(record.path.stem + ".webtrace.jsonl")

    try:
        connection.request("GET", "/api/sessions/session-1/events")
        response = connection.getresponse()
        status, _payload = _post_json(
            connection,
            "/api/sessions/session-1/messages",
            {"message": "Connect the A theme"},
        )
        assert status == 202
        _read_sse_events(response, until="run_finished")
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert webtrace_path.exists()
    persisted = [
        json.loads(line)
        for line in webtrace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert persisted[0]["type"] == "run_started"
    assert persisted[-1]["type"] == "run_finished"
    assert all("replay" not in event for event in persisted)

    # —— 重启后的新 runtime 从文件回填 ——
    restarted = TauWebRuntime(manager, lambda *args: None)  # type: ignore[arg-type]

    async def collect_backfill() -> list[dict[str, Any]]:
        _subscriber_id, subscriber = await restarted._subscribe(record.id)
        events: list[dict[str, Any]] = []
        while True:
            item = subscriber.get(timeout=1)
            if item is None:
                break
            events.append(item.payload)
            if item.payload["type"] == "run_finished":
                break
        return events

    try:
        events = asyncio.run(collect_backfill())
    finally:
        restarted.close()
    assert events[0]["type"] == "web_connected"
    replayed = events[1:]
    assert replayed[0]["type"] == "run_started"
    assert replayed[-1]["type"] == "run_finished"
    assert [event["type"] for event in replayed] == [event["type"] for event in persisted]


def test_delete_session_removes_the_webtrace_file(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Deleted trace session",
        session_id="session-1",
    )
    webtrace_path = record.path.with_name(record.path.stem + ".webtrace.jsonl")
    webtrace_path.parent.mkdir(parents=True, exist_ok=True)
    webtrace_path.write_text('{"type": "run_started"}\n', encoding="utf-8")
    runtime = TauWebRuntime(manager, lambda *args: None)  # type: ignore[arg-type]

    asyncio.run(runtime._delete_session(record.id))

    assert not webtrace_path.exists()
    runtime.close()


def test_corrupt_webtrace_file_degrades_to_memory_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Corrupt trace session",
        session_id="session-1",
    )
    webtrace_path = record.path.with_name(record.path.stem + ".webtrace.jsonl")
    webtrace_path.write_text("{not json\n", encoding="utf-8")
    runtime = TauWebRuntime(manager, lambda *args: None)  # type: ignore[arg-type]

    async def subscribe_once() -> None:
        _subscriber_id, subscriber = await runtime._subscribe(record.id)
        item = subscriber.get(timeout=1)
        assert item is not None
        assert item.payload["type"] == "web_connected"

    try:
        with caplog.at_level("WARNING"):
            asyncio.run(subscribe_once())
        assert any("webtrace" in record.message.lower() for record in caplog.records)
    finally:
        runtime.close()


def test_sse_reconnect_mid_run_receives_active_run_summary(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Summary session",
        session_id="session-1",
    )
    provider = _DisconnectFakeProvider()

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
    first_connection = HTTPConnection(host, port, timeout=2)
    second_connection = HTTPConnection(host, port, timeout=2)
    command_connection = HTTPConnection(host, port, timeout=2)

    try:
        first_connection.request("GET", "/api/sessions/session-1/events")
        first_response = first_connection.getresponse()
        assert first_response.status == 200

        status, payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Slow run"},
        )
        assert status == 202
        run_id = payload["runId"]
        assert provider.started.wait(timeout=1)

        second_connection.request("GET", "/api/sessions/session-1/events")
        second_response = second_connection.getresponse()
        assert second_response.status == 200

        events = _read_sse_events(second_response, until="run_summary")
        assert events[0]["type"] == "web_connected"
        assert events[0]["running"] is True
        replayed = events[1:-1]
        assert all(event.get("replay") is True for event in replayed)
        summary = events[-1]
        assert summary["sessionId"] == "session-1"
        assert summary["runId"] == run_id
        assert summary["status"] == "running"
        assert isinstance(summary["eventCount"], int)
        assert summary["eventCount"] >= 1
        assert summary["turnCount"] == 0
        assert isinstance(summary["elapsedMs"], int)

        provider.release.set()
        _read_sse_events(second_response, until="run_finished")
        finished = _read_sse_events(first_response, until="run_finished")[-1]
        assert finished["type"] == "run_finished"
    finally:
        first_connection.close()
        second_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_message_api_dispatches_help_without_calling_the_provider(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Command session",
        session_id="session-1",
    )

    class ProviderMustNotRun:
        def stream_response(self, **kwargs: object) -> AsyncIterator[AssistantMessageEvent]:
            del kwargs
            raise AssertionError("/help must not call the provider")

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(selected, selected_manager, ProviderMustNotRun())

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
            {"message": "/help"},
        )

        assert status == 200
        assert payload["status"] == "command"
        assert payload["command"] == "/help"
        assert "Available commands:" in payload["message"]
        assert not record.path.exists()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_message_api_runs_compaction_as_an_async_session_command(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Compact session",
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

        status, _payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Retain this context"},
        )
        assert status == 202
        _read_sse_events(events_response, until="run_finished")

        status, payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "/compact Focus on the Web implementation."},
        )

        assert status == 202
        assert payload["status"] == "accepted"
        events = _read_sse_events(events_response, until="run_finished")
        result = next(event for event in events if event["type"] == "command_result")
        assert result["command"] == "/compact"
        assert result["message"] == "Compacted 2 context entries."
        entries = [
            json.loads(line) for line in record.path.read_text(encoding="utf-8").splitlines()
        ]
        compaction = next(entry for entry in entries if entry["type"] == "compaction")
        assert compaction["summary"] == "A theme is live."
        detail = session_detail_payload(manager, record.id)
        assert detail is not None
        assert all(
            message["text"] != "/compact Focus on the Web implementation."
            for message in detail["messages"]
        )
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


def test_running_session_accepts_steering_and_follow_up_and_can_clear_queue(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Queued session",
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
    events_connection = HTTPConnection(host, port, timeout=2)

    try:
        status, _payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Keep working"},
        )
        assert status == 202
        assert provider.started.wait(timeout=1)

        status, steering = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Use the smaller API", "behavior": "steer"},
        )
        assert status == 200
        assert steering == {
            "status": "queued",
            "behavior": "steer",
            "queue": {
                "steering": ["Use the smaller API"],
                "followUp": [],
            },
        }

        status, follow_up = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Then update the docs", "behavior": "follow_up"},
        )
        assert status == 200
        assert follow_up["queue"] == {
            "steering": ["Use the smaller API"],
            "followUp": ["Then update the docs"],
        }

        events_connection.request("GET", "/api/sessions/session-1/events")
        events_response = events_connection.getresponse()
        connected = _read_sse_events(events_response, until="queue_update")
        assert connected[-1]["type"] == "queue_update"
        follow_up_replay = _read_sse_events(events_response, until="queue_update")[-1]
        assert follow_up_replay["steering"] == ["Use the smaller API"]
        assert follow_up_replay["followUp"] == ["Then update the docs"]

        status, cleared = _post_json(
            command_connection,
            "/api/sessions/session-1/queue/clear",
            {},
        )
        assert status == 200
        assert cleared == {
            "status": "cleared",
            "queue": {"steering": [], "followUp": []},
        }

        status, invalid = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Ambiguous", "behavior": "later"},
        )
        assert status == 422
        assert invalid["error"] == "streaming_behavior_invalid"
    finally:
        _post_json(
            command_connection,
            "/api/sessions/session-1/cancel",
            {},
        )
        events_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("decision", "expected_status", "should_execute"),
    [
        ("allow", "completed", True),
        ("deny", "completed", False),
        ("cancel", "cancelled", False),
    ],
)
def test_tool_authorization_supports_allow_deny_and_cancel(
    tmp_path: Path,
    decision: str,
    expected_status: str,
    should_execute: bool,
) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Tool session",
        session_id="session-1",
    )
    provider = _ToolCallingFakeProvider()
    executed = Event()

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, object],
        signal: CancellationToken | None = None,
        on_update: object = None,
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal, on_update
        executed.set()
        return AgentToolResult(content="tool completed")

    tool = AgentTool(
        name="write_file",
        label="Write file",
        description="Write a project file.",
        parameters={"type": "object"},
        execute_fn=execute,
    )

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(
            selected,
            selected_manager,
            provider,
            tools=[tool],
        )

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
        status, _payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Use the write tool"},
        )
        assert status == 202

        events = _read_sse_events(
            events_response,
            until="tool_authorization_requested",
        )
        request = events[-1]
        assert request["toolName"] == "write_file"
        assert request["arguments"] == {"path": "notes.md"}
        assert not executed.is_set()

        status, resolved = _post_json(
            command_connection,
            (f"/api/sessions/session-1/tool-authorizations/{request['requestId']}"),
            {"decision": decision},
        )
        assert status == 200
        assert resolved["decision"] == decision

        completed = _read_sse_events(events_response, until="run_finished")
        assert completed[-1]["status"] == expected_status
        assert executed.is_set() is should_execute
    finally:
        events_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_tool_trace_events_pair_authorization_with_execution(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Pairing session",
        session_id="session-1",
    )
    provider = _ToolCallingFakeProvider()

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, object],
        signal: CancellationToken | None = None,
        on_update: object = None,
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal, on_update
        return AgentToolResult(content="tool completed")

    tool = AgentTool(
        name="write_file",
        label="Write file",
        description="Write a project file.",
        parameters={"type": "object"},
        execute_fn=execute,
    )

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(selected, selected_manager, provider, tools=[tool])

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
        status, payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Use the write tool"},
        )
        assert status == 202
        run_id = payload["runId"]

        pre_auth = _read_sse_events(
            events_response,
            until="tool_authorization_requested",
        )
        # Tau yields tool_execution_start before invoking the authorization hook.
        start = next(e for e in pre_auth if e["type"] == "tool_execution_start")
        request = pre_auth[-1]
        assert request["type"] == "tool_authorization_requested"
        assert request["runId"] == run_id
        assert isinstance(request["timestamp"], int)
        assert request["toolCallId"] == "call-1"

        status, resolved = _post_json(
            command_connection,
            "/api/sessions/session-1/tool-authorizations/" + request["requestId"],
            {"decision": "allow"},
        )
        assert status == 200
        assert resolved["decision"] == "allow"

        events = _read_sse_events(events_response, until="run_finished")
        end = next(e for e in events if e["type"] == "tool_execution_end")
        end = next(e for e in events if e["type"] == "tool_execution_end")
        assert start["runId"] == run_id
        assert start["toolCallId"] == request["toolCallId"]
        assert start["args"] == {"path": "notes.md"}
        assert end["runId"] == run_id
        assert end["isError"] is False
        assert end["result"]["content"][0]["text"] == "tool completed"

        finished = events[-1]
        assert finished["type"] == "run_finished"
        assert finished["turnCount"] == 2
    finally:
        events_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_tool_authorization_defaults_to_deny_when_the_browser_disconnects(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Disconnected tool session",
        session_id="session-1",
    )
    provider = _ToolCallingFakeProvider()
    executed = Event()

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, object],
        signal: CancellationToken | None = None,
        on_update: object = None,
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal, on_update
        executed.set()
        return AgentToolResult(content="must not run")

    tool = AgentTool(
        name="write_file",
        label="Write file",
        description="Write a project file.",
        parameters={"type": "object"},
        execute_fn=execute,
    )

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(
            selected,
            selected_manager,
            provider,
            tools=[tool],
        )

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
    subscriber_id, subscriber = server.web_runtime.subscribe("session-1")

    try:
        status, _payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Use the write tool without a browser"},
        )

        assert status == 202
        while True:
            item = subscriber.get(timeout=1)
            assert item is not None
            if item.payload["type"] == "tool_authorization_requested":
                break
        assert not executed.is_set()

        server.web_runtime.unsubscribe("session-1", subscriber_id)
        assert provider.finished.wait(timeout=1)
        assert not executed.is_set()
    finally:
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_tool_authorization_decision_is_broadcast_to_subscribers(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Auth broadcast session",
        session_id="session-1",
    )
    provider = _ToolCallingFakeProvider()

    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, object],
        signal: CancellationToken | None = None,
        on_update: object = None,
    ) -> AgentToolResult:
        del tool_call_id, arguments, signal, on_update
        return AgentToolResult(content="tool completed")

    tool = AgentTool(
        name="write_file",
        label="Write file",
        description="Write a project file.",
        parameters={"type": "object"},
        execute_fn=execute,
    )

    async def load_session(
        selected: CodingSessionRecord,
        selected_manager: SessionManager,
    ) -> WebSessionHandle:
        return await _load_test_session(
            selected,
            selected_manager,
            provider,
            tools=[tool],
        )

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
        status, _payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Use the write tool"},
        )
        assert status == 202
        events = _read_sse_events(
            events_response,
            until="tool_authorization_requested",
        )
        request = events[-1]

        status, _resolved = _post_json(
            command_connection,
            f"/api/sessions/session-1/tool-authorizations/{request['requestId']}",
            {"decision": "allow"},
        )
        assert status == 200

        follow_up = _read_sse_events(
            events_response,
            until="tool_authorization_resolved",
        )
        resolved = follow_up[-1]
        assert resolved["requestId"] == request["requestId"]
        assert resolved["toolCallId"] == request["toolCallId"]
        assert resolved["decision"] == "allow"
        assert resolved["runId"] == request["runId"]
    finally:
        events_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_disconnect_denied_authorization_is_visible_after_reconnect(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Disconnect deny session",
        session_id="session-1",
    )

    class NeverEndingToolProvider:
        def __init__(self) -> None:
            self.calls = 0
            self.finished = Event()

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
            self.calls += 1
            call_number = self.calls

            async def iterator() -> AsyncIterator[AssistantMessageEvent]:
                if call_number > 1:
                    yield assistant_start(model="fake")
                    yield assistant_done(
                        message=AssistantMessage(content="Finished.", model="fake")
                    )
                    self.finished.set()
                    return
                call = ToolCall(id="call-1", name="write_file", arguments={"path": "notes.md"})
                assistant = AssistantMessage(content=[call], model="fake")
                yield assistant_start(model="fake")
                yield tool_call_end(call)
                yield assistant_done(message=assistant, finish_reason="toolUse")

            return iterator()

    provider = NeverEndingToolProvider()

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
    second_connection = HTTPConnection(host, port, timeout=2)
    command_connection = HTTPConnection(host, port, timeout=2)

    try:
        subscriber_id, subscriber = server.web_runtime.subscribe("session-1")
        status, _payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Trigger authorization"},
        )
        assert status == 202
        while True:
            item = subscriber.get(timeout=1)
            assert item is not None
            if item.payload["type"] == "tool_authorization_requested":
                break

        # 断开唯一订阅者 → 未决授权被自动拒绝
        server.web_runtime.unsubscribe("session-1", subscriber_id)
        assert provider.finished.wait(timeout=1)

        second_connection.request("GET", "/api/sessions/session-1/events")
        second_response = second_connection.getresponse()
        events = _read_sse_events(second_response, until="tool_authorization_resolved")
        requested = [e for e in events if e["type"] == "tool_authorization_requested"]
        resolved = events[-1]
        assert requested, "authorization request should be replayed"
        assert resolved["requestId"] == requested[-1]["requestId"]
        assert resolved["decision"] == "no_subscriber"
    finally:
        second_connection.close()
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_disconnect_denied_authorization_is_persisted_across_restart(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Persisted disconnect deny session",
        session_id="session-1",
    )
    provider = _ToolCallingFakeProvider()

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
    webtrace_path = record.path.with_name(record.path.stem + ".webtrace.jsonl")

    try:
        subscriber_id, subscriber = server.web_runtime.subscribe("session-1")
        status, _payload = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Trigger authorization"},
        )
        assert status == 202
        while True:
            item = subscriber.get(timeout=1)
            assert item is not None
            if item.payload["type"] == "tool_authorization_requested":
                break

        # 断开唯一订阅者 → 未决授权被自动拒绝，resolved 帧必须落盘
        server.web_runtime.unsubscribe("session-1", subscriber_id)
        assert provider.finished.wait(timeout=1)
    finally:
        command_connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    persisted = [
        json.loads(line)
        for line in webtrace_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        event["type"] == "tool_authorization_resolved" and event["decision"] == "no_subscriber"
        for event in persisted
    )

    # —— 重启后的新 runtime 从磁盘回填 resolved 帧 ——
    restarted = TauWebRuntime(manager, lambda *args: None)  # type: ignore[arg-type]

    async def collect_backfill() -> list[dict[str, Any]]:
        _subscriber_id, backfill = await restarted._subscribe(record.id)
        events: list[dict[str, Any]] = []
        while True:
            item = backfill.get(timeout=1)
            if item is None:
                break
            events.append(item.payload)
            if item.payload["type"] == "tool_authorization_resolved":
                break
        return events

    try:
        replayed = asyncio.run(collect_backfill())
    finally:
        restarted.close()
    assert any(
        event["type"] == "tool_authorization_resolved" and event["decision"] == "no_subscriber"
        for event in replayed
    )


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


def test_delete_api_refusal_keeps_the_webtrace_file_intact(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake",
        title="Running trace session",
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
    webtrace_path = record.path.with_name(record.path.stem + ".webtrace.jsonl")

    try:
        status, _ = _post_json(
            command_connection,
            "/api/sessions/session-1/messages",
            {"message": "Keep tracing"},
        )
        assert status == 202
        assert provider.started.wait(timeout=1)
        assert webtrace_path.exists()
        persisted_before = webtrace_path.read_text(encoding="utf-8")
        assert persisted_before.strip()

        status, rejected = _delete_json(
            command_connection,
            "/api/sessions/session-1",
            {"confirmation": "DELETE"},
        )

        assert status == 409
        assert rejected["error"] == "session_busy"
        assert manager.get_session(record.id) is not None
        assert record.path.exists()
        assert webtrace_path.exists()
        assert webtrace_path.read_text(encoding="utf-8") == persisted_before

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


class _ToolCallingFakeProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.finished = Event()

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
        self.calls += 1
        call_number = self.calls

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            if call_number == 1:
                call = ToolCall(
                    id="call-1",
                    name="write_file",
                    arguments={"path": "notes.md"},
                )
                assistant = AssistantMessage(content=[call], model="fake")
                yield assistant_start(model="fake")
                yield tool_call_end(call)
                yield assistant_done(message=assistant, finish_reason="toolUse")
                return
            yield assistant_start(model="fake")
            yield assistant_done(message=AssistantMessage(content="Finished.", model="fake"))
            self.finished.set()

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
