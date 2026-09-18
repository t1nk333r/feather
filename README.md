# Feather

A self-hosted app-source manager for iOS and Android. It serves an AltStore/Feather
`source.json` catalog and `.ipa` binaries to iOS devices, and an optional signed F-Droid
repository to Android devices. The admin UI manages both catalogs; an optional Telegram
ingest worker can publish new iOS builds without touching the UI.

## Status and caveats

Read this before deploying:

- It is served by **Waitress** (single process, `WAITRESS_THREADS` threads, default 8)
  from `app.py`'s `__main__`. Keep TLS termination and rate limiting at a reverse proxy.
- **There is no rate limiting.** `Flask-Limiter` is pinned in `requirements.txt` but is not
  imported or wired up anywhere in `app.py`.
- The catalog write path uses an in-process lock and assumes a single worker process.
  Running it under `gunicorn -w >1` (or any multi-process server) would silently defeat
  that lock and risk corrupting `source.json`.

## Quick start (Docker)

```bash
cp .env.example .env
# edit .env and set at least ADMIN_PASSWORD

# a fresh bind mount is created root-owned; the container runs as a
# non-root user and cannot write to it until you chown it
mkdir -p data
chown -R 999:999 data

docker compose up -d
```

Browse to `http://localhost:7000`. The `chown` step is not optional — skipping it fails
with a bare `PermissionError` and no other symptom.

## Quick start (local, no Docker)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

DATA_DIR=/tmp/feather ADMIN_PASSWORD=x .venv/bin/python app.py
```

## Running the tests

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

The `ADMIN_PASSWORD` prefix is mandatory: `app.py` raises `RuntimeError` at import time if
it is unset, and the test suite imports `app.py`. The full suite runs in a few seconds with
no network access and no Docker.

## Configuration

Full annotated list, including every optional-feature variable, lives in `.env.example`.
Summary:

- **Required:** `ADMIN_PASSWORD`. The app refuses to start without it — this is the most
  likely first-run failure, and it fails loudly (`RuntimeError`) rather than serving a
  broken instance.
- **Commonly set:** `PUBLIC_BASE_URL` (the canonical origin baked into catalog URLs; if
  unset, the app falls back to the client-supplied `Host` header and logs a warning),
  `DATA_DIR`, `PORT`, `SECRET_KEY`, `MAX_CONTENT_LENGTH`.
- **Optional — S3 storage:** `STORAGE_BACKEND` plus `GARAGE_*`. Defaults to local disk
  storage for IPAs and owned app icons; unset means today's behavior is unchanged.
  `ICON_STORAGE_BACKEND` (defaults to `STORAGE_BACKEND`) can pin icons to a different
  backend than IPAs — e.g. `STORAGE_BACKEND=garage` + `ICON_STORAGE_BACKEND=local` keeps
  IPAs on Garage while icons stay on local disk at `/app/data/icons` (already a mounted
  volume in `compose.yml`).
- **Optional — Telegram ingest and notifications:** `TELEGRAM_*`, `BOT_API_*`,
  `FEATHER_*`. Off unless explicitly configured.
- **Optional — cron release importer:** `RELEASE_IMPORT_*`, `GITHUB_TOKEN`,
  `GITLAB_TOKEN` (reuses `FEATHER_BASE_URL` / `FEATHER_ADMIN_PASSWORD`). Off
  unless explicitly scheduled; see below.

## Routes

| Route | Auth | Purpose |
|---|---|---|
| `GET /source.json` | public | the catalog every iOS client polls |
| `GET /ipas/<bundle_id>/<filename>` | public | IPA download (redirects to S3 when Garage storage is enabled) |
| `GET /icons/<bundle_id>/icon.<ext>` | public | app icons |
| `GET /qr` | public | QR code for the `feather://` source URL |
| `GET /fdroid/repo/<path:filename>` | public | F-Droid indexes, signatures, and APKs |
| `GET /fdroid/qr` | public | QR code for the Android repo's subscribe URL (`?fingerprint=...`) |
| `GET /` | public | admin UI |
| `GET /sw.js` | public | service worker |
| `POST /api/login`; `POST /api/logout`; `GET /api/session` | public | session lifecycle |
| `GET /api/apps`; `GET /api/app/<bundle_identifier>` | public | read-only app JSON |
| `POST /api/add-app`; `/api/delete-app`; `/api/update-app`; `/api/add-version`; `/api/update-version`; `/api/delete-version`; `/api/update-source` | session | seven iOS catalog mutations |
| `POST /api/import-release/inspect`; `POST /api/import-release` | session | release discovery and one-off import |
| `GET /api/import-history`; `POST /api/import-history/record`; `GET /api/import-provenance/<bundle_id>/<version>` | session | private import provenance |
| `GET /api/auto-import`; `POST /api/auto-import/config`; `/api/auto-import/job`; `/api/auto-import/job/delete`; `/api/auto-import/run` | session | scheduled import jobs |
| `GET /api/editorial`; `POST /api/featured-apps`; `/api/news`; `/api/news/delete` | session | featured apps and news authoring |
| `GET /api/health`; `GET /api/health-monitor`; `POST /api/health-monitor/config` | session | health scan, history, and scheduling |
| `GET /api/catalog-backups`; `GET /api/catalog-backups/<filename>/download`; `POST /api/catalog-backups/preview`; `/api/catalog-backups/restore` | session | catalog-only recovery |
| `POST /api/reconcile-icons`; `/api/storage-selftest`; `GET /api/diagnostics` | session | storage operations and diagnostics |
| `POST /api/certificate/inspect` | session | inspect a `.p12` + `.mobileprovision` pair; signs and stores nothing |
| `GET /api/android/status`; `GET /api/android/apps` | session | Android status and app inventory |
| `POST /api/android/add-apk`; `/api/android/update-app`; `/api/android/delete-version`; `/api/android/delete-app`; `/api/android/repo-config`; `/api/android/request-update` | session | Android/F-Droid mutations |

