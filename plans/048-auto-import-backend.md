# Plan 048: In-app auto-import backend — persistent job store, CRUD API, in-process scheduler, Run-Now

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat c052cb0..HEAD -- app.py scripts/release_source_ingest.py tests/
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 205 passed
> ```
> If the import-release route or the engine's public functions changed since
> `c052cb0`, compare the "Current state" excerpts against the live code first;
> on a semantic mismatch, STOP.

## Status

- **Priority**: P2 (replaces the `.env`/manifest/crontab workflow the maintainer wants gone)
- **Effort**: L
- **Risk**: MED–HIGH (a background thread that publishes to the catalog on a schedule)
- **Depends on**: plans 029 (the engine) + 034/035 (in-process engine reuse) — merged
- **Category**: feature
- **Planned at**: commit `c052cb0`, 2026-08-18
- **Design decided with the maintainer**: **fully in-app** — the app stores the
  repo job list AND runs imports itself on a schedule (a background thread), plus a
  Run-Now action. **No host crontab, no `.env` manifest.** This plan is the
  **backend + API only**; the admin UI is plan 049.

## Why this matters

Today the cron importer requires editing `release-sources.json`, setting
`FEATHER_*`/`GITHUB_TOKEN` env vars, and adding a host crontab line
(`plans/029`, README "Cron release importer"). The maintainer wants this managed
from the app instead. This plan makes the running web app the owner of the job
list and the scheduler: jobs live in an app-managed `data/auto-import.json`, a
CRUD API edits them, an in-process scheduler thread runs enabled jobs every N
hours, and a Run-Now endpoint imports on demand — all reusing the existing
release-ingest engine in-process (the same functions the UI "Import from Repo"
already calls). No crontab, no env manifest.

## Current state — how the UI already imports in-process

The `import-release` route (app.py ~1740–1830) builds an engine `Job` and drives
it in-process, publishing through `source_manager` (NOT the HTTP `FeatherClient`):

```python
job = release_ingest.Job(id="ui-import", provider=provider, project=project,
                         bundle_identifier=bundle_id or "", asset_glob=asset_glob or "*.ipa",
                         include_prereleases=..., create_if_missing=..., allowed_download_hosts=...,
                         name=..., developer_name=...)
candidate = release_ingest.select_candidate(job, session_req, tokens, timeout=30)
# ... stream_download(session_req, candidate, job, tmp_path, tokens, DEFAULT_TIMEOUT, DEFAULT_MAX_BYTES)
bundle_id, version, detected_name = release_ingest.extract_ipa_metadata(tmp_path, candidate.asset_name, job, candidate)
# ... then publish: source_manager.add_version(...) if the app exists,
#     else (create_if_missing) source_manager.add_app_manual(new_app, ipa_file=..., ...)
```
- `release_ingest` is the module alias for `scripts/release_source_ingest.py`.
- `tokens` is a dict of provider tokens; the UI route builds it from env
  (`GITHUB_TOKEN`/`GITLAB_TOKEN`) — reuse the same source.
- `session_req` is a `requests.Session`.
- The create branch sets name/developer/description/icon per plans 042/046.
- Tests avoid network by monkeypatching `release_ingest.select_candidate` /
  `release_ingest.stream_download` / `release_ingest.extract_ipa_metadata` and
  `source_manager.download_icon_from_url` (see `tests/test_import_release.py` top
  comment and `_reload_app_with` fixture there).

The server entrypoint (app.py `__main__`, ~line 2196):
```python
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
```

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `205 passed` before |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| New tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_auto_import.py -q` | pass |
| Full | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `205 + N passed` |

## Scope

**In scope:**
- `app.py` — an auto-import job store (load/save `data/auto-import.json` atomically
  under a lock), the CRUD + config + run routes (all `@requires_auth`), a shared
  per-job runner reusing the engine, and an in-process scheduler thread **started
  only from `__main__`**.
- `tests/test_auto_import.py` (new) — API + runner tests, no network.

**Out of scope:**
- The admin UI — that is **plan 049** (this plan ships a working API only).
- Refactoring the existing `import-release` route — leave it and its 6 tests
  untouched; write the auto-import job runner as its own function using the same
  engine calls (duplication here is safer than destabilizing the streaming route).
- The standalone `scripts/release_source_ingest.py` and the Compose `release-import`
  service — leave them; this is an additional, independent path.
- Multi-process safety — the scheduler assumes the single-process server (the
  documented model); do not add cross-process locking.

## Data model — `data/auto-import.json` (app-owned)

