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
class TestAutoRegisterPlugins:
    """Auto-registration is the load-bearing piece for CWA #243.

    Without it, dropping a .zip into the plugins folder is inert —
    calibre needs `calibre-customize -a <zip>` to record the plugin in
    its customize.py.json registry. Users on upstream CWA #243
    repeatedly reported 'copied the plugin folder, nothing happens'
    because of this gap. The bootstrap now runs the registration step
    on first boot.
    """

    def test_disabled_returns_empty(self, clean_env, tmp_path):
        # No env var set
        result = calibre_user_plugins.auto_register_plugins(
            calibre_customize_binary="/nonexistent"
        )
        assert result == []

    def test_no_plugins_dir_returns_empty(self, clean_env, tmp_path, monkeypatch):
        clean_env.setenv("CWA_CALIBRE_USER_PLUGINS", "true")
        # Point _HOME at an empty tmp; plugins/ doesn't exist
        monkeypatch.setattr(calibre_user_plugins, "_HOME", str(tmp_path))
        monkeypatch.setattr(
            calibre_user_plugins, "_CUSTOMIZE_JSON",
            tmp_path / ".config" / "calibre" / "customize.py.json",
        )
        result = calibre_user_plugins.auto_register_plugins(
            calibre_customize_binary="/nonexistent"
        )
        assert result == []

    def test_already_registered_short_circuits(
            self, clean_env, tmp_path, monkeypatch):
        """Once customize.py.json has plugin entries, skip the scan to
        avoid re-running calibre-customize -a on every boot."""
        clean_env.setenv("CWA_CALIBRE_USER_PLUGINS", "true")
        monkeypatch.setattr(calibre_user_plugins, "_HOME", str(tmp_path))
        registry_path = tmp_path / ".config" / "calibre" / "customize.py.json"
        monkeypatch.setattr(calibre_user_plugins, "_CUSTOMIZE_JSON", registry_path)
        # Pre-populate the registry as if we'd registered before
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text('{"plugins": {"DeDRM": "/some/path.zip"}}')
        # Drop a fresh .zip in plugins/
        plugins_dir = tmp_path / ".config" / "calibre" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "fake_plugin.zip").write_bytes(b"PK\x03\x04")
        # Should short-circuit because registry has entries
        result = calibre_user_plugins.auto_register_plugins(
            calibre_customize_binary="/nonexistent"
        )
        assert result == []

    def test_first_boot_invokes_calibre_customize_per_zip(
            self, clean_env, tmp_path, monkeypatch):
        """On a fresh container, every .zip in plugins/ should trigger
        a calibre-customize -a call. This is the regression vector for
        CWA #243 — without it, users see the plugin in the folder but
        calibre never loads it at conversion time."""
        from unittest.mock import patch, MagicMock

        clean_env.setenv("CWA_CALIBRE_USER_PLUGINS", "true")
        monkeypatch.setattr(calibre_user_plugins, "_HOME", str(tmp_path))
        registry_path = tmp_path / ".config" / "calibre" / "customize.py.json"
        monkeypatch.setattr(calibre_user_plugins, "_CUSTOMIZE_JSON", registry_path)
        plugins_dir = tmp_path / ".config" / "calibre" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "DeDRM_plugin.zip").write_bytes(b"PK\x03\x04")
        (plugins_dir / "Obok_plugin.zip").write_bytes(b"PK\x03\x04")

        called_zips = []

        def fake_run(args, **kwargs):
            called_zips.append(args[2])
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = "Plugin added: FakePlugin (1, 0, 0)\n"
            return mock

        with patch("subprocess.run", side_effect=fake_run):
            result = calibre_user_plugins.auto_register_plugins(
                calibre_customize_binary="/fake/calibre-customize"
            )

        assert len(called_zips) == 2, (
            f"each .zip in plugins/ must trigger one calibre-customize -a "
            f"call; got {len(called_zips)} calls"
        )
        # Ordered by sorted glob — DeDRM before Obok alphabetically
        assert "DeDRM_plugin.zip" in called_zips[0]
        assert "Obok_plugin.zip" in called_zips[1]
        # Both registered → both names returned
        assert len(result) == 2
        assert all(name == "FakePlugin (1, 0, 0)" for name in result)

    def test_failed_calibre_customize_doesnt_block_boot(
            self, clean_env, tmp_path, monkeypatch):
        """If the binary is missing or returns nonzero, bootstrap should
        log + continue rather than raise. Container start must not be
        gated on plugin registration."""
        from unittest.mock import patch, MagicMock

        clean_env.setenv("CWA_CALIBRE_USER_PLUGINS", "true")
        monkeypatch.setattr(calibre_user_plugins, "_HOME", str(tmp_path))
        registry_path = tmp_path / ".config" / "calibre" / "customize.py.json"
        monkeypatch.setattr(calibre_user_plugins, "_CUSTOMIZE_JSON", registry_path)
        plugins_dir = tmp_path / ".config" / "calibre" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "fake.zip").write_bytes(b"PK\x03\x04")

        def fake_run_fails(args, **kwargs):
            mock = MagicMock()
            mock.returncode = 1
            mock.stdout = ""
            mock.stderr = "error: corrupt plugin"
            return mock

        with patch("subprocess.run", side_effect=fake_run_fails):
            # Must not raise — boot is best-effort
            result = calibre_user_plugins.auto_register_plugins(
                calibre_customize_binary="/fake/calibre-customize"
            )
        # Returns empty list — nothing was actually registered
        assert result == []

    def test_missing_binary_doesnt_raise(
            self, clean_env, tmp_path, monkeypatch):
        """`calibre-customize` not present at the configured path
        (alternate Calibre install, dev container) returns [] cleanly."""
        clean_env.setenv("CWA_CALIBRE_USER_PLUGINS", "true")
        monkeypatch.setattr(calibre_user_plugins, "_HOME", str(tmp_path))
        registry_path = tmp_path / ".config" / "calibre" / "customize.py.json"
        monkeypatch.setattr(calibre_user_plugins, "_CUSTOMIZE_JSON", registry_path)
        plugins_dir = tmp_path / ".config" / "calibre" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "fake.zip").write_bytes(b"PK\x03\x04")

        # No mock — actually try to invoke a path that doesn't exist
        result = calibre_user_plugins.auto_register_plugins(
            calibre_customize_binary="/this/path/does/not/exist"
        )
        assert result == []

    def test_registered_plugin_names_parses_customize_json(
            self, clean_env, tmp_path, monkeypatch):
        registry_path = tmp_path / "customize.py.json"
        registry_path.write_text(
            '{"plugins": {"DeDRM": "/p/dedrm.zip", "Obok DeDRM": "/p/obok.zip"}}'
        )
        monkeypatch.setattr(calibre_user_plugins, "_CUSTOMIZE_JSON", registry_path)
        names = calibre_user_plugins._registered_plugin_names()
        assert names == {"DeDRM", "Obok DeDRM"}

    def test_registered_plugin_names_handles_missing_file(
            self, clean_env, tmp_path, monkeypatch):
        monkeypatch.setattr(
            calibre_user_plugins, "_CUSTOMIZE_JSON", tmp_path / "missing.json"
        )
        assert calibre_user_plugins._registered_plugin_names() == set()

    def test_registered_plugin_names_handles_corrupt_json(
            self, clean_env, tmp_path, monkeypatch):
        registry_path = tmp_path / "corrupt.json"
        registry_path.write_text("{not valid json")
        monkeypatch.setattr(calibre_user_plugins, "_CUSTOMIZE_JSON", registry_path)
        # Must not raise — return empty set
        assert calibre_user_plugins._registered_plugin_names() == set()


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
