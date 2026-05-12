# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression test for fork issue #141 — user-facing template URLs that
direct users to project resources (release notes, issue tracker, project
home) must point at ``new-usemame/Calibre-Web-NextGen``, not upstream.

Cherry-picks from ``crocodilestick/Calibre-Web-Automated`` keep
reintroducing the upstream URL into ``cps/templates/`` — the "See
Changelog" link in the in-app update banner, the "Create Issue" link on
error pages, and the "CWA GitHub" admin button were all still pointing
at upstream months after the fork detached. This test pins them.

Allowed upstream references (attribution + docs we don't have our own
copy of yet) are listed explicitly so the test doesn't fight legitimate
credit lines. Add to ``ALLOWED_UPSTREAM_REFS`` only with operator
review — anything new should default to the fork URL.
"""

import os
import re

import pytest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEMPLATES_DIR = os.path.join(REPO_ROOT, "cps", "templates")

UPSTREAM_HOST_RE = re.compile(
    r"https?://github\.com/crocodilestick/[Cc]alibre-[Ww]eb-[Aa]utomated",
)

ALLOWED_UPSTREAM_REFS = {
    # Attribution in the public stats page: "based on Calibre-Web Automated."
    # Credit line — must stay pointing at upstream.
    "stats.html",
    # Database-configuration wiki link. Our own wiki doesn't have an
    # equivalent page yet; users follow the upstream wiki for now.
    # Remove once we publish our own configuration docs.
    "config_db.html",
}


def _list_templates():
    found = []
    for dirpath, _, filenames in os.walk(TEMPLATES_DIR):
        for fn in filenames:
            if fn.endswith((".html", ".htm", ".j2", ".jinja", ".jinja2")):
                found.append(os.path.join(dirpath, fn))
    return sorted(found)


@pytest.mark.unit
def test_no_unexpected_upstream_template_urls():
    offenders = []
    for path in _list_templates():
        base = os.path.basename(path)
        if base in ALLOWED_UPSTREAM_REFS:
            continue
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if UPSTREAM_HOST_RE.search(line):
                    offenders.append((path, lineno, line.rstrip()))
    if offenders:
        msg = ["User-facing templates still link to upstream CWA:"]
        for path, lineno, line in offenders:
            rel = os.path.relpath(path, REPO_ROOT)
            msg.append(f"  {rel}:{lineno}  {line.strip()}")
        msg.append(
            "Point these at https://github.com/new-usemame/Calibre-Web-NextGen, "
            "or add the filename to ALLOWED_UPSTREAM_REFS with a reason."
        )
        pytest.fail("\n".join(msg))


@pytest.mark.unit
def test_changelog_link_is_fork():
    """Spot-pin the original #141 regression site so a refactor of
    layout.html doesn't silently regress @droM4X's symptom."""
    path = os.path.join(TEMPLATES_DIR, "layout.html")
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    assert "new-usemame/Calibre-Web-NextGen/releases" in body, (
        "layout.html cwa_update banner must link to the fork's releases page"
    )


@pytest.mark.unit
def test_create_issue_link_is_fork():
    """Error pages must direct bug reports at the fork's tracker, not
    upstream's — the fork is where issues actually get triaged."""
    path = os.path.join(TEMPLATES_DIR, "http_error.html")
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    assert "new-usemame/Calibre-Web-NextGen/issues" in body, (
        "http_error.html 'Create Issue' link must point at the fork tracker"
    )


@pytest.mark.unit
def test_admin_github_button_is_fork():
    """The Admin panel 'CWA GitHub' button is the most prominent
    project-home link in the UI — it must land users on the fork repo
    where they can actually file issues and follow releases."""
    path = os.path.join(TEMPLATES_DIR, "admin.html")
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    assert 'id="cwa_github_link"' in body
    assert "new-usemame/Calibre-Web-NextGen" in body, (
        "admin.html cwa_github_link must point at the fork repo"
    )
