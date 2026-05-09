# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Defense-in-depth pin: every Convert-Library / EPUB-Fixer admin endpoint
in `cps/cwa_functions.py` must carry both `@login_required_if_no_ano` and
`@admin_required`.

Background — upstream CWA bug [#1304](https://github.com/crocodilestick/Calibre-Web-Automated/issues/1304),
filed by @menelausx 2026-04-26: in upstream calibre-web-automated as of
v4.0.6, several admin endpoints (`/cwa-convert-library-start`,
`/cwa-epub-fixer-start`, plus the page renders / log archives / log
downloads) shipped without auth decorators. A remote unauthenticated
user could trigger full-library convert / EPUB-rewrite operations via a
simple GET — significant CPU / disk I/O / storage consumption on every
CWA install. @pacoorozco proposed the fix in [PR #1308](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1308).

Our fork already protects all 10 endpoints. This test pins that
protection so a future refactor can't silently strip the decorators.

Each endpoint must have:
  @<blueprint>.route('/...')
  @login_required_if_no_ano
  @admin_required
  def handler(...):

Why both: `login_required_if_no_ano` reads anonymous-browse config and
falls through to the actual `login_required` only when anon is off.
With anon on, the request reaches `admin_required`, which then runs
`current_user.role_admin()` — anonymous users don't satisfy that, so
the request gets aborted(403). The pair is correct; the upstream bug
was the absence of the pair entirely on these specific routes.

The test walks the AST of `cps/cwa_functions.py`, finds every
`@<blueprint>.route(...)` decoration on a function, extracts the route
path, and asserts that for the 10 known-sensitive endpoints both
decorators are present in the decoration stack. New routes added in
the future are flagged when their path matches the sensitive pattern.
"""

import ast
from pathlib import Path

import pytest


CWA_FUNCTIONS = (Path(__file__).resolve().parent.parent.parent /
                 "cps" / "cwa_functions.py")


# Endpoints from CWA #1304 + #1308. Format: (route-path, what-it-does).
# A new admin endpoint added under cwa_functions.py with a similar shape
# (full-library bulk operation, log download, page render of admin UI)
# should be added here too.
SENSITIVE_ENDPOINTS = [
    "/cwa-convert-library-overview",
    "/cwa-convert-library/log-archive",
    "/cwa-convert-library/download-current-log/<log_filename>",
    "/cwa-convert-library-start",
    "/convert-library-cancel",
    "/cwa-epub-fixer-overview",
    "/cwa-epub-fixer/log-archive",
    "/cwa-epub-fixer/download-current-log/<log_filename>",
    "/cwa-epub-fixer-start",
    "/epub-fixer-cancel",
]


def _is_blueprint_route_decorator(dec: ast.expr) -> bool:
    """Match `@<name>.route(...)` calls."""
    if not isinstance(dec, ast.Call):
        return False
    if not isinstance(dec.func, ast.Attribute):
        return False
    return dec.func.attr == "route"


def _route_path(dec: ast.Call) -> str | None:
    """Extract the literal first arg from a `route(...)` call. None if
    the path isn't a literal string (parametric, computed, etc.)."""
    if not dec.args:
        return None
    arg0 = dec.args[0]
    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
        return arg0.value
    return None


def _decorator_names(decs: list[ast.expr]) -> set[str]:
    """Return the set of @decorator names applied to a function. Handles
    both bare `@name` and `@name()` forms."""
    out = set()
    for d in decs:
        if isinstance(d, ast.Name):
            out.add(d.id)
        elif isinstance(d, ast.Call) and isinstance(d.func, ast.Name):
            out.add(d.func.id)
    return out


@pytest.fixture(scope="module")
def routes_with_decorators():
    """Parse cwa_functions.py and return a list of (route_path, decorator_names_set)
    tuples for every function whose decorator stack includes a *.route call."""
    src = CWA_FUNCTIONS.read_text()
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        decs = node.decorator_list
        route_decs = [d for d in decs if _is_blueprint_route_decorator(d)]
        if not route_decs:
            continue
        path = _route_path(route_decs[0])
        if path is None:
            continue
        names = _decorator_names(decs)
        out.append((path, names, node.name, node.lineno))
    return out


@pytest.mark.unit
class TestCWAFunctionsAdminDecorators:
    """Pin the auth decorators on every CWA bulk-operation / log-archive
    endpoint. Catches regressions like upstream CWA #1304."""

    def test_cwa_functions_present(self):
        assert CWA_FUNCTIONS.is_file(), f"missing: {CWA_FUNCTIONS}"

    def test_all_sensitive_endpoints_have_login_required_if_no_ano(
            self, routes_with_decorators):
        """Every endpoint in SENSITIVE_ENDPOINTS must carry
        `@login_required_if_no_ano` in its decorator stack. Without this,
        anonymous-browse users could hit the route directly (login is
        bypassed) and the only remaining gate is admin_required."""
        missing = []
        for path, decs, name, line in routes_with_decorators:
            if path in SENSITIVE_ENDPOINTS:
                if "login_required_if_no_ano" not in decs:
                    missing.append((path, name, line, sorted(decs)))
        assert not missing, (
            "Sensitive admin endpoints missing @login_required_if_no_ano: "
            + str(missing) +
            "\nUpstream CWA #1304 — without this decorator, anonymous-browse "
            "users could reach the handler unauthenticated."
        )

    def test_all_sensitive_endpoints_have_admin_required(
            self, routes_with_decorators):
        """Every endpoint in SENSITIVE_ENDPOINTS must carry
        `@admin_required`. login_required_if_no_ano alone is not enough —
        regular non-admin users could still trigger full-library bulk
        operations or download admin logs."""
        missing = []
        for path, decs, name, line in routes_with_decorators:
            if path in SENSITIVE_ENDPOINTS:
                if "admin_required" not in decs:
                    missing.append((path, name, line, sorted(decs)))
        assert not missing, (
            "Sensitive admin endpoints missing @admin_required: "
            + str(missing) +
            "\nUpstream CWA #1304 — without this decorator, regular "
            "authenticated users could trigger full-library bulk operations "
            "or download admin logs they shouldn't see."
        )

    def test_all_sensitive_endpoints_present_in_source(
            self, routes_with_decorators):
        """Sanity: every entry in SENSITIVE_ENDPOINTS must actually exist
        as a route in cwa_functions.py. If an endpoint is renamed or
        removed, this test goes red — forcing the test to be updated to
        match the new reality, rather than silently passing on a
        non-existent endpoint."""
        present_paths = {p for p, _, _, _ in routes_with_decorators}
        missing = [p for p in SENSITIVE_ENDPOINTS if p not in present_paths]
        assert not missing, (
            f"SENSITIVE_ENDPOINTS list out of sync with cwa_functions.py — "
            f"these paths are no longer in the source: {missing}. Update "
            f"the list to match the current routes."
        )

    def test_decorator_order_login_outermost_admin_inner(
            self, routes_with_decorators):
        """Decorator order matters: @login_required_if_no_ano must come
        BEFORE (above) @admin_required so that anonymous-browse callers
        are routed through admin_required (which then calls
        current_user.role_admin() — false for anon — and aborts 403). The
        reverse order would let anon users skip auth entirely on routes
        that allow anon-browse globally."""
        wrong_order = []
        for path, decs, name, line in routes_with_decorators:
            if path not in SENSITIVE_ENDPOINTS:
                continue
            # We need the ordered list, not the set, to check ordering.
            src = CWA_FUNCTIONS.read_text()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if (isinstance(node, ast.FunctionDef) and
                        node.lineno == line and node.name == name):
                    ordered = []
                    for d in node.decorator_list:
                        if isinstance(d, ast.Name):
                            ordered.append(d.id)
                        elif isinstance(d, ast.Call) and \
                                isinstance(d.func, ast.Name):
                            ordered.append(d.func.id)
                    try:
                        i_login = ordered.index("login_required_if_no_ano")
                        i_admin = ordered.index("admin_required")
                    except ValueError:
                        # Caught by other tests
                        continue
                    if i_login >= i_admin:
                        wrong_order.append(
                            (path, name, ordered)
                        )
                    break
        assert not wrong_order, (
            "@login_required_if_no_ano must appear ABOVE @admin_required "
            "(i.e. lower index in decorator_list, applied last). "
            f"Wrong-order endpoints: {wrong_order}"
        )
