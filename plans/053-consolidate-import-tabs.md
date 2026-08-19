# Plan 053: Consolidate "Import from Repo" and "Auto-Import" into one tab (one form, two actions)

> **Executor instructions**: Follow this plan step by step. This is a UI-only
> change to `templates/index.html` (plus one existing test update). Do NOT change
> `app.py` or any server code — both APIs already exist. Run the verification
> gates. If a STOP condition occurs, stop and report. Update this plan's status
> row in `plans/README.md` when done unless a reviewer maintains the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 22b1836..HEAD -- templates/index.html tests/test_routes.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 222 passed
> ```
> If the two tab panels or their JS changed since `22b1836`, compare the "Current
> state" excerpts below against the live file first; on a mismatch, STOP.

## Status

- **Priority**: P2 (UX — two tabs do the same core thing; user asked to merge them)
- **Effort**: M
- **Risk**: MED (substantial JS rewiring in one template; both import flows must keep working)
- **Depends on**: plans 034/035 (one-shot import) + 048/049 (auto-import) — merged
- **Category**: UX / consolidation
- **Planned at**: commit `22b1836`, 2026-08-19

## Why this matters

There are two tabs for importing an app from a git repo: **Import from Repo**
(`#import-repo`) does a one-off streaming import, and **Auto-Import**
(`#auto-import`, plan 049) manages scheduled jobs — with two nearly identical repo
forms. The maintainer wants them in **one place**. Decided shape: **one tab, one
form, two actions** — fill the repo form once, then either **Import now** (one-off,
live streaming) or **Save & auto-import** (add a scheduled job); the jobs list +
schedule controls live below the form. Nothing is lost; the duplicate tab and its
duplicate form go away. Both server APIs already exist — this is UI-only.

## Current state — `templates/index.html`

- Tab nav buttons (lines 603, 605):
  ```html
  <button class="tab" onclick="switchTab('import-repo', this)">Import from Repo</button>
  <button class="tab" onclick="switchTab('auto-import', this)">Auto-Import</button>
  ```
- **`#import-repo` panel (844–~902)**: `<form id="importRepoForm">` with fields via
  `name=`: `provider`, `project`, `bundleIdentifier`, `assetGlob`,
  `includePrereleases` (id), `createIfMissing` (id), `allowedDownloadHosts`, `name`,
  `developerName`, `iconURL`; a submit `<button>Import</button>`; and an
  `#importProgress` (progress bar + `#importStatus`). Its submit handler (JS ~1250)
  builds a body and streams `POST /api/import-release` (NDJSON progress reader).
- **`#auto-import` panel (922–~1010)**: a Schedule block (`#autoImportEnabled`
  checkbox, `#autoImportInterval` number, `saveAutoImportConfig()` btn,
  `runAutoImport(null,this)` "Run All Now" btn, `#autoImportLastRun`); a jobs list
  `#autoImportJobsList`; and a SEPARATE `<form id="autoImportJobForm">` with `id=`
  fields (`jobId`, `jobProvider`, `jobProject`, `jobBundleIdentifier`,
  `jobAssetGlob`, `jobIncludePrereleases`, `jobCreateIfMissing`, `jobName`,
  `jobDeveloperName`, `jobAllowedDownloadHosts`, `jobEnabled`) + a submit that POSTs
  `POST /api/auto-import/job`.
- Auto-import JS (~2079–2320): `toggleAutoImportJobFields`, `formatAutoImportResult`,
  `renderAutoImportJobs`, `loadAutoImport` (`GET /api/auto-import`),
  `saveAutoImportConfig`, `resetAutoImportJobForm`, `editAutoImportJob` (repopulates
  `autoImportJobForm`), the `autoImportJobForm` submit listener,
  `toggleAutoImportJobEnabled`, `deleteAutoImportJob`, `runAutoImport`.
