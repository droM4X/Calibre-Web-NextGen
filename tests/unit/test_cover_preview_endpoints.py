# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for the 5 cover-preview write endpoints.

We exercise the view functions directly via ``app.test_request_context()``
+ patched ``current_user``, NOT via the test_client + login machinery —
the latter requires standing up Flask-Login + CSRF + the full app boot,
which is overkill for unit-level coverage of validator + SQL behavior.
End-to-end auth + CSRF coverage lives in the live container smoke (Task 8).

Each view is decorated by ``@user_login_required`` and (for four of five)
``@edit_required``. We peel those off with ``inspect.unwrap`` so the
authn/authz machinery is out of the test path. The decorators are unit-
tested implicitly in cover_picker tests and explicitly in the live smoke.
"""

import datetime
import inspect
import json

import flask
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch
from werkzeug.exceptions import HTTPException


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def session():
    from cps import ub
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    ub.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    original = ub.session
    ub.session = s
    try:
        yield s
    finally:
        ub.session = original
        s.close()


@pytest.fixture
def alice(session):
    from cps import ub
    user = ub.User()
    user.id = 1
    user.name = "alice"
    user.nickname = "alice"
    user.email = "alice@example.com"
    user.password = "x"
    # ROLE_EDIT bit set so role_edit() returns True (edit_required passes).
    user.role = 1 << 3
    user.show_ereader_previews = True
    user.preview_preset = "kobo_libra_color"
    user.preview_default_fill = "edge_mirror"
    user.preview_default_color = None
    session.add(user)
    session.commit()
    return user


@pytest.fixture
def app():
    from cps.cover_preview_blueprint import cover_preview_bp
    a = flask.Flask(__name__)
    a.testing = True
    a.config["WTF_CSRF_ENABLED"] = False
    a.register_blueprint(cover_preview_bp)
    return a


def _bare(view_fn):
    """Strip the decorator chain so we can call the raw view function
    directly without Flask-Login or edit_required intercepting."""
    return inspect.unwrap(view_fn)


def _call(app, view_fn, alice, payload=None, **route_kwargs):
    """Invoke a view function inside a request context with current_user
    patched to alice. Returns the Flask Response."""
    body = json.dumps(payload) if payload is not None else None
    with app.test_request_context(
        method="POST",
        data=body,
        content_type="application/json",
    ):
        with patch("cps.cover_preview_blueprint.current_user", alice):
            return _bare(view_fn)(**route_kwargs)


def _json(resp):
    """Decode a Flask Response (or jsonify'd output) to dict."""
    return json.loads(resp.get_data(as_text=True))


# ============================================================
# POST /me/cover/preview-defaults
# ============================================================

@pytest.mark.unit
class TestPreviewDefaults:

    def test_partial_update_only_touches_provided_fields(self, session, alice, app):
        from cps.cover_preview_blueprint import set_my_preview_defaults
        resp = _call(app, set_my_preview_defaults, alice, {
            "preview_preset": "kindle_paperwhite",
        })
        data = _json(resp)
        assert data["ok"] is True
        assert data["preview_preset"] == "kindle_paperwhite"
        # Untouched fields preserved.
        assert data["preview_default_fill"] == "edge_mirror"
        assert data["preview_default_color"] is None
        assert data["show_ereader_previews"] is True

    def test_full_update_persists_all_four_fields(self, session, alice, app):
        from cps.cover_preview_blueprint import set_my_preview_defaults
        resp = _call(app, set_my_preview_defaults, alice, {
            "show_ereader_previews": False,
            "preview_preset": "kindle_oasis",
            "default_fill": "gradient",
            "default_color": "#abcdef",
        })
        data = _json(resp)
        assert data == {
            "ok": True,
            "show_ereader_previews": False,
            "preview_preset": "kindle_oasis",
            "preview_default_fill": "gradient",
            "preview_default_color": "#abcdef",
        }
        # And persisted on the user row.
        assert alice.preview_preset == "kindle_oasis"
        assert alice.preview_default_fill == "gradient"
        assert alice.preview_default_color == "#abcdef"
        assert alice.show_ereader_previews is False

    def test_empty_string_color_normalizes_to_none(self, session, alice, app):
        from cps.cover_preview_blueprint import set_my_preview_defaults
        alice.preview_default_color = "#ffffff"
        session.commit()
        resp = _call(app, set_my_preview_defaults, alice, {"default_color": ""})
        data = _json(resp)
        assert data["preview_default_color"] is None

    def test_bad_fill_mode_returns_400(self, session, alice, app):
        from cps.cover_preview_blueprint import set_my_preview_defaults
        with pytest.raises(HTTPException) as exc_info:
            _call(app, set_my_preview_defaults, alice, {"default_fill": "bogus"})
        assert exc_info.value.code == 400

    def test_bad_color_returns_400(self, session, alice, app):
        from cps.cover_preview_blueprint import set_my_preview_defaults
        # Missing '#'.
        with pytest.raises(HTTPException) as exc_info:
            _call(app, set_my_preview_defaults, alice, {"default_color": "ffffff"})
        assert exc_info.value.code == 400

    def test_bad_color_non_hex_chars_returns_400(self, session, alice, app):
        from cps.cover_preview_blueprint import set_my_preview_defaults
        with pytest.raises(HTTPException) as exc_info:
            _call(app, set_my_preview_defaults, alice, {"default_color": "#zzzzzz"})
        assert exc_info.value.code == 400

    def test_bad_preset_returns_400(self, session, alice, app):
        from cps.cover_preview_blueprint import set_my_preview_defaults
        with pytest.raises(HTTPException) as exc_info:
            _call(app, set_my_preview_defaults, alice, {"preview_preset": "made_up_device"})
        assert exc_info.value.code == 400


# ============================================================
# POST /book/<id>/cover/preview-settings
# ============================================================

@pytest.mark.unit
class TestPreviewSettings:

    def test_creates_new_row_for_book(self, session, alice, app):
        from cps import ub
        from cps.cover_preview_blueprint import set_book_preview_settings
        resp = _call(app, set_book_preview_settings, alice, {
            "fill_mode": "gradient",
            "custom_color": "#112233",
            "locked": True,
        }, book_id=42)
        data = _json(resp)
        assert data["ok"] is True
        assert data["book_id"] == 42
        assert data["fill_mode"] == "gradient"
        assert data["custom_color"] == "#112233"
        assert data["locked"] is True

        rows = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id, book_id=42,
        ).all()
        assert len(rows) == 1
        assert rows[0].fill_mode == "gradient"
        assert rows[0].custom_color == "#112233"
        assert bool(rows[0].locked) is True

    def test_updates_existing_row_in_place(self, session, alice, app):
        from cps import ub
        from cps.cover_preview_blueprint import set_book_preview_settings
        session.add(ub.BookCoverPreview(
            user_id=alice.id, book_id=42, fill_mode="edge_mirror",
            custom_color=None, locked=False,
            updated_at=datetime.datetime(2020, 1, 1),
        ))
        session.commit()

        resp = _call(app, set_book_preview_settings, alice, {
            "fill_mode": "average",
            "custom_color": None,
            "locked": False,
        }, book_id=42)
        assert _json(resp)["fill_mode"] == "average"

        rows = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id, book_id=42,
        ).all()
        assert len(rows) == 1  # no duplicate row
        assert rows[0].fill_mode == "average"
        assert rows[0].updated_at > datetime.datetime(2020, 1, 2)

    def test_bad_fill_mode_returns_400(self, session, alice, app):
        from cps.cover_preview_blueprint import set_book_preview_settings
        with pytest.raises(HTTPException) as exc_info:
            _call(app, set_book_preview_settings, alice, {
                "fill_mode": "totally_made_up",
                "custom_color": None,
                "locked": False,
            }, book_id=7)
        assert exc_info.value.code == 400


