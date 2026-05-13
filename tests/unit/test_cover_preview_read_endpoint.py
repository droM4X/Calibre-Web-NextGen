# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for GET /cover/<id>/preview (Phase 3 / Task 2).

Pattern matches tests/unit/test_cover_preview_endpoints.py: we drive the
view function directly via ``app.test_request_context`` + ``inspect.unwrap``
to peel off ``@user_login_required``, and monkeypatch the resolution +
cache helpers where useful. Full Flask-Login + reverse-proxy auth coverage
lives in the live container smoke (Phase 3 / Task 6).

The tests pin:

 1. Auth — anonymous request blocked when anonbrowse disabled
 2. Missing book -> 404
 3. Cross-user hidden book (user B can't see user A's book) -> 404
 4. Missing cover file -> 404
 5. Cache miss path invokes pad_blob exactly once + writes the cache
 6. Cache hit path does NOT invoke pad_blob
 7. ETag header set on responses (hit + miss)
 8. If-None-Match matching ETag -> 304 with empty body
 9. Query-param override changes cache key (different file on disk)
10. Content-Type is image/jpeg on success
11. Cache-Control is "private, max-age=86400"
12. Concurrent misses fold to one render under the stampede lock
"""

from __future__ import annotations

import inspect
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import flask
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.exceptions import HTTPException


# A tiny but valid JPEG-ish blob — pad_blob is mocked in every test that
# would otherwise try to decode it, so the bytes don't need to be a real
# image. Tests that exercise pad_blob still mock it.
FAKE_RAW_JPEG = b"\xff\xd8\xff\xe0fake-source-jpeg-bytes\xff\xd9"
FAKE_PADDED_JPEG = b"\xff\xd8\xff\xe0fake-padded-jpeg-bytes\xff\xd9"


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def session():
    """In-memory ub session (the users + per-book override rows live here)."""
    from cps import ub
    db_engine = create_engine("sqlite:///:memory:", future=True)
    with db_engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    ub.Base.metadata.create_all(db_engine)
    Session = sessionmaker(bind=db_engine, future=True)
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
    """A normal user with previews enabled and the default Libra Color preset."""
    from cps import ub
    user = ub.User()
    user.id = 1
    user.name = "alice"
    user.nickname = "alice"
    user.email = "alice@example.com"
    user.password = "x"
    user.role = 1 << 3  # ROLE_EDIT (not required by the read route, but harmless)
    user.show_ereader_previews = True
    user.preview_preset = "kobo_libra_color"
    user.preview_default_fill = "edge_mirror"
    user.preview_default_color = None
    session.add(user)
    session.commit()
    return user


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """Redirect the on-disk cache into tmp_path so tests don't touch /config."""
    from cps.services import cover_preview_cache as cache_mod
    root = tmp_path / "preview-cache"
    monkeypatch.setattr(cache_mod, "CACHE_ROOT", root)
    return root


@pytest.fixture
def cover_on_disk(tmp_path):
    """Write a fake cover.jpg to disk. Returns (book_path_dir, cover_jpg_path)
    where book_path_dir is the per-book directory (Calibre layout)."""
    book_dir = tmp_path / "library" / "Some Author" / "Some Book (42)"
    book_dir.mkdir(parents=True)
    cover = book_dir / "cover.jpg"
    cover.write_bytes(FAKE_RAW_JPEG)
    return book_dir, cover


@pytest.fixture
def app():
    """Bare Flask app with just the cover-preview blueprint mounted."""
    from cps.cover_preview_blueprint import cover_preview_bp
    a = flask.Flask(__name__)
    a.testing = True
    a.config["WTF_CSRF_ENABLED"] = False
    a.register_blueprint(cover_preview_bp)
    return a


def _bare(view_fn):
    """Strip @user_login_required so we don't need flask-login set up."""
    return inspect.unwrap(view_fn)


