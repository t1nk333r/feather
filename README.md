# Feather

A self-hosted app store backend for iOS and Android.

- **iOS:** serves an [AltStore](https://altstore.io)/Feather-compatible `source.json` and the `.ipa` files it points to.
- **Android:** serves a signed [F-Droid](https://f-droid.org) repository, built by the official `fdroidserver` in a sidecar container.
- **Admin UI** at `/` to publish, edit and delete apps on both platforms, curate featured apps and news, and watch catalog health.
- **Store preview** at `/store`: a public, read-only storefront showing exactly what subscribers see.
- **Hands-off publishing:** import from GitHub/GitLab releases (one-off, scheduled, or cron), or forward files to a Telegram bot.

Everything runs from Docker Compose; the Android, Telegram and cron pieces are opt-in profiles.

## How it fits together

```
                ┌──────────────────────────── ./data ────────────────────────────┐
 iPhone ──────► │ altstore-manager (app.py)   source.json, ipas/, icons/          │
  /source.json  │   admin UI, API, /store  ── fdroid/  ◄── fdroid-index sidecar   │ ◄── Android
  /ipas/...     │                                repo/, metadata/   (signs index)  │   /fdroid/repo
                └─────────────────────────────────────────────────────────────────┘
      optional:  ipa-ingest-bot + telegram-bot-api (profile telegram)
                 release-import (profile release-import, run from cron)
```

- `altstore-manager` is the product: a single Flask process served by Waitress.
- `fdroid-index` only turns APKs + metadata into a signed F-Droid index. The app drops APKs into `data/fdroid/repo/` and touches a marker file; the sidecar rebuilds within `FDROID_UPDATE_INTERVAL` seconds.
- Nothing talks to a database. `data/` **is** the state — back it up.

## Quick start

```bash
cp .env.example .env
# set at least ADMIN_PASSWORD, SECRET_KEY and PUBLIC_BASE_URL

mkdir -p data
sudo chown -R 999:999 data      # the container runs as uid 999; a fresh bind mount is root-owned

docker compose pull
docker compose up -d
```

Open `http://<host>:7000`, sign in with `ADMIN_PASSWORD`, and add an app. Put a TLS reverse proxy in front before exposing it.

Skipping the `chown` is the most common first-run failure: the app starts but every write fails with `PermissionError`.

## Subscribing devices

| | How |
|---|---|
| **iOS (Feather)** | Scan the QR on the admin page or `/store`, or open `feather://<host>/source.json` |
| **iOS (AltStore)** | Add source `https://<host>/source.json` |
| **Android (F-Droid, Droid-ify)** | Scan the Android QR (`fdroidrepos://<host>/fdroid/repo?fingerprint=…`), or add `https://<host>/fdroid/repo` and enter the fingerprint shown in the Android tab |
| **Any browser** | `https://<host>/store` — search, app details, version history, download links, and all of the above buttons/QRs |

`/store` reads only `/source.json` and `/fdroid/repo/index-v1.json`, the same public documents devices fetch, so it never shows anything a subscriber could not already see.

## Publishing apps

| Method | iOS | Android | Where |
|---|---|---|---|
| Admin UI upload or URL | ✓ | ✓ | "Add app" / "Android" tabs |
| HTTP API (scripts, CI) | ✓ | ✓ | see [`UPLOADING.md`](./UPLOADING.md) |
| Import a GitHub/GitLab release | ✓ | ✓ | "Import from Repo" tab |
| Scheduled auto-import (in-app) | ✓ | ✓ | "Save & auto-import" on the same tab |
| Cron importer (separate container) | ✓ | ✓ | [below](#cron-release-importer) |
| Telegram bot (forward a file, `/add`) | ✓ | ✓ | [below](#telegram-ingest-and-notifications) |

Every path inspects the binary itself: the bundle ID/package and version come from `Info.plist` or `AndroidManifest.xml`, never from the filename. Re-publishing the same version is an idempotent no-op.

**API field names** are `ipaFile` and `apkFile`, not `file`. A wrong name fails with an error that reads like a missing artifact. [`UPLOADING.md`](./UPLOADING.md) documents the login flow, each endpoint's fields, and how to confirm a publish landed.

**Release assets** are matched per job with a glob (`*.apk`) or a full-match regex (`.*arm64-v8a\.apk`). Regex exists for projects that ship one APK per ABI; a match must select at most one asset per platform.

## Android / F-Droid

Enable it once:

```bash
mkdir -p data/fdroid && sudo chown -R 999:999 data/fdroid
# in .env:
#   FDROID_KEYSTORE_PASSWORD=<long random string>
#   COMPOSE_PROFILES=android
docker compose up -d
```

The first start generates `data/fdroid/keystore.p12`. **Back up that file and `FDROID_KEYSTORE_PASSWORD` together.** Losing either creates a new signing key and a new fingerprint, and every subscribed device has to re-add the repo.

What to expect:

- **Unsigned APKs are rejected at upload.** F-Droid never publishes them, so accepting one would show "Added" for a version that never appears.
- **Anything F-Droid still drops is reported.** A signed APK can still be skipped or archived by `fdroid update` (for example, a corrupt signature); the Android tab lists these under *Not published by F-Droid*, and `data/fdroid/last-update.json` records them in `rejected`.
- **A stuck rebuild is killed** after `FDROID_UPDATE_TIMEOUT` seconds (default 1800) so one bad run can't freeze the index.
- **Signer changes are flagged.** A new version signed with a different key publishes, but phones won't offer it as an update to the installed app; the Android tab warns about mixed signers.

Troubleshooting:

```bash
docker logs --tail 20 feather-fdroid-index        # should end with "INFO: Finished"
cat data/fdroid/last-update.json                  # "ok": true, plus any "rejected" entries
curl -s https://<host>/fdroid/repo/index-v1.json | python3 -c 'import json,sys; print(list(json.load(sys.stdin)["packages"]))'
```

To rotate only the keystore password (the key and fingerprint stay the same), run `keytool -storepasswd -keystore /repo/keystore.p12 -storepass:env FDROID_KEYSTORE_PASSWORD -new:env NEW_PASSWORD` inside the sidecar, then update `.env` and restart it.

The sidecar's `fdroidserver` base image is pinned by digest in `Dockerfile.fdroid`. GitLab deletes old digests when upstream rebuilds `master`, so a build failing with `manifest unknown` means the pin needs refreshing — resolve the current `master` digest, run a real-APK index build, then update the digest and the note above it.

## Configuration

Everything is set in `.env`; [`.env.example`](./.env.example) lists and explains every variable. The ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `ADMIN_PASSWORD` | — | **Required.** The app refuses to start without it. |
| `SECRET_KEY` | random per boot | Set it, or every restart signs everyone out. |
| `PUBLIC_BASE_URL` | request `Host` | Canonical origin baked into catalog URLs, e.g. `https://apps.example.com`. Unset falls back to the client's `Host` header and logs a warning. |
| `DATA_DIR` | `/app/data` | Override for local runs. |
| `PORT` / `WAITRESS_THREADS` | `5000` / `8` | One process only — see [Operating notes](#operating-notes). |
| `MAX_CONTENT_LENGTH` | 2 GiB | Upload size cap. |
| `STORAGE_BACKEND` / `ICON_STORAGE_BACKEND` | `local` | `garage` stores IPAs/icons in S3-compatible storage; needs the `GARAGE_*` variables. |
| `FDROID_KEYSTORE_PASSWORD` | — | Required for the `android` profile. |
| `FDROID_UPDATE_INTERVAL` / `FDROID_UPDATE_TIMEOUT` | `15` / `1800` | Seconds between rebuild checks / max length of one rebuild. |
| `TELEGRAM_*`, `BOT_API_*` | off | Telegram ingest worker and notifications. |
| `RELEASE_IMPORT_*`, `GITHUB_TOKEN`, `GITLAB_TOKEN` | off | Cron importer; tokens only raise API rate limits / reach private repos. |
| `COMPOSE_PROFILES` | — | `android`, `telegram`, `release-import`, comma-separated. |

`RATE_LIMIT` is accepted but currently does nothing (see below).

## Optional features

### Cron release importer

A one-shot container that polls configured GitHub/GitLab releases and publishes new IPAs and APKs through the same HTTP API as the UI.

```bash
mkdir -p data/release-import
cp release-sources.example.json data/release-import/release-sources.json   # edit the jobs
sudo chown -R 999:999 data/release-import

docker compose run --rm -T release-import           # dry run: never downloads or publishes
docker compose run --rm -T release-import --apply   # publish; a second run reports "skipped"
```

Schedule it from the host's crontab:

```cron
17 */6 * * * cd /path/to/feather && docker compose run --rm -T release-import --apply >> /var/log/feather-release-import.log 2>&1
```

Supports `owner/repo` on github.com and `namespace/project` on gitlab.com; not self-hosted forges, branch builds or CI artifacts. For in-app scheduling instead, use auto-import in the admin UI.

### Telegram ingest and notifications

Forward an IPA or APK to your bot and confirm with `/add`. The bot runs against a self-hosted Bot API server (`telegram-bot-api`) because the cloud API caps downloads at 20 MB. Enable with `COMPOSE_PROFILES=telegram` once the `TELEGRAM_*` variables are set — the worker refuses to start without them.

Notifications post catalog events to a chat: `add_app`, `add_version`, `delete_app`, `delete_version`, `android_add_apk`, `health_transition` (all but the last two on by default).

### Garage / S3 storage

Store IPAs and icons in an S3-compatible bucket instead of on disk. Migrate **before** switching: run `scripts/migrate_ipas_to_garage.py` and `scripts/migrate_icons_to_garage.py` as a dry run, then with `--apply`, and only then set `STORAGE_BACKEND=garage`. Switching first makes existing apps uninstallable. Catalog URLs stay the same; the app redirects downloads to the bucket.

### Health, recovery and editorial

- **Health:** scans the iOS catalog for missing IPAs and icons, corrupt IPA files, zero sizes, duplicate versions and non-public download URLs; keeps 30 snapshots and can alert on healthy ↔ degraded changes.
- **Catalog recovery:** keeps the last 20 `source.json` snapshots with preview and typed-confirmation restore. They live in `DATA_DIR` next to the catalog, so they are not off-site backups and don't contain the binaries.
- **Editorial:** up to five featured apps and AltStore-schema news items.
- **Certificate check:** upload a `.p12` + `.mobileprovision` to see whether they pair, when each expires and which devices they cover. Nothing is signed or stored.

## Updating a deployment

```bash
docker compose pull && docker compose up -d
```

Use `pull`, not `--build`: CI already built and smoke-tested these exact images, and a local build bypasses that.

| Image | Service | Enabled by |
|---|---|---|
| `ghcr.io/t1nk333r/feather` | `altstore-manager`, `release-import` | always / profile `release-import` |
| `ghcr.io/t1nk333r/feather-bot` | `ipa-ingest-bot` | profile `telegram` |
| `ghcr.io/t1nk333r/feather-fdroid` | `fdroid-index` | profile `android` |

## Routes

Public — clients fetch these without credentials, so they must stay unauthenticated:

| Route | Purpose |
|---|---|
| `GET /source.json` | iOS catalog |
| `GET /ipas/<bundle_id>/<file>` | IPA download (302 to the bucket when using Garage) |
| `GET /icons/<bundle_id>/icon.<ext>` | app icons |
| `GET /qr`, `GET /fdroid/qr` | add-source QR codes |
| `GET /fdroid/repo/<path>` | F-Droid index, signatures, APKs, icons |
| `GET /store` | read-only storefront |
| `GET /`, `GET /sw.js` | admin UI shell (data behind it requires sign-in) |
| `POST /api/login`, `POST /api/logout`, `GET /api/session` | session |
| `GET /api/apps`, `GET /api/app/<bundle_id>` | read-only app JSON |

Everything else under `/api/` requires a session: iOS catalog edits, release import and auto-import, import history, editorial, health, catalog backups, storage tools, certificate inspection, and all `/api/android/*` routes. The full list is `grep -n "@app.route" app.py`.

## Operating notes

- **Single process.** Catalog writes use an in-process lock. Never run more than one worker process (for example `gunicorn -w 4`); raise `WAITRESS_THREADS` instead.
- **No rate limiting.** Flask-Limiter is pinned but not wired in. Rate-limit `/api/login` and uploads at your reverse proxy.
- **TLS belongs at the proxy.** The app speaks plain HTTP on port 5000.
- **The F-Droid sidecar runs as root** inside its container; the upstream image requires it. It has no ports and mounts only `data/fdroid`.
- **Back up** `data/` (catalog, binaries, icons, `fdroid/keystore.p12`), your `.env`, and any Garage bucket. Catalog snapshots alone cannot restore deleted binaries.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# run locally (there is no .env loading; export what you need)
DATA_DIR=./data ADMIN_PASSWORD=dev SECRET_KEY=dev .venv/bin/python app.py

# tests: no network, no Docker, a few seconds
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

`ADMIN_PASSWORD` must be set even for tests; the app refuses to import without it.

**CI** (`.github/workflows/ci.yml`) runs on every push and pull request: tests on Python 3.11 (the container) and 3.14, a `requests` pin check, then builds and smoke-tests all three images. Only `main` publishes to GHCR, and only after every smoke test passes.

Conventions that have each broken a build before:

- `.dockerignore` is deny-by-default. A new file a Dockerfile copies needs a matching `!path` line, and CI's image build is the only thing that will catch a missing one.
- Every pin in `requirements.txt` needs wheels for both Python 3.11 and 3.14.
- `data/` is gitignored and must never be committed.

## Repository layout

```
app.py                              the Flask app (routes, catalog, storage, Android repo)
templates/index.html                admin UI
templates/store.html                public /store preview
static/                             icons, favicons, web manifest
scripts/ipa_inspection.py           IPA metadata reader (shared)
scripts/apk_inspection.py           APK metadata + signature check (shared)
scripts/certificate_inspection.py   .p12 / .mobileprovision inspector
scripts/release_source_ingest.py    GitHub/GitLab release importer (cron + in-app)
scripts/telegram_bot_ingest.py      Telegram ingest worker
scripts/fdroid_index_loop.sh        F-Droid sidecar rebuild loop
scripts/migrate_*_to_garage.py      one-shot local → Garage migrations
Dockerfile, Dockerfile.bot, Dockerfile.fdroid, compose.yml
UPLOADING.md                        write-API contract for scripts
plans/                              design history; plans/README.md is the index
tests/                              pytest suite
```

## Further reading

- [`UPLOADING.md`](./UPLOADING.md) — publishing from scripts and CI.
- [`plans/README.md`](./plans/README.md) — every design decision, including what was rejected and why.
- [`plans/HANDOFF.md`](./plans/HANDOFF.md) — deployment traps and open operator tasks.
