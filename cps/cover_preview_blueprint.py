# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""HTTP endpoints for cover-preview user prefs, per-book overrides, and
the hot read path for rendered preview tiles.

Write endpoints (Phase 2):

    POST /me/cover/preview-defaults        -> set user defaults
    POST /book/<id>/cover/preview-settings -> upsert per-book override
    POST /book/<id>/cover/preview-lock     -> toggle per-book lock
    POST /me/cover/preview-apply-to-all    -> set defaults + wipe unlocked overrides
    POST /books/cover/preview-bulk-apply   -> apply fill+color to N books (cap 5000)

Read endpoint (Phase 3):

    GET /cover/<id>/preview                -> serve padded cover tile

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
import logging
import os
from functools import wraps
from pathlib import Path
from typing import Any, Optional, Tuple

from flask import Blueprint, Response, abort, jsonify, request, send_file

from cps import calibre_db, config, ub
from cps.cw_login import current_user
from cps.services import cover_preview as engine
from cps.services.cover_preview import CoverPreviewSettings, pad_blob
from cps.services.cover_preview_cache import (
    cache_hit,
    cache_key,
    stampede_lock,
    write_to_cache,
)
from cps.services.cover_preview_resolution import resolve_effective_settings
from cps.usermanagement import user_login_required

log = logging.getLogger(__name__)


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


# ---- read path: GET /cover/<id>/preview -----------------------------------
#
# Hot path: every cover render goes through here when the user has e-reader
# previews enabled. The auth + visibility + on-disk path resolution all
# mirror cps.web.get_cover -> cps.helper.get_book_cover_internal exactly so
# we never create a security drift between the two routes. Phase 3 Task 5
# will extract those checks into shared helpers; until then we inline.

def _resolve_book_for_current_user(book_id: int):
    """Look up the book through ``calibre_db.get_filtered_book`` so the
    visibility filters (anonymous mode, hidden books, archived, role
    restrictions) are applied exactly as ``/cover/<id>`` applies them.

    Returns the ``db.Books`` row, or ``None`` when the caller can't see it.
    """
    # allow_show_archived=True matches helper.get_book_cover -> we still want
    # to serve covers for archived books the user has access to (just like
    # /cover/<id> does), the user just won't see them in the main list.
    return calibre_db.get_filtered_book(book_id, allow_show_archived=True)


def _resolve_cover_disk_path(book) -> Optional[Path]:
    """Resolve the on-disk ``cover.jpg`` path for ``book``, mirroring the
    Calibre-directory branch of ``helper.get_book_cover_internal``.

    Returns ``None`` when:
      - the book has no cover (``has_cover`` is falsy),
      - Google Drive backing is configured (we don't proxy through GD
        from the preview path — the original /cover/<id> already handles
        that case, and the preview UI hides itself when GD is active),
      - the file isn't on disk.

    The pad engine needs the raw cover bytes, which means we need an
    actual filesystem path. The GD branch in get_book_cover_internal
    returns a Response wrapping a stream; we'd need GD-specific plumbing
    to extend the preview path there. Task 5 may add it; for now we let
    the original /cover/<id> path serve GD-backed covers unpadded.
    """
    if not book or not getattr(book, "has_cover", False):
        return None

    # GD-backed libraries: out of scope for the preview path today.
    # The frontend should not be requesting /cover/<id>/preview on a
    # GD-backed deployment, but if it does we 404 rather than silently
    # falling through to the unpadded image (which would mask the
    # config issue).
    if getattr(config, "config_use_google_drive", False):
        return None

    try:
        book_dir = os.path.join(config.get_book_path(), book.path)
        cover_path = Path(book_dir) / "cover.jpg"
    except Exception:
        # Defensive: config.get_book_path() can raise if the library
        # path isn't configured. A misconfigured server shouldn't 500
        # the preview route — 404 lets the frontend fall back to the
        # unpadded /cover/<id> route gracefully.
        return None

    return cover_path


def _send_cached(path: Path, etag: str) -> Response:
    """Standard cache-hit response shape: send the JPEG from disk with the
    ETag header set and a private 1-day cache so the browser can serve
    304s of its own. ``conditional=False`` because we handle If-None-Match
    ourselves at the route level (the cache key already accounts for
    cover mtime + settings)."""
    response = send_file(str(path), mimetype="image/jpeg", conditional=False)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


def _send_inline(jpeg_bytes: bytes, etag: str) -> Response:
    """Fallback for disk-write failures: serve the freshly-rendered bytes
    directly without persisting. The next request will retry the cache
    write — better to serve an uncached image than to 500 because the
    cache filesystem is full or unwritable."""
    return Response(
        jpeg_bytes,
        mimetype="image/jpeg",
        headers={
            "ETag": etag,
            "Cache-Control": "private, max-age=86400",
        },
    )


