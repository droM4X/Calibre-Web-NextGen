# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression tests for SyncToken parser robustness.

Three independent bugs surfaced in the 2026-05-10 Kobo subsystem
audit:

1. data_schema_v1 was applied to the wrong object -- the wrapper
   sync_token_json instead of the inner data_json. Combined with the
   schema having no required fields, the validation was effectively
   a no-op. A malformed `data` payload would slip past validation
   and crash later when reading `raw_kobo_store_token`.
2. token_schema declared `version` and `data` properties but didn't
   require them. A token missing `version` raised KeyError on the
   version-comparison line, returning 500 to the device.
3. raw_kobo_store_token access was outside the try/except, so a
   malformed token after passing schema validation could still crash
   from_headers.

The fix tightens the validation chain so a malformed sync token
gracefully degrades to a fresh SyncToken (which triggers a full
resync) instead of 500'ing.
"""

import base64
import json

import pytest

from cps.services.SyncToken import SyncToken


def _encode(payload):
    return base64.b64encode(json.dumps(payload).encode()).decode("utf-8")


@pytest.mark.unit
class TestSyncTokenFromHeadersRobustness:
    def test_no_header_returns_default_token(self):
        token = SyncToken.from_headers({})
        assert isinstance(token, SyncToken)
        assert token.raw_kobo_store_token == ""

    def test_kobo_store_format_returns_passthrough_token(self):
        token = SyncToken.from_headers({SyncToken.SYNC_TOKEN_HEADER: "abc.def"})
        assert token.raw_kobo_store_token == "abc.def"

    def test_missing_version_field_returns_default_token(self):
        """token_schema must require `version` -- without it the
        version-comparison line at from_headers raised KeyError and
        the request 500'd."""
        encoded = _encode({"data": {"raw_kobo_store_token": "x"}})
        token = SyncToken.from_headers({SyncToken.SYNC_TOKEN_HEADER: encoded})
        assert isinstance(token, SyncToken)
        assert token.raw_kobo_store_token == ""

    def test_missing_data_field_returns_default_token(self):
        """token_schema must require `data` -- without it the
        data_json access raised KeyError."""
        encoded = _encode({"version": SyncToken.VERSION})
        token = SyncToken.from_headers({SyncToken.SYNC_TOKEN_HEADER: encoded})
        assert isinstance(token, SyncToken)

    def test_data_with_wrong_types_returns_default_token(self):
        """data_schema_v1 must validate data_json, not the wrapper.
        Pre-fix, a malformed inner data structure slipped past the
        validation and crashed at the raw_kobo_store_token access."""
        encoded = _encode({
            "version": SyncToken.VERSION,
            "data": {
                "raw_kobo_store_token": 12345,
                "books_last_modified": ["not", "a", "string"],
            },
        })
        token = SyncToken.from_headers({SyncToken.SYNC_TOKEN_HEADER: encoded})
        assert isinstance(token, SyncToken)
        assert token.raw_kobo_store_token == ""

    def test_data_missing_raw_kobo_store_token_returns_default_token(self):
        """Pre-fix, raw_kobo_store_token access was outside the
        try/except -- a token that passed schema validation but
        omitted that key would crash."""
        encoded = _encode({
            "version": SyncToken.VERSION,
            "data": {},
        })
        token = SyncToken.from_headers({SyncToken.SYNC_TOKEN_HEADER: encoded})
        assert isinstance(token, SyncToken)
        assert token.raw_kobo_store_token == ""

    def test_valid_token_round_trips(self):
        original = SyncToken(raw_kobo_store_token="original-token")
        from werkzeug.datastructures import Headers
        out = Headers()
        original.to_headers(out)
        recovered = SyncToken.from_headers(out)
        assert recovered.raw_kobo_store_token == "original-token"


@pytest.mark.unit
class TestSyncTokenSchema:
    def test_token_schema_requires_version_and_data(self):
        required = SyncToken.token_schema.get("required", [])
        assert "version" in required, (
            "token_schema must require `version` so missing it is "
            "caught by validation rather than crashing on the "
            "version-comparison line."
        )
        assert "data" in required, (
            "token_schema must require `data` so missing it is "
            "caught by validation rather than crashing on the "
            "data_json access."
        )
