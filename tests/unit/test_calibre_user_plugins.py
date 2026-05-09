# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Regression tests pinning the opt-in Calibre user-plugin loading
mechanism (closes upstream Calibre-Web-Automated #243).

The contract:

1. ``cps.services.calibre_user_plugins`` reads ``CWA_CALIBRE_USER_PLUGINS``
   from the env, accepts truthy values ``1`` / ``true`` / ``yes`` / ``on``
   case-and-whitespace-insensitively. Default is off.
2. ``apply_to_env(env)`` mutates the dict in place to set
   ``HOME=/config`` when enabled, leaves it untouched when disabled.
3. ``ensure_plugins_dir()`` creates ``/config/.config/calibre/plugins``
   when enabled (or returns the path if it already exists), returns
   ``None`` when disabled.
4. The four subprocess sites that build Calibre invocations
   (``scripts/ingest_processor.py``, ``scripts/convert_library.py``,
   ``scripts/cover_enforcer.py``, ``cps/embed_helper.py``) all delegate
   to ``apply_to_env`` rather than hardcoding ``HOME=/config``.

Pin (4) is a source-pin via AST/regex walk so any future refactor that
re-hardcodes ``HOME = "/config"`` on the calibre_env dict trips the test.
This is the regression vector — without it, a contributor "simplifying"
the helper indirection back into a literal would silently re-enable
plugin loading even when the operator has opted out.
"""

from __future__ import annotations

import os
import re
from importlib import reload
from pathlib import Path

import pytest

from cps.services import calibre_user_plugins


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def clean_env(monkeypatch):
    """Each test starts from a known-empty CWA_CALIBRE_USER_PLUGINS state."""
    monkeypatch.delenv("CWA_CALIBRE_USER_PLUGINS", raising=False)
    yield monkeypatch


@pytest.mark.unit
class TestCalibreUserPluginsHelper:
    """Behavioral tests on the helper module itself."""

    def test_default_is_disabled(self, clean_env):
        assert calibre_user_plugins.is_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "  yes  ", "on", "True"])
    def test_truthy_values_enable(self, clean_env, value):
        clean_env.setenv("CWA_CALIBRE_USER_PLUGINS", value)
        assert calibre_user_plugins.is_enabled() is True, (
            f"value {value!r} should enable per the standard truthy-set "
            f"convention (matches NETWORK_SHARE_MODE handling)"
        )

    @pytest.mark.parametrize(
        "value", ["", "0", "false", "FALSE", "no", "off", "anything-else", "  "]
    )
    def test_falsy_values_disable(self, clean_env, value):
        clean_env.setenv("CWA_CALIBRE_USER_PLUGINS", value)
        assert calibre_user_plugins.is_enabled() is False, (
            f"value {value!r} should not enable — only the documented "
            f"truthy-set values opt in"
        )

    def test_apply_to_env_when_enabled_sets_HOME(self, clean_env):
        clean_env.setenv("CWA_CALIBRE_USER_PLUGINS", "true")
        env = {"PATH": "/usr/bin", "HOME": "/home/abc"}
        result = calibre_user_plugins.apply_to_env(env)
        assert result["HOME"] == "/config"
        assert result is env, "must mutate in place + return the same dict"
        assert env["PATH"] == "/usr/bin", "must not touch other env vars"

    def test_apply_to_env_when_disabled_leaves_HOME_alone(self, clean_env):
        # No env var set
        env = {"PATH": "/usr/bin", "HOME": "/home/abc"}
        result = calibre_user_plugins.apply_to_env(env)
        assert result["HOME"] == "/home/abc", (
            "default-off must NOT touch HOME — otherwise opt-in becomes "
            "opt-out and existing deployments inherit plugin loading "
            "they didn't ask for"
        )

    def test_apply_to_env_when_disabled_doesnt_invent_HOME(self, clean_env):
        # No env var set, no HOME in input env
        env = {"PATH": "/usr/bin"}
        result = calibre_user_plugins.apply_to_env(env)
        assert "HOME" not in result, (
            "off-state must not silently introduce HOME — it would be "
            "indistinguishable from on-state at runtime"
        )

    def test_plugins_dir_path(self, clean_env):
        assert (
            str(calibre_user_plugins.plugins_dir())
            == "/config/.config/calibre/plugins"
        ), "Calibre's plugin lookup path under HOME=/config is fixed"

    def test_ensure_plugins_dir_disabled_returns_None(self, clean_env):
        assert calibre_user_plugins.ensure_plugins_dir() is None

    def test_ensure_plugins_dir_enabled_creates(self, clean_env, tmp_path, monkeypatch):
        clean_env.setenv("CWA_CALIBRE_USER_PLUGINS", "true")
        # Redirect _HOME to tmp so we don't touch /config in CI
        monkeypatch.setattr(calibre_user_plugins, "_HOME", str(tmp_path))
        result = calibre_user_plugins.ensure_plugins_dir()
        assert result is not None
        assert result.is_dir()
        assert str(result).endswith(".config/calibre/plugins")

    def test_ensure_plugins_dir_idempotent(self, clean_env, tmp_path, monkeypatch):
        clean_env.setenv("CWA_CALIBRE_USER_PLUGINS", "true")
        monkeypatch.setattr(calibre_user_plugins, "_HOME", str(tmp_path))
        first = calibre_user_plugins.ensure_plugins_dir()
        second = calibre_user_plugins.ensure_plugins_dir()
        assert first == second, "must be idempotent — boot is best-effort"

    def test_env_var_name_constant(self):
        assert calibre_user_plugins.env_var_name() == "CWA_CALIBRE_USER_PLUGINS"

    def test_home_path_constant(self):
        assert calibre_user_plugins.home_path() == "/config"


@pytest.mark.unit
class TestCallSiteWiringSourcePin:
    """Source-pin: every subprocess site that builds a Calibre env must
    delegate to ``calibre_user_plugins.apply_to_env`` rather than
    hardcoding ``HOME = "/config"``. A future "simplification" that
    re-introduces the literal would silently regress the opt-in to
    always-on. The list also catches removal — if the helper indirection
    gets stripped entirely, the test goes red."""

    SITES = [
        "scripts/ingest_processor.py",
        "scripts/convert_library.py",
        "scripts/cover_enforcer.py",
        "cps/embed_helper.py",
    ]

    @pytest.mark.parametrize("site", SITES)
    def test_no_hardcoded_home_config_on_calibre_env(self, site):
        """Catch the regression vector: a literal `something_env["HOME"]
        = "/config"` (or single-quoted equivalent) anywhere in the file
        on a calibre-env-shaped variable."""
        path = REPO_ROOT / site
        assert path.is_file(), f"missing source file: {site}"
        src = path.read_text()
        # Strip comments before scanning so the "old code" comment in the
        # commit message / docstring doesn't trip the check.
        src_no_comments = re.sub(r"#[^\n]*", "", src)
        # The exact upstream / pre-fix shape: assignment of "/config" to
        # an env-mapping HOME key on a calibre-related variable.
        offenders = re.findall(
            r"calibre_env\s*\[\s*['\"]HOME['\"]\s*\]\s*=\s*['\"]/config['\"]",
            src_no_comments,
        )
        offenders += re.findall(
            r"my_env\s*\[\s*['\"]HOME['\"]\s*\]\s*=\s*['\"]/config['\"]",
            src_no_comments,
        )
        assert not offenders, (
            f"{site}: hardcoded `HOME = /config` on a calibre subprocess "
            f"env regressed the opt-in mechanism — every site must route "
            f"through cps.services.calibre_user_plugins.apply_to_env() "
            f"so CWA_CALIBRE_USER_PLUGINS controls the behavior. "
            f"Offending matches: {offenders}"
        )

    @pytest.mark.parametrize("site", SITES)
    def test_each_site_imports_or_references_helper(self, site):
        """Each site must mention the helper module name. Without this
        reference, the env never gets HOME=/config even when the operator
        opts in — which would silently regress the feature for users
        who set the env var expecting plugins to load."""
        path = REPO_ROOT / site
        src = path.read_text()
        assert "calibre_user_plugins" in src, (
            f"{site}: no reference to cps.services.calibre_user_plugins. "
            f"The opt-in routing must go through that module so all sites "
            f"share the same env-var contract."
        )
