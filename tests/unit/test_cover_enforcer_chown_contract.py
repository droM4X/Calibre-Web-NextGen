"""Pin the privilege-drop contract for ``cover_enforcer.py``.

Context — the May-13 s6 privilege-drop audit
(`notes/s6-privilege-drop-audit.md`, autopilot-side):

`metadata-change-detector/run` runs ``cover_enforcer.py`` as root by
design. ``cover_enforcer`` shells out to ``calibredb`` /
``ebook-polish`` to edit the on-disk Calibre tree, then explicitly
``os.chown(book_dir, uid, gid)`` back to PUID:PGID so the Flask app
(which runs as ``abc``) keeps write access to the same files.

If a future contributor either:

* removes the chown helper (assuming "the service drops privileges
  elsewhere now"), or
* changes the chown to skip files (e.g. forgets to walk subdirectories),

the user-visible symptom is silent — Flask just starts failing
cover-from-URL saves and metadata embeds with permission denied. This
test pins the helper's existence and the env-driven chown shape so
that regression goes red at unit-test time, long before anyone
notices the broken cover save in production.

The pin is intentionally structural (existence of method + AST shape
of its body), not behavioral. Behavioral testing of root→chown
requires running as root, which the test runner doesn't do.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
COVER_ENFORCER = REPO_ROOT / "scripts" / "cover_enforcer.py"


def _module_ast() -> ast.Module:
    return ast.parse(COVER_ENFORCER.read_text(), filename=str(COVER_ENFORCER))


def _find_function(name: str) -> ast.FunctionDef:
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(
        f"function {name!r} not found in {COVER_ENFORCER}. The "
        "metadata-change-detector privilege model depends on it — see "
        "notes/s6-privilege-drop-audit.md."
    )


def test_reset_book_dir_ownership_function_exists():
    """The chown helper that re-owns calibre tree artifacts back to
    abc must exist. Removing it breaks the metadata-edit flow on every
    container where PUID != 0."""
    fn = _find_function("_reset_book_dir_ownership")
    assert fn is not None


def test_chown_helper_reads_puid_and_pgid_from_env():
    """Source-pin that the chown helper reads PUID + PGID from
    ``os.environ`` — that's what makes it match the linuxserver/
    baseimage user mapping."""
    src = ast.unparse(_find_function("_reset_book_dir_ownership"))
    assert '"PUID"' in src or "'PUID'" in src, (
        "_reset_book_dir_ownership must read PUID from os.environ"
    )
    assert '"PGID"' in src or "'PGID'" in src, (
        "_reset_book_dir_ownership must read PGID from os.environ"
    )


def test_chown_helper_actually_calls_os_chown():
    """The function must actually call ``os.chown`` — a refactor that
    stops at PUID/PGID lookup without applying it is the same bug."""
    fn = _find_function("_reset_book_dir_ownership")
    saw_os_chown = False
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "chown"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ):
            saw_os_chown = True
            break
    assert saw_os_chown, (
        "_reset_book_dir_ownership must call os.chown on the book "
        "directory and its entries — without this, files end up root-"
        "owned and Flask (abc) cannot rewrite them."
    )


def test_chown_helper_walks_directory_contents():
    """The helper must also chown the entries *inside* book_dir, not
    just the directory itself. Without the inner walk, the metadata
    file rewritten by ebook-polish stays root-owned and Flask's next
    cover-from-URL save fails."""
    fn = _find_function("_reset_book_dir_ownership")
    src = ast.unparse(fn)
    # listdir + chown-in-a-loop is the production shape. Pin the
    # listdir + chown combo (anything else is a structural change
    # worth manual review).
    assert "os.listdir" in src, (
        "_reset_book_dir_ownership must enumerate book_dir contents "
        "with os.listdir; refactor to a different walker would silently "
        "skip files inside subdirectories or miss entries the old "
        "code covered."
    )
    # The chown call must appear after a listdir-based iteration too.
    # Cheap text check: chown count >= 2 (one for book_dir, one per-entry).
    chown_count = src.count("os.chown")
    assert chown_count >= 2, (
        f"_reset_book_dir_ownership has os.chown only {chown_count}× "
        "— the production shape chowns both book_dir AND each entry "
        "inside. A single chown would silently leave inner files "
        "root-owned."
    )


def test_metadata_change_detector_invokes_cover_enforcer():
    """Inverse pin: the metadata-change-detector run script must
    still invoke cover_enforcer.py. If a future PR routes around it
    (e.g. inlines the metadata logic into Flask), the chown invariant
    above becomes moot and the audit's disposition needs revisiting.
    Surface that change as a test failure rather than letting it
    silently land."""
    run = REPO_ROOT / "root" / "etc" / "s6-overlay" / "s6-rc.d" / "metadata-change-detector" / "run"
    assert run.exists(), f"missing {run}"
    text = run.read_text()
    assert "cover_enforcer.py" in text, (
        "metadata-change-detector no longer calls cover_enforcer.py — "
        "the chown contract pinned by the other tests in this file is "
        "now decoupled from the metadata-edit flow. Revisit "
        "notes/s6-privilege-drop-audit.md and update the audit "
        "disposition before adjusting these tests."
    )
