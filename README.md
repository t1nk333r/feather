# Feather

A self-hosted AltStore/Feather iOS app-source manager. It serves a `source.json` catalog
and `.ipa` binaries to iOS devices, with an admin UI for adding and updating apps, and an
optional Telegram ingest worker for publishing new builds without touching the UI.

## Status and caveats

Read this before deploying:

- It runs the Werkzeug **development server** (`app.run()` in `app.py`), not a production
  WSGI server. Put a real reverse proxy in front of it for anything beyond a private
  network.
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
| `GET /` | public | admin UI |
| `POST /api/login`, `/api/logout`, `GET /api/session` | public | session auth |
| `GET /api/apps`, `GET /api/app/<id>` | public | read-only JSON |
| `POST /api/add-app`, `/api/delete-app`, `/api/update-app`, `/api/add-version`, `/api/update-version`, `/api/update-source` | session required | the six mutating routes |

**The first four routes must never require authentication.** iOS clients fetch them with
no credentials; gating any of them breaks every subscribed device.

## Updating a deployment

```bash
docker compose pull && docker compose up -d
```

Use `pull`, **not `--build`**. Building locally produces a different image than the one CI
already built and smoke-tested — `docker compose build` silently bypasses that check.

## Optional features

**Garage S3 storage.** IPAs and owned app icons can be stored in an S3-compatible bucket
(Garage) instead of on local disk. Off by default (`STORAGE_BACKEND=local`). See
`plans/011-garage-s3-ipa-storage.md` and `plans/028-source-and-garage-app-icons.md`.
Before switching an existing deployment, run the dry-run-first
`scripts/migrate_icons_to_garage.py` migration (then `--apply`).

**Telegram ingest and notifications.** IPAs can be forwarded to a Telegram bot and
published automatically, and the catalog can post a Telegram message on add/update/delete.
Both are off unless the relevant `TELEGRAM_*` variables are set. See
`plans/013-telegram-bot-ingest.md` and `plans/014-telegram-notifications.md`.

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

## Repository layout

```
app.py                          the entire Flask app
templates/index.html            the admin UI
static/                         source icon and favicons
scripts/telegram_bot_ingest.py  optional: forward-an-IPA-to-a-bot ingest worker
scripts/release_source_ingest.py  optional: cron-driven GitHub/GitLab release importer
scripts/migrate_ipas_to_garage.py  one-shot local-disk -> Garage S3 migration
scripts/migrate_icons_to_garage.py  one-shot local app-icon -> Garage migration
release-sources.example.json    template for the release importer's manifest
tests/                          the test suite; no network, no Docker
plans/                          numbered implementation plans; plans/README.md is the index
```

## Contributing / conventions

- The four public routes (`/source.json`, `/ipas/...`, `/icons/...`, `/qr`) must stay
  public. Do not add auth to them.
- `data/` is gitignored and holds the live catalog, IPAs, and icons. Never `git add` it.
- `.dockerignore` is deny-by-default. A new file a Dockerfile needs to see must be
  re-admitted there with a `!` line, or the build silently won't see it.
- Every pin in `requirements.txt` needs wheels for both the container's Python (3.11) and
  CI's (3.14). A pin that only ships a cp311 wheel has broken a build before.

## Further reading

- `plans/README.md` — index of every implementation plan, and the record of what was
  considered and rejected.
- `plans/HANDOFF.md` — deployment traps and outstanding operator tasks.
