"""Workflow and state-gating tests (PRD testing decision 1)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tau_coding.dataquery.backends.base import QueryColumn
from tau_coding.dataquery.backends.fake import FakeKnowledgeBackend, FakeQueryBackend
from tau_coding.dataquery.backends.unavailable import UnavailableKnowledgeBackend
from tau_coding.dataquery.config import DataQuerySecrets
from tau_coding.dataquery.ledger import LedgerError
from tau_coding.dataquery.policy import AllowedObjects, SqlPolicyChecker
from tau_coding.dataquery.service import (
    DataQueryValidationError,
    DataQuestionService,
    KnowledgeError,
    QueryExecutionError,
    QueryInfrastructureError,
    QueryLimits,
    QuerySqlError,
)

pytestmark = pytest.mark.anyio

DOCUMENTS = {
    "ev1": (
        "# Orders table\nmyschema.orders has columns id, customer_id, region, amount, created_at"
    ),
    "ev2": "# Business rules\nregion codes are east/west",
    "ev3": "# Customers table\nmyschema.customers has columns id, name",
}
COLUMNS = [
    QueryColumn("id", "integer"),
    QueryColumn("region", "text"),
    QueryColumn("amount", "numeric"),
]
ROWS = [[1, "east", 100], [2, "west", 200], [3, "east", 300]]

ALLOWED = AllowedObjects.parse(["myschema.orders", "myschema.customers"])


def build_service(
    *,
    knowledge: FakeKnowledgeBackend | None = None,
    query: FakeQueryBackend | None = None,
    limits: QueryLimits | None = None,
    secrets: DataQuerySecrets | None = None,
) -> tuple[DataQuestionService, FakeKnowledgeBackend, FakeQueryBackend]:
    knowledge = knowledge or FakeKnowledgeBackend(DOCUMENTS)
    query = query or FakeQueryBackend(table_rows=ROWS, columns=COLUMNS)
    service = DataQuestionService(
        knowledge=knowledge,
        query=query,
        policy=SqlPolicyChecker(allowed_objects=ALLOWED),
        limits=limits,
        secrets=secrets,
    )
    service.on_run_start(session_id="session-1")
    return service, knowledge, query


async def issue_bundle(service: DataQuestionService) -> str:
    result = await service.search("orders by region")
    assert result["evidence"], "expected evidence hits"
    return str(result["bundleId"])


async def test_template_must_be_read_and_rendered_before_prepare():
    template = """## BS.SQL.TEMPLATE.003 — 资产负债率
```sql
SELECT a.total, l.total, l.total / NULLIF(a.total, 0) * 100 AS ratio
FROM {{asset_table}} AS a JOIN {{liability_table}} AS l ON a.year_month = l.year_month
WHERE a.entity = %s AND a.year_month = %s
```
"""
    knowledge = FakeKnowledgeBackend({"template": template})
    query = FakeQueryBackend(table_rows=[[100, 40, 40]])
    service = DataQuestionService(
        knowledge=knowledge,
        query=query,
        policy=SqlPolicyChecker(
            allowed_objects=AllowedObjects.parse(["myschema.assets", "myschema.liabilities"])
        ),
    )
    service.on_run_start(session_id="session-1")
    found = await service.search("京能信息 202504 资产负债率 BS.SQL.TEMPLATE.003")
    bundle_id = str(found["bundleId"])
    evidence_id = str(found["evidence"][0]["evidenceId"])
    with pytest.raises(DataQueryValidationError, match="read before"):
        service.prepare(
            sql="SELECT 1",
            params=[],
            evidence_ids=[evidence_id],
            bundle_id=bundle_id,
            template_id="BS.SQL.TEMPLATE.003",
            template_evidence_id=evidence_id,
            template_sql="SELECT 1",
            identifiers={},
        )
    await service.read(evidence_id, bundle_id)
    body = template.split("```sql\n", 1)[1].split("\n```", 1)[0]
    rendered = body.replace("{{asset_table}}", "myschema.assets").replace(
        "{{liability_table}}", "myschema.liabilities"
    )
    prepared = service.prepare(
        sql=rendered,
        params=["E100198", 202504],
        evidence_ids=[evidence_id],
        bundle_id=bundle_id,
        template_id="BS.SQL.TEMPLATE.003",
        template_evidence_id=evidence_id,
        template_sql=body,
        identifiers={"asset_table": "myschema.assets", "liability_table": "myschema.liabilities"},
    )
    await service.execute(str(prepared["planId"]))
    assert query.param_sets == [("E100198", 202504)]


async def test_template_render_rejects_body_not_matching_read_evidence():
    knowledge = FakeKnowledgeBackend({"template": "```sql\nSELECT 1 FROM myschema.orders\n```"})
    service, _, _ = build_service(knowledge=knowledge)
    found = await service.search("orders")
    bundle_id = str(found["bundleId"])
    evidence_id = str(found["evidence"][0]["evidenceId"])
    await service.read(evidence_id, bundle_id)
    with pytest.raises(DataQueryValidationError, match="does not match"):
        service.prepare(
            sql="SELECT 1 FROM myschema.orders",
            params=[],
            evidence_ids=[evidence_id],
            bundle_id=bundle_id,
            template_id="BS.SQL.TEMPLATE.003",
            template_evidence_id=evidence_id,
            template_sql="SELECT 2 FROM myschema.orders",
            identifiers={},
        )


async def test_template_render_accepts_safe_column_slots_and_requires_qualified_tables():
    template = """## BS.SQL.TEMPLATE.003 — 资产负债率
