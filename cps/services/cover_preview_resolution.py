# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Effective-settings resolution for cover-preview rendering.

Given a user and a book, computes the (preset, fill_mode, color)
tuple the rendering engine should use. Precedence (highest to
lowest):

1. Explicit query-param overrides (used by the live preview in the
   cover editor — UI passes the controls' current values without
   persisting them).
2. The user's per-book override row in book_cover_preview (if one
   exists). Locked rows are returned as-is.
3. The user's default preview_default_fill + preview_default_color +
   preview_preset (on the user row).

The preset is always taken from the user's row — there is no per-book
aspect override (aspect is a per-user device choice).
"""

from __future__ import annotations

from typing import Optional, Tuple

from cps import ub
from cps.services import cover_preview as engine


def resolve_effective_settings(
    user_id: int,
    book_id: int,
    p_override: Optional[str] = None,
    f_override: Optional[str] = None,
    c_override: Optional[str] = None,
) -> Tuple[str, str, Optional[str]]:
    """Return (preset, fill_mode, color) for rendering this user's
    view of this book."""
    user = ub.session.query(ub.User).filter(ub.User.id == user_id).first()
    if user is None:
        return engine.DEFAULT_PRESET, engine.DEFAULT_FILL_MODE, None

    override = (
        ub.session.query(ub.BookCoverPreview)
        .filter(
            ub.BookCoverPreview.user_id == user_id,
            ub.BookCoverPreview.book_id == book_id,
        )
        .first()
    )

    preset = p_override or user.preview_preset or engine.DEFAULT_PRESET

    if f_override is not None:
        fill = f_override
    elif override is not None:
        fill = override.fill_mode
    else:
        fill = user.preview_default_fill or engine.DEFAULT_FILL_MODE

    if c_override is not None:
        color = c_override
    elif override is not None:
        color = override.custom_color
    else:
        color = user.preview_default_color

    return preset, fill, color


def is_book_locked_for_user(user_id: int, book_id: int) -> bool:
    """True when a user has explicitly locked this book's per-book
    settings against apply-to-all sweeps."""
    override = (
        ub.session.query(ub.BookCoverPreview)
        .filter(
            ub.BookCoverPreview.user_id == user_id,
            ub.BookCoverPreview.book_id == book_id,
        )
        .first()
    )
    return bool(override and override.locked)
