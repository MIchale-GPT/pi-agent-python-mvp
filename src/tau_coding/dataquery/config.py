"""User-level data-query configuration for the evidence-bound DWS extension.

This module owns the durable configuration model (``~/.tau/dataquery.json``),
the environment-override resolution (env -> user config -> defaults) and the
secret lookup (DWS password and SAG token via the credential store). It has no
knowledge of Web, TUI, tools or database drivers, so CLI, TUI and Web share the
same effective configuration.

Field precedence follows the PRD implementation decision: environment variables
win over the user config file, which wins over the built-in defaults. Every
resolved field carries a ``source`` marker so hosts can render env-controlled
fields as read-only.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from json import dumps, loads
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, TypedDict, cast
from urllib.parse import urlsplit

from tau_coding.credentials import FileCredentialStore, credentials_path
from tau_coding.paths import TauPaths

# ---------------------------------------------------------------------------
# Env variable names (deployment overrides, never defaults for secrets).
# ---------------------------------------------------------------------------

ENV_DWS_HOST = "TAU_DWS_HOST"
ENV_DWS_PORT = "TAU_DWS_PORT"
ENV_DWS_DATABASE = "TAU_DWS_DATABASE"
ENV_DWS_USER = "TAU_DWS_USER"
ENV_DWS_SSL_MODE = "TAU_DWS_SSL_MODE"
ENV_DWS_TIMEOUT_SECONDS = "TAU_DWS_TIMEOUT_SECONDS"
ENV_DWS_MAX_ROWS = "TAU_DWS_MAX_ROWS"
ENV_DWS_MAX_RESULT_BYTES = "TAU_DWS_MAX_RESULT_BYTES"
ENV_DWS_MAX_CELL_BYTES = "TAU_DWS_MAX_CELL_BYTES"
ENV_SAG_ENDPOINT = "TAU_SAG_ENDPOINT"
ENV_SAG_TOKEN = "TAU_SAG_TOKEN"
ENV_SAG_PLANNING_MODE = "TAU_SAG_PLANNING_MODE"
ENV_SAG_AGENT_ORIGIN = "TAU_SAG_AGENT_ORIGIN"
ENV_SAG_AGENT_ID = "TAU_SAG_AGENT_ID"
ENV_SAG_AGENT_TIMEOUT_SECONDS = "TAU_SAG_AGENT_TIMEOUT_SECONDS"
ENV_SAG_AGENT_ANSWER_MAX_BYTES = "TAU_SAG_AGENT_ANSWER_MAX_BYTES"
ENV_SAG_CITATION_LIMIT = "TAU_SAG_CITATION_LIMIT"
ENV_SAG_CITATION_SNIPPET_MAX_BYTES = "TAU_SAG_CITATION_SNIPPET_MAX_BYTES"
ENV_SAG_PLANNER_TRANSCRIPT_MAX_BYTES = "TAU_SAG_PLANNER_TRANSCRIPT_MAX_BYTES"
ENV_SAG_SOURCE_ID = "TAU_SAG_SOURCE_ID"
ENV_SAG_SEARCH_TOOL = "TAU_SAG_SEARCH_TOOL"
ENV_SAG_READ_TOOL = "TAU_SAG_READ_TOOL"
ENV_SAG_ARG_QUERY = "TAU_SAG_ARG_QUERY"
ENV_SAG_ARG_SOURCE = "TAU_SAG_ARG_SOURCE"
ENV_SAG_ARG_DOCUMENT = "TAU_SAG_ARG_DOCUMENT"
ENV_SAG_PROTOCOL_VERSION = "TAU_SAG_PROTOCOL_VERSION"
ENV_SAG_RPC_TIMEOUT_SECONDS = "TAU_SAG_RPC_TIMEOUT_SECONDS"
ENV_SAG_PROBE_QUERY = "TAU_SAG_PROBE_QUERY"
ENV_SAG_SEARCH_SUMMARY_MAX_BYTES = "TAU_SAG_SEARCH_SUMMARY_MAX_BYTES"
# Question rewrite template applied before every SAG search. Set to an empty
# string to pass the user question through unchanged.
ENV_SAG_QUESTION_TEMPLATE = "TAU_SAG_QUESTION_TEMPLATE"
ENV_DWS_CONNECT_TIMEOUT = "TAU_DWS_CONNECT_TIMEOUT"
ENV_DWS_PROBE_QUERY = "TAU_DWS_PROBE_QUERY"
ENV_DATA_AUTO_APPROVE_EXECUTE = "TAU_DATA_AUTO_APPROVE_EXECUTE"
# Comma-separated `schema` or `schema.table` entries (deployment config).
ENV_DWS_ALLOWED_OBJECTS = "TAU_DWS_ALLOWED_OBJECTS"

# Credential-store names (never persisted in the config file).
CREDENTIAL_DWS_PASSWORD = "dataquery.dws.password"
CREDENTIAL_SAG_TOKEN = "dataquery.sag.token"

# Knowledge source and SAG tool/argument mapping (decision 4). These are SAG
# integration points and are now configurable; defaults keep the original
# deployment behavior.
DEFAULT_SAG_SOURCE_ID = "19d09d3733c34716bcdf906738d10b03"
DEFAULT_SAG_SEARCH_TOOL = "search"
DEFAULT_SAG_READ_TOOL = "get_chunk"
DEFAULT_SAG_ARG_QUERY = "query"
DEFAULT_SAG_ARG_SOURCE = "source_id"
DEFAULT_SAG_ARG_DOCUMENT = "chunk_id"
DEFAULT_SAG_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_SAG_RPC_TIMEOUT_SECONDS = 60
DEFAULT_SAG_PROBE_QUERY = "__probe__"
DEFAULT_SAG_SEARCH_SUMMARY_MAX_BYTES = 8 * 1024
DEFAULT_SAG_PLANNING_MODE = "unconfigured"
DEFAULT_SAG_AGENT_ORIGIN = ""
DEFAULT_SAG_AGENT_ID = ""
DEFAULT_SAG_AGENT_TIMEOUT_SECONDS = 60
DEFAULT_SAG_AGENT_ANSWER_MAX_BYTES = 64 * 1024
DEFAULT_SAG_CITATION_LIMIT = 5
DEFAULT_SAG_CITATION_SNIPPET_MAX_BYTES = 8 * 1024
DEFAULT_SAG_PLANNER_TRANSCRIPT_MAX_BYTES = 256 * 1024
# Default template instructs SAG to reference its SQL templates and produce
# SQL directly.  Set to "" (empty) to disable rewriting.
DEFAULT_SAG_QUESTION_TEMPLATE = (
    "请参考知识库中的SQL模板，在一次问答中为下面的数据问题直接编写完整、可执行的查询SQL：\n"
    "1. 只输出与用户提问相关的字段，不要输出多余字段；\n"
    "2. 结合知识库中的实体主体清单、口径说明和表结构，直接确定实体编码、"
    "单户/合并口径、表名和字段名，不要反问用户；\n"
    "3. 报告期按知识库SQL模板的格式归一化（如 2025年4月 → 202504）；\n"
    "4. 若问题涉及环比/同比或多期比较，按知识库中的SQL模板生成；\n"
    "5. 不要只返回表结构、字段解释或下一步检索建议，也不要要求按实体、口径、"
    "字段拆成多轮提问；请直接完成这些判断并给出SQL。\n\n"
    "用户问题：{question}"
)
DEFAULT_DWS_CONNECT_TIMEOUT = 10
DEFAULT_DWS_PROBE_QUERY = "SELECT 1"

DEFAULT_SSL_MODE = "prefer"
DEFAULT_QUERY_TIMEOUT_SECONDS = 180
DEFAULT_MAX_ROWS = 1000
DEFAULT_MAX_RESULT_BYTES = 1024 * 1024
DEFAULT_MAX_CELL_BYTES = 64 * 1024
DEFAULT_SAG_ENDPOINT = "http://localhost:8100/mcp/"

ConfigSource = Literal["env", "config", "default"]
PlanningMode = Literal["unconfigured", "legacy", "agent"]
_PLANNING_MODES = frozenset({"unconfigured", "legacy", "agent"})
_AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_CONFIG_FIELD_NAMES = (
    "host",
    "port",
    "database",
    "username",
    "sslmode",
    "query_timeout_seconds",
    "max_rows",
    "max_result_bytes",
    "max_cell_bytes",
    "planning_mode",
    "sag_endpoint",
    "sag_agent_origin",
    "sag_agent_id",
    "sag_agent_timeout_seconds",
    "sag_agent_answer_max_bytes",
    "sag_citation_limit",
    "sag_citation_snippet_max_bytes",
    "sag_planner_transcript_max_bytes",
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


class DataQueryConfigError(ValueError):
    """Raised when data-query configuration is invalid."""


@dataclass(frozen=True, slots=True)
class DataQueryUserConfig:
    """Durable non-secret settings persisted under ``~/.tau/dataquery.json``."""

    host: str = "localhost"
    port: int = 35432
    database: str = "exchange_service"
    username: str = ""
    sslmode: str = DEFAULT_SSL_MODE
    query_timeout_seconds: int = DEFAULT_QUERY_TIMEOUT_SECONDS
    max_rows: int = DEFAULT_MAX_ROWS
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES
    max_cell_bytes: int = DEFAULT_MAX_CELL_BYTES
    planning_mode: PlanningMode = "unconfigured"
    sag_endpoint: str = ""
    sag_agent_origin: str = DEFAULT_SAG_AGENT_ORIGIN
    sag_agent_id: str = DEFAULT_SAG_AGENT_ID
    sag_agent_timeout_seconds: int = DEFAULT_SAG_AGENT_TIMEOUT_SECONDS
    sag_agent_answer_max_bytes: int = DEFAULT_SAG_AGENT_ANSWER_MAX_BYTES
    sag_citation_limit: int = DEFAULT_SAG_CITATION_LIMIT
    sag_citation_snippet_max_bytes: int = DEFAULT_SAG_CITATION_SNIPPET_MAX_BYTES
    sag_planner_transcript_max_bytes: int = DEFAULT_SAG_PLANNER_TRANSCRIPT_MAX_BYTES
    sag_source_id: str = DEFAULT_SAG_SOURCE_ID
    sag_search_tool: str = DEFAULT_SAG_SEARCH_TOOL
    sag_read_tool: str = DEFAULT_SAG_READ_TOOL
    sag_arg_query: str = DEFAULT_SAG_ARG_QUERY
    sag_arg_source: str = DEFAULT_SAG_ARG_SOURCE
    sag_arg_document: str = DEFAULT_SAG_ARG_DOCUMENT
    sag_protocol_version: str = DEFAULT_SAG_PROTOCOL_VERSION
    sag_rpc_timeout_seconds: int = DEFAULT_SAG_RPC_TIMEOUT_SECONDS
    sag_probe_query: str = DEFAULT_SAG_PROBE_QUERY
    sag_search_summary_max_bytes: int = DEFAULT_SAG_SEARCH_SUMMARY_MAX_BYTES
    sag_question_template: str = DEFAULT_SAG_QUESTION_TEMPLATE
    dws_connect_timeout: int = DEFAULT_DWS_CONNECT_TIMEOUT
    dws_probe_query: str = DEFAULT_DWS_PROBE_QUERY

    def to_json(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in (
                "host",
                "port",
                "database",
                "username",
                "sslmode",
                "query_timeout_seconds",
                "max_rows",
                "max_result_bytes",
                "max_cell_bytes",
                "planning_mode",
                "sag_endpoint",
                "sag_agent_origin",
                "sag_agent_id",
                "sag_agent_timeout_seconds",
                "sag_agent_answer_max_bytes",
                "sag_citation_limit",
                "sag_citation_snippet_max_bytes",
                "sag_planner_transcript_max_bytes",
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
        }

    @classmethod
    def from_json(cls, raw: object) -> DataQueryUserConfig:
        if not isinstance(raw, dict):
            raise DataQueryConfigError("Data-query config must be a JSON object")
        return cls(
            host=_string_field(raw, "host", default="localhost"),
            port=_int_field(raw, "port", default=35432, minimum=1, maximum=65535),
            database=_string_field(raw, "database", default="exchange_service"),
            username=_string_field(raw, "username", default=""),
            sslmode=_string_field(raw, "sslmode", default=DEFAULT_SSL_MODE),
            query_timeout_seconds=_int_field(
                raw, "query_timeout_seconds", default=DEFAULT_QUERY_TIMEOUT_SECONDS, minimum=1
            ),
            max_rows=_int_field(raw, "max_rows", default=DEFAULT_MAX_ROWS, minimum=1),
            max_result_bytes=_int_field(
                raw, "max_result_bytes", default=DEFAULT_MAX_RESULT_BYTES, minimum=1
            ),
            max_cell_bytes=_int_field(
                raw, "max_cell_bytes", default=DEFAULT_MAX_CELL_BYTES, minimum=1
            ),
            planning_mode=_planning_mode_field(raw, "planning_mode"),
            sag_endpoint=_string_field(raw, "sag_endpoint", default=""),
            sag_agent_origin=_string_field(raw, "sag_agent_origin", default=""),
            sag_agent_id=_string_field(raw, "sag_agent_id", default=""),
            sag_agent_timeout_seconds=_int_field(
                raw,
                "sag_agent_timeout_seconds",
                default=DEFAULT_SAG_AGENT_TIMEOUT_SECONDS,
                minimum=1,
                maximum=300,
            ),
            sag_agent_answer_max_bytes=_int_field(
                raw,
                "sag_agent_answer_max_bytes",
                default=DEFAULT_SAG_AGENT_ANSWER_MAX_BYTES,
                minimum=1,
                maximum=DEFAULT_SAG_AGENT_ANSWER_MAX_BYTES,
            ),
            sag_citation_limit=_int_field(
                raw,
                "sag_citation_limit",
                default=DEFAULT_SAG_CITATION_LIMIT,
                minimum=1,
                maximum=DEFAULT_SAG_CITATION_LIMIT,
            ),
            sag_citation_snippet_max_bytes=_int_field(
                raw,
                "sag_citation_snippet_max_bytes",
                default=DEFAULT_SAG_CITATION_SNIPPET_MAX_BYTES,
                minimum=1,
                maximum=DEFAULT_SAG_CITATION_SNIPPET_MAX_BYTES,
            ),
            sag_planner_transcript_max_bytes=_int_field(
                raw,
                "sag_planner_transcript_max_bytes",
                default=DEFAULT_SAG_PLANNER_TRANSCRIPT_MAX_BYTES,
                minimum=1,
                maximum=DEFAULT_SAG_PLANNER_TRANSCRIPT_MAX_BYTES,
            ),
            sag_source_id=_string_field(raw, "sag_source_id", default=DEFAULT_SAG_SOURCE_ID),
            sag_search_tool=_string_field(raw, "sag_search_tool", default=DEFAULT_SAG_SEARCH_TOOL),
            sag_read_tool=_string_field(raw, "sag_read_tool", default=DEFAULT_SAG_READ_TOOL),
            sag_arg_query=_string_field(raw, "sag_arg_query", default=DEFAULT_SAG_ARG_QUERY),
            sag_arg_source=_string_field(raw, "sag_arg_source", default=DEFAULT_SAG_ARG_SOURCE),
            sag_arg_document=_string_field(
                raw, "sag_arg_document", default=DEFAULT_SAG_ARG_DOCUMENT
            ),
            sag_protocol_version=_string_field(
                raw, "sag_protocol_version", default=DEFAULT_SAG_PROTOCOL_VERSION
            ),
            sag_rpc_timeout_seconds=_int_field(
                raw, "sag_rpc_timeout_seconds", default=DEFAULT_SAG_RPC_TIMEOUT_SECONDS, minimum=1
            ),
            sag_probe_query=_string_field(raw, "sag_probe_query", default=DEFAULT_SAG_PROBE_QUERY),
            sag_search_summary_max_bytes=_int_field(
                raw,
                "sag_search_summary_max_bytes",
                default=DEFAULT_SAG_SEARCH_SUMMARY_MAX_BYTES,
                minimum=1,
            ),
            sag_question_template=_string_field(
                raw,
                "sag_question_template",
                default=DEFAULT_SAG_QUESTION_TEMPLATE,
            ),
            dws_connect_timeout=_int_field(
                raw, "dws_connect_timeout", default=DEFAULT_DWS_CONNECT_TIMEOUT, minimum=1
            ),
            dws_probe_query=_string_field(raw, "dws_probe_query", default=DEFAULT_DWS_PROBE_QUERY),
        )


@dataclass(frozen=True, slots=True)
class DataQuerySecrets:
    """Secrets resolved from env or the credential store, never persisted."""

    dws_password: str | None = None
    sag_token: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedDataQueryConfig:
    """Effective configuration with per-field provenance for hosts."""

    host: str
    port: int
    database: str
    username: str
    sslmode: str
    query_timeout_seconds: int
    max_rows: int
    max_result_bytes: int
    max_cell_bytes: int
    planning_mode: PlanningMode
    sag_endpoint: str
    sag_agent_origin: str
    sag_agent_id: str
    sag_agent_timeout_seconds: int
    sag_agent_answer_max_bytes: int
    sag_citation_limit: int
    sag_citation_snippet_max_bytes: int
    sag_planner_transcript_max_bytes: int
    sag_source_id: str
    sag_search_tool: str
    sag_read_tool: str
    sag_arg_query: str
    sag_arg_source: str
    sag_arg_document: str
    sag_protocol_version: str
    sag_rpc_timeout_seconds: int
    sag_probe_query: str
    sag_search_summary_max_bytes: int
    sag_question_template: str
    dws_connect_timeout: int
    dws_probe_query: str
    secrets: DataQuerySecrets
    sources: Mapping[str, ConfigSource]
    secret_sources: Mapping[str, ConfigSource]

    @property
    def complete(self) -> bool:
        """Return whether all required fields for tool registration are set."""
        common = bool(
            self.host
            and self.port
            and self.database
            and self.username
            and self.secrets.dws_password
        )
        if not common or not self.secrets.sag_token:
            return False
        if self.planning_mode == "legacy":
            return bool(self.sag_endpoint and self.sag_source_id)
        if self.planning_mode == "agent":
            return bool(
                self.sag_agent_origin
                and _agent_origin_error(self.sag_agent_origin) is None
                and self.sag_agent_id
                and _AGENT_ID_PATTERN.fullmatch(self.sag_agent_id)
            )
        return False

    @property
    def configuration_diagnostics(self) -> tuple[dict[str, str], ...]:
        """Return stable mode-specific diagnostics without secret values."""
        diagnostics: list[dict[str, str]] = []
        if self.planning_mode == "unconfigured":
            diagnostics.append(
                _diagnostic(
                    "planning_mode_unconfigured",
                    "planning_mode",
                    "Choose legacy or agent planning mode.",
                )
            )
            return tuple(diagnostics)
        if not self.secrets.sag_token:
            diagnostics.append(
                _diagnostic("sag_token_missing", "sagToken", "Configure the SAG token.")
            )
        if self.planning_mode == "legacy":
            if not self.sag_endpoint:
                diagnostics.append(
                    _diagnostic(
                        "legacy_mcp_endpoint_missing",
                        "sag_endpoint",
                        "Configure the legacy SAG MCP endpoint.",
                    )
                )
            if not self.sag_source_id:
                diagnostics.append(
                    _diagnostic(
                        "legacy_source_id_missing",
                        "sag_source_id",
                        "Configure the approved legacy SAG source id.",
                    )
                )
        elif self.planning_mode == "agent":
            if not self.sag_agent_origin:
                diagnostics.append(
                    _diagnostic(
                        "agent_origin_missing",
                        "sag_agent_origin",
                        "Configure the SAG Agent API origin.",
                    )
                )
            elif (message := _agent_origin_error(self.sag_agent_origin)) is not None:
                diagnostics.append(
                    _diagnostic("agent_origin_invalid", "sag_agent_origin", message)
                )
            if not self.sag_agent_id:
                diagnostics.append(
                    _diagnostic(
                        "agent_id_missing",
                        "sag_agent_id",
                        "Configure the SAG Agent id.",
                    )
                )
            elif _AGENT_ID_PATTERN.fullmatch(self.sag_agent_id) is None:
                diagnostics.append(
                    _diagnostic(
                        "agent_id_invalid",
                        "sag_agent_id",
                        "SAG Agent id contains unsupported characters.",
                    )
                )
        return tuple(diagnostics)


def data_query_config_path(paths: TauPaths | None = None) -> Path:
    """Return the durable data-query config path."""
    return (paths or TauPaths()).home / "dataquery.json"


_BUNDLED_EXTENSION_DIR_NAME = "data_query"
_DWS_ENV_NAMES = (
    ENV_DWS_HOST,
    ENV_DWS_PORT,
    ENV_DWS_DATABASE,
    ENV_DWS_USER,
    ENV_DWS_SSL_MODE,
    ENV_DWS_TIMEOUT_SECONDS,
    ENV_DWS_MAX_ROWS,
    ENV_DWS_MAX_RESULT_BYTES,
    ENV_DWS_MAX_CELL_BYTES,
    ENV_DWS_ALLOWED_OBJECTS,
    ENV_DWS_CONNECT_TIMEOUT,
    ENV_DWS_PROBE_QUERY,
)
_SAG_ENV_NAMES = (
    ENV_SAG_ENDPOINT,
    ENV_SAG_TOKEN,
    ENV_SAG_PLANNING_MODE,
    ENV_SAG_AGENT_ORIGIN,
    ENV_SAG_AGENT_ID,
    ENV_SAG_AGENT_TIMEOUT_SECONDS,
    ENV_SAG_AGENT_ANSWER_MAX_BYTES,
    ENV_SAG_CITATION_LIMIT,
    ENV_SAG_CITATION_SNIPPET_MAX_BYTES,
    ENV_SAG_PLANNER_TRANSCRIPT_MAX_BYTES,
    ENV_SAG_SOURCE_ID,
    ENV_SAG_SEARCH_TOOL,
    ENV_SAG_READ_TOOL,
    ENV_SAG_ARG_QUERY,
    ENV_SAG_ARG_SOURCE,
    ENV_SAG_ARG_DOCUMENT,
    ENV_SAG_PROTOCOL_VERSION,
    ENV_SAG_RPC_TIMEOUT_SECONDS,
    ENV_SAG_PROBE_QUERY,
    ENV_SAG_SEARCH_SUMMARY_MAX_BYTES,
    ENV_SAG_QUESTION_TEMPLATE,
)


def dataquery_configured(
    paths: TauPaths | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return whether the user has started configuring data queries.

    Guards the bundled extension's registration (decision 20): with no config
    file and no data-query environment variables, the extension is not loaded
    at all and existing session behavior is unchanged. Once a ``dataquery.json``
    exists or a ``TAU_DWS_*``/``TAU_SAG_*`` variable is set, the extension is
    discovered and its tools are gated on the resolved config being complete.
    """
    if data_query_config_path(paths).exists():
        return True
    env_map = env if env is not None else os.environ
    return any(env_map.get(name) for name in (*_DWS_ENV_NAMES, *_SAG_ENV_NAMES))


