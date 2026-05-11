# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Concurrent-insert test for the Kobo reading-state race fix
(audit 2026-05-11, item B2).

The audit's specific concern: two simultaneous PUTs to
``/v1/library/<uuid>/state`` from the same Kobo can race past the
read-then-write check in ``get_or_create_reading_state`` and both
insert, leaving the DB with duplicate (user_id, book_id) rows that
later 500 the device with MultipleResultsFound.

This test simulates the race directly. It fires N threads at the
SQLite ``INSERT ... ON CONFLICT(user_id, book_id) DO NOTHING`` upsert
path used by the fix and asserts that exactly one row exists
afterwards, regardless of contention.

We don't try to drive a full Flask request stack here — the ORM
operations are what the fix changed, and they're what we want
pinned. The test goes through ``sqlite_insert(...).on_conflict_do_nothing(...)``
on a real SQLite DB to validate end-to-end behavior, not a mock.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def shared_file_db(tmp_path):
    """File-backed (not :memory:) SQLite engine — threading semantics
    on :memory: are connection-local, defeating the race we want to
    actually exercise."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker, scoped_session
    from cps import ub

    db_path = tmp_path / "race.db"
    # check_same_thread=False is needed because we share the engine
    # across threads. Each thread gets its own connection from the pool.
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        conn.exec_driver_sql("PRAGMA journal_mode = WAL")
        conn.commit()

    ub.Base.metadata.create_all(engine)

    Session = scoped_session(sessionmaker(bind=engine, future=True))
    yield Session, engine
    Session.remove()


@pytest.mark.unit
class TestConcurrentReadingStateInsert:
    def test_n_threads_produce_exactly_one_row(self, shared_file_db):
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from cps.ub import KoboReadingState

        Session, _ = shared_file_db
        user_id, book_id = 1, 42
        n_threads = 16

        barrier = threading.Barrier(n_threads)

        def worker():
            session = Session()
            try:
                barrier.wait()  # release all threads simultaneously
                stmt = (
                    sqlite_insert(KoboReadingState)
                    .values(user_id=user_id, book_id=book_id)
                    .on_conflict_do_nothing(
                        index_elements=["user_id", "book_id"],
                    )
                )
                session.execute(stmt)
                session.commit()
            finally:
                Session.remove()

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            futures = [pool.submit(worker) for _ in range(n_threads)]
            for f in futures:
                f.result()  # raises if any worker raised

        # Critical: exactly one row, no duplicates, no exception swallowed.
        check_session = Session()
        rows = check_session.query(KoboReadingState).filter_by(
            user_id=user_id, book_id=book_id,
        ).all()
        assert len(rows) == 1, (
            f"Race produced {len(rows)} rows; on_conflict_do_nothing "
            f"with the (user_id, book_id) UNIQUE index must collapse "
            f"to exactly one."
        )

    def test_n_threads_produce_exactly_one_read_book_row(self, shared_file_db):
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from cps.ub import ReadBook

        Session, _ = shared_file_db
        user_id, book_id = 7, 99
        n_threads = 16
        barrier = threading.Barrier(n_threads)

        def worker():
            session = Session()
            try:
                barrier.wait()
                stmt = (
                    sqlite_insert(ReadBook)
                    .values(user_id=user_id, book_id=book_id,
                            read_status=ReadBook.STATUS_UNREAD,
                            times_started_reading=0)
                    .on_conflict_do_nothing(
                        index_elements=["user_id", "book_id"],
                    )
                )
                session.execute(stmt)
                session.commit()
            finally:
                Session.remove()

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            for f in [pool.submit(worker) for _ in range(n_threads)]:
                f.result()

        check_session = Session()
        rows = check_session.query(ReadBook).filter_by(
            user_id=user_id, book_id=book_id,
        ).all()
        assert len(rows) == 1

    def test_different_books_dont_collide(self, shared_file_db):
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from cps.ub import KoboReadingState

        Session, _ = shared_file_db
        # Each thread tries a different (user, book) pair.
        n_threads = 8
        barrier = threading.Barrier(n_threads)

        def worker(thread_idx):
            session = Session()
            try:
                barrier.wait()
                stmt = (
                    sqlite_insert(KoboReadingState)
                    .values(user_id=1, book_id=thread_idx)
                    .on_conflict_do_nothing(
                        index_elements=["user_id", "book_id"],
                    )
                )
                session.execute(stmt)
                session.commit()
            finally:
                Session.remove()

        with ThreadPoolExecutor(max_workers=n_threads) as pool:
            for f in [pool.submit(worker, i) for i in range(n_threads)]:
                f.result()

        check_session = Session()
        rows = check_session.query(KoboReadingState).filter_by(user_id=1).all()
        assert len(rows) == n_threads, (
            "Different (user, book) pairs must each create their own "
            "row — the on-conflict path should only block exact key "
            "collisions, not unrelated inserts."
        )
