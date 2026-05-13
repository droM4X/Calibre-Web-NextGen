"""Behavioral regression test for the tier-2 file classifier in
``.github/workflows/auto-merge.yml``.

The workflow's ``case`` statement decides whether a tier-2 PR has
"code changes" (forcing it to update CHANGES-vs-upstream.md) by
walking each changed file's path against a list of patterns. The
match is positional — order matters. If the wildcard ``*.md`` appears
before the explicit ``CHANGES-vs-upstream.md`` branch, the wildcard
swallows the explicit name and ``has_changes_update`` never gets set,
which demotes every well-behaved tier-2 PR that *does* update
CHANGES-vs-upstream.md.

This test extracts the case statement from the live workflow file and
runs it under real bash against representative file lists, asserting
the expected ``has_code`` / ``has_changes_update`` outcome. If a
future edit re-orders the cases or adds a new wildcard that
accidentally swallows the CHANGES-vs-upstream.md name, the assertions
fire.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTO_MERGE = REPO_ROOT / ".github" / "workflows" / "auto-merge.yml"


def _extract_case_block() -> str:
    """Pull the file-classifier case block out of auto-merge.yml so the
    test exercises the actual production logic, not a fixture copy."""
    text = AUTO_MERGE.read_text()
    # Anchor on the unique `has_changes_update=0` initialization just
    # above the while loop, then capture through the matching `esac`.
    match = re.search(
        r"has_code=0\s*\n\s*has_changes_update=0\s*\n"
        r"(\s*while IFS= read -r f; do\s*\n.*?\n\s*esac\s*\n\s*done <<<\"\$files\")",
        text,
        re.DOTALL,
    )
    assert match, (
        "Couldn't locate the tier-2 file-classifier block in "
        "auto-merge.yml — was the structure refactored? Update this "
        "test's anchor regex."
    )
    return match.group(1)


def _classify(file_paths: list[str]) -> tuple[int, int]:
    """Feed `file_paths` (newline-separated) to the extracted case
    block and return ``(has_code, has_changes_update)``."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not on PATH")

    case_block = _extract_case_block()
    files_input = "\n".join(file_paths)
    script = f"""
set -euo pipefail
files=$(cat)
has_code=0
has_changes_update=0
{case_block}
echo "$has_code $has_changes_update"
"""
    result = subprocess.run(
        [bash, "-c", script],
        input=files_input,
        capture_output=True,
        text=True,
        check=True,
    )
    parts = result.stdout.strip().split()
    assert len(parts) == 2, f"unexpected output: {result.stdout!r}"
    return int(parts[0]), int(parts[1])


def test_tier2_pr_with_changes_update_is_recognized():
    """The bug we're regressing. A PR that touches workflow YAML AND
    updates CHANGES-vs-upstream.md must produce has_code=1 AND
    has_changes_update=1 — so the workflow proceeds, not skips."""
    has_code, has_changes_update = _classify([
        ".github/workflows/discord-release-bot.yml",
        ".github/workflows/dockerhub-description.yml",
        "CHANGES-vs-upstream.md",
    ])
    assert has_code == 1, "workflow YAML must count as code"
    assert has_changes_update == 1, (
        "CHANGES-vs-upstream.md update must be recognized — if this "
        "fails, the case-pattern order regressed (the *.md wildcard "
        "is matching CHANGES-vs-upstream.md before the explicit branch)"
    )


def test_pure_changes_md_only_pr_has_no_code():
    """A PR that ONLY touches CHANGES-vs-upstream.md is docs-only."""
    has_code, has_changes_update = _classify(["CHANGES-vs-upstream.md"])
    assert has_code == 0
    assert has_changes_update == 1


def test_tier2_pr_with_code_but_no_changes_is_demoted():
    """A PR that touches code but FORGETS to update CHANGES is the
    legitimate skip path the workflow guards. has_code=1,
    has_changes_update=0 → workflow comments + demotes."""
    has_code, has_changes_update = _classify([
        "cps/some_module.py",
        "root/etc/s6-overlay/s6-rc.d/some-service/run",
    ])
    assert has_code == 1
    assert has_changes_update == 0


def test_translation_only_pr_is_not_code():
    """Tier-1 territory: .po-only is exempt from the CHANGES rule."""
    has_code, has_changes_update = _classify([
        "cps/translations/de/LC_MESSAGES/messages.po",
        "cps/translations/fr/LC_MESSAGES/messages.po",
    ])
    assert has_code == 0
    assert has_changes_update == 0


def test_readme_and_docs_only_pr_is_not_code():
    """README + plain .md edits are exempt."""
    has_code, has_changes_update = _classify([
        "README.md",
        "notes/some-design.md",
    ])
    assert has_code == 0
    assert has_changes_update == 0


def test_mixed_translations_and_code_with_changes():
    """Realistic shape: code + translations + CHANGES.md = pass."""
    has_code, has_changes_update = _classify([
        "cps/web.py",
        "cps/translations/de/LC_MESSAGES/messages.po",
        "CHANGES-vs-upstream.md",
    ])
    assert has_code == 1
    assert has_changes_update == 1


def test_source_pin_changes_branch_precedes_md_wildcard():
    """Belt + suspenders: a static check that the explicit
    CHANGES-vs-upstream.md branch appears BEFORE any *.md wildcard in
    the case statement. The behavioral tests above already catch the
    semantic regression; this one guards against subtle textual edits
    that might re-arrange pattern order without immediately breaking
    the realistic inputs.

    Match only real case-statement branches — `<pattern>)` with the
    trailing paren — so prose comments mentioning `*.md` don't
    register as wildcards."""
    block = _extract_case_block()
    # Strip comment lines so prose can't trip the indices.
    stripped = "\n".join(
        line for line in block.splitlines() if not line.lstrip().startswith("#")
    )
    changes_match = re.search(r"CHANGES-vs-upstream\.md\)", stripped)
    wildcard_match = re.search(r"\*\.md\b", stripped)
    assert changes_match, "missing CHANGES-vs-upstream.md branch"
    assert wildcard_match, "missing *.md wildcard branch"
    assert changes_match.start() < wildcard_match.start(), (
        "CHANGES-vs-upstream.md branch must come BEFORE the *.md "
        "wildcard in the case statement, or the wildcard swallows the "
        "explicit name"
    )
