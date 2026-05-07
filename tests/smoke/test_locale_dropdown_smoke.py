# -*- coding: utf-8 -*-
"""Regression tests for repo issue #71 ([bug] Missing interface languages).

Two failure modes were live in v4.0.23:

  1. `scripts/compile_translations.sh` used `find | while read ... exit 1`,
     which truncated the .mo set at the first .po with a hard msgfmt
     error. The Docker image therefore shipped 6 of 28 locales.

  2. `cps.cw_babel.get_available_locale()` returned
     `[Locale('en')] + babel.list_translations()`. Modern flask_babel
     auto-appends the default locale to its return value, so the explicit
     prepend produced English twice in the dropdown.

This module covers (2) end-to-end against an in-memory Flask + Babel app.
The (1) script-shape contract is enforced by
`tests/autopilot/test_compile_translations.sh`.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


def _load_cw_babel_with_stubbed_login():
    """Load `cps.cw_babel` without the surrounding Flask app — we only
    need `get_available_locale()` and a real `babel.Babel` to exercise
    the dedup contract."""

    repo_root = Path(__file__).resolve().parents[2]

    if "cps" not in sys.modules:
        cps_pkg = types.ModuleType("cps")
        cps_pkg.__path__ = [str(repo_root / "cps")]  # type: ignore[attr-defined]
        sys.modules["cps"] = cps_pkg

    # `cw_babel` imports `from .cw_login import current_user`. Provide a
    # minimal stub so we don't drag in the real auth stack.
    if "cps.cw_login" not in sys.modules:
        cw_login_stub = types.ModuleType("cps.cw_login")
        cw_login_stub.current_user = None  # type: ignore[attr-defined]
        sys.modules["cps.cw_login"] = cw_login_stub
        # The real subpackage uses dotted access; satisfy `from .cw_login`
        sys.modules["cps"].cw_login = cw_login_stub  # type: ignore[attr-defined]

    if "cps.logger" not in sys.modules:
        logger_stub = types.ModuleType("cps.logger")
        import logging as _logging

        def _create(_name=None):
            return _logging.getLogger("cps.cw_babel.test")

        logger_stub.create = _create  # type: ignore[attr-defined]
        sys.modules["cps.logger"] = logger_stub
        sys.modules["cps"].logger = logger_stub  # type: ignore[attr-defined]

    spec = importlib.util.spec_from_file_location(
        "cps.cw_babel", repo_root / "cps" / "cw_babel.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["cps.cw_babel"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def babel_app(tmp_path):
    """Build a Flask + Babel app with the real translations directory so
    `babel.list_translations()` mirrors a runtime environment."""

    flask = pytest.importorskip("flask")
    flask_babel = pytest.importorskip("flask_babel")
    cw_babel = _load_cw_babel_with_stubbed_login()

    repo_root = Path(__file__).resolve().parents[2]
    translations_dir = repo_root / "cps" / "translations"

    # We need .mo files present for list_translations() to return the
    # locales — they're gitignored, so compile on demand for the test.
    import subprocess

    compile_script = repo_root / "scripts" / "compile_translations.sh"
    subprocess.run(
        ["bash", str(compile_script)],
        check=True,
        capture_output=True,
    )

    app = flask.Flask("cw_babel_smoke")
    app.config["BABEL_TRANSLATION_DIRECTORIES"] = str(translations_dir)
    app.config["BABEL_DEFAULT_LOCALE"] = "en"

    cw_babel.babel.init_app(app)
    return app, cw_babel


def test_no_duplicate_english_in_locale_list(babel_app):
    """The dropdown source must not contain English twice."""
    app, cw_babel = babel_app
    with app.app_context():
        locales = cw_babel.get_available_locale()
        codes = [str(loc) for loc in locales]
        en_count = sum(1 for code in codes if code == "en")
        assert en_count == 1, (
            f"Expected exactly one 'en' locale, got {en_count}. "
            f"Full list: {codes}"
        )


def test_locale_list_is_alphabetically_sorted(babel_app):
    """Cancel/Save lists are right-aligned; the language picker should be
    alphabetically ordered by display name so users can scan it."""
    app, cw_babel = babel_app
    with app.app_context():
        locales = cw_babel.get_available_locale()
        names = [loc.display_name.lower() for loc in locales]
        assert names == sorted(names), (
            "get_available_locale should return locales sorted by display name"
        )


def test_locale_list_includes_a_useful_breadth_of_languages(babel_app):
    """Sanity check — if the .mo set ever truncates back to a handful of
    locales, this fails loudly. Threshold is conservative (>= 20) so that
    one .po file regressing doesn't false-fail the suite, but the
    `compile_translations.sh` autopilot test catches the strict 28/28."""
    app, cw_babel = babel_app
    with app.app_context():
        locales = cw_babel.get_available_locale()
        codes = {str(loc) for loc in locales}
        assert len(codes) >= 20, (
            f"Locale dropdown shrank to {len(codes)} entries: {sorted(codes)}. "
            f"Did compile_translations.sh regress?"
        )
        # Spot-check a handful of common locales that we want to keep
        # working — these were missing before the fix.
        for required in ("en", "de", "fr", "es", "it", "ru", "zh_Hans_CN"):
            assert required in codes, (
                f"Expected locale '{required}' present in dropdown; got {sorted(codes)}"
            )