<!-- regenerate: command grep -n "@app.route" app.py -->

The six public catalog delivery routes—`/source.json`, `/ipas/...`, `/icons/...`,
`/qr`, `/fdroid/repo/...`, and `/fdroid/qr`—must never require authentication.
iOS and F-Droid clients fetch them without credentials.

## Updating a deployment

```bash
docker compose pull && docker compose up -d
```

Use `pull`, **not `--build`**. Building locally produces a different image than the one CI
already built and smoke-tested — `docker compose build` silently bypasses that check.

CI publishes three GHCR images from the same tested commit:

| Component | Image | Compose activation |
|---|---|---|
| Core iOS/Android web app and release importer | `ghcr.io/t1nk333r/feather:latest` | always; importer uses profile `release-import` |
| Telegram IPA/APK ingest worker | `ghcr.io/t1nk333r/feather-bot:latest` | profile `telegram` |
| Android/F-Droid index signer | `ghcr.io/t1nk333r/feather-fdroid:latest` | profile `android` |

The core image serves both the iOS catalog and Android repository routes. The F-Droid
image is the optional signing/index sidecar; it does not replace the core web app.

## Optional features

**Garage S3 storage.** IPAs and owned app icons can be stored in an S3-compatible bucket
(Garage) instead of on local disk. Off by default (`STORAGE_BACKEND=local`). See
`plans/011-garage-s3-ipa-storage.md` and `plans/028-source-and-garage-app-icons.md`.
Before switching an existing deployment, run the dry-run-first
`scripts/migrate_icons_to_garage.py` migration (then `--apply`).

**Telegram ingest and notifications.** IPAs and APKs can be forwarded to the
Telegram bot and confirmed with `/add`. IPA metadata is detected locally and can be
overridden; APK identity and version are validated by Feather's Android API and queued
for an F-Droid index rebuild. Keep the `android` profile running for APKs to appear in
the signed repository. The catalog can also post Telegram notifications for mutations.
Both features are off unless the relevant `TELEGRAM_*` variables are set. See
`plans/013-telegram-bot-ingest.md` and `plans/014-telegram-notifications.md`.
Notification events are `add_app`, `add_version`, `delete_app`, `delete_version`,
`android_add_apk`, and `health_transition`; the default enables all except
`delete_version` and `health_transition`.

**Cron release importer.** A one-shot importer (`scripts/release_source_ingest.py`,
the `release-import` Compose service) can poll a configured GitHub or GitLab
repository's releases and publish new IPAs to feather automatically, without a
Telegram bot in the loop. It never touches `data/source.json`, `data/ipas/`, or
`data/icons/` directly -- it publishes through the same `/api/login` +
`/api/add-version` / `/api/add-app` HTTP API the admin UI uses. See
`plans/029-cron-release-imports.md` for the full design. Off unless you set it up:

```bash
# One-time setup
mkdir -p data/release-import
cp release-sources.example.json data/release-import/release-sources.json
# edit data/release-import/release-sources.json -- at minimum set each job's
# "project" and "bundleIdentifier"
chown -R 999:999 data/release-import

# Dry-run first: queries GitHub/GitLab + feather's public app endpoint, but
# never downloads, logs in, publishes, or writes state.
docker compose run --rm -T release-import

# Then apply. Run it twice in a row to see the idempotency: the first
# publishes, the second reports the job as skipped (already published).
docker compose run --rm -T release-import --apply
docker compose run --rm -T release-import --apply
```

Only `owner/repository` GitHub projects and `namespace/project` GitLab projects
are supported -- no self-hosted GitHub/GitLab, no branch/nightly builds, no CI
job artifacts. `--job <id>` restricts a run to one configured job (useful for
testing one entry without waiting on the others). To disable scheduling, remove
the crontab line below (or never add it) -- the Compose service is inert under
plain `docker compose up -d` regardless.

