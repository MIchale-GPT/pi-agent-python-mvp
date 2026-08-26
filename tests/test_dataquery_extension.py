"""Bundled extension registration and tool-flow tests (PRD testing decision 1/8)."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from tau_agent.events import (
    AgentEndEvent,
    AgentStartEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from tau_agent.messages import AssistantMessage, ToolCall, ToolResultMessage
from tau_coding.dataquery.backends.base import QueryColumn
from tau_coding.dataquery.backends.fake import FakeKnowledgeBackend, FakeQueryBackend
from tau_coding.dataquery.backends.sag_agent import SagAgentSqlPlanner
from tau_coding.dataquery.backends.unavailable import UnavailableKnowledgeBackend
from tau_coding.dataquery.config import (
    ENV_DATA_AUTO_APPROVE_EXECUTE,
    ENV_DWS_ALLOWED_OBJECTS,
    ENV_DWS_USER,
    bundled_extension_dir,
)
from tau_coding.dataquery.service import KnowledgeError, QuerySqlError
from tau_coding.extensions.runtime import ExtensionRuntime
from tau_coding.resources import TauResourcePaths
from tau_coding.tui.adapter import TuiEventAdapter
from tau_coding.tui.state import TuiState
from tau_coding.tui.widgets import transcript_item_selection_text

pytestmark = pytest.mark.anyio

DOCUMENTS = {
    "ev1": "# Orders table\nmyschema.orders has columns id, region, amount",
    "ev2": "# Business rules\nregion codes are east/west",
}
COLUMNS = [QueryColumn("id", "integer"), QueryColumn("region", "text")]
ROWS = [[1, "east"], [2, "west"]]


class _TrueUiBridge:
    has_ui = True

    def notify(self, message: str, level: str = "info") -> None:
        del message, level

    async def confirm(self, title: str, message: str, *, timeout: float | None = None) -> bool:
        del title, message, timeout
        return True


class _FakeSagAgentServer:
    """Deterministic HTTP Agent used by the registered-tool tracer bullet."""

    def __init__(self, *, response_mode: str = "success") -> None:
        self.requests: list[dict[str, object]] = []
        self.paths: list[str] = []
        self.authorization_headers: list[str] = []
        self.response_mode = response_mode
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_factory())
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def _handler_factory(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                server.requests.append(payload)
                server.paths.append(self.path)
                server.authorization_headers.append(
                    self.headers.get("Authorization", "")
                )
                if server.response_mode == "auth_error":
                    self._write_response(
                        401,
                        json.dumps(
                            {
                                "error": {
                                    "code": "unauthorized",
                                    "message": "provider secret must not escape",
                                }
                            }
                        ).encode("utf-8"),
                    )
                    return
                if server.response_mode == "malformed":
                    self._write_response(200, b"not-json")
                    return
                attempt = len(server.requests)
                answer = (
                    "SELECT bad_column FROM myschema.orders WHERE period = %s"
                    if attempt == 1
                    else "SELECT amount FROM myschema.orders WHERE period = %s"
                )
                response = {
                    "id": f"chat-{attempt}",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": answer},
                        }
                    ],
                    "sag": {
                        "citations": [
                            {
                                "kind": "internal",
                                "chunk_id": f"chunk-{attempt}",
                                "source_id": "finance-source",
                                "heading": "Financial SQL template",
                                "snippet": "Use the period field and requested indicator only.",
                            }
                        ]
                    },
                }
                if server.response_mode == "missing_citations":
                    response["sag"] = {"citations": []}
                body = json.dumps(response).encode("utf-8")
                self._write_response(200, body)

            def _write_response(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                del args

        return Handler

    def start(self) -> _FakeSagAgentServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


async def test_agent_backend_construction_does_not_require_mcp() -> None:
    from tau_coding.dataquery.extension import _create_backends

    resolved = _complete_resolved_config(planning_mode="agent")
    resolved.sag_endpoint = ""

    knowledge, query, planner = _create_backends(resolved)

    assert isinstance(knowledge, UnavailableKnowledgeBackend)
    assert isinstance(planner, SagAgentSqlPlanner)
    await query.close()
    await planner.close()


def _complete_resolved_config(*, planning_mode: str = "legacy") -> object:
    return SimpleNamespace(
        complete=True,
        planning_mode=planning_mode,
        host="localhost",
        port=35432,
        database="exchange_service",
        username="reader",
        sslmode="prefer",
        query_timeout_seconds=180,
        max_rows=1000,
        max_result_bytes=1024 * 1024,
        max_cell_bytes=64 * 1024,
        sag_endpoint="http://localhost:8100/mcp/",
        sag_agent_origin="http://localhost:8100",
        sag_agent_id="agent-01",
        sag_agent_timeout_seconds=60,
        sag_agent_answer_max_bytes=64 * 1024,
        sag_citation_limit=5,
        sag_citation_snippet_max_bytes=8 * 1024,
        sag_planner_transcript_max_bytes=256 * 1024,
        sag_source_id="19d09d3733c34716bcdf906738d10b03",
        sag_search_tool="search",
        sag_read_tool="read",
        sag_arg_query="query",
        sag_arg_source="sourceId",
        sag_arg_document="documentId",
        sag_protocol_version="2025-03-26",
        sag_rpc_timeout_seconds=60,
        sag_probe_query="__probe__",
        sag_search_summary_max_bytes=8 * 1024,
        sag_question_template="参考知识库模板：{question}",
        dws_connect_timeout=10,
        dws_probe_query="SELECT 1",
        secrets=SimpleNamespace(dws_password="pw", sag_token="tok"),
        sources={},
        secret_sources={},
        configuration_diagnostics=(),
    )


class _FakeSqlPlanner:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def plan(self, messages, *, signal=None):
        del signal
        self.calls.append(list(messages))
        request = messages[-1]["content"]
        return SimpleNamespace(
            answer=(
                "SELECT bpc_rs01_00470 FROM bpc_zbpc_con_s001 "
                "WHERE entity_code = 'E101426' AND period = '202504'"
            ),
            citations=(
                SimpleNamespace(
                    provider_id="chunk-company-loan",
                    source_id="finance-source",
                    title="京能技术单户资产负债表模板",
                    snippet="京能技术为单户 E101426；短期借款期末数字段 bpc_rs01_00470。",
                ),
            ),
            request=request,
            response="SAG raw response",
        )

    async def test(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _RepairingFakeSqlPlanner(_FakeSqlPlanner):
    async def plan(self, messages, *, signal=None):
        del signal
        self.calls.append(list(messages))
        attempt = len(self.calls)
        answer = (
            "SELECT bad_column FROM myschema.orders WHERE period = '202504'"
            if attempt == 1
            else "SELECT amount FROM myschema.orders WHERE period = '202504'"
        )
        return SimpleNamespace(
            answer=answer,
            citations=(
                SimpleNamespace(
                    provider_id="chunk-company-loan",
                    source_id="finance-source",
                    title="SQL template",
                    snippet="Use myschema.orders and the approved period field.",
                ),
            ),
            request=messages[-1]["content"],
            response=f"SAG raw response {attempt}",
        )


class _NonExpandableFakeSqlPlanner(_FakeSqlPlanner):
    async def plan(self, messages, *, signal=None):
        del signal
        self.calls.append(list(messages))
        return SimpleNamespace(
            answer="SELECT amount FROM myschema.orders",
            citations=(
                SimpleNamespace(
                    provider_id=None,
                    source_id="finance-source",
                    title="SAG answer citation",
                    snippet="The citation has a useful snippet but no chunk id.",
                ),
            ),
            request=messages[-1]["content"],
            response="SAG raw response",
        )


class _BlockingFakeSqlPlanner(_FakeSqlPlanner):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def plan(self, messages, *, signal=None):
        self.started.set()
        await self.release.wait()
        return await super().plan(messages, signal=signal)


class _FailOnceQueryBackend(FakeQueryBackend):
    async def execute(self, *args, **kwargs):
        if not self.statements:
            sql = args[0]
            params = args[1]
            self.statements.append(sql)
            self.param_sets.append(tuple(params))
            raise QuerySqlError('column "bad_column" does not exist')
        return await super().execute(*args, **kwargs)


def _load_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ExtensionRuntime:
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        _complete_resolved_config,
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._create_backends",
        lambda resolved: (
            FakeKnowledgeBackend(DOCUMENTS),
            FakeQueryBackend(table_rows=ROWS, columns=COLUMNS),
            None,
        ),
    )
    monkeypatch.setenv(ENV_DWS_USER, "reader")
    monkeypatch.setenv(ENV_DATA_AUTO_APPROVE_EXECUTE, "true")
    monkeypatch.setenv(ENV_DWS_ALLOWED_OBJECTS, "myschema.orders,myschema.customers")
    runtime = ExtensionRuntime(ui=_TrueUiBridge())
    runtime.load(
        TauResourcePaths(root=tmp_path / ".tau", cwd=tmp_path),
        extra_paths=_bundled_paths(),
    )
    return runtime


def _tool_names(runtime: ExtensionRuntime) -> set[str]:
    return {tool.name for tool in runtime.compose_tools([])}


async def _call_tool(
    runtime: ExtensionRuntime,
    name: str,
    arguments: dict[str, object],
    *,
    signal: object | None = None,
    tool_call_id: str = "call-1",
):
    tool = next(tool for tool in runtime.compose_tools([]) if tool.name == name)
    return await tool.execute(tool_call_id, arguments, signal, None)  # type: ignore[arg-type]


def test_bundled_extension_registers_four_tools(tmp_path: Path, monkeypatch) -> None:
    runtime = _load_runtime(tmp_path, monkeypatch)
    names = _tool_names(runtime)
    assert names == {
        "data_knowledge_search",
        "data_knowledge_read",
        "data_query_prepare",
        "data_query_execute",
    }


def test_search_guideline_avoids_fragmented_sag_round_trips(tmp_path: Path, monkeypatch) -> None:
    runtime = _load_runtime(tmp_path, monkeypatch)
    search = next(
        tool for tool in runtime.compose_tools([]) if tool.name == "data_knowledge_search"
    )
    guideline = "\n".join(search.prompt_guidelines)

    assert "Do not split" in guideline
    assert "direct answer itself is evidence" in guideline
    assert "do not call data_knowledge_read" in guideline


def test_unconfigured_extension_registers_no_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        lambda: SimpleNamespace(
            complete=False,
            host="",
            port=0,
            database="",
            username="",
            planning_mode="unconfigured",
            sag_endpoint="",
            secrets=SimpleNamespace(dws_password=None, sag_token=None),
            configuration_diagnostics=(
                {
                    "code": "planning_mode_unconfigured",
                    "field": "planning_mode",
                    "message": "Choose legacy or agent planning mode.",
                },
            ),
        ),
    )
    runtime = ExtensionRuntime()
    runtime.load(
        TauResourcePaths(root=tmp_path / ".tau", cwd=tmp_path),
        extra_paths=_bundled_paths(),
    )
    assert _tool_names(runtime) == set()
    messages = [d.message for d in runtime.diagnostics]
    assert messages == [
        "data query tools are not enabled: planning_mode_unconfigured: "
        "Choose legacy or agent planning mode."
    ]


def _bundled_paths() -> tuple[Path, ...]:
    bundled = bundled_extension_dir()
    return (bundled,) if bundled is not None else ()


def test_optional_driver_failure_is_non_fatal(tmp_path: Path, monkeypatch) -> None:
    def _boom(resolved):
        raise ImportError("psycopg missing")

    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config", _complete_resolved_config
    )
    monkeypatch.setattr("tau_coding.dataquery.extension._create_backends", _boom)
    runtime = ExtensionRuntime()
    runtime.load(
        TauResourcePaths(root=tmp_path / ".tau", cwd=tmp_path),
        extra_paths=_bundled_paths(),
    )
    assert _tool_names(runtime) == set()
    assert any("backends unavailable" in d.message for d in runtime.diagnostics)


async def test_tool_flow_search_read_prepare_execute(tmp_path: Path, monkeypatch) -> None:
    runtime = _load_runtime(tmp_path, monkeypatch)

    search = await _call_tool(runtime, "data_knowledge_search", {"question": "orders by region"})
    assert search.text.startswith("{")
    import json

    search_payload = json.loads(search.text)
    bundle_id = search_payload["bundleId"]
    evidence_id = search_payload["evidence"][0]["evidenceId"]

    read = await _call_tool(
        runtime, "data_knowledge_read", {"evidenceId": evidence_id, "bundleId": bundle_id}
    )
    read_payload = json.loads(read.text)
    assert read_payload["content"]

    prepare = await _call_tool(
        runtime,
        "data_query_prepare",
        {
            "sql": "SELECT region, COUNT(*) AS n FROM myschema.orders GROUP BY region",
            "params": [],
            "evidenceIds": [evidence_id],
            "bundleId": bundle_id,
        },
    )
    prepare_payload = json.loads(prepare.text)
    plan_id = prepare_payload["planId"]

    execute = await _call_tool(runtime, "data_query_execute", {"planId": plan_id})
    execute_payload = json.loads(execute.text)
    assert execute_payload["rowCount"] == 2
    assert execute_payload["sql"].startswith("SELECT region")
    assert "params" not in execute_payload


async def test_agent_mode_search_returns_answer_oriented_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner = _FakeSqlPlanner()
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        lambda: _complete_resolved_config(planning_mode="agent"),
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._create_backends",
        lambda resolved: (
            FakeKnowledgeBackend(DOCUMENTS),
            FakeQueryBackend(table_rows=ROWS, columns=COLUMNS),
            planner,
        ),
    )
    monkeypatch.setenv(ENV_DATA_AUTO_APPROVE_EXECUTE, "true")
    monkeypatch.setenv(ENV_DWS_ALLOWED_OBJECTS, "myschema.orders")
    runtime = ExtensionRuntime(ui=_TrueUiBridge())
    runtime.load(
        TauResourcePaths(root=tmp_path / ".tau", cwd=tmp_path),
        extra_paths=_bundled_paths(),
    )

    result = await _call_tool(
        runtime,
        "data_knowledge_search",
        {"question": "京能技术 2025年4月短期借款是多少"},
    )

    import json

    payload = json.loads(result.text)
    assert payload["mode"] == "agent"
    assert payload["attempt"] == 1
    assert payload["bundleId"].startswith("bundle_")
    assert "bpc_rs01_00470" in payload["answer"]
    assert payload["citations"] == [
        {
            "evidenceId": payload["citations"][0]["evidenceId"],
            "title": "京能技术单户资产负债表模板",
            "snippet": "京能技术为单户 E101426；短期借款期末数字段 bpc_rs01_00470。",
            "expandable": True,
        }
    ]
    assert payload["citations"][0]["evidenceId"].startswith("evidence_")
    assert planner.calls == [
        [{"role": "user", "content": "参考知识库模板：京能技术 2025年4月短期借款是多少"}]
    ]
    assert result.details == {
        "sagExchange": {
            "mode": "agent",
            "attempt": 1,
            "request": "参考知识库模板：京能技术 2025年4月短期借款是多少",
            "response": "SAG raw response",
            "citations": [
                {
                    "evidenceId": payload["citations"][0]["evidenceId"],
                    "title": "京能技术单户资产负债表模板",
                    "snippet": "京能技术为单户 E101426；短期借款期末数字段 bpc_rs01_00470。",
                    "expandable": True,
                }
            ],
        }
    }

    collapsed = runtime.render_tool_result("data_knowledge_search", result, expanded=False)
    expanded = runtime.render_tool_result("data_knowledge_search", result, expanded=True)
    assert collapsed == (
        "[green]✓[/green] SAG Agent 初始规划完成 · 第 1 轮 · 1 条引用 · Ctrl+O 展开"
    )
    assert expanded is not None
    assert "SAG Agent 初始规划 · 第 1 轮" in expanded
    assert "京能技术单户资产负债表模板" in expanded
    assert "短期借款期末数字段 bpc_rs01_00470" in expanded
    assert "Tau → SAG（实际请求）" in expanded
    assert "SAG → Tau（原始回复）" in expanded


async def test_restored_agent_exchange_keeps_collapsed_and_expanded_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner = _FakeSqlPlanner()
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        lambda: _complete_resolved_config(planning_mode="agent"),
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._create_backends",
        lambda resolved: (
            FakeKnowledgeBackend(DOCUMENTS),
            FakeQueryBackend(table_rows=ROWS, columns=COLUMNS),
            planner,
        ),
    )
    runtime = ExtensionRuntime(ui=_TrueUiBridge())
    runtime.load(
        TauResourcePaths(root=tmp_path / ".tau", cwd=tmp_path),
        extra_paths=_bundled_paths(),
    )
    question = "京能技术 2025年4月短期借款是多少"
    result = await _call_tool(
        runtime,
        "data_knowledge_search",
        {"question": question},
        tool_call_id="call-restored-agent",
    )
    state = TuiState(
        tool_call_renderer=runtime.render_tool_call,
        tool_result_renderer=runtime.render_tool_result,
    )
    state.load_messages(
        [
            AssistantMessage(
                content=[
                    ToolCall(
                        id="call-restored-agent",
                        name="data_knowledge_search",
                        arguments={"question": question},
                    )
                ]
            ),
            ToolResultMessage(
                tool_call_id="call-restored-agent",
                tool_name="data_knowledge_search",
                content=result.content,
                details=result.details,
            ),
        ]
    )

    item = state.items[-1]
    collapsed = state.resolve_tool_result(item, expanded=False)
    expanded = state.resolve_tool_result(item, expanded=True)
    assert collapsed == (
        "[green]✓[/green] SAG Agent 初始规划完成 · 第 1 轮 · 1 条引用 · Ctrl+O 展开"
    )
    assert expanded is not None
    assert "Tau → SAG（实际请求）" in expanded
    assert "SAG → Tau（原始回复）" in expanded
    assert "京能技术单户资产负债表模板" in expanded


async def test_agent_mode_repairs_failed_sql_in_the_same_planner_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tau_coding.dataquery.service import QueryExecutionError

    planner = _RepairingFakeSqlPlanner()
    query = _FailOnceQueryBackend(table_rows=ROWS, columns=COLUMNS)
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        lambda: _complete_resolved_config(planning_mode="agent"),
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._create_backends",
        lambda resolved: (FakeKnowledgeBackend(DOCUMENTS), query, planner),
    )
    monkeypatch.setenv(ENV_DATA_AUTO_APPROVE_EXECUTE, "true")
    monkeypatch.setenv(ENV_DWS_ALLOWED_OBJECTS, "myschema.orders")
    runtime = ExtensionRuntime(ui=_TrueUiBridge())
    runtime.load(
        TauResourcePaths(root=tmp_path / ".tau", cwd=tmp_path),
        extra_paths=_bundled_paths(),
    )
    question = "京能技术 2025年4月短期借款是多少"

    import json

    first = json.loads(
        (await _call_tool(runtime, "data_knowledge_search", {"question": question})).text
    )
    first_evidence = first["citations"][0]["evidenceId"]
    first_plan = json.loads(
        (
            await _call_tool(
                runtime,
                "data_query_prepare",
                {
                    "sql": "SELECT bad_column FROM myschema.orders WHERE period = %s",
                    "params": ["202504"],
                    "evidenceIds": [first_evidence],
                    "bundleId": first["bundleId"],
                },
            )
        ).text
    )
    with pytest.raises(QueryExecutionError) as exc_info:
        await _call_tool(runtime, "data_query_execute", {"planId": first_plan["planId"]})

    repaired_result = await _call_tool(
        runtime,
        "data_knowledge_search",
        {"question": question, "retryContext": str(exc_info.value)},
    )
    repaired = json.loads(repaired_result.text)

    assert repaired["mode"] == "agent"
    assert repaired["attempt"] == 2
    assert "SAG Agent 修正完成 · 第 2 轮" in str(
        runtime.render_tool_result(
            "data_knowledge_search", repaired_result, expanded=False
        )
    )
    assert "amount" in repaired["answer"]
    assert len(planner.calls) == 2
    assert planner.calls[1][0] == planner.calls[0][0]
    assert planner.calls[1][1]["role"] == "assistant"
    assert "bad_column" in planner.calls[1][1]["content"]
    assert planner.calls[1][2]["role"] == "user"
    assert "SELECT bad_column FROM myschema.orders WHERE period = %s" in planner.calls[1][2][
        "content"
    ]
    assert 'column "bad_column" does not exist' in planner.calls[1][2]["content"]
    assert "202504" not in planner.calls[1][2]["content"]

    repaired_evidence = repaired["citations"][0]["evidenceId"]
    repaired_plan = json.loads(
        (
            await _call_tool(
                runtime,
                "data_query_prepare",
                {
                    "sql": "SELECT amount FROM myschema.orders WHERE period = %s",
                    "params": ["202504"],
                    "evidenceIds": [repaired_evidence],
                    "bundleId": repaired["bundleId"],
                },
            )
        ).text
    )
    executed = json.loads(
        (
            await _call_tool(
                runtime,
                "data_query_execute",
                {"planId": repaired_plan["planId"]},
            )
        ).text
    )
    assert executed["rowCount"] == 2


async def test_registered_tools_complete_http_agent_failure_repair_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD primary seam: registered tools, real HTTP adapter and fake DWS."""
    from tau_coding.dataquery.service import QueryExecutionError

    server = _FakeSagAgentServer().start()
    planner = SagAgentSqlPlanner(
        origin=f"http://127.0.0.1:{server.port}",
        agent_id="trusted-agent",
        token="secret-token",
    )
    query = _FailOnceQueryBackend(table_rows=ROWS, columns=COLUMNS)
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        lambda: _complete_resolved_config(planning_mode="agent"),
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._create_backends",
        lambda resolved: (FakeKnowledgeBackend(DOCUMENTS), query, planner),
    )
    monkeypatch.setenv(ENV_DATA_AUTO_APPROVE_EXECUTE, "true")
    monkeypatch.setenv(ENV_DWS_ALLOWED_OBJECTS, "myschema.orders")
    runtime = ExtensionRuntime(ui=_TrueUiBridge())
    runtime.load(
        TauResourcePaths(root=tmp_path / ".tau", cwd=tmp_path),
        extra_paths=_bundled_paths(),
    )
    question = "京能技术 2025年4月短期借款是多少"

    try:
        first_result = await _call_tool(
            runtime,
            "data_knowledge_search",
            {"question": question},
            tool_call_id="http-plan-1",
        )
        first = json.loads(first_result.text)
        first_plan = json.loads(
            (
                await _call_tool(
                    runtime,
                    "data_query_prepare",
                    {
                        "sql": "SELECT bad_column FROM myschema.orders WHERE period = %s",
                        "params": ["202504"],
                        "evidenceIds": [first["citations"][0]["evidenceId"]],
                        "bundleId": first["bundleId"],
                    },
                )
            ).text
        )
        with pytest.raises(QueryExecutionError) as exc_info:
            await _call_tool(
                runtime,
                "data_query_execute",
                {"planId": first_plan["planId"]},
            )

        repaired_result = await _call_tool(
            runtime,
            "data_knowledge_search",
            {"question": question, "retryContext": str(exc_info.value)},
            tool_call_id="http-plan-2",
        )
        repaired = json.loads(repaired_result.text)
        repaired_plan = json.loads(
            (
                await _call_tool(
                    runtime,
                    "data_query_prepare",
                    {
                        "sql": "SELECT amount FROM myschema.orders WHERE period = %s",
                        "params": ["202504"],
                        "evidenceIds": [repaired["citations"][0]["evidenceId"]],
                        "bundleId": repaired["bundleId"],
                    },
                )
            ).text
        )
        executed = json.loads(
            (
                await _call_tool(
                    runtime,
                    "data_query_execute",
                    {"planId": repaired_plan["planId"]},
                )
            ).text
        )
    finally:
        await planner.close()
        server.stop()

    assert executed["rowCount"] == 2
    assert server.paths == [
        "/api/v1/openai/trusted-agent/chat/completions",
        "/api/v1/openai/trusted-agent/chat/completions",
    ]
    assert server.authorization_headers == ["Bearer secret-token", "Bearer secret-token"]
    assert len(server.requests) == 2
    assert server.requests[0]["stream"] is False
    first_messages = server.requests[0]["messages"]
    second_messages = server.requests[1]["messages"]
    assert isinstance(first_messages, list) and len(first_messages) == 1
    assert isinstance(second_messages, list) and len(second_messages) == 3
    assert second_messages[0] == first_messages[0]
    assert second_messages[1]["role"] == "assistant"
    assert "bad_column" in second_messages[1]["content"]
    assert second_messages[2]["role"] == "user"
    assert "SELECT bad_column FROM myschema.orders WHERE period = %s" in second_messages[2][
        "content"
    ]
    assert "202504" not in json.dumps(server.requests, ensure_ascii=False)
    assert "202504" not in json.dumps(repaired_result.details, ensure_ascii=False)
    assert "secret-token" not in json.dumps(repaired_result.details, ensure_ascii=False)