```json
{
  "enabled": false,
  "intervalHours": 6,
  "lastRunAt": null,
  "jobs": [
    {
      "id": "anymex",
      "provider": "github",
      "project": "RyanYuuki/AnymeX",
      "bundleIdentifier": "com.ryan.anymex",
      "assetGlob": "*.ipa",
      "includePrereleases": false,
      "createIfMissing": false,
      "name": null,
      "developerName": null,
      "allowedDownloadHosts": [],
      "enabled": true,
      "lastRunAt": null,
      "lastResult": null
    }
  ]
}
```
- `id` is a slug, unique. `enabled` (per job) and the top-level `enabled` both gate
  scheduled runs. `intervalHours` ∈ [1, 168]. `lastResult` is a short string/object
  the UI shows (e.g. `{"status":"published","version":"1.2.3"}` or `{"status":"skipped"}` / `{"status":"error","message":"..."}`).

## Steps

### Step 1: Baseline → `205 passed`. If not, STOP.

### Step 2: Job store (atomic, locked)
Add a small store with its own `threading.Lock` (mirror how `save_source` writes
atomically — temp file + `os.replace`). Functions:
- `_auto_import_path()` → `os.path.join(DATA_DIR, "auto-import.json")`.
- `load_auto_import()` → the dict above; if the file is missing/corrupt, return the
  default `{"enabled": False, "intervalHours": 6, "lastRunAt": None, "jobs": []}`
  (never raise — a GET must not 500).
- `save_auto_import(data)` → atomic write under the lock.
- Validation helper `_validate_auto_import_job(raw)` → returns a normalized job dict
  or raises `ValueError(message)`. Rules mirror the engine's manifest validation:
  `provider` ∈ {github, gitlab}; github `project` matches `release_ingest._GITHUB_PROJECT_RE`
  and is `owner/repo`; non-empty `bundleIdentifier` and `assetGlob`; gitlab requires
  a non-empty `allowedDownloadHosts` list; `createIfMissing` true requires non-empty
  `name` + `developerName`. Default `enabled` to true.

### Step 3: Shared per-job runner (the core both Run-Now and the scheduler call)
`_run_auto_import_job(job, base_url, session_req, tokens) -> dict`:
- Build `release_ingest.Job(...)` from the stored job (same fields the import-release
  route uses).
- `candidate = release_ingest.select_candidate(job_obj, session_req, tokens, timeout=DEFAULT_TIMEOUT)`.
- Download to a temp file via `release_ingest.stream_download(...)`, extract metadata
  via `release_ingest.extract_ipa_metadata(...)`, then publish exactly like the
  import-release create/add-version branches (reuse `source_manager.add_version` /
  `add_app_manual`, honoring `createIfMissing`, plus the 042/046 name/developer/
  description/icon defaults). **Idempotency**: before publishing, if the app already
  has that version, return `{"status":"skipped","version":version}` and publish
  nothing (the catalog's `add_version` is already idempotent per plan 037 — rely on
  it, but short-circuit for a clean "skipped" result).
- Always clean up the temp file. Never raise out — catch exceptions and return
  `{"status":"error","message":str(e)}`. Update the job's `lastRunAt`/`lastResult`
  and persist.
