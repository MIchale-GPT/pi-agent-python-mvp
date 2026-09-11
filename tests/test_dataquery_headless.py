"""Headless host uses the real harness, extension and durable session store."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from pi_event_helpers import assistant_done, assistant_start, text_delta, tool_call_end
from tau_agent.messages import AssistantMessage, ToolCall, ToolResultMessage, message_text
from tau_agent.provider_events import AssistantErrorEvent, AssistantMessageEvent
from tau_coding.dataquery.backends.fake import FakeKnowledgeBackend, FakeQueryBackend
from tau_coding.dataquery.headless import HeadlessQueryConfig, HeadlessQueryExecutor
from tau_coding.paths import TauPaths
from test_dataquery_extension import (
    _complete_resolved_config,
    _FailOnceQueryBackend,
    _FakeSqlPlanner,
    _RepairingFakeSqlPlanner,
)


class QueryProvider:
    def __init__(self, *, previous_evidence: str | None = None) -> None:
        self.step = 0
        self.previous_evidence = previous_evidence
        self.evidence = ""
        self.contexts = []
        self.errors = []
        self.systems = []
        self.tool_definitions = []

    def stream_response(self, *, model, system, messages, tools, signal=None):
        if signal is not None and signal.is_cancelled():

            async def aborted():
                yield AssistantErrorEvent(
                    reason="aborted", error=AssistantMessage(stop_reason="aborted")
                )

            return aborted()
        assert {tool.name for tool in tools} == {
            "data_knowledge_search",
            "data_knowledge_read",
            "data_query_prepare",
            "data_query_execute",
        }
        self.contexts.append(list(messages))
        self.systems.append(system)
        self.tool_definitions.append([(t.name, t.description, t.parameters) for t in tools])
        latest = messages[-1]
        payload = {}
        if isinstance(latest, ToolResultMessage):
            if latest.is_error:
                self.errors.append(message_text(latest))
            else:
                payload = json.loads(message_text(latest))
        self.step += 1
        assert self.step <= 10, self.errors
        if self.step == 1 and self.previous_evidence:
            name, arguments = (
                "data_query_prepare",
                {
                    "sql": "SELECT id FROM myschema.orders",
                    "params": [],
                    "evidenceIds": [self.previous_evidence],
                },
            )
        elif self.step == 1 or isinstance(latest, ToolResultMessage) and latest.is_error:
            name, arguments = "data_knowledge_search", {"question": "orders"}
            if isinstance(latest, ToolResultMessage) and latest.tool_name == "data_query_execute":
                arguments["retryContext"] = message_text(latest)
        elif isinstance(latest, ToolResultMessage) and latest.tool_name == "data_knowledge_search":
            self.evidence = payload.get("citations", payload.get("evidence"))[0]["evidenceId"]
            name, arguments = (
                "data_query_prepare",
                {
                    "sql": "SELECT id FROM myschema.orders",
                    "params": [],
                    "evidenceIds": [self.evidence],
                },
            )
            if "citations" in payload:
                arguments.update(
                    {
                        "sql": "SELECT bad_column FROM myschema.orders WHERE period = %s"
                        if not self.errors
                        else "SELECT amount FROM myschema.orders WHERE period = %s",
                        "params": ["202504"],
                        "bundleId": payload["bundleId"],
                    }
                )
        elif isinstance(latest, ToolResultMessage) and latest.tool_name == "data_query_prepare":
            name, arguments = "data_query_execute", {"planId": payload["planId"]}
        else:
            name, arguments = "", {}

        async def stream() -> AsyncIterator[AssistantMessageEvent]:
            yield assistant_start()
            if name:
                call = ToolCall(id=f"call-{self.step}", name=name, arguments=arguments)
                yield tool_call_end(call)
                yield assistant_done(AssistantMessage(content=[call], model=model))
            else:
                yield assistant_done(AssistantMessage(content="Orders inspected.", model=model))

        return stream()


@pytest.fixture
def query_config(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config", _complete_resolved_config
    )
    backends = []

    def create_backends(resolved):
        knowledge = FakeKnowledgeBackend({"orders": "myschema.orders contains id"})
        query = FakeQueryBackend()
        backends.append((knowledge, query))
        return knowledge, query, None

    monkeypatch.setattr("tau_coding.dataquery.extension._create_backends", create_backends)
    monkeypatch.setenv("TAU_DWS_ALLOWED_OBJECTS", "myschema.orders")
    # A CLI auto-approve value must never bypass the headless host gate.
    monkeypatch.setenv("TAU_DATA_AUTO_APPROVE_EXECUTE", "1")
    config = HeadlessQueryConfig(
        session_id="a" * 32,
        session_root=tmp_path / "query-sessions",
        paths=TauPaths(home=tmp_path / "home", agents_home=tmp_path / "agents"),
        provider_name="fake",
        model="fake",
    )
    return config, backends


def run(executor, question, *, decision="allow"):
    events = []
    for event in executor.prompt(question):
        events.append(event)
        if event["type"] == "tool_authorization_requested":
            assert executor.respond_tool_authorization(event["requestId"], decision)
    assert events[-1]["type"] == "run_finished"
    return events


def test_query_loop_and_fresh_process_context_restore(query_config):
    config, backends = query_config
    first = QueryProvider()
    with HeadlessQueryExecutor(config, provider=first) as executor:
        events = run(executor, "Inspect orders")
        assert events[-1]["status"] == "completed", events
        assert executor.result["answer"] == "Orders inspected."
        assert executor.result["lastResult"]["rows"] == [["1", "alice"], ["2", "bob"]]
        assert len(executor.result["executions"]) == 1
        assert len(executor.result["trace"]) == 6
        assert executor.result["error"] is None
    assert len(backends) == 1
    assert backends[0][1].statements == ["SELECT id FROM myschema.orders"]
    assert all(backend.closed for backend in backends[0])

    second = QueryProvider(previous_evidence=first.evidence)
    with HeadlessQueryExecutor(config, provider=second) as executor:
        events = run(executor, "Explain those orders")
    assert events[-1]["status"] == "completed"
    assert len(backends) == 2
    assert any("previous run" in error for error in second.errors)
    assert any(message_text(message) == "Inspect orders" for message in second.contexts[0])
    assert any(message_text(message) == "Orders inspected." for message in second.contexts[0])
    assert len(backends[1][0].search_calls) == 1
    assert len(backends[1][1].statements) == 1


def test_cancel_at_host_gate_never_executes_even_with_cli_auto_approve(query_config):
    config, backends = query_config
    with HeadlessQueryExecutor(config, provider=QueryProvider()) as executor:
        events = run(executor, "Inspect orders", decision="cancel")
    assert events[-1]["status"] == "cancelled", [e for e in events if e["type"] == "run_error"]
    assert backends[0][1].statements == []


def test_user_question_is_literal_not_a_tau_command(query_config):
    config, _ = query_config
    provider = QueryProvider()
    with HeadlessQueryExecutor(config, provider=provider) as executor:
        run(executor, "/compact explain orders")
    assert message_text(provider.contexts[0][-1]) == "/compact explain orders"


def test_sql_failure_is_repaired_in_one_service_and_planner_conversation(query_config, monkeypatch):
    config, _ = query_config
    query = _FailOnceQueryBackend()
    planner = _RepairingFakeSqlPlanner()
    created = []

    def backends(resolved):
        created.append(True)
        return FakeKnowledgeBackend(), query, planner

    monkeypatch.setattr("tau_coding.dataquery.extension._create_backends", backends)
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        lambda: _complete_resolved_config(planning_mode="agent"),
    )
    with HeadlessQueryExecutor(config, provider=QueryProvider()) as executor:
        events = run(executor, "Inspect orders")
        assert executor.result["status"] == "completed", executor.result
        assert len(executor.result["executions"]) == 1
        assert any(item["status"] == "error" for item in executor.result["trace"])
        assert "202504" not in json.dumps(executor.result["trace"])
        assert "alice" not in json.dumps(executor.result["trace"])
    assert created == [True]
    assert len(query.statements) == 2
    assert len(planner.calls) == 2
    assert len(planner.calls[0]) == 1
    assert len(planner.calls[1]) == 3
    assert "bad_column" in planner.calls[1][-1]["content"]
    assert sum(event["type"] == "tool_authorization_requested" for event in events) == 2


def test_project_extensions_and_prompt_templates_cannot_change_question(query_config):
    config, _ = query_config
    directory = config.session_root / config.session_id
    for root in (directory, directory / ".tau", config.paths.home):
        extension = root / "extensions" / "unexpected.py"
        extension.parent.mkdir(parents=True, exist_ok=True)
        extension.write_text('raise RuntimeError("untrusted extension loaded")\n')
    template = directory / "prompts" / "report.md"
    template.parent.mkdir(parents=True)
    template.write_text("Unexpected replacement\n")
    provider = QueryProvider()
    with HeadlessQueryExecutor(config, provider=provider) as executor:
        run(executor, "/report original question")
        with pytest.raises(RuntimeError, match="new headless executor"):
            list(executor.prompt("second question"))
    assert message_text(provider.contexts[0][-1]) == "/report original question"


def test_incomplete_tool_configuration_fails_closed(query_config, monkeypatch):
    config, _ = query_config
    resolved = _complete_resolved_config()
    resolved.complete = False
    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config", lambda: resolved
    )
    with (
        HeadlessQueryExecutor(config, provider=QueryProvider()) as executor,
        pytest.raises(RuntimeError, match="four configured data tools"),
    ):
        list(executor.prompt("Inspect orders"))


def test_slow_consumer_does_not_drop_events(query_config):
    config, _ = query_config
    produced = threading.Event()

    class BurstProvider:
        def stream_response(self, **kwargs):
            async def stream():
                yield assistant_start()
                for _ in range(2200):
                    yield text_delta("x")
                yield assistant_done(AssistantMessage(content="Complete answer", model="fake"))
                produced.set()

            return stream()

    with HeadlessQueryExecutor(config, provider=BurstProvider()) as executor:
        events = executor.prompt("Explain the result")
        assert next(events)["type"] == "run_started"
        assert produced.wait(10)
        retained = list(events)
        assert len([event for event in retained if event["type"] == "message_update"]) == 2200
        assert executor.result["status"] == "completed"
        assert executor.result["answer"] == "Complete answer"
        assert executor.result["lastResult"] is None


def test_agent_auto_mode_skips_host_confirmation(query_config):
    config, backends = query_config
    config = replace(config, auto_execute=True, run_id="sag-run")
    with HeadlessQueryExecutor(config, provider=QueryProvider()) as executor:
        events = list(executor.prompt("Inspect orders"))
        assert executor.result["runId"] == "sag-run"
    assert not any(e["type"] == "tool_authorization_requested" for e in events)
    assert len(backends[0][1].statements) == 1


def test_budget_tool_rejection_allows_model_summary(query_config):
    config, backends = query_config

    class SummarizingProvider(QueryProvider):
        def stream_response(self, **kwargs):
            latest = kwargs["messages"][-1]
            if isinstance(latest, ToolResultMessage) and "budget_exhausted" in message_text(latest):

                async def summary():
                    yield assistant_done(
                        AssistantMessage(content="Evidence found, execution pending.")
                    )

                return summary()
            return super().stream_response(**kwargs)

    with HeadlessQueryExecutor(
        replace(config, max_tool_calls=1), provider=SummarizingProvider()
    ) as ex:
        list(ex.prompt("Inspect orders"))
        assert ex.result["status"] == "budget_exhausted"
        assert ex.result["answer"] == "Evidence found, execution pending."
    assert not backends[0][1].statements


def test_time_budget_interrupts_model_that_ignores_token(query_config):
    config, _ = query_config

    class SlowProvider:
        def stream_response(self, **kwargs):
            async def stream():
                yield assistant_start()
                await asyncio.sleep(30)
                yield assistant_done(AssistantMessage(content="Too late"))

            return stream()

    start = time.monotonic()
    with HeadlessQueryExecutor(replace(config, max_seconds=0.1), provider=SlowProvider()) as ex:
        list(ex.prompt("Inspect orders"))
        assert ex.result["status"] == "budget_exhausted"
        assert ex.result["answer"]
    assert time.monotonic() - start < 5


def test_confirmation_wait_is_excluded_from_execution_budget(query_config):
    config, backends = query_config
    with HeadlessQueryExecutor(replace(config, max_seconds=0.3), provider=QueryProvider()) as ex:
        for event in ex.prompt("Inspect orders"):
            if event["type"] == "tool_authorization_requested":
                time.sleep(0.4)
                assert ex.respond_tool_authorization(event["requestId"], "allow")
        assert ex.result["status"] == "completed"
    assert len(backends[0][1].statements) == 1


def test_confirmation_timeout_cancels_run(query_config):
    config, backends = query_config
    with HeadlessQueryExecutor(
        replace(config, confirm_wait_seconds=0.1), provider=QueryProvider()
    ) as ex:
        list(ex.prompt("Inspect orders"))
        assert ex.result["status"] == "cancelled"
    assert not backends[0][1].statements


def test_legacy_history_is_imported_once_across_agent_restarts(query_config):
    config, _ = query_config
    config = replace(
        config,
        history=(
            {
                "id": "legacy-run",
                "question": "Company B assets",
                "answer": "Company B",
                "lastResult": None,
            },
        ),
    )
    for _ in range(2):
        provider = QueryProvider()
        with HeadlessQueryExecutor(config, provider=provider) as ex:
            run(ex, "Its liabilities?")
        imported = [m for m in provider.contexts[0] if "Company B assets" in message_text(m)]
        assert len(imported) == 1


def test_headless_and_cli_session_share_query_tools_and_sql_acceptance(query_config):
    config, backends = query_config
    from tau_coding.dataquery.config import bundled_extension_dir
    from tau_coding.session import CodingSession, CodingSessionConfig, jsonl_session_storage

    provider = QueryProvider()
    with HeadlessQueryExecutor(config, provider=provider) as ex:
        run(ex, "Inspect orders")
        assert ex.result["status"] == "completed"
    cli_provider = QueryProvider()

    async def cli_prompt():
        extension = bundled_extension_dir()
        assert extension is not None
        session = await CodingSession.load(
            CodingSessionConfig(
                provider=cli_provider,
                model="fake",
                provider_name="fake",
                cwd=config.paths.home,
                storage=jsonl_session_storage(config.paths.home / "cli.jsonl"),
                tools=[],
                skills_enabled=False,
                extensions_enabled=False,
                extension_paths=(extension,),
            )
        )
        try:
            async for _ in session.prompt("Inspect orders"):
                pass
        finally:
            await session.aclose()

    asyncio.run(cli_prompt())
    assert not provider.errors and not cli_provider.errors
    assert provider.tool_definitions[0] == cli_provider.tool_definitions[0]
    assert (
        backends[0][1].statements == backends[1][1].statements == ["SELECT id FROM myschema.orders"]
    )
    for system in [provider.systems[0], cli_provider.systems[0]]:
        assert "For business results use Chinese output aliases" in system
        assert "For the SAG query webpage" not in system


@pytest.mark.parametrize("chart_only_first", [False, True])
def test_annual_query_and_incomplete_plan_recovery_match_cli(
    query_config, monkeypatch, chart_only_first
):
    config, _ = query_config
    from tau_coding.dataquery.backends.base import QueryColumn
    from tau_coding.dataquery.config import bundled_extension_dir
    from tau_coding.session import CodingSession, CodingSessionConfig, jsonl_session_storage

    question = "京能科技 2025 年的每个月 营收情况"
    sql = (
        'SELECT company AS "公司名称", period AS "日期", amount AS "营业收入" '
        "FROM myschema.orders WHERE period BETWEEN %s AND %s ORDER BY period"
    )
    rows = [["京能科技", str(202500 + month), str(month * 100)] for month in range(1, 13)]
    created = []

    class Planner(_FakeSqlPlanner):
        async def plan(self, messages, *, signal=None):
            result = await super().plan(messages, signal=signal)
            result.answer = (
                '```json\n{"kind":"line","metrics":["营业收入"]}\n```'
                if chart_only_first and len(self.calls) == 1
                else "```sql_plan\n"
                + json.dumps({"sql": sql, "params": [202501, 202512]})
                + "\n```"
            )
            result.citations[
                0
            ].snippet = "myschema.orders: company, period, amount; monthly revenue"
            return result

    def backends(resolved):
        planner = Planner()
        query = FakeQueryBackend(
            rows, columns=[QueryColumn(n, "text") for n in ["公司名称", "日期", "营业收入"]]
        )
        created.append((planner, query))
        return FakeKnowledgeBackend(), query, planner

    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        lambda: _complete_resolved_config(planning_mode="agent"),
    )
    monkeypatch.setattr("tau_coding.dataquery.extension._create_backends", backends)

    class Provider(QueryProvider):
        def stream_response(self, *, model, system, messages, tools, signal=None):
            assert "one set-based SQL query for all requested months" in system
            self.step += 1
            assert self.step <= 5
            latest = messages[-1]
            payload = {}
            if isinstance(latest, ToolResultMessage):
                assert not latest.is_error, message_text(latest)
                payload = json.loads(message_text(latest))
            if self.step == 1 or payload.get("planningStatus") == "incomplete":
                if payload:
                    assert payload["citations"]
                    assert "protocol error" in payload["planningWarning"]
                name, args = "data_knowledge_search", {"question": question}
            elif latest.tool_name == "data_knowledge_search":
                name, args = (
                    "data_query_prepare",
                    {
                        "sql": sql,
                        "params": [202501, 202512],
                        "bundleId": payload["bundleId"],
                        "evidenceIds": [payload["citations"][0]["evidenceId"]],
                    },
                )
            elif latest.tool_name == "data_query_prepare":
                name, args = "data_query_execute", {"planId": payload["planId"]}
            else:
                assert payload["rows"] == rows
                name, args = "", {}

            async def stream():
                yield assistant_start()
                if name:
                    call = ToolCall(id=f"annual-{self.step}", name=name, arguments=args)
                    yield tool_call_end(call)
                    yield assistant_done(AssistantMessage(content=[call], model=model))
                else:
                    yield assistant_done(
                        AssistantMessage(content="已查询全年 12 个月。", model=model)
                    )

            return stream()

    with HeadlessQueryExecutor(config, provider=Provider()) as executor:
        events = run(executor, question)
        assert executor.result["lastResult"]["rows"] == rows
        assert events[-1]["status"] == "completed"

    async def cli_prompt():
        extension = bundled_extension_dir()
        assert extension is not None
        session = await CodingSession.load(
            CodingSessionConfig(
                provider=Provider(),
                model="fake",
                provider_name="fake",
                cwd=config.paths.home,
                storage=jsonl_session_storage(config.paths.home / "annual-cli.jsonl"),
                tools=[],
                skills_enabled=False,
                extensions_enabled=False,
                extension_paths=(extension,),
            )
        )
        try:
            async for _ in session.prompt(question):
                pass
        finally:
            await session.aclose()

    asyncio.run(cli_prompt())
    for planner, query in created:
        assert len(planner.calls) == 1 + int(chart_only_first)
        assert all(question in call[0]["content"] for call in planner.calls)
        assert query.statements == [sql]
        assert query.param_sets == [(202501, 202512)]


@pytest.mark.parametrize("session_id", ["../escape", "/tmp/escape", "", "a/b"])
def test_session_path_rejects_non_server_ids(tmp_path, session_id):
    with pytest.raises(ValueError, match="session_id"):
        HeadlessQueryConfig(session_id=session_id, session_root=tmp_path)
