# Uploading a build to Feather from a script or an agent

**Use an API token and `POST /api/publish`.** One endpoint serves both
platforms. It reads the platform, the bundle ID or package, and the version
from the file, and it creates the app if it is new. Most clients need only
section 0. Sections 1 to 3 document the older session-cookie routes that the
admin UI uses.

## 0. A token and `/api/publish` (recommended)

Create a token in the admin page, in the *API tokens* section of the Source
tab. The page shows the token once, and the server stores only its SHA-256.
A token can publish, edit app details and read, through `/api/publish`,
`/api/app-details`, `/api/android/apps` and `/api/android/status`. It cannot
delete apps, change settings, or manage tokens. You can limit a token to
named apps, given as bundle IDs or package names. Any other app then answers
`403`. You can also give the token an expiry in days. Revoke it on the same
page.

```bash
# upload a file (field name: file; ipaFile/apkFile also accepted)
curl -fsS -H "Authorization: Bearer $FEATHER_TOKEN" \
  -F file=@app-release.apk \
  -F name="DAVKeep" -F developerName="Me" -F summary="CardDAV contacts sync" \
  -F description="Longer text…" -F license="GPL-3.0-only" \
  -F sourceCode="https://github.com/me/davkeep" -F categories="Connectivity,Sync" \
  -F whatsNew="$(cat changelogs/42.txt)" \
  https://apps.example.com/api/publish

# `url` (Feather downloads the file) works only with an admin session: a
# server-side fetch on a token's behalf would let a token reach hosts only
# the server can see. With a token, upload the file; the MCP server's
# publish_app does that for you when given a url.
```

| Field | Default | Notes |
|---|---|---|
| `file` or `url` | none | Send exactly one. It must be an `.ipa` or `.apk`, detected from the archive, not the name. With an API token, Feather refuses `url` with 400, so upload the file instead |
| `whatsNew` | none | This version's release notes, up to 4000 characters. On iOS they become the version's `localizedDescription`. On Android they go to `metadata/<pkg>/en-US/changelogs/<versionCode>.txt`, and F-Droid clients show the newest version's notes |
| `name` (up to 50), `developerName` (up to 100), `summary` (up to 80), `description` (up to 4000) | from the file, or `Unknown` | App details. Feather applies them **only when it creates the app**. Change them later with `/api/app-details`. iOS uses `summary` as the subtitle. Without a `summary`, Feather uses the first line of `description` |
| `license`, `website`, `sourceCode` (http(s) URLs), `categories` (comma-separated list or array, up to 10) | none | Android only, for the F-Droid metadata. If you send them for an iOS app, the response reports them as a warning |
| `createIfMissing` | `true` | With `false`, the call refuses an app that is not published yet and answers 404 |

Response (`200`):

```json
{"success": true, "platform": "android", "id": "org.example.davkeep", "version": "1.4",
 "build": 42, "added": true, "created": false, "name": "DAVKeep", "pending": true,
 "downloadURL": "https://apps.example.com/fdroid/repo/org.example.davkeep_42.apk",
 "pruned": [], "warnings": [], "message": "Added org.example.davkeep versionCode 42; …"}
```

**Read `warnings`.** They report input that Feather accepted but that is not
ideal:

- A debuggable APK. Feather publishes it, because fdroidserver only warns
  about debuggable APKs. Set `APK_REJECT_DEBUGGABLE=true` on the server to
  refuse them.
- App details sent for an app that already exists. Feather ignores them.
- Android-only fields sent for an iOS app.

`added: false` means that exact version was already published, so retrying
is safe.

**`pruned` lists the versions that the app's retention policy deleted**
because this upload became its newest version. An entry looks like
`[{"version": "1.2"}]` for iOS and `[{"versionCode": 41, "versionName": "1.3"}]`
for Android. `pruned` is `[]` unless an admin gave the app a policy, either
with *Keep previous versions* in the admin UI or with `POST /api/retention`
and a session. Tokens cannot set a policy. An upload that is not the newest
version, such as a backport, never prunes. On Android, all APKs that share a
versionName count as one version, so retention keeps or removes per-ABI APKs
together.

