"""Application-layer SQL planner port for SAG Agent-backed data queries.

The port owns no HTTP, MCP, DWS, Rich, or Textual behavior.  It accepts an
explicit bounded chat history and returns an untrusted cited planning answer;
the data-query service turns citations into run-scoped evidence and Tau still
authors, validates, freezes, and authorizes the executable SQL.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from tau_agent.tools import ToolCancellationToken

PLANNING_SCOPE_RULES = (
    "查询范围与口径约束：保留用户要求的完整期间和粒度，全年逐月趋势优先一次集合查询，"
    "不要拆成十二个单月规划。主体简称的取数口径必须依据知识库中该主体的明确映射；"
    "用户未指定单体或合并，不代表默认单体。明确的用户口径优先，其次是知识库简称映射；"
    "两者都不足时说明缺口，不猜测编码或表名。引用对应主体和指标的依据，核对 SQL 的"
    "表、过滤编码与所选口径一致。保留单月发生额与年初累计的区别，不因改写而改变含义。"
    "同一主体的源表编码、报表取数码必须分别按字段证据使用，不可混用。"
    "比率必须核对分子和分母与指标定义一致，不能倒置。"
    "已有充分依据时给出完整 SQL，不要只返回图表配置。"
    "输出协议：简述主体、口径、单位和引用后，必须返回且只返回一个 sql_plan JSON 代码块，"
    '格式为 {"sql":"带 %s 占位符的完整 SELECT","params":[参数值],'
    '"visualization":{"kind":"line","dimension":"精确列名","metrics":["精确列名"],'
    '"unit":"实际结果单位","ordered_periods":true}}。'
    "代码块语言必须是 sql_plan，禁止改成 json、sql 或 visualization。"
    "visualization 不适用时省略，不要重复生成同一 SQL 或长篇解释。"
)


class PlannerMessage(TypedDict):
    """One portable message sent to the configured SQL planner."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class PlannerCitation:
    """One provider citation before Tau issues a run-scoped evidence id."""

    provider_id: str | None
    source_id: str | None
    title: str
    snippet: str


@dataclass(frozen=True, slots=True)
class PlannerAnswer:
    """One bounded, untrusted planner response and its observable exchange."""

    answer: str
    citations: tuple[PlannerCitation, ...]
    request: str
    response: str


class SqlPlanner(Protocol):
    """Port implemented by SAG Agent chat and deterministic test fakes."""

    async def plan(
        self,
        messages: Sequence[PlannerMessage],
        *,
        signal: ToolCancellationToken | None = None,
    ) -> PlannerAnswer:
        """Return one cited answer without executing SQL."""
        ...

    async def test(self) -> None:
        """Verify planner connectivity and authentication."""
        ...

    async def close(self) -> None:
        """Release HTTP or provider resources."""
        ...


def planning_warning(answer: str) -> str | None:
    """Report nonconforming planning output without guessing a replacement plan."""
    if structured_plan(answer) is not None:
        return None
    return (
        "SQL planner protocol error: expected exactly one sql_plan JSON block with sql and "
        "params, plus optional visualization. This response is not an executable plan. "
        "Do not infer SQL, units or missing data from it. Report missing evidence when present; "
        "otherwise request a protocol correction preserving the original scope and evidence."
    )


def structured_plan(answer: str) -> dict[str, object] | None:
    blocks = re.findall(r"```([^\n]*)\n(.*?)```", answer, re.S)
    if len(blocks) != 1 or blocks[0][0].strip() != "sql_plan":
        return None
    try:
        value = json.loads(blocks[0][1])
    except ValueError:
        return None
    if (
        not isinstance(value, dict)
        or set(value) - {"sql", "params", "visualization"}
        or not isinstance(value.get("sql"), str)
        or not value["sql"].strip()
        or not isinstance(value.get("params"), list)
        or ("visualization" in value and not isinstance(value["visualization"], dict))
    ):
        return None
    return value


def bound_visualization(
    answer: str, sql: str, params: Sequence[object]
) -> dict[str, object] | None:
    """Only transfer a chart when the executed expression and values match its source SQL."""
    from sqlglot import Tokenizer, exp, parse_one
    from sqlglot.errors import SqlglotError

    plan = structured_plan(answer)
    if plan is None:
        return None
    source_sql, source_params = plan["sql"], plan["params"]
    hint = plan.get("visualization")
    if (
        not isinstance(source_sql, str)
        or not isinstance(source_params, list)
        or not isinstance(hint, dict)
    ):
        return None

    def canonical(statement: str, values: Sequence[object]) -> str:
        tokens = Tokenizer(dialect="postgres").tokenize(statement)
        positions = [
            (a.start, b.end + 1)
            for a, b in zip(tokens, tokens[1:], strict=False)
            if a.text == "%" and b.text == "s" and a.end + 1 == b.start
        ]
        if len(positions) != len(values):
            raise ValueError("Placeholder mismatch")
        for (start, end), value in reversed(list(zip(positions, values, strict=True))):
            statement = (
                statement[:start] + exp.convert(value).sql(dialect="postgres") + statement[end:]
            )
        return parse_one(statement, read="postgres").sql(
            dialect="postgres", normalize=True, identify=True
        )

    try:
        if canonical(source_sql, source_params) != canonical(sql, params):
            return None
    except (SqlglotError, ValueError, TypeError, NotImplementedError):
        return None
    return hint


def rewrite_planner_question(question: str, template: str, *, max_bytes: int) -> str:
    """Apply the trusted Tau template and cap the final UTF-8 request."""
    stripped = question.strip()
    if template and "{question}" in template:
        rewritten = template.replace("{question}", stripped)
    elif template:
        rewritten = f"{template.rstrip()}\n\n{stripped}"
    else:
        rewritten = stripped
    # Apply the same scope contract even when a host supplies a custom question template.
    return bound_utf8(f"{PLANNING_SCOPE_RULES}\n\n{rewritten}", max_bytes)


def bound_utf8(value: str, max_bytes: int) -> str:
    """Return a valid UTF-8 prefix with an explicit truncation marker."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker = "…[truncated]"
    marker_bytes = marker.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore")
    return f"{prefix}{marker}"


__all__ = [
    "PlannerAnswer",
    "PlannerCitation",
    "PlannerMessage",
    "SqlPlanner",
    "bound_utf8",
    "rewrite_planner_question",
]