Host crontab example (adjust the path and `docker` binary for your host; do not
assume this path matches your deployment):

```cron
17 */6 * * * cd /path/to/feather && /usr/bin/docker compose run --rm -T release-import --apply >> /var/log/feather-release-import.log 2>&1
```

**Android / F-Droid repository.** Feather can also serve a third-party F-Droid repo
for Android devices, built and signed by the official `fdroidserver` tool running in
a sidecar container (`fdroid-index`, profile `android`). Feather's job is limited to
accepting APKs, writing `metadata/<package>.yml`, and asking the sidecar to rebuild;
the sidecar owns JAR-signing the index. See `plans/073-android-fdroid-repo.md` for the
full design. Off unless you enable the `android` profile:

```bash
# One-time setup
mkdir -p data/fdroid
chown -R 999:999 data/fdroid   # same reason as the top-level data/ chown above

# Set FDROID_KEYSTORE_PASSWORD in .env before first start -- the sidecar
# refuses to run without it. Dockge cannot pass `--profile`, so set
# COMPOSE_PROFILES in .env instead (or export it) rather than relying on
# a CLI flag.
echo 'COMPOSE_PROFILES=android' >> .env

docker compose up -d
```

The first start generates a signing keystore at `data/fdroid/keystore.p12`.
**Back this file up along with `FDROID_KEYSTORE_PASSWORD`** — losing either means a new
signing key, a new repository fingerprint, and every device that already subscribed
must re-add the repo. Once the sidecar has produced an index, open the admin UI's
Android tab, upload an APK, and scan the QR code. The QR encodes the standard
`fdroidrepos://<host>/fdroid/repo?fingerprint=<64-hex>` deep link so compatible
clients such as Droid-ify can claim it directly. For manual entry, use
`https://<host>/fdroid/repo` as the address and enter the fingerprint separately.

The fdroidserver base image is pinned by digest. Refresh it deliberately: resolve the
current upstream `master` digest, rerun an init plus real-APK index build, then update
the digest and fdroid version note in `Dockerfile.fdroid`.

To rotate only the keystore password, run the sidecar with both the current
`FDROID_KEYSTORE_PASSWORD` and a temporary `NEW_PASSWORD`, then execute
`keytool -storepasswd -keystore /repo/keystore.p12 -storepass:env FDROID_KEYSTORE_PASSWORD -new:env NEW_PASSWORD`.
Update `.env` immediately afterward and restart `fdroid-index`. Password rotation keeps
the key and fingerprint, so subscribed devices are unaffected; replacing
`keystore.p12` changes the fingerprint and forces every device to re-add the repo.

**Catalog health, recovery, and editorial metadata.** The Health tab can retain up
to 30 sanitized scan snapshots, run an optional default-off monitor, and alert on
healthy/degraded transitions. Catalog Recovery lists and previews the 20 local
`source.json` snapshots before a typed-confirmation restore. These snapshots are in
the same `DATA_DIR`; they are not off-site backups and contain no IPA/icon bytes.
Restoring metadata cannot recreate deleted artifacts. Back up local artifacts,
Garage data, automation configuration, signing keys, and secrets separately.

Source Information can order up to five existing bundle identifiers in
`featuredApps` and author schema-validated AltStore news. News `notify` is client
metadata only and never sends a server-side Telegram message.

## Repository layout

```
app.py                          the entire Flask app
templates/index.html            the admin UI
static/                         source icon and favicons
scripts/telegram_bot_ingest.py  optional: Telegram IPA/APK ingest worker
scripts/release_source_ingest.py  optional: cron-driven GitHub/GitLab release importer
scripts/migrate_ipas_to_garage.py  one-shot local-disk -> Garage S3 migration
scripts/migrate_icons_to_garage.py  one-shot local app-icon -> Garage migration
scripts/fdroid_index_loop.sh    optional: the fdroid-index sidecar's rebuild loop
release-sources.example.json    template for the release importer's manifest
tests/                          the test suite; no network, no Docker
plans/                          numbered implementation plans; plans/README.md is the index
```

## Contributing / conventions

- The six public catalog routes listed in [Routes](#routes) must stay public.
- `data/` is gitignored and holds the live catalog, IPAs, and icons. Never `git add` it.
- `.dockerignore` is deny-by-default. A new file a Dockerfile needs to see must be
  re-admitted there with a `!` line, or the build silently won't see it.
- Every pin in `requirements.txt` needs wheels for both the container's Python (3.11) and
  CI's (3.14). A pin that only ships a cp311 wheel has broken a build before.

## Further reading

- `plans/README.md` — index of every implementation plan, and the record of what was
  considered and rejected.
- `plans/HANDOFF.md` — deployment traps and outstanding operator tasks.
