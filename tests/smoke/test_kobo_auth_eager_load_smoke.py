# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Regression test for upstream issue #1328 / fork issue #50.

Symptom: Profile -> Create/View Kobo Auth Token returned a blank page with
    sqlite3.InterfaceError: bad parameter or other API misuse
    (cps/kobo_auth.py line ~104, lazy load of book.data)

Root cause: `query(Books).join(Data).all()` returned duplicate book rows
(one per format), then `book.data` lazy-loaded again per row -- N+1 queries
on the request-scoped session. When a worker thread crashed mid-request
(`worker:237 list index out of range` is in the original report just above
the trace), subsequent lazy-loads hit a poisoned connection and raised the
SQLite InterfaceError, blanking the page.

This test pins the structural change so a future refactor can't reintroduce
the lazy-load pattern: the kepub auto-conversion path must use joinedload,
must skip the explicit JOIN-with-duplicates, and must wrap each per-book
enqueue in a try/except so one bad book doesn't blank-page the user.
"""

import ast
import pathlib
import pytest


KOBO_AUTH = pathlib.Path(__file__).resolve().parent.parent.parent / "cps" / "kobo_auth.py"


@pytest.mark.smoke
class TestKoboAuthGenerateAuthTokenEagerLoad:
    def setup_method(self):
        self.source = KOBO_AUTH.read_text()
        self.tree = ast.parse(self.source)
        self.fn = next(
            (n for n in ast.walk(self.tree)
             if isinstance(n, ast.FunctionDef) and n.name == "generate_auth_token"),
            None,
        )
        assert self.fn is not None, "generate_auth_token function not found"

    def test_imports_joinedload(self):
        assert "from sqlalchemy.orm import joinedload" in self.source, \
            "joinedload import is required to avoid the N+1 lazy-load that triggered #1328"

    def test_uses_joinedload_on_books_data(self):
        joinedload_calls = [
            n for n in ast.walk(self.fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "joinedload"
        ]
        assert joinedload_calls, "generate_auth_token must call joinedload(...) on Books.data"

    def test_no_explicit_join_data(self):
        # The legacy `.join(db.Data)` pattern returned one row per (book, data)
        # tuple, multiplying lazy loads -- that's exactly what crashed under load.
        attr_chain_text = ast.unparse(self.fn)
        assert ".join(db.Data)" not in attr_chain_text, \
            "Stale .join(db.Data) — duplicates books N times where N=len(formats)"

    def test_per_book_conversion_is_guarded(self):
        # The kepub auto-conversion loop must wrap each book in try/except so
        # a single bad book doesn't bubble up and blank-page the route.
        for_nodes = [n for n in ast.walk(self.fn) if isinstance(n, ast.For)]
        guarded = [
            f for f in for_nodes
            if any(isinstance(stmt, ast.Try) for stmt in f.body)
        ]
        assert guarded, "Per-book conversion loop must contain a try/except guard"

    def test_kepub_check_short_circuits_when_disabled(self):
        # When config_kepubifypath is unset there's nothing to convert; we shouldn't
        # walk every book in the library at all (cheap perf + avoids the N+1 path).
        attr_chain_text = ast.unparse(self.fn)
        assert "config.config_kepubifypath" in attr_chain_text, \
            "config_kepubifypath gate must be checked before the books query"