def bundled_extension_dir() -> Path | None:
    """Return the packaged data-query extension directory, if on disk."""
    try:
        from importlib.resources import files

        path = Path(
            str(files("tau_coding").joinpath("data", "extensions", _BUNDLED_EXTENSION_DIR_NAME))
        )
    except Exception:  # noqa: BLE001 - resource lookup is best-effort
        return None
    return path if path.is_dir() else None


def dataquery_bundled_extension_path(paths: TauPaths | None = None) -> Path | None:
    """Return the bundled extension dir when data queries are being configured."""
    if not dataquery_configured(paths):
        return None
    return bundled_extension_dir()


def load_user_config(paths: TauPaths | None = None) -> DataQueryUserConfig:
    """Load the durable user config, falling back to built-in defaults."""
    path = data_query_config_path(paths)
    if not path.exists():
        return DataQueryUserConfig()
    raw = loads(path.read_text(encoding="utf-8"))
    return DataQueryUserConfig.from_json(raw)


def save_user_config(config: DataQueryUserConfig, paths: TauPaths | None = None) -> Path:
    """Write the durable user config atomically and return its path."""
    path = data_query_config_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, dumps(config.to_json(), indent=2, sort_keys=True) + "\n")
    return path


class _EnvOverrides(TypedDict):
    host: str | None
    port: int | None
    database: str | None
    username: str | None
    sslmode: str | None
    query_timeout_seconds: int | None
    max_rows: int | None
    max_result_bytes: int | None
    max_cell_bytes: int | None
    planning_mode: str | None
    sag_endpoint: str | None
    sag_agent_origin: str | None
    sag_agent_id: str | None
    sag_agent_timeout_seconds: int | None
    sag_agent_answer_max_bytes: int | None
    sag_citation_limit: int | None
    sag_citation_snippet_max_bytes: int | None
    sag_planner_transcript_max_bytes: int | None
    sag_source_id: str | None
    sag_search_tool: str | None
    sag_read_tool: str | None
    sag_arg_query: str | None
    sag_arg_source: str | None
    sag_arg_document: str | None
    sag_protocol_version: str | None
    sag_rpc_timeout_seconds: int | None
    sag_probe_query: str | None
    sag_search_summary_max_bytes: int | None
    sag_question_template: str | None
    dws_connect_timeout: int | None
    dws_probe_query: str | None


