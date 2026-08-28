# Feather Project Map

[TECH_STACK]

- Python 3.11 production / 3.14 CI compatibility; Flask 2.3.3; Waitress 3.0.2.
- Vanilla HTML/CSS/JavaScript admin UI in `templates/index.html`.
- Local or Garage S3 storage for iOS artifacts; local F-Droid repository plus an official `fdroidserver` sidecar for Android.
- Pytest verification: `ADMIN_PASSWORD=x /tmp/feather-verify/bin/python -m pytest tests/ -q -p no:cacheprovider`.
- Docker Compose deployment and Jenkins build/smoke/publish pipeline.

[SYSTEM_FLOW]

1. Public clients fetch iOS `source.json`/IPA/icon/QR or F-Droid index/APK/QR routes without authentication.
2. An operator logs into the admin UI, mutates iOS/Android catalog state, and receives stable JSON outcomes.
3. Manual, Telegram, one-off repository, and scheduled repository imports validate artifacts before catalog mutation.
4. Catalog mutations are serialized and atomically persisted; the F-Droid sidecar consumes rebuild markers and signs generated indexes.
5. CI tests on supported Python versions, builds all images, smoke-tests them, and only then publishes deployable tags.

[ARCHITECTURE]

- `app.py`: Flask routes, iOS source/storage managers, Android repository manager, scheduler, diagnostics, and health/recovery APIs.
- `scripts/release_source_ingest.py`: standalone release discovery/download/publish engine reused by in-app imports.
- `scripts/telegram_bot_ingest.py`: Telegram-to-Feather ingest worker.
- `scripts/fdroid_index_loop.sh`: F-Droid index/signing sidecar loop.
- `templates/index.html`: single-page admin UI.
- `tests/`: isolated-data pytest suite; no external network required.
- `plans/`: implementation contracts and execution status.

[ORPHANS & PENDING]

- No selected implementation plans remain pending. Plan 079 was rejected as already fixed; plans 067–078 and 080–081 are complete.
- Deferred audit findings remain documented in `plans/README.md`; they are not authorized work until selected.
