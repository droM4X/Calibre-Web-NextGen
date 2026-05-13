# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Pin the precedence order for cover-preview resolution.

The resolver is called on every cover render. Drift here would
silently change what every user sees — load-bearing.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def session():
    from cps import ub
    engine = create_engine("sqlite:///:memory:", future=True)
    # FKs off so we don't need to wrangle the full User/Book graph.
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
    user.role = 0
    user.show_ereader_previews = True
    user.preview_preset = "kobo_libra_color"
    user.preview_default_fill = "edge_mirror"
    user.preview_default_color = None
    session.add(user)
    session.commit()
    return user


@pytest.mark.unit
class TestResolveEffectiveSettings:

    def test_user_defaults_when_no_override(self, session, alice):
        from cps.services.cover_preview_resolution import resolve_effective_settings
        preset, fill, color = resolve_effective_settings(alice.id, 42)
        assert preset == "kobo_libra_color"
        assert fill == "edge_mirror"
        assert color is None

    def test_override_row_supersedes_user_default(self, session, alice):
        from cps import ub
        from cps.services.cover_preview_resolution import resolve_effective_settings
        session.add(ub.BookCoverPreview(
            user_id=alice.id, book_id=42, fill_mode="manual",
            custom_color="#000000", locked=False,
        ))
        session.commit()
        preset, fill, color = resolve_effective_settings(alice.id, 42)
        # preset is per-user; no per-book aspect override
        assert preset == "kobo_libra_color"
        assert fill == "manual"
        assert color == "#000000"

    def test_query_param_supersedes_override_row(self, session, alice):
        from cps import ub
        from cps.services.cover_preview_resolution import resolve_effective_settings
        session.add(ub.BookCoverPreview(
            user_id=alice.id, book_id=42, fill_mode="manual",
            custom_color="#000000", locked=False,
        ))
        session.commit()
        preset, fill, color = resolve_effective_settings(
            alice.id, 42, p_override="kindle_paperwhite",
            f_override="gradient", c_override="#ffffff",
        )
        assert preset == "kindle_paperwhite"
        assert fill == "gradient"
        assert color == "#ffffff"

    def test_partial_override_blends_with_override_row(self, session, alice):
        from cps import ub
        from cps.services.cover_preview_resolution import resolve_effective_settings
        session.add(ub.BookCoverPreview(
            user_id=alice.id, book_id=42, fill_mode="manual",
            custom_color="#000000", locked=False,
        ))
        session.commit()
        preset, fill, color = resolve_effective_settings(
            alice.id, 42, f_override="gradient",
        )
        assert fill == "gradient"
        # color comes from the override row, not user default
        assert color == "#000000"

    def test_unknown_user_returns_engine_defaults(self, session):
        from cps.services.cover_preview_resolution import resolve_effective_settings
        from cps.services import cover_preview as engine
        preset, fill, color = resolve_effective_settings(99999, 42)
        assert preset == engine.DEFAULT_PRESET
        assert fill == engine.DEFAULT_FILL_MODE
        assert color is None

    def test_null_color_in_override_row_does_not_fall_through(self, session, alice):
        """Override row's NULL color is itself the answer — does NOT
        fall back to user's default_color. Once a per-book row exists,
        that row IS the source of truth for color."""
        from cps import ub
        from cps.services.cover_preview_resolution import resolve_effective_settings
        alice.preview_default_color = "#bbbbbb"
        session.commit()
        session.add(ub.BookCoverPreview(
            user_id=alice.id, book_id=42, fill_mode="gradient",
            custom_color=None, locked=False,
        ))
        session.commit()
        preset, fill, color = resolve_effective_settings(alice.id, 42)
        assert fill == "gradient"
        assert color is None  # NOT "#bbbbbb"

    def test_user_with_null_preview_preset_falls_back_to_engine_default(self, session, alice):
        """A user row with a null preview_preset (e.g. migration default
        never ran or was cleared) falls back to engine.DEFAULT_PRESET."""
        from cps.services.cover_preview_resolution import resolve_effective_settings
        from cps.services import cover_preview as engine
        alice.preview_preset = None
        session.commit()
        preset, _, _ = resolve_effective_settings(alice.id, 42)
        assert preset == engine.DEFAULT_PRESET


@pytest.mark.unit
class TestIsBookLockedForUser:

    def test_locked_row_returns_true(self, session, alice):
        from cps import ub
        from cps.services.cover_preview_resolution import is_book_locked_for_user
        session.add(ub.BookCoverPreview(
            user_id=alice.id, book_id=42, fill_mode="mirror",
            custom_color=None, locked=True,
        ))
        session.commit()
        assert is_book_locked_for_user(alice.id, 42) is True

    def test_unlocked_row_returns_false(self, session, alice):
        from cps import ub
        from cps.services.cover_preview_resolution import is_book_locked_for_user
        session.add(ub.BookCoverPreview(
            user_id=alice.id, book_id=42, fill_mode="mirror",
            custom_color=None, locked=False,
        ))
        session.commit()
        assert is_book_locked_for_user(alice.id, 42) is False

    def test_no_row_returns_false(self, session, alice):
        from cps.services.cover_preview_resolution import is_book_locked_for_user
        assert is_book_locked_for_user(alice.id, 99) is False
