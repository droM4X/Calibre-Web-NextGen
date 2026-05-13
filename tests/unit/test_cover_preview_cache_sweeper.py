# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Regression tests for the cover-preview LRU cache sweeper.

Pins the load-bearing properties:

* Under-cap is a no-op (idempotent, cheap)
* Over-cap evicts oldest-atime first (correct LRU order)
* Missing cache root is a no-op (cold start safety)
* Env-var override works (operator-configurable cap)
* Bad env-var value defaults to 1024 MB (safe failure mode)
* Dry run preserves files (planning without mutation)
* Multi-prefix layout walked (not just one shard)
* ``.tmp-`` orphan files skipped (no racing write_to_cache)
* Race-tolerant: FileNotFoundError during unlink doesn't abort
* Permission errors counted but don't abort the sweep
* ``noatime`` filesystem fallback uses mtime

These tests exist because the sweeper is the only thing standing
between the cache and an unbounded fill — getting any one of the above
wrong means either an operator complaint ("my disk filled up") or a
performance regression ("the sweep is deleting hot tiles").
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_cache_root(monkeypatch, tmp_path):
    """Redirect both modules' view of CACHE_ROOT to a tmp dir.

    The sweeper imports the cache module and reads its ``CACHE_ROOT``
    at call time, so patching the cache module is sufficient — but
    we also patch any direct reference defensively in case future
    refactors copy the symbol.
    """
    from cps.services import cover_preview_cache as cache_mod
    root = tmp_path / ".cwa-preview-cache"
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", root)
    return root


@pytest.fixture
def clean_env(monkeypatch):
    """Ensure the cap env var is unset unless a test sets it explicitly."""
    monkeypatch.delenv("CWA_PREVIEW_CACHE_MAX_MB", raising=False)
    return monkeypatch


def _seed(root: Path, prefix: str, name: str, content_bytes: bytes = b"x" * 1024,
          atime: float | None = None, mtime: float | None = None) -> Path:
    """Create a cached file in the right layout, optionally setting timestamps.

    ``content_bytes`` defaults to 1 KiB so test-cap arithmetic is easy.
    ``atime`` / ``mtime``, when given, are forced via ``os.utime``.
    """
    d = root / prefix
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(content_bytes)
    if atime is not None or mtime is not None:
        # os.utime requires both; default the missing one to the file's
        # current value so we only override what the caller asked for.
        st = f.stat()
        a = atime if atime is not None else st.st_atime
        m = mtime if mtime is not None else st.st_mtime
        os.utime(f, (a, m))
    return f


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_under_cap_noop(tmp_cache_root, clean_env, monkeypatch):
    """Total well under cap → no eviction, returns 0/0/0/0."""
    monkeypatch.setenv("CWA_PREVIEW_CACHE_MAX_MB", "1")  # 1 MB cap
    _seed(tmp_cache_root, "aa", "111.jpg", b"x" * 1024)  # 1 KiB
    _seed(tmp_cache_root, "aa", "222.jpg", b"x" * 1024)  # 1 KiB

    from cps.services import cover_preview_cache_sweeper as sw
    result = sw.sweep()

    assert result["evicted"] == 0
    assert result["errors"] == 0
    assert result["before_bytes"] == result["after_bytes"]
    assert result["before_bytes"] == 2048


def test_over_cap_evicts_oldest_first(tmp_cache_root, clean_env, monkeypatch):
    """3 files, cap = 2 files of bytes → oldest evicted, rest survive."""
    # Each file is 1024 bytes. Cap = 2400 bytes leaves room for 2 of them
    # but not 3, so exactly one (the oldest) should be evicted.
    monkeypatch.setattr(
        "cps.services.cover_preview_cache_sweeper._cap_bytes",
        lambda: 2400,
    )

    now = time.time()
    oldest = _seed(tmp_cache_root, "aa", "old.jpg", b"x" * 1024, atime=now - 1000)
    mid = _seed(tmp_cache_root, "aa", "mid.jpg", b"x" * 1024, atime=now - 500)
    newest = _seed(tmp_cache_root, "aa", "new.jpg", b"x" * 1024, atime=now - 1)

    from cps.services import cover_preview_cache_sweeper as sw
    result = sw.sweep()

    assert result["evicted"] == 1
    assert result["errors"] == 0
    assert not oldest.exists(), "Oldest-atime file should be evicted first"
    assert mid.exists(), "Mid file should survive"
    assert newest.exists(), "Newest file should survive"
    assert result["after_bytes"] == 2048


def test_missing_cache_dir_noop(tmp_path, monkeypatch, clean_env):
    """Cache root doesn't exist → no walk, no errors."""
    from cps.services import cover_preview_cache as cache_mod
    from cps.services import cover_preview_cache_sweeper as sw

    nonexistent = tmp_path / "does-not-exist"
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", nonexistent)

    result = sw.sweep()

    assert result["before_bytes"] == 0
    assert result["after_bytes"] == 0
    assert result["evicted"] == 0
    assert result["errors"] == 0


