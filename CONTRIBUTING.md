# Contributing

Bug reports, fixes, translations, and small features welcome.

## Reporting a bug

[Open an issue](https://github.com/new-usemame/Calibre-Web-NextGen/issues/new). Useful template:

```
**Version**: v4.0.x (from container env or `/about`)
**Browser / client**: Safari 17 / Chrome 124 / KOReader 2024.04 / Kobo Libra Color
**Reverse proxy**: yes/no, with path prefix?
**Repro steps**:
1. ...
2. ...
**Expected vs actual**: ...
**Container logs (last 50 lines)**: ...
```

Bug reports without a version + repro are still useful — we'll just ask follow-ups before we can act on them.

## Submitting a PR

1. Fork → branch off `main` → commit → push → open PR against `main`.
2. Keep PRs focused on one logical change. A 200-line PR with one purpose merges; a 200-line PR with five purposes stalls.
3. CI must pass: `Fast Tests`, `validate-author`, `evaluate`, `Test Suite Summary`. Integration + E2E run on Docker-touching changes.
4. If touching `cps/` Python: add a unit test for the change (look at `cps/tests/` for the pattern).
5. If touching `cps/static/js/` or templates: include a one-line "tested in <browser>" note in the PR description.
6. If touching `cps/translations/`: just the `.po` is enough; CI runs `i18n-validate`.

Don't introduce new dependencies, license changes, or external service URLs without flagging in the PR description and tagging `@new-usemame` for approval.

## Backporting an upstream PR

If you spot a useful PR sitting unmerged on `crocodilestick/Calibre-Web-Automated`:

1. `git remote add upstream https://github.com/crocodilestick/Calibre-Web-Automated.git && git fetch upstream pull/<N>/head:upstream-pr-<N>`
2. `git checkout -b merge/upstream-pr-<N>` from current `main`
3. `git cherry-pick upstream-pr-<N>` (or rebase if it conflicts on refreshed `messages.pot`)
4. Re-author the commit so the author email is yours, not upstream's: `git commit --amend --reset-author`
5. Push, open PR, mention the upstream PR number + author in the PR title/body. Release notes will credit `@upstream-author`.

The autopilot script does this automatically (`scripts/draft-cherry-pick.sh <N>`), so check `notes/merge/` first to see if it's already in flight.

## Tier policy (which PRs auto-merge)

`safe-tier-1` (translations / docs only) auto-merge once CI is green.
`safe-tier-2` (≤50 LOC isolated single-file code, no security-adjacent paths) auto-merge after a 7-day clean tier-1 history.
`needs-review` (everything else) waits for project lead.

Full tier definitions in [`CLAUDE.md`](CLAUDE.md#tier-policy).

## Style

Follow the existing code style of the file you're editing. CWA is mostly Flask + SQLAlchemy + jQuery + Bootstrap; we're not doing a rewrite. New code that fits the existing patterns merges; new code that introduces a new framework or paradigm gets bounced.

## Getting commit access

See [`GOVERNANCE.md`](GOVERNANCE.md#becoming-a-maintainer). Short version: ~3 quality merged PRs + ask.

## Credit

Every backported upstream PR credits the original author by handle in the release notes. Direct contributions are credited the same way. We don't squash credit out.
