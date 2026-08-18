# Plan 040: In-app "reconcile icons" action — upload on-disk icons to the active backend

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 50fb1c9..HEAD -- app.py templates/index.html scripts/migrate_icons_to_garage.py tests/
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 178 passed
> ```
> If `app.py` or `scripts/migrate_icons_to_garage.py` changed since `50fb1c9`,
> compare the "Current state" notes below against the live code first; on a
> semantic mismatch, STOP.

## Status

- **Priority**: P3
- **Effort**: M
- **Risk**: LOW–MED (a new authenticated action that writes to icon storage; dry-run by default)
- **Depends on**: none (complements plan 038's missing-icon detection)
- **Category**: direction / DX
- **Planned at**: commit `50fb1c9`, 2026-08-18

## Why this matters

When this deployment switched `STORAGE_BACKEND=garage`, the app icons that had
been stored on local disk were never uploaded to Garage, so `/icons/...` began
404ing and apps showed placeholders. The repair tool —
`scripts/migrate_icons_to_garage.py` — is a standalone script that is **not in
the Docker image**, so running it means a repo checkout + venv + the live env
(the operator hit `command not found`). This plan folds that repair into an
**authenticated admin action** the running app performs itself — it already has
the `data/icons/` mount and the Garage credentials — so no standalone script is
needed. Dry-run by default; `--apply` uploads.

## Current state

- `scripts/migrate_icons_to_garage.py` (the logic to mirror, do NOT run it here)
  reads `app_module.ICON_FOLDER` for `(bundle, ext, path)` icon files and uploads
  each to `garage_icon_key(bundle, ext)`. Its default is a read-only dry run;
  `--apply` uploads; it never removes local icons.
- In `app.py`: `ICON_FOLDER` is the module constant; `icon_storage` is the active
  backend (`LocalIconStorage` or `GarageIconStorage`), both exposing
  `put(src, bundle_id, ext)` (accepts a local path) and `exists(bundle_id, ext)`;
  `ALLOWED_ICON_EXTENSIONS` is the valid-extension set; `secure_filename` is used
  throughout for path safety.
- For the **local** backend, on-disk icons ARE what's served — reconciliation is
  a no-op. The action is only meaningful for the **garage** backend (upload the
  local files that aren't yet in Garage).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `178 passed` before changes |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| New tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_storage.py -q -k reconcile` | new tests pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `178 + N passed` |

## Scope

**In scope:**
- `app.py` — a `POST /api/reconcile-icons` route (`@requires_auth`) that scans
  `ICON_FOLDER` for local icon files and, per file, uploads to `icon_storage` when
  `icon_storage.exists(bundle, ext)` is False. Body `{"apply": bool}` — default
  `false` = dry-run (report only, upload nothing). Return a JSON summary
  `{"backend", "checked", "would_upload"|"uploaded", "skipped_existing", "failed", "items":[...]}`.
- `templates/index.html` — a control (on the Health tab from plan 038 if present,
  else a small standalone control) that calls the route dry-run, shows the plan,
  and has an "Apply" button.
- `tests/test_storage.py` — tests using the existing `FakeS3Client` Garage fixture.

**Out of scope:**
- Deleting or modifying local icons (never — the standalone script's invariant).
- Extracting icons from IPAs (plan 023 rejected this).
- The local backend doing any upload (it's a no-op; report `"backend":"local","uploaded":0`).
- `scripts/migrate_icons_to_garage.py` — leave it; this is an in-app complement, not a replacement to delete.

## Steps

### Step 1: Confirm baseline → `178 passed`. If not, STOP.

### Step 2: Reconcile helper + route
Add a helper that walks `ICON_FOLDER` for `<bundle>/icon.<ext>` files (ext in
`ALLOWED_ICON_EXTENSIONS`, both segments `secure_filename`-safe), and for each:
if `icon_storage.exists(bundle, ext)` → count `skipped_existing`; else in
dry-run count `would_upload`; in apply mode call `icon_storage.put(path, bundle,
ext)` and count `uploaded` / `failed`. Never raise out of the route:
```python
@app.route('/api/reconcile-icons', methods=['POST'])
@requires_auth
def reconcile_icons():
    apply = bool((request.get_json(silent=True) or {}).get('apply'))
    try:
        return jsonify(_reconcile_icons(apply=apply))
    except Exception as e:
        logging.error(f"Icon reconcile error: {str(e)}")
        return jsonify({"error": "reconcile failed"}), 500
```
For the local backend, short-circuit: return `{"backend":"local","uploaded":0,"note":"local backend serves on-disk icons directly; nothing to reconcile"}`.

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 3: UI control
Add a "Reconcile icons" button that POSTs `{apply:false}` first (shows the
dry-run summary), then an "Apply" button that POSTs `{apply:true}` and shows the
result via `showToast`. Plain text, no emoji.

### Step 4: Tests (model on `tests/test_storage.py`'s Garage fixture / `FakeS3Client`)
1. `test_reconcile_dry_run_reports_missing_without_uploading` — Garage backend, a local icon file present, not in the fake bucket → dry-run reports `would_upload>=1`, the fake bucket is unchanged.
2. `test_reconcile_apply_uploads_missing_icons` — same, `apply:true` → the object now exists in the fake bucket, summary `uploaded>=1`.
3. `test_reconcile_skips_already_present` — icon already in the fake bucket → `skipped_existing>=1`, no re-upload.
4. `test_reconcile_local_backend_is_noop` — local backend → returns the `"backend":"local"` no-op summary, uploads nothing.
5. `test_reconcile_requires_auth` — unauthenticated → 401.

**Prove discrimination**: temporarily make apply-mode skip the `put` → test 2 fails. Restore.

**Verify**: focused `-k reconcile`; then full suite.

## Done criteria
- [ ] `POST /api/reconcile-icons` (`@requires_auth`) dry-runs by default; `{apply:true}` uploads only icons missing from the active backend; never deletes/modifies local files.
- [ ] Local backend is a reported no-op; Garage backend uploads missing icons.
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (178 + 5).
- [ ] `git status --short` shows only `app.py`, `templates/index.html`, `tests/test_storage.py`.

## STOP conditions
- `icon_storage.put` / `exists` no longer accept `(path/src, bundle, ext)` as in the excerpts — reconcile before editing.
- Reconciliation would need to delete or rewrite a local icon — it must not.
- The Garage test fixture (`FakeS3Client`) can't be reused for these tests — report rather than making a real network call.

## Maintenance notes
- This does not obsolete `scripts/migrate_icons_to_garage.py` (useful off-host);
  it's the in-app path for operators without a checkout.
- Pairs with plan 038: the Health view *detects* missing icons; this *repairs*
  the on-disk-but-not-in-Garage subset. Icons that exist nowhere still need
  Edit-App (or the import icon field from plan 035).
- Reviewer: confirm dry-run uploads nothing, local backend is a no-op, and local
  files are never modified.