# ============================================================
# POST /book/<id>/cover/preview-lock
# ============================================================

@pytest.mark.unit
class TestPreviewLock:

    def test_creates_row_from_user_defaults_when_none_exists(self, session, alice, app):
        from cps import ub
        from cps.cover_preview_blueprint import toggle_book_preview_lock

        alice.preview_default_fill = "average"
        alice.preview_default_color = "#cccccc"
        session.commit()

        resp = _call(app, toggle_book_preview_lock, alice, {"locked": True}, book_id=99)
        data = _json(resp)
        assert data["ok"] is True
        assert data["book_id"] == 99
        assert data["locked"] is True

        rows = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id, book_id=99,
        ).all()
        assert len(rows) == 1
        # Row materialized from the user's effective settings.
        assert rows[0].fill_mode == "average"
        assert rows[0].custom_color == "#cccccc"
        assert bool(rows[0].locked) is True

    def test_toggles_existing_row_without_creating_a_duplicate(self, session, alice, app):
        from cps import ub
        from cps.cover_preview_blueprint import toggle_book_preview_lock
        session.add(ub.BookCoverPreview(
            user_id=alice.id, book_id=42, fill_mode="manual",
            custom_color="#000000", locked=False,
            updated_at=datetime.datetime(2020, 1, 1),
        ))
        session.commit()

        resp = _call(app, toggle_book_preview_lock, alice, {"locked": True}, book_id=42)
        assert _json(resp)["locked"] is True

        rows = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id, book_id=42,
        ).all()
        assert len(rows) == 1
        assert bool(rows[0].locked) is True
        # fill/color preserved.
        assert rows[0].fill_mode == "manual"
        assert rows[0].custom_color == "#000000"

        # Now unlock.
        resp = _call(app, toggle_book_preview_lock, alice, {"locked": False}, book_id=42)
        assert _json(resp)["locked"] is False
        rows = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id, book_id=42,
        ).all()
        assert bool(rows[0].locked) is False


