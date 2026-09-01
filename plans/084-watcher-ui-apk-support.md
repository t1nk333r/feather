# Plan 084: Let the watcher and the "Import from Repo" UI publish APKs

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat <the SHA plan 083 landed on>..HEAD -- app.py templates/index.html`
> Compare the "Current state" excerpts against the live code before starting;
> on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M–L
- **Risk**: MED
- **Depends on**: **083** — this plan cannot start until 083 is merged
- **Category**: direction
- **Planned at**: commit `dc43400`, 2026-09-01

## Why this matters

Plan 083 taught the shared release-import engine to select, validate, and
publish APKs, but only the CLI/cron path (`process_job`) actually uses that
engine end to end. The two paths a person actually touches — the admin UI's
"Import from Repo" and the auto-import watcher that polls on a schedule —
**re-implement** the same pipeline in `app.py` and still hardcode iOS. This
plan closes that gap, which is the half the maintainer actually asked for.

After this lands, one auto-import job pointed at a cross-platform project
publishes the IPA to the AltStore catalog and the APK to the F-Droid repo on
the same schedule, with no manual Telegram forwarding.

## Current state

### Three orchestrations, not one

This is the fact that shapes the whole plan. `scripts/release_source_ingest.py`
has `process_job`, but `app.py` does **not** call it. It calls the engine's
*pieces* twice over:

1. `POST /api/import-release` (`app.py:3533`) — the UI's one-off import, which
   streams SSE progress
2. `_run_auto_import_job` (`app.py:3212`) — the watcher's per-job runner

Both do: `select_candidate` → `mkstemp(suffix=".ipa")` → `stream_download` →
`inspect_ipa_metadata` → publish in-process through `source_manager`.

`_run_auto_import_job` at `app.py:3226-3260`:

```python
        job_obj = release_ingest.Job(
            id=job.get('id') or '',
            provider=job.get('provider') or '',
            project=job.get('project') or '',
            bundle_identifier=job.get('bundleIdentifier') or "",
            asset_glob=job.get('assetGlob') or "*.ipa",
            asset_exclude_glob=job.get('assetExcludeGlob') or None,
            include_prereleases=bool(job.get('includePrereleases')),
            create_if_missing=bool(job.get('createIfMissing')),
            allowed_download_hosts=frozenset(
                h.strip().lower() for h in (job.get('allowedDownloadHosts') or []) if h.strip()
            ),
            name=job.get('name') or None,
            developer_name=job.get('developerName') or None,
        )
        candidate = release_ingest.select_candidate(job_obj, session_req, tokens, timeout=30)
        stage = "download"
        fd, tmp_path = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=".ipa")
        os.close(fd)
        _downloaded, digest = release_ingest.stream_download(
            session_req, candidate, job_obj, tmp_path, tokens,
            release_ingest.DEFAULT_TIMEOUT, release_ingest.DEFAULT_MAX_BYTES,
        )
        stage = "preflight"
        inspection = release_ingest.inspect_ipa_metadata(
            tmp_path, candidate.asset_name, job_obj, candidate)
```

Note `stage` — it is threaded through for error reporting and the UI shows it.
Keep that mechanism and add the platform to it.

### The job schema the watcher validates

`_validate_auto_import_job` at `app.py:3150-3210` is the API-facing validator.
It mirrors the engine's manifest rules deliberately (its own docstring says
so). Today it hard-requires an iOS identity at `:3172-3174`:

```python
    bundle_id = (raw.get('bundleIdentifier') or '').strip()
    if not bundle_id:
        raise ValueError("bundleIdentifier is required")
```

and defaults the glob at `:3176`: `asset_glob = (raw.get('assetGlob') or '*.ipa').strip()`.
It returns an explicit dict of keys (`:3196-3210`) — anything not listed there
is dropped, so a new field must be added in **both** the validation and the
returned dict.

### The UI form is shared between both features

`templates/index.html` has **one** form, `importRepoForm`, used for the one-off
import *and* for editing a watcher job (plan 053 consolidated the tabs).
`editAutoImportJob` at `:2987-3006` populates it field by field:

```javascript
            const f = document.getElementById('importRepoForm');
            f.provider.value = job.provider;
            f.project.value = job.project;
            f.bundleIdentifier.value = job.bundleIdentifier;
            f.assetGlob.value = job.assetGlob || '*.ipa';
