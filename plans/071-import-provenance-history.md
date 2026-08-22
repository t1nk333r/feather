# Plan 071: Preserve import provenance and bounded run history outside the catalog

> **Executor instructions**: Execute after Plans 067 and 068 when following the
> recommended order. Read their landed code, reconcile drift, then follow every
> step here. Provenance is best-effort operational metadata and must never make
> catalog publication fail. Update the index status when complete unless review
> owns it.
>
> **Drift check (run first)**:
> `git diff --stat f8a8827..HEAD -- scripts/release_source_ingest.py app.py templates/index.html tests/test_release_source_ingest.py tests/test_import_release.py tests/test_auto_import.py tests/test_routes.py`
> Drift is expected if dependencies landed. Stop only if there is no longer a
> normalized candidate/preflight model or import execution flow to instrument.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW–MED
- **Depends on**: Plan 067 and Plan 068
- **Category**: direction
- **Planned at**: commit `f8a8827`, 2026-08-22

## Why this matters

Provider candidate objects already know the repository, release, and asset, but
in-app publication discards that context and scheduled jobs overwrite one
`lastResult`. After the fact, an operator cannot answer which asset produced a
version, when a selector began failing, or whether a version was manual versus
managed. Add a sanitized, bounded, atomic sidecar ledger and authenticated UI
views. Keep it out of public `source.json` and keep publication independent of
ledger health.

## Current state

`ReleaseCandidate` carries provider/project/release/asset metadata
(`scripts/release_source_ingest.py:118-132`). The standalone importer records a
subset plus checksum in its private last-state object
(`scripts/release_source_ingest.py:860-870`), but that state is one record per
job and is not mounted into the web app.

The standalone `FeatherClient` publishes through authenticated HTTP
(`scripts/release_source_ingest.py:733-807`). This is the correct boundary for a
best-effort event-report endpoint; the standalone service deliberately does not
mount the app's `DATA_DIR`.

In-app auto-import stores only `lastRunAt`/`lastResult` and overwrites them
(`app.py:2448-2557`). The jobs UI can display only that final state
(`templates/index.html:2262-2305`). Public catalog versions contain no provider
metadata (`app.py:1545-1551`), and the version editor shows only date, minimum
OS, URL, and mutation controls (`templates/index.html:1808-1851`).

Applicable constraints:

- `source.json` remains the authority for whether a version exists.
- Ledger failure/corruption must never roll back or fail a successful publish.
- Never store tokens, authorization headers, cookies, passwords, download URLs,
  temporary paths, raw exception reprs, or plist/provisioning contents.
- All ledger routes require auth; `/api/app/<id>` remains public and must not
  grow private provenance fields.
- Follow the app's temp-file + `fsync` + `os.replace` convention.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Standalone reporting | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q -k 'history or provenance or publish'` | all selected pass |
| In-app recording | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py tests/test_auto_import.py -q -k 'history or provenance or result'` | all selected pass |
| Route/UI tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'history or index'` | all selected pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | all pass |
| Diff hygiene | `git diff --check` | no output |

## Scope

**In scope**:

- `scripts/release_source_ingest.py`
- `app.py`
- `templates/index.html`
- `tests/test_release_source_ingest.py`
- `tests/test_import_release.py`
- `tests/test_auto_import.py`
- `tests/test_routes.py`

**Out of scope**:

- Public `source.json` extensions.
- Manual Add-App/Add-Version provenance; show those as Manual/Unknown.
- Telegram provenance (may adopt the endpoint later).
- A full event-sourcing system or unbounded audit log.
- Deleting history when an auto-import job or catalog version is deleted.
- Storing provider download URLs, headers, response bodies, or secrets.
- Making ledger persistence transactional with catalog writes.

## Git workflow

- Branch: `advisor/071-import-provenance-history`
- Conventional commit example: `feat(import): retain sanitized run provenance`.
- Commit ledger/API before UI if practical.
- Do not push/open a PR unless instructed.

## Target ledger contract

Create `DATA_DIR/import-history.json`:

```json
{
  "schemaVersion": 1,
  "records": []
}
```

Retain the newest 200 records globally. Each normalized record may contain only:

- generated opaque `id`;
- UTC `timestamp`, integer `durationMs`;
- `trigger`: `one-off`, `auto`, or `standalone`;
- optional `jobId`;
- `status`: `published`, `skipped`, or `error`;
- `stage`: `selection`, `download`, `preflight`, or `publish`;
- provider/project;
- release ID/tag and asset ID/name;
- bundle identifier, marketing/build version, platform;
- SHA-256 when bytes were downloaded;
- sanitized/truncated `message` (maximum 500 characters).

Absent values are omitted or `null`. No URL/path/header/token field is accepted.
Records are append-only until bounded pruning. Deleting a job/version does not
rewrite history.

## Steps

### Step 1: Add strict normalization and atomic bounded storage

In `app.py`, add load/save/append/query helpers with a dedicated lock. Loading a
missing file returns the empty v1 store. Corrupt/unknown schema logs a warning
and returns an empty store without replacing the corrupt file until a new event
is appended. Saving uses a same-directory temp file, flush, `fsync`, mode 0600,
and `os.replace`.

Normalize through an explicit field allowlist and type/length bounds. Run every
message through `_redact_secret`; strip control characters and cap at 500.
Cap project/tag/name/ID strings to reasonable documented lengths (for example
256), then retain only the last 200 records.