**iOS permissions come from the binary.** For every IPA it stores, Feather
adds two kinds of key to the app's `appPermissions`: the entitlement keys
that the app and extension executables are signed with, and the
`NS…UsageDescription` keys from their `Info.plist`s. AltStore and SideStore
refuse to install an app from a source that does not list them all. Feather
only adds keys and never removes them. It never lists
`application-identifier` or `com.apple.developer.team-identifier`, because
both installers ignore them.

Feather reads the keys from the XML entitlements in each executable's code
signature, which `codesign`, `ldid` and `rcodesign` all write. It drops a key
longer than 256 characters and cuts privacy text to 2000 characters. An app
lists at most 256 entitlement keys and 64 privacy keys. Feather reads only
executables that are stored or compressed with deflate.

Sending an already-published IPA to `/api/publish` again declares whatever
permissions its app still lacks, and stores nothing else. An iOS
`added: false` response reports those keys in `permissionsAdded`, which is
`[]` when nothing was missing. The admin UI, the importers and the Telegram
bot stop at "already present" without reading the file, so a repeat through
them declares nothing.

Android responses carry `"pending": true`. The APK is stored, and it appears
in the F-Droid index after the sidecar's next rebuild.

Errors are `{"success": false, "error": "..."}` with one of these statuses:

- `400` for a file that is not a valid IPA/APK, or a bad field
- `401` for a missing, revoked or expired token
- `403` for a token limited to other apps
- `404` for `createIfMissing: false`
- `413` for an upload over `MAX_CONTENT_LENGTH` (an oversized `url` download
  is a `400`)
- `503` when the server could not store the file on disk or in Garage. Retry
  these.

Feather refuses with `400` a version or bundle ID that would share a storage
file name with an existing one, such as `2.0` and `2.0.`, instead of
overwriting the existing file. It stores a version older than the app's
newest, but that version never becomes `versions[0]`.

### Changing an existing app's details

```bash
curl -fsS -H "Authorization: Bearer $FEATHER_TOKEN" -H "Content-Type: application/json" \
  -d '{"id": "org.example.davkeep", "summary": "CardDAV contacts sync", "license": "GPL-3.0-only"}' \
  https://apps.example.com/api/app-details
```

`id` is the bundle ID or package. The call takes the same detail fields as
`/api/publish`, and only the fields you send change. To add release notes to
a version that is already published, send `whatsNew` with `version`. On iOS
`version` is the version string, and on Android it is the versionCode. The
binary stays untouched. The call returns the updated app on iOS. On Android
it returns the F-Droid metadata, with `pending: true` until the next index
rebuild. When the same `id` exists on both iOS and Android, send
`platform: "ios"` or `"android"`. Without it, the call answers `409`.

### MCP server for agents

`scripts/feather_mcp.py` wraps the same API in an MCP stdio server. It uses
only the standard library and needs Python 3.9 or later, so you can copy the
one file to wherever the agent runs.

```bash
claude mcp add feather \
  --env FEATHER_URL=https://apps.example.com \
  --env FEATHER_TOKEN=ftr_... \
  -- python3 /path/to/feather_mcp.py
```

Any MCP client works the same way, with `command: python3`,
`args: [/path/to/feather_mcp.py]` and the same two env vars. The server has
these tools:

| Tool | What it does |
|---|---|
| `publish_app` | Takes a `path` to a local file, which it streams, or a `url`, which it downloads on the agent's machine and then uploads. Optional arguments are `whats_new`, `create_if_missing`, and the app details `name`, `developer_name`, `summary`, `description`, `license`, `website`, `source_code` and `categories`, which apply when the app is created |
| `update_app` | Takes an `id`, plus `platform` when the id exists on both platforms, and any of the app-detail fields. Only those fields change. `whats_new` with `version` sets the notes on an already-published version |
| `list_apps` | Takes `platform`, one of `ios`, `android` or `all` |
| `get_app` | Looks up an app by bundle ID or package. An id on both platforms returns both, with `platform: "both"` |
| `repo_status` | Returns `server_version` (the commit the server was built from), the F-Droid subscribe URL and fingerprint, the last index build, and the rejected APKs |

