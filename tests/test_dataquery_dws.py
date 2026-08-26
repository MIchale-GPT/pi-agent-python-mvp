"""DWS adapter tests with a fake psycopg driver (PRD testing decision 3).

The fake driver records setup statements and parameter bindings, simulates row
fetching (1001 rows available), and supports cancellation. Tests never touch a
real database.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from collections.abc import Sequence

import pytest

from tau_coding.dataquery.backends.dws import DwsPostgresQueryBackend
from tau_coding.dataquery.service import QueryExecutionError

pytestmark = pytest.mark.anyio


class FakeCursor:
    def __init__(self, fake_conn: FakeConnection) -> None:
        self.fake_conn = fake_conn
        self.description = [
            types.SimpleNamespace(name="id", type_code="integer"),
            types.SimpleNamespace(name="region", type_code="text"),
        ]
        self._rows = [[i, f"r{i}"] for i in range(1001)]
        self._index = 0
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.executed.append((sql, params))
        self.fake_conn.executed.append((sql, params))
        if sql.startswith("SET LOCAL"):
            return
        if params is not None and "not-json" in repr(params):
            raise ValueError("could not adapt parameter")
        self._index = 0

    def fetchone(self) -> Sequence[object] | None:
        if self.fake_conn.cancelled or self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        del exc


class FakeConnection:
    def __init__(self, cursor_factory: type[FakeCursor] = FakeCursor) -> None:
        self.read_only: bool | None = None
        self.executed: list[tuple[str, object]] = []
        self.cancelled = False
        self.closed = False
        self.rolled_back = False
        self._cursor = cursor_factory(self)

    def cursor(self) -> FakeCursor:
        return self._cursor

    def cancel(self) -> None:
        self.cancelled = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class FakePsycopg:
    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.connect_kwargs: list[dict[str, object]] = []

    def connect(self, **kwargs: object) -> FakeConnection:
        self.connect_kwargs.append(kwargs)
        conn = FakeConnection()
        self.connections.append(conn)
        return conn


@pytest.fixture
def fake_driver(monkeypatch: pytest.MonkeyPatch) -> FakePsycopg:
    driver = FakePsycopg()
    module = types.ModuleType("psycopg")
    module.connect = driver.connect
    monkeypatch.setitem(sys.modules, "psycopg", module)
    return driver


def make_backend() -> DwsPostgresQueryBackend:
    return DwsPostgresQueryBackend(
        host="dws.example.com",
        port=8000,
        database="exchange",
        username="reader",
        password="hunter2",
        sslmode="prefer",
        connect_timeout=10,
        probe_query="SELECT 1",
    )


def make_limits() -> dict[str, int]:
    return {
        "timeout_seconds": 180,
        "max_rows": 1000,
        "max_result_bytes": 1024 * 1024,
        "max_cell_bytes": 64 * 1024,
    }


async def test_execute_sets_up_read_only_transaction(fake_driver: FakePsycopg) -> None:
    backend = make_backend()
    await backend.execute(
        "SELECT region FROM myschema.orders WHERE region = %s",
        ["east"],
        **make_limits(),
    )
    conn = fake_driver.connections[0]
    assert conn.read_only is True
    setup_statements = [sql for sql, _ in conn.executed if sql.startswith("SET LOCAL")]
    assert any("statement_timeout" in sql for sql in setup_statements)
    assert any("search_path" in sql for sql in setup_statements)
    assert ("SELECT region FROM myschema.orders WHERE region = %s", ["east"]) in conn.executed
    await backend.close()


async def test_execute_rolls_back_after_driver_error(fake_driver: FakePsycopg) -> None:
    backend = make_backend()
    with pytest.raises(QueryExecutionError):
        await backend.execute(
            "SELECT region FROM myschema.orders WHERE region = %s",
            ["not-json"],
            **make_limits(),
        )
    conn = fake_driver.connections[0]
    assert conn.rolled_back is True
    await backend.close()


async def test_test_connection_uses_custom_probe_query(fake_driver: FakePsycopg) -> None:
    backend = DwsPostgresQueryBackend(
        host="dws.example.com",
        port=8000,
        database="exchange",
        username="reader",
        password="hunter2",
        sslmode="prefer",
        connect_timeout=10,
        probe_query="SELECT 2",
    )
    elapsed, error = await backend.test_connection()
    assert error is None
    assert elapsed >= 0
    conn = fake_driver.connections[0]
    assert ("SELECT 2", None) in conn.executed
    await backend.close()


async def test_row_limit_returns_1000_of_1001(fake_driver: FakePsycopg) -> None:
    backend = make_backend()
    result = await backend.execute("SELECT * FROM myschema.orders", [], **make_limits())
    assert len(result.rows) == 1000
    assert result.truncated is True
    assert result.truncation_reasons == ["row_limit"]
    await backend.close()


async def test_byte_and_cell_limits_applied(fake_driver: FakePsycopg) -> None:
    backend = make_backend()
    limits = dict(make_limits())
    limits["max_result_bytes"] = 64
    result = await backend.execute("SELECT * FROM myschema.orders", [], **limits)
    assert result.truncated is True
    assert "byte_limit" in result.truncation_reasons
    await backend.close()


class _BoomingCursor(FakeCursor):
    def execute(self, sql: str, params: object = None) -> None:
        raise RuntimeError("connection to dws.example.com password=hunter2 failed")


async def test_error_is_sanitized(fake_driver: FakePsycopg) -> None:
    backend = make_backend()
    backend._connection = FakeConnection(cursor_factory=_BoomingCursor)
    with pytest.raises(QueryExecutionError) as exc_info:
        await backend.execute("SELECT * FROM myschema.orders", [], **make_limits())
    message = str(exc_info.value)
    assert "hunter2" not in message
    assert "password" not in message.lower()
    await backend.close()


class _BlockingCursor(FakeCursor):
    """Blocks on fetchone until the driver connection is cancelled."""

    def fetchone(self) -> Sequence[object] | None:
        deadline = time.monotonic() + 5
        while not self.fake_conn.cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        return None


class _MutableSignal:
    def __init__(self) -> None:
        self.cancelled_flag = False

    def is_cancelled(self) -> bool:
        return self.cancelled_flag

    def cancel(self) -> None:
        self.cancelled_flag = True


async def test_cancellation_calls_driver_cancel(fake_driver: FakePsycopg) -> None:
    backend = DwsPostgresQueryBackend(host="h", port=1, database="d", username="u", password="p")
    backend._connection = FakeConnection(cursor_factory=_BlockingCursor)
    signal = _MutableSignal()

    task = asyncio.create_task(
        backend.execute("SELECT * FROM myschema.orders", [], **make_limits(), signal=signal)
    )
    await asyncio.sleep(0.1)
    signal.cancel()
    with pytest.raises(QueryExecutionError, match="query cancelled"):
        await task

    assert backend._connection.cancelled is True
    await backend.close()


async def test_test_connection_probe(fake_driver: FakePsycopg) -> None:
    backend = make_backend()
    elapsed, error = await backend.test_connection()
    assert error is None
    assert elapsed >= 0
    assert fake_driver.connections[0].read_only is True
    await backend.close()


async def test_missing_driver_raises_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)
    backend = DwsPostgresQueryBackend(host="h", port=1, database="d", username="u", password="p")
    with pytest.raises(QueryExecutionError, match="psycopg"):
        await backend.execute("SELECT 1 FROM myschema.orders", [], **make_limits())


def test_driver_threading_isolation(fake_driver: FakePsycopg) -> None:
    """conn.cancel() is safe to call from a different thread."""
    conn = FakeConnection()
    thread = threading.Thread(target=conn.cancel)
    thread.start()
    thread.join(timeout=2)
    assert conn.cancelled is True
