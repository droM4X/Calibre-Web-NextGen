# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Regression tests for the cover-preview disk cache module.

Pins the load-bearing properties:

* Key determinism + auto-invalidation via cover mtime
* color None vs "" collide (both mean "no manual color")
* user_id is NOT a key input (cache is shared across users with the
  same effective rendering)
* Git-style 2-char prefix path layout
* cache_hit touches atime so the LRU sweeper sees recency
* cache_hit returns None on miss, Path on hit
* write_to_cache is atomic — partial writes never visible to readers
* stampede_lock returns the same Lock per key, different Locks per
  different keys, with live-concurrency proof
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_cache_root(monkeypatch, tmp_path):
    """Redirect the cache module's root to ``tmp_path`` for isolation.

    Also resets the stampede-lock dict and the noatime-warning flag so
    tests don't bleed state into each other.
    """
    from cps.services import cover_preview_cache as mod
    root = tmp_path / ".cwa-preview-cache"
    monkeypatch.setattr(mod, "CACHE_ROOT", root)
    # Reset module-level mutable state so previous tests' locks /
    # warning flag don't leak.
    monkeypatch.setattr(mod, "_stampede_locks", {})
    monkeypatch.setattr(mod, "_noatime_warned", False)
    return root


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------


class TestCacheKey:
    def test_same_inputs_same_key(self):
        from cps.services.cover_preview_cache import cache_key
        a = cache_key(1, 100, "kobo_libra_color", "edge_mirror", None)
        b = cache_key(1, 100, "kobo_libra_color", "edge_mirror", None)
        assert a == b
        assert len(a) == 16
        # All hex
        int(a, 16)

    def test_mtime_change_busts_key(self):
        """Updated covers automatically invalidate the cache."""
        from cps.services.cover_preview_cache import cache_key
        a = cache_key(1, 100, "kobo_libra_color", "edge_mirror", None)
        b = cache_key(1, 101, "kobo_libra_color", "edge_mirror", None)
        assert a != b

    def test_color_none_vs_empty_string_collide(self):
        """None and "" both normalize to "" — same cache entry.

        Important because the resolution layer can pass either depending
        on whether the user has a NULL row or an empty-string default;
        we don't want to double-render the same tile under two keys.
        """
        from cps.services.cover_preview_cache import cache_key
        a = cache_key(1, 100, "kobo_libra_color", "manual", None)
        b = cache_key(1, 100, "kobo_libra_color", "manual", "")
        assert a == b

    def test_user_id_not_in_key(self):
        """No user_id parameter exists — two users with identical
        effective settings share the rendered tile."""
        from cps.services.cover_preview_cache import cache_key
        # Same book + mtime + preset + fill + color → same key,
        # regardless of which user is asking.
        a = cache_key(42, 100, "kobo_libra_color", "edge_mirror", None)
        b = cache_key(42, 100, "kobo_libra_color", "edge_mirror", None)
        assert a == b

    def test_different_book_different_key(self):
        from cps.services.cover_preview_cache import cache_key
        a = cache_key(1, 100, "kobo_libra_color", "edge_mirror", None)
        b = cache_key(2, 100, "kobo_libra_color", "edge_mirror", None)
        assert a != b

    def test_different_preset_different_key(self):
        from cps.services.cover_preview_cache import cache_key
        a = cache_key(1, 100, "kobo_libra_color", "edge_mirror", None)
        b = cache_key(1, 100, "kobo_clara_2e", "edge_mirror", None)
        assert a != b

    def test_different_fill_different_key(self):
        from cps.services.cover_preview_cache import cache_key
        a = cache_key(1, 100, "kobo_libra_color", "edge_mirror", None)
        b = cache_key(1, 100, "kobo_libra_color", "blur", None)
        assert a != b

    def test_different_color_different_key(self):
        from cps.services.cover_preview_cache import cache_key
        a = cache_key(1, 100, "kobo_libra_color", "manual", "#000000")
        b = cache_key(1, 100, "kobo_libra_color", "manual", "#ffffff")
        assert a != b


