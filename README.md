# Feather

Feather is a self-hosted app store for iOS and Android. One server and one admin page serve two kinds of subscribers.

- iPhones get an [AltStore](https://altstore.io) / Feather-compatible source (`/source.json`) and the `.ipa` files it points to.
- Android phones get a signed [F-Droid](https://f-droid.org) repository (`/fdroid/repo`). The official `fdroidserver` builds it in a sidecar container.
- The admin page (`/`) publishes, edits and deletes apps on both platforms. It also curates featured apps and news and checks catalog health.
- The store preview (`/store`) is a public, read-only storefront that shows exactly what subscribers see. It works in any browser.
- Apps can arrive without a manual upload. Feather imports GitHub/GitLab releases (one-off, on an in-app schedule, or from cron), and a Telegram bot accepts forwarded files.

Everything runs with Docker Compose. Android, Telegram and the cron importer are opt-in profiles.

---

## How it fits together

```
                  ┌───────────────────────────── ./data ─────────────────────────────┐
 iPhone ────────► │ altstore-manager (app.py)        source.json  ipas/  icons/        │
   /source.json   │   admin page · API · /store ──►  fdroid/repo  fdroid/metadata    │ ◄──── Android
   /ipas/...      │                                        ▲                          │   /fdroid/repo
                  │                  fdroid-index sidecar ─┘ builds + signs the index │
                  └──────────────────────────────────────────────────────────────────┘
        optional: Garage/S3 bucket serves IPA, icon and APK downloads (302 redirects)
                  release-import (cron) · ipa-ingest-bot + telegram-bot-api (Telegram)
```

- `altstore-manager` is the product. It is a single Flask process that runs under Waitress. There is no database. **`data/` holds all the state.**
- `fdroid-index` only turns APKs and metadata into a signed F-Droid index. The app writes files into `data/fdroid/` and touches a marker, and the sidecar rebuilds within `FDROID_UPDATE_INTERVAL` seconds.

### Where your data lives

| What | On the host (stack folder) | Served at | With Garage enabled |
|---|---|---|---|
| iOS catalog | `data/source.json` (and 20 snapshots in `data/backups/`) | `/source.json` | stays local |
| IPAs | `data/ipas/<bundle>/<version>.ipa` | `/ipas/<bundle>/<version>.ipa` | moved to the bucket (`STORAGE_BACKEND=garage`) |
| App icons | `data/icons/<bundle>/icon.<ext>` | `/icons/<bundle>/icon.<ext>` | moved to the bucket (`ICON_STORAGE_BACKEND`) |
| APKs | `data/fdroid/repo/<package>_<versionCode>.apk` | `/fdroid/repo/<file>.apk` | **copied** to `<bucket>/apks/`, and downloads redirect there. The local copy stays because the sidecar signs from it. |
| Android metadata and icons | `data/fdroid/metadata/<package>.yml`, `metadata/<package>/en-US/icon.png` | inside the signed index | stays local |
| F-Droid signing key | `data/fdroid/keystore.p12` | not served | stays local. **Back it up.** |
| Importer config/state | `data/release-import/` | not served | stays local |
| Version retention policies | `data/retention.json` | not served | stays local |
| Operator pages | `data/site/<path>` (you put them there) | `/<path>`, when no route matches it | stays local |

---

## Quick start

```bash
cp .env.example .env
# set at least ADMIN_PASSWORD, SECRET_KEY and PUBLIC_BASE_URL (e.g. https://apps.example.com)

mkdir -p data/ipas data/icons data/fdroid   # compose mounts these separately; create them now,
sudo chown -R 999:999 data      # or Docker creates them as root and the app cannot start (it runs as uid 999)

docker compose pull
docker compose up -d
```

Open `http://<host>:7000`, sign in with `ADMIN_PASSWORD`, and add an app. Put a TLS reverse proxy in front of it before you expose it to the internet.

> Skipping the `mkdir`/`chown` is the most common first-run failure. It shows up as a `PermissionError` at startup (a root-owned `data/fdroid`) or on every write. To fix it, run `sudo chown -R 999:999 data` and then `docker compose up -d`.

## Subscribing devices

| Device | How |
|---|---|
| iPhone with Feather | Scan the QR code on the admin page or `/store`, or open `feather://<host>/source.json` |
| iPhone with AltStore | Add the source `https://<host>/source.json` |
| Android with F-Droid or Droid-ify | Scan the Android QR code (`fdroidrepos://<host>/fdroid/repo?fingerprint=…`), or add `https://<host>/fdroid/repo` and enter the fingerprint from the Android tab |
| Any browser | Open `https://<host>/store` for search, details, version history, downloads, and the buttons and QR codes above |

`/store` reads only `/source.json` and the F-Droid index, the same public documents that devices fetch. It never shows anything a subscriber couldn't already see.

---

## Publishing apps

| Method | iOS | Android | Where |
|---|---|---|---|
| Upload a file or paste a URL | ✓ | ✓ | admin page, **Add** or **Android** tab |
| HTTP API with a token (scripts, CI) | ✓ | ✓ | `POST /api/publish`, see [`UPLOADING.md`](./UPLOADING.md) |
| MCP server (AI agents) | ✓ | ✓ | `scripts/feather_mcp.py`, see [`UPLOADING.md`](./UPLOADING.md#mcp-server-for-agents) |
| Import a GitHub/GitLab release | ✓ | ✓ | **Import** tab |
| Scheduled auto-import (inside the app) | ✓ | ✓ | **Import** tab, *Save & auto-import* |
| Cron importer (separate container) | ✓ | ✓ | [Cron importer](#cron-release-importer) |
| Telegram bot (forward a file, then `/add`) | ✓ | ✓ | [Telegram](#telegram-ingest-and-notifications) |

What every path has in common:

- **The binary is the truth.** The server reads the iOS bundle ID or Android package name, and the version, from `Info.plist` or `AndroidManifest.xml`, never from the filename. Publishing a version that already exists stores nothing. Through `/api/publish`, though, an iOS repeat still declares any entitlement or privacy key that the app's `appPermissions` lacks, because AltStore and SideStore refuse to install without them.
- **Bad files are refused up front.** The server rejects a file that isn't a real IPA (an HTML error page, for example) and gives the reason. It also rejects unsigned APKs, because F-Droid would never publish them and would not say so.
- **Icons come from the binary.** If you don't supply an icon, the server uses the IPA's own icon files or the APK's launcher icon. It never replaces an icon you supplied or a real icon the app already has. For apps added before this existed, click **Fill missing icons** on the Apps tab. On Android, **Rebuild index** does it too. The server can't extract an iOS icon that exists only inside `Assets.car` or an Android icon that is vector-only, so those apps show the default icon.
- **A different bundle ID is allowed, with a warning.** Renamed or patched builds often get their own catalog ID on purpose. Devices still install and update them by the IPA's real identifier, so the response tells you.

Scripts and agents publish with a token. Create one on the **Source** tab (*API tokens*) and send the file to `POST /api/publish`. The one endpoint serves both platforms, and the platform, ID and version all come from the file:

```bash
curl -H "Authorization: Bearer $FEATHER_TOKEN" -F file=@MyApp.ipa https://apps.example.com/api/publish
```

AI agents can use the MCP server instead, which needs only the Python standard library (`claude mcp add feather --env FEATHER_URL=… --env FEATHER_TOKEN=… -- python3 scripts/feather_mcp.py`). [`UPLOADING.md`](./UPLOADING.md) has the full contract.

Each import job matches release assets with a glob (`*.apk`) or a full-match regex (`.*arm64-v8a\.apk`). The regex option is for projects that ship one APK per CPU architecture. A job must match at most one asset per platform. When a release ships several variants (such as `…-sideloaded-Twitter_7.0.0.ipa` and `…-sideloaded-X_7.0.0.ipa`), the error lists every match. Narrow the glob to the part that differs (`*-sideloaded-Twitter_*.ipa`) or set an exclude glob.

---

## Android / F-Droid

Enable it once:

```bash
mkdir -p data/fdroid && sudo chown -R 999:999 data/fdroid
# in .env:
#   FDROID_KEYSTORE_PASSWORD=<long random string>
#   COMPOSE_PROFILES=android
docker compose up -d
```

The first start creates `data/fdroid/keystore.p12`. **Back up that file and `FDROID_KEYSTORE_PASSWORD` together.** If you lose either, you get a new signing key and fingerprint, and every subscribed phone has to re-add the repo.

What to expect:

- **Every rejected APK is reported.** If `fdroid update` skips or archives an APK (because of a corrupt signature, for example), the Android tab lists it under *Not published by F-Droid* and `data/fdroid/last-update.json` records it under `rejected`.
- **The sidecar kills a stuck rebuild** after `FDROID_UPDATE_TIMEOUT` seconds (default 1800), so one bad run can't freeze the index.
- **Signer changes are flagged.** The server publishes a new version signed with a different key, but phones won't offer it as an update. The Android tab warns about mixed signers.
- **Rebuild index** re-signs the index and fills missing icons. With Garage, it also uploads any APKs the bucket is missing.

Troubleshooting:

```bash
docker logs --tail 20 feather-fdroid-index      # should end with "INFO: Finished"
cat data/fdroid/last-update.json                # "ok": true, plus any "rejected" entries
curl -s https://<host>/fdroid/repo/index-v1.json | python3 -c 'import json,sys; print(list(json.load(sys.stdin)["packages"]))'
```

To rotate only the keystore password and keep the key and fingerprint, run `keytool -storepasswd -keystore /repo/keystore.p12 -storepass:env FDROID_KEYSTORE_PASSWORD -new:env NEW_PASSWORD` inside the sidecar, update `.env`, and restart the sidecar.

`Dockerfile.fdroid` pins the sidecar's `fdroidserver` base image by digest. GitLab deletes old digests when upstream rebuilds `master`, so a build that fails with `manifest unknown` means the pin needs a refresh. Resolve the current `master` digest, run an index build with a real APK, then update the digest and the note above it.

---

## Storage: local disk or Garage/S3

By default everything is on local disk. With Garage (or any S3-compatible store), the server answers downloads with 302 redirects to the bucket, and catalog URLs never change.

| Setting | Moves | Before switching |
|---|---|---|
| `STORAGE_BACKEND=garage` | IPAs (and icons, unless `ICON_STORAGE_BACKEND` says otherwise) | Run both migrations below, first as a dry run and then with `--apply`. **Switching first makes existing apps uninstallable.** |
| `ICON_STORAGE_BACKEND=garage` | icons only | `scripts/migrate_icons_to_garage.py` |
| `APK_STORAGE_BACKEND=garage` | APK **downloads** (defaults to `STORAGE_BACKEND`) | Nothing. Afterwards, click **Rebuild index** once to upload existing APKs. If an upload fails, the APK stays published from local disk. |

The migrations ship in the app image and read the same `.env`. Set the `GARAGE_*` variables first and keep `STORAGE_BACKEND=local` while migrating. You can run the migrations next to the live app, because they only read `data/` and write to Garage:

```bash
docker compose run --rm altstore-manager python scripts/migrate_ipas_to_garage.py            # dry run
docker compose run --rm altstore-manager python scripts/migrate_ipas_to_garage.py --apply
docker compose run --rm altstore-manager python scripts/migrate_icons_to_garage.py --apply
# then set STORAGE_BACKEND=garage in .env and run docker compose up -d
```

All three need the `GARAGE_*` variables. The bucket must also be **publicly readable** at `GARAGE_PUBLIC_BASE_URL` (Garage website access), or devices get 403s. To offload only APK traffic, set `APK_STORAGE_BACKEND=garage` and leave `STORAGE_BACKEND` alone.

---

## Automation

### Cron release importer

This one-shot container checks the configured GitHub/GitLab releases and publishes new IPAs and APKs through the same API the admin page uses.

```bash
mkdir -p data/release-import
cp release-sources.example.json data/release-import/release-sources.json   # edit the jobs
sudo chown -R 999:999 data/release-import

docker compose run --rm --no-deps -T release-import           # dry run: never downloads or publishes
docker compose run --rm --no-deps -T release-import --apply   # publish; a second run reports "skipped"
```

Schedule it from the host's crontab:

```cron
17 */6 * * * cd /path/to/feather && docker compose run --rm --no-deps -T release-import --apply >> /var/log/feather-release-import.log 2>&1
```

- **Set `FEATHER_BASE_URL=http://altstore-manager:5000`** in `.env`, so the importer talks to the app inside Compose rather than through your proxy.
- **`--no-deps`** stops cron from starting or recreating the app container. The importer only needs the app to be reachable.
- **`bundleIdentifier` and `package` are optional** in each job. The importer reads them from the downloaded file, and when you set them they act as a check.
- **The importer downloads only what it needs.** It skips an unchanged release, and it imports an APK again if someone deleted it from the repo.
- **Exit codes.** `0` means every job succeeded or had nothing new. `1` means at least one job failed. The importer reports a wrong password or an unreachable app once, before any download. `2` means the manifest or environment is invalid.

The importer supports `owner/repo` on github.com and `namespace/project` on gitlab.com. It does not support self-hosted forges, branch builds or CI artifacts. You need `GITHUB_TOKEN` or `GITLAB_TOKEN` only for private repos or higher rate limits.

### Auto-import inside the app

On the **Import** tab, *Save & auto-import* stores the same kind of job in the app. A background scheduler runs each enabled job every `intervalHours`. Use this or cron for a given repo, not both.

### Telegram ingest and notifications

Forward an IPA or APK to your bot and confirm with `/add`. The bot talks to a self-hosted Bot API server (`telegram-bot-api`), because the cloud API caps downloads at 20 MB. Set the `TELEGRAM_*` variables, then enable the bot with `COMPOSE_PROFILES=telegram`. The worker refuses to start without those variables.

Pending uploads survive worker restarts in `data/telegram-pending/pending.sqlite3`. Before enabling Telegram, create `data/telegram-pending` and give it ownership `101:101` with mode `0700`, to match the worker's Compose user. A new forward replaces the sender's previous pending upload, if there is one. `/add` clears the stored entry only after a successful publish, so the entry stays available when publishing fails. The pending upload also keeps the IPA metadata the bot detected, including the build version and minimum iOS version, so an `/add` after a restart keeps those values. The worker streams IPA, APK and icon uploads as multipart with bounded memory, so a large file never needs a file-sized allocation in the worker.

Notifications post catalog events to a chat. `add_app`, `add_version`, `delete_app` and `android_add_apk` are on by default. `TELEGRAM_NOTIFY_EVENTS` can also turn on `delete_version`, `prune_versions` (a retention policy removed older versions) and `health_transition`.

---

## More admin tools

- **Health** scans the iOS catalog for missing IPAs and icons, corrupt IPA files, zero sizes, duplicate versions and non-public download URLs. It keeps 30 snapshots and can alert when the catalog changes between healthy and degraded.
- **Catalog recovery** lists the last 20 `source.json` snapshots. You can preview a snapshot and restore it after typing a confirmation. The snapshots sit next to the catalog in `data/`, so they are not off-site backups, and they don't include the binaries.
- **Version retention** is set per app with *Keep previous versions*, in the iOS app's Edit dialog and on each Android app's card. It keeps the newest version and that many before it. Whenever an update arrives, the server deletes the older versions along with their IPA or APK files. All APKs that share a versionName count as one version, so an app's per-ABI APKs are kept or deleted together. A backport below the newest version prunes nothing. *Prune now* applies the saved policy without waiting for an update. Retention is off until you set it.
- **Featured apps and news** lets you pick up to five featured apps and write news items in AltStore format.
- **Certificate check** shows whether an uploaded `.p12` and `.mobileprovision` pair, when each expires and which devices they cover. It signs nothing and stores nothing.
- **Storage tools** are a storage self-test, an icon reconcile, and a list of recent server errors with secrets redacted.
- **Operator pages** are files in `data/site/`, served at the same path. They are for anything that has to live at a fixed URL on this host, such as `data/site/.well-known/assetlinks.json` for an Android app's links, or a small static page (`data/site/w/index.html` at `/w/`, and `/w` redirects there). Everything in `data/site/` is public except hidden files (`.git/` and the like, though `.well-known/` is served) and symlinks that lead out of `data/site/`. The server looks up these files only once no route matches, so they can't shadow the catalogue, the repo or the API. The server sends each one with a sandboxing CSP that gives it an opaque origin. A page there can run its own inline script, but it can't read the admin session or call the API.

---

## Configuration

All settings go in `.env`, and [`.env.example`](./.env.example) explains every variable. These are the ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `ADMIN_PASSWORD` | none | **Required.** The app refuses to start without it. |
| `SECRET_KEY` | random per boot | Set it, or every restart signs you out. |
| `PUBLIC_BASE_URL` | request `Host` | The canonical origin written into catalog URLs. If unset, the app uses the client's `Host` header and logs a warning. |
| `DATA_DIR` | `/app/data` | Override it for local runs. |
| `PORT` / `WAITRESS_THREADS` | `5000` / `8` | Run one process only. See [Operating notes](#operating-notes). |
| `MAX_CONTENT_LENGTH` | 2 GiB | Upload size cap. |
| `STORAGE_BACKEND` / `ICON_STORAGE_BACKEND` / `APK_STORAGE_BACKEND` | `local` | See [Storage](#storage-local-disk-or-garages3). Needs `GARAGE_*`. |
| `APK_REJECT_DEBUGGABLE` | `false` | Refuse debuggable APKs on every upload path instead of publishing them with a warning. |
| `DEVICE_BACKUPS` | `false` | Let phones keep their apps' settings here with a backups-only token (plan 088). The limits are `DEVICE_BACKUP_MAX_BYTES` (256 MiB), `DEVICE_BACKUP_KEEP` (30 per device) and `DEVICE_BACKUP_MAX_DEVICES` (20 per app). |
| `SESSION_COOKIE_SECURE` | `false` | Mark the admin cookie `Secure`. Turn it on when a TLS proxy is in front. |
| `DOWNLOAD_DEADLINE_SECONDS` | `1800` | Wall-clock limit for any download from a URL. |
| `FDROID_KEYSTORE_PASSWORD` | none | Required for the `android` profile. |
| `FDROID_UPDATE_INTERVAL` / `FDROID_UPDATE_TIMEOUT` | `15` / `1800` | Seconds between rebuild checks, and the maximum length of one rebuild in seconds. |
| `FEATHER_BASE_URL` / `FEATHER_ADMIN_PASSWORD` | none | How the cron importer and the bot reach the app. Set the URL to `http://altstore-manager:5000`. |
| `RELEASE_IMPORT_*`, `GITHUB_TOKEN`, `GITLAB_TOKEN` | off | Cron importer. |
| `TELEGRAM_*`, `BOT_API_*` | off | Telegram ingest and notifications. |
| `COMPOSE_PROFILES` | none | Any of `android`, `telegram` and `release-import`, comma-separated. |

---

## Updating a deployment

```bash
docker compose pull && docker compose up -d
```

Use `pull`, not `--build`. CI has already built and smoke-tested these exact images, and a local build skips that check.

| Image | Service | Enabled by |
|---|---|---|
| `ghcr.io/t1nk333r/feather` | `altstore-manager`, `release-import` | always for `altstore-manager`, profile `release-import` for `release-import` |
| `ghcr.io/t1nk333r/feather-bot` | `ipa-ingest-bot` | profile `telegram` |
| `ghcr.io/t1nk333r/feather-fdroid` | `fdroid-index` | profile `android` |

Quick checks after an update:

```bash
curl -sI https://<host>/source.json | head -1          # 200
curl -sI https://<host>/store | head -1                # 200
docker logs --tail 5 feather-fdroid-index              # ends with "INFO: Finished"
docker compose run --rm --no-deps -T release-import    # dry run; summary shows failed=0
```

---

## Security

What the app does for you:

- **Changes require sign-in or an API token.** The session cookie is `HttpOnly` and `SameSite=Lax`, and also `Secure` with `SESSION_COOKIE_SECURE=true`. Changing `ADMIN_PASSWORD` or `SECRET_KEY` signs out every existing session. Signing out clears only your own browser's cookie.
- **API tokens never reach admin routes.** A publish token can upload, edit app details and read. It can never delete apps, change settings or create tokens. A backup token (`kind: backup`) can only list, upload and download device backups for its named apps, and only while `DEVICE_BACKUPS` is on. You can limit a publish token to named apps, and give any token an expiry. The app stores only a SHA-256 of each token (`data/api-tokens.json`, mode 600), and revoking a token takes effect on the next request.
- **Logins are throttled.** After 5 failed attempts from one client within 60 s, the app answers `429` with `Retry-After`, and a successful login clears the count. Behind a reverse proxy all clients share the proxy's address, so in practice one limit covers every client.
- **The app caps what it reads from untrusted files.** It checks sizes before it decompresses any IPA or APK metadata (`Info.plist` at most 4 MB, `AndroidManifest.xml` at most 16 MB, icon images at most 8 MB, and an oversized `resources.arsc` is skipped), so a zip bomb can't exhaust memory. App names and descriptions always render as text.
- **Security headers.** The app sends `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: same-origin`. A CSP sandboxes everything under `/fdroid/repo/`, because fdroidserver writes the repo name into its HTML without escaping it.
- **The app validates catalog fields.** Source and news URLs must be absolute `http(s)` URLs, and colours must be `#RRGGBB`.

What stays your job:

- **TLS** belongs at the reverse proxy. The app speaks plain HTTP on port 5000.
- **Rate limiting uploads**, and any wider abuse protection, also belongs at the proxy.
- **Download-from-URL** features fetch whatever an admin gives them, including LAN addresses. The app publishes nothing that isn't a valid IPA or APK, but guard the admin password with that in mind. API tokens cannot use these features. `/api/publish` refuses `url` from a token, and the MCP server downloads the file on the agent's side instead.
- **Uploaded binaries are untrusted.** The app restricts IPA bundle IDs and versions to safe characters and checks APK size caps against the real inflated size. Icon decoding is bounded to 1024 px for Apple's CgBI format and 4096 px for other images, and stored icons are at most 512 px.

Clients fetch these routes without credentials, so they must stay public and unauthenticated:

| Route | Purpose |
|---|---|
| `GET /source.json` | iOS catalog |
| `GET /ipas/<bundle>/<file>` | IPA download (302 to the bucket with Garage) |
| `GET /icons/<bundle>/icon.<ext>` | app icons |
| `GET /fdroid/repo/<path>` | F-Droid index, signatures, APKs (302 to the bucket with Garage), icons |
| `GET /qr`, `GET /fdroid/qr` | add-source QR codes |
| `GET /store` | read-only storefront |
| `GET /`, `GET /sw.js` | admin page shell (everything behind it requires sign-in) |
| `POST /api/login`, `POST /api/logout`, `GET /api/session` | session |
| `GET /api/apps`, `GET /api/app/<bundle>` | read-only app JSON |
| `GET /api/version` | the commit the running image was built from |
| `GET /<path>` | operator pages from `data/site/`, once no route matches |

Everything else under `/api/` requires a session. Publish tokens also reach `/api/publish`, `/api/app-details`, `/api/android/status` and `/api/android/apps`, and backup tokens reach the device-backup routes above. For the full list, run `grep -n "@app.route" app.py`.

---

## Operating notes

- **Run one process only.** Catalog writes use an in-process lock, so never run several worker processes (for example `gunicorn -w 4`). Raise `WAITRESS_THREADS` instead.
- **The F-Droid sidecar runs as root** inside its container, because the upstream image requires it. It has no ports and mounts only `data/fdroid`.
- **Back up** `data/` (catalog, binaries, icons, `fdroid/keystore.p12`), your `.env`, and any Garage bucket. Catalog snapshots alone can't restore deleted binaries.
- **`RATE_LIMIT`** in `.env.example` is accepted but not used.

---

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# run locally (there is no .env loading, so export what you need)
DATA_DIR=./data ADMIN_PASSWORD=dev SECRET_KEY=dev .venv/bin/python app.py

# tests: no network, no Docker, ~20 s
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

Set `ADMIN_PASSWORD` even for tests, because the app refuses to import without it.

CI (`.github/workflows/ci.yml`) runs on every push and pull request. It runs the tests on Python 3.11 (the container's version) and 3.14, checks the `requests` pin, then builds and smoke-tests all three images. Only `main` publishes to GHCR, and only after every smoke test passes.

Rules that have each broken a build before:

- `.dockerignore` is deny-by-default, so a new file that a Dockerfile copies needs a matching `!path` line. Only CI's image build catches a missing one.
- Every pin in `requirements.txt` needs wheels for Python 3.11 and 3.14.
- `data/` is gitignored and must never be committed.
- The admin page and `/store` pass an axe-core accessibility audit in light and dark mode. Keep new controls labelled.

### Repository layout

```
app.py                              the Flask app: routes, catalog, storage, Android repo
templates/index.html                admin page
templates/store.html                public /store
static/                             icons, favicons, web manifest
scripts/ipa_inspection.py           IPA metadata reader (shared)
scripts/apk_inspection.py           APK metadata, signature and icon extraction (shared)
scripts/certificate_inspection.py   .p12 / .mobileprovision inspector
scripts/release_source_ingest.py    GitHub/GitLab release importer (cron and in-app)
scripts/feather_mcp.py              MCP server for agents (stdlib only)
scripts/telegram_bot_ingest.py      Telegram ingest worker
scripts/fdroid_index_loop.sh        F-Droid sidecar rebuild loop
scripts/migrate_*_to_garage.py      one-shot migrations from local disk to Garage
Dockerfile, Dockerfile.bot, Dockerfile.fdroid, compose.yml
UPLOADING.md                        write-API contract for scripts and agents
plans/                              design history; plans/README.md is the index
tests/                              pytest suite
```

## Further reading

- [`UPLOADING.md`](./UPLOADING.md) covers publishing from scripts and CI.
- [`plans/README.md`](./plans/README.md) lists every design decision, including what was rejected and why.
- [`plans/HANDOFF.md`](./plans/HANDOFF.md) lists deployment traps and open operator tasks.
