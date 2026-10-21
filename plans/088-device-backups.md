# Plan 088: Keep device backups for the operator's own phones

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW — new routes behind a default-off flag, a new token kind, and one new folder under `data/`; the only change to an existing path is that `requires_publish` refuses the new kind.
- **Depends on**: 010 (session auth), the API tokens (`/api/tokens`, `requires_publish`)
- **Status**: DONE
- **Executed at**: 2026-10-21

## Why

The operator's Android launcher (dev.omalauncher, published here) wants to back
up its own settings (home screen, widgets, drawer, style) somewhere they own,
and restore them after a reinstall or on a new phone. Android's own backup goes
to Google and has a 25 MB quota. Feather already runs on the operator's network,
already holds a per-app token system, and the phone already talks to it for
updates.

## Design

- **Off by default** (`DEVICE_BACKUPS`), per invariant 6. Off, every
  `/api/device-backups/<app>/…` route answers 404; the session-only overview
  answers `{"enabled": false}` so the admin UI can say so.
- **A new token kind, `backup`.** `POST /api/tokens` takes `kind`
  (`publish`, the default and what every older token is, or `backup`). A
  backup token must name its apps. It reaches the backup routes for those apps
  and nothing else: `requires_publish` refuses it with 403, and the backup
  routes refuse publish tokens, scoped or not. A phone holds this token, so it
  must not be able to ship an APK; and what ships an app has no business
  reading a phone's settings.
- **Storage**: `data/device-backups/<app>/<device>/<UTC stamp>[.NNN].zip`, a
  `.sha256` beside each, and `device.json` with the device's label. Names are
  checked against patterns with no separators and no leading dot, so nothing
  resolves outside the root. Uploads in the same second get `.001`, `.002`…
  so order follows upload order.
- **The archive is the app's business.** The server checks only that it is a
  zip of at most `DEVICE_BACKUP_MAX_BYTES` (256 MiB: user themes and wallpapers ride along) whose entries have no absolute,
  backslash or `..` path, at most 20,000 entries and 1 GiB unpacked, so a
  phone restoring it can't be handed a path that escapes.
- **An unchanged backup is a no-op**: the same SHA-256 as the newest snapshot
  answers `unchanged: true` and stores nothing, so a daily backup costs nothing
  on days the settings didn't change. Beyond `DEVICE_BACKUP_KEEP` the oldest go.
  `DEVICE_BACKUP_MAX_DEVICES` caps devices per app, so a token can't fill the
  disk by inventing device IDs.
- **Deleting is session-only**, like every delete (invariant 5).

## Routes

| Route | Tier | |
|---|---|---|
| `GET /api/device-backups` | session | every app's devices, for the admin UI |
| `GET /api/device-backups/<app>` | backup token / session | devices: id, label, count, size, last backup |
| `GET /api/device-backups/<app>/<device>` | backup token / session | snapshots, newest first: name, created, size, sha256 |
| `POST /api/device-backups/<app>/<device>` | backup token / session | multipart `file` (zip), optional `label` |
| `GET /api/device-backups/<app>/<device>/<name>` | backup token / session | the zip; `X-Content-SHA256` |
| `DELETE /api/device-backups/<app>/<device>[/<name>]` | session | a snapshot, or a device and all its snapshots |

## Verification

`tests/test_device_backups.py` (27 tests): off by default; token kinds; a
backup token refused by publish/read/details routes; anonymous, publish and
app-scoped publish tokens refused by backup routes; scope; round trip with
SHA-256; dedupe; ordering within one second and pruning; the device cap; bad
app/device IDs; empty, non-zip, absolute, backslash and `..` archives; the
size cap with no leftover temp file; snapshot names that try to escape; label
cleaning; session-only deletes; and the overview's fields. Each guard was
broken in turn (the publish refusal, the publish-token refusal, dedupe, the
`..` check) and a test failed each time. The admin UI panel was driven against
a local server with one backup token and one upload.
