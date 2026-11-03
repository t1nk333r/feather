# Feather project map

[TECH_STACK]

- Python 3.11 runs in production, and CI also tests 3.14 for compatibility. The server uses Flask 2.3.3 on Waitress 3.0.2.
- The admin UI in `templates/index.html` is vanilla HTML, CSS and JavaScript.
- iOS artifacts live on local disk or in Garage S3 storage. Android uses a local F-Droid repository and an official `fdroidserver` sidecar.
- `cryptography` 50.0.1 parses PKCS#12 files. It is the only cryptographic dependency, and it was added for certificate inspection.
- The tests run with pytest: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider`.
- Deployment uses Docker Compose. A GitHub Actions pipeline (`.github/workflows/ci.yml`) builds, smoke-tests and publishes the images.

[SYSTEM_FLOW]

1. Public clients fetch the iOS routes (`source.json`, IPAs, icons, QR) and the F-Droid routes (index, APKs, QR) without authentication. The Android QR code encodes a standard `fdroidrepos://` deep link for F-Droid clients.
2. An operator logs into the admin UI, changes the iOS and Android catalogs, and gets stable JSON responses. Agents and CI publish with an API token, through `POST /api/publish` or the MCP server. Tokens cannot delete or change settings.
3. Every import path validates artifacts before it changes the catalog. The paths are manual uploads, Telegram, one-off repository imports and scheduled repository imports.
4. Feather can check a `.p12` and `.mobileprovision` pair for usability and device coverage. It signs nothing and stores nothing.
5. With `DEVICE_BACKUPS` on, a phone that holds a `kind: backup` token can keep and fetch settings zips under `data/device-backups/<app>/<device>/` (plan 088). That token can do nothing else.
6. Feather serializes catalog changes and saves each one atomically. The F-Droid sidecar picks up rebuild markers and signs the indexes it generates.
7. CI runs the tests on the supported Python versions, builds and smoke-tests all images, and only then publishes deployable tags.

[ARCHITECTURE]

- `app.py` holds the Flask routes, the iOS source and storage managers, the Android repository manager, the scheduler, diagnostics, and the health and recovery APIs.
- `scripts/release_source_ingest.py` is the standalone engine that discovers, downloads and publishes releases. In-app imports reuse it.
- `scripts/telegram_bot_ingest.py` is the Telegram worker that ingests IPAs and APKs into Feather. It works from an allowlist and waits for an explicit `/add` confirmation.
- `scripts/ipa_inspection.py` and `scripts/apk_inspection.py` are the shared artifact validators. `ipa_inspection.py` uses only the standard library, and `apk_inspection.py` imports pyaxmlparser and Pillow when it needs them. The main image includes both, and the cron importer runs that image. The Telegram bot image includes only `ipa_inspection.py`.
- `scripts/certificate_inspection.py` inspects a PKCS#12 file and a provisioning profile. It reads the profile with the standard library, because the profile's CMS payload is cleartext, and it reads the p12 with `cryptography`.
- `scripts/fdroid_index_loop.sh` is the loop the F-Droid sidecar runs to build and sign the index.
- `templates/index.html` is the single-page admin UI.
- `tests/` is the pytest suite. Each test uses its own data directory, and no test needs an external network.
- `plans/` holds the implementation contracts and their execution status.
- `scripts/feather_mcp.py` is an MCP stdio server for agents that uses only the standard library. It calls `POST /api/publish` and the read routes with an API token.
- `UPLOADING.md` is the write API's contract for scripts and agents. It covers a token with `/api/publish` first, then the legacy session routes.
- `AGENTS.md` holds the instructions and invariants for coding agents. `CLAUDE.md` imports it.

[ORPHANS & PENDING]

- No selected implementation plan is pending. Plan 079 was rejected because its problem was already fixed. Plans 067 to 078 and 080 to 091 are complete, and 086 is research that produced no code.
- `plans/README.md` still documents the deferred audit findings. They are not authorized work until someone selects them.
- The repo does not track deployment state. `GET /api/version` reports the running commit.