@pytest.mark.parametrize(
    ("response_mode", "message"),
    [
        ("auth_error", r"HTTP 401 \(unauthorized\)"),
        ("malformed", "returned invalid JSON"),
        ("missing_citations", "missing usable citations"),
    ],
)
async def test_registered_search_sanitizes_agent_protocol_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response_mode: str,
    message: str,
) -> None:
    """PRD primary seam: protocol failures never issue an evidence bundle."""
    server = _FakeSagAgentServer(response_mode=response_mode).start()
    planner = SagAgentSqlPlanner(
        origin=f"http://127.0.0.1:{server.port}",
        agent_id="trusted-agent",
        token="secret-token",
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        lambda: _complete_resolved_config(planning_mode="agent"),
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._create_backends",
        lambda resolved: (
            FakeKnowledgeBackend(DOCUMENTS),
            FakeQueryBackend(table_rows=ROWS, columns=COLUMNS),
            planner,
        ),
    )
    runtime = ExtensionRuntime(ui=_TrueUiBridge())
    runtime.load(
        TauResourcePaths(root=tmp_path / ".tau", cwd=tmp_path),
        extra_paths=_bundled_paths(),
    )

    try:
        with pytest.raises(KnowledgeError, match=message) as exc_info:
            await _call_tool(
                runtime,
                "data_knowledge_search",
                {"question": "京能技术短期借款"},
            )
    finally:
        await planner.close()
        server.stop()

    assert "provider secret" not in str(exc_info.value)
    assert "secret-token" not in str(exc_info.value)
    assert server.paths == [
        "/api/v1/openai/trusted-agent/chat/completions"
    ]