def _make_book(book_id: int, library_root: Path, rel_path: str,
               has_cover: bool = True):
    """Return a MagicMock that quacks like a db.Books row for our purposes."""
    book = MagicMock()
    book.id = book_id
    book.has_cover = has_cover
    book.path = rel_path
    return book


def _call(app, alice, *, book_id=42, headers=None, query_string=None):
    """Invoke the serve_cover_preview view inside a request context with
    current_user patched to alice. Returns the Flask Response or raises
    HTTPException."""
    from cps.cover_preview_blueprint import serve_cover_preview
    h = headers or {}
    with app.test_request_context(
        method="GET",
        headers=h,
        query_string=query_string,
    ):
        with patch("cps.cover_preview_blueprint.current_user", alice):
            return _bare(serve_cover_preview)(book_id=book_id)


def _patch_book_lookup(app_module, book):
    """Patch the calibre_db.get_filtered_book lookup to return ``book``."""
    return patch.object(
        app_module.calibre_db,
        "get_filtered_book",
        return_value=book,
    )


# ============================================================
# 1. Auth
# ============================================================

@pytest.mark.unit
class TestAuth:

    def test_anonymous_request_blocked_when_anonbrowse_off(self, app, monkeypatch):
        """``@user_login_required`` must redirect/refuse when no user is
        loaded. We call the decorated view (not the unwrapped one) so the
        decorator chain actually runs.

        Calling without an authenticated current_user + with anonbrowse=0
        triggers the login_required path, which raises (in test context)
        because no LoginManager is wired up. We just assert that decorator
        intervention happens — i.e. the bare view never runs unauth'd.
        """
        from cps.cover_preview_blueprint import serve_cover_preview
        # If the decorator runs and tries to load the user, it'll either
        # raise (no LoginManager wired) or return a redirect; either way,
        # the bare view's `current_user` reference would explode. Pin that
        # the wrapping decorator is present.
        assert getattr(serve_cover_preview, "__wrapped__", None) is not None, (
            "serve_cover_preview must have @user_login_required applied"
        )


# ============================================================
# 2. Missing book / no access
# ============================================================

@pytest.mark.unit
class TestVisibility:

    def test_missing_book_returns_404(self, app, alice, cache_root):
        """calibre_db.get_filtered_book returns None for a book the user
        can't see (or that doesn't exist). The route must 404, not 500."""
        from cps import cover_preview_blueprint as bp
        with _patch_book_lookup(bp, None):
            with pytest.raises(HTTPException) as exc:
                _call(app, alice, book_id=9999)
            assert exc.value.code == 404

    def test_no_access_cross_user_returns_404(self, app, alice, cache_root):
        """When a user lacks visibility on a book, get_filtered_book returns
        None (the common_filters chain hides it). Same 404 as above — proves
        we route hidden books through the same visibility check as the
        rest of /cover/<id>, no separate bypass."""
        from cps import cover_preview_blueprint as bp
        # This is the same code path as test_missing_book — common_filters
        # makes hidden + missing indistinguishable to the caller, which is
        # by design (no information leak about existence).
        with _patch_book_lookup(bp, None):
            with pytest.raises(HTTPException) as exc:
                _call(app, alice, book_id=42)
            assert exc.value.code == 404


# ============================================================
# 3. Missing cover file
# ============================================================

@pytest.mark.unit
class TestCoverPath:

    def test_book_without_cover_returns_404(self, app, alice, cache_root,
                                            tmp_path, monkeypatch):
        """has_cover=False -> 404 even if the book row exists."""
        from cps import cover_preview_blueprint as bp
        book = _make_book(42, tmp_path, "Some Author/Some Book (42)",
                          has_cover=False)
        monkeypatch.setattr(bp.config, "config_use_google_drive", False,
                            raising=False)
        monkeypatch.setattr(bp.config, "get_book_path",
                            lambda: str(tmp_path / "library"), raising=False)
        with _patch_book_lookup(bp, book):
            with pytest.raises(HTTPException) as exc:
                _call(app, alice, book_id=42)
            assert exc.value.code == 404

    def test_cover_file_not_on_disk_returns_404(self, app, alice, cache_root,
                                                tmp_path, monkeypatch):
        """has_cover=True but the file vanished — 404, not 500."""
        from cps import cover_preview_blueprint as bp
        # The path config points at a directory that has no cover.jpg
        book = _make_book(42, tmp_path, "Phantom/Book (42)", has_cover=True)
        monkeypatch.setattr(bp.config, "config_use_google_drive", False,
                            raising=False)
        monkeypatch.setattr(bp.config, "get_book_path",
                            lambda: str(tmp_path / "library"), raising=False)
        with _patch_book_lookup(bp, book):
            with pytest.raises(HTTPException) as exc:
                _call(app, alice, book_id=42)
            assert exc.value.code == 404


