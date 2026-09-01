"""DataQuestionService: workflow and state gating for the four domain tools.

Implements the PRD workflow contract:

- ``data_knowledge_search`` issues a bundle for the current run;
- ``data_knowledge_read`` only expands evidence already in the current bundle;
- ``data_query_prepare`` requires at least one concrete evidence id from the
  current bundle, validates SQL against the policy checker, binds positional
  parameters and freezes a QueryPlan;
- ``data_query_execute`` only accepts a frozen plan id and runs the query in a
  read-only transaction, re-authorizing on every execution.

Every handle is opaque and run-scoped; stale or guessed handles fail closed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlglot import tokenize
from sqlglot.errors import SqlglotError

from tau_agent.tools import ToolCancellationToken
from tau_coding.dataquery.backends.base import (
    MAX_BUNDLE_BYTES,
    MAX_READ_BYTES,
    KnowledgeBackend,
    KnowledgeExchange,
    KnowledgeExchangeCallback,
    QueryBackend,
)
from tau_coding.dataquery.config import DataQuerySecrets, PlanningMode
from tau_coding.dataquery.ledger import (
    BundleRecord,
    EvidenceLedger,
    EvidenceRecord,
    LedgerError,
    QueryPlan,
    QueryPlanStore,
    RunScope,
    new_handle,
    sql_fingerprint,
    summarize_params,
)
from tau_coding.dataquery.planner import (
    PlannerMessage,
    SqlPlanner,
    bound_utf8,
    rewrite_planner_question,
)
from tau_coding.dataquery.policy import SqlPolicyChecker, is_schema_introspection_query

MAX_PREPARE_PARAMS = 100
MAX_QUESTION_BYTES = 8 * 1024
MAX_RETRY_CONTEXT_BYTES = 8 * 1024
# Compatibility aliases for extensions that imported the Phase 26 constants.
MAX_QUESTION_LENGTH = MAX_QUESTION_BYTES
MAX_RETRY_CONTEXT_LENGTH = MAX_RETRY_CONTEXT_BYTES
MAX_EVIDENCE_IDS = 20
MAX_SQL_LENGTH = 64 * 1024
_TEMPLATE_ID = re.compile(r"^[A-Z][A-Z0-9_.-]*\.TEMPLATE\.[0-9]+$")
_SQL_FENCE = re.compile(r"```sql\s*\n(?P<body>.*?)\n```", re.IGNORECASE | re.DOTALL)
_IDENTIFIER_SLOT = re.compile(r"\{\{(?P<name>[a-z][a-z0-9_]*)\}\}")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_SAFE_COLUMN_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TABLE_SLOTS = frozenset({"asset_table", "fact_table", "liability_table", "merged_table"})

logger = logging.getLogger(__name__)


def _safe_template_identifier(slot: str, value: str) -> bool:
    """Validate table slots separately from column/field slots."""
    candidate = value.strip()
    if slot in _TABLE_SLOTS or slot.endswith("_table"):
        return bool(_SAFE_IDENTIFIER.fullmatch(candidate))
    return bool(_SAFE_COLUMN_IDENTIFIER.fullmatch(candidate))


class DataQueryError(ValueError):
    """Base class for user-visible data-query errors."""


class DataQueryValidationError(DataQueryError):
    """Raised when a tool call violates the workflow contract."""


class QueryExecutionError(DataQueryError):
    """Raised when the database rejects or cancels an execution."""


class QuerySqlError(QueryExecutionError):
    """Raised when DWS accepted the connection but rejected the SQL statement."""


class QueryInfrastructureError(QueryExecutionError):
    """Raised when DWS execution cannot start or complete for infrastructure reasons."""


class KnowledgeError(DataQueryError):
    """Raised when the knowledge backend fails."""


@dataclass(frozen=True, slots=True)
class QueryLimits:
    """Effective execution limits (resolved from config, decision 10/12)."""

    query_timeout_seconds: int = 180
    max_rows: int = 1000
    max_result_bytes: int = 1024 * 1024
    max_cell_bytes: int = 64 * 1024


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Bounded audit entry (decision 19: no password, no parameter values)."""

    event: str
    run_id: str
    bundle_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    plan_id: str | None = None
    sql_fingerprint: str | None = None
    param_count: int | None = None
    duration_ms: int | None = None
    row_count: int | None = None
    truncated: bool | None = None
    planner_mode: PlanningMode | None = None
    planner_attempt: int | None = None
    planner_tool_call_id: str | None = None
    citation_count: int | None = None
    status: str = "ok"
    detail: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "event": self.event,
            "runId": self.run_id,
            "bundleId": self.bundle_id,
            "evidenceIds": list(self.evidence_ids),
            "planId": self.plan_id,
            "sqlFingerprint": self.sql_fingerprint,
            "paramCount": self.param_count,
            "durationMs": self.duration_ms,
            "rowCount": self.row_count,
            "truncated": self.truncated,
            "plannerMode": self.planner_mode,
            "plannerAttempt": self.planner_attempt,
            "plannerToolCallId": self.planner_tool_call_id,
            "citationCount": self.citation_count,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(slots=True)
