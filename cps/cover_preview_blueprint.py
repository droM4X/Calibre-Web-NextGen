# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""HTTP endpoints for cover-preview user prefs and per-book overrides.

The image-render endpoint ``GET /cover/<id>/preview`` is Phase 3; this
blueprint covers the write paths only:

    POST /me/cover/preview-defaults        -> set user defaults
    POST /book/<id>/cover/preview-settings -> upsert per-book override
    POST /book/<id>/cover/preview-lock     -> toggle per-book lock
    POST /me/cover/preview-apply-to-all    -> set defaults + wipe unlocked overrides
    POST /books/cover/preview-bulk-apply   -> apply fill+color to N books (cap 5000)

CSRF: every POST is protected by the global CSRFProtect middleware
initialized in cps.__init__ (no per-route decorator needed). Anonymous
callers are blocked by ``@user_login_required``; the four endpoints that
mutate per-book state additionally require edit role via the local
``edit_required`` wrapper (mirrors the one in cps.cover_picker).

Validation: ``fill_mode`` must be a member of ``engine.FILL_MODES``,
``preset`` must be a key in ``engine.PRESET_ASPECTS``, colors must look
like ``#rgb`` or ``#rrggbb`` (empty/None normalize to None). Bad input
returns HTTP 400 via ``abort()``.
"""
from __future__ import annotations

import datetime
from functools import wraps
from typing import Any, Optional

from flask import Blueprint, abort, jsonify, request

from cps import ub
from cps.cw_login import current_user
from cps.services import cover_preview as engine
from cps.usermanagement import user_login_required


cover_preview_bp = Blueprint("cover_preview_bp", __name__)


# ---- decorators -----------------------------------------------------------

def edit_required(f):
    """Mirrors cps.cover_picker.edit_required — admin or edit-role only."""
    @wraps(f)
    def inner(*args, **kwargs):
        if current_user.role_edit() or current_user.role_admin():
            return f(*args, **kwargs)
        abort(403)
    return inner


# ---- validation helpers ---------------------------------------------------

def _validate_fill_mode(fill_mode: Any) -> str:
    """Validate fill_mode is one of the known engine constants.
    Returns the validated string or raises a 400."""
    if not isinstance(fill_mode, str) or fill_mode not in engine.FILL_MODES:
        abort(400, description=f"unknown fill_mode (must be one of {sorted(engine.FILL_MODES)})")
    return fill_mode


def _validate_color(color: Any) -> Optional[str]:
    """Validate color is a hex string like '#rgb' / '#rrggbb' or None.
    Empty strings normalize to None."""
    if color is None or color == "":
        return None
    if not isinstance(color, str):
        abort(400, description="color must be a string")
    if not color.startswith("#") or len(color) not in (4, 7):
        abort(400, description="color must be '#rgb' or '#rrggbb'")
    try:
        int(color[1:], 16)
    except ValueError:
        abort(400, description="color hex digits invalid")
    return color


def _validate_preset(preset: Any) -> str:
    """Validate preset is a key in engine.PRESET_ASPECTS."""
    if not isinstance(preset, str) or preset not in engine.PRESET_ASPECTS:
        abort(400, description="unknown preset (must be a key in PRESET_ASPECTS)")
    return preset


# ---- endpoints ------------------------------------------------------------

@cover_preview_bp.route("/me/cover/preview-defaults", methods=["POST"])
@user_login_required
def set_my_preview_defaults():
    """Update the calling user's preview defaults (toggle + preset + fill + color).
    All fields optional; only present fields are updated."""
    body = request.get_json(silent=True) or {}

    if "show_ereader_previews" in body:
        current_user.show_ereader_previews = bool(body["show_ereader_previews"])
    if "preview_preset" in body:
        current_user.preview_preset = _validate_preset(body["preview_preset"])
    if "default_fill" in body:
        current_user.preview_default_fill = _validate_fill_mode(body["default_fill"])
    if "default_color" in body:
        current_user.preview_default_color = _validate_color(body["default_color"])

    ub.session.merge(current_user)
    ub.session.commit()

    return jsonify({
        "ok": True,
        "show_ereader_previews": bool(current_user.show_ereader_previews),
        "preview_preset": current_user.preview_preset,
        "preview_default_fill": current_user.preview_default_fill,
        "preview_default_color": current_user.preview_default_color,
    })


@cover_preview_bp.route("/book/<int:book_id>/cover/preview-settings", methods=["POST"])
@user_login_required
@edit_required
def set_book_preview_settings(book_id):
    """Upsert a per-book override row for the calling user. Body:
    ``{"fill_mode": "...", "custom_color": "#..." | null, "locked": bool}``.
    Returns the resulting effective state for the row."""
    body = request.get_json(silent=True) or {}

    fill_mode = _validate_fill_mode(body.get("fill_mode"))
    custom_color = _validate_color(body.get("custom_color"))
    locked = bool(body.get("locked", False))

    row = ub.session.query(ub.BookCoverPreview).filter(
        ub.BookCoverPreview.user_id == current_user.id,
        ub.BookCoverPreview.book_id == book_id,
    ).first()
    now = datetime.datetime.utcnow()
    if row is None:
        row = ub.BookCoverPreview(
            user_id=current_user.id,
            book_id=book_id,
            fill_mode=fill_mode,
            custom_color=custom_color,
            locked=locked,
            updated_at=now,
        )
        ub.session.add(row)
    else:
        row.fill_mode = fill_mode
        row.custom_color = custom_color
        row.locked = locked
        row.updated_at = now
    ub.session.commit()

    return jsonify({
        "ok": True,
        "book_id": book_id,
        "fill_mode": row.fill_mode,
        "custom_color": row.custom_color,
        "locked": bool(row.locked),
    })


@cover_preview_bp.route("/book/<int:book_id>/cover/preview-lock", methods=["POST"])
@user_login_required
@edit_required
def toggle_book_preview_lock(book_id):
    """Toggle lock on a per-book row. If no row exists, create one
    with the user's current effective settings, locked=<target>."""
    body = request.get_json(silent=True) or {}
    target = bool(body.get("locked", True))

    row = ub.session.query(ub.BookCoverPreview).filter(
        ub.BookCoverPreview.user_id == current_user.id,
        ub.BookCoverPreview.book_id == book_id,
    ).first()
    now = datetime.datetime.utcnow()
    if row is None:
        # Materialize an override row using whatever the user currently sees.
        from cps.services.cover_preview_resolution import resolve_effective_settings
        _, fill, color = resolve_effective_settings(current_user.id, book_id)
        row = ub.BookCoverPreview(
            user_id=current_user.id,
            book_id=book_id,
            fill_mode=fill,
            custom_color=color,
            locked=target,
            updated_at=now,
        )
        ub.session.add(row)
    else:
        row.locked = target
        row.updated_at = now
    ub.session.commit()

    return jsonify({"ok": True, "book_id": book_id, "locked": target})


