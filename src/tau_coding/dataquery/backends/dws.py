"""DWS read-only query backend (PostgreSQL wire protocol, decision 8).

The backend uses the psycopg driver (v3) when installed. The driver is imported
lazily so sessions without the optional dependency still start; calls raise a
sanitized :class:`QueryExecutionError` instead.

Security posture (decision 9/10/17):

- every execution runs in a database-forced read-only transaction;
- ``statement_timeout`` and a safe ``search_path`` are set per transaction;
- results are fetched row-by-row and bounded by the shared truncation logic;
- cancellation runs a monitor task that calls ``conn.cancel()`` from another
  thread instead of waiting for the loop to give up.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import time
from collections.abc import Sequence
from contextlib import suppress
from typing import Any, Protocol, cast

from tau_agent.tools import ToolCancellationToken
from tau_coding.dataquery.backends.base import (
    QueryBackend,
    QueryColumn,
    QueryResult,
    truncate_result_rows,
)
from tau_coding.dataquery.service import (
    QueryExecutionError,
    QueryInfrastructureError,
    QuerySqlError,
)

logger = logging.getLogger(__name__)

DRIVER_MISSING_MESSAGE = (
    "psycopg driver is not installed; install the optional dataquery dependency "
    "to enable DWS queries"
)


class ColumnDescription(Protocol):
    name: str
    type_code: object


class DriverCursor(Protocol):
    description: Sequence[ColumnDescription] | None

    def execute(self, sql: str, params: object = None) -> None: ...
    def fetchone(self) -> Sequence[object] | None: ...
    def __enter__(self) -> DriverCursor: ...
    def __exit__(self, *exc: object) -> None: ...


class DriverConnection(Protocol):
    read_only: bool

    def cursor(self) -> DriverCursor: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...


class DwsPostgresQueryBackend(QueryBackend):
    """Lazy single-connection DWS backend with per-execution read-only setup."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        username: str,
        password: str,
        sslmode: str = "prefer",
        connect_timeout: int = 10,
        probe_query: str = "SELECT 1",
    ) -> None:
        self._params = {
            "host": host,
            "port": port,
            "dbname": database,
            "user": username,
            "password": password,
            "sslmode": sslmode,
            "connect_timeout": connect_timeout,
        }
        self._probe_query = probe_query
        self._connection: DriverConnection | None = None
        self._connection_lock = asyncio.Lock()

    async def execute(
        self,
        sql: str,
        params: Sequence[object],
        *,
        timeout_seconds: int,
        max_rows: int,
        max_result_bytes: int,
        max_cell_bytes: int,
        signal: ToolCancellationToken | None = None,
    ) -> QueryResult:
        conn = await self._connection_or_raise()
        started = time.monotonic()
        monitor: asyncio.Task[None] | None = None
        if signal is not None:
            monitor = asyncio.create_task(self._watch_cancel(signal, conn))
        try:
            outcome = await asyncio.to_thread(
                self._run_statement,
                conn,
                sql,
                list(params),
                timeout_seconds,
                max_rows,
                max_result_bytes,
                max_cell_bytes,
            )
            if signal is not None and signal.is_cancelled():
                raise QueryExecutionError("query cancelled")
        except Exception:
            await asyncio.to_thread(_rollback_connection, conn)
            raise
        finally:
            if monitor is not None:
                monitor.cancel()
        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        outcome.statement = sql
        return outcome

    async def _watch_cancel(self, signal: ToolCancellationToken, conn: DriverConnection) -> None:
        """Poll the cancellation token and interrupt the statement (decision 17)."""
        while True:
            await asyncio.sleep(0.05)
            if signal.is_cancelled():
                _cancel(conn)
                return

    def _run_statement(
        self,
        conn: DriverConnection,
        sql: str,
        params: list[object],
        timeout_seconds: int,
        max_rows: int,
        max_result_bytes: int,
        max_cell_bytes: int,
    ) -> QueryResult:
        try:
            return _run_with_cursor(
                conn,
                sql,
                params,
                timeout_seconds,
                max_rows,
                max_result_bytes,
                max_cell_bytes,
            )
        except QueryExecutionError:
            raise
        except Exception as exc:  # noqa: BLE001 - driver isolation
            error_detail = _extract_psycopg_error_detail(exc)
            error_message = _sanitize_driver_error(
                error_detail.get("message", ""),
                secrets=(str(self._params.get("password") or ""),),
            )
            logger.warning(
                "DWS query failed: sqlstate=%s message=%s sql=%s param_count=%s",
                error_detail.get("sqlstate"),
                error_message,
                sql[:500] + "..." if len(sql) > 500 else sql,
                len(params),
            )
            raise QuerySqlError(f"query failed: {type(exc).__name__}: {error_message}") from exc

    async def test_connection(self) -> tuple[int, str | None]:
        started = time.monotonic()
        try:
            conn = await self._connection_or_raise()
            await asyncio.to_thread(_probe, conn, self._probe_query)
        except QueryExecutionError as exc:
            return int((time.monotonic() - started) * 1000), str(exc)
        return int((time.monotonic() - started) * 1000), None

    async def close(self) -> None:
        conn = self._connection
        self._connection = None
        if conn is not None:
            await asyncio.to_thread(_close_connection, conn)

    async def _connection_or_raise(self) -> DriverConnection:
        if self._connection is not None:
            return self._connection
        async with self._connection_lock:
            if self._connection is not None:
                return self._connection
            try:
                self._connection = await asyncio.to_thread(_connect, self._params)
            except QueryExecutionError:
                raise
            except Exception as exc:  # noqa: BLE001 - driver isolation
                raise QueryInfrastructureError(
                    f"database connection failed: {type(exc).__name__}"
                ) from exc
            return self._connection


