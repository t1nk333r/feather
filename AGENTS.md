# AGENTS.md

Instructions for AI coding agents working in this repository. For the current
state of the project, open work, and operator tasks, read
[`plans/HANDOFF.md`](plans/HANDOFF.md) next. If you are an agent that wants to
publish apps to a Feather instance rather than change its code, skip to
[Using Feather from an agent](#using-feather-from-an-agent).

## What this is

Feather is a self-hosted app store for sideloaded apps. It has four parts:

- An AltStore/Feather/SideStore source for iOS. It serves `/source.json` plus
  IPAs and icons.
- A signed F-Droid repository for Android under `/fdroid/repo/`, which an
  `fdroidserver` sidecar builds.
- A single-page admin UI at `/`.
- A public read-only storefront at `/store`.

The whole server is one Flask process running on Waitress. An in-process lock
serialises catalogue writes, so **never run more than one app process against
the same `data/`**.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# tests: offline, no Docker, ~20 s. ADMIN_PASSWORD is mandatory, because app.py refuses to import without it
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider

# run locally. There is no dotenv support, so export everything. DATA_DIR defaults to the container path
DATA_DIR=./data ADMIN_PASSWORD=pw SECRET_KEY=dev .venv/bin/python app.py
```

CI (`.github/workflows/ci.yml`) does the following:

1. Runs the tests on Python 3.11, the version the image ships, and on 3.14.
2. Checks that the `requests` pin matches `Dockerfile.bot`.
3. Builds and smoke-tests all three images.
4. Pushes the images to GHCR, only from `main`.

A branch is not done until CI is green on it.

## Layout

| Path | What |
|---|---|
| `app.py` | Everything server-side: routes, `SourceManager` (iOS catalogue), `AndroidRepoManager`, Garage/S3 storage, auto-import scheduler, API tokens, `/api/publish` |
| `templates/index.html` | The admin UI (vanilla JS, no build step) |
| `templates/store.html` | The public `/store` page. It renders only from public documents. |
| `scripts/ipa_inspection.py`, `scripts/apk_inspection.py` | Artifact validators and icon extraction, shared by the app and the importers |
| `scripts/release_source_ingest.py` | GitHub/GitLab release importer, used by the cron container and by in-app import |
| `scripts/telegram_bot_ingest.py` | Telegram ingest worker (`Dockerfile.bot`) |
| `scripts/fdroid_index_loop.sh` | F-Droid sidecar loop (`Dockerfile.fdroid`) |
| `scripts/feather_mcp.py` | MCP server for agents. It is a client, uses only the standard library, and no image ships it. |
| `scripts/certificate_inspection.py` | Inspector for a `.p12` and `.mobileprovision` pair. It never signs anything. |
| `UPLOADING.md` | Write-API contract for scripts and agents |
| `plans/` | Design history. `plans/README.md` is the index, and `plans/HANDOFF.md` is the current state. |
| `tests/` | Pytest. Each test gets its own `tmp_path` data dir, and no test touches the network. |

## Invariants you must not break

1. **Public routes stay unauthenticated.** These are the public routes:
   - `/source.json`, `/ipas/…`, `/icons/…`, `/qr`
   - `/fdroid/repo/…`, `/fdroid/qr`
   - `/store`, `/api/apps`, `/api/app/<id>`, `/api/version`
   - operator pages from `data/site/` (`/.well-known/…` and the like), which
     the 404 handler serves with a sandboxing CSP once no route matches

   Phones and F-Droid clients send no credentials, so gating any of these
   breaks every subscriber.
2. **`data/` is the product.** It holds the catalogue, the binaries and the
   F-Droid signing keystore. Never commit it, never bulk-rewrite it, and
   prefer read-path fixes over migrations. Losing `data/fdroid/keystore.p12`
   changes the repo fingerprint for every Android subscriber.
3. **Metadata comes from the binary.** Feather reads the bundle ID, package
   and version from `Info.plist` or `AndroidManifest.xml`, never from
   filenames, because filenames lie. It reads an iOS app's `appPermissions`
   from its executables' signed entitlements and plists. Re-publishing an
   existing version never overwrites it. A re-publish stores nothing, and
   through iOS `/api/publish` it only declares the permissions that the
   binary carries and the app lacks.
4. **Validate before storing.** Every ingest path calls the inspectors
   before anything touches storage. Keep the inspectors' zip-bomb caps:
   plist 4 MB, manifest 16 MB, resources 64 MB, icons 8 MB. Keep the IPA
   entitlement reader's per-inspection budgets too: 2 GiB of executables
   inflated and 4 MiB of entitlements parsed. `tests/test_ipa_inspection.py`
   pins every default. IPA inspection runs under the catalogue lock.
5. **Auth tiers.** There are three:
   - `requires_auth` means an admin session only.
   - `requires_publish` means a session or an API token.
   - `requires_backup` means a session or a `kind: backup` token, and only
     while `DEVICE_BACKUPS` is on (plan 088).

   Tokens may publish, edit app details and read. They must never reach
   delete, settings, token-management or restore routes. A backup token
   reaches only the device-backup routes of its named apps.
   `requires_publish` refuses backup tokens, and `requires_backup` refuses
   publish tokens. Every route that writes an app must check a token limited
   to named apps with `_token_may_touch(id)`. When you add a route, decide
   which tier it needs. `tests/test_publish_api.py` pins the current
   boundaries.
6. **Optional features default off.** A new compose service gets
   `profiles:`, and new behaviour gets a default-off env flag. A pull must
   never break a working deployment.
7. **`.dockerignore` is deny-by-default.** A file an image needs must get
   both a `!` line and a `COPY` in the same change. Nothing in `tests/`
   catches a missing one. Only the CI image smoke test does. `.dockerignore`
   leaves out `feather_mcp.py` on purpose.
8. **Every pin in `requirements.txt` must have wheels for cp311 and cp314.**

## How to work here

- **Test first, and prove the test discriminates.** Break the behaviour and
  watch the test fail. Several "passing" tests here turned out to assert
  nothing.
- **Drive a real app in a browser with Playwright** and the preinstalled
  Chromium. Log in through `input[type=password]:visible`, because a hidden
  p12 password field matches first.
- **Match the house style.**
  - Routes return `{"success": bool, "error"|"message": …}`.
  - `ValueError` maps to 400. Routes log unexpected errors and return a
    generic message.
  - Comments explain *why*.
- **Keep changes minimal and scoped.** Don't reformat `app.py`. It is big on
  purpose, because it is the one deployable file.
- **Don't trust line numbers** in plans or older docs. Grep again.
- **Use conventional-ish commit subjects** (`fix(scope): …`,
  `feat(scope): …`, `docs: …`), with bodies that say what you verified and
  how.
- **Never publish infrastructure details** (LAN IPs, hostnames, proxy
  addresses) in the repo. The repo is public, and its history was already
  rewritten once to remove them.

## Using Feather from an agent

A human creates an API token for you in the admin UI, in the *API tokens*
section of the Source tab. You can use it in two ways.

- MCP is the preferred way for agents:

  ```bash
  claude mcp add feather --env FEATHER_URL=https://<host> --env FEATHER_TOKEN=ftr_... \
      -- python3 scripts/feather_mcp.py
  ```

  It provides these tools:
  - `publish_app` takes a `path` or a `url`, and optionally `whats_new`
    (release notes), `create_if_missing`, and app details (`name`,
    `developer_name`, `summary`, `description`, `license`, `website`,
    `source_code`, `categories`) that apply when the app is created.
  - `update_app` changes an existing app's details.
  - `list_apps`
  - `get_app`
  - `repo_status`
- Plain HTTP works too:

  ```bash
  curl -H "Authorization: Bearer ftr_..." -F file=@App.ipa https://<host>/api/publish
  ```

  It accepts an `.ipa` or an `.apk` and detects the platform from the file.
  The response has `added`, `created`, `id`, `version`, `downloadURL` and
  `warnings`. Read the warnings, because Feather publishes a debuggable APK
  with only a warning. `added: false` means that version was already there,
  so retries are safe. `POST /api/app-details` changes an existing app's
  details.

Android publishes answer `pending: true`. The APK becomes installable after
the F-Droid sidecar's next index rebuild, and `repo_status` shows the last
one. [`UPLOADING.md`](UPLOADING.md) has the full contract.