def _resolve_env_overrides(
    env: Mapping[str, str],
) -> _EnvOverrides:
    """Read validated environment overrides for the non-secret fields."""
    return {
        "host": _env_str(env, ENV_DWS_HOST),
        "port": _env_int(env, ENV_DWS_PORT, minimum=1, maximum=65535),
        "database": _env_str(env, ENV_DWS_DATABASE),
        "username": _env_str(env, ENV_DWS_USER),
        "sslmode": _env_str(env, ENV_DWS_SSL_MODE),
        "query_timeout_seconds": _env_int(env, ENV_DWS_TIMEOUT_SECONDS, minimum=1),
        "max_rows": _env_int(env, ENV_DWS_MAX_ROWS, minimum=1),
        "max_result_bytes": _env_int(env, ENV_DWS_MAX_RESULT_BYTES, minimum=1),
        "max_cell_bytes": _env_int(env, ENV_DWS_MAX_CELL_BYTES, minimum=1),
        "planning_mode": _env_str(env, ENV_SAG_PLANNING_MODE),
        "sag_endpoint": _env_str(env, ENV_SAG_ENDPOINT),
        "sag_agent_origin": _env_str(env, ENV_SAG_AGENT_ORIGIN),
        "sag_agent_id": _env_str(env, ENV_SAG_AGENT_ID),
        "sag_agent_timeout_seconds": _env_int(
            env, ENV_SAG_AGENT_TIMEOUT_SECONDS, minimum=1, maximum=300
        ),
        "sag_agent_answer_max_bytes": _env_int(
            env,
            ENV_SAG_AGENT_ANSWER_MAX_BYTES,
            minimum=1,
            maximum=DEFAULT_SAG_AGENT_ANSWER_MAX_BYTES,
        ),
        "sag_citation_limit": _env_int(
            env, ENV_SAG_CITATION_LIMIT, minimum=1, maximum=DEFAULT_SAG_CITATION_LIMIT
        ),
        "sag_citation_snippet_max_bytes": _env_int(
            env,
            ENV_SAG_CITATION_SNIPPET_MAX_BYTES,
            minimum=1,
            maximum=DEFAULT_SAG_CITATION_SNIPPET_MAX_BYTES,
        ),
        "sag_planner_transcript_max_bytes": _env_int(
            env,
            ENV_SAG_PLANNER_TRANSCRIPT_MAX_BYTES,
            minimum=1,
            maximum=DEFAULT_SAG_PLANNER_TRANSCRIPT_MAX_BYTES,
        ),
        "sag_source_id": _env_str(env, ENV_SAG_SOURCE_ID),
        "sag_search_tool": _env_str(env, ENV_SAG_SEARCH_TOOL),
        "sag_read_tool": _env_str(env, ENV_SAG_READ_TOOL),
        "sag_arg_query": _env_str(env, ENV_SAG_ARG_QUERY),
        "sag_arg_source": _env_str(env, ENV_SAG_ARG_SOURCE),
        "sag_arg_document": _env_str(env, ENV_SAG_ARG_DOCUMENT),
        "sag_protocol_version": _env_str(env, ENV_SAG_PROTOCOL_VERSION),
        "sag_rpc_timeout_seconds": _env_int(env, ENV_SAG_RPC_TIMEOUT_SECONDS, minimum=1),
        "sag_probe_query": _env_str(env, ENV_SAG_PROBE_QUERY),
        "sag_search_summary_max_bytes": _env_int(env, ENV_SAG_SEARCH_SUMMARY_MAX_BYTES, minimum=1),
        "sag_question_template": _env_str(env, ENV_SAG_QUESTION_TEMPLATE),
        "dws_connect_timeout": _env_int(env, ENV_DWS_CONNECT_TIMEOUT, minimum=1),
        "dws_probe_query": _env_str(env, ENV_DWS_PROBE_QUERY),
    }