def _ensure_driver() -> None:
    import importlib.util
    import sys

    if sys.modules.get("psycopg") is None:
        try:
            available = importlib.util.find_spec("psycopg") is not None
        except (ImportError, ValueError):
            available = False
        if not available:
            raise QueryInfrastructureError(DRIVER_MISSING_MESSAGE)


def _connect(params: dict[str, object]) -> DriverConnection:
    try:
        psycopg_module = importlib.import_module("psycopg")
    except ImportError as exc:
        raise QueryInfrastructureError(DRIVER_MISSING_MESSAGE) from exc
    connect = cast(Any, psycopg_module).connect
    return cast(DriverConnection, connect(**params))


def _close_connection(conn: DriverConnection) -> None:
    with suppress(Exception):  # noqa: BLE001 - best-effort shutdown
        conn.close()


def _rollback_connection(conn: DriverConnection) -> None:
    with suppress(Exception):  # noqa: BLE001 - best-effort recovery
        rollback = getattr(conn, "rollback", None)
        if callable(rollback):
            rollback()


def _probe(conn: DriverConnection, probe_query: str = "SELECT 1") -> None:
    _ensure_driver()
    try:
        with conn.cursor() as cursor:
            conn.read_only = True
            cursor.execute(probe_query)
            cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 - driver isolation
        raise QueryInfrastructureError(f"connection probe failed: {type(exc).__name__}") from exc


def _driver_sql(sql: str) -> str:
    """Escape literal percent signs only at the psycopg binding boundary."""
    from sqlglot import Tokenizer

    tokens = Tokenizer(dialect="postgres").tokenize(sql)
    placeholders = {
        a.start
        for a, b in zip(tokens, tokens[1:], strict=False)
        if a.text == "%" and b.text == "s" and a.end + 1 == b.start
    }
    return "".join(
        "%%" if char == "%" and index not in placeholders else char
        for index, char in enumerate(sql)
    )


def _run_with_cursor(
    conn: DriverConnection,
    sql: str,
    params: list[object],
    timeout_seconds: int,
    max_rows: int,
    max_result_bytes: int,
    max_cell_bytes: int,
) -> QueryResult:
    _ensure_driver()
    _rollback_connection(conn)
    conn.read_only = True
    with conn.cursor() as cursor:
        cursor.execute(f"SET LOCAL statement_timeout = '{timeout_seconds}s'")
        cursor.execute("SET LOCAL search_path = pg_catalog")
        # psycopg parses literal percent signs whenever a params argument is supplied.
        if params:
            cursor.execute(_driver_sql(sql), params)
        else:
            cursor.execute(sql)
        columns = [
            QueryColumn(name=column.name, type=str(column.type_code))
            for column in cursor.description or ()
        ]
        rows: list[list[object]] = []
        for _ in range(max_rows + 1):
            row = cursor.fetchone()
            if row is None:
                break
            rows.append([_adapt_cell(value) for value in row])
        outcome = truncate_result_rows(
            rows,
            max_rows=max_rows,
            max_result_bytes=max_result_bytes,
            max_cell_bytes=max_cell_bytes,
        )
    return QueryResult(
        columns=columns,
        rows=outcome.rows,
        truncated=outcome.truncated,
        truncation_reasons=list(outcome.reasons),
        previewed_cells=outcome.previewed_cells,
    )


def _cancel(conn: DriverConnection) -> None:
    """Interrupt a running statement from another thread (decision 17)."""
    with suppress(Exception):  # noqa: BLE001 - best-effort cancel
        conn.cancel()


def _extract_psycopg_error_detail(exc: Exception) -> dict[str, str]:
    detail: dict[str, str] = {}
    for attr, key in (
        ("sqlstate", "sqlstate"),
        ("pgcode", "sqlstate"),
        ("message", "message"),
        ("pgerror", "message"),
        ("diag.message_primary", "message"),
    ):
        value: object | None = None
        if attr == "diag.message_primary":
            diag = getattr(exc, "diag", None)
            value = getattr(diag, "message_primary", None) if diag is not None else None
        else:
            value = getattr(exc, attr, None)
        if isinstance(value, str) and value.strip() and key not in detail:
            detail[key] = value.strip()
    if "message" not in detail:
        text = str(exc).strip()
        if text:
            detail["message"] = text
    return detail


def _sanitize_driver_error(text: str, *, secrets: Sequence[str]) -> str:
    """Bound driver diagnostics without exposing credentials or parameter values."""
    sanitized = text
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[redacted]")
    sanitized = re.sub(
        r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*\S+",
        "[redacted]",
        sanitized,
    )
    sanitized = re.sub(r"(://)([^/@\s]+)(@)", r"\1[redacted]\3", sanitized)
    return sanitized or "database operation failed"


def _adapt_cell(value: object) -> object:
    """Convert driver types to JSON-safe values (bytes stay hex-ish strings)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, bytes):
        return f"\\x{value.hex()}"
    return str(value)
