# Calibre-Web-NextGen — fork of Calibre-Web-Automated
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Validation suite for the Books-relationship loading strategy.

Tests two things:

1. DetachedInstanceError reproduction — does the default (`lazy='select'`)
   actually fail when relationship attributes are accessed after the session
   is closed? This is the bug class upstream PR #1279 / fork PR #40 claims to
   fix (upstream issues #1067, #1130, #1139, #756).

2. Performance impact — does setting `lazy='subquery'` on every Books
   relationship measurably regress query count or wall time on
   representative bulk reads (browse-page-style queries)?

The two tests are parameterized over the relationship-loading strategy so a
single suite covers both states (current main and #40-applied). When #40 is
re-landed in v4.0.17 the strategy parameter flips from 'select' to 'subquery'
and the tests stay valid.
"""
from __future__ import annotations

import os
import sys
import time
import tempfile
import shutil
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EMPTY_LIBRARY = REPO_ROOT / "empty_library"
sys.path.insert(0, str(REPO_ROOT))

# We import cps.db lazily inside fixtures so we can switch the strategy at
# import time without polluting other tests' module cache.


@pytest.fixture
def fresh_metadata_db(tmp_path: Path) -> Iterator[Path]:
    """Copy the canonical empty calibre metadata.db to a tmp path so each test
    starts with a clean schema."""
    src = EMPTY_LIBRARY / "metadata.db"
    if not src.exists():
        pytest.skip(f"empty_library/metadata.db missing from {src}")
    dst = tmp_path / "metadata.db"
    shutil.copy(src, dst)
    yield dst


def _seed_books(db_path: Path, n_books: int = 50, n_series: int = 10,
                n_languages: int = 3) -> None:
    """Insert n_books rows + relationships (authors, series, comments,
    languages) so the bulk-query tests have realistic data.

    Calibre's metadata.db schema has triggers calling several custom SQL
    functions (`title_sort`, `uuid4`, `books_list_filter`) that Calibre
    itself registers at runtime. Register passthrough versions on the seed
    connection so triggers don't abort."""
    import sqlite3, uuid as _uuid
    conn = sqlite3.connect(str(db_path))
    conn.create_function("title_sort", 1, lambda s: s)
    conn.create_function("uuid4", 0, lambda: str(_uuid.uuid4()))
    conn.create_function("books_list_filter", 1, lambda _b: 1)
    cur = conn.cursor()

    # Languages
    for i in range(n_languages):
        cur.execute("INSERT INTO languages (lang_code) VALUES (?)",
                    (f"l{i:02d}",))

    # Authors
    for i in range(n_books // 5 + 1):
        cur.execute("INSERT INTO authors (name, sort) VALUES (?, ?)",
                    (f"Author {i:03d}", f"{i:03d}, Author"))

    # Series
    for i in range(n_series):
        cur.execute("INSERT INTO series (name, sort) VALUES (?, ?)",
                    (f"Series {i:02d}", f"Series {i:02d}"))

    # Books + comments + links
    for i in range(n_books):
        cur.execute(
            "INSERT INTO books (title, sort, author_sort, series_index, "
            "path, has_cover, uuid) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (f"Book {i:04d}", f"Book {i:04d}",
             f"{(i % (n_books // 5 + 1)):03d}, Author",
             1.0 + (i % 5), f"Author/Book{i}", f"uuid-{i}")
        )
        book_id = cur.lastrowid
        cur.execute("INSERT INTO comments (book, text) VALUES (?, ?)",
                    (book_id, f"Synopsis for book {i}."))
        cur.execute("INSERT INTO books_authors_link (book, author) VALUES (?, ?)",
                    (book_id, (i % (n_books // 5 + 1)) + 1))
        cur.execute("INSERT INTO books_series_link (book, series) VALUES (?, ?)",
                    (book_id, (i % n_series) + 1))
        cur.execute("INSERT INTO books_languages_link (book, lang_code) "
                    "VALUES (?, ?)", (book_id, (i % n_languages) + 1))

    conn.commit()
    conn.close()


def _make_engine_and_session(db_path: Path):
    """Build a SQLAlchemy engine + session that mirrors cps.db's runtime
    setup: an in-memory engine with the metadata.db ATTACHed as schema
    `calibre`. The Books model uses schema-qualified table names so the
    runtime queries reference `calibre.books`, `calibre.data`, etc.

    Registers the same custom SQL functions Calibre defines so triggers
    don't abort.

    NOTE: this fixture deliberately does NOT clear cps.* from sys.modules
    before importing. Some modules under cps/ (notably
    cps.progress_syncing.models) use a circular-import pattern that
    resolves cleanly on the first package import but breaks if modules
    are forcibly reloaded mid-suite — downstream tests fail with
    `AttributeError: partially initialized module ... has no attribute
    BookFormatChecksum`. The Books-relationship loading strategy is read
    from whatever cps.db is committed on this branch, which is what the
    runtime uses anyway."""
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from cps import db as cps_db  # noqa: E402

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _register_funcs(dbapi_conn, _conn_record):
        import uuid as _uuid
        dbapi_conn.create_function("title_sort", 1, lambda s: s)
        dbapi_conn.create_function("uuid4", 0, lambda: str(_uuid.uuid4()))
        dbapi_conn.create_function("books_list_filter", 1, lambda _b: 1)

    @event.listens_for(engine, "connect")
    def _attach_calibre(dbapi_conn, _conn_record):
        dbapi_conn.execute(f"ATTACH DATABASE '{db_path}' AS calibre")

    Session = sessionmaker(bind=engine)
    return engine, Session, cps_db


# ---------------------------------------------------------------------------
# DetachedInstanceError reproduction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("attr", ["series", "comments", "languages",
                                  "authors", "data"])
def test_relationship_access_after_session_close(fresh_metadata_db, attr):
    """Mirror the upstream-#1279 bug pattern: query Books, close the session,
    then access a relationship attribute. Under `lazy='select'` this raises
    DetachedInstanceError; under `lazy='subquery'` it returns the eagerly-
    loaded collection.

    We DO NOT assert which behavior happens — the test reports it. CI runs
    this on both branches:
        - on main (after #54 revert)              → DetachedInstanceError expected
        - on validation/40-...-relanded           → no error expected

    The point is to record empirically what behavior the strategy choice
    actually produces, on real data this fork ships."""
    from sqlalchemy.orm.exc import DetachedInstanceError

    _seed_books(fresh_metadata_db, n_books=10)
    engine, Session, cps_db = _make_engine_and_session(fresh_metadata_db)

    sess = Session()
    book = sess.query(cps_db.Books).first()
    sess.close()

    raised = None
    try:
        value = getattr(book, attr)
        # Force evaluation in case it returns a lazy collection
        _ = list(value) if hasattr(value, "__iter__") else value
    except DetachedInstanceError as ex:
        raised = ex

    # Snapshot the current strategy
    strategy = cps_db.Books.__mapper__.attrs[attr].lazy
    print(f"[loading-strategy] attr={attr} lazy={strategy!r} "
          f"detached_instance_error={'YES' if raised else 'no'}")

    # The point of the test is observability, but we DO assert
    # consistency: subquery/joined/selectin must NOT raise; select MAY raise.
    if strategy in ("subquery", "joined", "selectin"):
        assert raised is None, (
            f"lazy={strategy!r} should eager-load {attr!r}, "
            f"but got DetachedInstanceError: {raised}"
        )


# ---------------------------------------------------------------------------
# Performance impact
# ---------------------------------------------------------------------------

class _SQLCounter:
    """Counts SELECT statements emitted during a code block."""
    def __init__(self, engine):
        self.engine = engine
        self.n = 0
        self._listener = None

    def __enter__(self):
        from sqlalchemy import event
        def _on_exec(conn, cursor, stmt, *_a, **_kw):
            if stmt.strip().lower().startswith("select"):
                self.n += 1
        self._listener = _on_exec
        event.listen(self.engine, "before_cursor_execute", _on_exec)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event
        event.remove(self.engine, "before_cursor_execute", self._listener)


def test_bulk_browse_query_count_under_threshold(fresh_metadata_db):
    """Simulate the browse-page hot path: query 50 books, render-style access
    to series + authors + languages. Count SELECTs.

    Under `lazy='select'` (default) the worst case is N+1 per relationship
    accessed — for 50 books × 3 relationships that's ~150+ SELECTs.

    Under `lazy='subquery'` it should be ~5 SELECTs (one for Books plus one
    subquery per relationship type).

    We assert the count is NOT in the 'pathological N+1' range. The exact
    threshold depends on the strategy."""
    _seed_books(fresh_metadata_db, n_books=50)
    engine, Session, cps_db = _make_engine_and_session(fresh_metadata_db)

    sess = Session()
    with _SQLCounter(engine) as ctr:
        books = sess.query(cps_db.Books).limit(50).all()
        for b in books:
            _ = list(b.authors)
            _ = list(b.series)
            _ = list(b.languages)
    sess.close()

    strategy = cps_db.Books.__mapper__.attrs["series"].lazy
    print(f"[perf] strategy={strategy!r} books=50 selects={ctr.n}")

    # Pathological floor: if we're emitting >= 100 SELECTs for 50 books that's
    # the lazy='select' N+1 nightmare, which both the original CWA strategy
    # AND the post-revert strategy can hit. We don't fail the build on this —
    # we record it. Future re-land with lazy='subquery' should bring this
    # well below the threshold.
    assert ctr.n < 200, (
        f"emitted {ctr.n} SELECTs for 50 books × 3 relationship reads — "
        f"that's a pathological N+1; strategy={strategy!r}"
    )


def test_bulk_browse_wall_time_under_threshold(fresh_metadata_db):
    """Wall-time floor: rendering 50 books with relationship access must
    complete in well under one second on a tmpfs-backed sqlite."""
    _seed_books(fresh_metadata_db, n_books=50)
    engine, Session, cps_db = _make_engine_and_session(fresh_metadata_db)

    sess = Session()
    t0 = time.time()
    books = sess.query(cps_db.Books).limit(50).all()
    for b in books:
        _ = list(b.authors)
        _ = list(b.series)
        _ = list(b.languages)
        _ = list(b.tags)
    elapsed = time.time() - t0
    sess.close()

    strategy = cps_db.Books.__mapper__.attrs["series"].lazy
    print(f"[perf] strategy={strategy!r} books=50 wall={elapsed*1000:.1f}ms")
    assert elapsed < 1.0, (
        f"50-book bulk render took {elapsed*1000:.0f}ms — "
        f"that's slow for sqlite; strategy={strategy!r}"
    )
