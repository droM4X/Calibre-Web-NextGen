# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web-NextGen contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Unit tests for the Apple Books metadata provider."""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _stub_cps_modules():
    """Set up just enough of the cps namespace for apple_books.py to import.

    The full package init has heavy side effects (CWA login, database
    bootstrap, etc.); we only need constants + logger + the Metadata base.

    Other tests in this directory may already have stubbed `cps` for their
    own minimal needs, so this helper top-ups any missing attributes
    rather than short-circuiting on first sight of `cps`.
    """
    cps_pkg = sys.modules.get("cps")
    if cps_pkg is None:
        cps_pkg = types.ModuleType("cps")
        cps_pkg.__path__ = [str(REPO_ROOT / "cps")]
        sys.modules["cps"] = cps_pkg

    constants = sys.modules.get("cps.constants") or types.ModuleType("cps.constants")
    if not hasattr(constants, "STATIC_DIR"):
        constants.STATIC_DIR = str(REPO_ROOT / "cps" / "static")
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

    if "cps.services.Metadata" not in sys.modules:
        metadata_path = REPO_ROOT / "cps" / "services" / "Metadata.py"
        spec = importlib.util.spec_from_file_location("cps.services.Metadata", metadata_path)
        metadata_module = importlib.util.module_from_spec(spec)
        sys.modules["cps.services.Metadata"] = metadata_module
        spec.loader.exec_module(metadata_module)

    if "cps.metadata_provider" not in sys.modules:
        provider_pkg = types.ModuleType("cps.metadata_provider")
        provider_pkg.__path__ = [str(REPO_ROOT / "cps" / "metadata_provider")]
        sys.modules["cps.metadata_provider"] = provider_pkg


def _load_apple_books_module():
    _stub_cps_modules()
    module_path = REPO_ROOT / "cps" / "metadata_provider" / "apple_books.py"
    spec = importlib.util.spec_from_file_location(
        "cps.metadata_provider.apple_books", module_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cps.metadata_provider.apple_books"] = module
    spec.loader.exec_module(module)
    return module


apple_books = _load_apple_books_module()


def _itunes_search_payload():
    """A real-shaped iTunes /search response (single ebook hit)."""
    return {
        "resultCount": 1,
        "results": [
            {
                "kind": "ebook",
                "trackId": 437002526,
                "trackName": "Wuthering Heights",
                "trackCensoredName": "Wuthering Heights",
                "artistName": "Emily Bronte",
                "trackViewUrl": "https://books.apple.com/us/book/wuthering-heights/id437002526?uo=4",
                "releaseDate": "2008-12-05T08:00:00Z",
                "genres": ["Classics", "Books", "Fiction & Literature"],
                "description": "<b>Rediscover Emily Bronte's powerful tale.</b>",
                "artworkUrl60": "https://is1-ssl.mzstatic.com/img/60x60bb.jpg",
                "artworkUrl100": "https://is1-ssl.mzstatic.com/img/100x100bb.jpg",
            }
        ],
    }


@pytest.mark.unit
class TestQueryShape:
    def test_isbn10_query_routes_to_lookup(self):
        provider = apple_books.AppleBooks()
        assert provider._looks_like_isbn("1853260010")
        assert not provider._looks_like_isbn("Wuthering Heights")

    def test_isbn13_with_separators_routes_to_lookup(self):
        provider = apple_books.AppleBooks()
        assert provider._looks_like_isbn("978-1-85326-001-8")
        assert provider._looks_like_isbn("9781853260018")

    def test_short_query_is_not_isbn(self):
        provider = apple_books.AppleBooks()
        assert not provider._looks_like_isbn("123")
        assert not provider._looks_like_isbn("")


@pytest.mark.unit
class TestParseItem:
    def test_parses_full_search_hit(self):
        provider = apple_books.AppleBooks()
        item = _itunes_search_payload()["results"][0]
        record = provider._parse_item(item, generic_cover="generic.svg")
        assert record is not None
        assert record.title == "Wuthering Heights"
        assert record.authors == ["Emily Bronte"]
        assert record.url.startswith("https://books.apple.com/us/book/")
        assert record.publishedDate == "2008-12-05"
        assert "Classics" in record.tags
        assert "Books" not in record.tags  # The redundant meta-tag is dropped.
        assert record.identifiers == {"apple": "437002526"}
        # Cover URL must be at the 1500 sweet spot so the booster's hi-res
        # skip-list matches and we don't waste cycles re-upgrading.
        assert "/1500x1500bb.jpg" in record.cover

    def test_skips_audiobook_when_we_asked_for_ebook(self):
        provider = apple_books.AppleBooks()
        item = {"kind": "song", "trackId": 1, "trackName": "X", "artistName": "Y"}
        assert provider._parse_item(item, generic_cover="g.svg") is None

    def test_falls_back_to_generic_cover_when_no_artwork(self):
        provider = apple_books.AppleBooks()
        item = {"kind": "ebook", "trackId": 1, "trackName": "X", "artistName": "Y"}
        record = provider._parse_item(item, generic_cover="generic.svg")
        assert record is not None
        assert record.cover == "generic.svg"

    def test_splits_multi_author_string(self):
        provider = apple_books.AppleBooks()
        item = {
            "kind": "ebook",
            "trackId": 99,
            "trackName": "Book",
            "artistName": "Alice & Bob, Carol and Dave",
        }
        record = provider._parse_item(item, generic_cover="g.svg")
        assert record is not None
        assert record.authors == ["Alice", "Bob", "Carol", "Dave"]


@pytest.mark.unit
class TestSearch:
    def test_isbn_query_calls_lookup_endpoint(self):
        provider = apple_books.AppleBooks()
        with patch.object(provider, "_call", return_value=_itunes_search_payload()["results"]) as call:
            records = provider.search("9781853260018")
        assert len(records) == 1
        assert records[0].title == "Wuthering Heights"
        # First positional arg is the URL.
        assert call.call_args.args[0] == apple_books.AppleBooks.LOOKUP_URL
        params = call.call_args.args[1]
        assert params["isbn"] == "9781853260018"

    def test_title_query_calls_search_endpoint(self):
        provider = apple_books.AppleBooks()
        with patch.object(provider, "_call", return_value=_itunes_search_payload()["results"]) as call:
            records = provider.search("Wuthering Heights")
        assert len(records) == 1
        assert call.call_args.args[0] == apple_books.AppleBooks.SEARCH_URL
        params = call.call_args.args[1]
        assert params["term"] == "Wuthering Heights"
        assert params["entity"] == "ebook"

    def test_inactive_provider_returns_empty(self):
        provider = apple_books.AppleBooks()
        provider.active = False
        with patch.object(provider, "_call") as call:
            assert provider.search("Wuthering Heights") == []
        call.assert_not_called()

    def test_empty_query_returns_empty(self):
        provider = apple_books.AppleBooks()
        with patch.object(provider, "_call") as call:
            assert provider.search("") == []
            assert provider.search("   ") == []
        call.assert_not_called()
