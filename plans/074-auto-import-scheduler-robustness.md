# Plan 074: Make the auto-import scheduler actually run off-request, and stop losing edits to `auto-import.json`

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> ```
> cd <repo root>
> git rev-parse --short HEAD                        # plan written at 5e8f447
> git diff --stat 5e8f447..HEAD -- app.py tests/test_auto_import.py
> ```
> If either file changed, compare the "Current state" excerpts against the
> live code before proceeding; on a mismatch, STOP.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW — one call swapped for an existing safe helper, one lock widened around code that already exists, one pure function extracted; no route contract changes.
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `5e8f447`, 2026-08-28

## Why this matters

Plan 048 added an in-process scheduler thread that runs configured GitHub/GitLab release imports every `intervalHours`. Two defects, both invisible in the test suite because no test drives the scheduler loop:

1. **The thread calls `resolve_base_url()`**, which — when `PUBLIC_BASE_URL` is unset — reads `request.url_root`. There is no request on the scheduler thread, so Flask raises `RuntimeError: Working outside of request context`. The loop's `except Exception: logging.exception(...)` swallows it and sleeps; **no job ever runs**, while the UI shows `enabled: true` and "Run now" works (it *is* in a request). A helper that handles exactly this, `_safe_base_url()`, was added for the Android work and not applied here. The production deployment sets `PUBLIC_BASE_URL`, so this is latent there — but the README documents the Host-header fallback as supported, and any deployment relying on it has a scheduler that silently does nothing.
2. **Lost updates.** `_AUTO_IMPORT_WRITE_LOCK` guards only the file write inside `save_auto_import`. Five call sites do `load_auto_import()` → mutate → `save_auto_import()` with no lock around the sequence: the scheduler's per-job result stamp, `_run_all_enabled_jobs`' top-level `lastRunAt` stamp, and the three admin routes (`config`, `job` upsert, `job/delete`). A job runs for minutes (a full IPA download). An operator who adds, edits, or deletes a job during a run has their change overwritten when the run stamps its result — and the UI told them the save succeeded.

The scheduling predicate (is a run due?) is also untested, so the fix adds a pure function and table-tests it.

## Current state

`app.py:3002-3037` — the scheduler loop (the bug is the `resolve_base_url()` call at line 3029):

```python
def _auto_import_scheduler_loop(stop_event):
    while not stop_event.is_set():
        interval_hours = 6
        try:
            data = load_auto_import()
            interval_hours = data.get('intervalHours') or 6
            if data.get('enabled'):
                last_run_at = data.get('lastRunAt')
                should_run = not last_run_at
                if not should_run:
                    try:
                        last_dt = datetime.fromisoformat(last_run_at)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                        should_run = elapsed >= interval_hours * 3600
                    except ValueError:
                        should_run = True
                if should_run:
                    if _auto_import_run_lock.acquire(blocking=False):
                        try:
                            _run_all_enabled_jobs(resolve_base_url())
                        finally:
                            _auto_import_run_lock.release()
        except Exception:
            logging.exception("auto-import scheduler cycle failed")

        wait_s = min(max(interval_hours, 1) * 3600, 300)
        _auto_import_wake_event.wait(timeout=wait_s)
        _auto_import_wake_event.clear()
```

`app.py:190-203` — `resolve_base_url()` ends with `return request.url_root.rstrip('/')` when `PUBLIC_BASE_URL` is unset.

`app.py:1686-1694` — the safe helper that already exists:

```python
def _safe_base_url():
    """resolve_base_url() outside a request context raises RuntimeError.
    Callers that may run outside a request (module-scope smoke checks,
    scripts) use this instead and treat None as "unknown right now"."""
    try:
        return resolve_base_url()
    except RuntimeError:
        return None
```

`app.py:2749-2751` — the locks:

```python
_AUTO_IMPORT_WRITE_LOCK = threading.Lock()
_auto_import_run_lock = threading.Lock()
_auto_import_wake_event = threading.Event()
```

`app.py:2781-2800` — `save_auto_import(data)` takes `_AUTO_IMPORT_WRITE_LOCK` around temp-file + `os.replace` only.

The five unguarded read-modify-write sites:
- `app.py:2966-2973` — per-job result stamp at the end of `_run_all_enabled_jobs`'s helper (`data = load_auto_import()` … `save_auto_import(data)`).
- `app.py:2996-2998` — top-level `lastRunAt` stamp at the end of `_run_all_enabled_jobs`.
- `app.py:3217-3232` — `auto_import_config`.
- `app.py:3237-3258` — `auto_import_upsert_job`.
- `app.py:3261-3273` — `auto_import_delete_job`.