# ============================================================
# POST /me/cover/preview-apply-to-all
# ============================================================

@pytest.mark.unit
class TestApplyToAll:

    def _seed_rows(self, session, alice):
        """Seed a mix of locked + unlocked override rows for alice."""
        from cps import ub
        rows = [
            ub.BookCoverPreview(user_id=alice.id, book_id=1,
                                fill_mode="manual", custom_color="#000000",
                                locked=False),
            ub.BookCoverPreview(user_id=alice.id, book_id=2,
                                fill_mode="manual", custom_color="#111111",
                                locked=True),
            ub.BookCoverPreview(user_id=alice.id, book_id=3,
                                fill_mode="dominant", custom_color=None,
                                locked=False),
            ub.BookCoverPreview(user_id=alice.id, book_id=4,
                                fill_mode="manual", custom_color="#222222",
                                locked=True),
        ]
        for r in rows:
            session.add(r)
        session.commit()

    def test_wipes_unlocked_rows_and_updates_defaults(self, session, alice, app):
        from cps import ub
        from cps.cover_preview_blueprint import apply_preview_to_all
        self._seed_rows(session, alice)

        resp = _call(app, apply_preview_to_all, alice, {
            "fill_mode": "gradient",
            "custom_color": "#999999",
            "wipe_unlocked": True,
        })
        data = _json(resp)
        assert data["ok"] is True
        # 2 unlocked rows existed; both got updated.
        assert data["updated_books"] == 2
        assert data["skipped_locked"] == 2

        # Locked rows untouched.
        locked = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id, locked=True,
        ).all()
        assert len(locked) == 2
        assert {r.book_id for r in locked} == {2, 4}
        assert {r.custom_color for r in locked} == {"#111111", "#222222"}

        # Unlocked rows: now all match the new default → compacted away.
        unlocked = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id, locked=False,
        ).all()
        assert unlocked == []

        # Defaults updated on the user row.
        assert alice.preview_default_fill == "gradient"
        assert alice.preview_default_color == "#999999"

    def test_preserves_locked_rows(self, session, alice, app):
        from cps import ub
        from cps.cover_preview_blueprint import apply_preview_to_all
        self._seed_rows(session, alice)

        _call(app, apply_preview_to_all, alice, {
            "fill_mode": "gradient",
            "custom_color": "#999999",
            "wipe_unlocked": True,
        })

        locked_after = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id, locked=True,
        ).order_by(ub.BookCoverPreview.book_id).all()
        assert [r.fill_mode for r in locked_after] == ["manual", "manual"]
        assert [r.custom_color for r in locked_after] == ["#111111", "#222222"]

    def test_compacts_unlocked_rows_with_null_color_default(self, session, alice, app):
        """When the new default color is NULL, the compaction step must
        match unlocked rows where custom_color IS NULL (not via `=` which
        is NULL-unsafe in SQLite)."""
        from cps import ub
        from cps.cover_preview_blueprint import apply_preview_to_all
        self._seed_rows(session, alice)

        # Pre-state: book 3 is unlocked with fill=dominant, color=None.
        resp = _call(app, apply_preview_to_all, alice, {
            "fill_mode": "dominant",
            "custom_color": None,
            "wipe_unlocked": True,
        })
        data = _json(resp)
        assert data["updated_books"] == 2  # both unlocked rows touched

        # After compaction, book 3 (which matches the new default) is gone.
        # Book 1 also got coerced to (dominant, None) and is also gone.
        unlocked_after = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id, locked=False,
        ).all()
        assert unlocked_after == []

    def test_wipe_unlocked_false_only_updates_defaults(self, session, alice, app):
        from cps import ub
        from cps.cover_preview_blueprint import apply_preview_to_all
        self._seed_rows(session, alice)

        resp = _call(app, apply_preview_to_all, alice, {
            "fill_mode": "gradient",
            "custom_color": "#999999",
            "wipe_unlocked": False,
        })
        data = _json(resp)
        assert data["updated_books"] == 0
        assert data["skipped_locked"] == 2

        # All four rows still present, unmodified.
        all_rows = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id,
        ).all()
        assert len(all_rows) == 4

        # Defaults still updated.
        assert alice.preview_default_fill == "gradient"
        assert alice.preview_default_color == "#999999"

    def test_apply_to_all_bad_fill_mode_returns_400(self, session, alice, app):
        from cps.cover_preview_blueprint import apply_preview_to_all
        with pytest.raises(HTTPException) as exc_info:
            _call(app, apply_preview_to_all, alice, {
                "fill_mode": "nope",
                "custom_color": None,
            })
        assert exc_info.value.code == 400


