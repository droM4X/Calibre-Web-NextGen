# Calibre-Web-NextGen — fork of Calibre-Web-Automated
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Validation suite for #37 (s6-setuidgid abc on ingest python workers).

The patch is architecturally correct (web UI runs as abc; ingest used to
run as root, leaving root:root-owned books that the web UI couldn't
delete). But it introduces a regression risk for users whose libraries
already contain root-owned author/series subdirectories from before the
patch landed: post-#37, an ingest of a new book targeting an existing
root-owned author dir fails with EACCES.

These tests prove the regression empirically by running the actual
production container image and exercising the filesystem transitions an
ingest worker performs. They also verify the chown migration that needs
to ship alongside #37 actually heals the regression.

The tests are skipped when Docker is unavailable, so the suite still
imports cleanly during a unit-only `pytest` run.
"""
from __future__ import annotations

import shutil
import subprocess
import textwrap

import pytest


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                       capture_output=True, check=True, timeout=5)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="docker required to exercise s6-setuidgid abc permission semantics",
)


# Image used to host the regression scenarios. The /tmp directory inside
# any Calibre-Web-NextGen image already has the abc user defined and
# `s6-setuidgid` available; the test does not touch the real ingest
# pipeline, only the filesystem-permission transitions an ingest_processor
# performs at the os layer.
IMAGE = "ghcr.io/new-usemame/calibre-web-nextgen:v4.0.15"


def _run(script: str) -> subprocess.CompletedProcess:
    """Run a bash script inside a disposable container and return the
    completed process (stdout/stderr captured)."""
    return subprocess.run(
        [
            "docker", "run", "--rm",
            "--entrypoint", "",
            IMAGE,
            "bash", "-c", textwrap.dedent(script),
        ],
        capture_output=True, text=True, timeout=120,
    )


def test_pre37_root_worker_can_write_into_root_owned_subdir():
    """Establish baseline: an ingest worker running as root (pre-#37
    behavior) successfully writes into a root-owned author directory."""
    proc = _run("""
        set -e
        mkdir -p /tmp/lib/root-author /tmp/ingest
        chown root:root /tmp/lib/root-author
        echo fake > /tmp/ingest/b.epub
        cp /tmp/ingest/b.epub /tmp/lib/root-author/b.epub
        ls -la /tmp/lib/root-author/b.epub
    """)
    assert proc.returncode == 0, f"baseline FAILED: {proc.stderr}"
    assert "/tmp/lib/root-author/b.epub" in proc.stdout


def test_post37_abc_worker_blocked_by_root_owned_subdir():
    """The regression — with #37 applied, an ingest worker running as abc
    cannot write into a pre-existing root-owned author directory."""
    proc = _run("""
        mkdir -p /tmp/lib/root-author /tmp/ingest
        chown root:root /tmp/lib/root-author
        echo fake > /tmp/ingest/b.epub
        chown abc:abc /tmp/ingest/b.epub
        s6-setuidgid abc cp /tmp/ingest/b.epub /tmp/lib/root-author/b.epub 2>&1
        echo EXIT=$?
        [ -f /tmp/lib/root-author/b.epub ] && echo MARKER=present || echo MARKER=absent
    """)
    # The cp itself should print "Permission denied" and exit nonzero,
    # the file should be absent.
    assert "Permission denied" in proc.stdout, \
        f"expected EACCES output, got: {proc.stdout}"
    assert "MARKER=absent" in proc.stdout, \
        "regression-canary file present — EACCES did not actually block write"


def test_post37_abc_worker_unblocked_after_chown_migration():
    """The fix — applying `chown -R abc:abc /calibre-library` once heals
    the regression and lets the abc-uid ingest worker write into every
    author directory regardless of prior ownership."""
    proc = _run("""
        set -e
        mkdir -p /tmp/lib/root-author /tmp/lib/abc-author /tmp/ingest
        chown root:root /tmp/lib/root-author
        chown abc:abc   /tmp/lib/abc-author
        echo fake > /tmp/ingest/b.epub
        chown abc:abc /tmp/ingest/b.epub
        chown -R abc:abc /tmp/lib  # the proposed migration
        s6-setuidgid abc cp /tmp/ingest/b.epub /tmp/lib/root-author/b.epub
        s6-setuidgid abc cp /tmp/ingest/b.epub /tmp/lib/abc-author/b.epub
        ls /tmp/lib/root-author/b.epub /tmp/lib/abc-author/b.epub
    """)
    assert proc.returncode == 0, \
        f"post-migration ingest FAILED: stderr={proc.stderr} stdout={proc.stdout}"
    assert "/tmp/lib/root-author/b.epub" in proc.stdout
    assert "/tmp/lib/abc-author/b.epub" in proc.stdout


def test_post37_abc_worker_writes_into_clean_library():
    """Confirm #37 does not regress the happy path: a clean library with
    all-abc-owned subdirectories accepts ingest writes from the abc
    worker."""
    proc = _run("""
        set -e
        mkdir -p /tmp/lib/abc-author /tmp/ingest
        chown abc:abc /tmp/lib/abc-author
        echo fake > /tmp/ingest/b.epub
        chown abc:abc /tmp/ingest/b.epub
        s6-setuidgid abc cp /tmp/ingest/b.epub /tmp/lib/abc-author/b.epub
        stat -c '%U:%G' /tmp/lib/abc-author/b.epub
    """)
    assert proc.returncode == 0
    assert "abc:abc" in proc.stdout, \
        f"new book should land abc-owned, got: {proc.stdout}"


# ---------------------------------------------------------------------------
# Migration script verification — runs the actual cwa-chown-library-migration
# /run script that ships with v4.0.17. The script is copied INTO the test
# container from the source tree so the test exercises the real artifact.
# ---------------------------------------------------------------------------

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_SCRIPT = REPO_ROOT / "root/etc/s6-overlay/s6-rc.d/cwa-chown-library-migration/run"


_MIGRATION_BODY: str | None = None


def _migration_body() -> str:
    """Read the migration script text once. Embedded into test scripts via
    heredoc so the test works under both local Docker and Docker-over-SSH
    where bind-mounts of host paths don't resolve to the test runner's
    filesystem."""
    global _MIGRATION_BODY
    if _MIGRATION_BODY is None:
        _MIGRATION_BODY = MIGRATION_SCRIPT.read_text()
    return _MIGRATION_BODY


def _run_with_migration_script(script: str,
                                env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a bash script inside a disposable container after writing the
    real migration script to /usr/local/bin/cwa-chown-migrate from a
    heredoc. This intentionally avoids host-path bind mounts so the test
    behaves the same under local Docker and Docker-over-SSH."""
    body = _migration_body().replace("'", "'\\''")
    cmd = [
        "docker", "run", "--rm",
        "--entrypoint", "",
    ]
    for k, v in (env or {}).items():
        cmd.extend(["-e", f"{k}={v}"])
    cmd.extend([
        IMAGE,
        "bash", "-c",
        f"cat > /usr/local/bin/cwa-chown-migrate <<'CWAMIG_EOF'\n"
        f"{_migration_body()}\nCWAMIG_EOF\n"
        f"chmod +x /usr/local/bin/cwa-chown-migrate\n"
        f"{textwrap.dedent(script)}",
    ])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def test_migration_script_chowns_library_and_writes_sentinel():
    """When run on a fresh container with a mixed-ownership library, the
    real migration script aligns everything to abc:abc and drops the
    sentinel marker so subsequent boots no-op."""
    proc = _run_with_migration_script("""
        set -e
        mkdir -p /calibre-library/root-author /calibre-library/abc-author /cwa-book-ingest /config
        chown root:root /calibre-library/root-author
        chown abc:abc   /calibre-library/abc-author
        chown root:root /cwa-book-ingest

        # Run the real migration script
        bash /usr/local/bin/cwa-chown-migrate

        # Verify
        stat -c '%U:%G %n' /calibre-library/root-author /calibre-library/abc-author /cwa-book-ingest
        [ -f /config/.cwa-chown-library-done ] && echo "sentinel: present" || echo "sentinel: ABSENT"
    """)
    assert proc.returncode == 0, f"migration FAILED: {proc.stderr}"
    assert "abc:abc /calibre-library/root-author" in proc.stdout, \
        f"root-author dir not migrated to abc:abc: {proc.stdout}"
    assert "abc:abc /cwa-book-ingest" in proc.stdout
    assert "sentinel: present" in proc.stdout