- `_run_all_enabled_jobs(base_url)` → iterate enabled jobs, run each (isolated —
  one failure doesn't stop the others), set top-level `lastRunAt`, persist.

### Step 4: Routes (all `@requires_auth`)
- `GET /api/auto-import` → `jsonify(load_auto_import())`.
- `POST /api/auto-import/config` → body `{enabled?: bool, intervalHours?: int}`; validate interval ∈ [1,168]; persist; return the updated config. Wake the scheduler (Step 6) so an interval/enable change takes effect promptly.
- `POST /api/auto-import/job` → body is a job; validate via `_validate_auto_import_job`; upsert by `id` (400 with the message on validation failure); persist.
- `POST /api/auto-import/job/delete` → `{id}`; remove; persist; 404-style `{"success":false}` if absent.
- `POST /api/auto-import/run` → `{id?}`; run one job (by id) or all enabled jobs **now**, in-process. Return a JSON summary of per-job results. (Streaming NDJSON like import-release is nice-to-have; a plain JSON summary is acceptable for the API — the UI plan 049 can switch to streaming if wanted.) Guard with the run-lock from Step 5 so a manual run can't overlap a scheduled run.

### Step 5: A run-lock
A module `threading.Lock` (`_auto_import_run_lock`) taken for the duration of any
run (manual or scheduled) so two runs never publish concurrently. If a run is
already in progress, `POST /run` returns `{"status":"busy"}` (HTTP 200) rather than
blocking or double-running.

### Step 6: The scheduler thread (started ONLY from `__main__`)
- `_auto_import_scheduler_loop(stop_event)`: a daemon loop that, each cycle, reads
  `load_auto_import()`; if top-level `enabled`, and `intervalHours` has elapsed since
  `lastRunAt` (or `lastRunAt is None`), calls `_run_all_enabled_jobs(base_url)`;
  then waits on a `threading.Event` for up to a short poll interval (e.g. `min(intervalHours*3600, 300)` seconds) so config changes are picked up within ~5 min. The `stop_event`/wake `Event` lets `POST /config` nudge it.
- Start it in the `__main__` block, **not at import time**:
  ```python
  if __name__ == '__main__':
      _start_auto_import_scheduler()   # starts the daemon thread
      app.run(host='0.0.0.0', port=PORT, debug=False)
  ```
  Starting only from `__main__` keeps the **test suite from ever spawning the
  thread** (tests import `app`, they don't run `__main__`). Do NOT start it at
  module scope.
- `base_url` for publishing: reuse whatever `resolve_base_url()`/`PUBLIC_BASE_URL`
  the import-release route uses so hosted IPA/icon URLs are correct.

### Step 7: Tests (`tests/test_auto_import.py`, new — model on `tests/test_import_release.py`'s
monkeypatch-the-engine approach; use the authed test client; NO network, NO real thread)
1. `test_auto_import_empty_by_default` — GET returns `enabled False`, `jobs []`.
2. `test_auto_import_add_and_list_job` — POST a valid github job → GET shows it.
3. `test_auto_import_rejects_bad_job` — POST provider `"svn"` / a non-`owner/repo` project / gitlab without `allowedDownloadHosts` → 400 with a message; nothing stored.
4. `test_auto_import_update_and_delete_job` — upsert same id twice (updates, not dupes); delete removes it.
5. `test_auto_import_config_bounds` — `intervalHours` 0 or 999 → 400; a valid value persists.
6. `test_auto_import_run_publishes` — monkeypatch `release_ingest.select_candidate`/`stream_download`/`extract_ipa_metadata` to canned values (as `test_import_release.py` does), one enabled create-if-missing job, POST `/api/auto-import/run` → the catalog now has the app/version; the job's `lastResult.status == "published"`.
7. `test_auto_import_run_skips_existing_version` — seed the app+version first → run → `lastResult.status == "skipped"`, no duplicate version.
8. `test_auto_import_run_one_bad_job_isolated` — two jobs, one whose monkeypatched `select_candidate` raises → the other still publishes; the bad job's `lastResult.status == "error"`.
9. `test_auto_import_routes_require_auth` — GET/POSTs unauthenticated → 401.
10. `test_importing_app_module_starts_no_thread` — importing `app` (as the suite does) leaves no auto-import scheduler thread running (assert no thread named/join needed) — proves Step 6's `__main__`-only start.

**Prove discrimination**: temporarily make `_run_auto_import_job` skip the publish
→ test 6 fails. Restore.

**Verify**: focused `tests/test_auto_import.py`; then full suite.

## Done criteria
- [ ] `data/auto-import.json` is app-managed (atomic writes, corrupt/missing → safe default; GET never 500s).
- [ ] CRUD + config routes work and validate (provider/project/bundleId/glob, gitlab hosts, interval bounds), all `@requires_auth`.
- [ ] `POST /api/auto-import/run` imports one/all enabled jobs in-process, publishing via `source_manager`, idempotent (skips an already-present version), one bad job isolated, guarded by a run-lock.
- [ ] A scheduler daemon thread runs enabled jobs every `intervalHours` — **started only from `__main__`**, so the test suite never spawns it.
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (205 + 10).
- [ ] `git status --short` shows only `app.py` and `tests/test_auto_import.py`.

## STOP conditions
- The engine's public functions (`Job`, `select_candidate`, `stream_download`,
  `extract_ipa_metadata`) don't match the import-release usage above — reconcile
  against the live route before writing the runner.
- Publishing in-process requires something the import-release route doesn't already
  do (e.g. a different base-url source) — mirror that route exactly; if you can't,
  STOP and report.
- Starting the scheduler at import time is the only way you can make it work —
  STOP; it MUST be `__main__`-only or the test suite will spawn background threads.

## Maintenance notes
- This is additive: the standalone script + Compose `release-import` service still
  work (they read the old manifest); auto-import uses its own `data/auto-import.json`.
  A future cleanup could deprecate the script once the UI (plan 049) is trusted.
- The scheduler is single-process (documented model). If the app is ever moved to
  gunicorn `-w >1`, exactly one worker must own the scheduler — note it loudly then.
- Plan 049 builds the admin UI on these endpoints (an "Auto-Import" tab: job list,
  add/edit/delete, enable + interval, Run-Now with progress).
- Reviewer: confirm the `__main__`-only thread start (test 10), the run-lock
  prevents overlap, idempotency skips existing versions, and one bad job never
  aborts the batch.
