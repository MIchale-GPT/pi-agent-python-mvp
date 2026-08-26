"""Project-local ``.env`` loading tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from tau_coding.env import load_env_file


def _write_env(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_simple_entries(tmp_path: Path) -> None:
    _write_env(tmp_path / ".env", "TAU_DWS_ALLOWED_OBJECTS=exchange_service\nFOO=bar\n")
    env: dict[str, str] = {}
    loaded = load_env_file(tmp_path, env=env)
    assert loaded == tmp_path / ".env"
    assert env == {"TAU_DWS_ALLOWED_OBJECTS": "exchange_service", "FOO": "bar"}


def test_searches_ancestor_directories(tmp_path: Path) -> None:
    _write_env(tmp_path / ".env", "FOO=from_root\n")
    subdir = tmp_path / "nested" / "deeper"
    subdir.mkdir(parents=True)
    env: dict[str, str] = {}
    assert load_env_file(subdir, env=env) == tmp_path / ".env"
    assert env["FOO"] == "from_root"


def test_existing_environment_wins(tmp_path: Path) -> None:
    _write_env(tmp_path / ".env", "FOO=from_file\n")
    env = {"FOO": "from_process"}
    load_env_file(tmp_path, env=env)
    assert env["FOO"] == "from_process"


def test_parses_export_quotes_and_comments(tmp_path: Path) -> None:
    _write_env(
        tmp_path / ".env",
        "\n".join(
            [
                "# full-line comment",
                "export EXPORTED=1",
                'QUOTED="value with spaces"',
                "SINGLE='single'",
                "INLINE=value # trailing comment",
                "NO_EQUALS_IGNORED",
                "   =ignored",
                "",
            ]
        ),
    )
    env: dict[str, str] = {}
    load_env_file(tmp_path, env=env)
    assert env == {
        "EXPORTED": "1",
        "QUOTED": "value with spaces",
        "SINGLE": "single",
        "INLINE": "value",
    }


def test_missing_file_returns_none(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    assert load_env_file(tmp_path, env=env) is None
    assert env == {}


def test_writes_to_os_environ_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setenv("ALREADY_SET", "keep")
    _write_env(tmp_path / ".env", "ALREADY_SET=overwrite\nNEW=added\n")
    with monkeypatch.context() as ctx:
        ctx.chdir(tmp_path)
        assert load_env_file() == tmp_path / ".env"
    assert os.environ["NEW"] == "added"
    assert os.environ["ALREADY_SET"] == "keep"


def test_idempotent_across_calls(tmp_path: Path) -> None:
    _write_env(tmp_path / ".env", "FOO=bar\n")
    env: dict[str, str] = {}
    load_env_file(tmp_path, env=env)
    load_env_file(tmp_path, env=env)
    assert env == {"FOO": "bar"}
