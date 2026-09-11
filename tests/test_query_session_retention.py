from __future__ import annotations

import os
import time
from pathlib import Path

from tau_coding.dataquery.session_retention import (
    archive_expired_sessions,
    archive_root,
    restore_session,
    session_lock,
)


def old_session(root: Path) -> Path:
    directory = root / ("a" * 32)
    directory.mkdir(parents=True)
    transcript = directory / "session.jsonl"
    transcript.write_text('{"sql":"SELECT 1","rows":[[1]]}\n')
    old = time.time() - 100 * 86400
    os.utime(transcript, (old, old))
    os.utime(directory, (old, old))
    return directory


def test_archive_is_verified_and_restorable_without_deleting_audit(tmp_path):
    root = tmp_path / "active"
    directory = old_session(root)
    assert archive_expired_sessions(root) == 1
    assert not directory.exists()
    archives = list(archive_root(root).iterdir())
    assert len(archives) == 1
    with session_lock(root, directory.name):
        restore_session(root, directory.name)
    assert (directory / "session.jsonl").read_bytes() == (
        archives[0] / "session.jsonl"
    ).read_bytes()
    assert archives[0].exists()
    assert archive_expired_sessions(root) == 0


def test_active_lock_prevents_cleanup(tmp_path):
    root = tmp_path / "active"
    directory = old_session(root)
    with session_lock(root, directory.name):
        assert archive_expired_sessions(root) == 0
    assert directory.exists()


def test_failed_archive_copy_keeps_active_files(tmp_path, monkeypatch, caplog):
    root = tmp_path / "active"
    directory = old_session(root)

    def denied(*args, **kwargs):
        raise PermissionError("archive read only")

    monkeypatch.setattr("tau_coding.dataquery.session_retention.shutil.copytree", denied)
    assert archive_expired_sessions(root) == 0
    assert (directory / "session.jsonl").exists()
    assert "archival skipped" in caplog.text


def test_disabled_cleanup_still_warns_about_archive_capacity(tmp_path, caplog):
    root = tmp_path / "active"
    directory = old_session(root)
    archive = archive_root(root)
    archive.mkdir()
    (archive / "retained").write_text("audit")
    assert archive_expired_sessions(root, retention_days=0, warning_bytes=1) == 0
    assert directory.exists()
    assert "capacity threshold" in caplog.text
