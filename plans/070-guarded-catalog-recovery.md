# Plan 070: Expose catalog snapshots with diff, validation, and guarded restore

> **Executor instructions**: This plan touches the recovery path for the core
> catalog. Follow every verification gate, prove the tests discriminate, and
> stop instead of weakening validation. Update `plans/README.md` status when
> complete unless a reviewer owns the index.
>
> **Drift check (run first)**:
> `git diff --stat f8a8827..HEAD -- app.py templates/index.html tests/test_routes.py README.md`
> Stop if backup naming/retention, `SourceManager` locking, atomic save, or the
> Health tab has materially changed.

## Status

- **Priority**: P2
- **Effort**: M–L
- **Risk**: HIGH
- **Depends on**: none; Plan 069 is recommended first but not required
- **Category**: direction
- **Planned at**: commit `f8a8827`, 2026-08-22

## Why this matters

Every catalog mutation already preserves the previous `source.json`, but those
snapshots are accessible only through shell access. A mistaken mobile edit or
delete should be inspectable and recoverable from the authenticated admin UI.
Expose snapshot listing, download, structural diff, storage-aware preflight,
and an explicit restore that atomically backs up the current catalog first.
This is **catalog recovery**, not full disaster recovery: deleted IPA/icon
binaries cannot be recreated from JSON and must never be implied as covered.

## Current state

- All live artifacts and backups share `DATA_DIR` (`app.py:70-75`). A host loss
  therefore removes both live data and these local snapshots.
- `SourceManager` uses a single-process lock (`app.py:895-906`). All catalog
  restore work must run under that same lock.
- `_backup_source()` writes `source-YYYYMMDDTHHMMSSZ[.NNN].json` and retains the
  newest 20 (`app.py:1164-1204`).
- `save_source()` backs up first, writes a same-directory temp file, `fsync`s,
  and atomically replaces the live file (`app.py:1206-1232`). Reuse it.
- Deleting apps/versions can delete hosted binaries (`app.py:1334-1402`), so a
  JSON snapshot may reference an artifact that no longer exists.
- The Health tab has diagnostics but no recovery surface
  (`templates/index.html:875-911`).
- Tests already prove backup-before-save, atomic failure safety, no temp leaks,
  and pruning at `tests/test_routes.py:1180-1296`. Extend those patterns.

Applicable constraints:

- New endpoints are authenticated.
- Backup filenames are untrusted input even though generated locally.
- Restore must never delete/upload/move any IPA or icon.
- A failed validation/restore leaves live `source.json` byte-for-byte unchanged.
- Public `/source.json`, IPA, icon, and QR routes stay public.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Recovery tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'backup or recovery or restore'` | all selected tests pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | all pass; baseline 243 |
| Diff hygiene | `git diff --check` | no output |

## Scope

**In scope**:

- `app.py`
- `templates/index.html`
- `tests/test_routes.py`
- `README.md`

**Out of scope**:

- Backing up/restoring IPA or icon bytes.
- Remote/off-site snapshot destinations, encryption, deduplication, or archives.
- Deleting or changing backup retention.
- Restoring `auto-import.json`, health history, credentials, or `.env`.
- Editing a backup snapshot.
- Fetching missing remote artifacts during restore.
- Any unauthenticated recovery endpoint.

## Git workflow

- Branch: `advisor/070-guarded-catalog-recovery`
- Conventional commit example: `feat(health): add guarded catalog recovery`.
- Commit read-only list/download/diff before the mutating restore if practical.
- Do not push/open a PR unless instructed.

## Target API contract

Add authenticated endpoints:

- `GET /api/catalog-backups` — newest-first snapshot metadata.
- `GET /api/catalog-backups/<filename>/download` — attachment response.
- `POST /api/catalog-backups/preview` with `{filename}` — diff, SHA-256,
  validation, and candidate health.
- `POST /api/catalog-backups/restore` with
  `{filename, expectedSha256, confirm, allowMissingArtifacts}`.

Listing returns filename, byte size, SHA-256, valid flag/error category,
source name, app count, version count, and filesystem mtime in UTC. Never return
absolute paths. Preview diff returns changed source scalar names, apps
added/removed, and per-app versions added/removed—no entire catalog dump.

`confirm` must equal the exact filename. `expectedSha256` must equal the file
re-read under the catalog lock. `allowMissingArtifacts` defaults false.

## Steps

### Step 1: Add strict backup resolution and catalog validation helpers

Implement one resolver that accepts only filenames matching the existing
backup pattern, rejects separators/encoding tricks, requires
`secure_filename(name) == name`, resolves `realpath`, and proves the result's
parent is exactly `BACKUP_FOLDER`. Reject symlinks. Use it for every endpoint.

Add a validator for recovery candidates:

- root is a JSON object;
- `name` is a non-empty string;
- `apps` and `news` are lists;
- every app is an object with a unique non-empty string `bundleIdentifier` and
  a `versions` list;
- every version is an object with a unique non-empty string `version` within
  that app.

Do not require optional fields or mutate the candidate through normalization.
Validation returns safe category messages, not Python exceptions or paths.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'backup_resolver or backup_validation'
```

Expected: traversal, symlink, malformed JSON, duplicate app/version, and valid
snapshot cases pass.

### Step 2: Add read-only listing, download, and structural diff

List only files with the established backup name pattern; include invalid files
as `valid: false` with a safe reason so corruption is visible. Cap the response
to the existing maximum 20. Hash by streaming chunks, not `read()` of an
unbounded future file.

Download must use the validated exact path and set attachment disposition. It
must not serve live `source.json` or arbitrary files.

