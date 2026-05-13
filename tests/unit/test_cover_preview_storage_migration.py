# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Pin the cover-preview Phase 2 migrations.

Behavior under test:
- ``migrate_user_table`` adds the 4 Phase-2 columns idempotently. Existing
  rows on upgrade get ``show_ereader_previews=0`` so the rollout is silent
  (users opt in via settings later); new rows after migration default to
  True via the column-level default.
- ``migrate_book_cover_preview_table`` creates the table idempotently.
- Both migrations no-op when re-run.

We build a deliberately-minimal pre-Phase-2 ``user`` table — just enough
scaffolding for the migration's ``exists().where(User.<col>)`` probes to
either succeed or raise ``OperationalError`` cleanly (which is the
trigger the migration uses to ``ALTER TABLE``). The pre-migration schema
intentionally omits every column the migration adds.
"""

import datetime as _dt
import os
import tempfile

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker


# --- helpers ----------------------------------------------------------

# Pre-Phase-2 user table. We include the legacy columns the migration
# probes BEFORE the Phase-2 block so those earlier probes don't trip on
# their own ALTERs and mask what we're actually testing here. The four
# Phase-2 columns are intentionally absent.
_PRE_MIGRATION_USER_DDL = """
    CREATE TABLE user (
        id INTEGER PRIMARY KEY,
        name VARCHAR,
        nickname VARCHAR,
        email VARCHAR,
        password VARCHAR,
        kindle_mail VARCHAR DEFAULT '',
        locale VARCHAR(2) DEFAULT 'en',
        sidebar_view INTEGER DEFAULT 1,
        default_language VARCHAR(3) DEFAULT 'all',
        denied_tags VARCHAR DEFAULT '',
        allowed_tags VARCHAR DEFAULT '',
        denied_column_value VARCHAR DEFAULT '',
        allowed_column_value VARCHAR DEFAULT '',
        view_settings JSON DEFAULT '{}',
        kobo_only_shelves_sync INTEGER DEFAULT 0,
        role INTEGER DEFAULT 0,
        theme INTEGER DEFAULT 1,
        hardcover_token VARCHAR,
        auto_send_enabled BOOLEAN DEFAULT 0,
        allow_additional_ereader_emails BOOLEAN DEFAULT 1,
        kindle_mail_subject VARCHAR DEFAULT ''
    )
"""


def _build_pre_migration_db():
    """Return (engine, tmpfile_path) for a DB shaped like pre-Phase-2.

    Uses a file-backed SQLite (not :memory:) so the engine's connections
    see consistent schema after DDL — file mode is more forgiving across
    SQLAlchemy/connection-pool variations.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    engine = create_engine(f"sqlite:///{tmp}", future=True)
    with engine.begin() as conn:
        conn.execute(text(_PRE_MIGRATION_USER_DDL))
        conn.execute(text(
            "INSERT INTO user (id, name, nickname, email, password, role) "
            "VALUES (1, 'alice', 'alice', 'a@x', 'pw', 0)"
        ))
        conn.execute(text(
            "INSERT INTO user (id, name, nickname, email, password, role) "
            "VALUES (2, 'bob', 'bob', 'b@x', 'pw', 0)"
        ))
    return engine, tmp


@pytest.fixture
def pre_migration_engine():
    engine, tmp = _build_pre_migration_db()
    try:
        yield engine
    finally:
        try:
            engine.dispose()
        except Exception:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass


# --- tests ------------------------------------------------------------


