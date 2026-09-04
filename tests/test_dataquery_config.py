"""Data-query config model tests (PRD testing decision 5, config part)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tau_coding.credentials import FileCredentialStore, credentials_path
from tau_coding.dataquery.config import (
    CREDENTIAL_DWS_PASSWORD,
    CREDENTIAL_SAG_TOKEN,
    DEFAULT_SAG_AGENT_ID,
    ENV_DWS_HOST,
    ENV_DWS_MAX_ROWS,
    ENV_DWS_PORT,
    ENV_DWS_USER,
    ENV_SAG_TOKEN,
    DataQueryConfigError,
    DataQueryUserConfig,
    apply_config_api_update,
    config_api_payload,
    data_query_config_path,
    load_user_config,
    resolve_data_query_config,
    save_user_config,
)
from tau_coding.paths import TauPaths


@pytest.fixture
def paths(tmp_path: Path) -> TauPaths:
    return TauPaths(home=tmp_path / ".tau")


@pytest.fixture
def store(paths: TauPaths) -> FileCredentialStore:
    return FileCredentialStore(credentials_path(paths))


def test_default_config_when_missing(paths: TauPaths) -> None:
    config = load_user_config(paths)
    assert config.planning_mode == "unconfigured"
    assert config.sag_agent_origin == ""
    assert config.sag_agent_id == DEFAULT_SAG_AGENT_ID
    assert config.sag_agent_timeout_seconds == 60
    assert config.sag_agent_answer_max_bytes == 64 * 1024
    assert config.sag_citation_limit == 5
    assert config.sag_citation_snippet_max_bytes == 8 * 1024
    assert config.sag_planner_transcript_max_bytes == 256 * 1024
    assert config.host == "localhost"
    assert config.port == 35432
    assert config.sslmode == "prefer"
    assert config.query_timeout_seconds == 180
    assert config.max_rows == 1000
    assert config.max_result_bytes == 1024 * 1024
    assert config.max_cell_bytes == 64 * 1024
    assert config.sag_source_id == "19d09d3733c34716bcdf906738d10b03"
    assert config.sag_search_tool == "search"
    assert config.sag_read_tool == "get_chunk"
    assert config.sag_arg_query == "query"
    assert config.sag_arg_source == "source_id"
    assert config.sag_arg_document == "chunk_id"
    assert config.sag_protocol_version == "2025-03-26"
    assert config.sag_rpc_timeout_seconds == 60
    assert config.sag_probe_query == "__probe__"
    assert config.sag_search_summary_max_bytes == 8 * 1024
    assert "参考知识库中的SQL模板" in config.sag_question_template
    assert config.dws_connect_timeout == 10
    assert config.dws_probe_query == "SELECT 1"


def test_unconfigured_mode_is_incomplete_with_stable_diagnostic(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    store.set(CREDENTIAL_DWS_PASSWORD, "pw")
    store.set(CREDENTIAL_SAG_TOKEN, "tok")

    payload = config_api_payload(
        paths,
        env={ENV_DWS_USER: "reader"},
        credentials=store,
    )

    assert payload["planningMode"] == "unconfigured"
    assert payload["complete"] is False
    assert payload["configurationDiagnostics"] == [
        {
            "code": "planning_mode_unconfigured",
            "field": "planning_mode",
            "message": "Choose legacy or agent planning mode.",
        }
    ]


def test_sag_integration_fields_round_trip(paths: TauPaths) -> None:
    original = DataQueryUserConfig(
        host="dws.example.com",
        port=8000,
        username="reader",
        sag_source_id="custom-source",
        sag_search_tool="find",
        sag_read_tool="fetch",
        sag_arg_query="q",
        sag_arg_source="src",
        sag_arg_document="doc",
        sag_question_template="{question} 请直接生成SQL",
    )
    path = save_user_config(original, paths)
    loaded = load_user_config(paths)
    assert loaded == original
    assert path == data_query_config_path(paths)


def test_sag_integration_env_overrides(paths: TauPaths, store: FileCredentialStore) -> None:
    from tau_coding.dataquery.config import (
        ENV_DWS_CONNECT_TIMEOUT,
        ENV_DWS_PROBE_QUERY,
        ENV_SAG_ARG_DOCUMENT,
        ENV_SAG_ARG_QUERY,
        ENV_SAG_ARG_SOURCE,
        ENV_SAG_PROBE_QUERY,
        ENV_SAG_PROTOCOL_VERSION,
        ENV_SAG_QUESTION_TEMPLATE,
        ENV_SAG_READ_TOOL,
        ENV_SAG_RPC_TIMEOUT_SECONDS,
        ENV_SAG_SEARCH_SUMMARY_MAX_BYTES,
        ENV_SAG_SEARCH_TOOL,
        ENV_SAG_SOURCE_ID,
    )

    store.set(CREDENTIAL_DWS_PASSWORD, "pw")
    store.set(CREDENTIAL_SAG_TOKEN, "tok")
    resolved = resolve_data_query_config(
        paths,
        env={
            ENV_SAG_SOURCE_ID: "env-source",
            ENV_SAG_SEARCH_TOOL: "find",
            ENV_SAG_READ_TOOL: "fetch",
            ENV_SAG_ARG_QUERY: "q",
            ENV_SAG_ARG_SOURCE: "src",
            ENV_SAG_ARG_DOCUMENT: "doc",
            ENV_SAG_PROTOCOL_VERSION: "2025-06-18",
            ENV_SAG_RPC_TIMEOUT_SECONDS: "120",
            ENV_SAG_PROBE_QUERY: "ping",
            ENV_SAG_SEARCH_SUMMARY_MAX_BYTES: "4096",
            ENV_SAG_QUESTION_TEMPLATE: "参考模板：{question}",
            ENV_DWS_CONNECT_TIMEOUT: "30",
            ENV_DWS_PROBE_QUERY: "SELECT 2",
            ENV_DWS_USER: "reader",
        },
        credentials=store,
    )
    assert resolved.sag_source_id == "env-source"
    assert resolved.sag_search_tool == "find"
    assert resolved.sag_read_tool == "fetch"
    assert resolved.sag_arg_query == "q"
    assert resolved.sag_arg_source == "src"
    assert resolved.sag_arg_document == "doc"
    assert resolved.sag_protocol_version == "2025-06-18"
    assert resolved.sag_rpc_timeout_seconds == 120
    assert resolved.sag_probe_query == "ping"
    assert resolved.sag_search_summary_max_bytes == 4096
    assert resolved.sag_question_template == "参考模板：{question}"
    assert resolved.dws_connect_timeout == 30
    assert resolved.dws_probe_query == "SELECT 2"
    assert all(
        resolved.sources[name] == "env"
        for name in (
            "sag_source_id",
            "sag_search_tool",
            "sag_read_tool",
            "sag_arg_query",
            "sag_arg_source",
            "sag_arg_document",
            "sag_protocol_version",
            "sag_rpc_timeout_seconds",
            "sag_probe_query",
            "sag_search_summary_max_bytes",
            "sag_question_template",
            "dws_connect_timeout",
            "dws_probe_query",
        )
    )


def test_sag_integration_defaults_marked_as_default(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    store.set(CREDENTIAL_DWS_PASSWORD, "pw")
    store.set(CREDENTIAL_SAG_TOKEN, "tok")
    resolved = resolve_data_query_config(paths, env={}, credentials=store)
    assert resolved.sag_source_id == "19d09d3733c34716bcdf906738d10b03"
    assert resolved.sources["sag_source_id"] == "default"
    assert resolved.sag_agent_id == DEFAULT_SAG_AGENT_ID
    assert resolved.sources["sag_agent_id"] == "default"
    assert resolved.sources["sag_search_tool"] == "default"
    assert resolved.sag_protocol_version == "2025-03-26"
    assert resolved.sag_rpc_timeout_seconds == 60
    assert resolved.sag_probe_query == "__probe__"
    assert resolved.sag_search_summary_max_bytes == 8 * 1024
    assert resolved.sources["sag_question_template"] == "default"
    assert resolved.dws_connect_timeout == 10
    assert resolved.dws_probe_query == "SELECT 1"


def test_sag_source_id_in_api_payload_tracks_config(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    save_user_config(DataQueryUserConfig(sag_source_id="configured-source"), paths)
    payload = config_api_payload(paths, env={}, credentials=store)
    assert payload["sagSourceId"] == "configured-source"


def test_save_and_load_round_trip(paths: TauPaths) -> None:
    original = DataQueryUserConfig(host="dws.example.com", port=8000, username="reader")
    path = save_user_config(original, paths)
    assert path == data_query_config_path(paths)
    loaded = load_user_config(paths)
    assert loaded == original


def test_invalid_config_rejected(tmp_path: Path) -> None:
    path = tmp_path / ".tau" / "dataquery.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"port": "not-an-int"}', encoding="utf-8")
    with pytest.raises(DataQueryConfigError):
        load_user_config(TauPaths(home=tmp_path / ".tau"))


def test_env_overrides_user_config(paths: TauPaths, store: FileCredentialStore) -> None:
    save_user_config(DataQueryUserConfig(host="config.example.com"), paths)
    store.set(CREDENTIAL_DWS_PASSWORD, "pw")
    store.set(CREDENTIAL_SAG_TOKEN, "tok")

    resolved = resolve_data_query_config(
        paths,
        env={ENV_DWS_HOST: "env.example.com", ENV_DWS_PORT: "8000"},
        credentials=store,
    )
    assert resolved.host == "env.example.com"
    assert resolved.sources["host"] == "env"
    assert resolved.port == 8000
    assert resolved.sources["port"] == "env"
    assert resolved.username == ""
    assert resolved.sources["username"] == "default"


def test_env_source_marking(paths: TauPaths, store: FileCredentialStore) -> None:
    save_user_config(DataQueryUserConfig(host="config.example.com", max_rows=50), paths)
    resolved = resolve_data_query_config(
        paths,
        env={ENV_DWS_MAX_ROWS: "25"},
        credentials=store,
    )
    assert resolved.sources["host"] == "config"
    assert resolved.sources["max_rows"] == "env"
    assert resolved.sources["port"] == "default"
    assert resolved.max_rows == 25


def test_secrets_resolved_from_credential_store(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    store.set(CREDENTIAL_DWS_PASSWORD, "secret-pw")
    resolved = resolve_data_query_config(paths, env={}, credentials=store)
    assert resolved.secrets.dws_password == "secret-pw"
    assert "dws_password" in resolved.secret_sources
    assert resolved.secret_sources["dws_password"] == "config"


def test_sag_token_prefers_env(paths: TauPaths, store: FileCredentialStore) -> None:
    store.set(CREDENTIAL_SAG_TOKEN, "stored-token")
    resolved = resolve_data_query_config(paths, env={ENV_SAG_TOKEN: "env-token"}, credentials=store)
    assert resolved.secrets.sag_token == "env-token"
    assert resolved.secret_sources["sag_token"] == "env"


def test_complete_requires_explicit_mode_and_mode_specific_fields(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    assert not resolve_data_query_config(paths, env={}, credentials=store).complete
    store.set(CREDENTIAL_DWS_PASSWORD, "pw")
    store.set(CREDENTIAL_SAG_TOKEN, "tok")
    common_env = {ENV_DWS_USER: "reader"}

    unconfigured = resolve_data_query_config(paths, env=common_env, credentials=store)
    assert not unconfigured.complete

    legacy = resolve_data_query_config(
        paths,
        env={**common_env, "TAU_SAG_PLANNING_MODE": "legacy"},
        credentials=store,
    )
    assert legacy.complete

    agent_without_id = resolve_data_query_config(
        paths,
        env={
            **common_env,
            "TAU_SAG_PLANNING_MODE": "agent",
            "TAU_SAG_AGENT_ORIGIN": "http://localhost:8100",
        },
        credentials=store,
    )
    assert not agent_without_id.complete
    assert [item["code"] for item in agent_without_id.configuration_diagnostics] == [
        "agent_id_missing"
    ]

    agent = resolve_data_query_config(
        paths,
        env={
            **common_env,
            "TAU_SAG_PLANNING_MODE": "agent",
            "TAU_SAG_AGENT_ORIGIN": "http://localhost:8100",
            "TAU_SAG_AGENT_ID": "agent_01",
        },
        credentials=store,
    )
    assert agent.complete


def test_mcp_endpoint_default_applies_only_to_legacy_mode(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    store.set(CREDENTIAL_DWS_PASSWORD, "pw")
    store.set(CREDENTIAL_SAG_TOKEN, "tok")
    common = {ENV_DWS_USER: "reader"}

    legacy = resolve_data_query_config(
        paths,
        env={**common, "TAU_SAG_PLANNING_MODE": "legacy"},
        credentials=store,
    )
    assert legacy.sag_endpoint == "http://localhost:8100/mcp/"
    assert legacy.complete

    agent = resolve_data_query_config(
        paths,
        env={
            **common,
            "TAU_SAG_PLANNING_MODE": "agent",
            "TAU_SAG_AGENT_ORIGIN": "http://localhost:8100",
            "TAU_SAG_AGENT_ID": "agent-01",
        },
        credentials=store,
    )
    assert agent.sag_endpoint == ""
    assert agent.complete


def test_invalid_env_values_rejected(paths: TauPaths, store: FileCredentialStore) -> None:
    with pytest.raises(DataQueryConfigError):
        resolve_data_query_config(paths, env={ENV_DWS_PORT: "not-a-port"}, credentials=store)
    with pytest.raises(DataQueryConfigError):
        resolve_data_query_config(paths, env={ENV_DWS_PORT: "99999"}, credentials=store)


def test_agent_origin_is_normalized_and_url_path_inputs_are_diagnosed(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    store.set(CREDENTIAL_DWS_PASSWORD, "pw")
    store.set(CREDENTIAL_SAG_TOKEN, "tok")
    common = {
        ENV_DWS_USER: "reader",
        "TAU_SAG_PLANNING_MODE": "agent",
    }

    normalized = resolve_data_query_config(
        paths,
        env={
            **common,
            "TAU_SAG_AGENT_ORIGIN": "http://localhost:8100/",
            "TAU_SAG_AGENT_ID": "agent-01_ok",
        },
        credentials=store,
    )
    assert normalized.sag_agent_origin == "http://localhost:8100"
    assert normalized.complete

    invalid = resolve_data_query_config(
        paths,
        env={
            **common,
            "TAU_SAG_AGENT_ORIGIN": "http://localhost:8100/api",
            "TAU_SAG_AGENT_ID": "../agent",
        },
        credentials=store,
    )
    assert not invalid.complete
    assert [item["code"] for item in invalid.configuration_diagnostics] == [
        "agent_origin_invalid",
        "agent_id_invalid",
    ]


def test_agent_limits_resolve_from_environment(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    store.set(CREDENTIAL_DWS_PASSWORD, "pw")
    store.set(CREDENTIAL_SAG_TOKEN, "tok")
    resolved = resolve_data_query_config(
        paths,
        env={
            ENV_DWS_USER: "reader",
            "TAU_SAG_PLANNING_MODE": "agent",
            "TAU_SAG_AGENT_ORIGIN": "https://sag.example.com/",
            "TAU_SAG_AGENT_ID": "planner_01",
            "TAU_SAG_AGENT_TIMEOUT_SECONDS": "45",
            "TAU_SAG_AGENT_ANSWER_MAX_BYTES": "32768",
            "TAU_SAG_CITATION_LIMIT": "3",
            "TAU_SAG_CITATION_SNIPPET_MAX_BYTES": "4096",
            "TAU_SAG_PLANNER_TRANSCRIPT_MAX_BYTES": "131072",
        },
        credentials=store,
    )

    assert resolved.complete
    assert resolved.sag_agent_origin == "https://sag.example.com"
    assert resolved.sag_agent_id == "planner_01"
    assert resolved.sag_agent_timeout_seconds == 45
    assert resolved.sag_agent_answer_max_bytes == 32768
    assert resolved.sag_citation_limit == 3
    assert resolved.sag_citation_snippet_max_bytes == 4096
    assert resolved.sag_planner_transcript_max_bytes == 131072


def test_agent_response_limits_cannot_exceed_hard_caps(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    with pytest.raises(DataQueryConfigError, match="TAU_SAG_AGENT_ANSWER_MAX_BYTES"):
        resolve_data_query_config(
            paths,
            env={"TAU_SAG_AGENT_ANSWER_MAX_BYTES": str(64 * 1024 + 1)},
            credentials=store,
        )


def test_api_payload_has_no_secret_values(paths: TauPaths, store: FileCredentialStore) -> None:
    store.set(CREDENTIAL_DWS_PASSWORD, "super-secret-pw")
    payload = config_api_payload(paths, env={}, credentials=store)
    serialized = str(payload)
    assert "super-secret-pw" not in serialized
    assert payload["passwordConfigured"] is True
    assert payload["sagSourceId"] == "19d09d3733c34716bcdf906738d10b03"
    assert payload["fields"]["sslmode"]["value"] == "prefer"


def test_api_update_saves_fields_and_preserves_empty_password(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    store.set(CREDENTIAL_DWS_PASSWORD, "original-pw")
    payload = apply_config_api_update(
        {"host": "web.example.com", "password": ""},
        paths,
        env={},
        credentials=store,
    )
    assert payload["fields"]["host"]["value"] == "web.example.com"
    assert payload["passwordConfigured"] is True
    assert store.get(CREDENTIAL_DWS_PASSWORD) == "original-pw"


def test_api_update_replaces_password(paths: TauPaths, store: FileCredentialStore) -> None:
    store.set(CREDENTIAL_DWS_PASSWORD, "old-pw")
    apply_config_api_update(
        {"password": "new-pw"},
        paths,
        env={},
        credentials=store,
    )
    assert store.get(CREDENTIAL_DWS_PASSWORD) == "new-pw"


def test_api_update_ignores_env_overridden_fields(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    save_user_config(DataQueryUserConfig(host="config.example.com"), paths)
    payload = apply_config_api_update(
        {"host": "should-be-ignored", "database": "newdb"},
        paths,
        env={ENV_DWS_HOST: "env.example.com"},
        credentials=store,
    )
    assert payload["fields"]["host"]["value"] == "env.example.com"
    assert payload["fields"]["database"]["value"] == "newdb"


def test_api_update_rejects_non_string_password(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    with pytest.raises(DataQueryConfigError, match="password"):
        apply_config_api_update({"password": 123}, paths, env={}, credentials=store)


def test_api_update_rejects_agent_origin_paths_and_unsafe_agent_ids(
    paths: TauPaths, store: FileCredentialStore
) -> None:
    with pytest.raises(DataQueryConfigError, match="origin must not contain a path"):
        apply_config_api_update(
            {
                "planning_mode": "agent",
                "sag_agent_origin": "http://localhost:8100/api/v1",
                "sag_agent_id": "agent-01",
            },
            paths,
            env={},
            credentials=store,
        )

    with pytest.raises(DataQueryConfigError, match="unsupported characters"):
        apply_config_api_update(
            {
                "planning_mode": "agent",
                "sag_agent_origin": "http://localhost:8100",
                "sag_agent_id": "../agent%2Fmessages",
            },
            paths,
            env={},
            credentials=store,
        )