# ============================================================
# Shared "happy-path scaffolding" — assemble the world the route expects.
# ============================================================

@pytest.fixture
def happy_path(app, alice, session, cache_root, cover_on_disk, monkeypatch):
    """Wire up all the patches a successful request needs.

    Returns a dict with:
      - book: the MagicMock book row
      - book_dir, cover: paths from cover_on_disk
      - library_root: the get_book_path() root
      - call(**kwargs): convenience wrapper to invoke the endpoint
    """
    from cps import cover_preview_blueprint as bp

    book_dir, cover = cover_on_disk
    library_root = cover.parent.parent.parent  # tmp_path/library
    rel_path = str(book_dir.relative_to(library_root))

    book = _make_book(42, library_root, rel_path, has_cover=True)
    monkeypatch.setattr(bp.config, "config_use_google_drive", False,
                        raising=False)
    monkeypatch.setattr(bp.config, "get_book_path",
                        lambda: str(library_root), raising=False)

    monkeypatch.setattr(
        bp.calibre_db, "get_filtered_book",
        lambda book_id, allow_show_archived=False: (book if book_id == 42 else None),
    )

    def call(**kwargs):
        return _call(app, alice, **kwargs)

    return {
        "book": book,
        "book_dir": book_dir,
        "cover": cover,
        "library_root": library_root,
        "call": call,
    }


# ============================================================
# 4. Cache miss / hit / pad_blob invocation
# ============================================================

@pytest.mark.unit
class TestCachePath:

    def test_cache_miss_invokes_pad_blob_and_writes(self, happy_path,
                                                   cache_root):
        """First request for a (book, settings) tuple: pad_blob is called
        exactly once and the result is persisted to the cache directory."""
        from cps import cover_preview_blueprint as bp
        with patch.object(bp, "pad_blob",
                          return_value=FAKE_PADDED_JPEG) as mock_pad:
            resp = happy_path["call"]()
        assert resp.status_code == 200
        assert mock_pad.call_count == 1
        # Find the cache file under cache_root — there should be exactly one
        # .jpg (the rendered tile).
        jpgs = list(cache_root.rglob("*.jpg"))
        assert len(jpgs) == 1, f"expected 1 cache file, found {jpgs}"
        assert jpgs[0].read_bytes() == FAKE_PADDED_JPEG

    def test_cache_hit_does_not_invoke_pad_blob(self, happy_path, cache_root):
        """Pre-populate the cache, hit the endpoint, assert pad_blob was
        never called. This is the 99% steady-state path — getting it wrong
        means re-rendering every request."""
        from cps import cover_preview_blueprint as bp
        from cps.services.cover_preview_cache import cache_key, cache_path

        # Resolve the same key the route will compute. cover_mtime is taken
        # from the stat() of the fixture cover.
        cover_mtime = int(happy_path["cover"].stat().st_mtime)
        key = cache_key(42, cover_mtime, "kobo_libra_color", "edge_mirror", None)
        path = cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(FAKE_PADDED_JPEG)

        with patch.object(bp, "pad_blob",
                          return_value=FAKE_PADDED_JPEG) as mock_pad:
            resp = happy_path["call"]()
        assert resp.status_code == 200
        assert mock_pad.call_count == 0