---

The sections below describe the session-cookie contract that the admin UI
uses. Its field names are **not guessable**. The file field is `ipaFile` or
`apkFile`, not `file`, and a wrong field name fails with a validation error
that reads like a missing file.

Everything below was verified against a running instance on 2026-09-18 with a
real 3,381,621-byte APK and a real 6,125,156-byte IPA.

## 1. Sign in and check the session

```bash
JAR=$(mktemp)
curl -s -c "$JAR" -H 'Content-Type: application/json' \
  --data-binary '{"password":"…"}' "$HOST/api/login"
curl -s -b "$JAR" "$HOST/api/session"        # must answer {"authed":true}
```

Read the password from the environment or a `0600` file, and post it in the
JSON body. **Never pass it as a command-line argument.** Any user can read
argv through `/proc/<pid>/cmdline`, and it lands in shell history.

A `200` from `/api/login` does not prove by itself that the session works.
Check `/api/session` before uploading, or a later `401` will look like a
broken route.

Feather throttles logins. After 5 failed attempts from one client address
within 60 s, it answers `429` with `Retry-After`. Behind a reverse proxy
every client shares the proxy's address, so a script that keeps failing
locks out the admin UI too. Use an API token (section 0) for unattended
clients instead. There is no CSRF token. `SameSite=Lax` is the defence, as
`plans/010-login-session-auth.md` explains.

## 2. The multipart contract

| Endpoint | File field | Required fields | Notes |
|---|---|---|---|
| `POST /api/add-app` | `ipaFile` | `bundleIdentifier` | Also accepts `name`, `developerName`, `version`, `buildVersion`, `localizedDescription`, `minOSVersion` (default `14.0`), `iconURL`, `privacy` (JSON object) and `iconFile` |
| `POST /api/add-version` | `ipaFile` | `bundleIdentifier`, `version` | Also accepts `buildVersion` and `minOSVersion`. This route **ignores** `name` and `developerName` |
| `POST /api/android/add-apk` | `apkFile` | none | Feather reads the package, version name and version code from the APK. `package` is an optional assertion |

Both IPA routes also accept a JSON body instead of multipart. All three routes
can also fetch the artifact itself, given `downloadFromUrl=true` and
`downloadURL=…`, instead of receiving uploaded bytes.

Two failures are worth recognising:

```
-F "file=@build.apk"   -> 400 {"error":"Either an APK file or downloadFromUrl is required"}
-F "file=@build.ipa"   -> 400 {"error":"Either IPA file or download URL is required"}
```

The fields are `apkFile` and `ipaFile`. The error text names the missing
artifact, not the field, so a misnamed field is easy to mistake for a
missing artifact.

**`add-version` does not create an app.** A bundle identifier that the
catalog has never seen must go through `add-app` first. Otherwise
`add-version` answers `400 {"error":"App not found"}` and drops the
`name`/`developerName` you sent. Re-uploading an APK is safe and idempotent.
The second attempt answers
`200 {"added": false, "message": "Already present: … versionCode 63"}`
(plan 081).

`MAX_CONTENT_LENGTH` defaults to 2 GiB (`app.py`). Werkzeug rejects a
larger artifact before any route runs.

## 3. Confirm the publish instead of trusting the 200

For iOS, read the catalog back:

```bash
curl -s "$HOST/source.json" | jq '.apps[] | {id: .bundleIdentifier,
  versions: [.versions[] | {version, size}]}'
```

A version with a `downloadURL` but `size: 0` is a failed upload that reported
success.

For Android, the upload answers `"pending": true`, which means *accepted, not
published*:

```json
{"success": true, "added": true, "package": "dev.example.app",
 "versionCode": 63, "pending": true}
```

Feather only writes the APK and `metadata/<package>.yml`, and touches
`.update-requested`. The `fdroid-index` sidecar, in the Compose profile
`android`, does the JAR signing of the index, so the package appears only
after the sidecar rebuilds it:

```bash
curl -s "$HOST/fdroid/repo/index-v2.json" | jq '.packages | keys'
```

If that list stays empty, the sidecar is not running. This failure has
happened in practice. Re-uploading will not fix it, because Feather
correctly reports the second upload as already present.