Implement a pure structural diff helper between current and candidate catalogs.
Compare bundle/version identities, not list positions. Source scalar changes
should cover `name`, `subtitle`, `description`, `website`, `iconURL`,
`headerURL`, `tintColor`, `featuredApps`, and `news` as changed/not-changed;
do not echo full descriptions/news into the response.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'catalog_backups and (list or download or preview or diff)'
```

Expected: auth, ordering, cap, metadata, safe download, and identity-based diff
tests pass.

### Step 3: Preflight candidate storage without changing live state

Refactor `_scan_catalog_health()` minimally so it may scan a supplied catalog
object while defaulting to the live source. Preview must run it against the
candidate and existing storage. Separate blocking artifact findings:

- `missing-ipa`;
- `not-a-zip`;
- hosted `missing-icon` (warning, not blocking by itself);
- empty hosted download URL.

Preview returns these findings. It never writes the candidate or artifacts.
External URLs remain unverifiable and must be labeled as such rather than
fetched.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'backup_preview or candidate_health'
```

Expected: candidate scan does not change live source bytes; missing local IPA
is clearly reported.

### Step 4: Implement one atomic guarded restore method

Add `SourceManager.restore_backup(filename, expected_sha256,
allow_missing_artifacts=False)` and keep the whole check/read/write sequence
under `self._lock`:

1. resolve/re-read the snapshot under lock;
2. stream-hash and compare with `expectedSha256` using `hmac.compare_digest`;
3. parse and validate again (do not trust preview);
4. preflight storage again;
5. refuse blocking missing/corrupt artifacts unless explicitly acknowledged;
6. call existing `save_source(candidate)` exactly once; it backs up the current
   live catalog and atomically replaces it;
7. return safe diff/health summary.

Never call `delete_ipa_file`, `delete_icon_file`, storage `put`, or any remote
download. If save fails, report failure and preserve the live file.

The route must require exact filename confirmation, expected hash, and the
separate missing-artifact acknowledgement. Log filename/hash prefix/result only;
do not log catalog content.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'restore'
```

Expected: success, stale-hash, missing confirmation, validation failure,
missing-artifact refusal/acknowledgement, concurrent mutation, and atomic
failure tests pass.

### Step 5: Add a Recovery section to Health

Add a mobile-friendly Recovery section that:

- explains “catalog only; does not contain IPA/icon files” prominently;
- lists snapshots newest first;
- previews a selected snapshot before enabling Restore;
- displays compact added/removed/changed summaries and storage warnings;
- provides Download;
- requires typing the exact filename;
- requires a separate checkbox when missing/corrupt artifacts are acknowledged;
- refreshes Source, Apps, Health, and backup list after success.

Do not use a single browser `confirm()` as the only safety barrier. Escape all
snapshot/source strings and use existing cards/buttons/toasts.

**Verify**:

```bash
rg -n "Catalog Recovery|expectedSha256|allowMissingArtifacts|catalog-backups" templates/index.html
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'index or backup or restore'
```

Expected: recovery hooks exist and focused tests pass.

### Step 6: Document boundaries and run full verification

README must state:

- snapshots cover only prior `source.json` values;
- they reside under the same `DATA_DIR` and are not off-site backups;
- restoring metadata cannot recreate deleted IPAs/icons;
- full deployment backups must separately cover local artifacts, Garage data,
  automation configuration, and secrets through the operator's backup system.

Add at least these tests:

1. all four routes require auth;
2. traversal/symlink/non-pattern names fail;
3. list includes invalid snapshot safely;
4. download serves only exact validated backup;
5. diff ignores array ordering and reports identities;
6. preview does not mutate live bytes;
7. wrong/stale hash refuses;
8. wrong confirmation refuses;
9. invalid snapshot refuses;
10. missing artifact refuses by default and succeeds only with acknowledgement;
11. successful restore creates a backup of the pre-restore live source;
12. successful restore never changes artifact bytes;
13. forced atomic save failure preserves live source;
14. concurrent mutation cannot pass a stale hash.

Prove discrimination by temporarily skipping the under-lock hash comparison;
the stale/concurrent tests must fail. Restore before final verification.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
git diff --check
git status --short
```

Expected: all tests pass; only in-scope files and index status changed.

## Done criteria

- [ ] Snapshot list/download/preview/restore are all authenticated.
- [ ] Path traversal, symlinks, stale hashes, malformed JSON, and duplicates fail.
- [ ] Preview is structural and storage-aware without mutating live state.
- [ ] Restore revalidates under the SourceManager lock and uses atomic save.
- [ ] Current live catalog is backed up before restore.
- [ ] No restore path modifies IPA/icon objects.
- [ ] Missing/corrupt artifact references require explicit acknowledgement.
- [ ] UI clearly states that snapshots are catalog-only.
- [ ] Full suite passes with at least fourteen focused cases.

## STOP conditions

- Restore requires changing backup retention/naming or bypassing `save_source()`.
- Candidate validation would reject valid existing catalog shapes not described
  here. Capture a fixture and report rather than loosening validation blindly.
- Missing-artifact detection requires downloading external or Garage IPA bodies.
- Any proposed restore action deletes, uploads, renames, or fetches an artifact.
- A symlink-safe exact path cannot be proven on the deployment filesystem.
- A full disaster-recovery claim would be made without separate binary backups.

## Maintenance notes

- Any new required source schema field must update recovery validation fixtures.
- Plan 069's health scanner refactor may overlap; reuse its supplied-catalog API
  if already landed and resolve drift explicitly.
- A future full DR plan should design independent destination, retention,
  integrity manifest, encryption, and rehearsed restore; do not bolt it onto
  this catalog-only UI.
