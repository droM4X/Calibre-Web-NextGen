# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Opt-in support for user-installed Calibre plugins during ingest.

When ``CWA_CALIBRE_USER_PLUGINS`` is set to a truthy value (``1`` /
``true`` / ``yes`` / ``on``), Calibre subprocess invocations launched by
the ingest pipeline run with ``HOME=/config`` so that the embedded
Calibre process loads any plugins the operator has placed under
``/config/.config/calibre/plugins/``. The plugins directory is created
on first use if missing.

The default is **off**. Plugin loading is the operator's explicit
choice — it activates third-party Python code from a user-controlled
directory inside the running container, and the operator should opt in
deliberately. Closes upstream Calibre-Web-Automated [issue
#243](https://github.com/crocodilestick/Calibre-Web-Automated/issues/243).

Public API:
    is_enabled() -> bool
    apply_to_env(env: dict[str, str]) -> dict[str, str]
    ensure_plugins_dir() -> Path | None

The module has no Flask / SQLAlchemy dependencies — safe to import from
any layer (cps/, scripts/) and from cont-init bootstrap code.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


_ENV_VAR = "CWA_CALIBRE_USER_PLUGINS"
_HOME = "/config"
_PLUGINS_SUBPATH = ".config/calibre/plugins"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_enabled() -> bool:
    """True iff the operator has opted into Calibre user-plugin loading.

    Reads ``CWA_CALIBRE_USER_PLUGINS`` from the process environment and
    normalizes the value (case- and whitespace-insensitive) against the
    standard truthy set used elsewhere in the codebase
    (cf. NETWORK_SHARE_MODE).
    """
    raw = os.environ.get(_ENV_VAR, "")
    return raw.strip().lower() in _TRUTHY


def apply_to_env(env: dict[str, str]) -> dict[str, str]:
    """If enabled, set ``HOME=/config`` on the given env mapping in
    place and return it. If disabled, return the env unchanged.

    Designed to be called right before ``subprocess.run(..., env=env)``:

        env = os.environ.copy()
        env = calibre_user_plugins.apply_to_env(env)
        subprocess.run(["ebook-convert", ...], env=env, check=True)

    When disabled, the subprocess inherits whatever HOME is already set
    in the parent (typically the abc service user's home), and Calibre
    looks for plugins in that user's home — usually empty, so plugins
    do not load. That is the intended off-state.
    """
    if is_enabled():
        env["HOME"] = _HOME
    return env


def plugins_dir() -> Path:
    """Absolute path to where Calibre will look for user plugins when
    HOME=/config. Always returns a path; doesn't check existence."""
    return Path(_HOME) / _PLUGINS_SUBPATH


def ensure_plugins_dir() -> Path | None:
    """If enabled, create ``/config/.config/calibre/plugins`` (with
    parents) so the operator has a destination ready for their plugin
    .zip files. Returns the path on success, ``None`` if disabled or on
    permission error (logged, not raised — bootstrap should be best-
    effort, not block the container start).

    Idempotent: harmless when the dir already exists.
    """
    if not is_enabled():
        return None
    target = plugins_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except (PermissionError, OSError):
        # Bootstrap is best-effort. The operator can mkdir manually if
        # the container is running with restricted FS perms; ingest will
        # still see HOME=/config and use whatever the operator placed
        # there.
        return None


def env_var_name() -> str:
    """Public accessor so tests / docs can reference the canonical name
    without re-defining it."""
    return _ENV_VAR


def home_path() -> str:
    """Public accessor for the HOME value injected when enabled."""
    return _HOME