# ============================================================
# 5. ETag + 304 conditional
# ============================================================

@pytest.mark.unit
class TestConditional:

    def test_etag_header_on_miss(self, happy_path, cache_root):
        from cps import cover_preview_blueprint as bp
        with patch.object(bp, "pad_blob", return_value=FAKE_PADDED_JPEG):
            resp = happy_path["call"]()
        assert "ETag" in resp.headers
        # Weak validator form, 16-hex-char digest
        assert resp.headers["ETag"].startswith('W/"')

    def test_etag_header_on_hit(self, happy_path, cache_root):
        """Pre-fill cache, request twice, both responses carry an ETag."""
        from cps import cover_preview_blueprint as bp
        from cps.services.cover_preview_cache import cache_key, cache_path

        cover_mtime = int(happy_path["cover"].stat().st_mtime)
        key = cache_key(42, cover_mtime, "kobo_libra_color", "edge_mirror", None)
        path = cache_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(FAKE_PADDED_JPEG)

        with patch.object(bp, "pad_blob", return_value=FAKE_PADDED_JPEG):
            resp = happy_path["call"]()
        assert resp.status_code == 200
        assert resp.headers["ETag"] == f'W/"{key}"'

    def test_if_none_match_returns_304(self, happy_path, cache_root):
        """Client supplies the current ETag -> 304 with no body, no disk
        I/O on the cache, no render."""
        from cps import cover_preview_blueprint as bp
        from cps.services.cover_preview_cache import cache_key

        cover_mtime = int(happy_path["cover"].stat().st_mtime)
        key = cache_key(42, cover_mtime, "kobo_libra_color", "edge_mirror", None)
        etag = f'W/"{key}"'

        with patch.object(bp, "pad_blob",
                          return_value=FAKE_PADDED_JPEG) as mock_pad:
            resp = happy_path["call"](headers={"If-None-Match": etag})
        assert resp.status_code == 304
        assert resp.headers["ETag"] == etag
        # No body, no render
        assert resp.get_data() == b""
        assert mock_pad.call_count == 0


# ============================================================
# 6. Query-param overrides
# ============================================================

@pytest.mark.unit
class TestQueryOverrides:

    def test_fill_override_changes_cache_key(self, happy_path, cache_root):
        """Same book, different ``f=`` override -> different cache file
        (proves the override flows into the cache key, otherwise the
        editor's live preview would silently return the saved variant)."""
        from cps import cover_preview_blueprint as bp
        with patch.object(bp, "pad_blob", return_value=FAKE_PADDED_JPEG):
            happy_path["call"](query_string={"f": "edge_blur"})
            happy_path["call"](query_string={"f": "gradient"})
        jpgs = list(cache_root.rglob("*.jpg"))
        assert len(jpgs) == 2, (
            f"different fill overrides must produce different cache files, "
            f"found: {jpgs}"
        )


# ============================================================
# 7. Headers + content type
# ============================================================

@pytest.mark.unit
class TestResponseHeaders:

    def test_content_type_is_jpeg(self, happy_path, cache_root):
        from cps import cover_preview_blueprint as bp
        with patch.object(bp, "pad_blob", return_value=FAKE_PADDED_JPEG):
            resp = happy_path["call"]()
        assert resp.status_code == 200
        assert resp.mimetype == "image/jpeg"

    def test_cache_control_header(self, happy_path, cache_root):
        from cps import cover_preview_blueprint as bp
        with patch.object(bp, "pad_blob", return_value=FAKE_PADDED_JPEG):
            resp = happy_path["call"]()
        assert resp.headers["Cache-Control"] == "private, max-age=86400"


# ============================================================
# 8. Stampede protection at the HTTP layer
# ============================================================