## 4. The credential-free alternative

If the client should not hold the admin password, it does not have to upload
at all. Publish a GitHub or GitLab release and let Feather pull it. Three
parts of Feather import releases: the `release-import` Compose service, the
admin UI's "Import from repo", and the auto-import watcher. They share one
engine. It infers the platform from the asset's file extension, and it can
publish an IPA and an APK from a single release.

Selection allows **at most one matching asset per platform**. A multi-ABI
Android release therefore needs `assetMatchMode: regex`, for example
`.*arm64-v8a\.apk`. Otherwise the importer refuses the job as ambiguous
instead of publishing the wrong binary. See
`plans/029-cron-release-imports.md`, `plans/083-engine-apk-support.md` and
`plans/085-regex-asset-matching.md`.

Feather files an imported IPA under the bundle ID inside it. To keep a
patched build separate from its upstream app, set `catalogBundleIdentifier`
on the job. In the admin UI that field is *Publish under catalog ID*.
`bundleIdentifier` still checks the IPA's own ID. Only the watcher and
"Import from repo" have the field. The cron manifest does not. See
`plans/090-catalog-bundle-override.md`.

The release itself is then the audit trail, which a direct upload does not
have.

## 4b. Device backups (a phone keeping its own settings)

This is off unless the server sets `DEVICE_BACKUPS=true` (plan 088). Create
a token with *Kind* set to *Device backups only*, and put the app's package
in *Limit to apps*. The token reaches `/api/device-backups/<app>/…` for those
apps and nothing else. It cannot publish, read the catalogue, or delete.

```bash
# keep a backup (multipart `file`, a zip; `label` is the device's display name)
curl -fsS -H "Authorization: Bearer $TOKEN" -F file=@backup.zip -F label="Galaxy S24 Ultra" \
  https://feather.example.com/api/device-backups/dev.omalauncher/<device-id>
# -> {"success": true, "unchanged": false, "snapshot": {"name", "created", "size", "sha256"}}
#    unchanged: true means it matched the newest snapshot and nothing was stored

curl -fsS -H "Authorization: Bearer $TOKEN" https://feather.example.com/api/device-backups/dev.omalauncher
#    devices: id, label, snapshots, size, lastBackup
curl -fsS -H "Authorization: Bearer $TOKEN" https://feather.example.com/api/device-backups/dev.omalauncher/<device-id>
#    snapshots, newest first
curl -fsS -H "Authorization: Bearer $TOKEN" -o backup.zip \
  https://feather.example.com/api/device-backups/dev.omalauncher/<device-id>/<name>
#    X-Content-SHA256 carries the digest to check
```

A device ID is 1 to 64 characters from letters, digits, `.`, `_` and `-`,
and it starts with a letter or digit. The zip must not contain absolute paths,
backslash paths or `..` paths. An upload over `DEVICE_BACKUP_MAX_BYTES`
(256 MiB) answers 413. The server keeps the newest `DEVICE_BACKUP_KEEP` (30)
snapshots per device and at most `DEVICE_BACKUP_MAX_DEVICES` (20) devices
per app. Delete old devices in the admin UI.

## 5. What this API does not do

- **It does not verify provenance.** Validation by `scripts/ipa_inspection.py`
  and `scripts/apk_inspection.py` proves that the bytes are a well-formed
  IPA/APK. It does not prove that the build is safe or unmodified. Whoever
  can reach the write API decides what installs on subscribed devices.
- **It does not sign iOS artifacts, and should not be asked to.** AltStore and
  SideStore re-sign on install with the subscriber's own Apple identity, so an
  unsigned IPA is the correct thing to upload.
  `plans/086-research-signing-ipas-on-upload.md` has the full argument from
  primary sources. The certificate panel (`POST /api/certificate/inspect`,
  plan 087) inspects a signing identity but signs nothing, by design.
- **It does not serialize across clients.** Catalog mutations take an
  in-process lock, and the deployment is a single process (plan 052). Two
  agents uploading at the same time are therefore safe at the catalog level.
  But nothing decides *which* build wins if both publish the same version.