def resolve_data_query_config(
    paths: TauPaths | None = None,
    *,
    env: Mapping[str, str] | None = None,
    credentials: FileCredentialStore | None = None,
) -> ResolvedDataQueryConfig:
    """Merge env -> user config -> defaults and resolve secrets."""
    env_map = dict(env if env is not None else os.environ)
    user = load_user_config(paths)
    store = credentials if credentials is not None else FileCredentialStore(credentials_path(paths))
    overrides = _resolve_env_overrides(env_map)

    dws_password = store.get(CREDENTIAL_DWS_PASSWORD)
    env_sag_token = _env_str(env_map, ENV_SAG_TOKEN)
    sag_token = env_sag_token or store.get(CREDENTIAL_SAG_TOKEN)

    sources: dict[str, ConfigSource] = {}
    dynamic_overrides: dict[str, object] = cast(
        dict[str, object], {**overrides}
    )
    for name in _CONFIG_FIELD_NAMES:
        sources[name] = _source_of(
            getattr(user, name),
            dynamic_overrides[name],
            _is_default_value(name, getattr(user, name)),
        )
    secret_sources: dict[str, ConfigSource] = {}
    if sag_token is not None:
        secret_sources["sag_token"] = "env" if env_sag_token is not None else "config"
    if dws_password is not None:
        secret_sources["dws_password"] = "config"

    planning_mode = _planning_mode(
        _first(overrides["planning_mode"], user.planning_mode, DEFAULT_SAG_PLANNING_MODE)
    )
    configured_sag_endpoint = _first(overrides["sag_endpoint"], user.sag_endpoint, "")
    sag_endpoint = (
        configured_sag_endpoint
        if configured_sag_endpoint
        else DEFAULT_SAG_ENDPOINT if planning_mode == "legacy" else ""
    )

    return ResolvedDataQueryConfig(
        host=_first(overrides["host"], user.host, "localhost"),
        port=_first_int(overrides["port"], user.port),
        database=_first(overrides["database"], user.database, "exchange_service"),
        username=_first(overrides["username"], user.username, ""),
        sslmode=_first(overrides["sslmode"], user.sslmode, DEFAULT_SSL_MODE),
        query_timeout_seconds=_first_int(
            overrides["query_timeout_seconds"], user.query_timeout_seconds
        ),
        max_rows=_first_int(overrides["max_rows"], user.max_rows),
        max_result_bytes=_first_int(overrides["max_result_bytes"], user.max_result_bytes),
        max_cell_bytes=_first_int(overrides["max_cell_bytes"], user.max_cell_bytes),
        planning_mode=planning_mode,
        sag_endpoint=sag_endpoint,
        sag_agent_origin=_normalize_agent_origin(
            _first(
                overrides["sag_agent_origin"],
                user.sag_agent_origin,
                DEFAULT_SAG_AGENT_ORIGIN,
            )
        ),
        sag_agent_id=_first(overrides["sag_agent_id"], user.sag_agent_id, DEFAULT_SAG_AGENT_ID),
        sag_agent_timeout_seconds=_first_int(
            overrides["sag_agent_timeout_seconds"], user.sag_agent_timeout_seconds
        ),
        sag_agent_answer_max_bytes=_first_int(
            overrides["sag_agent_answer_max_bytes"], user.sag_agent_answer_max_bytes
        ),
        sag_citation_limit=_first_int(
            overrides["sag_citation_limit"], user.sag_citation_limit
        ),
        sag_citation_snippet_max_bytes=_first_int(
            overrides["sag_citation_snippet_max_bytes"], user.sag_citation_snippet_max_bytes
        ),
        sag_planner_transcript_max_bytes=_first_int(
            overrides["sag_planner_transcript_max_bytes"], user.sag_planner_transcript_max_bytes
        ),
        sag_source_id=_first(overrides["sag_source_id"], user.sag_source_id, DEFAULT_SAG_SOURCE_ID),
        sag_search_tool=_first(
            overrides["sag_search_tool"], user.sag_search_tool, DEFAULT_SAG_SEARCH_TOOL
        ),
        sag_read_tool=_first(overrides["sag_read_tool"], user.sag_read_tool, DEFAULT_SAG_READ_TOOL),
        sag_arg_query=_first(overrides["sag_arg_query"], user.sag_arg_query, DEFAULT_SAG_ARG_QUERY),
        sag_arg_source=_first(
            overrides["sag_arg_source"], user.sag_arg_source, DEFAULT_SAG_ARG_SOURCE
        ),
        sag_arg_document=_first(
            overrides["sag_arg_document"], user.sag_arg_document, DEFAULT_SAG_ARG_DOCUMENT
        ),
        sag_protocol_version=_first(
            overrides["sag_protocol_version"],
            user.sag_protocol_version,
            DEFAULT_SAG_PROTOCOL_VERSION,
        ),
        sag_rpc_timeout_seconds=_first_int(
            overrides["sag_rpc_timeout_seconds"], user.sag_rpc_timeout_seconds
        ),
        sag_probe_query=_first(
            overrides["sag_probe_query"], user.sag_probe_query, DEFAULT_SAG_PROBE_QUERY
        ),
        sag_search_summary_max_bytes=_first_int(
            overrides["sag_search_summary_max_bytes"], user.sag_search_summary_max_bytes
        ),
        sag_question_template=_first(
            overrides["sag_question_template"],
            user.sag_question_template,
            DEFAULT_SAG_QUESTION_TEMPLATE,
        ),
        dws_connect_timeout=_first_int(overrides["dws_connect_timeout"], user.dws_connect_timeout),
        dws_probe_query=_first(
            overrides["dws_probe_query"], user.dws_probe_query, DEFAULT_DWS_PROBE_QUERY
        ),
        secrets=DataQuerySecrets(dws_password=dws_password, sag_token=sag_token),
        sources=sources,
        secret_sources=secret_sources,
    )