def test_env_var_override(tmp_cache_root, clean_env, monkeypatch):
    """CWA_PREVIEW_CACHE_MAX_MB=10 → cap_bytes = 10 * 1024 * 1024."""
    monkeypatch.setenv("CWA_PREVIEW_CACHE_MAX_MB", "10")

    from cps.services import cover_preview_cache_sweeper as sw
    result = sw.sweep()

    assert result["cap_bytes"] == 10 * 1024 * 1024


def test_bad_env_value_defaults_to_1024(tmp_cache_root, clean_env, monkeypatch):
    """Garbage env value → falls back to DEFAULT_CAP_MB (1024 MB)."""
    monkeypatch.setenv("CWA_PREVIEW_CACHE_MAX_MB", "garbage")

    from cps.services import cover_preview_cache_sweeper as sw
    result = sw.sweep()

    assert result["cap_bytes"] == 1024 * 1024 * 1024


def test_negative_env_value_defaults_to_1024(tmp_cache_root, clean_env, monkeypatch):
    """Negative env value would silently disable eviction → must default."""
    monkeypatch.setenv("CWA_PREVIEW_CACHE_MAX_MB", "-100")

    from cps.services import cover_preview_cache_sweeper as sw
    result = sw.sweep()

    assert result["cap_bytes"] == 1024 * 1024 * 1024


def test_zero_env_value_defaults_to_1024(tmp_cache_root, clean_env, monkeypatch):
    """Zero would disable eviction entirely → must default."""
    monkeypatch.setenv("CWA_PREVIEW_CACHE_MAX_MB", "0")

    from cps.services import cover_preview_cache_sweeper as sw
    result = sw.sweep()

    assert result["cap_bytes"] == 1024 * 1024 * 1024


def test_dry_run_preserves_files(tmp_cache_root, clean_env, monkeypatch):
    """dry_run=True reports the same eviction plan but unlinks nothing."""
    monkeypatch.setattr(
        "cps.services.cover_preview_cache_sweeper._cap_bytes",
        lambda: 2400,
    )

    now = time.time()
    oldest = _seed(tmp_cache_root, "aa", "old.jpg", b"x" * 1024, atime=now - 1000)
    mid = _seed(tmp_cache_root, "aa", "mid.jpg", b"x" * 1024, atime=now - 500)
    newest = _seed(tmp_cache_root, "aa", "new.jpg", b"x" * 1024, atime=now - 1)

    from cps.services import cover_preview_cache_sweeper as sw
    result = sw.sweep(dry_run=True)

    assert result["evicted"] == 1
    assert oldest.exists(), "Dry run must not unlink"
    assert mid.exists()
    assert newest.exists()
    # Accounting still reports the would-be after-size.
    assert result["after_bytes"] == 2048


def test_multi_prefix_layout_walked(tmp_cache_root, clean_env, monkeypatch):
    """Files spread across many <aa>/ shards are all considered."""
    monkeypatch.setattr(
        "cps.services.cover_preview_cache_sweeper._cap_bytes",
        lambda: 1500,  # holds 1 file (1024) but not 3
    )

    now = time.time()
    a = _seed(tmp_cache_root, "aa", "1.jpg", b"x" * 1024, atime=now - 100)
    b = _seed(tmp_cache_root, "bb", "1.jpg", b"x" * 1024, atime=now - 50)
    c = _seed(tmp_cache_root, "cc", "1.jpg", b"x" * 1024, atime=now - 10)

    from cps.services import cover_preview_cache_sweeper as sw
    result = sw.sweep()

    # Two oldest evicted, newest survives.
    assert result["evicted"] == 2
    assert not a.exists()
    assert not b.exists()
    assert c.exists()


def test_tmp_orphan_files_skipped(tmp_cache_root, clean_env, monkeypatch):
    """``.tmp-`` files are NOT eviction candidates — write_to_cache uses
    them as the in-flight rename source and the sweeper must not race
    a writer."""
    monkeypatch.setattr(
        "cps.services.cover_preview_cache_sweeper._cap_bytes",
        lambda: 1,  # tiny cap forces eviction of any non-tmp file
    )

    now = time.time()
    # An in-flight tempfile that the sweeper must leave alone.
    orphan = _seed(tmp_cache_root, "aa", ".tmp-abc.jpg",
                   b"x" * 1024, atime=now - 10000)
    # A real cache file that should be evicted.
    real = _seed(tmp_cache_root, "aa", "real.jpg",
                 b"x" * 1024, atime=now - 1)

    from cps.services import cover_preview_cache_sweeper as sw
    result = sw.sweep()

    assert orphan.exists(), ".tmp- orphan must not be evicted"
    assert not real.exists(), "Real cache file should be evicted"
    # before_bytes only includes the real file because the orphan is
    # skipped before size accounting.
    assert result["before_bytes"] == 1024


