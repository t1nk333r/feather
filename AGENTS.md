# AGENTS.md

Instructions for AI coding agents working in this repository. For the current
state of the project, open work, and operator tasks, read
[`plans/HANDOFF.md`](plans/HANDOFF.md) next. If you are an agent that wants to
**publish apps to** a Feather instance rather than change its code, skip to
[Using Feather from an agent](#using-feather-from-an-agent).

## What this is

Feather is a self-hosted app store for sideloaded apps:

- **iOS:** an AltStore/Feather/SideStore source. It serves `/source.json` plus
  IPAs and icons.
- **Android:** a signed F-Droid repository under `/fdroid/repo/`, built by an
  `fdroidserver` sidecar.
- **Admin:** a single-page UI at `/`.
- **Store:** a public read-only storefront at `/store`.

The whole server is one Flask process (Waitress). Catalogue writes are
serialised with an in-process lock, so **never run more than one app process
against the same `data/`**.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# tests: offline, no Docker, ~20 s. ADMIN_PASSWORD is mandatory -- app.py refuses to import without it
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider

# run locally (no dotenv support -- export everything; DATA_DIR defaults to the container path)
DATA_DIR=./data ADMIN_PASSWORD=pw SECRET_KEY=dev .venv/bin/python app.py
```

CI (`.github/workflows/ci.yml`) does the following:

1. Runs the tests on Python **3.11** (what the image ships) and **3.14**.
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
| `scripts/feather_mcp.py` | MCP server for agents. It is a **client**, stdlib only, and is not shipped in any image. |
| `scripts/certificate_inspection.py` | `.p12` + `.mobileprovision` inspector. It never signs anything. |
| `UPLOADING.md` | Write-API contract for scripts and agents |
| `plans/` | Design history. `plans/README.md` is the index, `plans/HANDOFF.md` the current state. |
| `tests/` | Pytest. Each test gets its own `tmp_path` data dir, and no test touches the network. |

## Invariants: do not break these

1. **Public routes stay unauthenticated:**
   - `/source.json`, `/ipas/…`, `/icons/…`, `/qr`
   - `/fdroid/repo/…`, `/fdroid/qr`
   - `/store`, `/api/apps`, `/api/app/<id>`

   Phones and F-Droid clients send no credentials; gating any of these breaks
   every subscriber.
2. **`data/` is the product.** It holds the catalogue, binaries and the
   F-Droid signing keystore. Never commit it, never bulk-rewrite it, and
   prefer read-path fixes over migrations. Losing `data/fdroid/keystore.p12`
   changes the repo fingerprint for every Android subscriber.
3. **The binary is the truth.** Bundle ID, package and version are read from
   `Info.plist` or `AndroidManifest.xml`, never from filenames (filenames
   lie). Re-publishing an existing version is a no-op, not an overwrite.
4. **Validate before storing.** Every ingest path calls the inspectors
   before anything touches storage. Keep the zip-bomb caps in the inspectors
   (plist 4 MB, manifest 16 MB, resources 64 MB, icons 8 MB).
5. **Auth tiers:**
   - `requires_auth` means an admin session only.
   - `requires_publish` means a session **or** an API token.

   Tokens may publish, edit app details and read; they must never reach
   delete, settings, token-management or restore routes. A token limited to
   named apps must be checked with `_token_may_touch(id)` in every route
   that writes an app. When you add a route, choose the tier
   deliberately. `tests/test_publish_api.py` pins the current boundaries.
6. **Optional features default off.** A new compose service gets
   `profiles:`, and new behaviour gets a default-off env flag. A pull must
   never break a working deployment.
7. **`.dockerignore` is deny-by-default.** A file an image needs must get a
   `!` line **and** a `COPY` in the same change. No test catches this; only
   the CI image smoke does. `feather_mcp.py` is deliberately not admitted.
8. **Every pin in `requirements.txt` must have wheels for cp311 and cp314.**

## How to work here

- **Test first, and prove the test discriminates:** break the behaviour and
  watch the test fail. Several "passing" tests here were found to assert
  nothing.
- **Driving a real app in a browser:** Playwright with the preinstalled
  Chromium works. Log in via `input[type=password]:visible`, because a
  hidden p12 password field matches first.
- **Match the house style.**
  - Routes return `{"success": bool, "error"|"message": …}`.
  - `ValueError` maps to 400, and unexpected errors are logged and return a
    generic message.
  - Comments explain *why*.
- **Keep changes minimal and scoped.** Don't reformat `app.py`; it is big on
  purpose (one deployable file).
- **Don't trust line numbers** in plans or older docs; re-grep.
- **Commits:** conventional-ish subjects (`fix(scope): …`, `feat(scope): …`,
  `docs: …`), with bodies that say what was verified and how.
- **Never publish infrastructure details** (LAN IPs, hostnames, proxy
  addresses) in the repo. The repo is public and its history was rewritten
  once already to remove them.

## Using Feather from an agent

A human creates an API token for you in the admin UI (**Source** tab →
*API tokens*). With it:

- **MCP** (preferred for agents):

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
- **HTTP:**

  ```bash
  curl -H "Authorization: Bearer ftr_..." -F file=@App.ipa https://<host>/api/publish
  ```

  It accepts `.ipa` or `.apk`, and the platform is detected from the file.
  The response has `added`, `created`, `id`, `version`, `downloadURL` and
  `warnings` (read them: a debuggable APK is published with a warning).
  `added: false` means that version was already there, so retries are safe.
  `POST /api/app-details` changes an existing app's details.

Android publishes answer `pending: true`: the APK becomes installable after
the F-Droid sidecar's next index rebuild (`repo_status` shows the last one).
Full contract: [`UPLOADING.md`](UPLOADING.md).