def config_api_payload(
    paths: TauPaths | None = None,
    *,
    env: Mapping[str, str] | None = None,
    credentials: FileCredentialStore | None = None,
) -> dict[str, object]:
    """Return the Web-facing read payload (no secret values, decision 13)."""
    resolved = resolve_data_query_config(paths, env=env, credentials=credentials)
    fields: dict[str, object] = {}
    for name in _CONFIG_FIELD_NAMES:
        fields[name] = {"value": getattr(resolved, name), "source": resolved.sources[name]}
    return {
        "fields": fields,
        "passwordConfigured": resolved.secrets.dws_password is not None,
        "sagTokenConfigured": resolved.secrets.sag_token is not None,
        "sagSourceId": resolved.sag_source_id,
        "planningMode": resolved.planning_mode,
        "configurationDiagnostics": list(resolved.configuration_diagnostics),
        "complete": resolved.complete,
    }


def apply_config_api_update(
    payload: Mapping[str, object],
    paths: TauPaths | None = None,
    *,
    env: Mapping[str, str] | None = None,
    credentials: FileCredentialStore | None = None,
) -> dict[str, object]:
    """Apply a Web-facing write payload and return the updated read payload.

    Env-controlled fields are ignored on write (hosts render them read-only).
    An empty ``password`` preserves the existing secret; a non-empty one
    replaces it. ``sagToken`` follows the same rule.
    """
    resolved = resolve_data_query_config(paths, env=env, credentials=credentials)
    user = load_user_config(paths)
    store = credentials if credentials is not None else FileCredentialStore(credentials_path(paths))

    updates: dict[str, object] = {}
    for name in _CONFIG_FIELD_NAMES:
        if name in payload and resolved.sources[name] != "env":
            updates[name] = payload.get(name)
    if updates:
        merged = replace(user, **{k: v for k, v in updates.items()})  # type: ignore[arg-type]
        user = DataQueryUserConfig.from_json(merged.to_json())
        _validate_agent_path_inputs(user)
        user = replace(
            user,
            sag_agent_origin=_normalize_agent_origin(user.sag_agent_origin),
        )
        save_user_config(user, paths)

    password = payload.get("password")
    if password is not None:
        if not isinstance(password, str):
            raise DataQueryConfigError("password must be a string")
        if password.strip():
            store.set(CREDENTIAL_DWS_PASSWORD, password)
    sag_token = payload.get("sagToken")
    if sag_token is not None:
        if not isinstance(sag_token, str):
            raise DataQueryConfigError("sagToken must be a string")
        if sag_token.strip():
            store.set(CREDENTIAL_SAG_TOKEN, sag_token)

    return config_api_payload(paths, env=env, credentials=store)