@cover_preview_bp.route("/cover/<int:book_id>/preview", methods=["GET"])
@user_login_required
def serve_cover_preview(book_id: int):
    """Serve a padded cover-preview tile for ``book_id``.

    Auth + visibility mirror ``/cover/<id>``: ``user_login_required``
    handles authn (with reverse-proxy header support); the calibre_db
    filter inside ``_resolve_book_for_current_user`` applies the same
    common_filters() chain that the canonical cover route applies.

    Query params (optional, used by the cover-editor live preview):
      - ``p``: preset key override
      - ``f``: fill_mode override
      - ``c``: hex color override

    Response shape:
      - 200 with image/jpeg body + ETag + Cache-Control on success
      - 304 (empty) when If-None-Match matches the current ETag
      - 404 when book is missing, hidden, or has no on-disk cover
      - 503 when the render engine fails (logged; client should retry)
    """
    # 1. Resolve the book through the same filter chain as /cover/<id>.
    book = _resolve_book_for_current_user(book_id)
    if book is None:
        abort(404)

    # 2. Find the actual file on disk.
    cover_path = _resolve_cover_disk_path(book)
    if cover_path is None:
        abort(404)
    try:
        if not cover_path.is_file():
            abort(404)
        cover_mtime = int(cover_path.stat().st_mtime)
    except OSError:
        # Permission denied / NFS hiccup. Treat as missing.
        abort(404)

    # 3. Resolve effective settings (per-user defaults + per-book override +
    #    optional query-param overrides). The query params let the cover
    #    editor render a live preview without persisting the choice yet.
    p_q = request.args.get("p") or None
    f_q = request.args.get("f") or None
    c_q = request.args.get("c") or None
    preset, fill, color = resolve_effective_settings(
        current_user.id,
        book_id,
        p_override=p_q,
        f_override=f_q,
        c_override=c_q,
    )

    # 4. Build cache key + ETag. The key encodes (book, mtime, settings)
    #    so any change to the underlying cover or to the user's resolved
    #    settings produces a new key naturally — no explicit purge needed.
    key = cache_key(book_id, cover_mtime, preset, fill, color)
    etag = f'W/"{key}"'

    # 5. Browser conditional GET fast path. Bail before any disk I/O if
    #    the client's cached copy is still valid.
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag})

    # 6. Cache hit fast path (no lock needed for read).
    hit = cache_hit(key)
    if hit is not None:
        return _send_cached(hit, etag)

    # 7. Cache miss — serialize the render under the per-key stampede
    #    lock so N simultaneous misses on the same tile fold to one
    #    render. Critical for cold-cache first-page-load bursts where
    #    20+ tiles miss in parallel.
    with stampede_lock(key):
        # Re-check inside the lock: another thread may have rendered and
        # written the file while we were waiting on the lock. Without
        # this re-check we'd render N-1 redundant times under load.
        hit = cache_hit(key)
        if hit is not None:
            return _send_cached(hit, etag)

        try:
            raw_blob = cover_path.read_bytes()
        except OSError:
            # The file disappeared between stat() and read() — race with
            # an ingest or metadata edit. 404 is correct; client can retry.
            abort(404)

        # The render engine has its own settings type. We synthesize one
        # from the resolved triple — ``enabled=True`` because being on
        # this route at all means the user wants padding; the toggle
        # check happens at the template/frontend layer (templates only
        # rewrite img src to /preview when show_ereader_previews is on).
        settings = CoverPreviewSettings(
            enabled=True,
            target_aspect=preset,
            fill_mode=fill,
            manual_color=color or "",
        )
        try:
            padded = pad_blob(raw_blob, settings)
        except Exception as exc:  # noqa: BLE001 - engine surface is broad
            # Render failures (corrupt JPEG, OOM, IM crash) get a 503 so
            # the browser knows to retry, not a 500 which it would cache.
            log.warning(
                "cover_preview render failed for book %s (key=%s): %s",
                book_id, key, exc,
            )
            abort(503)

        cache_file = write_to_cache(key, padded)
        if cache_file is None:
            # Disk-write failure (full, permission, read-only FS). Serve
            # the bytes inline so the user still sees their cover; the
            # next request will retry the write. Operator should see the
            # debug log from write_to_cache and fix the underlying issue.
            return _send_inline(padded, etag)

        return _send_cached(cache_file, etag)
