# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit + smoke tests for the Kobo cover preview endpoint (issue #84).

The endpoint at POST /book/<id>/cover/kobo-preview re-renders an image
through the existing cover_padding pipeline and returns a base64 data
URL. We test:

1. The new pure helper render_kobo_preview_data_url() in
   cps.services.cover_padding (extends the existing module rather than
   adding a new one — pad_blob lives there too).
2. Static structure of the Flask endpoint registered in
   cps.cover_picker (route present, function name, calls pad_blob,
   returns JSON).

Tests use the spec_from_file_location shim so they don't require full
cps package init.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _ensure_cps_stub():
    """Top up sys.modules with the minimal cps package stub. Idempotent
    so the test can co-exist with the other unit tests that share it."""
    cps_pkg = sys.modules.get("cps")
    if cps_pkg is None:
        cps_pkg = types.ModuleType("cps")
        cps_pkg.__path__ = [str(REPO_ROOT / "cps")]
        sys.modules["cps"] = cps_pkg

    constants = sys.modules.get("cps.constants") or types.ModuleType("cps.constants")
    if not hasattr(constants, "USER_AGENT"):
        constants.USER_AGENT = "Calibre-Web-NextGen-tests"
    sys.modules["cps.constants"] = constants
    cps_pkg.constants = constants

    logger_mod = sys.modules.get("cps.logger") or types.ModuleType("cps.logger")
    if not hasattr(logger_mod, "create"):
        logger_mod.create = lambda *_a, **_k: types.SimpleNamespace(
            debug=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
            error=lambda *_args, **_kwargs: None,
        )
    sys.modules["cps.logger"] = logger_mod
    cps_pkg.logger = logger_mod

    if "cps.services" not in sys.modules:
        services_pkg = types.ModuleType("cps.services")
        services_pkg.__path__ = [str(REPO_ROOT / "cps" / "services")]
        sys.modules["cps.services"] = services_pkg


def _load_cover_padding():
    _ensure_cps_stub()
    spec = importlib.util.spec_from_file_location(
        "cps.services.cover_padding",
        REPO_ROOT / "cps" / "services" / "cover_padding.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cps.services.cover_padding"] = module
    spec.loader.exec_module(module)
    return module


def _make_jpeg_bytes(width: int = 200, height: int = 300, color=(180, 60, 60)) -> bytes:
    """Build a real JPEG using Wand if present, falling back to a tiny
    static byte string (which the pad_blob will refuse and pass through).
    Tests that need real image processing must skip when Wand is missing."""
    try:
        from wand.image import Image
        from wand.color import Color
    except ImportError:
        pytest.skip("Wand/ImageMagick not available in this environment")

    color_str = "rgb(%d,%d,%d)" % color
    with Image(width=width, height=height, background=Color(color_str)) as img:
        img.format = "jpeg"
        return img.make_blob()


# --------------------------------------------------------------------- helper