async def test_cancelled_query_does_not_open_an_agent_correction_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tau_coding.dataquery.service import DataQueryValidationError, QueryExecutionError

    class _Cancelled:
        def is_cancelled(self) -> bool:
            return True

    planner = _FakeSqlPlanner()
    query = FakeQueryBackend(table_rows=ROWS, columns=COLUMNS)
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        lambda: _complete_resolved_config(planning_mode="agent"),
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._create_backends",
        lambda resolved: (FakeKnowledgeBackend(DOCUMENTS), query, planner),
    )
    monkeypatch.setenv(ENV_DATA_AUTO_APPROVE_EXECUTE, "true")
    monkeypatch.setenv(ENV_DWS_ALLOWED_OBJECTS, "myschema.orders")
    runtime = ExtensionRuntime(ui=_TrueUiBridge())
    runtime.load(
        TauResourcePaths(root=tmp_path / ".tau", cwd=tmp_path),
        extra_paths=_bundled_paths(),
    )

    import json

    question = "京能技术 2025年4月短期借款是多少"
    search = json.loads(
        (await _call_tool(runtime, "data_knowledge_search", {"question": question})).text
    )
    plan = json.loads(
        (
            await _call_tool(
                runtime,
                "data_query_prepare",
                {
                    "sql": "SELECT amount FROM myschema.orders WHERE period = %s",
                    "params": ["202504"],
                    "evidenceIds": [search["citations"][0]["evidenceId"]],
                    "bundleId": search["bundleId"],
                },
            )
        ).text
    )
    with pytest.raises(QueryExecutionError) as exc_info:
        await _call_tool(
            runtime,
            "data_query_execute",
            {"planId": plan["planId"]},
            signal=_Cancelled(),
        )

    assert "retryContext" not in str(exc_info.value)
    with pytest.raises(DataQueryValidationError, match="requires a failed"):
        await _call_tool(
            runtime,
            "data_knowledge_search",
            {"question": question, "retryContext": "query cancelled"},
        )
    assert len(planner.calls) == 1


