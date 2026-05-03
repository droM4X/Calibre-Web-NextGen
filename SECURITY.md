# Security

## Reporting

Report suspected vulnerabilities by opening a private security advisory at
<https://github.com/new-usemame/Calibre-Web-NextGen/security/advisories/new>.

Please do not file public issues for vulnerabilities.

Include affected version (container tag / commit SHA), reproduction steps or PoC,
impact assessment, and a suggested fix if you have one.

## Response timeline

- **Acknowledgement**: within 72 hours.
- **Initial assessment + severity**: within 7 days.
- **Patch + release**: within 30 days for High/Critical, 90 days for Medium/Low.
- **Public advisory**: published with the patch release once users have had time to update.

## Coordinated disclosure with upstream

Because this fork tracks `crocodilestick/Calibre-Web-Automated`, a vulnerability we
find in shared code may also affect upstream users. Default flow:

1. Patch the fork.
2. Notify upstream privately (GitHub security advisory or maintainer email)
   with the patch link + technical detail. Same 30/90-day window before public.
3. Publish our advisory after the disclosure window — even if upstream
   hasn't acted, so users on the upstream image know to upgrade.

If a vulnerability is already public (e.g. an unprivileged user filed a public
bug describing it before reporting privately), we patch and disclose immediately
— withholding a public fix from a public bug helps no one.

## Scope

**In scope**: authentication bypass, privilege escalation, IDOR, RCE,
command injection, path traversal, SQLi, SSRF, XXE, stored/reflected XSS
affecting other users, sensitive-data exposure, container escape.

**Out of scope**: physical access required, third-party software vulnerabilities
not exposed by our usage, self-XSS, clickjacking on pages with no sensitive
actions, DoS requiring authenticated admin, automated-scanner output without
analysis.

## Resolved security advisories

### v4.0.7

- **Kobo IDOR** (closes upstream issue [#1303](https://github.com/crocodilestick/Calibre-Web-Automated/issues/1303)):
  `/kobo_auth/generate_auth_token` and `/deleteauthtoken` accepted arbitrary
  `user_id` in the request body, allowing any authenticated user to mint or
  revoke another user's Kobo auth token. Patched in
  [`9f50bb2`](https://github.com/new-usemame/Calibre-Web-NextGen/commit/9f50bb2).
  Severity: HIGH.
- **14 unauthenticated CWA admin/log routes** (fork audit): the `cwa_logs`,
  `convert_library`, and `epub_fixer` blueprints exposed log download/read,
  conversion start/cancel/status, and epub-fixer start/cancel/status routes
  without auth decorators. Patched in
  [`09bf581`](https://github.com/new-usemame/Calibre-Web-NextGen/commit/09bf581).
  Severity: HIGH. Privately disclosed to upstream.
- **`cover_enforcer.py` shell injection** (fork audit):
  `os.system(f'cp "{path}" "{dst}"')` interpolated Calibre book paths
  (containing user-controlled titles) into a shell, allowing command execution
  as the `abc` user via crafted book metadata. Patched in
  [`b70fb53`](https://github.com/new-usemame/Calibre-Web-NextGen/commit/b70fb53).
  Severity: MEDIUM. Privately disclosed to upstream.

## Credit

Reporters credited by handle in advisories unless they request otherwise.
Researchers who follow responsible disclosure get credited; public 0-day drops
get patched but not credited.

## Verifying release artifacts

Every released image is signed with [cosign](https://github.com/sigstore/cosign)
keyless (Sigstore Fulcio + Rekor) and carries a SLSA build-provenance attestation.

Verify a pulled image:

```bash
cosign verify ghcr.io/new-usemame/calibre-web-nextgen:vX.Y.Z \
  --certificate-identity-regexp '^https://github.com/new-usemame/Calibre-Web-NextGen/.github/workflows/' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

Inspect the build provenance:

```bash
gh attestation verify \
  oci://ghcr.io/new-usemame/calibre-web-nextgen:vX.Y.Z \
  --owner new-usemame
```

A passing verification means the image was built by this repo's release workflow,
on a GitHub-hosted runner, from the commit referenced in the attestation.

The release workflow signs the manifest list **and** each per-platform image
recursively, so `cosign verify --platform linux/amd64` and
`cosign verify --platform linux/arm64` both succeed.

### SBOM scope

SBOM and per-arch SLSA provenance attestations live as OCI referrers on the
**per-architecture digests**, not the manifest-list tag. To inspect the SBOM
for a specific platform:

```bash
# Resolve per-arch digest first
docker buildx imagetools inspect ghcr.io/new-usemame/calibre-web-nextgen:vX.Y.Z \
  --format '{{ range .Manifest.Manifests }}{{ .Platform.Architecture }} {{ .Digest }}{{ "\n" }}{{ end }}'

# Then download the SBOM
cosign download sbom \
  ghcr.io/new-usemame/calibre-web-nextgen@<per-arch-digest>
```

The top-level `gh attestation verify` covers the manifest list itself.

## Supported versions

The latest published GitHub Release receives security backports. Older releases
are best-effort.
