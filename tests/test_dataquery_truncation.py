"""Truncation semantics tests (PRD testing decision 3, truncation part)."""

from __future__ import annotations

from tau_coding.dataquery.backends.base import serialize_cell, truncate_result_rows


def make_rows(count: int) -> list[list[object]]:
    return [[i, f"row-{i}"] for i in range(count)]


def test_row_limit_truncates_at_max_rows() -> None:
    outcome = truncate_result_rows(
        make_rows(1001),
        max_rows=1000,
        max_result_bytes=1024 * 1024,
        max_cell_bytes=64 * 1024,
    )
    assert len(outcome.rows) == 1000
    assert outcome.truncated is True
    assert outcome.reasons == ("row_limit",)


def test_no_truncation_when_fewer_rows_than_limit() -> None:
    outcome = truncate_result_rows(
        make_rows(3),
        max_rows=1000,
        max_result_bytes=1024 * 1024,
        max_cell_bytes=64 * 1024,
    )
    assert len(outcome.rows) == 3
    assert outcome.truncated is False
    assert outcome.reasons == ()


def test_row_limit_not_flagged_when_no_extra_rows() -> None:
    outcome = truncate_result_rows(
        make_rows(1000),
        max_rows=1000,
        max_result_bytes=1024 * 1024,
        max_cell_bytes=64 * 1024,
    )
    assert outcome.truncated is False
    assert outcome.reasons == ()


def test_byte_limit_drops_incomplete_rows() -> None:
    rows = make_rows(50)
    outcome = truncate_result_rows(
        rows,
        max_rows=1000,
        max_result_bytes=64,
        max_cell_bytes=64 * 1024,
    )
    assert outcome.truncated is True
    assert "byte_limit" in outcome.reasons
    # No half row: every kept row is complete.
    assert outcome.rows
    kept_bytes = sum(len(cell.encode("utf-8")) for row in outcome.rows for cell in row)
    assert kept_bytes <= 64


def test_cell_limit_returns_preview_marker() -> None:
    big = "x" * (70 * 1024)
    outcome = truncate_result_rows(
        [["id", big]],
        max_rows=1000,
        max_result_bytes=1024 * 1024,
        max_cell_bytes=64 * 1024,
    )
    assert outcome.truncated is True
    assert "cell_limit" in outcome.reasons
    assert outcome.previewed_cells == 1
    assert "omitted" in outcome.rows[0][1]
    assert len(outcome.rows[0][1]) < 70 * 1024


def test_cell_limit_without_row_overflow_keeps_row() -> None:
    outcome = truncate_result_rows(
        [["id", "x" * (100 * 1024)]],
        max_rows=1000,
        max_result_bytes=1024 * 1024,
        max_cell_bytes=64 * 1024,
    )
    assert len(outcome.rows) == 1
    assert outcome.truncated is True
    assert outcome.reasons == ("cell_limit",)


def test_single_row_exceeding_byte_budget_is_dropped() -> None:
    outcome = truncate_result_rows(
        [[f"row-{i}" for i in range(200)]],
        max_rows=1000,
        max_result_bytes=256,
        max_cell_bytes=8 * 1024,
    )
    assert outcome.rows == []
    assert outcome.truncated is True
    assert "byte_limit" in outcome.reasons


def test_multiple_reasons_can_coexist() -> None:
    outcome = truncate_result_rows(
        make_rows(1001),
        max_rows=10,
        max_result_bytes=1024,
        max_cell_bytes=64 * 1024,
    )
    assert "row_limit" in outcome.reasons
    # bytes may trip before the row limit is even reached
    assert outcome.truncated is True


def test_serialize_cell_handles_common_types() -> None:
    assert serialize_cell(None) == "NULL"
    assert serialize_cell(True) == "true"
    assert serialize_cell(3) == "3"
    assert serialize_cell(1.5) == "1.5"
    assert serialize_cell("text") == "text"
    assert serialize_cell({"a": 1}) == '{"a": 1}'


def test_result_protocol_preserves_null_distinct_from_literal_null_text():
    result = truncate_result_rows(
        [[None, "NULL", 0]], max_rows=10, max_result_bytes=100, max_cell_bytes=10
    )
    assert result.rows == [[None, "NULL", "0"]]
