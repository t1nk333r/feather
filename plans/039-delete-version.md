# Plan 039: Add delete-version — finish the version CRUD

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
> If `app.py` changed since `50fb1c9`, compare the "Current state" excerpts below
> against the live code before proceeding; on a mismatch, STOP.

## Status

- **Priority**: P2
- **Effort**: S–M
- **Risk**: MED (a new *destructive* route that deletes an IPA and a catalog entry)
- **Depends on**: none
- **Category**: feature (CRUD completeness)
- **Planned at**: commit `50fb1c9`, 2026-08-18

## Why this matters

The catalog supports `add-version` and `update-version` but has **no
delete-version**. When a bad or duplicate version lands (this happened in
production — three identical `12.17` entries on one app), the only remedies are
hand-editing `data/source.json` or deleting the whole app and losing its other
versions. This plan adds a delete-version capability — remove one version entry
*and* its stored IPA — completing the version CRUD and giving the operator a
clean way to prune.

## Current state — `app.py`

- The exemplar for "delete + storage cleanup + save under the lock" is
  `SourceManager.delete_app` (app.py:1038):
  ```python
  def delete_app(self, bundle_identifier):
      with self._lock:
          source_data = self.load_source()
          if not source_data:
              return False, "Failed to load source data"
          app_to_delete = None
          for app in source_data['apps']:
              if app['bundleIdentifier'] == bundle_identifier:
                  app_to_delete = app
                  break
          initial_count = len(source_data['apps'])
          source_data['apps'] = [a for a in source_data['apps'] if a['bundleIdentifier'] != bundle_identifier]
          if len(source_data['apps']) < initial_count:
              if app_to_delete:
                  for version in app_to_delete.get('versions', []):
                      self.delete_ipa_file(bundle_identifier, version.get('version', ''))
              success = self.save_source(source_data)
              if success:
                  self.delete_icon_file(bundle_identifier)
              return success, "App deleted successfully" if success else "Failed to save source after deletion"
          else:
              return False, "App not found"
  ```
- `SourceManager.delete_ipa_file(bundle_id, version)` (app.py:734) removes the
  IPA through the configured storage backend. Reuse it.
- The route wiring exemplar is `update_version()` (route at app.py:1783) and
  `delete_app()` (route at app.py:1515) — both `@requires_auth`, read JSON, call
  the manager, return `{"success": ...}`.
- **`save_source` writes atomically and backs up** the previous catalog to
  `data/backups/` (plan 006), so a delete is recoverable from a backup — but it
  removes the IPA first, which is NOT recoverable for the local backend. That is
  why this is a destructive action and the UI must confirm.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `178 passed` before changes |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| New tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k delete_version` | new tests pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `178 + N passed` |

## Scope

**In scope:**
- `app.py` — a `SourceManager.delete_version(self, bundle_identifier, version)` method (modeled on `delete_app`), and a `POST /api/delete-version` route (`@requires_auth`, modeled on `delete_app`'s route).
- `templates/index.html` — a delete control per version in the existing per-app version list, with a JS confirm() before calling the route.
- `tests/test_routes.py` — tests.

**Out of scope:**
- `add_version` / `update_version` / `delete_app` — unchanged.
- Deleting the *last* version of an app: for MVP, **refuse** it with a clear message ("delete the app instead") rather than leaving an app with zero versions (which would be a malformed entry). Do not auto-delete the app.
- Storage backend internals — reuse `delete_ipa_file`.

## Steps

### Step 1: Confirm baseline
`ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 178 passed. If not, STOP.

### Step 2: `SourceManager.delete_version(bundle_identifier, version)`

Add it next to `delete_app`. Under `self._lock`: load; find the app (else
`(False, "App not found")`); find the version index (else `(False, "Version not
found")`); if the app has only that one version, return `(False, "Cannot delete
the only version; delete the app instead")`; otherwise remove the version entry,
`save_source`, and **only if the save succeeded** call
`self.delete_ipa_file(bundle_identifier, version)` (delete the catalog entry
first, then the binary — the inverse of `delete_app` is fine here because a
failed save must not orphan the catalog from its IPA; deleting the IPA after a
confirmed save mirrors the "commit, then clean up" ordering used for the icon in
`delete_app`). Return `(True, "Version deleted successfully")` /
`(False, "Failed to save source after deletion")`.

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 3: `POST /api/delete-version` route

Model on the `delete_app` route (app.py:1515): `@requires_auth`, read
`bundleIdentifier` + `version` from JSON, 400 if either is missing, call
`source_manager.delete_version(...)`, return `{"success": bool, "message"/"error"}`.
Optionally `notify("delete_version", ...)` for parity with other mutations (only
if the `notify` events set makes sense — keep it consistent with existing
routes; if unsure, omit the notify).

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 4: UI delete control

In `templates/index.html`, in the per-version rendering inside the app's version
list (search where versions are listed with their update controls), add a
"Delete" control that calls `confirm("Delete version X of <app>? This removes the
IPA and cannot be undone.")` then POSTs `{bundleIdentifier, version}` to
`/api/delete-version` and refreshes the list on success (reuse the existing
list-refresh + `showToast` used by the update/delete-app controls). Plain text,
no emoji (plan 025).

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 5: Tests

Add to `tests/test_routes.py` (use `authed_client`, seed an app with 2+ versions):
1. `test_delete_version_removes_one_version` — app with versions `["2.0","1.0"]`; `POST /api/delete-version {bundle, "1.0"}` → success; the app now has only `2.0`.
2. `test_delete_version_refuses_last_version` — app with a single version; delete it → `(False, ...)` "only version"; the version is still present.
3. `test_delete_version_unknown_version_404_message` — delete a non-existent version → `success False`, message "Version not found".
4. `test_delete_version_requires_auth` — unauthenticated POST → 401.
5. `test_delete_version_deletes_the_ipa` — with a local IPA present for the version, after delete the IPA no longer exists (use `ipa_storage.exists` or the on-disk path); model the IPA-presence setup on an existing add-version/storage test.

**Prove discrimination**: temporarily make `delete_version` a no-op returning
success → test 1 fails (version still there). Restore.

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k delete_version`; then full suite.

## Done criteria

- [ ] `POST /api/delete-version` (`@requires_auth`) removes one version entry and its IPA, atomically (catalog write is the `save_source` atomic+backup path).
- [ ] Deleting the only version is refused with a clear message (no zero-version app).
- [ ] Unknown app/version returns a clear failure; auth required.
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (178 + 5).
- [ ] `git status --short` shows only `app.py`, `templates/index.html`, `tests/test_routes.py`.

## STOP conditions
- `delete_app` / `delete_ipa_file` no longer match the "Current state" excerpts (the manager was refactored) — reconcile first.
- Removing a version would leave an app with zero versions and the plan's refusal path can't be applied cleanly — STOP.
- The UI confirm/refresh pattern can't be reused from the existing delete-app control — report rather than hand-rolling a divergent one.

## Maintenance notes
- The IPA delete is irreversible for the local backend (Garage delete too); the
  catalog side is recoverable from `data/backups/`. Keep the confirm dialog.
- If a future "prune to last N versions" bulk action is added, it should call
  this same `delete_version` per pruned entry, not re-implement deletion.
- Reviewer: confirm the IPA is deleted only *after* a successful `save_source`,
  the only-version refusal holds, and the route is auth-gated.