async def test_agent_citation_without_chunk_id_is_visible_but_not_expandable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tau_coding.dataquery.service import DataQueryValidationError

    planner = _NonExpandableFakeSqlPlanner()
    knowledge = FakeKnowledgeBackend(DOCUMENTS)
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        lambda: _complete_resolved_config(planning_mode="agent"),
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._create_backends",
        lambda resolved: (
            knowledge,
            FakeQueryBackend(table_rows=ROWS, columns=COLUMNS),
            planner,
        ),
    )
    monkeypatch.setenv(ENV_DWS_ALLOWED_OBJECTS, "myschema.orders")
    runtime = ExtensionRuntime(ui=_TrueUiBridge())
    runtime.load(
        TauResourcePaths(root=tmp_path / ".tau", cwd=tmp_path),
        extra_paths=_bundled_paths(),
    )

    import json

    search = json.loads(
        (
            await _call_tool(
                runtime,
                "data_knowledge_search",
                {"question": "京能技术短期借款"},
            )
        ).text
    )
    citation = search["citations"][0]
    assert citation["expandable"] is False
    assert "useful snippet" in citation["snippet"]
    with pytest.raises(DataQueryValidationError, match="citation_expansion_unavailable"):
        await _call_tool(
            runtime,
            "data_knowledge_read",
            {"bundleId": search["bundleId"], "evidenceId": citation["evidenceId"]},
        )
    assert knowledge.read_calls == []