```sql
SELECT a.{{asset_value_field}}, l.{{liability_value_field}}
FROM {{asset_table}} AS a JOIN {{liability_table}} AS l ON a.entity = l.entity
WHERE a.entity = %s
```
    """
    knowledge = FakeKnowledgeBackend({"template": template})
    service = DataQuestionService(
        knowledge=knowledge,
        query=FakeQueryBackend(table_rows=[]),
        policy=SqlPolicyChecker(
            allowed_objects=AllowedObjects.parse(["myschema.assets", "myschema.liabilities"])
        ),
    )
    found = await service.search("资产负债率")
    bundle_id = str(found["bundleId"])
    evidence_id = str(found["evidence"][0]["evidenceId"])
    await service.read(evidence_id, bundle_id)
    body = template.split("```sql\n", 1)[1].split("\n```", 1)[0]
    rendered = body.replace("{{asset_value_field}}", "bpc_rs05_0460")
    rendered = rendered.replace("{{liability_value_field}}", "bpc_rs06_0400")
    rendered = rendered.replace("{{asset_table}}", "myschema.assets")
    rendered = rendered.replace("{{liability_table}}", "myschema.liabilities")
    plan = service.prepare(
        sql=rendered,
        params=["E100198"],
        evidence_ids=[evidence_id],
        bundle_id=bundle_id,
        template_id="BS.SQL.TEMPLATE.003",
        template_evidence_id=evidence_id,
        template_sql=body,
        identifiers={
            "asset_value_field": "bpc_rs05_0460",
            "liability_value_field": "bpc_rs06_0400",
            "asset_table": "myschema.assets",
            "liability_table": "myschema.liabilities",
        },
    )
    assert plan["type"] == "plan_query"
    with pytest.raises(DataQueryValidationError, match="unsafe: asset_table"):
        service.prepare(
            sql=rendered.replace("myschema.assets", "assets"),
            params=["E100198"],
            evidence_ids=[evidence_id],
            bundle_id=bundle_id,
            template_id="BS.SQL.TEMPLATE.003",
            template_evidence_id=evidence_id,
            template_sql=body,
            identifiers={
                "asset_value_field": "bpc_rs05_0460",
                "liability_value_field": "bpc_rs06_0400",
                "asset_table": "assets",
                "liability_table": "myschema.liabilities",
            },
        )


def valid_sql() -> str:
    return (
        "SELECT region, SUM(amount) AS total FROM myschema.orders WHERE region = %s GROUP BY region"
    )


class _CountingPlanner:
    def __init__(self, *, source_id: str | None = None) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.source_id = source_id

    async def plan(self, messages, *, signal=None):
        del signal
        self.calls.append(list(messages))
        attempt = len(self.calls)
        return SimpleNamespace(
            answer=f"SELECT amount_{attempt} FROM myschema.orders",
            citations=(
                SimpleNamespace(
                    provider_id=f"chunk-{attempt}",
                    source_id=self.source_id,
                    title="Approved SQL template",
                    snippet=f"Use amount_{attempt} from myschema.orders.",
                ),
            ),
            request=messages[-1]["content"],
            response=f"planner response {attempt}",
        )

    async def test(self) -> None:
        return None

    async def close(self) -> None:
        return None


def build_agent_service(
    *, query: FakeQueryBackend | None = None, planner: _CountingPlanner | None = None
) -> tuple[DataQuestionService, FakeQueryBackend, _CountingPlanner]:
    query = query or FakeQueryBackend(table_rows=ROWS, columns=COLUMNS)
    planner = planner or _CountingPlanner()
    service = DataQuestionService(
        knowledge=FakeKnowledgeBackend(DOCUMENTS),
        query=query,
        policy=SqlPolicyChecker(allowed_objects=ALLOWED),
        planning_mode="agent",
        planner=planner,
        question_template="Plan exactly: {question}",
    )
    service.on_run_start(session_id="session-agent")
    return service, query, planner


class TestSearch:
    async def test_search_issues_bundle_with_bounded_evidence(self) -> None:
        service, knowledge, _query = build_service()
        result = await service.search("orders by region")

        assert result["bundleId"].startswith("bundle_")
        assert result["evidence"]
        assert all(item["evidenceId"] for item in result["evidence"])
        assert knowledge.search_calls == ["orders by region"]

    async def test_search_rejects_empty_question(self) -> None:
        service, _k, _q = build_service()
        with pytest.raises(DataQueryValidationError):
            await service.search("   ")

    async def test_search_limits_evidence_count(self) -> None:
        many = FakeKnowledgeBackend({f"ev{i}": f"document {i} about orders" for i in range(20)})
        service, _k, _q = build_service(knowledge=many)
        result = await service.search("orders")
        assert len(result["evidence"]) <= 5

    async def test_search_forwards_retry_context(self) -> None:
        service, knowledge, _q = build_service()
        result = await service.search(
            "orders by region", retry_context="SELECT bad FROM t\nERROR: no such column"
        )
        assert result["bundleId"].startswith("bundle_")
        assert knowledge.search_retry_contexts == ["SELECT bad FROM t\nERROR: no such column"]

    async def test_search_rejects_invalid_retry_context(self) -> None:
        service, _k, _q = build_service()
        with pytest.raises(DataQueryValidationError, match="retryContext must be a string"):
            await service.search("orders", retry_context=42)  # type: ignore[arg-type]

    async def test_search_rejects_overlong_retry_context(self) -> None:
        from tau_coding.dataquery.service import MAX_RETRY_CONTEXT_BYTES

        service, _k, _q = build_service()
        with pytest.raises(DataQueryValidationError, match="retryContext is too long"):
            await service.search("orders", retry_context="界" * (MAX_RETRY_CONTEXT_BYTES // 3 + 1))

    async def test_search_question_limit_is_measured_in_utf8_bytes(self) -> None:
        from tau_coding.dataquery.service import MAX_QUESTION_BYTES

        service, _k, _q = build_service()
        with pytest.raises(DataQueryValidationError, match="question is too long.*bytes"):
            await service.search("界" * (MAX_QUESTION_BYTES // 3 + 1))


class TestRead:
    async def test_read_expands_discovered_evidence(self) -> None:
        service, knowledge, _q = build_service()
        bundle_id = await issue_bundle(service)
        result = await service.read("ev1", bundle_id)

        assert result["content"].startswith("# Orders table")
        assert knowledge.read_calls == ["ev1"]

    async def test_read_rejects_evidence_not_in_bundle(self) -> None:
        service, _k, _q = build_service()
        await issue_bundle(service)
        with pytest.raises(DataQueryValidationError, match="unknown or belongs to a previous run"):
            await service.read("unknown_id")

    async def test_read_rejects_stale_evidence_after_new_run(self) -> None:
        service, _k, _q = build_service()
        bundle_id = await issue_bundle(service)
        service.on_run_start(session_id="session-2")
        with pytest.raises(DataQueryValidationError, match="previous run"):
            await service.read("ev1", bundle_id)


class TestPrepare:
    async def test_prepare_freezes_plan(self) -> None:
        service, _k, _q = build_service()
        bundle_id = await issue_bundle(service)
        result = service.prepare(
            sql=valid_sql(),
            params=["east"],
            evidence_ids=["ev1"],
            bundle_id=bundle_id,
        )

        assert result["planId"].startswith("plan_")
        assert result["paramCount"] == 1
        assert result["paramTypes"] == ["text"]
        assert result["evidenceCount"] == 1
        assert result["policyVersion"]
        assert len(result["sqlFingerprint"]) == 16

    async def test_prepare_allows_empty_evidence_for_schema_queries(self) -> None:
        service, _k, _q = build_service()
        await issue_bundle(service)
        result = service.prepare(
            sql=("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = %s"),
            params=["exchange_service"],
            evidence_ids=[],
        )
        assert result["planId"].startswith("plan_")
        assert result["evidenceCount"] == 0

    async def test_prepare_requires_evidence_for_business_queries(self) -> None:
        """An allowlisted business table still requires current evidence."""
        service, _k, _q = build_service()
        await issue_bundle(service)
        with pytest.raises(DataQueryValidationError, match="business SQL requires evidence"):
            service.prepare(
                sql=(
                    "SELECT region, COUNT(*) FROM myschema.orders "
                    "WHERE region = %s GROUP BY region"
                ),
                params=["east"],
                evidence_ids=[],
            )

    async def test_prepare_rejects_unknown_evidence(self) -> None:
        service, _k, _q = build_service()
        await issue_bundle(service)
        with pytest.raises(DataQueryValidationError, match="unknown or belongs to a previous run"):
            service.prepare(sql=valid_sql(), params=["east"], evidence_ids=["nope"])

    async def test_prepare_rejects_policy_violation(self) -> None:
        service, _k, _q = build_service()
        bundle_id = await issue_bundle(service)
        with pytest.raises(DataQueryValidationError, match="SQL rejected by policy"):
            service.prepare(
                sql="DROP TABLE myschema.orders",
                params=[],
                evidence_ids=["ev1"],
                bundle_id=bundle_id,
            )

    async def test_prepare_rejects_parameter_count_mismatch(self) -> None:
        service, _k, _q = build_service()
        bundle_id = await issue_bundle(service)
        with pytest.raises(DataQueryValidationError, match="parameter count mismatch"):
            service.prepare(
                sql=valid_sql(), params=["east", "extra"], evidence_ids=["ev1"], bundle_id=bundle_id
            )

    async def test_prepare_rejects_named_or_dollar_placeholders(self) -> None:
        service, _k, _q = build_service()
        bundle_id = await issue_bundle(service)
        with pytest.raises(DataQueryValidationError, match="positional placeholders"):
            service.prepare(
                sql="SELECT * FROM myschema.orders WHERE region = %(name)s",
                params=[],
                evidence_ids=["ev1"],
                bundle_id=bundle_id,
            )

    async def test_prepare_parameter_summary_contains_no_values(self) -> None:
        service, _k, _q = build_service()
        bundle_id = await issue_bundle(service)
        result = service.prepare(
            sql="SELECT * FROM myschema.orders WHERE region = %s AND amount > %s",
            params=["east", 42],
            evidence_ids=["ev1"],
            bundle_id=bundle_id,
        )

        assert result["paramCount"] == 2
        assert result["paramTypes"] == ["text", "integer"]
        assert "east" not in str(result)
        assert 42 not in result["paramTypes"]

    async def test_plan_sql_is_frozen_and_immutable(self) -> None:
        service, _k, _q = build_service()
        bundle_id = await issue_bundle(service)
        plan_id = str(
            service.prepare(
                sql=valid_sql(), params=["east"], evidence_ids=["ev1"], bundle_id=bundle_id
            )["planId"]
        )
        plan = service.plan_display(plan_id)
        assert plan.sql == valid_sql()
        assert plan.params == ("east",)


class TestExecute:
    async def test_execute_runs_frozen_plan(self) -> None:
        service, _k, query = build_service()
        bundle_id = await issue_bundle(service)
        plan_id = str(
            service.prepare(
                sql=valid_sql(), params=["east"], evidence_ids=["ev1"], bundle_id=bundle_id
            )["planId"]
        )
        result = await service.execute(plan_id)

        assert result["rowCount"] == 3
        assert result["columns"] == ["id", "region", "amount"]
        assert result["truncated"] is False
        assert result["truncationReasons"] == []
        assert result["emptyResult"] is False
        assert result["resultStatus"] == "rows_returned"
        assert query.statements == [valid_sql()]
        assert query.param_sets == [("east",)]

    async def test_execute_requires_plan(self) -> None:
        service, _k, _q = build_service()
        await issue_bundle(service)
        with pytest.raises(LedgerError, match="unknown"):
            await service.execute("plan_bogus")

    async def test_execute_rejects_stale_plan_after_new_run(self) -> None:
        service, _k, _q = build_service()
        bundle_id = await issue_bundle(service)
        plan_id = str(
            service.prepare(
                sql=valid_sql(), params=["east"], evidence_ids=["ev1"], bundle_id=bundle_id
            )["planId"]
        )
        service.on_run_start(session_id="session-2")
        with pytest.raises(LedgerError, match="previous run"):
            await service.execute(plan_id)

    async def test_same_plan_can_execute_multiple_times_in_run(self) -> None:
        service, _k, _q = build_service()
        bundle_id = await issue_bundle(service)
        plan_id = str(
            service.prepare(
                sql=valid_sql(), params=["east"], evidence_ids=["ev1"], bundle_id=bundle_id
            )["planId"]
        )
        first = await service.execute(plan_id)
        second = await service.execute(plan_id)
        assert first["rowCount"] == second["rowCount"] == 3
        assert len([a for a in service.audit_records if a.event == "execute"]) == 2

    async def test_execute_surfaces_sanitized_backend_error(self) -> None:
        failing = FakeQueryBackend(table_rows=ROWS, columns=COLUMNS)
        failing.fail_with = RuntimeError("password=supersecret crashed")
        service, _k, _q = build_service(
            query=failing, secrets=DataQuerySecrets(dws_password="supersecret")
        )
        bundle_id = await issue_bundle(service)
        plan_id = str(
            service.prepare(
                sql=valid_sql(), params=["east"], evidence_ids=["ev1"], bundle_id=bundle_id
            )["planId"]
        )
        with pytest.raises(QueryExecutionError) as exc_info:
            await service.execute(plan_id)
        assert "supersecret" not in str(exc_info.value)
        assert "[redacted]" in str(exc_info.value)

    async def test_execute_error_contains_copyable_sag_repair_context(self) -> None:
        failing = FakeQueryBackend(table_rows=ROWS, columns=COLUMNS)
        failing.fail_with = QuerySqlError('column "total_bad" does not exist')
        service, _k, _q = build_service(query=failing)
        bundle_id = await issue_bundle(service)
        sql = "SELECT total_bad FROM myschema.orders WHERE region = %s"
        plan_id = str(
            service.prepare(
                sql=sql,
                params=["east"],
                evidence_ids=["ev1"],
                bundle_id=bundle_id,
            )["planId"]
        )

        with pytest.raises(QueryExecutionError) as exc_info:
            await service.execute(plan_id)

        message = str(exc_info.value)
        assert "data_knowledge_search" in message
        assert "retryContext" in message
        assert sql in message
        assert 'column "total_bad" does not exist' in message
        assert "east" not in message
        assert "terminalMessage" not in message

    async def test_execute_infrastructure_error_does_not_invite_sql_repair(self) -> None:
        failing = FakeQueryBackend(table_rows=ROWS, columns=COLUMNS)
        failing.fail_with = QueryInfrastructureError("database connection failed: OSError")
        service, _k, _q = build_service(query=failing)
        bundle_id = await issue_bundle(service)
        plan_id = str(
            service.prepare(
                sql=valid_sql(), params=["east"], evidence_ids=["ev1"], bundle_id=bundle_id
            )["planId"]
        )

        with pytest.raises(QueryInfrastructureError) as exc_info:
            await service.execute(plan_id)

        message = str(exc_info.value)
        assert "database connection failed" in message
        assert "retryContext" not in message
        record = service.audit_records[-1]
        assert record.event == "execute"
        assert record.status == "infrastructure_error"

    async def test_execute_cancellation_signal_reaches_backend(self) -> None:
        class _CancelSignal:
            def is_cancelled(self) -> bool:
                return True

        service, _k, _q = build_service()
        bundle_id = await issue_bundle(service)
        plan_id = str(
            service.prepare(
                sql=valid_sql(), params=["east"], evidence_ids=["ev1"], bundle_id=bundle_id
            )["planId"]
        )
        with pytest.raises(QueryExecutionError, match="cancel"):
            await service.execute(plan_id, signal=_CancelSignal())


class TestRunScoping:
    async def test_audit_records_are_bounded_and_leak_free(self) -> None:
        service, _k, _q = build_service()
        bundle_id = await issue_bundle(service)
        plan_id = str(
            service.prepare(
                sql=valid_sql(), params=["east"], evidence_ids=["ev1"], bundle_id=bundle_id
            )["planId"]
        )
        await service.execute(plan_id)

        records = service.audit_records
        assert [record.event for record in records] == ["search", "prepare", "execute"]
        serialized = "".join(record.to_json().__repr__() for record in records)
        assert "east" not in serialized
        assert "plan_" in serialized

    async def test_execute_returns_terminal_empty_result_note(self) -> None:
        query = FakeQueryBackend(table_rows=[], columns=COLUMNS)
        service, _k, _q = build_service(query=query)
        bundle_id = await issue_bundle(service)
        plan_id = str(
            service.prepare(
                sql=valid_sql(), params=["east"], evidence_ids=["ev1"], bundle_id=bundle_id
            )["planId"]
        )
        result = await service.execute(plan_id)

        assert result["rowCount"] == 0
        assert result["emptyResult"] is True
        assert result["resultStatus"] == "no_rows"
        assert "final factual result" in result["terminalMessage"]
        assert "no matching records" in result["terminalMessage"]

    async def test_handles_are_opaque_and_server_generated(self) -> None:
        service, _k, _q = build_service()
        bundle_id = await issue_bundle(service)
        result = service.prepare(
            sql=valid_sql(), params=["east"], evidence_ids=["ev1"], bundle_id=bundle_id
        )
        assert len(str(result["planId"]).split("_")[1]) >= 32
        assert len(bundle_id.split("_")[1]) >= 32


class TestAgentPlannerConversation:
    async def test_transcript_budget_is_checked_before_calling_planner(self) -> None:
        planner = _CountingPlanner()
        service = DataQuestionService(
            knowledge=FakeKnowledgeBackend(DOCUMENTS),
            query=FakeQueryBackend(table_rows=ROWS, columns=COLUMNS),
            policy=SqlPolicyChecker(allowed_objects=ALLOWED),
            planning_mode="agent",
            planner=planner,
            planner_transcript_max_bytes=100,
        )
        service.on_run_start(session_id="session-agent")

        with pytest.raises(KnowledgeError, match="configured limit before request"):
            await service.search("x" * 60)

        assert planner.calls == []

    async def test_agent_fields_and_total_transcript_use_utf8_byte_budgets(self) -> None:
        class _LargeUnicodePlanner(_CountingPlanner):
            async def plan(self, messages, *, signal=None):
                del signal
                self.calls.append(list(messages))
                return SimpleNamespace(
                    answer="答案" * 100,
                    citations=(
                        SimpleNamespace(
                            provider_id="chunk-1",
                            source_id="finance-source",
                            title="标题" * 100,
                            snippet="证据" * 100,
                        ),
                    ),
                    request="请求" * 100,
                    response="响应" * 100,
                )

        planner = _LargeUnicodePlanner()
        exchanges = []
        service = DataQuestionService(
            knowledge=FakeKnowledgeBackend(DOCUMENTS),
            query=FakeQueryBackend(table_rows=ROWS, columns=COLUMNS),
            policy=SqlPolicyChecker(allowed_objects=ALLOWED),
            planning_mode="agent",
            planner=planner,
            planner_request_max_bytes=24,
            planner_answer_max_bytes=30,
            citation_limit=1,
            citation_title_max_bytes=15,
            citation_snippet_max_bytes=18,
            planner_transcript_max_bytes=130,
        )
        service.on_run_start(session_id="session-agent")

        result = await service.search("问题问题", on_exchange=exchanges.append)

        citation = result["citations"][0]  # type: ignore[index]
        exchange = exchanges[0]
        assert len(str(result["answer"]).encode("utf-8")) <= 30
        assert len(citation["title"].encode("utf-8")) <= 15  # type: ignore[index,union-attr]
        assert len(citation["snippet"].encode("utf-8")) <= 18  # type: ignore[index,union-attr]
        assert len(exchange.request.encode("utf-8")) <= 24
        assert len(exchange.response.encode("utf-8")) <= 30
        assert str(result["answer"]).endswith("…[truncated]")
        transcript_bytes = (
            sum(
                len(message["content"].encode("utf-8"))
                for message in planner.calls[0]
            )
            + len(str(result["answer"]).encode("utf-8"))
            + len(citation["title"].encode("utf-8"))  # type: ignore[index,union-attr]
            + len(citation["snippet"].encode("utf-8"))  # type: ignore[index,union-attr]
            + len(exchange.request.encode("utf-8"))
            + len(exchange.response.encode("utf-8"))
        )
        assert transcript_bytes <= 130

    async def test_multiple_searches_keep_independent_correction_histories(self) -> None:
        query = FakeQueryBackend(
            table_rows=ROWS,
            columns=COLUMNS,
            fail_with=QuerySqlError('column "amount_bad" does not exist'),
        )
        service, _query, planner = build_agent_service(query=query)
        questions = (
            "杭锦旗西部能源开发有限公司2024年4月单体口径负债合计",
            "杭锦旗西部能源开发有限公司2024年4月合并口径负债合计",
        )
        failed_sql = (
            "SELECT amount_single FROM myschema.orders",
            "SELECT amount_consolidated FROM myschema.orders",
        )

        bundles: list[str] = []
        for question, sql in zip(questions, failed_sql, strict=True):
            search = await service.search(question)
            bundles.append(str(search["bundleId"]))
            citation = search["citations"][0]  # type: ignore[index]
            plan = service.prepare(
                sql=sql,
                params=[],
                evidence_ids=[citation["evidenceId"]],  # type: ignore[index]
                bundle_id=str(search["bundleId"]),
            )
            with pytest.raises(QueryExecutionError):
                await service.execute(str(plan["planId"]))

        repaired_single = await service.search(questions[0], retry_context="retry single")
        repaired_consolidated = await service.search(
            questions[1], retry_context="retry consolidated"
        )

        assert bundles[0] != bundles[1]
        assert repaired_single["attempt"] == repaired_consolidated["attempt"] == 2
        assert planner.calls[0] == [
            {"role": "user", "content": f"Plan exactly: {questions[0]}"}
        ]
        assert planner.calls[1] == [
            {"role": "user", "content": f"Plan exactly: {questions[1]}"}
        ]
        assert planner.calls[2][0] == planner.calls[0][0]
        assert planner.calls[2][1] == {
            "role": "assistant",
            "content": "SELECT amount_1 FROM myschema.orders",
        }
        assert failed_sql[0] in planner.calls[2][2]["content"]
        assert planner.calls[3][0] == planner.calls[1][0]
        assert planner.calls[3][1] == {
            "role": "assistant",
            "content": "SELECT amount_2 FROM myschema.orders",
        }
        assert failed_sql[1] in planner.calls[3][2]["content"]

    async def test_retry_rejects_ambiguous_duplicate_question_histories(self) -> None:
        query = FakeQueryBackend(
            table_rows=ROWS,
            columns=COLUMNS,
            fail_with=QuerySqlError('column "amount_bad" does not exist'),
        )
        service, _query, planner = build_agent_service(query=query)
        question = "京能技术2025年4月短期借款"

        for attempt in range(2):
            search = await service.search(question)
            citation = search["citations"][0]  # type: ignore[index]
            plan = service.prepare(
                sql=f"SELECT amount_{attempt} FROM myschema.orders",
                params=[],
                evidence_ids=[citation["evidenceId"]],  # type: ignore[index]
                bundle_id=str(search["bundleId"]),
            )
            with pytest.raises(QueryExecutionError):
                await service.execute(str(plan["planId"]))

        with pytest.raises(DataQueryValidationError, match="retryContext is ambiguous"):
            await service.search(question, retry_context="retry")
        assert len(planner.calls) == 2

    async def test_correction_history_keeps_original_question_and_latest_answer_only(
        self,
    ) -> None:
        query = FakeQueryBackend(
            table_rows=ROWS,
            columns=COLUMNS,
            fail_with=QuerySqlError('column "amount_bad" does not exist'),
        )
        service, _query, planner = build_agent_service(query=query)
        question = "京能技术 2025年4月短期借款"

        for attempt in range(1, 4):
            search = await service.search(
                question,
                retry_context="correction requested" if attempt > 1 else None,
            )
            citation = search["citations"][0]  # type: ignore[index]
            plan = service.prepare(
                sql=f"SELECT amount_{attempt} FROM myschema.orders",
                params=[],
                evidence_ids=[citation["evidenceId"]],  # type: ignore[index]
                bundle_id=str(search["bundleId"]),
            )
            with pytest.raises(QueryExecutionError):
                await service.execute(str(plan["planId"]))

        assert len(planner.calls) == 3
        assert planner.calls[2] == [
            {"role": "user", "content": f"Plan exactly: {question}"},
            {
                "role": "assistant",
                "content": "SELECT amount_2 FROM myschema.orders",
            },
            {
                "role": "user",
                "content": planner.calls[2][2]["content"],
            },
        ]
        assert "SELECT amount_2 FROM myschema.orders" in planner.calls[2][2]["content"]
        assert planner.calls[2][2]["content"].startswith("SQL:\n")
        assert "Retry the same user question" not in planner.calls[2][2]["content"]
        with pytest.raises(DataQueryValidationError, match="correction limit reached"):
            await service.search(question, retry_context="correction requested")

    async def test_infrastructure_failure_does_not_open_agent_correction(self) -> None:
        query = FakeQueryBackend(
            table_rows=ROWS,
            columns=COLUMNS,
            fail_with=QueryInfrastructureError("database connection failed: OSError"),
        )
        service, _query, planner = build_agent_service(query=query)
        question = "京能技术短期借款"
        search = await service.search(question)
        citation = search["citations"][0]  # type: ignore[index]
        plan = service.prepare(
            sql="SELECT amount_1 FROM myschema.orders",
            params=[],
            evidence_ids=[citation["evidenceId"]],  # type: ignore[index]
            bundle_id=str(search["bundleId"]),
        )

        with pytest.raises(QueryInfrastructureError):
            await service.execute(str(plan["planId"]))
        with pytest.raises(DataQueryValidationError, match="requires a failed"):
            await service.search(question, retry_context="please retry")
        assert len(planner.calls) == 1

    async def test_new_run_starts_with_empty_planner_history(self) -> None:
        service, _query, planner = build_agent_service()
        await service.search("first question")

        service.on_run_start(session_id="session-agent")
        second = await service.search("second question")

        assert second["attempt"] == 1
        assert planner.calls[1] == [
            {"role": "user", "content": "Plan exactly: second question"}
        ]

    async def test_configured_agent_can_cite_any_of_its_financial_sources(self) -> None:
        planner = _CountingPlanner(source_id="source-returned-by-sag")
        knowledge = FakeKnowledgeBackend({"chunk-1": "financial source evidence"})
        service = DataQuestionService(
            knowledge=knowledge,
            query=FakeQueryBackend(table_rows=ROWS, columns=COLUMNS),
            policy=SqlPolicyChecker(allowed_objects=ALLOWED),
            planning_mode="agent",
            planner=planner,
            question_template="Plan exactly: {question}",
        )
        service.on_run_start(session_id="session-agent")

        result = await service.search("京能技术短期借款")
        citation = result["citations"][0]  # type: ignore[index]
        await service.read(str(citation["evidenceId"]), str(result["bundleId"]))  # type: ignore[index]

        assert knowledge.read_source_ids == ["source-returned-by-sag"]

    async def test_agent_without_mcp_exposes_snippet_but_disables_expansion(self) -> None:
        planner = _CountingPlanner()
        service = DataQuestionService(
            knowledge=UnavailableKnowledgeBackend(),
            query=FakeQueryBackend(table_rows=ROWS, columns=COLUMNS),
            policy=SqlPolicyChecker(allowed_objects=ALLOWED),
            planning_mode="agent",
            planner=planner,
            question_template="Plan exactly: {question}",
        )
        service.on_run_start(session_id="session-agent")

        result = await service.search("京能技术短期借款")
        citation = result["citations"][0]  # type: ignore[index]

        assert citation["snippet"] == "Use amount_1 from myschema.orders."  # type: ignore[index]
        assert citation["expandable"] is False  # type: ignore[index]
        with pytest.raises(DataQueryValidationError, match="citation_expansion_unavailable"):
            await service.read(
                str(citation["evidenceId"]),  # type: ignore[index]
                str(result["bundleId"]),
            )


class TestLimits:
    async def test_service_uses_resolved_limits(self) -> None:
        service, _k, query = build_service(limits=QueryLimits(max_rows=2))
        bundle_id = await issue_bundle(service)
        plan_id = str(
            service.prepare(
                sql=valid_sql(), params=["east"], evidence_ids=["ev1"], bundle_id=bundle_id
            )["planId"]
        )
        result = await service.execute(plan_id)
        assert result["rowCount"] == 2
        assert result["truncated"] is True
        assert result["truncationReasons"] == ["row_limit"]