# ---------------------------------------------------------------------------
# Path layout
# ---------------------------------------------------------------------------


class TestCachePathLayout:
    def test_path_uses_two_char_prefix(self, tmp_cache_root):
        """Git-style 2-char sharding caps directory size."""
        from cps.services.cover_preview_cache import cache_path
        p = cache_path("abcd1234567890ef")
        assert p.name == "cd1234567890ef.jpg"
        assert p.parent.name == "ab"
        # And the root of the path is the (monkeypatched) cache root
        assert p.parent.parent == tmp_cache_root

    def test_path_rejects_too_short_key(self, tmp_cache_root):
        """A truncated key would break the 2-char layout — fail loudly."""
        from cps.services.cover_preview_cache import cache_path
        with pytest.raises(ValueError):
            cache_path("ab")
        with pytest.raises(ValueError):
            cache_path("")


# ---------------------------------------------------------------------------
# Cache hit / miss
# ---------------------------------------------------------------------------


class TestCacheHitMiss:
    def test_miss_returns_none(self, tmp_cache_root):
        from cps.services.cover_preview_cache import cache_hit
        assert cache_hit("0000000000000000") is None

    def test_miss_when_parent_dir_missing(self, tmp_cache_root):
        """If even the prefix dir doesn't exist, we should miss cleanly,
        not raise."""
        from cps.services.cover_preview_cache import cache_hit
        assert cache_hit("ffffffffffffffff") is None

    def test_hit_returns_path_and_touches_atime(self, tmp_cache_root):
        """On hit, atime/mtime should advance so the LRU sweeper
        treats this tile as recently-used."""
        from cps.services.cover_preview_cache import (
            cache_path, cache_hit, write_to_cache,
        )
        key = "abcd1234567890ef"
        write_to_cache(key, b"\xff\xd8fake-jpeg-bytes-padding")
        p = cache_path(key)
        # Force the on-disk timestamps backwards so we have headroom to
        # observe forward motion (sub-second resolution on some FS).
        old_ts = time.time() - 60
        os.utime(p, (old_ts, old_ts))
        before = p.stat().st_atime

        hit = cache_hit(key)

        assert hit == p
        after = p.stat().st_atime
        # atime must have moved forward by the touch
        assert after > before


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


class TestWriteToCache:
    def test_write_then_read(self, tmp_cache_root):
        from cps.services.cover_preview_cache import cache_path, write_to_cache
        key = "fedcba9876543210"
        result = write_to_cache(key, b"hello-world")
        assert result == cache_path(key)
        assert cache_path(key).read_bytes() == b"hello-world"

    def test_creates_prefix_dir(self, tmp_cache_root):
        from cps.services.cover_preview_cache import cache_path, write_to_cache
        key = "11223344aabbccdd"
        write_to_cache(key, b"x")
        assert cache_path(key).parent.is_dir()
        assert cache_path(key).parent.name == "11"

    def test_no_tempfile_orphans_on_success(self, tmp_cache_root):
        """A successful write should leave no `.tmp-*` orphans behind."""
        from cps.services.cover_preview_cache import cache_path, write_to_cache
        key = "0011223344556677"
        write_to_cache(key, b"clean")
        parent = cache_path(key).parent
        tmps = [c for c in parent.iterdir() if c.name.startswith(".tmp-")]
        assert tmps == []

    def test_atomic_write_never_exposes_partial_file(self, tmp_cache_root):
        """Race: while writer is running, readers must see either
        no-file or fully-written-file. Never a partial.

        We can't easily inject a sleep between tmp.write and rename
        without monkeypatching the module, so we drive it as a live
        concurrent test: one writer thread loops writing the full
        payload, many reader threads loop checking the file's bytes.
        Any reader observing a non-empty file with the wrong length
        or wrong content would prove non-atomicity.
        """
        from cps.services.cover_preview_cache import cache_path, write_to_cache

        key = "deadbeefcafe0000"
        # Use a sizable payload so the write isn't a single syscall —
        # gives the race more surface area to manifest if it exists.
        payload = b"JPEG" * 4096  # 16 KB

        stop = threading.Event()
        partial_seen: list = []

        def writer():
            while not stop.is_set():
                write_to_cache(key, payload)

        def reader():
            p = cache_path(key)
            while not stop.is_set():
                try:
                    data = p.read_bytes()
                except (OSError, FileNotFoundError):
                    continue
                if data and data != payload:
                    partial_seen.append(len(data))
                    return

        w = threading.Thread(target=writer, daemon=True)
        readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
        w.start()
        for r in readers:
            r.start()
        # Let them race for a moment.
        time.sleep(0.5)
        stop.set()
        w.join(timeout=2)
        for r in readers:
            r.join(timeout=2)

        assert partial_seen == [], (
            f"readers observed {len(partial_seen)} partial writes "
            f"(sizes: {partial_seen[:5]}); write is not atomic"
        )

    def test_write_failure_returns_none(self, tmp_cache_root, monkeypatch):
        """If the FS rejects the write, we return None instead of
        raising — the endpoint can still serve the bytes uncached."""
        from cps.services import cover_preview_cache as mod

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(mod.os, "rename", boom)
        result = mod.write_to_cache("ffffffffaaaaaaaa", b"data")
        assert result is None
        # And we should not have left a tempfile orphan around (the
        # finally clause cleans it up).
        parent = mod.cache_path("ffffffffaaaaaaaa").parent
        if parent.is_dir():
            tmps = [c for c in parent.iterdir() if c.name.startswith(".tmp-")]
            assert tmps == []