async def test_concurrent_agent_planner_turns_cannot_interleave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tau_coding.dataquery.service import DataQueryValidationError

    planner = _BlockingFakeSqlPlanner()
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        lambda: _complete_resolved_config(planning_mode="agent"),
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._create_backends",
        lambda resolved: (
            FakeKnowledgeBackend(DOCUMENTS),
            FakeQueryBackend(table_rows=ROWS, columns=COLUMNS),
            planner,
        ),
    )
    runtime = ExtensionRuntime(ui=_TrueUiBridge())
    runtime.load(
        TauResourcePaths(root=tmp_path / ".tau", cwd=tmp_path),
        extra_paths=_bundled_paths(),
    )

    first = asyncio.create_task(
        _call_tool(
            runtime,
            "data_knowledge_search",
            {"question": "first question"},
            tool_call_id="call-first",
        )
    )
    await planner.started.wait()
    second = asyncio.create_task(
        _call_tool(
            runtime,
            "data_knowledge_search",
            {"question": "second question"},
            tool_call_id="call-second",
        )
    )
    await asyncio.sleep(0)
    planner.release.set()

    assert (await first).text
    with pytest.raises(DataQueryValidationError, match="already exists"):
        await second
    assert len(planner.calls) == 1


async def test_search_tool_keeps_sag_exchange_in_display_only_details(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _load_runtime(tmp_path, monkeypatch)

    result = await _call_tool(
        runtime,
        "data_knowledge_search",
        {"question": "orders by region"},
    )

    assert isinstance(result.details, dict)
    exchange = result.details["sagExchange"]
    assert isinstance(exchange, dict)
    assert exchange["request"] == "orders by region"
    assert "myschema.orders" in str(exchange["response"])
    assert "sagExchange" not in result.text


async def test_search_tool_renders_collapsed_summary_and_expanded_sag_exchange(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _load_runtime(tmp_path, monkeypatch)
    result = await _call_tool(
        runtime,
        "data_knowledge_search",
        {"question": "orders by region"},
    )

    collapsed = runtime.render_tool_result("data_knowledge_search", result, expanded=False)
    expanded = runtime.render_tool_result("data_knowledge_search", result, expanded=True)

    assert collapsed is not None
    assert "SAG 问答完成" in collapsed
    assert "Ctrl+O" in collapsed
    assert "myschema.orders" not in collapsed
    assert expanded is not None
    assert "Tau → SAG（实际请求）" in expanded
    assert "orders by region" in expanded
    assert "SAG → Tau（原始回复）" in expanded
    assert "myschema.orders" in expanded


async def test_tui_projection_expands_the_real_sag_exchange(tmp_path: Path, monkeypatch) -> None:
    runtime = _load_runtime(tmp_path, monkeypatch)
    result = await _call_tool(
        runtime,
        "data_knowledge_search",
        {"question": "orders by region"},
    )
    state = TuiState(
        tool_call_renderer=runtime.render_tool_call,
        tool_result_renderer=runtime.render_tool_result,
    )
    adapter = TuiEventAdapter(state)
    adapter.apply(
        ToolExecutionStartEvent(
            tool_call_id="call-1",
            tool_name="data_knowledge_search",
            args={"question": "orders by region"},
        )
    )
    adapter.apply(
        ToolExecutionEndEvent(
            tool_call_id="call-1",
            tool_name="data_knowledge_search",
            result=result,
            is_error=False,
        )
    )

    item = state.items[-1]
    invocation = state.resolve_tool_invocation(item)
    collapsed = transcript_item_selection_text(
        item,
        invocation=invocation,
        result_markup=state.resolve_tool_result(item, expanded=False),
    )
    expanded = transcript_item_selection_text(
        item,
        show_tool_results=True,
        invocation=invocation,
        result_markup=state.resolve_tool_result(item, expanded=True),
    )

    assert "SAG knowledge: orders by region" in collapsed
    assert "SAG 问答完成 · Ctrl+O 查看真实交互" in collapsed
    assert "myschema.orders" not in collapsed
    assert "Tau → SAG（实际请求）" in expanded
    assert "SAG → Tau（原始回复）" in expanded
    assert "myschema.orders" in expanded


async def test_execute_without_plan_fails_cleanly(tmp_path: Path, monkeypatch) -> None:
    from tau_coding.dataquery.service import DataQueryValidationError

    runtime = _load_runtime(tmp_path, monkeypatch)
    with pytest.raises(DataQueryValidationError, match="unknown"):
        await _call_tool(runtime, "data_query_execute", {"planId": "plan_bogus"})


async def test_agent_start_scopes_ledger(tmp_path: Path, monkeypatch) -> None:
    runtime = _load_runtime(tmp_path, monkeypatch)
    await runtime.emit_event(AgentStartEvent())
    search = await _call_tool(runtime, "data_knowledge_search", {"question": "orders"})
    import json

    plan = await _call_tool(
        runtime,
        "data_query_prepare",
        {
            "sql": "SELECT * FROM myschema.orders WHERE region = %s",
            "params": ["east"],
            "evidenceIds": [json.loads(search.text)["evidence"][0]["evidenceId"]],
        },
    )
    plan_id = json.loads(plan.text)["planId"]

    await runtime.emit_event(AgentEndEvent())
    await runtime.emit_event(AgentStartEvent())
    from tau_coding.dataquery.service import DataQueryValidationError

    with pytest.raises(DataQueryValidationError, match="previous run"):
        await _call_tool(runtime, "data_query_execute", {"planId": plan_id})


def test_prepare_render_call_hides_parameter_values() -> None:
    from tau_coding.dataquery.extension import _prepare_render_call

    rendered = _prepare_render_call({"params": ["secret-value", 42], "evidenceIds": ["e1"]})
    assert rendered == "data_query_prepare: 2 params, 1 evidence items"
    assert "secret-value" not in (rendered or "")


# -- session integration (decision 20 gating) --------------------------------


async def _session_tool_names(tmp_path: Path, resource_root: Path) -> set[str]:
    from tau_agent.session import JsonlSessionStorage
    from tau_ai.fake import FakeProvider
    from tau_coding.resources import TauResourcePaths
    from tau_coding.session import CodingSession, CodingSessionConfig

    session = await CodingSession.load(
        CodingSessionConfig(
            provider=FakeProvider([]),
            model="fake",
            system="You are Tau.",
            storage=JsonlSessionStorage(tmp_path / "session.jsonl"),
            cwd=tmp_path,
            resource_paths=TauResourcePaths(root=resource_root, agents_root=None),
        )
    )
    try:
        return {tool.name for tool in session.tools}
    finally:
        await session.aclose()


async def test_session_without_dataquery_config_has_no_data_tools(
    tmp_path: Path,
) -> None:
    names = await _session_tool_names(tmp_path, tmp_path / ".tau")
    assert "data_query_execute" not in names


async def test_session_with_dataquery_config_loads_bundled_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_root = tmp_path / ".tau"
    resource_root.mkdir(parents=True)
    (resource_root / "dataquery.json").write_text('{"username": "reader"}', encoding="utf-8")
    monkeypatch.setenv("TAU_SAG_TOKEN", "tok")
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        _complete_resolved_config,
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._create_backends",
        lambda resolved: (
            FakeKnowledgeBackend(DOCUMENTS),
            FakeQueryBackend(table_rows=ROWS, columns=COLUMNS),
            None,
        ),
    )

    names = await _session_tool_names(tmp_path, resource_root)
    assert {
        "data_knowledge_search",
        "data_knowledge_read",
        "data_query_prepare",
        "data_query_execute",
    } <= names


async def test_session_reload_registers_data_tools_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tau_agent.session import JsonlSessionStorage
    from tau_ai.fake import FakeProvider
    from tau_coding.resources import TauResourcePaths
    from tau_coding.session import CodingSession, CodingSessionConfig

    resource_root = tmp_path / ".tau"
    resource_root.mkdir(parents=True)
    (resource_root / "dataquery.json").write_text('{"username": "reader"}', encoding="utf-8")
    monkeypatch.setenv("TAU_SAG_TOKEN", "tok")
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        _complete_resolved_config,
    )
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._create_backends",
        lambda resolved: (
            FakeKnowledgeBackend(DOCUMENTS),
            FakeQueryBackend(table_rows=ROWS, columns=COLUMNS),
            None,
        ),
    )

    session = await CodingSession.load(
        CodingSessionConfig(
            provider=FakeProvider([]),
            model="fake",
            system="You are Tau.",
            storage=JsonlSessionStorage(tmp_path / "session.jsonl"),
            cwd=tmp_path,
            resource_paths=TauResourcePaths(root=resource_root, agents_root=None),
        )
    )
    try:
        names = {tool.name for tool in session.tools}
        assert "data_query_execute" in names
        await session.reload()
        names_after_reload = {tool.name for tool in session.tools}
        assert "data_query_execute" in names_after_reload
    finally:
        await session.aclose()