# ============================================================
# POST /books/cover/preview-bulk-apply
# ============================================================

@pytest.mark.unit
class TestBulkApply:

    def test_creates_rows_for_book_list(self, session, alice, app):
        from cps import ub
        from cps.cover_preview_blueprint import bulk_apply_preview

        resp = _call(app, bulk_apply_preview, alice, {
            "book_ids": [10, 11, 12],
            "fill_mode": "gradient",
            "custom_color": "#444444",
            "lock": False,
        })
        data = _json(resp)
        assert data == {"ok": True, "affected": 3}

        rows = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id,
        ).order_by(ub.BookCoverPreview.book_id).all()
        assert [r.book_id for r in rows] == [10, 11, 12]
        assert all(r.fill_mode == "gradient" for r in rows)
        assert all(r.custom_color == "#444444" for r in rows)
        assert all(bool(r.locked) is False for r in rows)

    def test_updates_existing_rows_without_duplicating(self, session, alice, app):
        from cps import ub
        from cps.cover_preview_blueprint import bulk_apply_preview
        session.add(ub.BookCoverPreview(
            user_id=alice.id, book_id=10, fill_mode="manual",
            custom_color="#000000", locked=False,
        ))
        session.commit()

        resp = _call(app, bulk_apply_preview, alice, {
            "book_ids": [10, 11],
            "fill_mode": "average",
            "custom_color": None,
            "lock": False,
        })
        assert _json(resp)["affected"] == 2

        rows = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id,
        ).order_by(ub.BookCoverPreview.book_id).all()
        assert len(rows) == 2
        assert rows[0].fill_mode == "average"
        assert rows[0].custom_color is None

    def test_lock_flag_pins_rows(self, session, alice, app):
        from cps import ub
        from cps.cover_preview_blueprint import bulk_apply_preview

        resp = _call(app, bulk_apply_preview, alice, {
            "book_ids": [50, 51],
            "fill_mode": "gradient",
            "custom_color": "#abcabc",
            "lock": True,
        })
        assert _json(resp)["affected"] == 2

        rows = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id,
        ).all()
        assert all(bool(r.locked) is True for r in rows)

    def test_lock_false_does_not_unlock_existing_locked_row(self, session, alice, app):
        """Documented behavior: lock=False is a no-op on the lock column
        for existing rows (the endpoint only flips lock to True when
        lock=True; it never forces False). This pins that semantic."""
        from cps import ub
        from cps.cover_preview_blueprint import bulk_apply_preview
        session.add(ub.BookCoverPreview(
            user_id=alice.id, book_id=10, fill_mode="manual",
            custom_color="#000000", locked=True,
        ))
        session.commit()

        _call(app, bulk_apply_preview, alice, {
            "book_ids": [10],
            "fill_mode": "average",
            "custom_color": None,
            "lock": False,
        })
        row = session.query(ub.BookCoverPreview).filter_by(
            user_id=alice.id, book_id=10,
        ).first()
        assert bool(row.locked) is True  # still locked

    def test_rejects_empty_list_400(self, session, alice, app):
        from cps.cover_preview_blueprint import bulk_apply_preview
        with pytest.raises(HTTPException) as exc_info:
            _call(app, bulk_apply_preview, alice, {
                "book_ids": [],
                "fill_mode": "gradient",
                "custom_color": None,
            })
        assert exc_info.value.code == 400

    def test_rejects_missing_book_ids_400(self, session, alice, app):
        from cps.cover_preview_blueprint import bulk_apply_preview
        with pytest.raises(HTTPException) as exc_info:
            _call(app, bulk_apply_preview, alice, {
                "fill_mode": "gradient",
                "custom_color": None,
            })
        assert exc_info.value.code == 400

    def test_rejects_over_5000_400(self, session, alice, app):
        from cps.cover_preview_blueprint import bulk_apply_preview
        with pytest.raises(HTTPException) as exc_info:
            _call(app, bulk_apply_preview, alice, {
                "book_ids": list(range(5001)),
                "fill_mode": "gradient",
                "custom_color": None,
            })
        assert exc_info.value.code == 400

    def test_rejects_non_int_items_400(self, session, alice, app):
        from cps.cover_preview_blueprint import bulk_apply_preview
        with pytest.raises(HTTPException) as exc_info:
            _call(app, bulk_apply_preview, alice, {
                "book_ids": [1, 2, "three"],
                "fill_mode": "gradient",
                "custom_color": None,
            })
        assert exc_info.value.code == 400

    def test_rejects_bool_items_400(self, session, alice, app):
        """bool is a subclass of int in Python; explicitly disallow."""
        from cps.cover_preview_blueprint import bulk_apply_preview
        with pytest.raises(HTTPException) as exc_info:
            _call(app, bulk_apply_preview, alice, {
                "book_ids": [1, True],
                "fill_mode": "gradient",
                "custom_color": None,
            })
        assert exc_info.value.code == 400
