"""Run-scoped evidence ledger and frozen query-plan store (decision 5/6).

All handles are opaque, server-generated, at-least-128-bit random values. Every
lookup validates the caller's session + run + state-machine stage so stale or
guessed handles fail closed, including after ``agent_start`` clears the scope.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime


class LedgerError(ValueError):
    """Raised when a handle is unknown, stale or used out of stage order."""


def new_handle(prefix: str) -> str:
    """Return a 128-bit opaque handle with a human-readable prefix."""
    return f"{prefix}_{secrets.token_hex(16)}"


def sql_fingerprint(sql: str) -> str:
    """Return a short, stable fingerprint of the frozen SQL text."""
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class RunScope:
    """Identity of one agent run (decision 30)."""

    run_id: str
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    title: str
    summary: str
    provider_id: str | None = None
    provider_source_id: str | None = None
    expandable: bool = True


@dataclass(frozen=True, slots=True)
class BundleRecord:
    bundle_id: str
    evidence: dict[str, EvidenceRecord]
    total_bytes: int
    created_ms: int


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """Frozen plan created by prepare (decision 6)."""

    plan_id: str
    run_id: str
    session_id: str | None
    bundle_id: str | None
    evidence_ids: tuple[str, ...]
    sql: str
    params: tuple[object, ...]
    param_count: int
    param_types: tuple[str, ...]
    policy_version: str
    sql_fingerprint: str
    created_ms: int


@dataclass(slots=True)
class EvidenceLedger:
    """Run-scoped evidence state.

    ``agent_start`` resets the ledger to a new run scope. Evidence and bundles
    from previous runs are unreachable: every read requires the current scope.
    """

    scope: RunScope = field(default_factory=lambda: RunScope(run_id=new_handle("run")))
    policy_version: str = "sqlglot-postgres-v1"

    _bundles: dict[str, BundleRecord] = field(default_factory=dict)
    _evidence_to_bundle: dict[str, str] = field(default_factory=dict)

    def reset_scope(self, *, run_id: str | None = None, session_id: str | None = None) -> RunScope:
        """Start a new run scope and drop all prior evidence state."""
        self._bundles.clear()
        self._evidence_to_bundle.clear()
        self.scope = RunScope(
            run_id=run_id or new_handle("run"),
            session_id=session_id,
        )
        return self.scope

    @property
    def current(self) -> RunScope:
        return self.scope

    def register_evidence(self, records: Sequence[EvidenceRecord], *, total_bytes: int) -> str:
        """Create a bundle for the current run and return its opaque id."""
        bundle_id = new_handle("bundle")
        self._bundles[bundle_id] = BundleRecord(
            bundle_id=bundle_id,
            evidence={record.evidence_id: record for record in records},
            total_bytes=total_bytes,
            created_ms=_now_ms(),
        )
        for record in records:
            self._evidence_to_bundle[record.evidence_id] = bundle_id
        return bundle_id

    def bundle(self, bundle_id: str, *, scope: RunScope | None = None) -> BundleRecord:
        """Return a bundle belonging to the current scope."""
        record = self._bundles.get(bundle_id)
        if record is None:
            raise LedgerError("bundle is unknown or belongs to a previous run")
        self._assert_scope(scope)
        return record

    def evidence_in_bundle(
        self, bundle_id: str, evidence_id: str, *, scope: RunScope | None = None
    ) -> bool:
        """Return whether an evidence id belongs to the current bundle."""
        bundle = self.bundle(bundle_id, scope=scope)
        return evidence_id in bundle.evidence

    def bundle_for_evidence(self, evidence_id: str, *, scope: RunScope | None = None) -> str:
        bundle_id = self._evidence_to_bundle.get(evidence_id)
        if bundle_id is None:
            raise LedgerError("evidence is unknown or belongs to a previous run")
        self._assert_scope(scope)
        return bundle_id

    def _assert_scope(self, scope: RunScope | None) -> None:
        active = scope if scope is not None else self.scope
        if active.run_id != self.scope.run_id:
            raise LedgerError("ledger handle belongs to a previous run")
        if self.scope.session_id is not None and active.session_id != self.scope.session_id:
            raise LedgerError("ledger handle belongs to a different session")


@dataclass(slots=True)
class QueryPlanStore:
    """Run-scoped frozen plans keyed by opaque plan id."""

    _plans: dict[str, QueryPlan] = field(default_factory=dict)

    def reset_scope(self) -> None:
        self._plans.clear()

    def put(self, plan: QueryPlan) -> None:
        self._plans[plan.plan_id] = plan

    def get(self, plan_id: str, *, scope: RunScope) -> QueryPlan:
        plan = self._plans.get(plan_id)
        if plan is None:
            raise LedgerError("plan is unknown or belongs to a previous run")
        if plan.run_id != scope.run_id:
            raise LedgerError("plan belongs to a previous run")
        if scope.session_id is not None and plan.session_id != scope.session_id:
            raise LedgerError("plan belongs to a different session")
        return plan


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def summarize_params(params: Sequence[object]) -> tuple[int, tuple[str, ...]]:
    """Return the parameter summary (count + generic types, never values).

    Decision 6: the summary carries no values, prefixes or plain hashes; the
    frozen plan itself keeps the values for execution only.
    """
    return len(params), tuple(_param_type(value) for value in params)


def _param_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "numeric"
    if isinstance(value, str):
        return "text"
    if isinstance(value, (list, dict)):
        return "json"
    if isinstance(value, bytes):
        return "bytea"
    return "text"