class TestRenderKoboPreviewDataUrl:
    """The new helper: takes JPEG bytes + settings, returns a base64 data URL."""

    def test_returns_data_url_prefixed_with_image_jpeg(self):
        cover_padding = _load_cover_padding()
        if not hasattr(cover_padding, "render_kobo_preview_data_url"):
            pytest.fail(
                "cps.services.cover_padding.render_kobo_preview_data_url is not implemented yet"
            )

        jpeg = _make_jpeg_bytes()
        url = cover_padding.render_kobo_preview_data_url(
            jpeg,
            aspect="kobo_libra_color",
            fill_mode="edge_mirror",
            color="",
        )
        assert isinstance(url, str)
        assert url.startswith("data:image/jpeg;base64,")

    def test_decoded_payload_is_jpeg_magic_bytes(self):
        cover_padding = _load_cover_padding()
        if not hasattr(cover_padding, "render_kobo_preview_data_url"):
            pytest.fail("helper not implemented")

        jpeg = _make_jpeg_bytes()
        url = cover_padding.render_kobo_preview_data_url(
            jpeg, aspect="kobo_libra_color", fill_mode="edge_mirror", color=""
        )
        b64 = url.split(",", 1)[1]
        decoded = base64.b64decode(b64)
        # JPEG SOI marker
        assert decoded[:2] == b"\xff\xd8"

    def test_padded_output_has_target_aspect_ratio(self):
        cover_padding = _load_cover_padding()
        if not hasattr(cover_padding, "render_kobo_preview_data_url"):
            pytest.fail("helper not implemented")

        try:
            from wand.image import Image
        except ImportError:
            pytest.skip("Wand not available")

        # 2:3 publisher cover (200x300) padded to Libra Color 1264x1680 (3:4ish)
        jpeg = _make_jpeg_bytes(width=200, height=300)
        url = cover_padding.render_kobo_preview_data_url(
            jpeg, aspect="kobo_libra_color", fill_mode="edge_mirror", color=""
        )
        decoded = base64.b64decode(url.split(",", 1)[1])
        with Image(blob=decoded) as out:
            ratio = out.width / out.height
        # Libra Color = 1264/1680 = 0.7524; 2:3 = 0.6667. Padded should
        # land near 0.7524 (within rounding).
        assert abs(ratio - (1264 / 1680)) < 0.01

    def test_manual_color_is_honored(self):
        cover_padding = _load_cover_padding()
        if not hasattr(cover_padding, "render_kobo_preview_data_url"):
            pytest.fail("helper not implemented")

        jpeg = _make_jpeg_bytes(width=200, height=300, color=(255, 255, 255))
        url = cover_padding.render_kobo_preview_data_url(
            jpeg,
            aspect="kobo_libra_color",
            fill_mode="manual",
            color="#cc7b19",
        )
        # Just verify it round-trips without error and returns a JPEG.
        assert url.startswith("data:image/jpeg;base64,")
        decoded = base64.b64decode(url.split(",", 1)[1])
        assert decoded[:2] == b"\xff\xd8"

    def test_gradient_mode_renders_a_jpeg(self):
        # New "gradient" fill mode (operator-requested 2026-05-08): builds a
        # palette-matched top→bottom gradient on the pad area. Must round-trip
        # to a valid JPEG.
        cover_padding = _load_cover_padding()
        if not hasattr(cover_padding, "render_kobo_preview_data_url"):
            pytest.fail("helper not implemented")
        if "gradient" not in cover_padding.FILL_MODES:
            pytest.fail("'gradient' must be a registered fill mode")

        jpeg = _make_jpeg_bytes(width=200, height=300)
        url = cover_padding.render_kobo_preview_data_url(
            jpeg, aspect="kobo_libra_color", fill_mode="gradient", color="",
        )
        assert url.startswith("data:image/jpeg;base64,")
        decoded = base64.b64decode(url.split(",", 1)[1])
        assert decoded[:2] == b"\xff\xd8"

    def test_gradient_mode_produces_target_aspect(self):
        cover_padding = _load_cover_padding()
        if not hasattr(cover_padding, "render_kobo_preview_data_url"):
            pytest.fail("helper not implemented")
        try:
            from wand.image import Image
        except ImportError:
            pytest.skip("Wand not available")

        jpeg = _make_jpeg_bytes(width=200, height=300)
        url = cover_padding.render_kobo_preview_data_url(
            jpeg, aspect="kobo_libra_color", fill_mode="gradient", color="",
        )
        decoded = base64.b64decode(url.split(",", 1)[1])
        with Image(blob=decoded) as out:
            ratio = out.width / out.height
        assert abs(ratio - (1264 / 1680)) < 0.01

    def test_invalid_aspect_falls_back_to_default_or_raises_clear_error(self):
        cover_padding = _load_cover_padding()
        if not hasattr(cover_padding, "render_kobo_preview_data_url"):
            pytest.fail("helper not implemented")

        jpeg = _make_jpeg_bytes()
        # parse_target_ratio is documented to fall back to default for
        # bad input, so this should NOT raise — but the output should
        # still be a valid JPEG.
        url = cover_padding.render_kobo_preview_data_url(
            jpeg,
            aspect="not-a-real-aspect",
            fill_mode="edge_mirror",
            color="",
        )
        assert url.startswith("data:image/jpeg;base64,")


# --------------------------------------------------------------------- endpoint structure


