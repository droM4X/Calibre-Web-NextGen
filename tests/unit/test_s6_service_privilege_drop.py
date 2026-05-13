"""Source-pin that the auto-zipper service drops privileges to abc
before invoking auto_zip.py.

Background — issue #162: the auto-zipper service called
``python3 /app/calibre-web-automated/scripts/auto_zip.py`` directly,
without ``s6-setuidgid abc``. The resulting nightly .zip archives in
``/config/processed_books/fixed_originals/`` were owned by ``root:root``
while .epub outputs in the same directory — produced by
cwa-ingest-service, which already drops privileges — were owned by
PUID:PGID. The mismatch broke host-side cleanup and backup workflows
for any deployment where PUID isn't 0.

This test is narrow on purpose: it pins the exact regression the user
reported. A broader audit of every long-running service is tracked in
``notes/s6-privilege-drop-audit.md`` — several services (e.g.
metadata-change-detector) run as root but rely on downstream Python
helpers calling ``os.chown`` themselves, which is structurally
different from the auto_zip pattern and needs per-service analysis.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTO_ZIPPER_RUN = (
    REPO_ROOT
    / "root"
    / "etc"
    / "s6-overlay"
    / "s6-rc.d"
    / "cwa-auto-zipper"
    / "run"
)


def test_cwa_auto_zipper_invokes_auto_zip_under_s6_setuidgid():
    """Every uncommented invocation of auto_zip.py in the auto-zipper
    run script must be wrapped by ``s6-setuidgid abc``."""
    assert AUTO_ZIPPER_RUN.exists(), f"missing {AUTO_ZIPPER_RUN}"
    text = AUTO_ZIPPER_RUN.read_text()
    assert "auto_zip.py" in text, "cwa-auto-zipper run script no longer references auto_zip.py"

    setuid_pattern = re.compile(
        r"\bs6-setuidgid\s+abc\b.*python3?\b.*auto_zip\.py"
    )
    offenders = []
    saw_invocation = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if "auto_zip.py" not in line:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue  # documentation comment, not an invocation
        saw_invocation = True
        if not setuid_pattern.search(line):
            offenders.append(f"line {lineno}: {stripped}")
    assert saw_invocation, (
        "no live invocation of auto_zip.py found — did the service get "
        "renamed or rewritten?"
    )
    assert not offenders, (
        "cwa-auto-zipper invokes auto_zip.py without `s6-setuidgid abc` — "
        f"would regress issue #162: {offenders}"
    )


def test_cwa_ingest_service_still_uses_s6_setuidgid_for_python():
    """Sanity-anchor for the comparison case in the #162 bug report:
    cwa-ingest-service drops privs before invoking ingest_processor.py,
    which is why .epub outputs in fixed_originals are PUID-owned. If
    this assertion breaks, the regression test above loses its
    comparator."""
    run = REPO_ROOT / "root" / "etc" / "s6-overlay" / "s6-rc.d" / "cwa-ingest-service" / "run"
    assert run.exists(), f"missing {run}"
    text = run.read_text()
    assert re.search(
        r"s6-setuidgid\s+abc\s+(?:\S+\s+)*python3?\s+/app/calibre-web-automated/scripts/ingest_processor\.py",
        text,
    ), (
        "cwa-ingest-service no longer wraps ingest_processor.py with "
        "`s6-setuidgid abc`. The structural comparison underlying issue "
        "#162 has changed — re-evaluate the auto-zipper fix."
    )
