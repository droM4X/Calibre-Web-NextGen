# Calibre-Web-NextGen

> **The community-maintained build of [Calibre-Web-Automated](https://github.com/crocodilestick/Calibre-Web-Automated) (CWA).**
> Same software, same compose file, same data — with the bug fixes that have been sitting open in upstream's PR queue already merged in.

[![Latest release](https://img.shields.io/github/v/release/new-usemame/Calibre-Web-NextGen)](https://github.com/new-usemame/Calibre-Web-NextGen/releases/latest)
[![Container](https://img.shields.io/badge/ghcr.io-calibre--web--nextgen-blue?logo=docker)](https://github.com/new-usemame/Calibre-Web-NextGen/pkgs/container/calibre-web-nextgen)
[![Open issues](https://img.shields.io/github/issues/new-usemame/Calibre-Web-NextGen)](https://github.com/new-usemame/Calibre-Web-NextGen/issues)

---

## TL;DR

If you're already running CWA, change one line and run two commands:

```yaml
services:
  calibre-web-automated:
-   image: crocodilestick/calibre-web-automated:latest
+   image: ghcr.io/new-usemame/calibre-web-nextgen:latest
```

```bash
docker compose pull && docker compose up -d
```

Library, settings, users, OAuth, KOReader sync — all carry over. Nothing to migrate.

If you're brand-new and just want a working library: jump to **[Quick start](#quick-start)** below. ☕

---

## Table of contents

- [Why this fork](#why-this-fork)
- [What you get out of the box](#what-you-get-out-of-the-box)
- [Quick start](#quick-start) — 3-minute install
- [Full Docker Compose setup](#full-docker-compose-setup)
- [First run](#first-run)
- [Migrating](#migrating)
  - [From upstream CWA](#from-upstream-cwa)
  - [From stock Calibre-Web](#from-stock-calibre-web)
- [Common configurations](#common-configurations)
  - [Network shares (NFS, SMB, ZFS)](#network-shares-nfs-smb-zfs)
  - [Reverse proxy / Cloudflare Tunnel](#reverse-proxy--cloudflare-tunnel)
  - [Hardcover metadata provider](#hardcover-metadata-provider)
  - [KOReader sync](#koreader-sync)
- [Troubleshooting](#troubleshooting)
- [What's different from upstream](#whats-different-from-upstream)
- [Help, bug reports, contributing](#help-bug-reports-contributing)
- [Credits](#credits)

---

## Why this fork

Upstream CWA is a great project. The maintainer hasn't merged a PR in months, and the queue of community-written fixes has been piling up. That's how you end up with bugs like:

- Safari users couldn't search metadata at all (silent 400 since 4.0.6)
- Cover saves from Hardcover/Google Books returning a vague "not a valid image" error
- Generate Kobo Auth Token returning a blank page
- Several admin routes that were unauthenticated and shouldn't have been

This fork picks the safe community fixes out of upstream's backlog, ships them in regular releases, and writes fresh fixes for high-impact bugs that don't have an open PR yet. Same software, same data layout, same UI, same compose file. **We don't own upstream and we're not trying to take over** — every backported patch is mergeable back if upstream review resumes. Door's open.

---

## What you get out of the box

Every feature CWA upstream has — plus everything upstream `:latest` is missing because review's been quiet:

- **Safari fixes**: metadata search and book delete both work again
- **Cover saves work**: Hardcover, Google Books, iTunes, Open Library covers all save and persist
- **Kobo**: bookmark sync no longer crashes on missing `Location`; auth-token IDOR (#1303) closed; "blank page on generate token" (#1328) fixed
- **Reverse proxy**: user-profile updates honor path prefix
- **Docker**: healthcheck stops tripping orchestrators on `/ → /login` 302
- **OPDS**: `.cbr` / `.cbz` use IANA mimetypes (less pickier readers)
- **Security**: 14 admin/log routes that were silently unauthenticated are now `@admin_required`; cover-enforcer shell-injection patched
- **Higher-resolution covers**: Libra Color and other high-DPI e-readers get proper-sized covers from Google Books, Amazon, and an iTunes-backed booster
- **Translations**: backlog of community translation PRs merged (ja, fr, cs, hu, zh_Hans, zh_Hant, …)
- **DNB metadata provider**: no longer hangs in synchronous cover-validation
- **Dark theme + magic-shelf ordering** plus a stack of small UX fixes

Per-release detail: [Releases](https://github.com/new-usemame/Calibre-Web-NextGen/releases). Full divergence list: [`CHANGES-vs-upstream.md`](CHANGES-vs-upstream.md).

---

## Quick start

You need: **Docker** + **Docker Compose**. That's it.

1. Make a folder for your library:

   ```bash
   mkdir -p ~/calibre-web/{config,library,ingest}
   cd ~/calibre-web
   ```

2. Drop this `docker-compose.yml` in there:

   ```yaml
   services:
     calibre-web:
       image: ghcr.io/new-usemame/calibre-web-nextgen:latest
       container_name: calibre-web
       environment:
         - PUID=1000
         - PGID=1000
         - TZ=America/New_York   # change to your timezone
       volumes:
         - ./config:/config            # settings, user db, logs
         - ./library:/calibre-library  # your books live here
         - ./ingest:/cwa-book-ingest   # drop new books here to import
       ports:
         - 8083:8083
       restart: unless-stopped
   ```

3. Start it:

   ```bash
   docker compose up -d
   ```

4. Open `http://localhost:8083`, log in with **`admin` / `admin123`**, change the password.

Done. Drop an `.epub` into `./ingest/` and it'll show up in your library within a few seconds.

> **Tip — file ownership matters.** Files in your library and ingest folders should be owned by your user (UID 1000 by default), not `root`. If you see permission errors after restoring from backup or copying files in as root, run once: `sudo chown -R 1000:1000 ~/calibre-web`.

---

## Full Docker Compose setup

Here's a more complete compose file with every option commented:

```yaml
services:
  calibre-web:
    image: ghcr.io/new-usemame/calibre-web-nextgen:latest
    container_name: calibre-web
    environment:
      # Match your host user/group so files in your library
      # are writable from both the container and the host.
      - PUID=1000
      - PGID=1000

      # https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
      - TZ=America/New_York

      # Override the in-container port if you need to.
      # If you set this below 1024, also uncomment cap_add below.
      - CWA_PORT_OVERRIDE=8083

      # Set this if your /config or /calibre-library volumes are
      # on an NFS or SMB share. See "Network shares" below for what
      # this changes.
      - NETWORK_SHARE_MODE=false

      # If you sit behind multiple proxies (e.g. Cloudflare Tunnel
      # → nginx → CWA), set this to the total proxy count so session
      # protection sees the right client IP. Default 1.
      - TRUSTED_PROXY_COUNT=1

      # Optional: Hardcover API token for the Hardcover metadata
      # provider. Free, sign up at https://hardcover.app/account/api
      # - HARDCOVER_TOKEN=eyJhbGciOiJIUzI1NiI…

    volumes:
      # Settings, user database, logs. Empty folder for new installs;
      # for existing CWA users, point at your existing /config.
      - /path/to/config:/config

      # Your Calibre library. New install? Use an empty folder and
      # CWA will set one up. Existing user? Point at the folder
      # containing your metadata.db.
      - /path/to/library:/calibre-library

      # Drop new books in here to import them. WARNING: files in
      # this folder are DELETED after processing. Don't point this
      # at a folder you also use as long-term storage.
      - /path/to/ingest:/cwa-book-ingest

      # Optional: bind your existing Calibre plugins folder
      # - /path/to/calibre-plugins:/config/.config/calibre/plugins

    ports:
      - 8083:8083

    # Uncomment if CWA_PORT_OVERRIDE is below 1024.
    # cap_add:
    #   - NET_BIND_SERVICE

    restart: unless-stopped
```

### What goes in each volume

| Volume | What it is | Notes |
|---|---|---|
| `/config` | App settings, user accounts, OAuth tokens, KOReader sync state, logs | Empty folder for new installs. Carry over from CWA verbatim. |
| `/calibre-library` | Your books + Calibre's `metadata.db` | If empty, CWA creates a fresh library. If it has multiple `metadata.db` files inside, CWA picks the largest. |
| `/cwa-book-ingest` | Drop zone for new books | **Files here are deleted after processing.** Don't park books here long-term. |

> **Don't nest the binds.** All three should be separate top-level folders. Putting `ingest` inside `library` causes weird recursive behavior and ingest loops.

---

## First run

1. **Open the UI** at `http://your-host:8083`.
2. **Log in** with `admin` / `admin123`.
3. **Change the admin password** (Profile → Account).
4. Go to **Admin → Edit Basic Configuration → Feature Configuration** and enable **Allow Uploads**. (Without this, the metadata-fetch and cover-from-URL features can't write to your library.)
5. **Drop a book** into your ingest folder. Watch it appear in the library within a few seconds.

That's it. The Admin → Settings panel has lots of optional toggles (auto-convert formats, automatic backups, EPUB fixer, KOReader sync, OAuth, etc.) — the [upstream wiki](https://github.com/crocodilestick/Calibre-Web-Automated/wiki) is the source of truth for those, since this fork doesn't change them.

---

## Migrating

### From upstream CWA

One line change. Stop the container, swap the image, start it.

```diff
- image: crocodilestick/calibre-web-automated:latest
+ image: ghcr.io/new-usemame/calibre-web-nextgen:latest
```

```bash
docker compose pull && docker compose up -d
```

Same data, same settings, same users, same OAuth tokens, same KOReader sync state. If anything breaks, swap back — the data format is identical.

### From stock Calibre-Web

1. Stop your existing Calibre-Web container.
2. In your new compose file, point `/config` at the same `/config` folder you used for Calibre-Web.
3. Whatever you bound as `/books` in Calibre-Web should be bound as `/calibre-library` here.
4. Pick an empty folder for `/cwa-book-ingest` (it's CWA-specific, no equivalent in stock CW).
5. Start the container.

All your users, settings, and shelves carry over. The first launch will take a few extra seconds while CWA registers itself with the existing app database.

---

## Common configurations

### Network shares (NFS, SMB, ZFS)

If your `/config` or `/calibre-library` volumes live on a network share, set:

```yaml
- NETWORK_SHARE_MODE=true
```

This:
- Disables SQLite WAL mode (NFS/SMB don't reliably support it → "database is locked" errors)
- Skips the recursive ownership-fix step at startup (it's slow on NFS and often fails on SMB)
- Switches the ingest watcher from inotify to polling (network-FS inotify events are unreliable)

This is tested and supported. It's a few seconds slower on ingest but otherwise behaves the same.

> **Files owned by root after copy?** This fork already chowns files back to your `PUID:PGID` after each metadata-change cycle, but if you've copied files in as root before upgrading, run once: `docker exec calibre-web chown -R abc:abc /calibre-library` (replace `abc` with your `PUID` user if you've customized).

### Reverse proxy / Cloudflare Tunnel

If you sit behind multiple proxies — e.g. **Cloudflare Tunnel → nginx → CWA** — tell CWA how many proxies are in front of it:

```yaml
- TRUSTED_PROXY_COUNT=2
```

Without this, CWA may see different client IPs across requests and trigger "Session protection" warnings, forcing re-login on every page change. Default is `1`. Set to your actual proxy depth.

### Hardcover metadata provider

[Hardcover](https://hardcover.app/) is a great free metadata provider. To enable it:

1. Sign up at https://hardcover.app and grab an API token at https://hardcover.app/account/api.
2. Add to your compose env:

   ```yaml
   - HARDCOVER_TOKEN=eyJhbGciOiJIUzI1NiI...
   ```

   Or paste it into **Admin → Edit Basic Configuration → Hardcover API Key** in the UI.
3. Restart the container.

Now Hardcover shows up in the Fetch Metadata modal. (This fork includes a fix for "cover save shows blank/reverts on refresh" that affected stock CWA + Hardcover.)

### KOReader sync

CWA has built-in KOReader progress sync — no separate kosync server needed.

1. In KOReader, install the CWA plugin: visit `http://your-cwa:8083/kosync` for download + install instructions.
2. In KOReader, point the plugin at `http://your-cwa:8083` and log in with your CWA username + password.
3. Read on any device. Your progress syncs back to CWA, and from there to Kobo if you have Kobo sync turned on.

---

## Troubleshooting

### "Cover-file is not a valid image file, or could not be stored"

Was the #1 user complaint on upstream's tracker. **Fixed in v4.0.13+**.

If you're still seeing it, you probably have `root:root`-owned book directories from a pre-fix install. Run once:

```bash
docker exec calibre-web chown -R abc:abc /calibre-library
```

### "Generate Kobo Auth Token" returns a blank page

**Fixed in v4.0.14+.** Upgrade to the latest image.

### Database is locked errors / app frozen

If your library is on a network share, set `NETWORK_SHARE_MODE=true` (see [Network shares](#network-shares-nfs-smb-zfs) above). If on local disk, this usually means a previous container shutdown was unclean — restart Docker, then the container.

### Session-protection warnings, forced re-login on every nav

Set `TRUSTED_PROXY_COUNT` to match your proxy depth. See [Reverse proxy](#reverse-proxy--cloudflare-tunnel).

### Books in `/cwa-book-ingest` aren't being picked up

Three common causes:
1. **Files owned by root.** Make sure ingest files are owned by your `PUID:PGID` user.
2. **Watcher missed them.** Click the **Refresh Library** button on the navbar — it does a one-shot scan.
3. **Format isn't allowed.** Check **Admin → CWA Settings → Ingest** for your allowed formats.

### Default login isn't working

The defaults are **`admin`** / **`admin123`** (lowercase). If you've already logged in once and changed the password but forgot it, stop the container, delete `config/app.db`, and restart — this resets the database. (You'll lose all user accounts, but the library itself is untouched.)

### Something else is broken

Check the [open issues](https://github.com/new-usemame/Calibre-Web-NextGen/issues) on this fork or [open a new one](https://github.com/new-usemame/Calibre-Web-NextGen/issues/new). Include:
- Output of `docker exec calibre-web cat /app/CWA_STABLE_RELEASE` (the version you're on)
- Last 50 lines of `docker logs calibre-web 2>&1 | tail -50`
- What you did and what you expected to happen

---

## What's different from upstream

| | Upstream CWA `:latest` | This fork `:latest` |
|---|---|---|
| Cover saves from Hardcover/Google etc. | ❌ "Not a valid image" error | ✅ Saves and persists |
| Generate Kobo Auth Token | ❌ Blank page | ✅ Works |
| Safari metadata search | ❌ Silent 400 | ✅ Works |
| Safari book delete button | ❌ Killed by Feb-4 commit | ✅ Works |
| Kobo bookmark sync (missing Location) | ❌ Crashes | ✅ Tolerates |
| `/kobo_auth/generate_auth_token` IDOR | ❌ Open (any user can mint another user's token) | ✅ Closed |
| Reverse-proxy user-profile updates | ❌ Drops path prefix | ✅ Honors `getPath()` |
| Docker healthcheck on `/ → /login` 302 | ❌ Trips on `curl -f` | ✅ `-fsL` follows |
| `.cbr`/`.cbz` OPDS mimetypes | ❌ Non-IANA | ✅ IANA-compliant |
| Cover resolution on Libra Color etc. | ❌ Often 290×475 (Hardcover thumbnail size) | ✅ 1000×1500+ via booster |
| Admin routes (cwa_logs, convert, epub_fixer) | ❌ 14 unauthenticated | ✅ All `@admin_required` |
| Translations: ja/fr/cs/hu/zh_Hans/zh_Hant | ❌ Stuck in PRs | ✅ Merged |

Bug fixes are conservative; we don't add features upstream wouldn't accept. If a backport touches anything risky (auth, schema, deps) it gets manual review before merging.

---

## Help, bug reports, contributing

- **Bug?** [Open an issue](https://github.com/new-usemame/Calibre-Web-NextGen/issues/new). Reproduction steps + version tag + `docker logs` snippet = ideal.
- **PR?** PRs welcome. The bar is "doesn't break anything that works today" — if it touches auth, schema, or deps, expect a closer review. Per-commit identity is enforced (`new-usemame` for fork-original, original handle for backports).
- **Original CWA contributors with stalled PRs upstream?** Reach out — happy to ship your work here while keeping it mergeable back to upstream.
- **Real-time chat:** the upstream [CWA Discord](https://discord.gg/EjgSeek94R) is still the place. We're not trying to fragment the community.

Governance: [`GOVERNANCE.md`](GOVERNANCE.md). Contributing details: [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Credits

This fork stands on a tower of work by other people:

- **Calibre-Web-Automated** — [@crocodilestick](https://github.com/crocodilestick) and CWA contributors. The core software this fork builds on. If upstream review resumes, every patch in this fork is cleanly cherry-pickable back.
- **Calibre-Web** — [@janeczku](https://github.com/janeczku) and the Calibre-Web team. The web UI that makes self-hosted ebook libraries actually pleasant.
- **Calibre** — [@kovidgoyal](https://github.com/kovidgoyal). The 20-year foundation under all of this.

Every backported patch in this fork is credited to its original author by GitHub handle in the commit message and the [`CHANGES-vs-upstream.md`](CHANGES-vs-upstream.md) file.

If you find this fork useful and want to support upstream's continued development, [@crocodilestick has a Ko-fi](https://ko-fi.com/crocodilestick).

---

*License: GPL-3.0-or-later. See [`LICENSE`](LICENSE).*
