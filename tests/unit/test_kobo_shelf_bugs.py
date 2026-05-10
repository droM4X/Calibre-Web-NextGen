# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression tests for two shelf-sync bugs in HandleSyncRequest +
sync_shelves found in the 2026-05-10 Kobo subsystem audit.

1. Magic-shelf 1000-book hard cap.
   sync_shelves's magic-shelf branch called
   `get_books_for_magic_shelf(shelf.id, page=1, page_size=1000)`,
   silently truncating shelves over 1000 books. The sibling helper
   get_magic_shelf_book_ids_for_kobo (used in the deletion-tracking
   flow) already passes page_size=None for full traversal; the sync
   path must do the same or the device sees a partial collection
   with no error indicator.

2. sync_shelves used `not ub.Shelf.kobo_sync` (Python's `not`) inside
   a SQLAlchemy filter chain. `not column` doesn't produce a SQL
   NOT expression; SQLAlchemy raises or coerces the column to bool,
   so the filter is broken. The intended SQL is
   `ub.Shelf.kobo_sync == False`. Effect: when only_kobo_shelves is
   True, the loop that's supposed to emit DeletedTag events for
   shelves removed from kobo-sync didn't filter correctly.
"""

import inspect

import pytest


@pytest.mark.unit
class TestMagicShelfFullTraversal:
    def test_handle_sync_uses_page_size_none_for_magic_shelves(self):
        from cps.kobo import HandleSyncRequest
        src = inspect.getsource(HandleSyncRequest)
        assert "get_books_for_magic_shelf(\n                shelf.id, page=1, page_size=None" in src or \
               "get_books_for_magic_shelf(shelf.id, page=1, page_size=None" in src, (
            "HandleSyncRequest's magic-shelf branch must pass "
            "page_size=None so shelves over 1000 books are not "
            "silently truncated. Mirrors get_magic_shelf_book_ids_"
            "for_kobo which already does this for the deletion path."
        )

    def test_handle_sync_does_not_pass_page_size_1000(self):
        from cps.kobo import HandleSyncRequest
        src = inspect.getsource(HandleSyncRequest)
        assert "page_size=1000" not in src, (
            "HandleSyncRequest must not cap magic-shelf page_size at "
            "1000 -- shelves over that limit get truncated silently."
        )


@pytest.mark.unit
class TestSyncShelvesKoboSyncFilter:
    def test_uses_proper_sqlalchemy_not(self):
        """sync_shelves's only_kobo_shelves branch must compare
        kobo_sync explicitly with `== False` (or `~`), not Python's
        `not` keyword which doesn't produce a SQL NOT against a
        Column object."""
        from cps.kobo import sync_shelves
        src = inspect.getsource(sync_shelves)
        assert "ub.Shelf.kobo_sync == False" in src or \
               "~ub.Shelf.kobo_sync" in src, (
            "sync_shelves must filter with `ub.Shelf.kobo_sync == "
            "False` (or `~ub.Shelf.kobo_sync`), not Python's `not`. "
            "`not column` does not produce a SQL NOT in SQLAlchemy."
        )
        assert "not ub.Shelf.kobo_sync" not in src, (
            "Python `not` against a SQLAlchemy Column doesn't produce "
            "a SQL NOT -- the filter is broken."
        )
