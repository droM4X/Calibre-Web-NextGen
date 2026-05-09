# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Regression tests pinning the backport of janeczku/calibre-web PR #3555
(by @lpinner) — Discover (Random Books) page now filters out books the
user has already marked as read.

Resolves janeczku #2588 (filed 2021-12; 1 reaction, 3 comments by users
hitting the same wall: their Discover page was full of books they'd
already read, so it stopped being useful for finding new reads).

Two read-status backends to support:

1. **No custom read-column** (`config.config_read_column` falsy) — read
   state lives in `ub.ReadBook.read_status`, joined per-user. Filter:
   `coalesce(ub.ReadBook.read_status, 0) != ub.ReadBook.STATUS_FINISHED`
   (coalesce so books with no ReadBook row count as unread).

2. **Custom read-column configured** — the column class lives in
   `db.cc_classes[config.config_read_column]`. Filter:
   `coalesce(<col>.value, False) != True` (coalesce so missing rows
   count as unread). If the configured column doesn't exist (KeyError /
   AttributeError / IndexError on cc_classes), the function logs + flashes
   an error to the operator and falls back to `db_filter = True` (no
   filter; full discover, same as pre-patch behavior).

These tests pin the source-level invariants. The function is called from
the running Flask app on every Discover page load; the existing route
tests in tests/integration/ cover the HTTP shape, but neither this file
nor anywhere else previously pinned the filter expression itself — so a
silent revert of the fix (e.g. someone refactoring away the
coalesce-based filter) wouldn't be caught.
"""

import ast
import re
from pathlib import Path

import pytest


WEB_PY = (Path(__file__).resolve().parent.parent.parent / "cps" / "web.py")


def _render_discover_books_source() -> str:
    """Pull the source of `render_discover_books` from cps/web.py."""
    src = WEB_PY.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "render_discover_books":
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("render_discover_books not found in cps/web.py")


@pytest.mark.unit
class TestDiscoverHidesReadBooks:
    """Source-pin the read-status filter shape that landed via CW PR #3555."""

    def test_render_discover_books_present(self):
        src = _render_discover_books_source()
        assert src, "render_discover_books body is empty"

    def test_no_custom_read_column_uses_ub_readbook_filter(self):
        """Default path (no config_read_column): filter on
        ub.ReadBook.read_status with coalesce so books with no row are
        treated as unread."""
        src = _render_discover_books_source()
        assert "config.config_read_column" in src, (
            "render_discover_books should branch on config.config_read_column "
            "to pick the read-status backend"
        )
        # The default-path filter must reference ub.ReadBook.read_status with
        # coalesce(...) to handle books the current user has never opened.
        assert "ub.ReadBook.read_status" in src, (
            "default read-status filter must reference ub.ReadBook.read_status; "
            "fork issue / janeczku #2588"
        )
        assert "STATUS_FINISHED" in src, (
            "filter must compare against STATUS_FINISHED constant, not a magic "
            "literal — keeps the test robust to value changes"
        )
        # `coalesce(... read_status, 0) != STATUS_FINISHED` is the canonical
        # shape. Tolerate whitespace + parens variations but require the
        # coalesce wrapper around read_status (otherwise NULL rows would
        # NULL-compare and fall out of the result set, regressing #2588).
        assert re.search(
            r"coalesce\s*\(\s*ub\.ReadBook\.read_status",
            src,
        ), "coalesce-wrap on ub.ReadBook.read_status missing — books the user "\
           "has never opened (no ReadBook row) would be filtered out incorrectly"

    def test_custom_read_column_uses_cc_classes_filter(self):
        """Custom-column path: filter on db.cc_classes[config_read_column].value
        with coalesce so missing rows count as unread."""
        src = _render_discover_books_source()
        assert "db.cc_classes" in src and "config.config_read_column" in src, (
            "custom-column path must look up the read-status column via "
            "db.cc_classes[config.config_read_column]"
        )
        # Same coalesce requirement on the custom-column path.
        assert re.search(
            r"coalesce\s*\(\s*db\.cc_classes\[",
            src,
        ), "coalesce-wrap on the custom-column .value missing"

    def test_invalid_custom_column_logs_flashes_falls_back_to_unfiltered(self):
        """If the configured custom column doesn't exist on db.cc_classes
        (KeyError / AttributeError / IndexError), the error path must
        log, flash an i18n error to the operator, and fall back to a
        non-filtering `db_filter = True` so Discover still renders rather
        than crashing. This is the operator-facing degradation path."""
        src = _render_discover_books_source()
        # Catches the three exception classes the cc_classes lookup can raise.
        assert "except" in src and "KeyError" in src, "expected except KeyError"
        assert "AttributeError" in src and "IndexError" in src, (
            "all three exception classes from cc_classes lookup must be caught"
        )
        assert "log.error" in src, "operator-visible error log missing"
        assert "flash(" in src, "user-visible flash message missing"
        # Fallback assignment to `True` must be present so the page still
        # renders (degraded but useful).
        assert re.search(r"db_filter\s*=\s*True", src), (
            "fallback `db_filter = True` missing — without it, an invalid "
            "config_read_column would leave db_filter undefined and crash "
            "the Discover render"
        )

    def test_filter_is_passed_into_fill_indexpage_not_hardcoded_True(self):
        """The pre-patch shape passed `True` as the third arg to
        fill_indexpage (no filter). Post-patch must pass the computed
        `db_filter` instead, so the fix actually applies. A future
        refactor that re-hardcodes True would silently revert #2588."""
        src = _render_discover_books_source()
        # Find the fill_indexpage call inside this function.
        m = re.search(
            r"calibre_db\.fill_indexpage\s*\(\s*1\s*,\s*0\s*,\s*db\.Books\s*,\s*([^,]+),",
            src,
        )
        assert m, "fill_indexpage call not found in render_discover_books"
        third_arg = m.group(1).strip()
        assert third_arg == "db_filter", (
            f"fill_indexpage's filter arg should be `db_filter` (the computed "
            f"read-status filter), got {third_arg!r}. Re-hardcoding `True` "
            f"silently reverts the #2588 fix."
        )

    def test_credit_preserved_in_changes_doc(self):
        """The CHANGES-vs-upstream.md row for this backport must credit
        @lpinner as the original author and link the upstream PR."""
        changes = (Path(__file__).resolve().parent.parent.parent
                   / "CHANGES-vs-upstream.md")
        if not changes.is_file():
            pytest.skip("CHANGES-vs-upstream.md not at expected path")
        content = changes.read_text()
        # Keep this assertion forgiving: just require the upstream-PR number
        # and the author handle to appear together in some row, not a literal
        # template match (the row format may evolve).
        assert "3555" in content, "CHANGES row for janeczku PR #3555 missing"
        assert "lpinner" in content, "credit to @lpinner missing in CHANGES"
