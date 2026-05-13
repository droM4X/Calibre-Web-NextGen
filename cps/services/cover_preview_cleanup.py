# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Orphan cleanup for book_cover_preview rows.

When a book is deleted from Calibre's metadata.db, our per-user
`book_cover_preview` rows keyed on that book_id become orphans —
SQLAlchemy can't FK-cascade because the two databases are separate
SQLite files. This module provides a sweep helper that the app can
call at startup or periodically to remove orphan rows.

Scheduler wiring is intentionally deferred to a follow-up. For now the
operator can invoke this directly (e.g. via a future admin button or a
periodic task registered in `cps/schedule.py`):

    from cps.services.cover_preview_cleanup import sweep_orphaned_cover_previews
    deleted = sweep_orphaned_cover_previews()
    log.info("cover-preview cleanup: removed %d orphan rows", deleted)
"""

from __future__ import annotations

from cps import calibre_db, db, ub


def sweep_orphaned_cover_previews() -> int:
    """Remove rows from `book_cover_preview` whose `book_id` no longer
    exists in metadata.db. Returns the count of rows deleted.

    Safe to call at any time. If there are no books in metadata.db
    (fresh install / empty library), this is a no-op — we don't
    interpret 'no books exist' as 'every row is an orphan'.
    """
    # Collect all current book ids from the Calibre metadata DB.
    try:
        all_book_ids = {row[0] for row in calibre_db.session.query(db.Books.id).all()}
    except Exception:
        # If the Calibre DB isn't available, skip — better to keep
        # potential orphans than wipe valid rows.
        return 0

    if not all_book_ids:
        # No books to compare against — treat as nothing-to-do.
        return 0

    orphan_rows = (
        ub.session.query(ub.BookCoverPreview)
        .filter(~ub.BookCoverPreview.book_id.in_(all_book_ids))
        .all()
    )
    count = len(orphan_rows)
    for row in orphan_rows:
        ub.session.delete(row)
    if count:
        ub.session.commit()
    return count
