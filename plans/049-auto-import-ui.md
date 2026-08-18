# Plan 049: Auto-Import admin UI — manage repos, schedule, and Run-Now from the app

> **Executor instructions**: Follow this plan step by step. Run the verification
> gates. If a STOP condition occurs, stop and report — do not improvise. Update
> this plan's status row in `plans/README.md` when done unless a reviewer
> maintains the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat <PLAN-048-MERGE>..HEAD -- app.py templates/index.html
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
> ```
> This plan **requires plan 048 to be merged first** (it consumes its API). If
> `GET /api/auto-import` and `POST /api/auto-import/{config,job,job/delete,run}`
> don't exist yet, STOP — execute 048 first.

## Status

- **Priority**: P2 (the user-facing half of the "cron in the UI" request)
- **Effort**: M–L
- **Risk**: LOW–MED (UI on an existing API; no new server behavior)
- **Depends on**: **plan 048 (hard dependency — its API)**, plan 043 (iOS token system)
- **Category**: feature / UI
- **Planned at**: commit `c052cb0`, 2026-08-18 (re-check drift against the 048 merge)

## Why this matters

Plan 048 gives the app a job store, scheduler, and Run-Now API so auto-import no
longer needs `.env`/manifest/crontab. This plan is the admin page that drives it:
an "Auto-Import" tab where the operator adds repos, toggles the schedule, sets the
interval, and runs imports on demand — the whole point of "it should be in the
apps page, not `.env`".

## Current state — `templates/index.html`

- Tabs are `<button class="tab" onclick="switchTab('<id>', this)">Label</button>`
  in the `.tabs` bar (~line 585), with matching `<div id="<id>" class="tab-content">`
  panels. Existing tabs: `manage-apps`, `qr-code`, `source-info`, `import-repo`,
  `health`, plus a `Sign out` button. Follow this exact pattern for a new
  `auto-import` tab.
- The **Import-from-Repo** tab (`#import-repo`, ~line 829) is the closest model:
  a provider `<select>`, project/bundle inputs, a submit, and a **streaming
  progress area** (reads the NDJSON `application/x-ndjson` response and updates a
  progress bar). Reuse its fetch-and-stream helper for Run-Now if 048's `/run`
  streams; if 048's `/run` returns a plain JSON summary, just show it via
  `showToast` + a results list.
- Styling: the iOS token system from plan 043 (`--surface`, `--label`,
  `--accent`, `--danger`, `--radius-*`, etc.). Use tokens, **no raw hex**, **no
  emoji** (plan 025). Buttons use `.btn` / `.btn-danger` / `.btn-secondary`.
- `showToast(...)`, `setButtonLoading(...)`, and the list-refresh patterns already
  exist — reuse them (grep for `function showToast`).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | passes (048's count) before AND after |
| Style block intact | `grep -c "<style>\|</style>" templates/index.html` | `2` |
| Tab wired | `grep -n "switchTab('auto-import'" templates/index.html` | present (nav + panel) |
| No raw hex added | `grep -nE "#[0-9a-fA-F]{3,6}" templates/index.html | grep -v ":root\|#fff\|#8e8e93\|#007" | wc -l` | unchanged vs baseline |

## Scope

**In scope:** `templates/index.html` — a new `auto-import` tab (nav button +
panel), its form/list markup, and the JS that calls plan 048's endpoints.
Optionally a tiny render-smoke assertion in `tests/test_routes.py` (that `GET /`
contains the new tab) — behavior is covered by 048's API tests.

**Out of scope:** `app.py` / any server code (048 owns the API — if you find
yourself needing a new endpoint, STOP: it belongs in 048). No new deps, no CDN, no
emoji, no custom dropdown widget (reuse the native `<select>`, skinned by plan 047
if merged).

## Steps

### Step 1: Confirm 048's API exists (`grep -n "api/auto-import" app.py`). If not, STOP.

### Step 2: Add the tab
- Nav button in `.tabs` (before `Sign out`): `<button class="tab" onclick="switchTab('auto-import', this)">Auto-Import</button>`.
- Panel `<div id="auto-import" class="tab-content"> … </div>` with three sections:
  1. **Schedule** — an enable checkbox/toggle, an interval `<input type="number" min="1" max="168">` (hours), a Save button → `POST /api/auto-import/config`. Show current values from `GET /api/auto-import`.
  2. **Jobs** — a list rendered from `GET /api/auto-import`, one card per job: project, bundleIdentifier, provider, per-job enabled toggle, `lastResult` (status + version/message), and per-job **Run now** + **Edit** + **Delete** controls. Delete uses `confirm(...)` then `POST /api/auto-import/job/delete`.
  3. **Add / edit job** — a form: provider `<select>` (GitHub/GitLab), project (`owner/repo`), bundleIdentifier, assetGlob (default `*.ipa`), includePrereleases + createIfMissing checkboxes, name + developerName (shown/required when createIfMissing is checked — mirror the manifest rule), and allowedDownloadHosts (shown/required for GitLab). Submit → `POST /api/auto-import/job`. Show validation errors returned by the API (400 `error` message) via `showToast`.
- A **Run all now** button → `POST /api/auto-import/run` (no id).

### Step 3: Wire the JS
- `loadAutoImport()` — `GET /api/auto-import`, render the schedule + job list; call it on tab open (extend `switchTab` or add an onclick like the Health tab's scan).
- `saveAutoImportConfig()`, `saveAutoImportJob()`, `deleteAutoImportJob(id)`, `runAutoImport(id?)` — POST to the matching endpoints, `showToast` the result, re-`loadAutoImport()` on success. For Run-Now: if 048's `/run` streams NDJSON, reuse the import-repo streaming reader to show progress; otherwise render the returned JSON summary.
- Escape all interpolated strings with the existing `escapeHtml(...)` helper (grep for it) — job fields are user input.

### Step 4: Verify
```bash
grep -c "<style>\|</style>" templates/index.html          # 2
grep -n "switchTab('auto-import'" templates/index.html    # nav + (if used) panel refs
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # unchanged count (+1 if you add the render-smoke test)
```
Then load `/` and exercise the tab against a running app (add a job, toggle the
schedule, Run-Now) — describe what you saw in your report; a human reviewer will
also eyeball it in light + dark.

## Done criteria
- [ ] An "Auto-Import" tab lists jobs, adds/edits/deletes them, toggles the schedule + interval, and runs one/all jobs — all via plan 048's endpoints, no page reload beyond `loadAutoImport()`.
- [ ] Validation errors from the API surface to the user (not a silent failure); all user input is `escapeHtml`-ed.
- [ ] iOS-styled (043 tokens), no raw hex, no emoji; `grep -c "<style>\|</style>"` → `2`.
- [ ] Full suite unchanged (or +1 for a render-smoke test); `git status --short` shows only `templates/index.html` (and maybe `tests/test_routes.py`).

## STOP conditions
- You need a server endpoint 048 didn't provide — STOP; it belongs in 048, not here.
- The Run-Now response shape (stream vs JSON) is ambiguous — check 048's `/run`
  implementation and match it; don't assume.

## Maintenance notes
- Keep this a thin client over 048's API — all logic/validation lives server-side
  so a future CLI or bot can reuse it.
- Reviewer: confirm no server code changed, user input is escaped, the schedule
  toggle round-trips, and the tab reads correctly in both themes.
