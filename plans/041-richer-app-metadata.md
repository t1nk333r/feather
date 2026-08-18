# Plan 041: Let the UI edit richer app metadata (subtitle, screenshots, tint color)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 50fb1c9..HEAD -- app.py templates/index.html tests/test_routes.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 178 passed
> ```
> If `app.py` or the template changed since `50fb1c9`, compare the "Current
> state" excerpts below against the live code first; on a mismatch, STOP.

## Status

- **Priority**: P3
- **Effort**: M
- **Risk**: LOW (extends an existing update path with more optional fields)
- **Depends on**: none
- **Category**: direction / feature (polish)
- **Planned at**: commit `50fb1c9`, 2026-08-18

## Why this matters

In the admin UI most apps show "No description available" and no subtitle or
screenshots, because the Edit form only lets you set name, developer, and
description. The AltStore/Feather source schema supports per-app `subtitle`,
`screenshotURLs`, and `tintColor`, which make an app render as first-class in the
client. This plan adds those three fields to the Edit App flow so the catalog can
actually carry them. Purely additive — no field becomes required, existing apps
are unaffected.

## Current state — `app.py`

- `SourceManager.update_app` (app.py:1083) applies a fixed list of scalar fields:
  ```python
  # app.py:1101
  updatable_fields = ['name', 'developerName', 'localizedDescription']
  for field in updatable_fields:
      if field in data and data[field] is not None:
          app[field] = data[field]
  ```
- The `/api/update-app` route (multipart branch) builds `data` from
  `request.form.get(...)` — e.g. `'localizedDescription': request.form.get('localizedDescription', '')`
  (app.py:1479). New fields must be read there too.
- The Edit form is `editAppForm` (templates/index.html:605) with inputs
  `editName` (612), `editDeveloperName` (616), etc.; `editApp(bundleId)`
  (template:1234) populates them from the fetched app (e.g. `editName.value =
  app.name || ''`, template:1247). New fields need an input + a populate line +
  inclusion in the submitted form data.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `178 passed` before changes |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| New tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k metadata` | new tests pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `178 + N passed` |

## Scope

**In scope:**
- `app.py` — `update_app` (add `subtitle`, `tintColor` to the scalar
  `updatable_fields`; handle `screenshotURLs` as a list separately) and the
  `/api/update-app` multipart branch (read the three new form fields).
- `templates/index.html` — three inputs in `editAppForm` + populate lines in
  `editApp()` + include them in the submitted data.
- `tests/test_routes.py` — tests.

**Out of scope:**
- `add_app_manual` / add-app form (creation stays minimal; these are edit-time).
- `screenshotURLs` per-device object form (the newer AltStore variant) — MVP is a
  flat array of URL strings.
- Any required-field change; validation beyond "tintColor looks like a hex color;
  screenshotURLs entries look like URLs" (best-effort, non-blocking).

## Steps

### Step 1: Confirm baseline → `178 passed`. If not, STOP.

### Step 2: `update_app` — accept the new fields
Add `subtitle` and `tintColor` to `updatable_fields` (they are scalars, so the
existing loop handles them). Handle `screenshotURLs` separately, right after the
loop: if `screenshotURLs` is present in `data`, set `app['screenshotURLs']` to a
list of non-empty trimmed strings (accept either a JSON list or a
newline/comma-separated string from the form; drop empties). An empty submission
should clear the field to `[]` only if the key was explicitly provided — do not
wipe it when the key is absent (mirror the "only overwrite when present"
convention already used for the scalars).

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 3: `/api/update-app` — read the new form fields
In the multipart branch that builds `data` (near app.py:1479), add:
`'subtitle': request.form.get('subtitle')`, `'tintColor': request.form.get('tintColor')`,
and `'screenshotURLs': request.form.get('screenshotURLs')` (a
newline/comma-separated string the method parses). Use `.get(...)` with no
default so an absent field stays `None` and is skipped by update_app's
"only when present" logic — matching how the scalars behave. (For the JSON
branch, add the same keys.)

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 4: Edit form UI
In `editAppForm`, add: a text input `editSubtitle` (name `subtitle`), a
`<textarea>` `editScreenshotURLs` (name `screenshotURLs`, one URL per line), and
a `<input type="color">`-or-text `editTintColor` (name `tintColor`). In
`editApp()`, populate them from the fetched app
(`app.subtitle || ''`, `(app.screenshotURLs || []).join('\n')`,
`app.tintColor || ''`). Ensure they're included in the submitted form data the
same way the existing fields are. Plain text labels, no emoji (plan 025).

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 5: Tests
Add to `tests/test_routes.py` (use `authed_client`, seed an app):
1. `test_update_app_sets_subtitle_and_tint` — POST `/api/update-app` with `subtitle` + `tintColor` → the app in `/api/app/<id>` has both.
2. `test_update_app_sets_screenshot_urls_from_lines` — submit `screenshotURLs` as two newline-separated URLs → the app has `screenshotURLs == [url1, url2]`.
3. `test_update_app_absent_metadata_fields_preserved` — update only `name`, without the new keys → existing `subtitle`/`screenshotURLs` are unchanged (not wiped).
4. `test_update_app_metadata_requires_auth` — unauthenticated → 401.

**Prove discrimination**: temporarily drop `subtitle` from `updatable_fields` →
test 1 fails. Restore.

**Verify**: focused `-k metadata`; then full suite.

## Done criteria
- [ ] `/api/update-app` can set `subtitle`, `tintColor`, and `screenshotURLs` (list); all optional.
- [ ] Absent fields are preserved (not wiped), matching the existing "only when present" convention.
- [ ] The Edit form shows and round-trips the three fields.
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (178 + 4).
- [ ] `git status --short` shows only `app.py`, `templates/index.html`, `tests/test_routes.py`.

## STOP conditions
- `update_app`'s `updatable_fields` pattern no longer matches the excerpt — reconcile first.
- Handling `screenshotURLs` cleanly would require a schema decision (per-device object) — MVP is a flat string array; if the client rejects that shape, STOP and report rather than guessing the object form.

## Maintenance notes
- `normalize_source` (plan 037) coerces empty `iconURL`; these new fields are
  optional and don't need normalization, but if the client ever *requires*
  `subtitle`, add a default there.
- Reviewer: confirm absent fields are preserved (the wipe-only-when-present rule),
  and `screenshotURLs` is stored as a list of strings, not a raw blob.
