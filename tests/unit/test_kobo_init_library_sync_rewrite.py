# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression test pinning janeczku/calibre-web#3585's sister fix:
``library_sync`` must be rewritten to point at the local Flask app in
``HandleInitRequest``. Without it, Kobo devices behind a reverse proxy
fetch sync from ``storeapi.kobo.com`` instead of our server, and no
books are delivered.

Upstream commit: a9713bd4 (Noé Sierra-Velasquez).

The ``HandleInitRequest`` function rewrites a handful of Kobo resource
URLs to point at the local server. ``image_host`` and the cover
templates were already rewritten; ``library_sync`` was left at its
``storeapi.kobo.com`` default. The fix adds the rewrite in both
branches (proxied vs unproxied request shape).

Source-inspection rather than live HTTP because ``HandleInitRequest``
needs a Flask request context, kobo auth headers, and config setup
that aren't available at unit scope. The point of this test is to
ensure the assignment lines aren't accidentally removed in a future
refactor of the resource-rewriting block.
"""

import inspect

import pytest


@pytest.mark.unit
class TestHandleInitLibrarySyncRewrite:
    def test_proxied_branch_rewrites_library_sync(self):
        from cps.kobo import HandleInitRequest
        src = inspect.getsource(HandleInitRequest)
        assert (
            'kobo_resources["library_sync"] = calibre_web_url + url_for(' in src
        ), (
            "Proxied branch of HandleInitRequest must rewrite "
            "library_sync to point at the local HandleSyncRequest. "
            "Without it Kobo devices behind a reverse proxy hit "
            "storeapi.kobo.com and no books sync. See "
            "janeczku/calibre-web a9713bd4."
        )

    def test_unproxied_branch_rewrites_library_sync(self):
        from cps.kobo import HandleInitRequest
        src = inspect.getsource(HandleInitRequest)
        assert 'kobo_resources["library_sync"] = url_for(' in src, (
            "Unproxied branch of HandleInitRequest must rewrite "
            "library_sync to a local _external=True URL. See "
            "janeczku/calibre-web a9713bd4."
        )

    def test_default_resource_table_still_has_storeapi_default(self):
        """The default resource table is intentionally seeded with the
        upstream Kobo URL -- this is the value that HandleInitRequest
        overrides. If the default disappears, the upstream Kobo store
        passthrough breaks for users who enable kobo_proxy."""
        from cps.kobo import NATIVE_KOBO_RESOURCES
        defaults = NATIVE_KOBO_RESOURCES()
        assert defaults.get("library_sync") == (
            "https://storeapi.kobo.com/v1/library/sync"
        )
