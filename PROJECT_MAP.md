# Feather Project Map

[TECH_STACK]

- Python 3.11 production / 3.14 CI compatibility; Flask 2.3.3; Waitress 3.0.2.
- Vanilla HTML/CSS/JavaScript admin UI in `templates/index.html`.
- Local or Garage S3 storage for iOS artifacts; local F-Droid repository plus an official `fdroidserver` sidecar for Android.
- `cryptography` 50.0.1 for PKCS#12 parsing; the only cryptographic dependency, added for certificate inspection.
- Pytest verification: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider`.
- Docker Compose deployment and GitHub Actions build/smoke/publish pipeline (`.github/workflows/ci.yml`).

[SYSTEM_FLOW]

1. Public clients fetch iOS `source.json`/IPA/icon/QR or F-Droid index/APK/QR routes without authentication; the Android QR encodes a standard `fdroidrepos://` client deep link.
2. An operator logs into the admin UI, mutates iOS/Android catalog state, and receives stable JSON outcomes.
3. Manual, Telegram, one-off repository, and scheduled repository imports validate artifacts before catalog mutation.
4. A `.p12` + `.mobileprovision` pair can be inspected for usability and device coverage; nothing is signed and nothing is stored.
5. Catalog mutations are serialized and atomically persisted; the F-Droid sidecar consumes rebuild markers and signs generated indexes.
6. CI tests on supported Python versions, builds all images, smoke-tests them, and only then publishes deployable tags.

[ARCHITECTURE]

- `app.py`: Flask routes, iOS source/storage managers, Android repository manager, scheduler, diagnostics, and health/recovery APIs.
- `scripts/release_source_ingest.py`: standalone release discovery/download/publish engine reused by in-app imports.
- `scripts/telegram_bot_ingest.py`: allowlisted Telegram-to-Feather IPA/APK ingest worker with explicit `/add` confirmation.
- `scripts/ipa_inspection.py`, `scripts/apk_inspection.py`: shared dependency-free artifact validators packaged with both standalone importer images.
- `scripts/certificate_inspection.py`: PKCS#12 + provisioning-profile inspector; stdlib for the profile (its CMS payload is cleartext), `cryptography` for the p12.
- `scripts/fdroid_index_loop.sh`: F-Droid index/signing sidecar loop.
- `templates/index.html`: single-page admin UI.
- `tests/`: isolated-data pytest suite; no external network required.
- `plans/`: implementation contracts and execution status.
- `UPLOADING.md`: the write API's contract for unattended clients — session flow, per-endpoint multipart field names, publish confirmation.

[ORPHANS & PENDING]

- No selected implementation plans remain pending. Plan 079 was rejected as already fixed; plans 067–078 and 080–087 are complete, and 086 is research that produced no code.
- Deferred audit findings remain documented in `plans/README.md`; they are not authorized work until selected.
- Nothing on `main` past `936e1d4` is deployed. The certificate panel additionally requires an image rebuild, because a stale image has no `cryptography` and 404s `/api/certificate/inspect`.
