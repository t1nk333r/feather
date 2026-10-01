# Uploading a build to Feather from a script or an agent

**Use an API token and `POST /api/publish`.** It is one endpoint for both
platforms, it reads the platform, bundle ID / package and version from the
file, and it creates the app if it is new. Section 0 is all most clients need;
sections 1–3 document the older session-cookie routes the admin UI uses.

## 0. Token + `/api/publish` (recommended)

Create a token in the admin page: **Source** tab → *API tokens*. It is shown
once; only its SHA-256 is stored. A token can publish, edit app details and
read (`/api/publish`, `/api/app-details`, `/api/android/apps`,
`/api/android/status`); it cannot delete apps, change settings, or manage
tokens. Optionally **limit it to named apps** (bundle IDs / package names —
anything else answers `403`) and give it an **expiry** in days. Revoke it on
the same page.

```bash
# upload a file (field name: file; ipaFile/apkFile also accepted)
curl -fsS -H "Authorization: Bearer $FEATHER_TOKEN" \
  -F file=@app-release.apk \
  -F name="DAVKeep" -F developerName="Me" -F summary="CardDAV contacts sync" \
  -F description="Longer text…" -F license="GPL-3.0-only" \
  -F sourceCode="https://github.com/me/davkeep" -F categories="Connectivity,Sync" \
  -F whatsNew="$(cat changelogs/42.txt)" \
  https://apps.example.com/api/publish

# or have Feather download it
curl -fsS -H "Authorization: Bearer $FEATHER_TOKEN" -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/builds/app-release.apk", "whatsNew": "Bug fixes"}' \
  https://apps.example.com/api/publish
```

| Field | Default | Notes |
|---|---|---|
| `file` or `url` | — | exactly one; `.ipa` or `.apk` (detected from the archive, not the name) |
| `whatsNew` | — | this version's release notes (≤4000). iOS: the version's `localizedDescription`. Android: `metadata/<pkg>/en-US/changelogs/<versionCode>.txt`; F-Droid clients show the newest version's notes |
| `name` (≤50), `developerName` (≤100), `summary` (≤80), `description` (≤4000) | from the file / `Unknown` | app details — applied only when the app is **created**; change them later with `/api/app-details`. iOS uses `summary` as the subtitle. With no `summary`, the first line of `description` is used |
| `license`, `website`, `sourceCode` (http(s) URLs), `categories` (comma list or array, ≤10) | — | Android only (F-Droid metadata); sent for an iOS app they come back as a warning |
| `createIfMissing` | `true` | `false` refuses (404) an app that is not already published |

Response (`200`):

```json
{"success": true, "platform": "android", "id": "org.example.davkeep", "version": "1.4",
 "build": 42, "added": true, "created": false, "name": "DAVKeep", "pending": true,
 "downloadURL": "https://apps.example.com/fdroid/repo/org.example.davkeep_42.apk",
 "warnings": [], "message": "Added org.example.davkeep versionCode 42; …"}
```

**Read `warnings`.** They report what was accepted but not ideal: a
**debuggable APK** (published, because fdroidserver only warns — set
`APK_REJECT_DEBUGGABLE=true` on the server to refuse them), app details sent
for an app that already exists (ignored), and Android-only fields sent for
iOS.

`added: false` means that exact version was already published (a safe retry).
Android responses carry `"pending": true`: the APK is stored, and it appears in
the F-Droid index after the sidecar's next rebuild. Errors are
`{"success": false, "error": "..."}` with `400` (not a valid IPA/APK, bad
field), `401` (missing, revoked or expired token), `403` (token limited to
other apps), `404` (`createIfMissing: false`), or `413` (an upload over
`MAX_CONTENT_LENGTH`; an oversized `url` download is a `400`).

### Changing an existing app's details

```bash
curl -fsS -H "Authorization: Bearer $FEATHER_TOKEN" -H "Content-Type: application/json" \
  -d '{"id": "org.example.davkeep", "summary": "CardDAV contacts sync", "license": "GPL-3.0-only"}' \
  https://apps.example.com/api/app-details
```

`id` is the bundle ID or package; the same detail fields as above; only the
fields sent change. To add release notes to a version that is already
published, send `whatsNew` with `version` (iOS version string, Android
versionCode) — the binary is untouched. Returns the updated app (iOS) or F-Droid metadata
(Android, `pending: true` until the next index rebuild).

### MCP server for agents

`scripts/feather_mcp.py` wraps the same API as an MCP stdio server. Standard
library only (Python 3.9+), so copy the one file to wherever the agent runs.

```bash
claude mcp add feather \
  --env FEATHER_URL=https://apps.example.com \
  --env FEATHER_TOKEN=ftr_... \
  -- python3 /path/to/feather_mcp.py
```

Any MCP client works the same way (`command: python3`, `args: [/path/to/feather_mcp.py]`,
those two env vars). Tools:

| Tool | Does |
|---|---|
| `publish_app` | `path` (local file, streamed) or `url`; optional `whats_new`, `create_if_missing`, and the app details `name`, `developer_name`, `summary`, `description`, `license`, `website`, `source_code`, `categories` (used when the app is created) |
| `update_app` | `id` plus any of the app-detail fields; only those change. `whats_new` + `version` sets notes on an already-published version |
| `list_apps` | `platform`: `ios`, `android` or `all` |
| `get_app` | by bundle ID or package |
| `repo_status` | `server_version` (the commit the server was built from), F-Droid subscribe URL, fingerprint, last index build, rejected APKs |

---

The sections below are the session-cookie contract the admin UI uses. Field
names there are **not guessable** (`ipaFile`/`apkFile`, not `file`) and a wrong
one fails as a validation error that reads like a missing file.

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
