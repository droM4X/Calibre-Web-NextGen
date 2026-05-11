# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pin the admin Version Information table's "update available" indicator
(fork issue #125 follow-up).

PR #136 fixed the "Check for Update" button's Python flow, but Docker
builds set ``feature_support['updater'] = False`` (constants.UPDATER_AVAILABLE
is False in containers because the updater can't replace files at runtime
inside an image). The button never renders for Docker users — i.e. the
operator's deployment + every fork user pulling
``ghcr.io/new-usemame/calibre-web-nextgen``. Those users had no way to learn
from the admin UI that a newer release existed.

The s6-init bootstrap probe (PR #28, v4.0.8) already writes the latest fork
tag to ``/app/CWA_STABLE_RELEASE`` at container boot, and
``cwa_update_available()`` already compares installed vs stable. We just
need to surface that comparison next to the installed version in the admin
Version Information table.

These tests pin:

1. ``cwa_get_update_indicator()`` returns ``(True, "v4.0.46")`` when stable
   is strictly newer than installed.
2. It returns ``(False, ...)`` when installed equals or exceeds stable.
3. It returns ``(False, "")`` gracefully on any exception from
   ``cwa_update_available`` (never breaks the admin page render).
4. The admin route passes the indicator into the template under the names
   ``cwa_is_outdated`` and ``cwa_latest_tag``.
5. The template renders "Update available: <tag>" with the docker-pull
   command when outdated, otherwise the original "Current Version" label.
"""

import inspect

import pytest


@pytest.mark.unit
class TestUpdateIndicatorHelper:
    def test_returns_true_when_stable_is_newer(self, mocker):
        from cps import admin
        mocker.patch(
            "cps.render_template.cwa_update_available",
            return_value=(True, "v4.0.34", "v4.0.46"),
        )
        is_outdated, latest = admin.cwa_get_update_indicator()
        assert is_outdated is True
        assert latest == "v4.0.46"

    def test_returns_false_when_installed_equals_stable(self, mocker):
        from cps import admin
        mocker.patch(
            "cps.render_template.cwa_update_available",
            return_value=(False, "v4.0.46", "v4.0.46"),
        )
        is_outdated, latest = admin.cwa_get_update_indicator()
        assert is_outdated is False

    def test_swallow_exceptions_returns_safe_default(self, mocker):
        """The admin page must never fail to render because of a version
        probe glitch (network, missing file, parsing error, etc.). The
        indicator helper has to return a clean default on any throw."""
        from cps import admin
        mocker.patch(
            "cps.render_template.cwa_update_available",
            side_effect=RuntimeError("boom"),
        )
        is_outdated, latest = admin.cwa_get_update_indicator()
        assert is_outdated is False
        assert latest == ""


@pytest.mark.unit
class TestAdminRoutePassesIndicator:
    def test_admin_route_source_passes_indicator_kwargs(self):
        """Source-pin: ``admin()`` must include cwa_is_outdated and
        cwa_latest_tag in the render_title_template kwargs. A refactor
        that drops these would silently regress @SpookyUSAF's symptom."""
        from cps import admin
        src = inspect.getsource(admin.admin)
        assert "cwa_get_update_indicator()" in src, (
            "admin() must call cwa_get_update_indicator() to populate the "
            "outdated flag for the Version Information table"
        )
        assert "cwa_is_outdated=" in src
        assert "cwa_latest_tag=" in src


@pytest.mark.unit
class TestAdminTemplateRendersIndicator:
    """Template-source pin: when outdated, the admin.html Version
    Information row renders 'Update available' with the latest tag and a
    docker-pull command; otherwise the original 'Current Version' label.
    We assert against the template source rather than full Jinja rendering
    so this test stays independent of the Flask app context."""

    def test_template_has_outdated_branch(self):
        import os
        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        admin_html = os.path.join(repo_root, "cps", "templates", "admin.html")
        with open(admin_html, encoding="utf-8") as fh:
            template = fh.read()
        # The new branch must reference both variables.
        assert "cwa_is_outdated" in template
        assert "cwa_latest_tag" in template
        # And it must surface the docker-pull command in the user-visible
        # message so the user knows what to do next.
        assert "docker pull ghcr.io/new-usemame/calibre-web-nextgen" in template

    def test_template_preserves_current_version_fallback(self):
        import os
        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        admin_html = os.path.join(repo_root, "cps", "templates", "admin.html")
        with open(admin_html, encoding="utf-8") as fh:
            template = fh.read()
        # The pre-existing label must remain in the else-branch so up-to-date
        # installs see the original wording.
        assert "{{_('Current Version')}}" in template
