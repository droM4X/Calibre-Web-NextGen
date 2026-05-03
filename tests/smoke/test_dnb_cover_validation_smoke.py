# -*- coding: utf-8 -*-
"""Regression tests for the DNB cover-validation strategy refactor.

Pre-fix `_get_validated_cover_url` ran a synchronous 10-second
`requests.get(cover_url)` per ISBN, racking up ~100s of latency per
search and ERROR-level log spam from `portal.dnb.de`'s TLS-reset
behaviour for missing-cover ISBNs.

Post-fix, the default mode is "skip": always return the URL, let the
modal's <img onerror> handle missing covers. "head" and "get" modes are
preserved for ops who want strict server-side validation, both with
short timeouts, an LRU cache, and DEBUG-level logging on transport-class
failures.
"""

import importlib
import sys
from unittest.mock import patch

import pytest

import cps.metadata_provider.dnb as dnb_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_dnb_with_env(env_value):
    """Reimport the dnb module so module-level `_COVER_VALIDATION_MODE` is
    re-resolved against the environment we control here."""
    monkey_env = {"CWA_DNB_COVER_VALIDATION": env_value} if env_value is not None else {}
    with patch.dict("os.environ", monkey_env, clear=False):
        if env_value is None:
            # We need to remove it if previously set
            import os as _os
            _os.environ.pop("CWA_DNB_COVER_VALIDATION", None)
        importlib.reload(dnb_module)


# Preserve module-level state so subsequent tests see a known config
@pytest.fixture(autouse=True)
def _restore_default_mode():
    yield
    _reload_dnb_with_env("skip")
    dnb_module._probe_dnb_cover.cache_clear()


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------

def test_default_cover_mode_is_skip():
    import os
    os.environ.pop("CWA_DNB_COVER_VALIDATION", None)
    importlib.reload(dnb_module)
    assert dnb_module._COVER_VALIDATION_MODE == "skip"


@pytest.mark.parametrize("env,expected", [
    ("skip", "skip"),
    ("head", "head"),
    ("get",  "get"),
    ("HEAD", "head"),       # case-insensitive
    ("  get ", "get"),      # whitespace-tolerant
])
def test_cover_mode_env_resolution(env, expected):
    _reload_dnb_with_env(env)
    assert dnb_module._COVER_VALIDATION_MODE == expected


def test_cover_mode_unknown_value_falls_back_to_skip(caplog):
    _reload_dnb_with_env("nonsense")
    assert dnb_module._COVER_VALIDATION_MODE == "skip"


# ---------------------------------------------------------------------------
# Skip mode — cover URL returned unconditionally, no HTTP probe.
# ---------------------------------------------------------------------------

def test_skip_mode_never_calls_http(monkeypatch):
    _reload_dnb_with_env("skip")
    # Replace requests.get / requests.head with a sentinel that asserts
    # they were not called.
    def _fail(*_a, **_k):
        raise AssertionError("HTTP must not be called in skip mode")
    monkeypatch.setattr(dnb_module.requests, "get",  _fail)
    monkeypatch.setattr(dnb_module.requests, "head", _fail)

    instance = dnb_module.DNB.__new__(dnb_module.DNB)  # don't run __init__
    out = instance._get_validated_cover_url(
        {"isbn": "9783141274837"}, generic_cover="GENERIC")
    assert out == "https://portal.dnb.de/opac/mvb/cover?isbn=9783141274837"


def test_skip_mode_no_isbn_returns_generic(monkeypatch):
    _reload_dnb_with_env("skip")
    monkeypatch.setattr(dnb_module.requests, "get",  lambda *a, **k: pytest.fail("unreachable"))
    instance = dnb_module.DNB.__new__(dnb_module.DNB)
    assert instance._get_validated_cover_url({"isbn": ""}, "GENERIC") == "GENERIC"
    assert instance._get_validated_cover_url({}, "GENERIC")            == "GENERIC"