class _PlannerConversation:
    """Mutable run-scoped state serialized by ``_planner_lock``."""

    original_question: str
    messages: list[PlannerMessage]
    attempt: int
    latest_bundle_id: str
    pending_repair_context: str | None = None


class DataQuestionService:
    """Workflow and state gating for the evidence-bound query tools."""

    def __init__(
        self,
        *,
        knowledge: KnowledgeBackend,
        query: QueryBackend,
        policy: SqlPolicyChecker,
        planning_mode: PlanningMode = "legacy",
        planner: SqlPlanner | None = None,
        question_template: str = "{question}",
        planner_request_max_bytes: int = 8 * 1024,
        planner_answer_max_bytes: int = 64 * 1024,
        citation_limit: int = 5,
        citation_title_max_bytes: int = 1024,
        citation_snippet_max_bytes: int = 8 * 1024,
        planner_transcript_max_bytes: int = 256 * 1024,
        limits: QueryLimits | None = None,
        secrets: DataQuerySecrets | None = None,
        audit: list[AuditRecord] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._citation_expansion_available = bool(
            getattr(knowledge, "citation_expansion_available", True)
        )
        self._query = query
        self._policy = policy
        self._planning_mode = planning_mode
        self._planner = planner
        self._question_template = question_template
        self._planner_request_max_bytes = planner_request_max_bytes
        self._planner_answer_max_bytes = planner_answer_max_bytes
        self._citation_limit = citation_limit
        self._citation_title_max_bytes = citation_title_max_bytes
        self._citation_snippet_max_bytes = citation_snippet_max_bytes
        self._planner_transcript_max_bytes = planner_transcript_max_bytes
        self._limits = limits or QueryLimits()
        self._secrets = secrets or DataQuerySecrets()
        self._audit = audit if audit is not None else []
        self._clock = clock or _now_ms
        self._ledger = EvidenceLedger()
        self._plans = QueryPlanStore()
        self._bundle_read_bytes: dict[str, int] = {}
        self._template_reads: dict[tuple[str, str], str] = {}
        self._planner_lock = asyncio.Lock()
        self._planner_conversation: _PlannerConversation | None = None

    # -- run scope -----------------------------------------------------------

    def on_run_start(self, *, session_id: str | None = None) -> None:
        """Start a new run scope; old bundles and plans become unreachable."""
        self._plans.reset_scope()
        self._ledger.reset_scope(session_id=session_id)
        self._bundle_read_bytes.clear()
        self._template_reads.clear()
        self._planner_conversation = None

    def on_run_end(self) -> None:
        """End the run; scoped state is dropped on the next run start."""
        self._plans.reset_scope()

    @property
    def scope(self) -> RunScope:
        return self._ledger.current

    @property
    def audit_records(self) -> list[AuditRecord]:
        return list(self._audit)

    # -- tool workflows ------------------------------------------------------

    async def search(
        self,
        question: str,
        *,
        retry_context: str | None = None,
        on_exchange: KnowledgeExchangeCallback | None = None,
        tool_call_id: str | None = None,
        signal: ToolCancellationToken | None = None,
    ) -> dict[str, object]:
        """Search the fixed knowledge source and issue a bundle.

        ``retry_context`` optionally carries a previously generated SQL and the
        database error log so the knowledge source can correct the SQL.
        """
        if not isinstance(question, str) or not question.strip():
            raise DataQueryValidationError("question must be a non-empty string")
        question = question.strip()
        if len(question.encode("utf-8")) > MAX_QUESTION_BYTES:
            raise DataQueryValidationError(
                f"question is too long (max {MAX_QUESTION_BYTES} UTF-8 bytes)"
            )
        if retry_context is not None:
            if not isinstance(retry_context, str):
                raise DataQueryValidationError("retryContext must be a string")
            retry_context = retry_context.strip() or None
            if (
                retry_context is not None
                and len(retry_context.encode("utf-8")) > MAX_RETRY_CONTEXT_BYTES
            ):
                raise DataQueryValidationError(
                    f"retryContext is too long (max {MAX_RETRY_CONTEXT_BYTES} UTF-8 bytes)"
                )
        if self._planning_mode == "agent":
            return await self._plan_with_agent(
                question,
                retry_context=retry_context,
                on_exchange=on_exchange,
                tool_call_id=tool_call_id,
                signal=signal,
            )

        try:
            callback: KnowledgeExchangeCallback | None = None
            if on_exchange is not None:

                def callback(exchange: KnowledgeExchange) -> None:
                    on_exchange(
                        KnowledgeExchange(
                            request=(
                                _sanitize(exchange.request, self._secrets)
                                if exchange.request
                                else ""
                            ),
                            response=(
                                _sanitize(exchange.response, self._secrets)
                                if exchange.response
                                else ""
                            ),
                        )
                    )

            evidence = await self._knowledge.search(
                question,
                retry_context=retry_context,
                on_exchange=callback,
            )
        except Exception as exc:  # noqa: BLE001 - backend isolation
            raise KnowledgeError(_sanitize(str(exc) or type(exc).__name__, self._secrets)) from exc
        total_bytes = sum(
            len(item.summary.encode("utf-8")) + len(item.title.encode("utf-8")) for item in evidence
        )
        if total_bytes > MAX_BUNDLE_BYTES:
            total_bytes = MAX_BUNDLE_BYTES
        records = [
            EvidenceRecord(
                evidence_id=item.evidence_id,
                title=item.title,
                summary=item.summary,
                provider_id=item.evidence_id,
            )
            for item in evidence
        ]
        bundle_id = self._ledger.register_evidence(records, total_bytes=total_bytes)
        self._audit.append(
            AuditRecord(
                event="search",
                run_id=self.scope.run_id,
                bundle_id=bundle_id,
                evidence_ids=tuple(record.evidence_id for record in records),
                detail="knowledge search issued bundle",
            )
        )
        return {
            "bundleId": bundle_id,
            "evidence": [
                {
                    "evidenceId": record.evidence_id,
                    "title": record.title,
                    "summary": record.summary,
                }
                for record in records
            ],
        }

    async def read(self, evidence_id: str, bundle_id: str | None = None) -> dict[str, object]:
        """Expand one evidence item already discovered in the current run."""
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise DataQueryValidationError("evidenceId must be a non-empty string")
        bundle = self._resolve_bundle(bundle_id, evidence_id)
        if not self._ledger.evidence_in_bundle(bundle.bundle_id, evidence_id, scope=self.scope):
            raise DataQueryValidationError(
                "evidenceId was not discovered in the current knowledge search"
            )
        record = bundle.evidence[evidence_id]
        if not record.expandable or not record.provider_id:
            raise DataQueryValidationError("citation_expansion_unavailable")
        used = self._bundle_read_bytes.get(bundle.bundle_id, 0)
        remaining = MAX_BUNDLE_BYTES - used
        if remaining <= 0:
            raise DataQueryValidationError("bundle evidence budget is exhausted")
        try:
            content = await self._knowledge.read(
                record.provider_id,
                source_id=record.provider_source_id,
                max_bytes=min(MAX_READ_BYTES, remaining),
            )
        except Exception as exc:  # noqa: BLE001 - backend isolation
            raise KnowledgeError(_sanitize(str(exc) or type(exc).__name__, self._secrets)) from exc
        self._bundle_read_bytes[bundle.bundle_id] = used + len(content.content.encode("utf-8"))
        self._template_reads[(bundle.bundle_id, evidence_id)] = content.content
        self._audit.append(
            AuditRecord(
                event="read",
                run_id=self.scope.run_id,
                bundle_id=bundle.bundle_id,
                evidence_ids=(evidence_id,),
            )
        )
        return {"bundleId": bundle.bundle_id, "evidenceId": evidence_id, "content": content.content}

    def prepare(
        self,
        *,
        sql: str,
        params: Sequence[object],
        evidence_ids: Sequence[str],
        bundle_id: str | None = None,
        template_id: str | None = None,
        template_evidence_id: str | None = None,
        template_sql: str | None = None,
        identifiers: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Validate, bind and freeze a query plan (decision 6/7)."""
        if not isinstance(sql, str) or not sql.strip():
            raise DataQueryValidationError("sql must be a non-empty string")
        if len(sql) > MAX_SQL_LENGTH:
            raise DataQueryValidationError(f"sql is too long (max {MAX_SQL_LENGTH} characters)")
        if template_id is not None:
            sql = self._render_read_template(
                sql=sql,
                template_id=template_id,
                template_evidence_id=template_evidence_id,
                template_sql=template_sql,
                identifiers=identifiers,
                bundle_id=bundle_id,
                evidence_ids=evidence_ids,
            )
        if not isinstance(params, list) or not all(_is_json_value(value) for value in params):
            raise DataQueryValidationError("params must be a list of JSON values")
        if len(params) > MAX_PREPARE_PARAMS:
            raise DataQueryValidationError(f"too many parameters (max {MAX_PREPARE_PARAMS})")
        if evidence_ids is not None and not isinstance(evidence_ids, list):
            raise DataQueryValidationError("evidenceIds must be a list of strings")
        evidence_ids = list(evidence_ids or [])
        if len(evidence_ids) > MAX_EVIDENCE_IDS:
            raise DataQueryValidationError(f"too many evidence ids (max {MAX_EVIDENCE_IDS})")
        for evidence_id in evidence_ids:
            if not isinstance(evidence_id, str) or not evidence_id.strip():
                raise DataQueryValidationError("evidenceIds must be non-empty strings")

        bundle: BundleRecord | None = None
        if evidence_ids:
            try:
                bundle = self._resolve_bundle(bundle_id, evidence_ids[0])
                for evidence_id in evidence_ids:
                    if not self._ledger.evidence_in_bundle(
                        bundle.bundle_id, evidence_id, scope=self.scope
                    ):
                        raise DataQueryValidationError(
                            f"evidenceId {evidence_id!r} was not discovered in the current "
                            "knowledge search"
                        )
            except LedgerError as exc:
                raise DataQueryValidationError(str(exc)) from exc
        report = self._policy.validate(sql)
        if not report.allowed:
            raise DataQueryValidationError(f"SQL rejected by policy: {report.reason}")
        if not evidence_ids and not is_schema_introspection_query(sql):
            raise DataQueryValidationError(
                "business SQL requires evidence from the current knowledge bundle"
            )

        placeholder_count = _count_positional_placeholders(sql)
        if placeholder_count != len(params):
            raise DataQueryValidationError(
                f"parameter count mismatch: SQL has {placeholder_count} placeholders, "
                f"params has {len(params)}"
            )

        param_count, param_types = summarize_params(params)
        plan_id = new_handle("plan")
        plan = QueryPlan(
            plan_id=plan_id,
            run_id=self.scope.run_id,
            session_id=self.scope.session_id,
            bundle_id=bundle.bundle_id if bundle is not None else None,
            evidence_ids=tuple(evidence_ids),
            sql=sql,
            params=tuple(params),
            param_count=param_count,
            param_types=param_types,
            policy_version=self._ledger.policy_version,
            sql_fingerprint=sql_fingerprint(sql),
            created_ms=self._clock(),
        )
        self._plans.put(plan)
        self._audit.append(
            AuditRecord(
                event="prepare",
                run_id=self.scope.run_id,
                bundle_id=bundle.bundle_id if bundle is not None else None,
                evidence_ids=tuple(evidence_ids),
                plan_id=plan_id,
                sql_fingerprint=plan.sql_fingerprint,
                param_count=param_count,
                detail="direct query without evidence" if not evidence_ids else None,
            )
        )
        return {
            "type": "plan_query",
            "planId": plan_id,
            "policyVersion": plan.policy_version,
            "sqlFingerprint": plan.sql_fingerprint,
            "paramCount": plan.param_count,
            "paramTypes": list(plan.param_types),
            "evidenceCount": len(plan.evidence_ids),
        }

    def _render_read_template(
        self,
        *,
        sql: str,
        template_id: str,
        template_evidence_id: str | None,
        template_sql: str | None,
        identifiers: Mapping[str, object] | None,
        bundle_id: str | None,
        evidence_ids: Sequence[str],
    ) -> str:
        """Render only a template body previously expanded from this bundle."""
        if not isinstance(template_id, str) or not _TEMPLATE_ID.fullmatch(template_id.strip()):
            raise DataQueryValidationError("templateId must be a stable TEMPLATE id")
        if not isinstance(template_evidence_id, str) or not template_evidence_id.strip():
            raise DataQueryValidationError("templateEvidenceId is required for template rendering")
        if template_evidence_id not in evidence_ids:
            raise DataQueryValidationError("template evidence must be included in evidenceIds")
        bundle = self._resolve_bundle(bundle_id, template_evidence_id)
        recorded = self._template_reads.get((bundle.bundle_id, template_evidence_id))
        if recorded is None:
            raise DataQueryValidationError("template must be read before it can be rendered")
        bodies = _SQL_FENCE.findall(recorded)
        if len(bodies) != 1 or not bodies[0].strip():
            raise DataQueryValidationError("template evidence has no complete SQL body")
        if not isinstance(template_sql, str) or template_sql.strip() != bodies[0].strip():
            raise DataQueryValidationError("templateSql does not match the read template body")
        values = identifiers or {}
        slots = tuple(
            dict.fromkeys(match.group("name") for match in _IDENTIFIER_SLOT.finditer(template_sql))
        )
        if set(values) != set(slots):
            raise DataQueryValidationError("template identifier slots do not match the template")
        rendered = template_sql
        for slot in slots:
            value = values[slot]
            if not isinstance(value, str) or not _safe_template_identifier(slot, value):
                raise DataQueryValidationError(f"template identifier is unsafe: {slot}")
            rendered = rendered.replace("{{" + slot + "}}", value.strip())
        if "{{" in rendered or "}}" in rendered:
            raise DataQueryValidationError("template contains unresolved identifier slots")
        if sql.strip() != rendered.strip():
            raise DataQueryValidationError("sql must equal the controlled template rendering")
        return rendered

    def plan_display(self, plan_id: str) -> QueryPlan:
        """Return a frozen plan for the host confirmation dialog (decision 16)."""
        try:
            return self._plans.get(plan_id, scope=self.scope)
        except LedgerError as exc:
            raise DataQueryValidationError(str(exc)) from exc

    async def execute(
        self,
        plan_id: str,
        *,
        signal: ToolCancellationToken | None = None,
    ) -> dict[str, object]:
        """Execute a frozen plan in a read-only transaction (decision 6)."""
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise DataQueryValidationError("planId must be a non-empty string")
        plan = self._plans.get(plan_id, scope=self.scope)
        try:
            result = await self._query.execute(
                plan.sql,
                list(plan.params),
                timeout_seconds=self._limits.query_timeout_seconds,
                max_rows=self._limits.max_rows,
                max_result_bytes=self._limits.max_result_bytes,
                max_cell_bytes=self._limits.max_cell_bytes,
                signal=signal,
            )
        except Exception as exc:  # noqa: BLE001 - backend isolation
            sanitized_error = _sanitize(str(exc) or type(exc).__name__, self._secrets)
            logger.warning(
                "data query execute failed: run_id=%s plan_id=%s sql_fingerprint=%s error=%s",
                self.scope.run_id,
                plan.plan_id,
                plan.sql_fingerprint,
                sanitized_error,
            )
            if _signal_is_cancelled(signal):
                self._audit.append(
                    AuditRecord(
                        event="execute",
                        run_id=self.scope.run_id,
                        bundle_id=plan.bundle_id,
                        evidence_ids=plan.evidence_ids,
                        plan_id=plan.plan_id,
                        sql_fingerprint=plan.sql_fingerprint,
                        param_count=plan.param_count,
                        status="cancelled",
                        detail="query cancelled",
                    )
                )
                raise QueryExecutionError("query cancelled") from exc
            if not isinstance(exc, QuerySqlError):
                self._audit.append(
                    AuditRecord(
                        event="execute",
                        run_id=self.scope.run_id,
                        bundle_id=plan.bundle_id,
                        evidence_ids=plan.evidence_ids,
                        plan_id=plan.plan_id,
                        sql_fingerprint=plan.sql_fingerprint,
                        param_count=plan.param_count,
                        planner_mode=(
                            self._planning_mode if self._planning_mode == "agent" else None
                        ),
                        status="infrastructure_error",
                        detail=sanitized_error,
                    )
                )
                raise QueryInfrastructureError(sanitized_error) from exc
            repair_context = _execution_repair_context(plan.sql, sanitized_error)
            repair_message = _execution_repair_message(repair_context, sanitized_error)
            if self._planning_mode == "agent":
                async with self._planner_lock:
                    conversation = self._planner_conversation
                    if (
                        conversation is not None
                        and plan.bundle_id == conversation.latest_bundle_id
                    ):
                        conversation.pending_repair_context = repair_context
            self._audit.append(
                AuditRecord(
                    event="execute",
                    run_id=self.scope.run_id,
                    bundle_id=plan.bundle_id,
                    evidence_ids=plan.evidence_ids,
                    plan_id=plan.plan_id,
                    sql_fingerprint=plan.sql_fingerprint,
                    param_count=plan.param_count,
                    planner_mode=(self._planning_mode if self._planning_mode == "agent" else None),
                    status="sql_error",
                    detail="DWS rejected the frozen SQL",
                )
            )
            raise QueryExecutionError(repair_message) from exc
        self._audit.append(
            AuditRecord(
                event="execute",
                run_id=self.scope.run_id,
                bundle_id=plan.bundle_id,
                evidence_ids=plan.evidence_ids,
                plan_id=plan.plan_id,
                sql_fingerprint=plan.sql_fingerprint,
                param_count=plan.param_count,
                duration_ms=result.elapsed_ms,
                row_count=len(result.rows),
                truncated=result.truncated,
            )
        )
        empty_result = len(result.rows) == 0
        payload = {
            "sql": plan.sql,
            "columns": [column.name for column in result.columns],
            "rows": result.rows,
            "rowCount": len(result.rows),
            "emptyResult": empty_result,
            "resultStatus": "no_rows" if empty_result else "rows_returned",
            "truncated": result.truncated,
            "truncationReasons": list(result.truncation_reasons),
            "previewedCells": result.previewed_cells,
            "elapsedMs": result.elapsed_ms,
            "evidenceIds": list(plan.evidence_ids),
            "policyVersion": plan.policy_version,
        }
        if empty_result:
            payload["terminalMessage"] = (
                "The query returned no matching records for the requested entity, period, and "
                "indicator; "
                "treat this as the final factual result unless the user asks to diagnose "
                "missing data."
            )
        return payload

    async def close(self) -> None:
        if self._planner is not None:
            await self._planner.close()
        await self._knowledge.close()
        await self._query.close()

    # -- helpers -------------------------------------------------------------

    def _resolve_bundle(self, bundle_id: str | None, evidence_id: str) -> BundleRecord:
        try:
            if bundle_id is not None:
                if not isinstance(bundle_id, str) or not bundle_id.strip():
                    raise DataQueryValidationError("bundleId must be a non-empty string")
                return self._ledger.bundle(bundle_id, scope=self.scope)
            resolved = self._ledger.bundle_for_evidence(evidence_id, scope=self.scope)
        except LedgerError as exc:
            raise DataQueryValidationError(str(exc)) from exc
        return self._ledger.bundle(resolved, scope=self.scope)

    async def _plan_with_agent(
        self,
        question: str,
        *,
        retry_context: str | None,
        on_exchange: KnowledgeExchangeCallback | None,
        tool_call_id: str | None,
        signal: ToolCancellationToken | None,
    ) -> dict[str, object]:
        if self._planner is None:
            raise KnowledgeError("SAG Agent planner is unavailable")

        async with self._planner_lock:
            conversation = self._planner_conversation
            if retry_context is None:
                if conversation is not None:
                    raise DataQueryValidationError(
                        "an Agent planner conversation already exists in the current run"
                    )
                rewritten = rewrite_planner_question(
                    question,
                    self._question_template,
                    max_bytes=self._planner_request_max_bytes,
                )
                messages: list[PlannerMessage] = [{"role": "user", "content": rewritten}]
                attempt = 1
            else:
                if conversation is None or conversation.pending_repair_context is None:
                    raise DataQueryValidationError(
                        "retryContext requires a failed Agent-planned query in the current run"
                    )
                if question.strip() != conversation.original_question:
                    raise DataQueryValidationError(
                        "a planner correction must use the exact original question"
                    )
                if conversation.attempt >= 3:
                    raise DataQueryValidationError(
                        "SAG Agent correction limit reached; start a new user turn"
                    )
                rewritten = conversation.messages[0]["content"]
                messages = [
                    *conversation.messages,
                    {
                        "role": "user",
                        "content": bound_utf8(
                            conversation.pending_repair_context,
                            MAX_RETRY_CONTEXT_BYTES,
                        ),
                    },
                ]
                attempt = conversation.attempt + 1
            message_bytes = sum(
                len(message["content"].encode("utf-8")) for message in messages
            )
            output_reserve = min(
                self._planner_answer_max_bytes + self._citation_snippet_max_bytes,
                max(1, self._planner_transcript_max_bytes // 2),
            )
            if message_bytes + output_reserve > self._planner_transcript_max_bytes:
                raise KnowledgeError(
                    "SAG Agent planner transcript exceeds the configured limit before request"
                )
            try:
                planned = await self._planner.plan(messages, signal=signal)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - planner isolation
                raise KnowledgeError(
                    _sanitize(str(exc) or type(exc).__name__, self._secrets)
                ) from exc

            remaining_bytes = self._planner_transcript_max_bytes - message_bytes
            raw_citations = [
                (
                    citation,
                    _sanitize(str(citation.snippet).strip(), self._secrets),
                )
                for citation in tuple(planned.citations)[: self._citation_limit]
            ]
            raw_citations = [item for item in raw_citations if item[1]]
            if not raw_citations:
                raise KnowledgeError("SAG Agent response is missing usable citations")
            citation_reserve = min(
                self._citation_snippet_max_bytes,
                max(1, remaining_bytes // 2),
            )
            answer = bound_utf8(
                _sanitize(str(planned.answer).strip(), self._secrets),
                min(
                    self._planner_answer_max_bytes,
                    max(0, remaining_bytes - citation_reserve),
                ),
            )
            if not answer:
                raise KnowledgeError("SAG Agent response is missing an answer")
            remaining_bytes -= len(answer.encode("utf-8"))

            records: list[EvidenceRecord] = []
            citations: list[dict[str, object]] = []
            for citation, raw_snippet in raw_citations:
                snippet = bound_utf8(
                    raw_snippet,
                    min(self._citation_snippet_max_bytes, remaining_bytes),
                )
                if not snippet:
                    continue
                remaining_bytes -= len(snippet.encode("utf-8"))
                evidence_id = new_handle("evidence")
                title = bound_utf8(
                    _sanitize(str(citation.title).strip(), self._secrets),
                    min(self._citation_title_max_bytes, remaining_bytes),
                )
                remaining_bytes -= len(title.encode("utf-8"))
                provider_id = (
                    str(citation.provider_id).strip() if citation.provider_id else None
                )
                provider_source_id = (
                    str(citation.source_id).strip() if citation.source_id else None
                )
                expandable = bool(provider_id) and self._citation_expansion_available
                records.append(
                    EvidenceRecord(
                        evidence_id=evidence_id,
                        title=title,
                        summary=snippet,
                        provider_id=provider_id,
                        provider_source_id=provider_source_id,
                        expandable=expandable,
                    )
                )
                citations.append(
                    {
                        "evidenceId": evidence_id,
                        "title": title,
                        "snippet": snippet,
                        "expandable": expandable,
                    }
                )
            if not records:
                raise KnowledgeError("SAG Agent response is missing usable citations")

            request = bound_utf8(
                _sanitize(str(planned.request), self._secrets),
                min(self._planner_request_max_bytes, remaining_bytes),
            )
            remaining_bytes -= len(request.encode("utf-8"))
            response = bound_utf8(
                _sanitize(str(planned.response), self._secrets),
                min(self._planner_answer_max_bytes, remaining_bytes),
            )

            bundle_id = self._ledger.register_evidence(
                records,
                total_bytes=min(
                    MAX_BUNDLE_BYTES,
                    sum(
                        len(record.title.encode("utf-8"))
                        + len(record.summary.encode("utf-8"))
                        for record in records
                    ),
                ),
            )
            if conversation is None:
                self._planner_conversation = _PlannerConversation(
                    original_question=question.strip(),
                    messages=[*messages, {"role": "assistant", "content": answer}],
                    attempt=attempt,
                    latest_bundle_id=bundle_id,
                )
            else:
                conversation.messages = [
                    messages[0],
                    {"role": "assistant", "content": answer},
                ]
                conversation.attempt = attempt
                conversation.latest_bundle_id = bundle_id
                conversation.pending_repair_context = None
            self._audit.append(
                AuditRecord(
                    event="search",
                    run_id=self.scope.run_id,
                    bundle_id=bundle_id,
                    evidence_ids=tuple(record.evidence_id for record in records),
                    planner_mode="agent",
                    planner_attempt=attempt,
                    planner_tool_call_id=tool_call_id,
                    citation_count=len(records),
                    detail="SAG Agent planning turn issued bundle",
                )
            )
            if on_exchange is not None:
                on_exchange(KnowledgeExchange(request=request, response=response))
            return {
                "type": "plan_query",
                "mode": "agent",
                "attempt": attempt,
                "bundleId": bundle_id,
                "answer": answer,
                "citations": citations,
            }


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _count_positional_placeholders(sql: str) -> int:
    """Count ``%s`` placeholders on the token stream and reject other forms.

    String literals tokenize as single tokens, so a literal like ``'100%'``
    does not count. Named (``%(name)s``) and dollar (``$1``) forms are rejected
    because the query backend binds positionally.
    """
    try:
        tokens = list(tokenize(sql, read="postgres"))
    except SqlglotError as exc:
        raise DataQueryValidationError("SQL tokenization failed") from exc
    count = 0
    for index, token in enumerate(tokens):
        if token.text == "%":
            if index + 1 < len(tokens) and tokens[index + 1].text == "s":
                count += 1
            else:
                raise DataQueryValidationError("only %s positional placeholders are supported")
        if token.text == "$":
            raise DataQueryValidationError("dollar parameters ($1) are not supported")
    return count


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int, float)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _signal_is_cancelled(signal: object | None) -> bool:
    if signal is None:
        return False
    checker = getattr(signal, "is_cancelled", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:  # noqa: BLE001 - cancellation probes must not mask query errors
        return False


def _sanitize(text: str, secrets: DataQuerySecrets) -> str:
    """Remove configured secrets from an error message (decision 11/14)."""
    redacted = text
    for secret in (secrets.dws_password, secrets.sag_token):
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    redacted = re.sub(r"(://)([^/@\s]+)(@)", r"\1[redacted]\3", redacted)
    return redacted or "operation failed"


def _execution_repair_context(sql: str, error: str) -> str:
    """Build the bounded SQL/error user message sent to the SAG planner."""
    error_limit = MAX_RETRY_CONTEXT_BYTES // 2
    bounded_error = bound_utf8(error, error_limit)
    error_block = f"DATABASE ERROR:\n{bounded_error}"
    fixed_bytes = len(f"SQL:\n\n\n{error_block}".encode())
    sql_budget = max(0, MAX_RETRY_CONTEXT_BYTES - fixed_bytes)
    bounded_sql = bound_utf8(sql, sql_budget)
    return f"SQL:\n{bounded_sql}\n\n{error_block}"


def _execution_repair_message(repair_context: str, error: str) -> str:
    """Give Tau's model a copyable trigger for one SAG correction pass."""
    bounded_error = bound_utf8(error, MAX_RETRY_CONTEXT_BYTES // 2)
    return (
        f"query execution failed: {bounded_error}\n\n"
        "Retry the same user question with data_knowledge_search. Pass the following "
        "block unchanged as retryContext so SAG can correct the SQL:\n"
        f"{repair_context}"
    )


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)
