# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Regression test for upstream issue crocodilestick/Calibre-Web-Automated#817.

Symptom: visiting Profile -> Create/View Kobo Auth Token enqueues a kepub
conversion task for every EPUB in the library, regardless of whether the
book is on a Kobo Sync shelf. On large libraries (200k+ books) this floods
the worker queue, triggers a flood of `Skipping kepub auto-conversion`
warnings for un-convertible files, and starves the actual /v1/library/sync
calls until the container is marked unhealthy.

Fix: scope the auto-conversion to books on the user's Kobo Sync shelves
only. Books outside those shelves are still converted on-demand at sync
time by the existing Kobo flow.

This test pins the shelf-scope behavior so a refactor can't reintroduce
the unbounded library walk.
"""

import ast
import pathlib
import pytest


KOBO_AUTH = pathlib.Path(__file__).resolve().parent.parent.parent / "cps" / "kobo_auth.py"


@pytest.mark.smoke
class TestKoboAuthShelfScopedConvert:
    def setup_method(self):
        self.source = KOBO_AUTH.read_text()
        self.tree = ast.parse(self.source)
        self.fn = next(
            (n for n in ast.walk(self.tree)
             if isinstance(n, ast.FunctionDef) and n.name == "generate_auth_token"),
            None,
        )
        assert self.fn is not None, "generate_auth_token function not found"
        self.fn_text = ast.unparse(self.fn)

    def test_filters_by_kobo_sync_shelf(self):
        assert "kobo_sync" in self.fn_text, (
            "generate_auth_token must filter by Shelf.kobo_sync to avoid "
            "queuing a kepub convert for every book in the library"
        )

    def test_joins_book_shelf_link(self):
        # The shelf scope is computed by joining BookShelf to Shelf and filtering
        # by user_id + kobo_sync. Catching either name pins the structural change.
        assert "BookShelf" in self.fn_text, (
            "Must enumerate book_shelf_link rows to scope conversion to shelf members"
        )
        assert "Shelf" in self.fn_text

    def test_no_unbounded_books_query(self):
        # Legacy code did `query(Books).options(joinedload(...)).all()` with no
        # filter, walking the entire library. Pin that exact pattern out.
        for node in ast.walk(self.fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "all":
                # Walk back through the chained call to find any .filter()
                cursor = node.func.value
                has_filter = False
                while isinstance(cursor, ast.Call) and isinstance(cursor.func, ast.Attribute):
                    if cursor.func.attr in ("filter", "filter_by"):
                        has_filter = True
                        break
                    cursor = cursor.func.value
                # Only enforce on calibre_db Books queries, not on the ub.session shelf query.
                chain_text = ast.unparse(node)
                if "db.Books" in chain_text:
                    assert has_filter, (
                        "calibre_db.session.query(db.Books).....all() must include a "
                        ".filter(Books.id.in_(...)) to scope to shelf members; the "
                        "unscoped .all() is the #817 regression."
                    )
