# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""LRU-by-atime sweeper for the cover-preview disk cache.

Walks :data:`cps.services.cover_preview_cache.CACHE_ROOT`, totals file
sizes, and evicts oldest-``atime`` files until total is under
``CWA_PREVIEW_CACHE_MAX_MB`` (default 1024 MB).

Designed to be invoked from:

* The ``cwa-preview-cache-cleanup`` s6 service (hourly loop — Task 4).
* Operator-triggered shell:
  ``python -m cps.services.cover_preview_cache_sweeper``.
* Programmatically (e.g. after a bulk delete in Phase 2's cleanup).

Design notes / non-obvious choices
----------------------------------

* **Imports ``CACHE_ROOT`` from the cache module, doesn't redefine it.**
  Tests monkeypatch ``cover_preview_cache.CACHE_ROOT`` to ``tmp_path``;
  the sweeper has to see the same redirected root for tests to be
  meaningful. We import the module (not the symbol) and read
  ``mod.CACHE_ROOT`` at call time so monkeypatching either the cache
  module *or* this module works.

* **Single-pass, no locks.** Cache writes are atomic
  (write-tempfile-then-rename); a race where the sweeper deletes a file
  the endpoint just wrote is harmless — the next request just
  re-renders. Holding any lock across the walk would block reads for
  the duration of the sweep, which on a large cache is unacceptable.

* **atime with mtime fallback.** Linux servers commonly mount with
  ``noatime`` for performance. The cache module's ``cache_hit()``
  explicitly calls ``utime(None)`` on hits which forces both stamps
  even on ``noatime`` mounts, but a paranoid fallback to ``mtime``
  when ``atime == 0`` (filesystems that report a frozen zero atime)
  keeps the LRU ordering sane.

* **Skips ``.tmp-`` orphans.** :func:`cover_preview_cache.write_to_cache`
  uses ``NamedTemporaryFile(prefix=".tmp-", ...)`` for the
  write-then-rename dance. A file with that prefix is either an
  in-flight write (race — let it finish) or a leftover from a crashed
  write. Either way, the sweeper isn't the right tool to GC them —
  it would race the writer, and orphan cleanup is a separate concern.

* **Race-tolerant unlink.** ``FileNotFoundError`` during unlink means
  some other process (or a future concurrent sweeper) already deleted
  the file. That's success for our accounting purposes — count it
  as evicted, don't count it as an error. Other ``OSError`` subclasses
  (permission denied, read-only FS) are reported in ``errors`` but
  the sweep continues — a single bad file shouldn't stall eviction
  of the rest.

* **Bad env-var value defaults to 1024 MB.** A typo in
  ``CWA_PREVIEW_CACHE_MAX_MB`` (``"1gb"``, ``"-100"``, empty string)
  should NOT silently disable the sweeper, which would let the cache
  grow unbounded until the operator's disk fills. Falling back to the
  documented default is the safe failure mode.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Tuple

from cps.services import cover_preview_cache as _cache_mod

log = logging.getLogger(__name__)

DEFAULT_CAP_MB: int = 1024
ENV_CAP_VAR: str = "CWA_PREVIEW_CACHE_MAX_MB"


def _cap_bytes() -> int:
    """Return the cache size cap in bytes.

    Falls back to ``DEFAULT_CAP_MB`` on missing, empty, non-integer,
    zero, or negative values — so a typo in the env var doesn't
    silently disable eviction and let the disk fill up.
    """
    raw = os.environ.get(ENV_CAP_VAR, "")
    try:
        value_mb = int(raw)
        if value_mb > 0:
            return value_mb * 1024 * 1024
    except (ValueError, TypeError):
        pass
    return DEFAULT_CAP_MB * 1024 * 1024


def _file_recency(entry: Path) -> float:
    """Return the most-recent-use timestamp for LRU ordering.

    Prefers ``atime`` (what ``cache_hit()`` bumps via ``utime(None)``).
    Falls back to ``mtime`` when ``atime`` is zero, which happens on
    filesystems that report a frozen atime (some networked / unusual
    mounts). ``mtime`` is also touched by ``cache_hit()``'s
    ``utime(None)``, so it's a valid LRU proxy.

    Returns ``0.0`` if the file vanishes between the walk and the stat
    — caller treats that as "very old", which makes it a first-eviction
    candidate, which is fine because the file is gone anyway.
    """
    try:
        stat = entry.stat()
    except OSError:
        return 0.0
    if stat.st_atime > 0:
        return stat.st_atime
    return stat.st_mtime


def sweep(dry_run: bool = False) -> Dict[str, int]:
    """Evict oldest-recency files until total cache size is under cap.

    Idempotent: if total <= cap, returns immediately with
    ``evicted=0``. Otherwise sorts by recency ascending and unlinks
    oldest-first until total drops under the cap.

    Args:
        dry_run: When ``True``, reports the same eviction plan but
            does not actually unlink any files. Useful for operator
            inspection (``python -m ... --help``-style behaviour) and
            for tests that want to verify the planning logic without
            mutating the filesystem.

    Returns:
        A dict with::

            {
                'before_bytes': int,   # total size before sweep
                'after_bytes':  int,   # total size after sweep (or
                                       # planned after-size in dry_run)
                'evicted':      int,   # number of files evicted
                'errors':       int,   # number of files we tried to
                                       # delete but couldn't (perm, etc.)
                'cap_bytes':    int,   # the cap that was applied
                'cache_root':   str,   # the root that was walked
            }
    """
    cap = _cap_bytes()
    # Read CACHE_ROOT from the module at call time so monkeypatching
    # works in tests (test redirects cover_preview_cache.CACHE_ROOT to
    # tmp_path, and we want to see that change here).
    root: Path = _cache_mod.CACHE_ROOT

    if not root.is_dir():
        return {
            "before_bytes": 0,
            "after_bytes": 0,
            "evicted": 0,
            "errors": 0,
            "cap_bytes": cap,
            "cache_root": str(root),
        }

    # Walk all <aa>/<rest>.jpg files. We don't use rglob because the
    # 2-char-prefix layout is exactly two levels deep by construction
    # and iterdir() + iterdir() is meaningfully cheaper than rglob on
    # large caches (no recursion bookkeeping, no pattern matching).
    entries: List[Tuple[float, int, Path]] = []
    total = 0
    for prefix_dir in root.iterdir():
        if not prefix_dir.is_dir():
            continue
        try:
            children = list(prefix_dir.iterdir())
        except OSError:
            # Permission denied on a single shard — log + skip rather
            # than aborting the whole sweep.
            log.debug("cover-preview sweeper: cannot list %s", prefix_dir)
            continue
        for entry in children:
            if not entry.is_file():
                continue
            # Skip in-flight or orphaned tempfiles from
            # write_to_cache's atomic write-then-rename.
            if entry.name.startswith(".tmp-"):
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            recency = _file_recency(entry)
            entries.append((recency, size, entry))
            total += size

    before = total

    if total <= cap:
        return {
            "before_bytes": before,
            "after_bytes": before,
            "evicted": 0,
            "errors": 0,
            "cap_bytes": cap,
            "cache_root": str(root),
        }

    # Oldest first: stable sort on the recency float.
    entries.sort(key=lambda t: t[0])

    evicted = 0
    errors = 0
    for _recency, size, entry in entries:
        if total <= cap:
            break
        if dry_run:
            total -= size
            evicted += 1
            continue
        try:
            entry.unlink()
        except FileNotFoundError:
            # Race: another process / sweeper deleted it. The bytes
            # are gone from disk regardless, so update accounting and
            # don't count as an error.
            total -= size
            evicted += 1
            continue
        except OSError as exc:
            # Permission denied, read-only FS, etc. Report and move
            # on — one bad file shouldn't stall the rest.
            log.debug("cover-preview sweeper: cannot unlink %s: %s", entry, exc)
            errors += 1
            continue
        total -= size
        evicted += 1

    return {
        "before_bytes": before,
        "after_bytes": total,
        "evicted": evicted,
        "errors": errors,
        "cap_bytes": cap,
        "cache_root": str(root),
    }


def main() -> None:
    """Entry point for the ``cwa-preview-cache-cleanup`` s6 service."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [cwa-preview-cache-cleanup] %(message)s",
    )
    result = sweep(dry_run=False)
    log.info(
        "before=%d after=%d evicted=%d errors=%d cap=%d root=%s",
        result["before_bytes"],
        result["after_bytes"],
        result["evicted"],
        result["errors"],
        result["cap_bytes"],
        result["cache_root"],
    )


if __name__ == "__main__":
    main()
