# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for the cover-URL validator service."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_validator_module():
    """Idempotently top up the cps stub so this test co-exists with
    sibling service tests."""
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "cps" / "services" / "cover_url_validator.py"

    cps_pkg = sys.modules.get("cps")
    if cps_pkg is None:
        cps_pkg = types.ModuleType("cps")
        cps_pkg.__path__ = [str(repo_root / "cps")]
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

    advocate_mod = sys.modules.get("cps.cw_advocate") or types.ModuleType("cps.cw_advocate")
    if not hasattr(advocate_mod, "request"):
        advocate_mod.request = lambda *a, **k: None  # patched per-test
        advocate_mod.get = lambda *a, **k: None
    sys.modules["cps.cw_advocate"] = advocate_mod
    cps_pkg.cw_advocate = advocate_mod

    advocate_exc = sys.modules.get("cps.cw_advocate.exceptions")
    if advocate_exc is None or not hasattr(advocate_exc, "UnacceptableAddressException"):
        advocate_exc = types.ModuleType("cps.cw_advocate.exceptions")

        class UnacceptableAddressException(Exception):
            pass

        advocate_exc.UnacceptableAddressException = UnacceptableAddressException
        sys.modules["cps.cw_advocate.exceptions"] = advocate_exc
    advocate_mod.exceptions = advocate_exc

    if "cps.services" not in sys.modules:
        services_pkg = types.ModuleType("cps.services")
        services_pkg.__path__ = [str(repo_root / "cps" / "services")]
        sys.modules["cps.services"] = services_pkg

    spec = importlib.util.spec_from_file_location(
        "cps.services.cover_url_validator", module_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cps.services.cover_url_validator"] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator_module()


def _mock_head(status, content_type, content_length):
    return types.SimpleNamespace(
        status_code=status,
        headers={"content-type": content_type, "content-length": str(content_length)},
    )


@pytest.mark.unit
class TestEarlyRejects:
    def test_empty_url_rejected(self):
        result = validator.validate_cover_url("")
        assert not result.valid
        assert result.error_code == "empty"

    def test_whitespace_url_rejected(self):
        assert validator.validate_cover_url("   ").error_code == "empty"

    def test_non_http_scheme_rejected(self):
        result = validator.validate_cover_url("ftp://example.com/cover.jpg")
        assert not result.valid
        assert result.error_code == "bad_scheme"

    def test_javascript_scheme_rejected(self):
        result = validator.validate_cover_url("javascript:alert(1)")
        assert not result.valid
        assert result.error_code == "bad_scheme"


@pytest.mark.unit
class TestSsrfGuard:
    def test_ssrf_blocked_returns_friendly_error(self):
        with patch.object(
            validator.cw_advocate, "request",
            side_effect=validator.UnacceptableAddressException("blocked"),
        ):
            result = validator.validate_cover_url("http://localhost:8080/cover.jpg")
        assert not result.valid
        assert result.error_code == "ssrf_blocked"
        assert "internal" in result.error_message.lower() or "local" in result.error_message.lower()


@pytest.mark.unit
class TestUnreachable:
    def test_request_exception_returns_unreachable(self):
        import requests as _r
        with patch.object(validator.cw_advocate, "request",
                          side_effect=_r.ConnectionError("nope")):
            result = validator.validate_cover_url("https://nope.example/x.jpg")
        assert not result.valid
        assert result.error_code == "unreachable"


@pytest.mark.unit
class TestStatusAndContentType:
    def test_404_rejected(self):
        with patch.object(validator.cw_advocate, "request",
                          return_value=_mock_head(404, "text/html", 1234)):
            result = validator.validate_cover_url("https://example.com/missing.jpg")
        assert not result.valid
        assert result.error_code == "bad_status"

    def test_html_content_type_rejected(self):
        with patch.object(validator.cw_advocate, "request",
                          return_value=_mock_head(200, "text/html", 5000)):
            result = validator.validate_cover_url("https://example.com/page.html")
        assert not result.valid
        assert result.error_code == "not_image"

    def test_jpeg_content_type_passes_initial_check(self):
        with patch.object(validator.cw_advocate, "request",
                          return_value=_mock_head(200, "image/jpeg", 250000)), \
             patch.object(validator, "_probe_dimensions", return_value=(975, 1500)):
            result = validator.validate_cover_url("https://example.com/cover.jpg")
        assert result.valid
        assert result.content_type == "image/jpeg"
        assert result.width == 975
        assert result.height == 1500

    def test_content_type_with_charset_suffix_normalized(self):
        with patch.object(validator.cw_advocate, "request",
                          return_value=_mock_head(200, "image/png; charset=binary", 80000)), \
             patch.object(validator, "_probe_dimensions", return_value=(800, 1200)):
            result = validator.validate_cover_url("https://example.com/cover.png")
        assert result.valid
        assert result.content_type == "image/png"


@pytest.mark.unit
class TestSizeBounds:
    def test_too_small_rejected(self):
        # The 43-byte image/gif placeholder Amazon serves for unknown ASINs.
        with patch.object(validator.cw_advocate, "request",
                          return_value=_mock_head(200, "image/gif", 43)):
            result = validator.validate_cover_url("https://example.com/placeholder.gif")
        # Note: image/gif passes the content-type check, but the size filter
        # catches the placeholder.
        assert not result.valid
        assert result.error_code == "too_small"

    def test_too_large_rejected(self):
        # 200 MB JPEG — exceeds the 15 MB default.
        with patch.object(validator.cw_advocate, "request",
                          return_value=_mock_head(200, "image/jpeg", 200 * 1024 * 1024)):
            result = validator.validate_cover_url("https://example.com/huge.jpg")
        assert not result.valid
        assert result.error_code == "too_large"

    def test_missing_content_length_passes_size_check(self):
        # Some CDNs don't report content-length on HEAD. Don't reject for that;
        # the actual save-path will enforce the cap.
        with patch.object(validator.cw_advocate, "request",
                          return_value=_mock_head(200, "image/jpeg", 0)), \
             patch.object(validator, "_probe_dimensions", return_value=(900, 1200)):
            result = validator.validate_cover_url("https://example.com/cover.jpg")
        assert result.valid


@pytest.mark.unit
class TestSerialization:
    def test_to_dict_returns_jsonable_shape(self):
        with patch.object(validator.cw_advocate, "request",
                          return_value=_mock_head(200, "image/jpeg", 250000)), \
             patch.object(validator, "_probe_dimensions", return_value=(975, 1500)):
            result = validator.validate_cover_url("https://example.com/cover.jpg")
        d = result.to_dict()
        assert d["valid"] is True
        assert d["width"] == 975
        assert d["height"] == 1500
        assert d["content_type"] == "image/jpeg"
        assert d["size_bytes"] == 250000