def test_file_not_found_during_unlink_tolerated(tmp_cache_root, clean_env,
                                                 monkeypatch):
    """FileNotFoundError during unlink (race) → counted as evicted, not error."""
    monkeypatch.setattr(
        "cps.services.cover_preview_cache_sweeper._cap_bytes",
        lambda: 1024,
    )

    now = time.time()
    a = _seed(tmp_cache_root, "aa", "race.jpg", b"x" * 1024, atime=now - 1000)
    b = _seed(tmp_cache_root, "aa", "keep.jpg", b"x" * 1024, atime=now - 1)

    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        # Simulate the race for the oldest file only; let the rest proceed.
        if self.name == "race.jpg":
            # Actually remove the file so subsequent checks are honest,
            # then raise — same effect as another sweeper having beaten
            # us to it.
            real_unlink(self, *args, **kwargs)
            raise FileNotFoundError(self)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    from cps.services import cover_preview_cache_sweeper as sw
    result = sw.sweep()

    # The race file is gone (because we removed it), counted as evicted,
    # NOT counted as an error.
    assert not a.exists()
    assert b.exists(), "Newer file should survive — cap allowed it"
    assert result["evicted"] >= 1
    assert result["errors"] == 0


def test_permission_error_counted_not_aborted(tmp_cache_root, clean_env,
                                              monkeypatch):
    """PermissionError on one file → reported in errors, sweep continues."""
    monkeypatch.setattr(
        "cps.services.cover_preview_cache_sweeper._cap_bytes",
        lambda: 0,  # force eviction of everything
    )

    now = time.time()
    locked = _seed(tmp_cache_root, "aa", "locked.jpg",
                   b"x" * 1024, atime=now - 1000)
    free = _seed(tmp_cache_root, "aa", "free.jpg",
                 b"x" * 1024, atime=now - 500)

    real_unlink = Path.unlink

    def selective_unlink(self, *args, **kwargs):
        if self.name == "locked.jpg":
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", selective_unlink)

    from cps.services import cover_preview_cache_sweeper as sw
    result = sw.sweep()

    assert locked.exists(), "Locked file remained on disk"
    assert not free.exists(), "Sweeper continued past the failure"
    assert result["errors"] == 1
    assert result["evicted"] == 1


def test_noatime_fallback_to_mtime(tmp_cache_root, clean_env, monkeypatch):
    """When ``st_atime == 0``, ``_file_recency`` returns ``st_mtime``."""
    f = _seed(tmp_cache_root, "aa", "frozen.jpg", b"x" * 16)

    real_stat = Path.stat

    class FakeStat:
        def __init__(self, real):
            self._real = real
        # Always report atime=0 to simulate a `noatime` mount that
        # the kernel never advances.
        st_atime = 0
        @property
        def st_mtime(self): return self._real.st_mtime
        @property
        def st_size(self): return self._real.st_size

    def faked_stat(self, *args, **kwargs):
        real = real_stat(self, *args, **kwargs)
        # Only fake the recency-stat call (which doesn't pass kwargs)
        # for the file we care about. Returning a wrapper for all
        # files is fine because we only override atime.
        return FakeStat(real)

    monkeypatch.setattr(Path, "stat", faked_stat)

    from cps.services import cover_preview_cache_sweeper as sw
    recency = sw._file_recency(f)

    # Recency should equal mtime, NOT atime (which is fake-zero).
    assert recency > 0, "Should fall back to mtime, not return 0"


def test_main_entry_point_runs(tmp_cache_root, clean_env, monkeypatch):
    """``main()`` runs end-to-end without raising (smoke test for the
    s6-service invocation path)."""
    # Seed something to make sure the walk does work.
    _seed(tmp_cache_root, "aa", "1.jpg", b"x" * 1024)

    from cps.services import cover_preview_cache_sweeper as sw
    # Should not raise.
    sw.main()


def test_sort_order_is_strictly_atime(tmp_cache_root, clean_env, monkeypatch):
    """When atime differs but mtime is constant, eviction order follows
    atime — confirms the sweeper isn't accidentally sorting by mtime
    or path."""
    monkeypatch.setattr(
        "cps.services.cover_preview_cache_sweeper._cap_bytes",
        lambda: 1500,  # holds one file (1024) but not all three
    )

    now = time.time()
    # Same mtime for all three; different atimes.
    files = []
    for name, atime in [("a.jpg", now - 100),
                        ("b.jpg", now - 200),
                        ("c.jpg", now - 50)]:
        f = _seed(tmp_cache_root, "aa", name, b"x" * 1024,
                  atime=atime, mtime=now - 1)
        files.append((name, f, atime))

    from cps.services import cover_preview_cache_sweeper as sw
    result = sw.sweep()

    # b.jpg has the oldest atime → first to go.
    # a.jpg next.
    # c.jpg survives.
    survivors = {name for name, f, _ in files if f.exists()}
    assert survivors == {"c.jpg"}, (
        f"Expected only c.jpg (newest atime) to survive, got {survivors}"
    )
    assert result["evicted"] == 2
