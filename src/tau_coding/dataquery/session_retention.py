"""Archive inactive query sessions without expiring the audit archive."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import os
import re
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)


@contextmanager
def session_lock(root: Path, session_id: str) -> Iterator[None]:
    """Serialize a session's run, archival and restoration across processes."""
    locks = root / ".locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (locks / f"{session_id}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _manifest(directory: Path) -> dict[str, str]:
    result = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("Session archives must not contain symlinks")
        if path.is_file():
            with path.open("rb") as stream:
                result[str(path.relative_to(directory))] = hashlib.file_digest(
                    stream, "sha256"
                ).hexdigest()
    return result


def archive_root(root: Path) -> Path:
    return Path(
        os.environ.get("TAU_QUERY_SESSION_ARCHIVE_DIR", str(root.with_name(root.name + "-archive")))
    ).expanduser()


def restore_session(root: Path, session_id: str) -> None:
    """Restore the latest archive under the session lock; retain the archive."""
    if (root / session_id).exists():
        return
    archive = archive_root(root)
    candidates = sorted(archive.glob(f"{session_id}-*")) if archive.exists() else []
    if not candidates:
        return
    source = candidates[-1]
    expected = _manifest(source)
    staging = root / f".restore-{session_id}-{uuid4().hex}"
    shutil.copytree(source, staging)
    if _manifest(staging) != expected:
        raise OSError("Restored session verification failed")
    staging.rename(root / session_id)
    os.utime(root / session_id, None)


def archive_expired_sessions(
    root: Path,
    *,
    retention_days: int = 90,
    now: float | None = None,
    warning_bytes: int = 5 * 1024**3,
) -> int:
    """Copy and verify before removing inactive files; skip failures and live runs."""
    if retention_days < 0:
        raise ValueError("retention_days must not be negative")
    if not root.exists():
        return 0
    archive = archive_root(root)
    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    if archive.resolve() == root.resolve() or root.resolve() in archive.resolve().parents:
        raise ValueError("Archive must be outside the active session root")
    if archive.stat().st_dev != root.stat().st_dev:
        raise ValueError("Archive must be on the same persistent volume")
    total = sum(p.stat().st_size for p in archive.rglob("*") if p.is_file())
    if total >= warning_bytes:
        logger.warning("Query session archive capacity threshold reached: %s bytes", total)
    if retention_days == 0:
        return 0
    cutoff = (now if now is not None else time.time()) - retention_days * 86400
    count = 0
    for directory in root.iterdir():
        if not re.fullmatch(r"[a-f0-9]{32}", directory.name) or not directory.is_dir():
            continue
        try:
            with session_lock(root, directory.name):
                if directory.is_symlink():
                    raise ValueError("Session must not be a symlink")
                files = [p for p in directory.rglob("*") if p.is_file()]
                if max([directory.stat().st_mtime, *(p.stat().st_mtime for p in files)]) >= cutoff:
                    continue
                expected = _manifest(directory)
                destination = archive / f"{directory.name}-{time.time_ns()}-{uuid4().hex}"
                staging = archive / f".pending-{uuid4().hex}"
                shutil.copytree(directory, staging)
                if _manifest(staging) != expected:
                    raise OSError("Archive verification failed")
                staging.rename(destination)
                shutil.rmtree(directory)
                count += 1
        except BlockingIOError:
            continue
        except (OSError, ValueError):
            logger.exception("Query session archival skipped for %s", directory.name)
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="Run maintenance once per day")
    args = parser.parse_args()
    root = Path(os.environ.get("TAU_QUERY_SESSION_ROOT", str(Path.home() / ".tau/query-sessions")))
    while True:
        try:
            archive_expired_sessions(
                root, retention_days=int(os.environ.get("TAU_QUERY_SESSION_RETENTION_DAYS", "90"))
            )
        except (OSError, ValueError):
            logger.exception("Query session archive maintenance failed; active files retained")
        if not args.watch:
            break
        time.sleep(86400)


if __name__ == "__main__":
    main()
