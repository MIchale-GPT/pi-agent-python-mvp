import json

import pytest
from sqlglot.errors import SqlglotError

from tau_coding.dataquery.reuse import validate_reuse_sql, validate_reused_metrics
from tau_coding.dataquery.service import DataQueryValidationError
from test_dataquery_workflow import build_agent_service

BASE = "SELECT period, amount FROM myschema.orders WHERE period = %s"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT amount FROM myschema.orders WHERE period BETWEEN %s AND %s ORDER BY period",
        "WITH q AS (SELECT period, amount FROM myschema.orders) SELECT q.amount FROM q",
        "SELECT amount, CASE WHEN amount IS NULL THEN 1 ELSE 0 END AS missing FROM myschema.orders",
    ],
)
def test_known_columns_can_be_recombined(sql):
    validate_reuse_sql(BASE, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT invented FROM myschema.orders",
        "SELECT amount FROM myschema.orders WHERE hidden = 1",
        "SELECT amount FROM myschema.orders ORDER BY hidden",
        "SELECT amount FROM myschema.other",
        "SELECT * FROM myschema.orders",
        "WITH q AS (SELECT hidden AS amount FROM myschema.orders) SELECT amount FROM q",
        "SELECT a.amount FROM myschema.orders a JOIN myschema.orders b ON a.secret = b.period",
    ],
)
def test_unevidenced_identifiers_are_rejected(sql):
    with pytest.raises((ValueError, SqlglotError)):
        validate_reuse_sql(BASE, sql)


def test_metric_formula_cannot_silently_change_with_same_label():
    sql = "SELECT amount * 100 AS ratio FROM myschema.orders"
    validate_reused_metrics(sql, sql + " ORDER BY ratio", ["ratio"])
    with pytest.raises(ValueError, match="Changed metric"):
        validate_reused_metrics(sql, sql.replace("100", "10000"), ["ratio"])


@pytest.mark.parametrize(
    "change",
    [
        {"createdMs": 0},
        {"createdMs": 999999999999999},
        {"context": "other-source"},
        {"sessionId": "other-session"},
    ],
)
def test_expired_or_different_scope_snapshot_is_not_reusable(change):
    service, _, _ = build_agent_service()
    service._reuse_context = "source"
    service.on_run_start(session_id="session")
    snapshot = {
        "planId": "previous",
        "context": "source",
        "sessionId": "session",
        "createdMs": service._clock(),
        "sql": BASE,
        **change,
    }
    service.restore_reusable(snapshot)
    with pytest.raises(DataQueryValidationError, match="unavailable"):
        service.prepare_reused("previous", BASE, [202503])


@pytest.mark.anyio
async def test_restored_evidence_reuses_without_sag_and_does_not_reuse_authorization():
    service, query, planner = build_agent_service()
    service._reuse_context = "configured-source-and-policy"
    service.on_run_start(session_id="owner-session")
    original = planner.plan

    async def plan(messages, *, signal=None):
        answer = await original(messages, signal=signal)
        answer.answer = "```sql_plan\n" + json.dumps({"sql": BASE, "params": [202505]}) + "\n```"
        return answer

    planner.plan = plan
    search = await service.search("monthly amount")
    prepared = service.prepare_planned(search["bundleId"], [search["citations"][0]["evidenceId"]])
    result = await service.execute(prepared["planId"])
    snapshot = result["reuseEvidence"]
    service.on_run_start(session_id="owner-session")
    service.restore_reusable(snapshot)
    with pytest.raises(DataQueryValidationError):
        service.plan_display(prepared["planId"])
    followup = service.prepare_reused(result["reusablePlanId"], BASE, [202503])
    await service.execute(followup["planId"])
    assert len(planner.calls) == 1
    assert len(query.statements) == 2
    with pytest.raises(DataQueryValidationError):
        service.prepare_reused(
            result["reusablePlanId"], BASE.replace("amount", "invented"), [202503]
        )
    service.on_run_start(session_id="different-session")
    service.restore_reusable(snapshot)
    with pytest.raises(DataQueryValidationError):
        service.prepare_reused(result["reusablePlanId"], BASE, [202503])


@pytest.mark.anyio
async def test_malformed_plan_cannot_be_bypassed_by_handwritten_sql():
    service, _, planner = build_agent_service()
    original = planner.plan

    async def plan(messages, *, signal=None):
        answer = await original(messages, signal=signal)
        answer.answer = "```json\n" + json.dumps({"sql": BASE, "params": [202505]}) + "\n```"
        return answer

    planner.plan = plan
    search = await service.search("amount")
    with pytest.raises(DataQueryValidationError, match="protocol error"):
        service.prepare(
            sql=BASE,
            params=[202505],
            bundle_id=search["bundleId"],
            evidence_ids=[search["citations"][0]["evidenceId"]],
        )


@pytest.mark.anyio
async def test_empty_execution_does_not_certify_reusable_business_evidence():
    from tau_coding.dataquery.backends.fake import FakeQueryBackend

    service, _, planner = build_agent_service(query=FakeQueryBackend(table_rows=[]))
    service._reuse_context = "source"
    original = planner.plan

    async def plan(messages, *, signal=None):
        answer = await original(messages, signal=signal)
        answer.answer = "```sql_plan\n" + json.dumps({"sql": BASE, "params": [202505]}) + "\n```"
        return answer

    planner.plan = plan
    search = await service.search("amount")
    prepared = service.prepare_planned(search["bundleId"], [search["citations"][0]["evidenceId"]])
    result = await service.execute(prepared["planId"])
    assert result["rowCount"] == 0
    assert "reusablePlanId" not in result
    assert service.reusable_evidence() == []