```

The form's fields are at `:815-841`:

```html
                <div class="form-group">
                    <label>Bundle Identifier:</label>
                    <input type="text" name="bundleIdentifier" placeholder="leave blank to auto-detect">
                </div>
                <div class="form-group">
                    <label>Asset Glob:</label>
                    <input type="text" name="assetGlob" value="*.ipa">
                </div>
```

Two JS sites build the request body — `:1479` (one-off import) and `:1635`
(save watcher job). Both must learn the new field.

`_release_job_from_payload` at `app.py:3469-3510` normalises the shared
provider/selector fields for both the `/inspect` and `/import-release` routes,
and defaults `assetGlob` to `*.ipa` at `:3482`.

### Conventions to honour

- **All user-supplied strings rendered into the DOM go through `escapeHtml`.**
  Plan 049's reviewer checked exactly this; match it.
- The UI is the Omarchy flat theme (plan 058) on the plan 043 token system.
  Reuse existing `.form-group` markup; introduce no new colors, no inline
  styles beyond what neighbouring fields already use.
- `data/source.json` is the product. The iOS path must not change behaviour.
- Publishing happens under the catalog lock; the Android side writes through
  `android_repo`, which owns its own locking.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Setup | `python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt` | exit 0 |
| Focused | `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_import_release.py tests/test_auto_import.py -q -p no:cacheprovider` | all pass |
| Full suite | `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | 0 failures |
| Scope check | `git status --porcelain` | only in-scope files |

## Scope

**In scope**:
- `app.py` — `_validate_auto_import_job`, `_release_job_from_payload`,
  `_run_auto_import_job`, the `/api/import-release` and
  `/api/import-release/inspect` routes
- `templates/index.html`
- `tests/test_import_release.py`
- `tests/test_auto_import.py`

**Out of scope** (do NOT touch):
- `scripts/release_source_ingest.py` and `scripts/apk_inspection.py` — plan
  083 owns them. If you need an engine change, that is a STOP condition.
- `AndroidRepoManager` and every `/api/android/*` route — they already work;
  call them, do not modify them.
- The iOS publish path's behaviour.
- `scripts/telegram_bot_ingest.py`, `compose.yml`, the Dockerfiles.

## Git workflow

- Branch: `advisor/084-watcher-ui-apk-support`
- Conventional commits, one per step, `(plan 084)` suffix. Recent style:
  `feat(bot): ingest Android APKs from Telegram`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Accept an Android identity in the job schema

In `_validate_auto_import_job` (`app.py:3150`):

- add an optional `package` field, validated with the same Android package
  rule the shared inspector uses
- require **at least one** of `bundleIdentifier` / `package`, replacing the
  unconditional `bundleIdentifier is required` at `:3172-3174`; the error
  message must tell the operator that one of the two is needed
- add `package` to the returned dict at `:3196-3210` — a field missing there
  is silently dropped

Apply the same two changes to `_release_job_from_payload` (`app.py:3469`).

**Backward compatibility is a hard requirement**: every stored job in
`data/auto-import.json` today has `bundleIdentifier` and no `package`, and
must keep validating and running unchanged.

**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_auto_import.py -q -p no:cacheprovider` → all pass

### Step 2: Give the watcher a per-platform branch

Restructure `_run_auto_import_job` (`app.py:3212`) to iterate the candidate
list plan 083's `select_candidate` now returns, instead of the single
`candidate` it binds today.

Per candidate, branch on `candidate.platform`:

- **ios** — today's code path, unchanged.
- **android** — `mkstemp(..., suffix=".apk")`; validate with the engine's
  `inspect_apk_metadata`; publish by calling the same in-process
  `android_repo.add_apk(...)` + `android_repo.write_metadata(...)` pair that
  `/api/android/add-apk` uses at `app.py:4805-4820`. Treat `add_apk` returning
  `(False, ...)` as an idempotent skip, not a failure.

Keep the function's contract: it **never raises**, always returns a result
dict, and persists `lastRunAt` / `lastResult` on the stored job. Extend the
result so a per-platform outcome is visible — a job that publishes an IPA and
skips an APK must not report a bare "ok" that hides half the story. Keep
threading `stage`, and include the platform in it.

Do not call `notify` twice for one artifact; `add_apk`'s route helper already
notifies for Android — if you reuse the route's helper you get one
notification, if you reimplement it you must not add a second.

**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_auto_import.py -q -p no:cacheprovider` → all pass, new APK tests included