def auto_approve_execute(env: Mapping[str, str] | None = None) -> bool:
    """Return whether non-interactive hosts may auto-approve execute."""
    env_map = env if env is not None else os.environ
    value = env_map.get(ENV_DATA_AUTO_APPROVE_EXECUTE, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Small parsing helpers.
# ---------------------------------------------------------------------------


def _string_field(raw: Mapping[str, object], name: str, *, default: str) -> str:
    value = raw.get(name, default)
    if not isinstance(value, str):
        raise DataQueryConfigError(f"{name} must be a string")
    return value.strip() or default


def _planning_mode_field(raw: Mapping[str, object], name: str) -> PlanningMode:
    value = _string_field(raw, name, default=DEFAULT_SAG_PLANNING_MODE)
    return _planning_mode(value)


def _planning_mode(value: str) -> PlanningMode:
    if value not in _PLANNING_MODES:
        raise DataQueryConfigError(
            "planning_mode must be one of: unconfigured, legacy, agent"
        )
    return cast(PlanningMode, value)


def _int_field(
    raw: Mapping[str, object], name: str, *, default: int, minimum: int, maximum: int | None = None
) -> int:
    value = raw.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise DataQueryConfigError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        raise DataQueryConfigError(f"{name} is out of range")
    return value


def _env_str(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _env_int(
    env: Mapping[str, str], name: str, *, minimum: int, maximum: int | None = None
) -> int | None:
    value = _env_str(env, name)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise DataQueryConfigError(f"{name} must be an integer") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        raise DataQueryConfigError(f"{name} is out of range")
    return parsed


def _source_of(value: object, env_value: object, is_default: bool) -> ConfigSource:
    del value
    if env_value is not None:
        return "env"
    if is_default:
        return "default"
    return "config"


_DEFAULT_VALUES: dict[str, object] = {
    "host": "localhost",
    "port": 35432,
    "database": "exchange_service",
    "username": "",
    "sslmode": DEFAULT_SSL_MODE,
    "query_timeout_seconds": DEFAULT_QUERY_TIMEOUT_SECONDS,
    "max_rows": DEFAULT_MAX_ROWS,
    "max_result_bytes": DEFAULT_MAX_RESULT_BYTES,
    "max_cell_bytes": DEFAULT_MAX_CELL_BYTES,
    "planning_mode": DEFAULT_SAG_PLANNING_MODE,
    "sag_endpoint": "",
    "sag_agent_origin": DEFAULT_SAG_AGENT_ORIGIN,
    "sag_agent_id": DEFAULT_SAG_AGENT_ID,
    "sag_agent_timeout_seconds": DEFAULT_SAG_AGENT_TIMEOUT_SECONDS,
    "sag_agent_answer_max_bytes": DEFAULT_SAG_AGENT_ANSWER_MAX_BYTES,
    "sag_citation_limit": DEFAULT_SAG_CITATION_LIMIT,
    "sag_citation_snippet_max_bytes": DEFAULT_SAG_CITATION_SNIPPET_MAX_BYTES,
    "sag_planner_transcript_max_bytes": DEFAULT_SAG_PLANNER_TRANSCRIPT_MAX_BYTES,
    "sag_source_id": DEFAULT_SAG_SOURCE_ID,
    "sag_search_tool": DEFAULT_SAG_SEARCH_TOOL,
    "sag_read_tool": DEFAULT_SAG_READ_TOOL,
    "sag_arg_query": DEFAULT_SAG_ARG_QUERY,
    "sag_arg_source": DEFAULT_SAG_ARG_SOURCE,
    "sag_arg_document": DEFAULT_SAG_ARG_DOCUMENT,
    "sag_protocol_version": DEFAULT_SAG_PROTOCOL_VERSION,
    "sag_rpc_timeout_seconds": DEFAULT_SAG_RPC_TIMEOUT_SECONDS,
    "sag_probe_query": DEFAULT_SAG_PROBE_QUERY,
    "sag_search_summary_max_bytes": DEFAULT_SAG_SEARCH_SUMMARY_MAX_BYTES,
    "sag_question_template": DEFAULT_SAG_QUESTION_TEMPLATE,
    "dws_connect_timeout": DEFAULT_DWS_CONNECT_TIMEOUT,
    "dws_probe_query": DEFAULT_DWS_PROBE_QUERY,
}


def _is_default_value(name: str, value: object) -> bool:
    return value == _DEFAULT_VALUES[name]


def _agent_origin_error(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return "SAG Agent origin must use http or https."
    if not parsed.hostname or parsed.username or parsed.password:
        return "SAG Agent origin must contain only a host and optional port."
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        return "SAG Agent origin must not contain a path, query, or fragment."
    try:
        _ = parsed.port
    except ValueError:
        return "SAG Agent origin contains an invalid port."
    return None


def _normalize_agent_origin(value: str) -> str:
    if value.endswith("/") and _agent_origin_error(value) is None:
        return value[:-1]
    return value


def _validate_agent_path_inputs(config: DataQueryUserConfig) -> None:
    if config.sag_agent_origin:
        message = _agent_origin_error(config.sag_agent_origin)
        if message is not None:
            raise DataQueryConfigError(message)
    if config.sag_agent_id and _AGENT_ID_PATTERN.fullmatch(config.sag_agent_id) is None:
        raise DataQueryConfigError("SAG Agent id contains unsupported characters.")


def _diagnostic(code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message}


def _first(value: str | None, config: str, default: str) -> str:
    if value:
        return value
    if config:
        return config
    return default


def _first_int(value: int | None, config: int) -> int:
    if value is not None:
        return value
    return config


def _atomic_write_text(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