Conventions: `logging.warning(...)`/`logging.exception(...)` with f-strings or `%s`; module-level helpers are `_snake_case`; tests live in `tests/test_auto_import.py`, which has its own `client`/`authed_client` fixtures (lines 70-120) and already monkeypatches `release_ingest.select_candidate`/`stream_download` so no network is touched. Exemplar test: `test_importing_app_module_starts_no_thread` (line 413).

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Create venv + install | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt` | exit 0 |
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | `259 passed, 1 skipped` |
| Focused | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_auto_import.py -q -p no:cacheprovider` | all pass |

No lint/typecheck exists in this repo.

## Scope

**In scope**: `app.py` (only the auto-import region, lines ~2740-3050 and the three routes at ~3211-3275), `tests/test_auto_import.py`.

**Out of scope — do NOT touch**: `resolve_base_url()` itself; `_safe_base_url()` itself; `_run_auto_import_job`'s download/publish logic; `/api/auto-import/run` (it is already inside a request and holds `_auto_import_run_lock`); `_start_auto_import_scheduler` (must stay `__main__`-only); any other test file; `SourceManager`.

## Git workflow

- Branch: `advisor/074-auto-import-scheduler-robustness`
- Commits per step, e.g. `fix(auto-import): scheduler uses _safe_base_url, skips cycle when unknown (plan 074)`, `fix(auto-import): guard read-modify-write of auto-import.json (plan 074)`, `test(auto-import): table-test the due predicate (plan 074)`.
- Do not push.

## Steps

### Step 1: Extract the due-predicate as a pure function

Above `_auto_import_scheduler_loop`, add:

```python
def _auto_import_due(data, now):
    """True when a scheduled run is due. Pure: no I/O, no clock -- `now` is
    an aware UTC datetime supplied by the caller so this can be table-tested.
    Missing/unparseable lastRunAt counts as due (a never-run store must run)."""
    if not data.get('enabled'):
        return False
    interval_hours = data.get('intervalHours') or 6
    last_run_at = data.get('lastRunAt')
    if not last_run_at:
        return True
    try:
        last_dt = datetime.fromisoformat(last_run_at)
    except (TypeError, ValueError):
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return (now - last_dt).total_seconds() >= interval_hours * 3600
```

Replace lines 3012-3027 of the loop body (everything from `if data.get('enabled'):` through the `should_run` computation) with `should_run = _auto_import_due(data, datetime.now(timezone.utc))`, keeping `interval_hours = data.get('intervalHours') or 6` for the sleep calculation.

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -c "import app, datetime as d; now=d.datetime.now(d.timezone.utc); print(app._auto_import_due({'enabled':True}, now), app._auto_import_due({'enabled':False}, now), app._auto_import_due({'enabled':True,'intervalHours':6,'lastRunAt':now.isoformat()}, now))"` → `True False False`.

### Step 2: Never call `resolve_base_url()` from the scheduler thread

In the loop, replace `_run_all_enabled_jobs(resolve_base_url())` with:

```python
base_url = _safe_base_url()
if base_url is None:
    logging.warning(
        "auto-import scheduler: PUBLIC_BASE_URL is not set and there is no "
        "request context to derive a base URL from -- skipping this cycle. "
        "Set PUBLIC_BASE_URL to enable scheduled imports."
    )
else:
    _run_all_enabled_jobs(base_url)
```
(inside the existing `if _auto_import_run_lock.acquire(blocking=False): try/finally`.)

**Verify**: `command grep -n "resolve_base_url()" app.py` shows no occurrence between the `_auto_import_scheduler_loop` definition and `_start_auto_import_scheduler`.

### Step 3: Guard every load→mutate→save with the write lock

Add, next to `save_auto_import`:

```python
def mutate_auto_import(fn):
    """Atomically load, mutate, and save the auto-import store. `fn(data)`
    mutates in place and returns whatever the caller wants back. Holds
    _AUTO_IMPORT_WRITE_LOCK across the whole sequence so a scheduler stamp
    and an admin edit can never overwrite each other. Never hold this lock
    across a job run -- only across the read-modify-write."""
    with _AUTO_IMPORT_WRITE_LOCK:
        data = load_auto_import()
        result = fn(data)
        _save_auto_import_unlocked(data)
        return result