def test_migration_script_is_idempotent():
    """A second invocation of the migration script must no-op: the
    sentinel file stays, ownership is unchanged, and the script logs that
    it skipped."""
    proc = _run_with_migration_script("""
        set -e
        mkdir -p /calibre-library /config
        chown abc:abc /calibre-library
        touch /config/.cwa-chown-library-done   # simulate prior run

        # Drop a root-owned dir AFTER the sentinel is in place — script
        # must NOT chown it because the sentinel says we already migrated
        mkdir /calibre-library/post-migration-root
        chown root:root /calibre-library/post-migration-root

        bash /usr/local/bin/cwa-chown-migrate 2>&1

        stat -c '%U:%G %n' /calibre-library/post-migration-root
    """)
    assert proc.returncode == 0
    assert "sentinel present, skipping" in proc.stdout, \
        f"second run did not skip: {proc.stdout}"
    assert "root:root /calibre-library/post-migration-root" in proc.stdout, \
        "post-migration root-owned dir was unexpectedly chowned"


def test_migration_script_skips_under_network_share_mode():
    """When NETWORK_SHARE_MODE is true, the script must NOT chown over the
    network — it logs guidance and writes the sentinel so the skip is
    recorded."""
    proc = _run_with_migration_script("""
        set -e
        mkdir -p /calibre-library/root-author /config
        chown root:root /calibre-library/root-author

        bash /usr/local/bin/cwa-chown-migrate 2>&1

        stat -c '%U:%G %n' /calibre-library/root-author
        [ -f /config/.cwa-chown-library-done ] && echo "sentinel: present" || echo "sentinel: ABSENT"
    """, env={"NETWORK_SHARE_MODE": "true"})
    assert proc.returncode == 0
    assert "NETWORK_SHARE_MODE=true" in proc.stdout
    assert "skipping migration" in proc.stdout
    assert "root:root /calibre-library/root-author" in proc.stdout, \
        "NFS skip path should not have chowned anything"
    assert "sentinel: present" in proc.stdout


def test_post_migration_ingest_into_root_owned_unblocked():
    """End-to-end: run the real migration script over a mixed-ownership
    library, then prove the abc-uid ingest worker can now write into what
    used to be a root-owned author directory."""
    proc = _run_with_migration_script("""
        set -e
        mkdir -p /calibre-library/root-author /cwa-book-ingest /config
        chown root:root /calibre-library/root-author
        echo fake > /cwa-book-ingest/b.epub
        chown abc:abc /cwa-book-ingest/b.epub

        bash /usr/local/bin/cwa-chown-migrate

        s6-setuidgid abc cp /cwa-book-ingest/b.epub /calibre-library/root-author/b.epub
        stat -c '%U:%G %n' /calibre-library/root-author/b.epub
    """)
    assert proc.returncode == 0
    assert "abc:abc /calibre-library/root-author/b.epub" in proc.stdout, \
        f"post-migration ingest failed: {proc.stdout}\n{proc.stderr}"
