"""Exercise trusted evidence restoration through the actual headless session host."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from pi_event_helpers import assistant_done, assistant_start, tool_call_end
from tau_agent.messages import AssistantMessage, ToolCall, ToolResultMessage, message_text
from tau_coding.dataquery.backends.fake import FakeKnowledgeBackend, FakeQueryBackend
from tau_coding.dataquery.headless import HeadlessQueryExecutor
from test_dataquery_extension import _complete_resolved_config, _FakeSqlPlanner
from test_dataquery_headless import query_config as query_config
from test_dataquery_headless import run

BASE_SQL = (
    "SELECT period, amount FROM myschema.orders WHERE entity = %s AND period = %s ORDER BY period"
)
YEAR_SQL = (
    "SELECT period, amount FROM myschema.orders "
    "WHERE entity = %s AND period BETWEEN %s AND %s ORDER BY period"
)


class ReuseProvider:
    def __init__(
        self,
        sql: str | None = None,
        params: list[object] | None = None,
        *,
        search_first: bool = False,
        new_evidence_reason: str | None = None,
    ):
        self.sql = sql
        self.params = params or []
        self.step = 0
        self.calls: list[str] = []
        self.errors: list[str] = []
        self.restored: list[ToolResultMessage] = []
        self.plan_ids: list[str] = []
        self.search_first = search_first
        self.new_evidence_reason = new_evidence_reason
        self.search_results: list[dict] = []

    def stream_response(self, *, model, system, messages, tools, signal=None):
        del system, signal
        if not tools:

            async def title():
                yield assistant_done(AssistantMessage(content="Revenue session", model=model))

            return title()
        self.step += 1
        assert self.step <= 5
        latest = messages[-1]
        payload = {}
        if isinstance(latest, ToolResultMessage):
            if latest.is_error:
                self.errors.append(message_text(latest))
            else:
                payload = json.loads(message_text(latest))
        name, arguments = "", {}
        if self.step == 1:
            if self.sql is None or self.search_first:
                name, arguments = "data_knowledge_search", {"question": "May revenue"}
                if self.new_evidence_reason is not None:
                    arguments["newEvidenceReason"] = self.new_evidence_reason
            else:
                self.restored = [
                    message
                    for message in messages
                    if isinstance(message, ToolResultMessage)
                    and message.tool_name == "data_query_execute"
                    and not message.is_error
                    and isinstance(message.details, dict)
                    and "reuseEvidence" in message.details
                ]
                assert self.restored, "Successful host-owned snapshot was not persisted"
                saved = json.loads(message_text(self.restored[-1]))
                assert "reuseEvidence" not in saved, "Private snapshot leaked to model payload"
                name, arguments = (
                    "data_query_prepare",
                    {
                        "reusePlanId": saved["reusablePlanId"],
                        "sql": self.sql,
                        "params": self.params,
                    },
                )
        elif not self.errors and isinstance(latest, ToolResultMessage):
            if latest.tool_name == "data_knowledge_search":
                self.search_results.append(payload)
                name = "data_query_prepare"
                if payload.get("planningStatus") == "reuse_available":
                    arguments = {
                        "reusePlanId": payload["plans"][-1]["reusePlanId"],
                        "sql": self.sql,
                        "params": self.params,
                    }
                else:
                    arguments = {
                        "usePlannedSql": True,
                        "bundleId": payload["bundleId"],
                        "evidenceIds": [payload["citations"][0]["evidenceId"]],
                    }
            elif latest.tool_name == "data_query_prepare":
                self.plan_ids.append(payload["planId"])
                name, arguments = "data_query_execute", {"planId": payload["planId"]}
        if name:
            self.calls.append(name)

        async def stream():
            yield assistant_start()
            if name:
                call = ToolCall(id=f"reuse-{self.step}", name=name, arguments=arguments)
                yield tool_call_end(call)
                yield assistant_done(AssistantMessage(content=[call], model=model))
            else:
                yield assistant_done(AssistantMessage(content="Evidence checked.", model=model))

        return stream()


@pytest.fixture
def session_reuse(query_config, monkeypatch):
    config, _ = query_config
    created = []

    class Planner(_FakeSqlPlanner):
        async def plan(self, messages, *, signal=None):
            result = await super().plan(messages, signal=signal)
            result.answer = (
                "```sql_plan\n"
                + json.dumps({"sql": BASE_SQL, "params": ["group", "202505"]})
                + "\n```"
            )
            result.citations[0].snippet = "myschema.orders: entity, period, amount"
            return result

    def backends(resolved):
        knowledge, query, planner = FakeKnowledgeBackend(), FakeQueryBackend(), Planner()
        created.append((knowledge, query, planner))
        return knowledge, query, planner

    monkeypatch.setattr(
        "tau_coding.dataquery.extension._resolve_extension_config",
        lambda: _complete_resolved_config(planning_mode="agent"),
    )
    monkeypatch.setattr("tau_coding.dataquery.extension._create_backends", backends)
    return config, created


def test_fresh_executors_reuse_session_sql_without_sag_and_confirm_each_run(session_reuse):
    config, created = session_reuse
    providers = [
        ReuseProvider(),
        ReuseProvider(YEAR_SQL, ["group", "202501", "202512"]),
        ReuseProvider(
            "SELECT period, amount FROM myschema.orders "
            "WHERE entity = %s AND period = %s AND amount IS NULL",
            ["group", "202503"],
        ),
    ]
    requests = []
    for i, provider in enumerate(providers):
        with HeadlessQueryExecutor(replace(config, run_id=f"run-{i}"), provider=provider) as ex:
            events = run(ex, ["May revenue", "Annual monthly revenue", "Inspect March NULL"][i])
            assert events[-1]["status"] == "completed", ex.result
            assert not provider.errors
            assert len(ex.result["executions"]) == 1, (i, provider.calls, ex.result)
        confirmations = [e for e in events if e["type"] == "tool_authorization_requested"]
        assert len(confirmations) == 1
        requests.append(confirmations[0]["requestId"])
    assert len(set(requests)) == 3
    assert len({p.plan_ids[0] for p in providers}) == 3
    assert [len(planner.calls) for _, _, planner in created] == [1, 0, 0]
    assert [len(query.statements) for _, query, _ in created] == [1, 1, 1]
    assert all(not knowledge.search_calls for knowledge, _, _ in created)
    assert providers[1].calls == providers[2].calls == ["data_query_prepare", "data_query_execute"]
    assert len(providers[2].restored) == 2


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT invented FROM myschema.orders",
        "SELECT amount FROM myschema.orders WHERE invented = 1",
        "SELECT amount FROM myschema.orders WHERE EXISTS "
        "(SELECT 1 FROM myschema.orders AS nested WHERE nested.invented = 1)",
        "SELECT a.amount FROM myschema.orders a JOIN myschema.orders b ON a.invented = b.entity",
    ],
)
def test_reuse_unknown_fields_never_reach_confirmation_or_database(session_reuse, sql):
    config, created = session_reuse
    with HeadlessQueryExecutor(config, provider=ReuseProvider()) as ex:
        run(ex, "May revenue")
    provider = ReuseProvider(sql)
    with HeadlessQueryExecutor(config, provider=provider) as ex:
        events = run(ex, "Follow-up using an unevidenced field")
    assert len(provider.errors) == 1
    assert "Follow-up evidence rejected" in provider.errors[0]
    assert not any(e["type"] == "tool_authorization_requested" for e in events)
    assert not created[1][1].statements
    assert not created[1][2].calls


def test_prior_allow_does_not_authorize_reused_plan(session_reuse):
    config, created = session_reuse
    with HeadlessQueryExecutor(config, provider=ReuseProvider()) as ex:
        run(ex, "May revenue")
    provider = ReuseProvider(YEAR_SQL, ["group", "202501", "202512"])
    with HeadlessQueryExecutor(config, provider=provider) as ex:
        events = run(ex, "Annual revenue", decision="cancel")
    assert events[-1]["status"] == "cancelled"
    assert sum(e["type"] == "tool_authorization_requested" for e in events) == 1
    assert not created[1][1].statements
    assert not created[1][2].calls


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_followup_search_returns_local_plans_then_reuses_without_sag(session_reuse, reason):
    config, created = session_reuse
    with HeadlessQueryExecutor(config, provider=ReuseProvider()) as ex:
        run(ex, "May revenue")
    provider = ReuseProvider(
        YEAR_SQL,
        ["group", "202501", "202512"],
        search_first=True,
        new_evidence_reason=reason,
    )
    with HeadlessQueryExecutor(config, provider=provider) as ex:
        events = run(ex, "Annual monthly revenue")
        assert events[-1]["status"] == "completed", ex.result
        assert len(ex.result["executions"]) == 1
    assert not provider.errors
    assert len(provider.search_results) == 1
    available = provider.search_results[0]
    assert available["mode"] == "session"
    assert available["planningStatus"] == "reuse_available"
    assert len(available["plans"]) == 1
    assert available["plans"][0]["sql"] == BASE_SQL
    assert available["plans"][0]["params"] == ["group", "202505"]
    assert not created[1][0].search_calls
    assert not created[1][2].calls
    assert created[1][1].statements == [YEAR_SQL]
    assert sum(e["type"] == "tool_authorization_requested" for e in events) == 1


def test_new_evidence_reason_allows_sag_even_when_prior_plan_is_available(session_reuse):
    config, created = session_reuse
    with HeadlessQueryExecutor(config, provider=ReuseProvider()) as ex:
        run(ex, "May revenue")
    provider = ReuseProvider(
        search_first=True,
        new_evidence_reason="Need the separately evidenced profit metric",
    )
    with HeadlessQueryExecutor(config, provider=provider) as ex:
        events = run(ex, "Find the additional metric")
        assert events[-1]["status"] == "completed", ex.result
        assert len(ex.result["executions"]) == 1
    assert not provider.errors
    assert len(created[1][2].calls) == 1
    assert provider.search_results[0]["mode"] == "agent"
    assert "plans" not in provider.search_results[0]
    assert sum(e["type"] == "tool_authorization_requested" for e in events) == 1
