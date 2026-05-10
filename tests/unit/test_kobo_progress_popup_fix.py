# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Regression tests pinning the three janeczku/calibre-web Kobo
reading-state fixes that resolve the spurious "Sync to last page read"
popup loop. Without them, Kobo devices repeatedly prompt users to
re-sync their position and snap to a stale value, making it look like
progress was reset.

Upstream PRs:
- janeczku/calibre-web#3585 (commits d229f711, a4bf0285) -- float/int
  mismatch + truthy-check on ProgressPercent=0
- janeczku/calibre-web#3601 (commit 2d4ca23d) -- PUT /state response
  must echo PriorityTimestamp/LastModified
"""

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


@pytest.mark.unit
class TestCleanProgressHelper:
    """`_clean_progress` collapses whole-number floats to int so the
    JSON wire value matches what the Kobo device sent. Without this
    the device sees its own ``33`` echoed back as ``33.0`` and treats
    that as a divergent position."""

    def test_whole_number_float_becomes_int(self):
        from cps.kobo import _clean_progress
        assert _clean_progress(33.0) == 33
        assert isinstance(_clean_progress(33.0), int)

    def test_zero_float_becomes_int_zero(self):
        from cps.kobo import _clean_progress
        result = _clean_progress(0.0)
        assert result == 0
        assert isinstance(result, int)

    def test_fractional_float_preserved(self):
        from cps.kobo import _clean_progress
        result = _clean_progress(33.5)
        assert result == 33.5
        assert isinstance(result, float)

    def test_none_passes_through(self):
        from cps.kobo import _clean_progress
        assert _clean_progress(None) is None


def _make_bookmark(progress_percent=None,
                   content_source_progress_percent=None,
                   location_value=None,
                   location_type=None,
                   location_source=None,
                   last_modified=None):
    return SimpleNamespace(
        progress_percent=progress_percent,
        content_source_progress_percent=content_source_progress_percent,
        location_value=location_value,
        location_type=location_type,
        location_source=location_source,
        last_modified=last_modified or datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


@pytest.mark.unit
class TestCurrentBookmarkResponseShape:
    """`get_current_bookmark_response` is what the Kobo device reads on
    GET /v1/library/<uuid>/state and on the embedded ReadingState in
    sync responses. The response must always include ProgressPercent
    when a bookmark exists -- including when progress is exactly 0 --
    or the device interprets the omission as 'no synced position' and
    fires the 'Sync to last page read?' popup."""

    def test_progress_percent_present_when_zero(self):
        """Pre-fix: ``if current_bookmark.progress_percent:`` dropped
        the field for 0.0. Particularly bad for comics/manga where
        ProgressPercent stays 0 and ContentSourceProgressPercent
        carries the page-by-page progress."""
        from cps.kobo import get_current_bookmark_response
        resp = get_current_bookmark_response(
            _make_bookmark(progress_percent=0.0,
                           content_source_progress_percent=0.0)
        )
        assert "ProgressPercent" in resp
        assert resp["ProgressPercent"] == 0
        assert "ContentSourceProgressPercent" in resp
        assert resp["ContentSourceProgressPercent"] == 0

    def test_progress_percent_omitted_only_when_none(self):
        from cps.kobo import get_current_bookmark_response
        resp = get_current_bookmark_response(
            _make_bookmark(progress_percent=None,
                           content_source_progress_percent=None)
        )
        assert "ProgressPercent" not in resp
        assert "ContentSourceProgressPercent" not in resp

    def test_whole_number_progress_returned_as_int(self):
        """Pre-fix: SQLite Float column made 33 round-trip as 33.0,
        which the Kobo treats as a different position from its locally
        cached integer 33 and prompts the user."""
        from cps.kobo import get_current_bookmark_response
        resp = get_current_bookmark_response(
            _make_bookmark(progress_percent=33.0,
                           content_source_progress_percent=42.0)
        )
        assert resp["ProgressPercent"] == 33
        assert isinstance(resp["ProgressPercent"], int)
        assert resp["ContentSourceProgressPercent"] == 42
        assert isinstance(resp["ContentSourceProgressPercent"], int)

    def test_fractional_progress_preserved(self):
        from cps.kobo import get_current_bookmark_response
        resp = get_current_bookmark_response(
            _make_bookmark(progress_percent=33.5)
        )
        assert resp["ProgressPercent"] == 33.5
        assert isinstance(resp["ProgressPercent"], float)


@pytest.mark.unit
class TestHandleStateRequestPutResponse:
    """The PUT /v1/library/<uuid>/state handler must echo the freshly
    bumped LastModified + PriorityTimestamp back to the device.
    Without this the device's cached timestamps lag the server's, so
    the next GET returns 'newer' values the device hasn't seen yet
    and triggers the 'Return to last page read?' popup after sleep
    /wake. Pinned via source inspection because the function relies on
    Flask request/current_user context that isn't available at unit
    scope."""

    def test_priority_timestamp_added_to_put_response(self):
        from cps.kobo import HandleStateRequest
        src = inspect.getsource(HandleStateRequest)
        assert 'update_results_response["PriorityTimestamp"]' in src, (
            "PUT /state response must include PriorityTimestamp -- "
            "without it Kobo devices show spurious 'Return to last "
            "page read?' popup after auto-sleep/wake. See "
            "janeczku/calibre-web#3601."
        )

    def test_last_modified_added_to_put_response(self):
        from cps.kobo import HandleStateRequest
        src = inspect.getsource(HandleStateRequest)
        assert 'update_results_response["LastModified"]' in src, (
            "PUT /state response must include LastModified so the "
            "device's cached timestamp matches the server. See "
            "janeczku/calibre-web#3601."
        )