- The `/api/auto-import/job` body needs: `id`, `provider`, `project`,
  `bundleIdentifier`, `assetGlob`, `includePrereleases`, `createIfMissing`, `name`,
  `developerName`, `allowedDownloadHosts`, `enabled`. (It does NOT use `iconURL`.)
  `/api/import-release` uses the repo fields + `iconURL` (no `id`/`enabled`).
- A test asserts the auto-import tab renders — find it:
  `grep -n "auto-import" tests/test_routes.py`. It must be updated (the tab id moves).
- Styling: iOS tokens (043), `showToast`/`setButtonLoading`/`escapeHtml`, no emoji.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `222 passed` before |
| Style block intact | `grep -c "<style>\|</style>" templates/index.html` | `2` |
| Old tab gone | `grep -c "switchTab('auto-import'" templates/index.html` | `0` |
| Full | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `222 passed` (same count; update the smoke test, don't add) |

## Scope

**In scope:**
- `templates/index.html` — merge the two panels into `#import-repo`; remove the
  `auto-import` nav button and panel; add the second action button + the two
  job-only fields to the shared form; retarget the auto-import JS to the shared form.
- `tests/test_routes.py` — update the one render-smoke assertion that references the
  auto-import tab so it matches the merged tab.

**Out of scope:**
- `app.py` / any server endpoint — both `/api/import-release` and
  `/api/auto-import/*` already exist and are unchanged. If you think you need a
  server change, STOP.
- Changing what the APIs accept/return.
- Removing the one-off streaming import (keep it — it's the "Import now" action).

## Steps

### Step 1: Baseline → `222 passed`. If not, STOP.

### Step 2: Merge the markup into `#import-repo`
- Delete the `auto-import` nav button (line 605).
- In the `importRepoForm`, add two job-only fields (place them together, labeled as
  auto-import options): a `Job ID` text input (`name="jobId"`, placeholder
  "blank = derived from repo") and an `Enabled` checkbox (`name="jobEnabled"`,
  checked). Keep `iconURL` (used by Import now).
- Change the submit button text to **Import now** (keep it `type="submit"` so Enter
  imports now), and add a second button right after it:
  `<button type="button" class="btn btn-secondary" id="saveJobBtn" onclick="saveRepoAsJob()">Save &amp; auto-import</button>`.
- Below the form, MOVE the auto-import Schedule block, the `#autoImportJobsList`, and
  the `#autoImportLastRun` from the old panel into `#import-repo` (keep their ids
  and their `saveAutoImportConfig()` / `runAutoImport(null,this)` handlers).
- Delete the entire old `#auto-import` panel (including its now-redundant
  `autoImportJobForm`).

### Step 3: Retarget the JS to the shared form
- **Keep** the `importRepoForm` submit handler (Import now / streaming) unchanged.
- **Add** `saveRepoAsJob()`: read the SHARED form (`const f = document.getElementById('importRepoForm')`), build the `/api/auto-import/job` body from `f.provider.value`, `f.project.value`, `f.bundleIdentifier.value`, `f.assetGlob.value || '*.ipa'`, `f.includePrereleases.checked`, `f.createIfMissing.checked`, `f.name.value`, `f.developerName.value`, `f.allowedDownloadHosts.value.split(',')…`, `f.jobEnabled.checked`, and `id`: `f.jobId.value.trim() || <derive from project>` (derive = `project.trim().replace(/^https?:\/\/[^/]+\//,'').replace(/\.git$/,'').replace(/[^a-z0-9]+/gi,'-').toLowerCase()`). POST it (reuse the old submit's fetch/`showToast`/error handling), then `loadAutoImport()`. Guard the button with `setButtonLoading`.
- **Retarget** `editAutoImportJob(id)`: instead of the removed `jobId`/`jobProvider`/…
  fields, populate the shared form's fields (`f.provider.value = job.provider`, etc.,
  `f.jobId.value = job.id`, `f.jobEnabled.checked = job.enabled !== false`), scroll
  the form into view, and (optional) flip `#saveJobBtn` text to "Update job". Keep
  `f.jobId` editable-or-disabled as you prefer, but an upsert by the same id updates.
- **Remove** the old `autoImportJobForm` submit listener, `resetAutoImportJobForm`,
  and `toggleAutoImportJobFields` (the shared form shows the gitlab-hosts and
  name/developer fields unconditionally, as `importRepoForm` already does — no
  per-provider toggling needed). If `renderAutoImportJobs`/`editAutoImportJob`
  reference `toggleAutoImportJobFields`, drop those calls.
- **Keep** `loadAutoImport`, `renderAutoImportJobs`, `saveAutoImportConfig`,
  `runAutoImport`, `deleteAutoImportJob`, `toggleAutoImportJobEnabled` — but ensure
  `loadAutoImport()` is called when the merged tab opens: add a call in the
  `switchTab` path for `'import-repo'` (or wherever the health tab triggers its
  scan — mirror that), and/or on initial load.
- `escapeHtml` all job fields in `renderAutoImportJobs` (should already).

### Step 4: Update the render-smoke test
`grep -n "auto-import" tests/test_routes.py` → update the assertion that the
Auto-Import tab renders so it checks the merged surface instead (e.g. `GET /` now
contains `id="autoImportJobsList"` and no longer a separate `switchTab('auto-import'`
nav). Keep the test count the same (edit the existing test; don't add one).

**Verify**: `grep -c "switchTab('auto-import'" templates/index.html` → `0`;
`grep -c "<style>\|</style>"` → `2`.

### Step 5: Full suite unchanged → `222 passed`.

## Done criteria
- [ ] One "Import from Repo" tab; the separate "Auto-Import" nav button + panel are gone.
- [ ] The shared form has **Import now** (streaming one-off via `/api/import-release`) and **Save & auto-import** (`/api/auto-import/job`); the jobs list + schedule controls render below it and still work (config save, run-all, per-job run/edit/delete/enable).
- [ ] Editing a job repopulates the shared form; Job ID derives from the repo when blank.
- [ ] No `app.py`/server change; user input still `escapeHtml`-ed.
- [ ] `grep -c "switchTab('auto-import'"` → `0`; `grep -c "<style>\|</style>"` → `2`; suite `222 passed`.
- [ ] `git status --short` shows only `templates/index.html` and `tests/test_routes.py`.

## STOP conditions
- You discover the shared form can't satisfy BOTH endpoints' bodies without a server
  change — STOP (it can: the fields are a superset; `iconURL` is ignored by the job
  API, `jobId`/`jobEnabled` are ignored by import-release).
- Removing `toggleAutoImportJobFields` breaks a still-referenced handler you can't
  cleanly drop — report rather than leaving dead onclick refs.

## Steps to verify manually (do this, report what you saw)
Run the app (`DATA_DIR=/tmp/feather-data ADMIN_PASSWORD=x python app.py`), log in,
and confirm on the single tab: (a) **Save & auto-import** adds a job that appears in
the list; (b) **Edit** repopulates the shared form; (c) **Run now** / **Run All Now**
/ enable-toggle / delete work; (d) the **Import now** button still fires
`/api/import-release` (the request starts — it may error without a real repo, which
is fine). If you can't run a browser, exercise the endpoints via the test client and
say so.

## Maintenance notes
- Two intents share one form: "Import now" (transient) vs "Save & auto-import"
  (persistent). `iconURL` applies to Import-now (jobs get their icon from the IPA via
  plan 046); `jobId`/`jobEnabled` apply to Save-job. Keep that mapping documented in
  a comment near `saveRepoAsJob`.
- Reviewer: confirm BOTH flows work, no server code changed, the old tab id is fully
  gone (no dead `switchTab('auto-import')` or orphaned element ids), and the smoke
  test reflects the merged tab.