# ---------------------------------------------------------------------------
# Stampede guard
# ---------------------------------------------------------------------------


class TestStampedeLock:
    def test_same_key_same_lock(self, tmp_cache_root):
        from cps.services.cover_preview_cache import stampede_lock
        a = stampede_lock("k1")
        b = stampede_lock("k1")
        assert a is b

    def test_different_keys_different_locks(self, tmp_cache_root):
        from cps.services.cover_preview_cache import stampede_lock
        a = stampede_lock("k1")
        b = stampede_lock("k2")
        assert a is not b

    def test_concurrent_same_key_all_get_same_lock(self, tmp_cache_root):
        """Race: 20 threads simultaneously call stampede_lock(same_key).
        All 20 must receive the same Lock instance — if the master-lock
        dance is broken, you can transiently get distinct Lock objects
        for the same key, defeating the stampede guard.
        """
        from cps.services.cover_preview_cache import stampede_lock

        key = "concurrent-race"
        N = 20
        results: list = [None] * N
        barrier = threading.Barrier(N)

        def worker(idx):
            # Synchronize the entry so all threads hit stampede_lock as
            # close to simultaneously as possible.
            barrier.wait()
            results[idx] = stampede_lock(key)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert all(r is not None for r in results)
        # All N threads must have received the same Lock instance.
        first = results[0]
        same = sum(1 for r in results if r is first)
        assert same == N, (
            f"only {same}/{N} threads got the same Lock instance — "
            "stampede guard is broken; duplicate renders would occur"
        )

    def test_stampede_lock_actually_serializes(self, tmp_cache_root):
        """End-to-end: when 10 threads hold the lock around a critical
        section, the critical sections must not overlap. Proves the
        Lock returned is functional, not just identity-equal."""
        from cps.services.cover_preview_cache import stampede_lock

        key = "serialization-proof"
        N = 10
        in_critical = [0]
        max_concurrency = [0]
        guard = threading.Lock()
        barrier = threading.Barrier(N)

        def worker():
            barrier.wait()
            lock = stampede_lock(key)
            with lock:
                with guard:
                    in_critical[0] += 1
                    if in_critical[0] > max_concurrency[0]:
                        max_concurrency[0] = in_critical[0]
                # Small sleep so concurrency would be observable if
                # the lock weren't doing its job.
                time.sleep(0.01)
                with guard:
                    in_critical[0] -= 1

        threads = [threading.Thread(target=worker) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert max_concurrency[0] == 1, (
            f"stampede lock allowed {max_concurrency[0]} threads into "
            "the critical section simultaneously"
        )