class TestKoboPreviewEndpointStructure:
    """Static-source checks that the Flask endpoint exists with the
    right shape. Exercising the route itself requires full app init,
    which the smoke layer doesn't take on; the e2e Playwright pass
    covers the wired-up flow."""

    BLUEPRINT_FILE = REPO_ROOT / "cps" / "cover_picker.py"

    def _read(self) -> str:
        return self.BLUEPRINT_FILE.read_text(encoding="utf-8")

    def test_endpoint_route_is_registered(self):
        src = self._read()
        assert (
            '@cover_picker.route("/book/<int:book_id>/cover/kobo-preview"' in src
            or "'/book/<int:book_id>/cover/kobo-preview'" in src
        ), "kobo-preview route missing from cover_picker blueprint"

    def test_endpoint_uses_post_method(self):
        src = self._read()
        # The route decorator should declare methods=["POST"]
        assert (
            'methods=["POST"]' in src or "methods=['POST']" in src
        ), "POST method should be declared"
        # And the kobo-preview route should be near a POST decorator
        kobo_idx = src.find("/book/<int:book_id>/cover/kobo-preview")
        assert kobo_idx != -1
        # Look back ~80 chars for a methods=POST marker on the same decorator
        window = src[max(0, kobo_idx - 200):kobo_idx + 200]
        assert "POST" in window, "kobo-preview route is not declared POST"

    def test_endpoint_function_name_matches_convention(self):
        src = self._read()
        assert "def cover_picker_kobo_preview(" in src, (
            "endpoint handler should be named cover_picker_kobo_preview "
            "to match the existing cover_picker_* naming pattern"
        )

    def test_endpoint_is_login_and_edit_gated(self):
        src = self._read()
        kobo_idx = src.find("def cover_picker_kobo_preview")
        assert kobo_idx != -1
        # Decorators sit above the function — look at the 400 chars before
        prefix = src[max(0, kobo_idx - 400):kobo_idx]
        assert "@user_login_required" in prefix, "endpoint must be auth-gated"
        assert "@edit_required" in prefix, "endpoint must be edit-gated"

    def test_endpoint_invokes_pad_blob_or_helper(self):
        src = self._read()
        # Must reach the padding pipeline, either via the helper we add
        # or pad_blob directly.
        assert (
            "render_kobo_preview_data_url" in src or "pad_blob" in src
        ), "endpoint must route through cover_padding"

    def test_endpoint_rejects_non_http_scheme(self):
        # Defense-in-depth: cw_advocate is the SSRF guard, but we don't
        # actively maintain it. Reject file:// / javascript: / data: / etc.
        # at the endpoint before they ever hit the fetch path.
        src = self._read()
        kobo_idx = src.find("def cover_picker_kobo_preview")
        block = src[kobo_idx:kobo_idx + 2500]
        assert (
            'scheme not in ("http", "https")' in block
            or "scheme not in ('http', 'https')" in block
        ), "endpoint must reject non-http(s) schemes server-side"
        assert "bad_scheme" in block, "should return a structured error code"

    def test_fetch_url_bytes_pre_streams_content_length(self):
        # Stop a 1 GB advertised-Content-Length attacker before we waste
        # a worker iterating chunks.
        src = self._read()
        helper_idx = src.find("def _fetch_url_bytes")
        block = src[helper_idx:helper_idx + 2500]
        assert (
            'resp.headers.get("Content-Length")' in block
            or "resp.headers.get('Content-Length')" in block
        ), "fetch helper must check Content-Length before streaming"
        assert "max_bytes" in block

    def test_picker_page_passes_config_to_template(self):
        # Regression caught during browser e2e: the picker GET handler must
        # explicitly pass config=config so cover_picker.html's
        # `{% if config.config_kobo_cover_padding_enabled %}` resolves. Without
        # that pass, Jinja silently sees `config` as Undefined and the Kobo
        # panel never renders even when the admin flag is on.
        src = self._read()
        page_idx = src.find("def cover_picker_page(")
        assert page_idx != -1
        render_idx = src.find("render_title_template(", page_idx)
        assert render_idx != -1, "picker page must call render_title_template"
        block = src[render_idx:render_idx + 800]
        assert (
            "config=config" in block
        ), "cover_picker_page must pass config=config (regression: silent panel-missing)"