`append_import_record()` returns false/logs on failure and never raises to an
import caller.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'import_history_store or import_history_normalize'
```

Expected: atomic, corrupt fallback, pruning, allowlist, redaction, permission,
and failure-is-nonfatal tests pass.

### Step 2: Record one-off and auto-import lifecycle results

Instrument both in-app paths with a start monotonic time and last stage. Capture
the `(bytes, sha256)` returned by `stream_download()`; the current threaded
one-off worker must send the result through its queue rather than dropping it.

Append exactly one terminal record per attempted run:

- selection failure: provider/project/status error/stage selection;
- download/preflight/publish failure: candidate fields available so far;
- successful publish: full candidate, inspection, digest, duration;
- skipped existing version: available candidate/inspection, no invented digest.

Preserve existing NDJSON and `lastResult` contracts for compatibility. History
append happens after the authoritative terminal result is known and is wrapped
so failure cannot change that result.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py tests/test_auto_import.py -q -k 'history or provenance'
```

Expected: one record per run, correct stage/status, digest only after download,
and successful publish despite forced ledger failure.

### Step 3: Let the standalone importer report best-effort provenance

Add an authenticated web endpoint `POST /api/import-history/record` that applies
the same normalizer and forces/validates `trigger == "standalone"`. Add
`FeatherClient.record_import_event(record)` using the existing authenticated
session. A 401 remains an auth error for this call, but process_job must catch
all reporting errors after the publish result and log a redacted warning.

In `process_job()`, report published/skipped/error records when useful context is
available. The existing private state remains for idempotency and is not
replaced. A provenance POST failure must leave summary counts, state advancement,
and return value exactly as they were before this plan.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q -k 'provenance or history or state_advances'
```

Expected: payload minimization and non-fatal reporting failure tests pass; state
still advances only after verified catalog publish.

### Step 4: Add authenticated query APIs

Add `GET /api/import-history` with optional exact filters:

- `bundleIdentifier`;
- `version`;
- `jobId`;
- `limit` from 1–50, default 20.

Return newest first. Validate query length/types and never allow arbitrary
search expressions or file access. Add `GET /api/import-provenance/<bundle_id>/<version>`
as a convenience returning the newest published record or 404. Both require
auth. Keep public `/api/app/<id>` unchanged.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'import_history or import_provenance'
```

Expected: auth, exact filters, limit bounds, newest provenance, unknown version,
and public app-response non-expansion tests pass.

### Step 5: Surface job history and version provenance

In the auto-import job cards, add an expandable History action that fetches the
last 20 records by job ID and shows time/status/release/asset/version/stage/
duration. Add a “Copy diagnostic summary” action that copies only those same
sanitized fields.

When opening an app editor, fetch provenance separately after the authenticated
session check. Decorate versions with:

- Repo import / Auto-import / Standalone badge and release/asset details;
- Manual/Unknown when no record exists.

Failure to load history must not block app editing. Escape all strings. Do not
make release links because the ledger intentionally does not store provider URLs;
plain project/release text is sufficient for this MVP.

**Verify**:

```bash
rg -n "Import history|Copy diagnostic|Manual/Unknown|import-provenance" templates/index.html
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'history or provenance or index'
```

Expected: UI hooks exist and focused tests pass.

### Step 6: Complete regression and discrimination tests

Add at least these tests:

1. missing/corrupt ledger safe fallback;
2. atomic save and 0600 temp/final permissions;
3. global pruning to 200;
4. allowlist drops URL/path/header/token fields;
5. secret values and control characters are removed;
6. one-off success records candidate/digest/duration;
7. selection failure records only available fields;
8. auto skip records no invented digest;
9. forced append failure does not change import success;
10. standalone reporting payload is minimized;
11. standalone reporting failure does not change publish/state/summary;
12. query endpoints require auth and enforce filters/limits;
13. public `/api/app` has no provenance fields;
14. deleted job history remains queryable;
15. UI escapes malicious project/asset/message strings.

Prove discrimination: make ledger append raise into the import result; the
non-fatal tests must fail. Restore correct best-effort handling.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
git diff --check
git status --short
```

Expected: all tests pass; only in-scope files and index status changed.

## Done criteria

- [ ] In-app and standalone release imports produce sanitized terminal records.
- [ ] Ledger is atomic, mode 0600, bounded to 200, and append-only within retention.
- [ ] Publish/skip/error results never depend on ledger success.
- [ ] No URL/path/header/token/cookie/password/raw exception is persisted.
- [ ] Query/provenance endpoints require auth; public app/source shapes unchanged.
- [ ] Job cards show bounded history; version editor shows provenance or Unknown.
- [ ] Existing standalone state and auto-import `lastResult` remain compatible.
- [ ] Full suite passes with at least fifteen targeted cases.

## STOP conditions

- Recording provenance would need to put operational fields in public
  `source.json` or public `/api/app`.
- Standalone reporting requires mounting `DATA_DIR` into the release-import
  service. Use the HTTP boundary instead.
- A proposed field contains a provider download URL, auth header, cookie,
  password, temporary path, or raw provisioning data.
- Ledger write failure cannot be isolated from catalog publication.
- Dependencies 067/068 landed with incompatible candidate/preflight contracts;
  reconcile and report before changing their semantics.

## Maintenance notes

- When a new import trigger is added, extend the trigger allowlist and tests.
- History is operational evidence, not catalog authority; always confirm the
  version still exists before offering version-specific actions.
- If record volume grows, move to SQLite only through a separate migration plan;
  do not remove bounds from JSON.
- Telegram can later report through the same endpoint without changing the
  storage model.
