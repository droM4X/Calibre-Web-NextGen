# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Pin orphan-cleanup behavior for book_cover_preview rows."""

import datetime
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch, MagicMock


@pytest.fixture
def ub_session():
    from cps import ub
    engine = create_engine("sqlite:///:memory:")
    ub.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    original = ub.session
    ub.session = s
    try:
        yield s
    finally:
        ub.session = original
        s.close()


def _make_user(session, uid):
    from cps import ub
    u = ub.User()
    u.id = uid
    u.name = f"u{uid}"
    u.nickname = f"u{uid}"
    u.email = f"u{uid}@x"
    u.password = "x"
    u.role = 0
    session.add(u)
    session.commit()
    return u


def _make_override(session, uid, bid, fill="edge_mirror"):
    from cps import ub
    row = ub.BookCoverPreview(
        user_id=uid, book_id=bid, fill_mode=fill,
        custom_color=None, locked=False,
        updated_at=datetime.datetime.utcnow(),
    )
    session.add(row)
    session.commit()
    return row


def _stub_db_books(book_ids):
    """Return a patcher object that makes
    calibre_db.session.query(db.Books.id).all() return rows for the
    given iterable of ids."""
    import cps
    mock_all = MagicMock(return_value=[(bid,) for bid in book_ids])
    mock_query = MagicMock()
    mock_query.all = mock_all
    mock_session_query = MagicMock(return_value=mock_query)
    return patch.object(
        cps.calibre_db, "session", MagicMock(query=mock_session_query)
    )


class TestSweepOrphanedCoverPreviews:

    def test_no_orphans_no_op(self, ub_session):
        from cps import ub
        from cps.services.cover_preview_cleanup import sweep_orphaned_cover_previews
        _make_user(ub_session, 1)
        _make_override(ub_session, 1, 100)
        _make_override(ub_session, 1, 200)
        with _stub_db_books([100, 200, 300]):
            deleted = sweep_orphaned_cover_previews()
        assert deleted == 0
        assert ub_session.query(ub.BookCoverPreview).count() == 2

    def test_removes_orphans(self, ub_session):
        from cps import ub
        from cps.services.cover_preview_cleanup import sweep_orphaned_cover_previews
        _make_user(ub_session, 1)
        _make_override(ub_session, 1, 100)
        _make_override(ub_session, 1, 999)  # orphan — not in calibre
        with _stub_db_books([100, 200]):
            deleted = sweep_orphaned_cover_previews()
        assert deleted == 1
        rows = ub_session.query(ub.BookCoverPreview).all()
        assert len(rows) == 1
        assert rows[0].book_id == 100

    def test_empty_calibre_db_is_no_op(self, ub_session):
        """If metadata.db is empty (fresh install), don't wipe everything."""
        from cps import ub
        from cps.services.cover_preview_cleanup import sweep_orphaned_cover_previews
        _make_user(ub_session, 1)
        _make_override(ub_session, 1, 100)
        with _stub_db_books([]):
            deleted = sweep_orphaned_cover_previews()
        assert deleted == 0  # no books = nothing-to-do, NOT wipe-everything
        assert ub_session.query(ub.BookCoverPreview).count() == 1

    def test_multi_user_orphan(self, ub_session):
        """An orphan book_id deletes every user's row for that book."""
        from cps import ub
        from cps.services.cover_preview_cleanup import sweep_orphaned_cover_previews
        _make_user(ub_session, 1)
        _make_user(ub_session, 2)
        _make_override(ub_session, 1, 100)
        _make_override(ub_session, 2, 100)  # both users have a row for book 100
        _make_override(ub_session, 1, 999)  # orphan
        _make_override(ub_session, 2, 999)  # orphan for the other user too
        with _stub_db_books([100]):
            deleted = sweep_orphaned_cover_previews()
        assert deleted == 2  # both 999 rows
        rows = ub_session.query(ub.BookCoverPreview).all()
        assert len(rows) == 2
        assert all(r.book_id == 100 for r in rows)

    def test_db_unavailable_returns_zero(self, ub_session):
        """If calibre_db.session.query raises (e.g. no Calibre DB),
        skip cleanly."""
        import cps
        from cps import ub
        from cps.services.cover_preview_cleanup import sweep_orphaned_cover_previews
        _make_user(ub_session, 1)
        _make_override(ub_session, 1, 100)

        broken_query = MagicMock(side_effect=Exception("db not available"))
        with patch.object(
            cps.calibre_db, "session", MagicMock(query=broken_query)
        ):
            deleted = sweep_orphaned_cover_previews()
        assert deleted == 0
        # Existing rows untouched
        assert ub_session.query(ub.BookCoverPreview).count() == 1