### Step 3: Give the one-off import route the same branch

Apply the equivalent change to `POST /api/import-release` (`app.py:3533`).
Its SSE progress events (`q.put((...))` at `app.py:3579-3590`) must name the
platform so the UI can label progress; keep the existing event shapes
backward compatible rather than renaming fields the frontend already reads at
`templates/index.html:1589` and `:1598`.

Update `/api/import-release/inspect` (`app.py:3512`) so the asset preview
reports each asset's inferred platform — that is what makes the feature
discoverable, since the operator sees `.apk` assets listed before importing.

**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_import_release.py -q -p no:cacheprovider` → all pass

### Step 4: Surface it in the UI

In `templates/index.html`:

- Add a `package` input to `importRepoForm`, directly after the Bundle
  Identifier group at `:815-818`, using the identical `.form-group` markup.
  Label it so the split is obvious, e.g. `Android Package:` with placeholder
  `com.example.app — leave blank for iOS-only`.
- Change the Asset Glob field's hardcoded `value="*.ipa"` at `:823`. It must
  still default to `*.ipa` for an iOS-only job, but an operator wanting both
  needs a glob that matches both; put the both-platform example in the
  placeholder or helper text rather than silently widening the default, which
  would change what existing one-off imports select.
- Add `package` to both request bodies — `:1479` and `:1635`.
- Populate it in `editAutoImportJob` at `:2997`, mirroring the neighbouring
  lines: `f.package.value = job.package || '';`
- Wherever a job row or import result renders the new value, pass it through
  `escapeHtml`.

**Verify**: `grep -n 'name="package"' templates/index.html` → exactly one match
**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → 0 failures

### Step 5: Confirm the whole thing end to end, offline

Add a test that drives a watcher job with a stubbed provider response
containing both an `.ipa` and an `.apk`, and asserts both artifacts reach
their respective managers. This is the test that proves the feature, so make
it assert destinations, not just a success flag.

**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider`

## Test plan

`tests/test_auto_import.py`:
- a job with only `package` validates and runs Android-only
- a job with only `bundleIdentifier` behaves exactly as before (regression)
- a job with neither is rejected with a readable message
- a run publishing both platforms records both outcomes in `lastResult`
- a re-run where `add_apk` reports already-present counts as a skip, not a
  failure, and does not raise

`tests/test_import_release.py`:
- `/inspect` labels each asset's platform
- `/api/import-release` publishes an APK to `android_repo`
- the iOS-only path's existing assertions are untouched and still pass

All new tests must make **no real network calls** — the suite's existing
convention. Follow the stubbing pattern already used in these two files.

**Prove each new test discriminates**: break the behaviour, watch the test
fail, restore it. Report which tests you did this for.

## Done criteria

ALL must hold:

- [ ] `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → 0 failures, more passing than before your change
- [ ] `grep -n 'name="package"' templates/index.html` → exactly one match
- [ ] A stored job with only `bundleIdentifier` still validates and runs (a test asserts it)
- [ ] `git status --porcelain` lists no file outside the in-scope list
- [ ] No new call to a `/api/android/*` route was added from inside `app.py` — the in-process manager is used instead
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- Plan 083 is not merged, or `select_candidate` still returns a single
  candidate rather than a list.
- The change appears to require editing `scripts/release_source_ingest.py`
  or `AndroidRepoManager`.
- Publishing an APK from inside a request would require holding the catalog
  lock and the Android lock simultaneously — report the exact call path
  rather than inventing a lock ordering.
- The full suite does not pass **before** you make any edit.
- You find that `/api/import-release`'s SSE contract cannot carry a platform
  without breaking the existing frontend handlers.

## Maintenance notes

- The real debt this plan does not pay: `process_job`, `/api/import-release`,
  and `_run_auto_import_job` are three copies of one pipeline, and this plan
  makes each of them platform-aware separately. Collapsing them onto one
  shared orchestration is the obvious follow-up and was deliberately left out
  — doing it in the same change as adding APK support would make the diff
  unreviewable. Write it as its own plan.
- A reviewer should check that the iOS path is untouched, that the new tests
  assert destinations rather than flags, and that no user string reaches the
  DOM without `escapeHtml`.
- Android metadata for a newly created F-Droid app still comes from
  `add_apk`'s defaults; enriching it from the GitHub release body is a
  separate idea, not a gap in this plan.
