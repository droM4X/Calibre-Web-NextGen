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
class TestHandleStateRequestUsesDeviceLastModified:
    """The popup-loop root cause is timestamp drift between the
    server's PT and what the device last saw. Janeczku PR #3601
    (commit 2d4ca23d) tried echoing PT/LM back in the PUT response,
    but devices don't persist timestamps from PUT responses -- the
    auto-sleep variant of the popup persisted. The real fix
    (janeczku PR #3607) is to use the device's own LastModified for
    both PT and LM, mirroring what official Kobo cloud does so the
    next GET returns a timestamp the device already knows.

    Pinned via source inspection because HandleStateRequest needs
    Flask request/current_user context not available at unit scope.
    """

    def test_reads_last_modified_from_request_body(self):
        from cps.kobo import HandleStateRequest
        src = inspect.getsource(HandleStateRequest)
        assert 'request_reading_state.get("LastModified")' in src, (
            "PUT handler must read LastModified from the request "
            "body so the saved timestamps match what the device "
            "expects to see back. See janeczku/calibre-web#3607."
        )

    def test_threads_request_lm_into_flask_g(self):
        from cps.kobo import HandleStateRequest
        src = inspect.getsource(HandleStateRequest)
        assert "g.kobo_reading_state_lm = request_lm" in src, (
            "PUT handler must publish request_lm via Flask g so the "
            "before_flush hook in cps/ub.py can apply the device's "
            "timestamp to the parent KoboReadingState row instead of "
            "stamping datetime.now(). See janeczku/calibre-web#3607."
        )

    def test_does_not_echo_lm_pt_in_put_response(self):
        """The previous attempt (commit 2d4ca23d / janeczku#3601)
        added LastModified + PriorityTimestamp to the PUT response.
        Devices don't persist them from PUT responses -- the data
        was dead code. PR #3607 reverts that. If a future refactor
        re-adds those keys, the popup-loop debugging cycle starts
        over. Pin the absence."""
        from cps.kobo import HandleStateRequest
        src = inspect.getsource(HandleStateRequest)
        assert 'update_results_response["PriorityTimestamp"]' not in src, (
            "PUT response must NOT include PriorityTimestamp -- "
            "Kobo devices ignore timestamps from PUT responses, the "
            "data is dead code, and including it leaves the door "
            "open to inconsistent server timestamps causing the "
            "popup. See janeczku/calibre-web#3607."
        )
        assert 'update_results_response["LastModified"]' not in src

    def test_priority_timestamp_column_has_no_onupdate(self):
        """The popup root cause: priority_timestamp had its own
        onupdate=datetime.now() that ran independently of last_modified.
        PT and LM could drift, and PT-newer-than-device's-cached-PT
        triggers the popup. Fix removes the onupdate; the before_flush
        hook now sets PT and LM together to the device's own LM."""
        from cps.ub import KoboReadingState
        col = KoboReadingState.__table__.columns["priority_timestamp"]
        assert col.onupdate is None, (
            "priority_timestamp must NOT have onupdate -- if it does, "
            "PT advances on every flush regardless of what the device "
            "expects, and the 'Sync to last page read' popup returns. "
            "See janeczku/calibre-web#3607."
        )

    def test_before_flush_uses_request_lm_when_available(self):
        from cps import ub
        src = inspect.getsource(ub.receive_before_flush)
        assert "g.kobo_reading_state_lm" in src, (
            "before_flush hook must check Flask g.kobo_reading_state_lm "
            "(set by HandleStateRequest from the device's PUT body) "
            "before falling back to datetime.now(). See "
            "janeczku/calibre-web#3607."
        )
        assert "change.kobo_reading_state.priority_timestamp = ts" in src, (
            "before_flush hook must explicitly set priority_timestamp "
            "(not just last_modified) so PT and LM stay in lock-step. "
            "See janeczku/calibre-web#3607."
        )