# --------------------------------------------------------------------- JS module surface


class TestCoverPickerJsKoboPreview:
    """Smoke checks that the JS module wires up the toggle, debounces the
    candidate-grid MutationObserver, and (regression) refreshes previews
    immediately when aspect / fill_mode / color change while toggle is on."""

    JS_FILE = REPO_ROOT / "cps" / "static" / "js" / "cover_picker.js"

    def _read(self) -> str:
        return self.JS_FILE.read_text(encoding="utf-8")

    def test_js_has_kobo_setup_block(self):
        assert "setupKoboPreview" in self._read()

    def test_js_caches_per_settings(self):
        # Settings-change refresh must hit cache for previously-fetched
        # combinations rather than re-firing a server burst on each toggle.
        src = self._read()
        assert "settingsKey" in src, "must compute a per-settings cache key"
        assert "perImg.has(key)" in src, "must check cache before fetching"

    def test_js_debounces_mutation_observer(self):
        # MutationObserver fires once per appended candidate card. We need
        # to coalesce a render burst into one refresh, not N parallel fetches.
        src = self._read()
        assert (
            "obsTimer" in src or "MutationObserver" in src and "setTimeout" in src
        ), "must debounce the grid MutationObserver"

    def test_js_handles_same_origin_cover_url(self):
        # cw_advocate refuses to fetch the host's own URL (SSRF guard). Same-
        # origin /cover/<id>/... must short-circuit to the server's disk-cover
        # path instead.
        src = self._read()
        assert (
            "isSameOriginCoverUrl" in src
        ), "must detect same-origin /cover/ URLs"

    def test_js_uses_abort_controller(self):
        # Toggle off / settings change must abort in-flight server work.
        # Otherwise the user pays for Wand cycles they no longer want.
        src = self._read()
        assert "AbortController" in src, "must wire up an AbortController"
        assert "abort()" in src, "must actually call abort somewhere"
        assert "signal" in src, "fetch must be passed the signal"

    def test_js_renders_loading_status(self):
        # User-visible feedback while a burst is processing — no more
        # 5 seconds of "did the toggle even work?"
        src = self._read()
        assert "cwa-cover-picker-kobo-status" in src, "must read the status pill element"
        assert "activeInFlight" in src, "must track the active in-flight count"

    def test_js_refresh_on_aspect_change(self):
        # Regression (operator-reported, 2026-05-08): switching the Target
        # Aspect Ratio dropdown with the toggle on must refire previews. The
        # change handler must call refreshAll (or trigger a refresh) — not
        # just sit on a cached older response.
        src = self._read()
        # Find the line that wires aspectSel change.
        idx = src.find('aspectSel.addEventListener("change"')
        assert idx != -1, "aspect dropdown must have a change listener"
        block = src[idx:idx + 200]
        assert (
            "refreshAll" in block or "refresh" in block.lower()
        ), "aspect change must drive a refresh"

    def test_js_refresh_on_fill_mode_change(self):
        # Regression: switching Border fill style must refire previews.
        src = self._read()
        # syncColorEnabled is one bound listener; the refresh wiring is the
        # other. Both must exist on fillSel change.
        # Count fillSel.addEventListener("change", ...) occurrences.
        occurrences = src.count('fillSel.addEventListener("change"')
        assert occurrences >= 2, (
            "fillSel must have at least two change listeners "
            "(syncColorEnabled + refresh-trigger)"
        )

    def test_js_refresh_on_color_input(self):
        # Regression: typing in the manual hex color input must drive a
        # debounced refresh while fill_mode == manual.
        src = self._read()
        idx = src.find('colorInput.addEventListener("input"')
        assert idx != -1, "manual color input must have an input listener"
        block = src[idx:idx + 400]
        assert (
            "refreshAll" in block
        ), "color input must drive a refresh"

    def test_js_caps_client_side_concurrency(self):
        # Without a client cap, ~36 candidate covers fire simultaneous
        # fetches on toggle-on. Each fetch holds a gunicorn worker through
        # the server-side BoundedSemaphore(4) wait, starving login / static
        # routes until the burst drains.
        src = self._read()
        assert (
            "KOBO_MAX_CONCURRENT" in src
        ), "must declare a client-side max-concurrency cap"

    def test_js_uses_generation_for_race_safety(self):
        # The bug was: aborted fetches' .finally decremented a shared
        # inFlightCount that belonged to the NEW burst, corrupting state.
        # The fix is generation-tagged completion handlers — each fetch
        # checks `generation === myGen` before mutating shared UI state.
        src = self._read()
        assert "generation" in src, "must use a generation counter for race safety"
        assert "myGen" in src, "fetches must close over their own generation"
        assert (
            "generation === myGen" in src or "generation == myGen" in src
        ), "completion handlers must gate on generation match"


