# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Regression test for upstream CWA #1183.

Symptom: when KOReader sync is disabled, embedding metadata into a book
file still tries to write into the `book_format_checksums` table — a
table that doesn't exist in instances that have never enabled KOReader
sync. The error surfaces in logs as "Failed to store checksum for
book N: no such table: book_format_checksums".

Fix: gate the `calculate_and_store_checksum` call in
`cps/helper.py` on `is_koreader_sync_enabled()`. When sync is off, no
checksum work is attempted, no log noise.

This file uses a static-source assertion (the same pattern as the
cover-picker Kobo preview tests) since exercising the
`do_calibre_export` path live requires the full Calibre worker init.
"""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


class TestHelperKoreaderChecksumGuard:
    HELPER_FILE = REPO_ROOT / "cps" / "helper.py"

    def _read(self) -> str:
        return self.HELPER_FILE.read_text(encoding="utf-8")

    def test_calculate_and_store_checksum_call_is_present(self):
        # Sanity: the call we're guarding exists.
        src = self._read()
        assert "calculate_and_store_checksum(" in src

    def test_call_site_is_gated_on_is_koreader_sync_enabled(self):
        # Regression: the call must be gated. Otherwise instances with
        # sync disabled spam "no such table: book_format_checksums" in
        # logs on every metadata save.
        src = self._read()
        call_idx = src.find("calculate_and_store_checksum(")
        assert call_idx != -1

        # Look for the gate within the surrounding ~600-char window.
        # The gate function lives in cps.progress_syncing.settings and
        # is imported / referenced inside the if-block that wraps the
        # call.
        window_start = max(0, call_idx - 600)
        window = src[window_start:call_idx + 200]
        assert "is_koreader_sync_enabled" in window, (
            "calculate_and_store_checksum call must be gated on "
            "is_koreader_sync_enabled() — see CWA #1183"
        )


class TestStoreChecksumPureUtility:
    """The pure-utility functions in
    cps/progress_syncing/checksums/manager.py stay pure — no flag check
    inside them, so backfill paths can still invoke them when the user
    has enabled sync mid-flight. The guard lives at the call site, not
    in the function body."""

    MANAGER_FILE = REPO_ROOT / "cps" / "progress_syncing" / "checksums" / "manager.py"

    def _read(self) -> str:
        return self.MANAGER_FILE.read_text(encoding="utf-8")

    def test_calculate_and_store_checksum_does_not_check_setting_internally(self):
        # The function should remain a pure utility. Adding the flag
        # check inside it would break the explicit-backfill path used
        # when a user enables sync mid-instance — that path needs to
        # write checksums even though `is_koreader_sync_enabled` only
        # flips True after the first save.
        src = self._read()
        func_idx = src.find("def calculate_and_store_checksum(")
        assert func_idx != -1
        # Find the next def at module scope to bound the function body.
        next_def_idx = src.find("\ndef ", func_idx + 1)
        body = src[func_idx:next_def_idx if next_def_idx != -1 else len(src)]
        assert "is_koreader_sync_enabled" not in body, (
            "calculate_and_store_checksum should stay pure — gate at "
            "the call site instead. See test rationale in this file."
        )