```

Rename the body of `save_auto_import` to `_save_auto_import_unlocked(data)` (everything currently inside the `with _AUTO_IMPORT_WRITE_LOCK:`), and make `save_auto_import(data)` a thin wrapper that takes the lock and calls it — so `_AUTO_IMPORT_WRITE_LOCK` is never acquired twice (it is a plain `Lock`, not an `RLock`; re-entry would deadlock).

Convert the five sites to `mutate_auto_import(...)`:
- per-job stamp (`app.py:2966-2973`): `mutate_auto_import(lambda data: _stamp_job(data, job.get('id'), result))` with a small `_stamp_job` helper doing the loop currently inline; keep the surrounding `try/except Exception: logging.exception(...)`.
- top-level stamp (`app.py:2996-2998`): `mutate_auto_import(lambda data: data.__setitem__('lastRunAt', datetime.now(timezone.utc).isoformat()))`.
- `auto_import_config`: validate `payload` first (the existing 400 checks stay before the mutation), then `mutate_auto_import` applying `intervalHours`/`enabled`; the route's response must still return the updated store.
- `auto_import_upsert_job`: validation stays first; the merge-with-existing logic moves into the callback.
- `auto_import_delete_job`: the "Job not found" 404 decision must be made *inside* the callback (return a flag) so the check and the removal are atomic.

**Verify**: `command grep -n "load_auto_import()" app.py` → occurrences remain only in `get_auto_import` (read-only route), `mutate_auto_import`, `_run_all_enabled_jobs`' initial job-list read (read-only), and the scheduler loop's read-only check. Every site that also calls `save_auto_import` is gone. `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_auto_import.py -q` → all existing tests pass.

### Step 4: Tests

In `tests/test_auto_import.py` add:

1. `test_auto_import_due_table` — parametrised over: disabled → False; enabled + no `lastRunAt` → True; elapsed < interval → False; elapsed ≥ interval → True; naive timestamp treated as UTC; garbage `lastRunAt` → True; `intervalHours` missing → defaults to 6.
2. `test_scheduler_cycle_runs_jobs_without_request_context` — with `PUBLIC_BASE_URL` **unset** (`monkeypatch.delenv`) and set (`client_with_base_url`-style fixture; create one in this file mirroring `tests/test_routes.py:120-140` if none exists): monkeypatch `app_module._run_all_enabled_jobs` with a recorder, write an enabled store with `lastRunAt: None`, and run **one iteration** of the loop body. To make one iteration runnable without threads, refactor the loop so its body is `_auto_import_scheduler_cycle()` (returns `interval_hours`) and `_auto_import_scheduler_loop` just loops over it — call the cycle function directly in the test. Assert: unset → recorder not called **and** a warning containing `PUBLIC_BASE_URL` was logged (`caplog`); set → recorder called once with the base URL.
3. `test_admin_edit_survives_concurrent_job_stamp` — seed a store with job `A`; call `mutate_auto_import` from a thread that sleeps 0.2 s inside its callback before returning (simulating a slow stamp), and meanwhile `POST /api/auto-import/job` adding job `B`; after both finish, `GET /api/auto-import` shows both `A` and `B`. Prove the test discriminates: temporarily replace the route's `mutate_auto_import` with the old load/save pair, run → **fails**; restore → passes. Report both runs.

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_auto_import.py -q` → `10 + 3 = 13 passed` (the parametrised test counts as one file-level test id per case — state the actual number).

## Test plan

See Step 4. Pattern file: `tests/test_auto_import.py` (fixtures at lines 70-120; `test_importing_app_module_starts_no_thread` at 413 shows the module-reload approach). No new test may start the real scheduler thread.

## Done criteria

- [ ] `command grep -c "resolve_base_url()" app.py` is exactly one less than at `5e8f447` (the scheduler call is gone; no other caller changed)
- [ ] `command grep -n "def _auto_import_due\|def mutate_auto_import\|def _auto_import_scheduler_cycle" app.py` → three hits
- [ ] No function in `app.py` calls `load_auto_import()` and `save_auto_import()` in the same body (inspect the five listed sites)
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → `259 + N passed, 1 skipped` where N is the number of new test ids; state N
- [ ] The concurrency test was shown to fail against the old code (both runs reported)
- [ ] `git status --short` shows only `app.py` and `tests/test_auto_import.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

- `_AUTO_IMPORT_WRITE_LOCK` is an `RLock` or is acquired anywhere other than `save_auto_import` at `5e8f447` — the plan's re-entrancy reasoning would be wrong.
- `_safe_base_url()` does not exist at `app.py:1686` — do not write your own; report.
- Any existing test in `tests/test_auto_import.py` fails after Step 3 for a reason other than the lock refactor's known shape (e.g. a route returned a different JSON shape) — report rather than adjust the test.
- You are tempted to hold `_AUTO_IMPORT_WRITE_LOCK` across `_run_auto_import_job` — do not; that would block admin edits for the whole download.

## Maintenance notes

- Any future code that reads then writes `auto-import.json` must go through `mutate_auto_import`. Reviewers: reject a new `load_auto_import()` … `save_auto_import()` pair.
- If a job-status field is ever added, stamp it via `mutate_auto_import` too.
- The scheduler skipping with a warning when `PUBLIC_BASE_URL` is unset is deliberate: baking a client-supplied Host into `source.json` from a background thread is impossible (no request) and undesirable anyway. Plan 008 already recommends setting `PUBLIC_BASE_URL`; consider making it required for `enabled: true` in a later change (`POST /api/auto-import/config` could 400 when enabling without it).
