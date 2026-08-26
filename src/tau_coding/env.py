"""Project-local ``.env`` loading for Tau entry points.

A ``.env`` file in the project root (or any ancestor of the working
directory) is a deployment convenience: it is parsed at startup and its keys
are added to ``os.environ`` unless already set. The process environment always
wins, so variables exported by the shell, a systemd unit or a container
orchestrator take precedence over the file.

This module implements a small, dependency-free subset of the classic dotenv
syntax: ``KEY=VALUE`` lines, optional ``export`` prefixes, single- or
double-quoted values, full-line comments and trailing comments.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path


def load_env_file(
    start: Path | None = None,
    *,
    env: MutableMapping[str, str] | None = None,
) -> Path | None:
    """Load the nearest ``.env`` file and return its path, if any.

    Search order is the ``start`` directory (default: the current working
    directory) followed by each ancestor up to the filesystem root; the first
    ``.env`` found is used. Existing variables in ``env`` (default
    ``os.environ``) are never overridden. Returns the loaded path, or ``None``
    when no file exists.
    """
    path = _find_env_file(start)
    if path is None:
        return None
    target = env if env is not None else os.environ
    for name, value in _parse_env_file(path):
        target.setdefault(name, value)
    return path


def _find_env_file(start: Path | None) -> Path | None:
    directory = (start or Path.cwd()).resolve()
    for candidate in (directory, *directory.parents):
        path = candidate / ".env"
        if path.is_file():
            return path
    return None


def _parse_env_file(path: Path) -> list[tuple[str, str]]:
    """Parse a ``.env`` file into ``(name, value)`` pairs, skipping bad lines."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[tuple[str, str]] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name:
            continue
        entries.append((name, _clean_value(value)))
    return entries


def _clean_value(value: str) -> str:
    """Strip surrounding quotes and trailing comments from a raw value."""
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] in {"'", '"'}:
        quote = stripped[0]
        closing = stripped.find(quote, 1)
        if closing != -1:
            return stripped[1:closing]
        return stripped[1:]
    comment_index = stripped.find(" #")
    if comment_index != -1:
        stripped = stripped[:comment_index].rstrip()
    return stripped
