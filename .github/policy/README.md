# `.github/policy/` — single source of truth for autopilot policy

Policy values that multiple consumers (workflows, scripts, tests, docs) need to agree on live here. Each consumer reads from these files at run time; nobody hard-codes a value that's described in this directory.

## Files

### `tier-policy.env`

Auto-merge tier policy. Sourceable shell with simple `KEY=VALUE` lines (no interpolation, no functions). Currently consumed by:

- `.github/workflows/auto-merge.yml` — sources the file in the evaluate job and reads `$FORBIDDEN_PATHS_REGEX`, `$TIER2_MAX_ADDITIONS`, etc.
- `scripts/triage-prs.sh` — parses the file for the same constants when classifying PRs into buckets.
- `tests/autopilot/test_tier_policy.sh` — pins the file's existence + parseability + key fields.

### `README.md` — this file.

## Why this directory exists

Before this consolidation, the same constants (LOC caps, forbidden paths, required CI checks) were declared inline in five different places — workflow, triage script, test file, CLAUDE.md, the autopilot SKILL.md. Three of those declarations had drifted: CLAUDE.md said tier-2 was capped at "<50 LOC" while the workflow enforced 80; the workflow forbidden-paths regex missed `cps/cw_login/` and `cps/usermanagement.py`; the triage script's auth path list didn't cover `cps/db.py`. Drift produced confusion ("why are we still tier-1?") and gaps (tier-2 PRs slipping past meant-to-be forbidden surfaces).

Single source of truth here, doc references it by path-and-line. If a value needs to change, change it here — every consumer picks up the new value on next run.

## How to add a new policy value

1. Add the new `KEY=VALUE` line to the relevant `.env` file with a comment explaining what it gates.
2. Update each consumer to read it. For shell consumers, that's `source .github/policy/tier-policy.env`. For Python (e.g., `triage-prs.sh`'s embedded Python), read the file with a small parser:
   ```python
   import os, re
   policy = {}
   with open(".github/policy/tier-policy.env") as fh:
       for line in fh:
           m = re.match(r"^([A-Z_]+)='?(.*?)'?$", line.strip())
           if m: policy[m.group(1)] = m.group(2)
   ```
3. Add a test in `tests/autopilot/test_tier_policy.sh` asserting the key exists and parses.
4. Update CLAUDE.md if the value is policy-level (gate semantics, what counts as tier-1 vs tier-2). Inline numeric values stay out of docs — they live here.

## How to change an existing policy value

1. Update the value in the `.env` file.
2. Run `bash tests/autopilot/test_tier_policy.sh` to confirm parseability.
3. Run `bash scripts/autopilot-tick.sh` (with `AUTOPILOT_NO_LLM=1`) to confirm the triage script still loads cleanly.
4. Open a PR labeled `needs-review` — policy changes never auto-merge.
