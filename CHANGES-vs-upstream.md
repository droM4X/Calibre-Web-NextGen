# Changes vs upstream Calibre-Web-Automated

Tracks every divergence between this fork and `crocodilestick/Calibre-Web-Automated@main` since the fork point. Updated per release.

Format: each row is one fork-PR, mapped to its upstream PR or issue (if any), with a one-line description and the squash-merge SHA.

## Backports (upstream PRs we merged ahead of upstream review)

| Fork PR | Upstream | Author | Description | SHA | Release |
|---|---|---|---|---|---|
| #4 | [#1313](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1313) | @ikuma-hiroyuki | i18n(ja) fill empty msgstr + fix existing | `f5fc59a` | v4.0.6 |
| #5 | [#1291](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1291) | @sinyawskiy | i18n update messages.po | `67dce95` | v4.0.6 |
| #7 | [#1274](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1274) | @cu0uz | i18n update messages.po | `65ffc90` | v4.0.6 |
| #8 | [#1305](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1305) | @RFrens | i18n update messages.po | `f1dc937` | v4.0.6 |
| #9 | [#1267](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1267) | @area57 | i18n update messages.po | `4ebb7ae` | v4.0.6 |
| #11 | [#1296](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1296) | @julien-noblet | i18n(fr) fix lang | `1e1eca9` | v4.0.6 |
| #12 | [#1257](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1257) | @lexis11mob | i18n update messages.po | `188a854` | v4.0.6 |
| #13 | [#1298](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1298) | @rancur | Docker healthcheck `curl -fsL` so `/ → /login` 302 doesn't fail | `653a516` | v4.0.7 |
| #14 | [#1283](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1283) | @chloeroform | user-profile `fetch()` → `getPath()` for reverse-proxy path prefixes | `a5dd59c` | v4.0.7 |
| #15 | [#1322](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1322) | @Sycha | `.cbr` / `.cbz` → IANA mimetypes | `fc8ba00` | v4.0.7 |
| #16 | [#1096](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1096) | @SethMilliken | Safari metadata-search CSRF token | `6721638` | v4.0.7 |
| #17 | [#1213](https://github.com/crocodilestick/Calibre-Web-Automated/pull/1213) | @hsttlrjeff | Kobo `HandleStateRequest` `.get("Location")` + `last_modified` | `1fd6c50` | v4.0.7 |

## Original fork patches (no upstream PR existed)

### Bug fixes

| Fork PR | Upstream issue | Description | SHA | Release |
|---|---|---|---|---|
| #1 | (audit; user-reported #1206 / #1078 / #1097 / #1270 / #1329) | Revert hardcoded Safari skip in `uploadprogress.js` so `$.fn.uploadprogress` stays defined and `main.js` doesn't crash on Safari | `3b7d70c` | v4.0.6 |

### Security

| Fork PR | Upstream issue | Description | SHA | Release |
|---|---|---|---|---|
| #18 | [#1303](https://github.com/crocodilestick/Calibre-Web-Automated/issues/1303) | Kobo IDOR: `/kobo_auth/generate_auth_token` + `/deleteauthtoken` reject `current_user.id != user_id` and not admin | `9f50bb2` | v4.0.7 |
| #19 | (fork audit; private disclosure to upstream) | 14 CWA routes (cwa_logs, convert_library, epub_fixer) gated with `@login_required_if_no_ano + @admin_required` | `09bf581` | v4.0.7 |
| #20 | (fork audit; private disclosure to upstream) | `cover_enforcer.py` shell-injection: `os.system(f'cp "{title}" ...')` → `shutil.copy` / `shutil.rmtree` | `b70fb53` | v4.0.7 |

### Infrastructure

| Fork PR | Description | SHA | Release |
|---|---|---|---|
| #6 | CI: rewrite gating so auto-merge actually waits for green CI | `d7e22c6` | v4.0.6 |
| #10 | CI: resolve PRs on `workflow_run` via `.pull_requests[]` | `f8fb1ed` | v4.0.6 |
| #21 | Production-ready GHCR multi-arch release pipeline | `7a26106` | v4.0.7 |
| #27 | Drop deadsnakes PPA, install Python 3.13 from python-build-standalone | `2e5d781` | v4.0.7 |
| #28 | Updater: point release check at this fork; only flag true upgrades | `5997519` | v4.0.8 |

### Features (fork-original, not yet upstream)

| Fork PR | Description | SHA | Release |
|---|---|---|---|
| #29 | Bump cover resolution for high-DPI e-readers (Libra Color etc.) | `630ee22` | v4.0.9 |
| #30 | Metadata: Open Library + Google Books API key + per-provider status + in-modal API-key panel | `428530c` | v4.0.9 |
| #31 | `isoLanguages.get_language_names`: tolerate `None` / string locales (unblocks DNB) | `491af54` | v4.0.10 |
| #32 | DNB: drop synchronous cover-validation; switch to `<img onerror>` graceful fallback | `dab1589` | v4.0.11 |
| #33 | Metadata: cover-resolution booster + sort-by-cover-size in fetch dialog | `e5b7666` | v4.0.12 |
| #34 | docs(readme): user-facing fork front-matter | `aa89fd7` | (docs only) |
| #49 | Fix "Cover-file is not a valid image file" on URL covers (Hardcover/Google/iTunes): chown back to PUID:PGID after enforcer + diagnostics on cover-save failures | `4df03f0` | v4.0.13 |

## Container image

Published to `ghcr.io/new-usemame/calibre-web-nextgen` instead of upstream's `crocodilestick/calibre-web-automated`. Same data layout, same compose file shape — drop-in swap.

## Mergeable-back guarantee

Every entry in this file's "Backports" section is a clean cherry-pick of an upstream PR with the original author preserved as committer in the squash-merge message. If upstream coordination resumes and the maintainer wants to take any of these back, they can `git cherry-pick <sha>` from this fork's main and the patch will apply cleanly.

For "Original fork patches": the diffs are small and isolated; PR descriptions in this fork link the line-by-line rationale. Upstream is welcome to take them.
