"""SQL policy checker for frozen query plans (decision 10).

The checker parses the candidate SQL with sqlglot's PostgreSQL dialect and
enforces a fail-closed allowlist:

- exactly one statement, whose root is a read-only ``SELECT`` (or ``UNION`` of
  selects); multi-statement, DDL, DML, ``SELECT INTO``, locking clauses and
  unknown/unsupported syntax are rejected;
- every table reference must be schema-qualified and belong to the configured
  allowed-object set (decision 7);
- function calls are restricted to an allowlist of known-safe built-ins;
  unknown functions, unapproved schema-qualified calls and a denylist of
  dangerous functions are rejected.

The parser is an early interception layer only: the real boundary remains the
database-level read-only account, minimal object privileges and the read-only
transaction.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlglot import exp, parse, tokenize
from sqlglot.errors import SqlglotError

from tau_coding.dataquery.config import DataQueryConfigError

# --- function policy (decision 10) ----------------------------------------

# Approved built-in functions (normalized uppercase). The model should stick to
# core aggregate / numeric / text / date / window functions.
APPROVED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # aggregates
        "COUNT",
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "STDDEV",
        "STDDEV_SAMP",
        "STDDEV_POP",
        "VARIANCE",
        "VAR_SAMP",
        "VAR_POP",
        "BOOL_AND",
        "BOOL_OR",
        "EVERY",
        "ARRAY_AGG",
        "STRING_AGG",
        "JSON_AGG",
        "JSONB_AGG",
        "CORR",
        "COVAR_POP",
        "COVAR_SAMP",
        # numeric
        "ABS",
        "CEIL",
        "CEILING",
        "FLOOR",
        "ROUND",
        "TRUNC",
        "SIGN",
        "POWER",
        "SQRT",
        "EXP",
        "LN",
        "LOG",
        "MOD",
        "GREATEST",
        "LEAST",
        # text
        "LENGTH",
        "CHAR_LENGTH",
        "CHARACTER_LENGTH",
        "UPPER",
        "LOWER",
        "INITCAP",
        "TRIM",
        "BTRIM",
        "LTRIM",
        "RTRIM",
        "LPAD",
        "RPAD",
        "SUBSTRING",
        "SUBSTR",
        "LEFT",
        "RIGHT",
        "REPLACE",
        "TRANSLATE",
        "REPEAT",
        "REVERSE",
        "POSITION",
        "STRPOS",
        "SPLIT_PART",
        "CONCAT",
        "CONCAT_WS",
        "FORMAT",
        "MD5",
        # date / time
        "NOW",
        "CURRENT_TIMESTAMP",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "TIMESTAMP_TRUNC",
        "DATE_TRUNC",
        "DATE_PART",
        "EXTRACT",
        "AGE",
        "TO_CHAR",
        "TO_DATE",
        "TO_TIMESTAMP",
        "DATE",
        # type conversion (safe, no side effects)
        "CAST",
        "COALESCE",
        "NULLIF",
        # window
        "ROW_NUMBER",
        "RANK",
        "DENSE_RANK",
        "NTILE",
        "LAG",
        "LEAD",
        "FIRST_VALUE",
        "LAST_VALUE",
        "NTH_VALUE",
        "CUME_DIST",
        "PERCENT_RANK",
        # misc safe
        "GENERATE_SERIES",
    }
)

# Dangerous functions (denylist, decision 10). Even when the DB role cannot
# actually execute them, they are rejected at the application layer.
DANGEROUS_FUNCTIONS: frozenset[str] = frozenset(
    {
        "PG_SLEEP",
        "PG_TERMINATE_BACKEND",
        "PG_CANCEL_BACKEND",
        "PG_READ_FILE",
        "PG_READ_BINARY_FILE",
        "PG_LS_DIR",
        "PG_RELOAD_CONF",
        "PG_ROTATE_LOGFILE",
        "PG_START_BACKUP",
        "PG_STOP_BACKUP",
        "PG_LOG_BACKEND_STATUS",
        "PG_WRITE_FILE",
        "LO_EXPORT",
        "LO_IMPORT",
        "LO_CREATE",
        "LO_UNLINK",
        "LO_OPEN",
        "LO_CLOSE",
        "LO_READ",
        "LO_WRITE",
        "LO_TRUNCATE",
        "DBLINK",
        "DBLINK_CONNECT",
        "DBLINK_CONNECT_U",
        "DBLINK_DISCONNECT",
        "DBLINK_EXEC",
        "DBLINK_GET_RESULT",
        "DBLINK_SEND_QUERY",
        "DBLINK_OPEN",
        "DBLINK_FETCH",
        "DBLINK_GET_NOTIFY",
        "COPY",
        "PG_CREATE_RESTORE_POINT",
        "TXID_CURRENT",
        "TXID_CURRENT_SNAPSHOT",
        "PG_GET_SNAPSHOT",
        "PG_EXPORT_SNAPSHOT",
        "PG_IMPORT_SNAPSHOT",
        "PG_ADVISORY_LOCK",
        "PG_ADVISORY_UNLOCK",
        "PG_ADVISORY_XACT_LOCK",
    }
)

# Non-query statement classes that must never appear anywhere in the AST
# (including inside CTEs and subqueries).
_NON_READ_ONLY_STATEMENTS = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Alter,
    exp.Drop,
    exp.Command,
    exp.Set,
    exp.Copy,
    exp.TruncateTable,
    exp.Use,
    exp.Grant,
    exp.Revoke,
    exp.Lock,
    exp.Execute,
    exp.Commit,
    exp.Rollback,
    exp.Transaction,
    exp.Kill,
)

# exp.Func subclasses that are syntax forms rather than function calls (AND/OR
# connectors, CASE, CAST, ARRAY constructors, EXISTS predicates, JSON
# operators, intervals).
_NON_CALL_FUNCTION_FORMS = (
    exp.Connector,
    exp.Case,
    exp.If,
    exp.Cast,
    exp.Array,
    exp.Exists,
    exp.JSONExtract,
    exp.JSONExtractScalar,
    exp.JSONBExtract,
    exp.JSONBExtractScalar,
    exp.JSONBContains,
    exp.JSONObject,
    exp.JSONArray,
    exp.Interval,
)

_QUALIFIED_FUNCTION_CALL = re.compile(r"(\w+)\.(\w+)\s*\(", re.IGNORECASE)

# System schemas always readable (metadata catalogs, no user data). They are
# implicitly allowed regardless of the configured object allowlist so schema
# introspection works without deployment changes.
SYSTEM_SCHEMAS: frozenset[str] = frozenset({"information_schema", "pg_catalog"})


def is_schema_introspection_query(sql: str) -> bool:
    """Return whether every concrete relation belongs to a system schema.

    This is the sole evidence exemption.  A table-free ``SELECT 1`` and a query
    mixing catalog and business relations both return ``False``.
    """
    try:
        statements = parse(sql, read="postgres")
    except Exception:  # noqa: BLE001 - the policy validator reports parse details
        return False
    if len(statements) != 1:
        return False
    root = statements[0]
    if root is None:
        return False
    cte_names = {cte.alias for cte in root.find_all(exp.CTE)}
    relations = [table for table in root.find_all(exp.Table) if table.name not in cte_names]
    return bool(relations) and all(
        bool(table.db) and table.db.lower() in SYSTEM_SCHEMAS for table in relations
    )


@dataclass(frozen=True, slots=True)
class AllowedObjects:
    """Deployment-configured object allowlist (decision 7 / further notes).

    ``schemas`` allows every table in a schema; ``tables`` allows specific
    ``schema.table`` pairs. An empty allowlist permits nothing (fail-closed).
    """

    schemas: frozenset[str] = frozenset()
    tables: frozenset[tuple[str, str]] = frozenset()

    def allows(self, schema: str, table: str) -> bool:
        if schema in self.schemas:
            return True
        return (schema, table) in self.tables

    def allows_unqualified(self, table: str) -> bool:
        """Return whether an unqualified reference may resolve to allowed objects.

        Only meaningful when schema-qualification is not required: an
        unqualified name is permitted if any schema is wildcard-allowlisted or
        the name appears in the explicit table allowlist.
        """
        if self.schemas:
            return True
        return any(name == table for _schema, name in self.tables)

    @classmethod
    def parse(cls, entries: Iterable[str]) -> AllowedObjects:
        schemas: set[str] = set()
        tables: set[tuple[str, str]] = set()
        for entry in entries:
            normalized = entry.strip()
            if not normalized:
                continue
            parts = normalized.split(".")
            if len(parts) == 1:
                schemas.add(parts[0].lower())
            elif len(parts) == 2:
                tables.add((parts[0].lower(), parts[1].lower()))
            else:
                raise DataQueryConfigError(
                    f"invalid allowed-object entry (expected `schema` or `schema.table`): {entry}"
                )
        return cls(schemas=frozenset(schemas), tables=frozenset(tables))


class SqlPolicyError(ValueError):
    """Raised when SQL is rejected by the policy checker."""


@dataclass(frozen=True, slots=True)
class SqlPolicyReport:
    """Policy verdict plus structured reason for auditing."""

    allowed: bool
    reason: str | None = None

    @classmethod
    def ok(cls) -> SqlPolicyReport:
        return cls(allowed=True)

    @classmethod
    def reject(cls, reason: str) -> SqlPolicyReport:
        return cls(allowed=False, reason=reason)


class SqlPolicyChecker:
    """Fail-closed SQL validator for frozen query plans."""

    def __init__(
        self,
        *,
        allowed_objects: AllowedObjects | None = None,
        approved_functions: Sequence[str] | frozenset[str] = tuple(APPROVED_FUNCTIONS),
        require_schema_qualified_tables: bool = True,
    ) -> None:
        self._allowed_objects = allowed_objects or AllowedObjects()
        self._approved_functions = frozenset(name.upper() for name in approved_functions)
        self._require_schema_qualified_tables = require_schema_qualified_tables

    def validate(self, sql: str) -> SqlPolicyReport:
        """Validate a single SQL statement, returning an allow/reject report."""
        if not sql or not sql.strip():
            return SqlPolicyReport.reject("empty SQL")
        try:
            statements = parse(sql, read="postgres")
        except SqlglotError as exc:
            return SqlPolicyReport.reject(f"SQL parse failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - any parser failure is fail-closed
            return SqlPolicyReport.reject(f"SQL parse failed: {type(exc).__name__}")

        if len(statements) != 1:
            return SqlPolicyReport.reject("multi-statement SQL is not allowed")
        root = statements[0]
        if not isinstance(root, (exp.Select, exp.Union, exp.Subquery)):
            return SqlPolicyReport.reject(
                f"only read-only SELECT queries are allowed (root is {type(root).__name__})"
            )

        for node in root.walk():
            if isinstance(node, _NON_READ_ONLY_STATEMENTS):
                return SqlPolicyReport.reject(
                    f"statement not allowed inside query: {type(node).__name__}"
                )

        if isinstance(root, exp.Select):
            locks = root.args.get("locks")
            if locks:
                return SqlPolicyReport.reject(
                    "locking clauses (FOR UPDATE / FOR SHARE) are rejected"
                )
            if root.args.get("into") is not None:
                return SqlPolicyReport.reject("SELECT INTO is not allowed")

        tables = list(root.find_all(exp.Table))
        cte_names = {cte.alias for cte in root.find_all(exp.CTE)}
        for table in tables:
            schema = table.db
            name = table.name
            if name in cte_names:
                continue
            if not schema:
                if self._require_schema_qualified_tables:
                    return SqlPolicyReport.reject(
                        f"table reference must be schema-qualified: {name}"
                    )
                if not self._allowed_objects.allows_unqualified(name):
                    return SqlPolicyReport.reject(f"table not in allowed objects: {name}")
            elif schema.lower() in SYSTEM_SCHEMAS:
                continue
            elif not self._allowed_objects.allows(schema, name):
                return SqlPolicyReport.reject(f"table not in allowed objects: {table.sql()}")

        qualified_report = self._check_qualified_function_calls(sql)
        if not qualified_report.allowed:
            return qualified_report

        for func in root.find_all(exp.Func):
            if isinstance(func, _NON_CALL_FUNCTION_FORMS):
                continue
            name = self._function_name(func)
            if name in DANGEROUS_FUNCTIONS:
                return SqlPolicyReport.reject(f"dangerous function is rejected: {name}")
            if name not in self._approved_functions:
                return SqlPolicyReport.reject(f"function is not approved: {name}")

        return SqlPolicyReport.ok()

    def _check_qualified_function_calls(self, sql: str) -> SqlPolicyReport:
        """Reject schema-qualified function calls that are not explicitly approved.

        sqlglot drops the schema of unknown functions (``pg_catalog.count``
        parses as a plain ``COUNT``), so qualification is detected on the raw
        token stream (decision 10: unapproved schema-qualified functions are
        rejected). Token-based detection keeps string literals from producing
        false rejections.
        """
        try:
            tokens = list(tokenize(sql, read="postgres"))
        except SqlglotError:
            return SqlPolicyReport.reject("SQL tokenization failed")
        approved = {name.upper() for name in self._approved_functions}
        for index in range(len(tokens) - 3):
            first, dot, second, paren = (
                tokens[index],
                tokens[index + 1],
                tokens[index + 2],
                tokens[index + 3],
            )
            if dot.text != "." or paren.text != "(":
                continue
            qualified = f"{first.text}.{second.text}"
            if qualified.upper() not in approved:
                return SqlPolicyReport.reject(
                    f"schema-qualified function is not approved: {qualified}"
                )
        return SqlPolicyReport.ok()

    @staticmethod
    def _function_name(func: exp.Func) -> str:
        """Return the normalized function name for a function node."""
        if isinstance(func, exp.Anonymous):
            raw = func.this
            if isinstance(raw, str):
                return raw.strip().upper()
        return func.sql_name().upper() or type(func).__name__.upper()
