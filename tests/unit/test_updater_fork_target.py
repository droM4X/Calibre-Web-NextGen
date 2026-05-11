# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pin the updater's release-check target + version parser (fork issue #125).

Background: PR #28 (v4.0.8) pointed the s6-init bootstrap probe at this
fork, but left ``cps/updater.py`` hardcoded to ``janeczku/calibre-web``.
The admin "Check for Update" button kept hitting upstream, so users on
the fork saw "no update available" forever. @SpookyUSAF reported this
on v4.0.34.

Also pins ``_normalize_tag`` so our ``vX.Y.Z`` tag format parses without
the ``ValueError: invalid literal for int()`` that the old `split('.')`
path would have raised on ``v4.0.45``.
"""

import importlib

import pytest


@pytest.mark.unit
class TestRepositoryTarget:
    def test_default_repo_is_new_usemame_fork(self, monkeypatch):
        monkeypatch.delenv("CWA_RELEASE_REPO", raising=False)
        # Reimport so the module re-reads the env at import time.
        from cps import updater
        importlib.reload(updater)
        assert updater._REPOSITORY_SLUG == "new-usemame/Calibre-Web-NextGen"
        assert updater._REPOSITORY_API_URL == (
            "https://api.github.com/repos/new-usemame/Calibre-Web-NextGen"
        )

    def test_env_override_honored(self, monkeypatch):
        monkeypatch.setenv("CWA_RELEASE_REPO", "someone/downstream-fork")
        from cps import updater
        importlib.reload(updater)
        assert updater._REPOSITORY_SLUG == "someone/downstream-fork"
        assert updater._REPOSITORY_API_URL.endswith(
            "/someone/downstream-fork"
        )

    def test_empty_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CWA_RELEASE_REPO", "   ")
        from cps import updater
        importlib.reload(updater)
        assert updater._REPOSITORY_SLUG == "new-usemame/Calibre-Web-NextGen"


@pytest.mark.unit
class TestNormalizeTag:
    """The pre-fix code did ``int(tag.split('.')[0])`` which raised
    ``ValueError`` on every fork tag (``v4.0.45``) — the check button
    would crash without comparing anything. _normalize_tag must accept
    both ``vX.Y.Z`` (fork) and ``X.Y.Z`` (upstream) and return None on
    unparseable input rather than raising."""

    def test_fork_v_prefix_parses(self):
        from cps.updater import _normalize_tag
        assert _normalize_tag("v4.0.45") == (4, 0, 45)

    def test_uppercase_v_prefix_parses(self):
        from cps.updater import _normalize_tag
        assert _normalize_tag("V1.2.3") == (1, 2, 3)

    def test_no_prefix_parses(self):
        from cps.updater import _normalize_tag
        assert _normalize_tag("4.0.45") == (4, 0, 45)

    def test_returns_none_for_non_semver(self):
        from cps.updater import _normalize_tag
        assert _normalize_tag("nightly-2026-05-10") is None
        assert _normalize_tag("v4.0") is None
        assert _normalize_tag("") is None
        assert _normalize_tag(None) is None

    def test_does_not_raise_on_garbage(self):
        from cps.updater import _normalize_tag
        # Used to be ``int('alpha')`` -> uncaught ValueError up the stack.
        assert _normalize_tag("alpha.beta.gamma") is None
        assert _normalize_tag("4.x.45") is None

    def test_whitespace_tolerated(self):
        from cps.updater import _normalize_tag
        assert _normalize_tag("  v4.0.45 \n") == (4, 0, 45)


@pytest.mark.unit
class TestVersionCompareIntegration:
    """Behavioral pin: ``_stable_available_updates`` must use the
    fork-aware normalizer rather than raise on its own installed
    version. We can't easily mock the full request-handler stack here
    so we source-pin the invariants that prevent regression."""

    def test_stable_check_uses_normalizer_for_current_version(self):
        import inspect
        from cps.updater import Updater
        src = inspect.getsource(Updater._stable_available_updates)
        # Pre-fix: ``status['current_commit_hash'].split('.')`` directly
        # → int conversion would explode on 'v4.0.45'.
        assert "_normalize_tag(version)" in src, (
            "current-version parsing must go through _normalize_tag "
            "so the fork's v-prefixed tags don't crash the updater"
        )

    def test_stable_check_uses_normalizer_for_remote_tags(self):
        import inspect
        from cps.updater import Updater
        src = inspect.getsource(Updater._stable_available_updates)
        assert "_normalize_tag(commit[i]['tag_name'])" in src, (
            "each remote release tag must also go through "
            "_normalize_tag so unparseable tags don't bring down the "
            "whole check"
        )

    def test_major_version_parser_normalizes(self):
        import inspect
        from cps.updater import Updater
        src = inspect.getsource(Updater._stable_updater_parse_major_version)
        # Pre-fix used raw int(.split('.')[1]) on the next commit's tag.
        assert "_normalize_tag(commit[i + 1]['tag_name'])" in src
