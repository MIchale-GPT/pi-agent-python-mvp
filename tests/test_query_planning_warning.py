import pytest

from tau_coding.dataquery.planner import (
    PLANNING_SCOPE_RULES,
    planning_warning,
    rewrite_planner_question,
)


@pytest.mark.parametrize(
    "answer",
    [
        '{"kind":"line","metrics":["营收"]}',
        '```json\n{"kind":"line","metrics":["营收"]}\n```',
        '```visualization\n{"kind":"line","metrics":["营收"]}\n```',
    ],
)
def test_chart_only_answer_is_incomplete(answer):
    assert planning_warning(answer) is not None


@pytest.mark.parametrize(
    "answer",
    [
        "请补充期间范围。",
        "没有足够证据确定字段。",
        "SELECT amount FROM finance.income",
        'SELECT amount FROM finance.income;\n```json\n{"kind":"value","metrics":["amount"]}\n```',
        '{"kind":"line","metrics":["amount"],"sql":"SELECT amount FROM finance.income"}',
        "[]",
        "not json",
    ],
)
def test_nonconforming_output_is_not_an_executable_plan(answer):
    assert planning_warning(answer) is not None


def test_only_structured_plan_satisfies_protocol():
    assert planning_warning('```sql_plan\n{"sql":"SELECT 1", "params":[]}\n```') is None


def test_custom_template_preserves_shared_scope_and_caliber_rules():
    question = "公司甲 2025 年每个月营收，合并口径"
    result = rewrite_planner_question(question, "Plan: {question}", max_bytes=8192)
    assert result == f"{PLANNING_SCOPE_RULES}\n\nPlan: {question}"
    assert "不代表默认单体" in result
    assert "不猜测编码或表名" in result
    assert "不要拆成十二个单月规划" in result