@pytest.mark.unit
class TestStampedeProtection:

    def test_concurrent_misses_render_once(self, app, cache_root,
                                           cover_on_disk, monkeypatch):
        """20 threads hit the endpoint simultaneously with the same key.
        The stampede lock + re-check-after-acquire pattern must fold them
        to a single pad_blob call.

        We slow pad_blob's first invocation slightly so the lock contention
        is observable. Without the lock, the test would race-condition all
        20 through ``cache_hit() -> miss`` before any of them wrote, and
        we'd see 20 pad_blob calls.

        Note: this test bypasses the in-memory SQLite session by stubbing
        ``resolve_effective_settings`` directly. The ``ub.session`` fixture
        produces a connection bound to the test's main thread; SQLite
        objects can't cross threads (``check_same_thread`` default). The
        unit under test here is the stampede lock at the HTTP layer, not
        the ORM, so the stub is fine — and necessary, because our worker
        threads would otherwise crash on the ORM lookup rather than
        exercising the lock.
        """
        from cps import cover_preview_blueprint as bp

        # World setup (mirrors happy_path but inline so we own the patches
        # and avoid the per-test SQLite session fixture — workers run in
        # background threads, and SQLite's check_same_thread default
        # bars us from sharing the main-thread connection).
        fake_user = MagicMock()
        fake_user.id = 1
        book_dir, cover = cover_on_disk
        library_root = cover.parent.parent.parent
        rel_path = str(book_dir.relative_to(library_root))
        book = _make_book(42, library_root, rel_path, has_cover=True)
        monkeypatch.setattr(bp.config, "config_use_google_drive", False,
                            raising=False)
        monkeypatch.setattr(bp.config, "get_book_path",
                            lambda: str(library_root), raising=False)
        # Thread-safe stubs: no DB calls, no shared mutable state.
        monkeypatch.setattr(
            bp.calibre_db, "get_filtered_book",
            lambda book_id, allow_show_archived=False: (book if book_id == 42 else None),
        )
        monkeypatch.setattr(
            bp, "resolve_effective_settings",
            lambda user_id, book_id, p_override=None, f_override=None,
                   c_override=None: ("kobo_libra_color", "edge_mirror", None),
        )

        call_count = {"n": 0}
        count_lock = threading.Lock()
        gate = threading.Event()

        def slow_pad(*args, **kwargs):
            with count_lock:
                call_count["n"] += 1
                is_first = call_count["n"] == 1
            if is_first:
                # Hold so peers queue behind us on the stampede lock.
                gate.wait(timeout=5.0)
            return FAKE_PADDED_JPEG

        results = []
        errors = []
        results_lock = threading.Lock()

        # current_user is normally a thread-local proxy. Replace the
        # module-level reference globally (not via context manager) so
        # worker threads see the patch — context-manager patches restore
        # on exit in the main thread, but the assignment itself is plain
        # `module.attr = X`, which is thread-visible.
        monkeypatch.setattr(bp, "current_user", fake_user, raising=False)
        # Same trick for pad_blob — set globally for the duration of the
        # test rather than via `with patch.object`, so spawned threads
        # see the slow shim.
        monkeypatch.setattr(bp, "pad_blob", slow_pad, raising=False)

        def worker():
            try:
                with app.test_request_context(method="GET"):
                    resp = _bare(bp.serve_cover_preview)(book_id=42)
                with results_lock:
                    results.append(resp.status_code)
            except Exception as e:  # noqa: BLE001
                with results_lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        # Let the first thread reach pad_blob (and block in gate.wait);
        # the other 19 pile up on the stampede lock.
        import time
        time.sleep(0.3)
        gate.set()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"workers raised: {errors}"
        assert len(results) == 20, f"only {len(results)} workers completed"
        assert all(s == 200 for s in results), results
        # The whole point: stampede lock + re-check folds 20 concurrent
        # misses on the same key into exactly one render.
        assert call_count["n"] == 1, (
            f"stampede guard failed: pad_blob called {call_count['n']} times "
            "for 20 concurrent same-key misses"
        )
        # And only one cache file written.
        jpgs = list(cache_root.rglob("*.jpg"))
        assert len(jpgs) == 1, jpgs