class TestMigrateUserTable:
    """``migrate_user_table`` — Phase-2 column additions."""

    def test_adds_4_new_columns(self, pre_migration_engine):
        from cps import ub
        Session = sessionmaker(bind=pre_migration_engine)
        s = Session()
        try:
            ub.migrate_user_table(pre_migration_engine, s)
        finally:
            s.close()
        cols = [c["name"] for c in inspect(pre_migration_engine).get_columns("user")]
        for name in (
            "show_ereader_previews",
            "preview_preset",
            "preview_default_fill",
            "preview_default_color",
        ):
            assert name in cols, f"column {name!r} missing after migration"

    def test_existing_users_get_show_ereader_previews_false(self, pre_migration_engine):
        """Silent upgrade — pre-existing users keep stock layout (no eReader chrome)."""
        from cps import ub
        Session = sessionmaker(bind=pre_migration_engine)
        s = Session()
        try:
            ub.migrate_user_table(pre_migration_engine, s)
        finally:
            s.close()
        with pre_migration_engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, show_ereader_previews FROM user ORDER BY id"
            )).fetchall()
        assert len(rows) == 2
        for row in rows:
            assert row[1] in (0, False), (
                f"expected show_ereader_previews=0/False for upgraded user id={row[0]}, "
                f"got {row[1]!r}"
            )

    def test_new_users_after_migration_default_true(self, pre_migration_engine):
        """New rows opt-in to eReader previews via the column-level default."""
        from cps import ub
        Session = sessionmaker(bind=pre_migration_engine)
        s = Session()
        try:
            ub.migrate_user_table(pre_migration_engine, s)
            new = ub.User()
            new.id = 3
            new.name = "charlie"
            new.nickname = "charlie"
            new.email = "c@x"
            new.password = "x"
            new.role = 0
            # Intentionally NOT setting show_ereader_previews — must fall
            # back to the model's Column(default=True).
            s.add(new)
            s.commit()
        finally:
            s.close()
        with pre_migration_engine.connect() as conn:
            row = conn.execute(text(
                "SELECT show_ereader_previews FROM user WHERE id=3"
            )).fetchone()
        assert row is not None, "new user not persisted"
        assert row[0] in (1, True), (
            f"new user should default show_ereader_previews=True, got {row[0]!r}"
        )

    def test_idempotent_double_run(self, pre_migration_engine):
        """Running ``migrate_user_table`` twice must not raise or duplicate columns."""
        from cps import ub
        Session = sessionmaker(bind=pre_migration_engine)
        s = Session()
        try:
            ub.migrate_user_table(pre_migration_engine, s)
            # Second pass — exists()-probe should now succeed for every
            # Phase-2 column, so no ALTERs are issued. Must not raise.
            ub.migrate_user_table(pre_migration_engine, s)
        finally:
            s.close()
        cols = [c["name"] for c in inspect(pre_migration_engine).get_columns("user")]
        for name in (
            "show_ereader_previews",
            "preview_preset",
            "preview_default_fill",
            "preview_default_color",
        ):
            assert cols.count(name) == 1, (
                f"column {name!r} duplicated after second migration pass "
                f"(count={cols.count(name)})"
            )


class TestMigrateBookCoverPreviewTable:
    """``migrate_book_cover_preview_table`` — table creation."""

    def test_creates_table(self, pre_migration_engine):
        from cps import ub
        Session = sessionmaker(bind=pre_migration_engine)
        s = Session()
        try:
            ub.migrate_book_cover_preview_table(pre_migration_engine, s)
        finally:
            s.close()
        assert inspect(pre_migration_engine).has_table("book_cover_preview"), (
            "book_cover_preview table was not created by migration"
        )

    def test_idempotent_double_run(self, pre_migration_engine):
        """Re-running must preserve existing rows (no drop+recreate)."""
        from cps import ub
        Session = sessionmaker(bind=pre_migration_engine)
        s = Session()
        try:
            ub.migrate_book_cover_preview_table(pre_migration_engine, s)
        finally:
            s.close()
        # Seed a row that the second migration call MUST NOT clobber.
        with pre_migration_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO book_cover_preview "
                    "(user_id, book_id, fill_mode, locked, updated_at) "
                    "VALUES (1, 42, 'edge_mirror', 0, :ts)"
                ),
                {"ts": _dt.datetime.utcnow()},
            )
        s2 = Session()
        try:
            ub.migrate_book_cover_preview_table(pre_migration_engine, s2)
        finally:
            s2.close()
        with pre_migration_engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM book_cover_preview")
            ).scalar()
        assert count == 1, (
            f"second migration run dropped existing rows (count={count}, expected 1)"
        )
