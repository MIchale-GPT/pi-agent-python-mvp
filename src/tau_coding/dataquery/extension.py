"""Bundled evidence-bound data-query extension entry point (PRD).

``setup(tau)`` registers the four domain tools only when the deployment is fully
configured (decision 20). Tool registration is the extension API surface; all
workflow state lives in :class:`DataQuestionService`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from typing import TypeVar, cast

from rich.markup import escape

from tau_agent.messages import TextContent
from tau_agent.tools import AgentTool, AgentToolResult, ToolCancellationToken, ToolUpdateCallback
from tau_agent.types import JSONValue
from tau_coding.dataquery.backends.base import KnowledgeBackend, KnowledgeExchange, QueryBackend
from tau_coding.dataquery.config import (
    ENV_DWS_ALLOWED_OBJECTS,
    PlanningMode,
    ResolvedDataQueryConfig,
    auto_approve_execute,
    resolve_data_query_config,
)
from tau_coding.dataquery.ledger import QueryPlan
from tau_coding.dataquery.planner import SqlPlanner
from tau_coding.dataquery.policy import AllowedObjects, SqlPolicyChecker
from tau_coding.dataquery.service import (
    DataQueryError,
    DataQuestionService,
    QueryLimits,
)
from tau_coding.extensions import ExtensionAPI, ExtensionContext

_TOOL_NAMES = frozenset(
    {"data_knowledge_search", "data_knowledge_read", "data_query_prepare", "data_query_execute"}
)

_ResultT = TypeVar("_ResultT")

_LEGACY_SEARCH_GUIDELINE = (
    "Data queries require evidence first: rewrite the user's complete question into one "
    "precise SAG query naming the entity as the user phrased it (use the full company name "
    "when already known), the exact report period (normalized, e.g. 2025年4月 → 202504), "
    "the indicator and any explicit comparison or caliber requirement, then call "
    "data_knowledge_search once with that question. Do not split entity resolution, caliber, "
    "table structure, field lookup, and SQL generation into separate SAG searches. The "
    "extension automatically appends an "
    "instruction that asks the knowledge source to reference its SQL templates and directly "
    "produce the SQL with only the fields the user's question needs, resolving entity codes "
    "and single/consolidated (单户/合并) caliber from the knowledge base. Do not ask the user "
    "back about caliber, consolidation or entity codes that the knowledge base can resolve; "
    "if the evidence still leaves the caliber ambiguous, state the assumption (default to "
    "单户) and proceed instead of asking. A SAG direct answer itself is evidence; do not call "
    "data_knowledge_read when that answer already contains the complete SQL. Read is only for "
    "a ranked evidence summary that lacks required detail. Then call data_query_prepare with "
    "a parameterized SELECT over schema-qualified "
    "tables cited in the evidence, then data_query_execute with the returned planId. Never "
    "guess table, column or relation names not present in the evidence, and stop when evidence "
    "is insufficient. If data_query_execute fails because the generated SQL was wrong, call "
    "data_knowledge_search again with the same rewritten question and pass retryContext "
    "containing the failed SQL and the error log so the knowledge source can correct the SQL, "
    "then prepare and execute the corrected SQL. Exception: for schema introspection questions "
    "(list tables/columns, describe the schema), skip data_knowledge_search and call "
    "data_query_prepare directly with a read-only SELECT over pg_catalog/information_schema "
    "and an empty evidenceIds list."
)

_AGENT_SEARCH_GUIDELINE = (
    "For a business data question, call data_knowledge_search once with one precise rewrite "
    "that preserves the user's entity, exact period, indicator, comparison, and explicit "
    "caliber. The configured SAG Agent resolves entity codes, reporting caliber, tables, "
    "fields, and SQL templates in that one turn and returns an untrusted cited planning "
    "answer. Use its answer and citation evidence to author a readable parameterized SELECT, "
    "then call data_query_prepare and data_query_execute. Do not execute or copy inline values "
    "from the SAG answer without parameterization. Do not split the question into separate SAG "
    "lookups. Call data_knowledge_read only when a returned citation is expandable and its "
    "snippet is insufficient. If execution returns retryContext, call data_knowledge_search "
    "again with the exact same question and that retryContext; the extension continues the "
    "stored SAG conversation. For schema introspection limited to pg_catalog or "
    "information_schema, prepare directly with empty evidenceIds."
)

_SQL_GUIDELINE = (
    "Generate a standard PostgreSQL SELECT subset with %s positional placeholders for every "
    "user-supplied value (never inline values). Tables must be schema-qualified and restricted "
    "to the allowed objects; only approved built-in functions are permitted. The SQL text is "
    "displayed to the user, so keep it readable."
)


def setup(tau: ExtensionAPI) -> None:
    """Register the four data tools when configuration is complete."""
    resolved = _resolve_extension_config()
    if not resolved.complete:
        diagnostics = resolved.configuration_diagnostics
        if diagnostics:
            summary = "; ".join(
                f"{item['code']}: {item['message']}" for item in diagnostics
            )
            tau.register_diagnostic(f"data query tools are not enabled: {summary}")
        else:
            tau.register_diagnostic(
                f"data query tools are not enabled: missing {_missing_requirements(resolved)}"
            )
        return

    allowed_objects = _allowed_objects_from_env()
    policy = SqlPolicyChecker(allowed_objects=allowed_objects)
    limits = QueryLimits(
        query_timeout_seconds=resolved.query_timeout_seconds,
        max_rows=resolved.max_rows,
        max_result_bytes=resolved.max_result_bytes,
        max_cell_bytes=resolved.max_cell_bytes,
    )

    try:
        knowledge, query, planner = _create_backends(resolved)
    except Exception as exc:  # noqa: BLE001 - optional-driver isolation
        tau.register_diagnostic(f"data query backends unavailable: {exc!r}", severity="error")
        return

    service = DataQuestionService(
        knowledge=knowledge,
        query=query,
        policy=policy,
        planning_mode=resolved.planning_mode,
        planner=planner,
        question_template=resolved.sag_question_template,
        planner_answer_max_bytes=resolved.sag_agent_answer_max_bytes,
        citation_limit=resolved.sag_citation_limit,
        citation_snippet_max_bytes=resolved.sag_citation_snippet_max_bytes,
        planner_transcript_max_bytes=resolved.sag_planner_transcript_max_bytes,
        limits=limits,
        secrets=resolved.secrets,
    )
    ui = tau.context.ui

    _register_tools(tau, service, ui, limits, resolved.planning_mode)
    _subscribe_lifecycle(tau, service)


# Config and backend construction are module-level seams so tests can inject
# fakes without touching the real home directory or optional drivers.
_resolve_extension_config: Callable[[], ResolvedDataQueryConfig] = resolve_data_query_config


def _create_backends(
    resolved: ResolvedDataQueryConfig,
) -> tuple[KnowledgeBackend, QueryBackend, SqlPlanner | None]:
    """Build the real SAG and DWS backends (lazy drivers, decision 17)."""
    from tau_coding.dataquery.backends.dws import DwsPostgresQueryBackend
    from tau_coding.dataquery.backends.sag import SagMcpKnowledgeBackend

    knowledge: KnowledgeBackend
    planner: SqlPlanner | None = None
    if resolved.planning_mode == "agent":
        from tau_coding.dataquery.backends.sag_agent import SagAgentSqlPlanner
        from tau_coding.dataquery.backends.unavailable import UnavailableKnowledgeBackend

        planner = SagAgentSqlPlanner(
            origin=resolved.sag_agent_origin,
            agent_id=resolved.sag_agent_id,
            token=resolved.secrets.sag_token or "",
            timeout_seconds=resolved.sag_agent_timeout_seconds,
            max_response_bytes=resolved.sag_planner_transcript_max_bytes,
        )
        if resolved.sag_endpoint:
            knowledge = SagMcpKnowledgeBackend(
                endpoint=resolved.sag_endpoint,
                token=resolved.secrets.sag_token or "",
                source_id=resolved.sag_source_id,
                search_tool=resolved.sag_search_tool,
                read_tool=resolved.sag_read_tool,
                arg_query=resolved.sag_arg_query,
                arg_source=resolved.sag_arg_source,
                arg_document=resolved.sag_arg_document,
                timeout_seconds=resolved.sag_rpc_timeout_seconds,
                protocol_version=resolved.sag_protocol_version,
                probe_query=resolved.sag_probe_query,
                search_summary_max_bytes=resolved.sag_search_summary_max_bytes,
                question_template=resolved.sag_question_template,
            )
        else:
            knowledge = UnavailableKnowledgeBackend()
    else:
        knowledge = SagMcpKnowledgeBackend(
            endpoint=resolved.sag_endpoint,
            token=resolved.secrets.sag_token or "",
            source_id=resolved.sag_source_id,
            search_tool=resolved.sag_search_tool,
            read_tool=resolved.sag_read_tool,
            arg_query=resolved.sag_arg_query,
            arg_source=resolved.sag_arg_source,
            arg_document=resolved.sag_arg_document,
            timeout_seconds=resolved.sag_rpc_timeout_seconds,
            protocol_version=resolved.sag_protocol_version,
            probe_query=resolved.sag_probe_query,
            search_summary_max_bytes=resolved.sag_search_summary_max_bytes,
            question_template=resolved.sag_question_template,
        )
    query: QueryBackend = DwsPostgresQueryBackend(
        host=resolved.host,
        port=resolved.port,
        database=resolved.database,
        username=resolved.username,
        password=resolved.secrets.dws_password or "",
        sslmode=resolved.sslmode,
        connect_timeout=resolved.dws_connect_timeout,
        probe_query=resolved.dws_probe_query,
    )
    return knowledge, query, planner


# ---------------------------------------------------------------------------
# Tool registration.
# ---------------------------------------------------------------------------


def _register_tools(
    tau: ExtensionAPI,
    service: DataQuestionService,
    ui: object,
    limits: QueryLimits,
    planning_mode: PlanningMode,
) -> None:
    async def run_search(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        del on_update
        retry_context = arguments.get("retryContext")
        exchanges: list[KnowledgeExchange] = []
        payload = await _raise_clean(
            service.search(
                str(arguments.get("question") or ""),
                retry_context=(
                    str(retry_context)
                    if isinstance(retry_context, str) and retry_context.strip()
                    else None
                ),
                on_exchange=exchanges.append,
                tool_call_id=tool_call_id,
                signal=signal,
            )
        )
        details: dict[str, JSONValue] | None = None
        if exchanges:
            exchange = exchanges[-1]
            sag_exchange: dict[str, JSONValue] = {
                "request": exchange.request,
                "response": exchange.response,
            }
            if planning_mode == "agent":
                raw_attempt = payload.get("attempt")
                attempt = (
                    raw_attempt
                    if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool)
                    else 1
                )
                sag_exchange.update(
                    {
                        "mode": "agent",
                        "attempt": attempt,
                        "citations": cast(JSONValue, payload.get("citations", [])),
                    }
                )
            details = {"sagExchange": sag_exchange}
        return _ok_result(payload, details=details)

    async def run_read(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        del tool_call_id, signal, on_update
        bundle_id = arguments.get("bundleId")
        return _ok_result(
            await _raise_clean(
                service.read(
                    str(arguments.get("evidenceId") or ""),
                    bundle_id=str(bundle_id) if isinstance(bundle_id, str) else None,
                )
            )
        )

    async def run_prepare(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        del tool_call_id, signal, on_update
        params = arguments.get("params")
        raw_evidence = arguments.get("evidenceIds")
        evidence_ids = (
            [value for value in raw_evidence if isinstance(value, str)]
            if isinstance(raw_evidence, list)
            else []
        )
        bundle_id = arguments.get("bundleId")
        return _ok_result(
            service.prepare(
                sql=str(arguments.get("sql") or ""),
                params=params if isinstance(params, list) else [],
                evidence_ids=evidence_ids,
                bundle_id=str(bundle_id) if isinstance(bundle_id, str) else None,
            )
        )

    async def run_execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        del tool_call_id, on_update
        plan_id = arguments.get("planId")
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise _validation("planId must be a non-empty string")
        try:
            plan = service.plan_display(plan_id)
            if not auto_approve_execute():
                confirmed = await _request_confirmation(ui, plan, limits)
                if not confirmed:
                    raise _validation("query execution was not confirmed")
            else:
                print(f"[data-query] auto-approved execute of plan {plan.plan_id}", file=sys.stderr)
            return _ok_result(await _raise_clean(service.execute(plan_id, signal=signal)))
        except DataQueryError:
            raise
        except Exception as exc:  # noqa: BLE001 - clean tool errors
            raise _validation(f"execute failed: {type(exc).__name__}") from exc

    search_description = (
        "Ask the configured SAG Agent once for a complete cited SQL-planning answer for the "
        "user's entity, period, indicator, comparison, and caliber. Returns answer and "
        "citations in a run-scoped evidence bundle. Pass retryContext only after a DWS SQL "
        "failure to continue the stored planner conversation."
        if planning_mode == "agent"
        else (
            "Search the approved SAG knowledge source for tables, columns, relations, "
            "business definitions and SQL templates relevant to the user's data question. "
            "Returns the existing bundleId plus bounded evidence list. Pass retryContext "
            "with a failed SQL context to request corrected evidence."
        )
    )
    search_guideline = (
        _AGENT_SEARCH_GUIDELINE if planning_mode == "agent" else _LEGACY_SEARCH_GUIDELINE
    )

    tau.register_tool(
        AgentTool(
            name="data_knowledge_search",
            label="SAG Agent planning" if planning_mode == "agent" else "Knowledge search",
            description=search_description,
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "A precise rewrite of the user's data question: exact entity "
                            "name, normalized report period (e.g. 202504), the indicator and "
                            "any caliber requirement."
                        ),
                    },
                    "retryContext": {
                        "type": "string",
                        "description": (
                            "Optional. When the previously generated SQL failed to execute, "
                            "pass the failed SQL plus the database error log so the knowledge "
                            "source can correct the SQL. Keep the same indicator and period "
                            "in the question."
                        ),
                    },
                },
                "required": ["question"],
            },
            execute_fn=run_search,
            prompt_guidelines=(search_guideline,),
            render_call=_search_render_call,
            render_result=_search_render_result,
        )
    )
    tau.register_tool(
        AgentTool(
            name="data_knowledge_read",
            label="Evidence read",
            description=(
                "Expand the full content of one evidence item already returned by "
                "data_knowledge_search in the current run. Only discovered evidence is "
                "readable."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "evidenceId": {
                        "type": "string",
                        "description": "An evidenceId returned by data_knowledge_search.",
                    },
                    "bundleId": {
                        "type": "string",
                        "description": "Optional bundleId from data_knowledge_search.",
                    },
                },
                "required": ["evidenceId"],
            },
            execute_fn=run_read,
        )
    )
    tau.register_tool(
        AgentTool(
            name="data_query_prepare",
            label="Query prepare",
            description=(
                "Validate and freeze a parameterized read-only SELECT over the current "
                "evidence. Returns an opaque planId that data_query_execute will run. "
                "Requires at least one evidenceId from the current knowledge search; "
                "for schema introspection queries (pg_catalog/information_schema) the "
                "evidenceIds list may be empty."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": (
                            "Parameterized SELECT with %s placeholders, schema-qualified "
                            "tables only."
                        ),
                    },
                    "params": {
                        "type": "array",
                        "description": (
                            "Positional JSON parameter values matching the %s placeholders."
                        ),
                    },
                    "evidenceIds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Evidence ids from data_knowledge_search backing this SQL. "
                            "May be empty for schema introspection queries over "
                            "pg_catalog/information_schema."
                        ),
                    },
                    "bundleId": {
                        "type": "string",
                        "description": "Optional bundleId from data_knowledge_search.",
                    },
                },
                "required": ["sql", "params"],
            },
            execute_fn=run_prepare,
            render_call=_prepare_render_call,
            prompt_guidelines=(_SQL_GUIDELINE,),
        )
    )
    tau.register_tool(
        AgentTool(
            name="data_query_execute",
            label="Query execute",
            description=(
                "Execute a frozen query plan in a read-only DWS transaction and return "
                "bounded rows plus truncation status. Confirmation is required on every "
                "execution."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "planId": {
                        "type": "string",
                        "description": "The planId returned by data_query_prepare.",
                    }
                },
                "required": ["planId"],
            },
            execute_fn=run_execute,
            render_call=_execute_render_call,
        )
    )

    tau.register_authorization_view(
        "data_query_execute", _execute_authorization_view(service, limits)
    )


def _subscribe_lifecycle(tau: ExtensionAPI, service: DataQuestionService) -> None:
    def on_agent_start(event: object, context: ExtensionContext) -> None:
        del event
        service.on_run_start(session_id=context.session_id)

    def on_agent_end(event: object, context: ExtensionContext) -> None:
        del event, context
        service.on_run_end()

    def on_shutdown(event: object, context: ExtensionContext) -> Awaitable[None]:
        del event, context

        async def _close() -> None:
            await service.close()

        # Closing is awaited by the runtime's event dispatch; run it via the
        # same coroutine path to guarantee resource cleanup on quit/reload.
        return _close()

    tau.on("agent_start", on_agent_start)
    tau.on("agent_end", on_agent_end)
    tau.on("session_shutdown", on_shutdown)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


async def _request_confirmation(ui: object, plan: QueryPlan, limits: QueryLimits) -> bool:
    message = (
        f"Parameterized SQL:\n{plan.sql}\n\n"
        f"Evidence items: {len(plan.evidence_ids)} · "
        f"Parameters: {plan.param_count}\n"
        f"Timeout: {limits.query_timeout_seconds} s · Max rows: {limits.max_rows}"
    )
    confirm = getattr(ui, "confirm", None)
    if confirm is None:
        return False
    return bool(await confirm("Execute DWS query?", message))


def _execute_authorization_view(
    service: DataQuestionService, limits: QueryLimits
) -> Callable[[Mapping[str, JSONValue]], Mapping[str, JSONValue] | None]:
    """Return the plan display payload for the host confirmation dialog."""

    def view(arguments: Mapping[str, JSONValue]) -> Mapping[str, JSONValue] | None:
        plan_id = arguments.get("planId")
        if not isinstance(plan_id, str) or not plan_id.strip():
            return None
        try:
            plan = service.plan_display(plan_id)
        except DataQueryError:
            return None
        return {
            "planId": plan.plan_id,
            "sql": plan.sql,
            "evidenceCount": len(plan.evidence_ids),
            "paramCount": plan.param_count,
            "paramTypes": list(plan.param_types),
            "queryTimeoutSeconds": limits.query_timeout_seconds,
            "maxRows": limits.max_rows,
        }

    return view


def _prepare_render_call(arguments: Mapping[str, JSONValue]) -> str | None:
    """Render the prepare invocation without exposing parameter values."""
    params = arguments.get("params")
    evidence_ids = arguments.get("evidenceIds")
    param_count = len(params) if isinstance(params, list) else 0
    evidence_count = len(evidence_ids) if isinstance(evidence_ids, list) else 0
    return f"data_query_prepare: {param_count} params, {evidence_count} evidence items"


def _search_render_call(arguments: Mapping[str, JSONValue]) -> str | None:
    question = arguments.get("question")
    if not isinstance(question, str) or not question.strip():
        return None
    compact = " ".join(question.split())
    if len(compact) > 160:
        compact = f"{compact[:159]}…"
    return f"SAG knowledge: {compact}"


def _search_render_result(result: AgentToolResult, *, expanded: bool) -> str | None:
    if not isinstance(result.details, dict):
        return None
    raw_exchange = result.details.get("sagExchange")
    if not isinstance(raw_exchange, dict):
        return None
    request = raw_exchange.get("request")
    response = raw_exchange.get("response")
    if not isinstance(request, str) or not isinstance(response, str):
        return None
    mode = raw_exchange.get("mode")
    if mode == "agent":
        raw_attempt = raw_exchange.get("attempt")
        attempt = (
            raw_attempt
            if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool)
            else 1
        )
        raw_citations = raw_exchange.get("citations")
        citations = (
            [item for item in raw_citations if isinstance(item, dict)]
            if isinstance(raw_citations, list)
            else []
        )
        turn_label = "初始规划" if attempt == 1 else "修正"
        if not expanded:
            return (
                f"[green]✓[/green] SAG Agent {turn_label}完成 · 第 {attempt} 轮 · "
                f"{len(citations)} 条引用 · Ctrl+O 展开"
            )
        citation_lines: list[str] = []
        for index, citation in enumerate(citations, start=1):
            title = citation.get("title")
            snippet = citation.get("snippet")
            expandable = citation.get("expandable") is True
            if not isinstance(title, str) or not isinstance(snippet, str):
                continue
            expansion = "可展开" if expandable else "仅摘要"
            citation_lines.append(
                f"[bold]{index}. {escape(title)}[/bold] [dim]({expansion})[/dim]\n"
                f"{escape(snippet)}"
            )
        rendered_citations = (
            "\n\n".join(citation_lines) if citation_lines else "[dim]（无可显示引用）[/dim]"
        )
        return (
            f"[green]✓ SAG Agent {turn_label} · 第 {attempt} 轮[/green]\n\n"
            "[bold cyan]Tau → SAG（实际请求）[/bold cyan]\n"
            f"{escape(request)}\n\n"
            "[bold magenta]SAG → Tau（原始回复）[/bold magenta]\n"
            f"{escape(response) if response else '[dim]（空回复）[/dim]'}\n\n"
            "[bold yellow]Citations[/bold yellow]\n"
            f"{rendered_citations}"
        )
    if not expanded:
        return "[green]✓[/green] SAG 问答完成 · Ctrl+O 查看真实交互"
    return (
        "[green]✓ SAG 问答完成[/green]\n\n"
        "[bold cyan]Tau → SAG（实际请求）[/bold cyan]\n"
        f"{escape(request)}\n\n"
        "[bold magenta]SAG → Tau（原始回复）[/bold magenta]\n"
        f"{escape(response) if response else '[dim]（空回复）[/dim]'}"
    )


def _execute_render_call(arguments: Mapping[str, JSONValue]) -> str | None:
    plan_id = arguments.get("planId")
    if isinstance(plan_id, str):
        return f"data_query_execute: plan {plan_id}"
    return None


def _ok_result(
    payload: Mapping[str, object], *, details: dict[str, JSONValue] | None = None
) -> AgentToolResult:
    text = json.dumps(dict(payload), ensure_ascii=False, indent=2, default=str)
    return AgentToolResult(content=[TextContent(text=text)], details=details)


async def _raise_clean[ResultT](coro: Awaitable[ResultT]) -> ResultT:
    """Await a service coroutine, re-raising sanitized domain errors.

    Unexpected backend exceptions are converted to a generic error that never
    includes driver text (secrets could leak through connection errors).
    """
    from tau_coding.dataquery.service import (
        DataQueryValidationError,
        KnowledgeError,
        QueryExecutionError,
    )

    try:
        return await coro
    except (DataQueryValidationError, KnowledgeError, QueryExecutionError):
        raise
    except Exception as exc:  # noqa: BLE001 - unexpected backend failure
        raise QueryExecutionError(f"data query failed: {type(exc).__name__}") from exc


def _validation(message: str) -> DataQueryError:
    from tau_coding.dataquery.service import DataQueryValidationError

    return DataQueryValidationError(message)


def _missing_requirements(resolved: ResolvedDataQueryConfig) -> list[str]:
    missing: list[str] = []
    if not resolved.host:
        missing.append("dws host")
    if not resolved.port:
        missing.append("dws port")
    if not resolved.database:
        missing.append("dws database")
    if not resolved.username:
        missing.append("dws username")
    if not resolved.secrets.dws_password:
        missing.append("dws password")
    if not resolved.sag_endpoint:
        missing.append("sag endpoint")
    if not resolved.secrets.sag_token:
        missing.append("sag token")
    return missing


def _allowed_objects_from_env() -> AllowedObjects:
    import os

    raw = os.environ.get(ENV_DWS_ALLOWED_OBJECTS, "")
    if not raw.strip():
        return AllowedObjects()
    return AllowedObjects.parse(entry for entry in raw.split(","))