# ---------------------------------------------------------------------------
# Head mode — fast probe, treats SSL/Connection errors as "no cover".
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, content_type="image/jpeg", content=b""):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.content = content
    def raise_for_status(self):
        if self.status_code >= 400:
            raise dnb_module.requests.exceptions.HTTPError(
                response=self,
            )


def test_head_mode_returns_url_on_2xx(monkeypatch):
    _reload_dnb_with_env("head")
    dnb_module._probe_dnb_cover.cache_clear()
    monkeypatch.setattr(dnb_module.requests, "head",
                        lambda url, **k: _FakeResponse(200))
    instance = dnb_module.DNB.__new__(dnb_module.DNB)
    out = instance._get_validated_cover_url({"isbn": "9780451526342"}, "GENERIC")
    assert out == "https://portal.dnb.de/opac/mvb/cover?isbn=9780451526342"


def test_head_mode_returns_generic_on_404(monkeypatch):
    _reload_dnb_with_env("head")
    dnb_module._probe_dnb_cover.cache_clear()
    monkeypatch.setattr(dnb_module.requests, "head",
                        lambda url, **k: _FakeResponse(404))
    instance = dnb_module.DNB.__new__(dnb_module.DNB)
    assert instance._get_validated_cover_url({"isbn": "9783141274837"}, "GENERIC") == "GENERIC"


def test_head_mode_swallows_ssl_error(monkeypatch):
    """SSL EOF / connection-reset is the *exact* failure mode we observed
    on portal.dnb.de — must be swallowed silently, not raised, not logged
    at ERROR."""
    _reload_dnb_with_env("head")
    dnb_module._probe_dnb_cover.cache_clear()
    def _raise_ssl(*a, **k):
        raise dnb_module.requests.exceptions.SSLError("EOF in violation of protocol")
    monkeypatch.setattr(dnb_module.requests, "head", _raise_ssl)
    instance = dnb_module.DNB.__new__(dnb_module.DNB)
    assert instance._get_validated_cover_url({"isbn": "9783141274837"}, "GENERIC") == "GENERIC"


def test_head_mode_swallows_connection_reset(monkeypatch):
    _reload_dnb_with_env("head")
    dnb_module._probe_dnb_cover.cache_clear()
    def _raise_conn(*a, **k):
        raise dnb_module.requests.exceptions.ConnectionError("reset by peer")
    monkeypatch.setattr(dnb_module.requests, "head", _raise_conn)
    instance = dnb_module.DNB.__new__(dnb_module.DNB)
    assert instance._get_validated_cover_url({"isbn": "9783141274837"}, "GENERIC") == "GENERIC"


def test_head_mode_swallows_timeout(monkeypatch):
    _reload_dnb_with_env("head")
    dnb_module._probe_dnb_cover.cache_clear()
    def _raise_timeout(*a, **k):
        raise dnb_module.requests.exceptions.Timeout("read timeout")
    monkeypatch.setattr(dnb_module.requests, "head", _raise_timeout)
    instance = dnb_module.DNB.__new__(dnb_module.DNB)
    assert instance._get_validated_cover_url({"isbn": "9783141274837"}, "GENERIC") == "GENERIC"


# ---------------------------------------------------------------------------
# Caching — repeated calls for the same ISBN must NOT re-hit HTTP.
# ---------------------------------------------------------------------------

def test_probe_is_cached_per_isbn(monkeypatch):
    _reload_dnb_with_env("head")
    dnb_module._probe_dnb_cover.cache_clear()
    calls = {"n": 0}
    def _counting_head(url, **k):
        calls["n"] += 1
        return _FakeResponse(200)
    monkeypatch.setattr(dnb_module.requests, "head", _counting_head)
    instance = dnb_module.DNB.__new__(dnb_module.DNB)
    isbn_record = {"isbn": "9780451526342"}
    instance._get_validated_cover_url(isbn_record, "GEN")
    instance._get_validated_cover_url(isbn_record, "GEN")
    instance._get_validated_cover_url(isbn_record, "GEN")
    assert calls["n"] == 1, "lru_cache should have collapsed 3 calls into 1 HTTP probe"
