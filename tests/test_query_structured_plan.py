import json

import pytest

from tau_coding.dataquery.planner import bound_visualization, structured_plan
from tau_coding.dataquery.service import DataQueryValidationError
from test_dataquery_workflow import build_agent_service

HINT = {
    "kind": "line",
    "dimension": "period",
    "metrics": ["amount"],
    "unit": "亿元",
    "ordered_periods": True,
}
SQL = "SELECT period, amount / 100000000 AS amount FROM myschema.orders WHERE id = %s"


def test_visualization_is_bound_to_exact_sql_and_parameters():
    answer = (
        "```sql_plan\n" + json.dumps({"sql": SQL, "params": [2], "visualization": HINT}) + "\n```"
    )
    assert bound_visualization(answer, SQL, [2]) == HINT
    assert bound_visualization(answer, SQL.replace("100000000", "10000"), [2]) is None
    assert bound_visualization(answer, SQL, [3]) is None
    assert bound_visualization(answer, SQL.replace("AS amount", "AS revenue"), [2]) is None


def test_separate_sql_and_chart_blocks_are_rejected():
    answer = "```sql\n" + SQL.replace("%s", "2") + "\n```\n```json\n" + json.dumps(HINT) + "\n```"
    assert bound_visualization(answer, SQL, [2]) is None


def test_json_structured_plan_and_ambiguity():
    answer = "```json\n" + json.dumps({"sql": SQL, "params": [2], "visualization": HINT}) + "\n```"
    assert bound_visualization(answer, SQL, [2]) is None
    assert structured_plan(answer + answer) is None
    assert structured_plan("```json\n" + json.dumps(HINT) + "\n```") is None


@pytest.mark.anyio
async def test_structured_prepare_keeps_evidence_and_policy_gates():
    service, query, planner = build_agent_service()
    original = planner.plan

    async def plan(messages, *, signal=None):
        result = await original(messages, signal=signal)
        result.answer = (
            "```sql_plan\n"
            + json.dumps({"sql": SQL, "params": [2], "visualization": HINT})
            + "\n```"
        )
        return result

    planner.plan = plan
    search = await service.search("orders")
    evidence = [search["citations"][0]["evidenceId"]]
    bundle = search["bundleId"]
    assert search["preparedSqlAvailable"] is True
    with pytest.raises(DataQueryValidationError):
        service.prepare_planned(bundle, [])
    prepared = service.prepare_planned(bundle, evidence)
    result = await service.execute(prepared["planId"])
    assert query.statements == [SQL]
    assert result["visualization"] == HINT
    service.on_run_start(session_id="next")
    with pytest.raises(DataQueryValidationError):
        service.prepare_planned(bundle, evidence)