class TestCoverPaddingConcurrencyCap:
    """Server-side semaphore around pad_blob so a single user's burst
    can't starve all gunicorn workers on a multi-user instance."""

    PAD_FILE = REPO_ROOT / "cps" / "services" / "cover_padding.py"

    def _read(self) -> str:
        return self.PAD_FILE.read_text(encoding="utf-8")

    def test_module_declares_bounded_semaphore(self):
        src = self._read()
        assert (
            "BoundedSemaphore" in src or "Semaphore(" in src
        ), "cover_padding must declare a concurrency-cap semaphore"

    def test_render_helper_acquires_the_semaphore(self):
        src = self._read()
        helper_idx = src.find("def render_kobo_preview_data_url")
        block = src[helper_idx:helper_idx + 1500]
        assert (
            "_PREVIEW_SEMAPHORE" in block or "with " in block and "Semaphore" in src
        ), "render_kobo_preview_data_url must hold the semaphore around pad_blob"


# --------------------------------------------------------------------- template surface


class TestCoverPickerTemplateSurface:
    """Static checks that the new picker UI panel is wired into the template."""

    TEMPLATE_FILE = REPO_ROOT / "cps" / "templates" / "cover_picker.html"

    def _read(self) -> str:
        return self.TEMPLATE_FILE.read_text(encoding="utf-8")

    def test_template_gates_panel_on_kobo_padding_enabled(self):
        src = self._read()
        assert (
            "config.config_kobo_cover_padding_enabled" in src
        ), "Kobo preview panel must be conditional on config_kobo_cover_padding_enabled"

    def test_template_includes_kobo_preview_panel_id(self):
        src = self._read()
        assert (
            'id="cwa-cover-picker-kobo-panel"' in src
        ), "Kobo preview panel needs a stable ID for JS hookup"

    def test_template_includes_kobo_preview_toggle(self):
        src = self._read()
        assert (
            'id="cwa-cover-picker-kobo-enabled"' in src
        ), "Kobo preview toggle checkbox needs a stable ID"

    def test_template_includes_aspect_select(self):
        src = self._read()
        assert (
            'id="cwa-cover-picker-kobo-aspect"' in src
        ), "Kobo aspect select needs a stable ID"

    def test_template_includes_fill_mode_select(self):
        src = self._read()
        assert (
            'id="cwa-cover-picker-kobo-fill-mode"' in src
        ), "Kobo fill-mode select needs a stable ID"

    def test_template_includes_gradient_option(self):
        # The new gradient mode must be selectable in both the picker and
        # the admin Settings dropdowns. (Settings template covered by the
        # admin-side test below.)
        src = self._read()
        assert 'value="gradient"' in src, "picker fill-mode select needs a 'gradient' option"

    def test_template_includes_color_input(self):
        src = self._read()
        assert (
            'id="cwa-cover-picker-kobo-color"' in src
        ), "Kobo manual-color input needs a stable ID"

    def test_template_passes_kobo_preview_endpoint_to_js(self):
        src = self._read()
        assert (
            "cover_picker.cover_picker_kobo_preview" in src
        ), "the kobo-preview endpoint URL must be in window.cwaCoverPicker.endpoints"

    def test_template_includes_loading_status_pill(self):
        src = self._read()
        assert (
            'id="cwa-cover-picker-kobo-status"' in src
        ), "Kobo loading status pill needs a stable ID"