@cover_preview_bp.route("/me/cover/preview-apply-to-all", methods=["POST"])
@user_login_required
@edit_required
def apply_preview_to_all():
    """Set the calling user's default fill/color AND optionally wipe
    every unlocked per-book override row. Body:
    ``{"fill_mode": "...", "custom_color": "#..." | null,
       "wipe_unlocked": bool (default true)}``.
    Returns count of unlocked rows updated and count of locked rows skipped."""
    body = request.get_json(silent=True) or {}

    new_fill = _validate_fill_mode(body.get("fill_mode"))
    new_color = _validate_color(body.get("custom_color"))
    wipe_unlocked = bool(body.get("wipe_unlocked", True))

    skipped_locked = ub.session.query(ub.BookCoverPreview).filter(
        ub.BookCoverPreview.user_id == current_user.id,
        ub.BookCoverPreview.locked == True,  # noqa: E712 — SQLAlchemy needs ==
    ).count()

    updated_books = 0
    if wipe_unlocked:
        # 1) Coerce all unlocked rows to the new defaults.
        updated_books = ub.session.query(ub.BookCoverPreview).filter(
            ub.BookCoverPreview.user_id == current_user.id,
            ub.BookCoverPreview.locked == False,  # noqa: E712
        ).update(
            {
                "fill_mode": new_fill,
                "custom_color": new_color,
                "updated_at": datetime.datetime.utcnow(),
            },
            synchronize_session=False,
        )

        # 2) Compact: unlocked rows that now match the user's defaults
        # are redundant (no row == follows-defaults), so delete them.
        # NULL-safe comparison: SQLite's `=` returns NULL when either side
        # is NULL, so branch on the Python value to build the right SQL.
        delete_q = ub.session.query(ub.BookCoverPreview).filter(
            ub.BookCoverPreview.user_id == current_user.id,
            ub.BookCoverPreview.locked == False,  # noqa: E712
            ub.BookCoverPreview.fill_mode == new_fill,
        )
        if new_color is None:
            delete_q = delete_q.filter(ub.BookCoverPreview.custom_color.is_(None))
        else:
            delete_q = delete_q.filter(ub.BookCoverPreview.custom_color == new_color)
        delete_q.delete(synchronize_session=False)

    current_user.preview_default_fill = new_fill
    current_user.preview_default_color = new_color
    ub.session.merge(current_user)
    ub.session.commit()

    return jsonify({
        "ok": True,
        "updated_books": int(updated_books),
        "skipped_locked": int(skipped_locked),
    })


@cover_preview_bp.route("/books/cover/preview-bulk-apply", methods=["POST"])
@user_login_required
@edit_required
def bulk_apply_preview():
    """Apply fill_mode + custom_color (and optionally lock) to a list of
    book_ids for the calling user. Body:
    ``{"book_ids": [int, ...], "fill_mode": "...",
       "custom_color": "#..." | null, "lock": bool (default false)}``.
    Cap: 5000 book_ids per request (matches CW's bulk-edit cap)."""
    body = request.get_json(silent=True) or {}

    book_ids = body.get("book_ids")
    if not isinstance(book_ids, list) or not book_ids:
        abort(400, description="book_ids must be a non-empty list")
    if len(book_ids) > 5000:
        abort(400, description="book_ids exceeds 5000-item cap")
    if not all(isinstance(b, int) and not isinstance(b, bool) for b in book_ids):
        abort(400, description="book_ids must be a list of integers")

    fill_mode = _validate_fill_mode(body.get("fill_mode"))
    custom_color = _validate_color(body.get("custom_color"))
    lock = bool(body.get("lock", False))

    affected = 0
    now = datetime.datetime.utcnow()
    for bid in book_ids:
        row = ub.session.query(ub.BookCoverPreview).filter(
            ub.BookCoverPreview.user_id == current_user.id,
            ub.BookCoverPreview.book_id == bid,
        ).first()
        if row is None:
            row = ub.BookCoverPreview(
                user_id=current_user.id,
                book_id=bid,
                fill_mode=fill_mode,
                custom_color=custom_color,
                locked=lock,
                updated_at=now,
            )
            ub.session.add(row)
        else:
            row.fill_mode = fill_mode
            row.custom_color = custom_color
            if lock:
                row.locked = True
            row.updated_at = now
        affected += 1
    ub.session.commit()

    return jsonify({"ok": True, "affected": affected})
