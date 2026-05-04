# Calibre-Web-NextGen — fork of Calibre-Web-Automated
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression tests for scripts/watch_fallback.py — the polling watcher used when
inotify is unavailable (NETWORK_SHARE_MODE=true, Docker Desktop, ENOSPC).

The original implementation refired CLOSE_WRITE on every poll cycle for any
file older than --stabilize, causing infinite ingestion loops on NFS-backed
deployments (upstream Calibre-Web-Automated #1326).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import watch_fallback  # noqa: E402
from watch_fallback import FileKey, FileStat, FIRED_SENTINEL, scan_once  # noqa: E402


def _make_old_file(path: Path, age_seconds: float = 10.0) -> None:
    path.write_bytes(b"static content")
    t = time.time() - age_seconds
    os.utime(path, (t, t))


def _drive(root: Path, cycles: int, mutate_at: int | None = None,
           stabilize: float = 1.5) -> list[tuple[int, str]]:
    """Run scan_once `cycles` times against `root`, returning list of
    (cycle, path) for every emitted event."""
    index: dict = {}
    fires: list[tuple[int, str]] = []

    file_path = root / "book.epub"
    _make_old_file(file_path)

    for c in range(cycles):
        if mutate_at == c:
            file_path.write_bytes(b"NEW content - user replaced the file")

        emitted = []
        scan_once(
            str(root), True, None, index, stabilize,
            lambda evt, fp: emitted.append((c, fp)),
        )
        fires.extend(emitted)
        time.sleep(0.05)
    return fires


def test_static_file_fires_exactly_once_over_many_cycles(tmp_path):
    """A file that never changes must emit CLOSE_WRITE only once, even after
    many polling cycles — the regression check for #1326."""
    fires = _drive(tmp_path, cycles=20)
    assert len(fires) == 1, f"expected 1 fire, got {len(fires)}: {fires}"


def test_modified_file_refires(tmp_path):
    """If the file's stat changes after firing, the watcher must fire again
    (correct refire is preserved by the fix)."""
    fires = _drive(tmp_path, cycles=20, mutate_at=8)
    assert len(fires) == 2, f"expected 2 fires (initial + after mutation), got {fires}"


def test_50_cycle_stress_static_file(tmp_path):
    """50 cycles is the upper bound of what one ingest run could overlap with
    a 5-second polling interval; even at this scale only one fire is allowed."""
    fires = _drive(tmp_path, cycles=50)
    assert len(fires) == 1, f"50-cycle stress: expected 1 fire, got {len(fires)}"


def test_sentinel_not_overwritten_by_increment(tmp_path):
    """Direct state-machine check: after a fire the FileStat has stable_count
    set to FIRED_SENTINEL, and that sentinel must not be incremented away by
    later polls."""
    file_path = tmp_path / "book.epub"
    _make_old_file(file_path)
    index: dict = {}

    # Cycle 1: prime
    scan_once(str(tmp_path), True, None, index, 1.5, lambda *a: None)
    # Cycle 2: stable, fires
    fires: list = []
    scan_once(str(tmp_path), True, None, index, 1.5, lambda e, p: fires.append(p))
    assert len(fires) == 1
    fk = FileKey(str(file_path))
    assert index[fk].stable_count == FIRED_SENTINEL

    # Cycle 3-10: must remain at sentinel
    for _ in range(8):
        scan_once(str(tmp_path), True, None, index, 1.5, lambda *a: None)
        assert index[fk].stable_count == FIRED_SENTINEL, \
            "sentinel was overwritten — bug regressed"


def test_removed_file_drops_from_index(tmp_path):
    """When a file disappears (ingest deleted it), the index must purge it so
    a same-named replacement gets fresh stable-count tracking."""
    file_path = tmp_path / "book.epub"
    _make_old_file(file_path)
    index: dict = {}

    scan_once(str(tmp_path), True, None, index, 1.5, lambda *a: None)
    assert FileKey(str(file_path)) in index

    file_path.unlink()
    scan_once(str(tmp_path), True, None, index, 1.5, lambda *a: None)
    assert FileKey(str(file_path)) not in index, "removed file lingered in index"
