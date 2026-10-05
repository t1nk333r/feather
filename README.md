# Feather

A self-hosted app store for iOS and Android: one server, one admin page, two kinds of subscribers.

- **iOS** — an [AltStore](https://altstore.io) / Feather-compatible source (`/source.json`) plus the `.ipa` files it points to.
- **Android** — a signed [F-Droid](https://f-droid.org) repository (`/fdroid/repo`), built by the official `fdroidserver` in a sidecar container.
- **Admin page** (`/`) — publish, edit and delete apps on both platforms, curate featured apps and news, check catalog health.
- **Store preview** (`/store`) — a public, read-only storefront showing exactly what subscribers see; works in any browser.
- **Hands-off publishing** — import from GitHub/GitLab releases (one-off, scheduled in-app, or from cron), or forward files to a Telegram bot.

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

- `altstore-manager` is the product: one Flask process served by Waitress. There is no database — **`data/` is the state**.
- `fdroid-index` only turns APKs + metadata into a signed F-Droid index. The app drops files into `data/fdroid/` and touches a marker; the sidecar rebuilds within `FDROID_UPDATE_INTERVAL` seconds.

### Where your data lives

| What | On the host (stack folder) | Served at | With Garage enabled |
|---|---|---|---|
| iOS catalog | `data/source.json` (+ 20 snapshots in `data/backups/`) | `/source.json` | stays local |
| IPAs | `data/ipas/<bundle>/<version>.ipa` | `/ipas/<bundle>/<version>.ipa` | moved to the bucket (`STORAGE_BACKEND=garage`) |
| App icons | `data/icons/<bundle>/icon.<ext>` | `/icons/<bundle>/icon.<ext>` | moved to the bucket (`ICON_STORAGE_BACKEND`) |
| APKs | `data/fdroid/repo/<package>_<versionCode>.apk` | `/fdroid/repo/<file>.apk` | **copied** to `<bucket>/apks/`, downloads redirected; the local copy stays because the sidecar signs from it |
| Android metadata + icons | `data/fdroid/metadata/<package>.yml`, `metadata/<package>/en-US/icon.png` | inside the signed index | stays local |
| F-Droid signing key | `data/fdroid/keystore.p12` | — | stays local — **back it up** |
| Importer config/state | `data/release-import/` | — | stays local |

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

Open `http://<host>:7000`, sign in with `ADMIN_PASSWORD`, and add an app. Put a TLS reverse proxy in front before exposing it to the internet.

> Skipping the `mkdir`/`chown` is the most common first-run failure: `PermissionError` at startup (a root-owned `data/fdroid`) or on every write. Fix with `sudo chown -R 999:999 data` and `docker compose up -d`.

## Subscribing devices

| Device | How |
|---|---|
| **iPhone — Feather** | Scan the QR on the admin page or `/store`, or open `feather://<host>/source.json` |
| **iPhone — AltStore** | Add source `https://<host>/source.json` |
| **Android — F-Droid, Droid-ify** | Scan the Android QR (`fdroidrepos://<host>/fdroid/repo?fingerprint=…`), or add `https://<host>/fdroid/repo` and enter the fingerprint from the Android tab |
| **Any browser** | `https://<host>/store` — search, details, version history, downloads, and the buttons/QRs above |

`/store` only reads `/source.json` and the F-Droid index — the same public documents devices fetch — so it never shows anything a subscriber couldn't already see.

---

## Publishing apps

| Method | iOS | Android | Where |
|---|---|---|---|
| Upload a file or paste a URL | ✓ | ✓ | admin page → **Add** / **Android** |
| HTTP API with a token (scripts, CI) | ✓ | ✓ | `POST /api/publish` — [`UPLOADING.md`](./UPLOADING.md) |
| MCP server (AI agents) | ✓ | ✓ | `scripts/feather_mcp.py` — [`UPLOADING.md`](./UPLOADING.md#mcp-server-for-agents) |
| Import a GitHub/GitLab release | ✓ | ✓ | **Import** tab |
| Scheduled auto-import (inside the app) | ✓ | ✓ | **Import** → *Save & auto-import* |
| Cron importer (separate container) | ✓ | ✓ | [Cron importer](#cron-release-importer) |
| Telegram bot (forward a file, `/add`) | ✓ | ✓ | [Telegram](#telegram-ingest-and-notifications) |

What every path has in common:

- **The binary is the truth.** Bundle ID / package and version are read from `Info.plist` or `AndroidManifest.xml`, never from the filename. Publishing a version that already exists is a no-op.
- **Bad files are refused up front.** A file that isn't a real IPA (e.g. an HTML error page) is rejected with the reason. Unsigned APKs are rejected, because F-Droid would silently never publish them.
- **Icons come from the binary.** If you don't supply an icon, the IPA's own icon files or the APK's launcher icon are used. A supplied icon, or a real icon the app already has, is never replaced. For apps added before this existed, click **Fill missing icons** on the Apps tab (Android: **Rebuild Index** does it too). Limits: iOS icons that exist only inside `Assets.car`, and Android icons that are vector-only, can't be extracted — the default icon is shown.
- **A different bundle ID is allowed, with a warning.** Renamed/patched builds are often catalogued under their own ID on purpose; devices still install and update them by the IPA's real identifier, so the response tells you.

**For scripts and agents,** create a token on the **Source** tab (*API tokens*) and send the file to `POST /api/publish` — one endpoint for both platforms; the platform, ID and version come from the file:

```bash
curl -H "Authorization: Bearer $FEATHER_TOKEN" -F file=@MyApp.ipa https://apps.example.com/api/publish
```

AI agents can use the stdlib-only MCP server instead (`claude mcp add feather --env FEATHER_URL=… --env FEATHER_TOKEN=… -- python3 scripts/feather_mcp.py`). [`UPLOADING.md`](./UPLOADING.md) has the full contract.

**Release assets** are matched per job with a glob (`*.apk`) or a full-match regex (`.*arm64-v8a\.apk`). Regex exists for projects that ship one APK per CPU architecture; a job must match at most one asset per platform. When a release ships several variants (e.g. `…-sideloaded-Twitter_7.0.0.ipa` and `…-sideloaded-X_7.0.0.ipa`), the error lists every match; narrow the glob to the part that differs (`*-sideloaded-Twitter_*.ipa`) or set an exclude glob.

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

The first start creates `data/fdroid/keystore.p12`. **Back up that file and `FDROID_KEYSTORE_PASSWORD` together.** Losing either means a new signing key and fingerprint, and every subscribed phone has to re-add the repo.

What to expect:

- **Rejected APKs are reported, never silent.** If `fdroid update` skips or archives an APK (e.g. a corrupt signature), the Android tab lists it under *Not published by F-Droid* and `data/fdroid/last-update.json` records it under `rejected`.
- **A stuck rebuild is killed** after `FDROID_UPDATE_TIMEOUT` seconds (default 1800), so one bad run can't freeze the index.
- **Signer changes are flagged.** A new version signed with a different key is published, but phones won't offer it as an update; the Android tab warns about mixed signers.
- **Rebuild Index** re-signs the index, fills missing icons, and (with Garage) uploads any APKs the bucket is missing.

Troubleshooting:

```bash
docker logs --tail 20 feather-fdroid-index      # should end with "INFO: Finished"
cat data/fdroid/last-update.json                # "ok": true, plus any "rejected" entries
curl -s https://<host>/fdroid/repo/index-v1.json | python3 -c 'import json,sys; print(list(json.load(sys.stdin)["packages"]))'
```

To rotate only the keystore password (key and fingerprint unchanged), run `keytool -storepasswd -keystore /repo/keystore.p12 -storepass:env FDROID_KEYSTORE_PASSWORD -new:env NEW_PASSWORD` inside the sidecar, update `.env`, and restart it.

The sidecar's `fdroidserver` base image is pinned by digest in `Dockerfile.fdroid`. GitLab deletes old digests when upstream rebuilds `master`, so a build failing with `manifest unknown` means the pin needs refreshing: resolve the current `master` digest, run a real-APK index build, then update the digest and the note above it.

---

## Storage: local disk or Garage/S3

By default everything is on local disk. With Garage (or any S3-compatible store), downloads are served from the bucket via 302 redirects; catalog URLs never change.

| Setting | Moves | Before switching |
|---|---|---|
| `STORAGE_BACKEND=garage` | IPAs (and icons, unless `ICON_STORAGE_BACKEND` says otherwise) | Run `scripts/migrate_ipas_to_garage.py` and `scripts/migrate_icons_to_garage.py` — dry run, then `--apply`. **Switching first makes existing apps uninstallable.** |
| `ICON_STORAGE_BACKEND=garage` | icons only | `scripts/migrate_icons_to_garage.py` |
| `APK_STORAGE_BACKEND=garage` | APK **downloads** (defaults to `STORAGE_BACKEND`) | Nothing — then click **Rebuild Index** once to upload existing APKs. A failed upload keeps the APK published from local disk. |

All three need the `GARAGE_*` variables, and the bucket must be **publicly readable** at `GARAGE_PUBLIC_BASE_URL` (Garage website access) — otherwise devices get 403s. To offload only APK traffic, set `APK_STORAGE_BACKEND=garage` and leave `STORAGE_BACKEND` alone.

---

## Automation

### Cron release importer

A one-shot container that checks configured GitHub/GitLab releases and publishes new IPAs and APKs through the same API as the admin page.

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
- **`--no-deps`** stops cron from starting or recreating the app container; the importer only needs it reachable.
- **`bundleIdentifier` / `package` are optional** per job — they're read from the downloaded file, and act as a check when set.
- **Nothing is re-downloaded needlessly:** an unchanged release is skipped; an APK deleted from the repo is imported again.
- **Exit codes:** `0` every job succeeded or had nothing new · `1` at least one job failed (a wrong password or unreachable app is reported once, before any download) · `2` invalid manifest or environment.

Supports `owner/repo` on github.com and `namespace/project` on gitlab.com — not self-hosted forges, branch builds or CI artifacts. `GITHUB_TOKEN` / `GITLAB_TOKEN` are only needed for private repos or higher rate limits.

### Auto-import inside the app

**Import** → *Save & auto-import* stores the same kind of job in the app, and a background scheduler runs enabled jobs every `intervalHours`. Use this or cron, not both for the same repo.

### Telegram ingest and notifications

Forward an IPA or APK to your bot and confirm with `/add`. It runs against a self-hosted Bot API server (`telegram-bot-api`) because the cloud API caps downloads at 20 MB. Enable with `COMPOSE_PROFILES=telegram` once the `TELEGRAM_*` variables are set — the worker refuses to start without them.

Notifications post catalog events to a chat: `add_app`, `add_version`, `delete_app`, `delete_version`, `android_add_apk`, `health_transition` (all except the last two are on by default).

---

## More admin tools

- **Health** — scans the iOS catalog for missing IPAs and icons, corrupt IPA files, zero sizes, duplicate versions and non-public download URLs; keeps 30 snapshots and can alert on healthy ↔ degraded changes.
- **Catalog recovery** — the last 20 `source.json` snapshots with preview and typed-confirmation restore. They sit next to the catalog in `data/`, so they are not off-site backups and don't include the binaries.
- **Featured apps and news** — up to five featured apps and AltStore-format news items.
- **Certificate check** — upload a `.p12` + `.mobileprovision` to see whether they pair, when each expires and which devices they cover. Nothing is signed or stored.
- **Storage tools** — storage self-test, icon reconcile, and recent server errors (secrets redacted).

---

## Configuration

Everything is set in `.env`; [`.env.example`](./.env.example) explains every variable. The ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `ADMIN_PASSWORD` | — | **Required.** The app refuses to start without it. |
| `SECRET_KEY` | random per boot | Set it, or every restart signs you out. |
| `PUBLIC_BASE_URL` | request `Host` | Canonical origin baked into catalog URLs. Unset falls back to the client's `Host` header and logs a warning. |
| `DATA_DIR` | `/app/data` | Override for local runs. |
| `PORT` / `WAITRESS_THREADS` | `5000` / `8` | One process only — see [Operating notes](#operating-notes). |
| `MAX_CONTENT_LENGTH` | 2 GiB | Upload size cap. |
| `STORAGE_BACKEND` / `ICON_STORAGE_BACKEND` / `APK_STORAGE_BACKEND` | `local` | See [Storage](#storage-local-disk-or-garages3). Needs `GARAGE_*`. |
| `APK_REJECT_DEBUGGABLE` | `false` | Refuse debuggable APKs instead of publishing them with a warning. |
| `FDROID_KEYSTORE_PASSWORD` | — | Required for the `android` profile. |
| `FDROID_UPDATE_INTERVAL` / `FDROID_UPDATE_TIMEOUT` | `15` / `1800` | Seconds between rebuild checks / maximum length of one rebuild. |
| `FEATHER_BASE_URL` / `FEATHER_ADMIN_PASSWORD` | — | How the cron importer and bot reach the app: `http://altstore-manager:5000`. |
| `RELEASE_IMPORT_*`, `GITHUB_TOKEN`, `GITLAB_TOKEN` | off | Cron importer. |
| `TELEGRAM_*`, `BOT_API_*` | off | Telegram ingest and notifications. |
| `COMPOSE_PROFILES` | — | `android`, `telegram`, `release-import` (comma-separated). |

---

## Updating a deployment

```bash
docker compose pull && docker compose up -d
```

Use `pull`, not `--build`: CI has already built and smoke-tested these exact images, and a local build bypasses that.

| Image | Service | Enabled by |
|---|---|---|
| `ghcr.io/t1nk333r/feather` | `altstore-manager`, `release-import` | always / profile `release-import` |
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

- **Sign-in required** for every change. The session cookie is `HttpOnly` and `SameSite=Lax`.
- **API tokens are publish-only.** A token can upload, edit app details and read, never delete apps, change settings, or create tokens. A token can be limited to named apps and given an expiry. Only a SHA-256 of each token is stored (`data/api-tokens.json`, mode 600); revoking takes effect on the next request.
- **Login throttling:** 5 failed attempts per client per 60 s, then `429` with `Retry-After`; a successful login clears it. Behind a reverse proxy all clients share the proxy's address, so the limit is effectively global.
- **Untrusted files are handled defensively.** IPA/APK metadata reads are size-capped before anything is decompressed (`Info.plist` ≤ 4 MB, `AndroidManifest.xml` ≤ 16 MB, oversized `resources.arsc` skipped, icon images ≤ 8 MB), so a zip bomb can't exhaust memory. App names and descriptions are always rendered as text.
- **Headers:** `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`. Everything under `/fdroid/repo/` is sandboxed by CSP, because fdroidserver writes the repo name into its HTML unescaped.
- **Catalog fields are validated:** source/news URLs must be absolute `http(s)`, colours must be `#RRGGBB`.

What stays your job:

- **TLS** at the reverse proxy — the app speaks plain HTTP on port 5000.
- **Rate limiting uploads** and any wider abuse protection at the proxy.
- **Download-from-URL** features fetch whatever an admin gives them, including LAN addresses. Nothing is published unless it's a valid IPA/APK, but treat the admin password accordingly. API tokens cannot use them: `/api/publish` refuses `url` from a token (the MCP server downloads on the agent's side instead).
- **Uploaded binaries are untrusted.** IPA bundle IDs and versions are restricted to safe characters, APK size caps are checked against the real inflated size, and icon decoding is bounded (≤1024 px for Apple's CgBI format, ≤4096 px otherwise, stored at ≤512 px).

The public, unauthenticated routes — clients fetch these without credentials, so they must stay public:

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

Everything else under `/api/` requires a session. Full list: `grep -n "@app.route" app.py`.

---

## Operating notes

- **One process only.** Catalog writes use an in-process lock; never run several worker processes (e.g. `gunicorn -w 4`). Raise `WAITRESS_THREADS` instead.
- **The F-Droid sidecar runs as root** inside its container (the upstream image requires it). It has no ports and mounts only `data/fdroid`.
- **Back up** `data/` (catalog, binaries, icons, `fdroid/keystore.p12`), your `.env`, and any Garage bucket. Catalog snapshots alone can't restore deleted binaries.
- **`RATE_LIMIT`** in `.env.example` is accepted but not used.

---

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# run locally (no .env loading — export what you need)
DATA_DIR=./data ADMIN_PASSWORD=dev SECRET_KEY=dev .venv/bin/python app.py

# tests: no network, no Docker, ~20 s
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

`ADMIN_PASSWORD` must be set even for tests; the app refuses to import without it.

**CI** (`.github/workflows/ci.yml`) runs on every push and pull request: tests on Python 3.11 (the container) and 3.14, a `requests` pin check, then builds and smoke-tests all three images. Only `main` publishes to GHCR, and only after every smoke test passes.

Rules that have each broken a build before:

- `.dockerignore` is deny-by-default: a new file a Dockerfile copies needs a matching `!path` line. Only CI's image build catches a missing one.
- Every pin in `requirements.txt` needs wheels for Python 3.11 and 3.14.
- `data/` is gitignored and must never be committed.
- The admin page and `/store` pass an axe-core accessibility audit in light and dark mode — keep new controls labelled.

### Repository layout

```
app.py                              the Flask app: routes, catalog, storage, Android repo
templates/index.html                admin page
templates/store.html                public /store
static/                             icons, favicons, web manifest
scripts/ipa_inspection.py           IPA metadata reader (shared)
scripts/apk_inspection.py           APK metadata, signature and icon extraction (shared)
scripts/certificate_inspection.py   .p12 / .mobileprovision inspector
scripts/release_source_ingest.py    GitHub/GitLab release importer (cron + in-app)
scripts/feather_mcp.py              MCP server for agents (stdlib only)
scripts/telegram_bot_ingest.py      Telegram ingest worker
scripts/fdroid_index_loop.sh        F-Droid sidecar rebuild loop
scripts/migrate_*_to_garage.py      one-shot local → Garage migrations
Dockerfile, Dockerfile.bot, Dockerfile.fdroid, compose.yml
UPLOADING.md                        write-API contract for scripts and agents
plans/                              design history; plans/README.md is the index
tests/                              pytest suite
```

## Further reading

- [`UPLOADING.md`](./UPLOADING.md) — publishing from scripts and CI.
- [`plans/README.md`](./plans/README.md) — every design decision, including what was rejected and why.
- [`plans/HANDOFF.md`](./plans/HANDOFF.md) — deployment traps and open operator tasks.
