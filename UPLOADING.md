# Uploading a build to Feather from a script or an agent

The write API is the same one the admin UI uses. There is no API token: every
mutating route is gated by a session cookie obtained from `POST /api/login`
(plan 010). This file is the contract an unattended client has to satisfy —
written down because the field names are **not guessable** and a wrong one fails
as a validation error that reads like a missing file.

Everything below was verified against a running instance on 2026-09-18 with a
real 3,381,621-byte APK and a real 6,125,156-byte IPA.

## 1. Sign in, and prove it took

```bash
JAR=$(mktemp)
curl -s -c "$JAR" -H 'Content-Type: application/json' \
  --data-binary '{"password":"…"}' "$HOST/api/login"
curl -s -b "$JAR" "$HOST/api/session"        # must answer {"authed":true}
```

Read the password from the environment or a `0600` file and post it in the JSON
body. **Never pass it as a command-line argument** — argv is world-readable via
`/proc/<pid>/cmdline` and lands in shell history.

A `200` from `/api/login` is not by itself proof of a usable session: check
`/api/session` before uploading, or a later `401` will look like a broken route.

There is no login rate limiting and no CSRF token (`SameSite=Lax` is the
defence; see `plans/010-login-session-auth.md`). Treat the password as the whole
of the authorisation story and scope it accordingly.

## 2. The multipart contract

| Endpoint | File field | Required fields | Notes |
|---|---|---|---|
| `POST /api/add-app` | `ipaFile` | `bundleIdentifier` | Also `name`, `developerName`, `version`, `buildVersion`, `localizedDescription`, `minOSVersion` (default `14.0`), `iconURL`, `privacy` (JSON object), `iconFile` |
| `POST /api/add-version` | `ipaFile` | `bundleIdentifier`, `version` | Also `buildVersion`, `minOSVersion`. `name`/`developerName` are **ignored** here |
| `POST /api/android/add-apk` | `apkFile` | — | Package, version name and version code are read from the APK; `package` is an optional assertion |

Both IPA routes also accept a JSON body instead of multipart, and either route
can fetch the artifact itself with `downloadFromUrl=true` plus `downloadURL=…`
rather than uploading bytes.

**The two failures worth recognising:**

```
-F "file=@build.apk"   -> 400 {"error":"Either an APK file or downloadFromUrl is required"}
-F "file=@build.ipa"   -> 400 {"error":"Either IPA file or download URL is required"}
```

The field is `apkFile` / `ipaFile`. The error text names the *concept*, not the
field, so a misnamed field is easy to misread as a missing artifact.

**`add-version` does not create an app.** A bundle identifier the catalog has
never seen must go through `add-app` first: `add-version` answers
`400 {"error":"App not found"}`, and the `name`/`developerName` you send it are
dropped. Re-uploading an APK is safe and idempotent — the second attempt answers
`200 {"added": false, "message": "Already present: … versionCode 63"}` (plan 081).

`MAX_CONTENT_LENGTH` defaults to 2 GiB (`.env.example`). A larger artifact is
rejected by Werkzeug before any route runs.

## 3. Confirm the publish, do not trust the 200

**iOS** — read the catalog back:

```bash
curl -s "$HOST/source.json" | jq '.apps[] | {id: .bundleIdentifier,
  versions: [.versions[] | {version, size}]}'
```

A version with a `downloadURL` but `size: 0` is a failed upload that reported
success.

**Android** — the upload answers `"pending": true`, which means *accepted, not
published*:

```json
{"success": true, "added": true, "package": "dev.example.app",
 "versionCode": 63, "pending": true}
```

Feather only writes the APK plus `metadata/<package>.yml` and touches
`.update-requested`. The `fdroid-index` sidecar (Compose profile `android`) owns
JAR-signing the index, so the package appears only after it rebuilds:

```bash
curl -s "$HOST/fdroid/repo/index-v2.json" | jq '.packages | keys'
```

If that stays empty, the sidecar is not running — verified failure mode, not a
hypothetical. Re-uploading will not fix it; the second upload is correctly
reported as already present.

## 4. The credential-free alternative

If the client should not hold the admin password, do not upload at all: publish
a GitHub or GitLab release and let Feather pull it. The release importer — the
`release-import` Compose service, the admin UI's "Import from Repo", and the
auto-import watcher — all share one engine that infers platform from the asset's
file extension and can publish an IPA *and* an APK from a single release.

Selection allows **at most one matching asset per platform**, so a multi-ABI
Android release needs `assetMatchMode: regex` (e.g. `.*arm64-v8a\.apk`) or the
job is refused as ambiguous rather than publishing the wrong binary. See
`plans/029-cron-release-imports.md`, `plans/083-engine-apk-support.md` and
`plans/085-regex-asset-matching.md`.

This leaves the release itself as the audit trail, which the direct upload path
does not have.

## 5. What this API does not do

- **It does not verify provenance.** Validation proves the bytes are a
  well-formed IPA/APK (`scripts/ipa_inspection.py`, `scripts/apk_inspection.py`),
  not that the build is safe or unmodified. Whoever can reach the write API
  decides what installs on subscribed devices.
- **It does not sign iOS artifacts, and should not be asked to.** AltStore and
  SideStore re-sign on install with the subscriber's own Apple identity, so an
  unsigned IPA is the correct thing to upload. The full primary-source argument
  is in `plans/086-research-signing-ipas-on-upload.md`; the certificate panel
  (`POST /api/certificate/inspect`, plan 087) inspects a signing identity but
  deliberately signs nothing.
- **It does not serialize across clients.** Catalog mutations take an in-process
  lock and the deployment is single-process (plan 052), so two agents uploading
  concurrently are safe at the catalog level — but nothing coordinates *which*
  build wins if they publish the same version.
