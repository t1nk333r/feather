# Plan 045: Harden `_reconcile_icons` — a filesystem error in the icon walk must not 500

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git show b985103:plans/045-harden-reconcile-icons-fs-walk.md >/dev/null 2>&1 || true
> git diff --stat 1fd622e..HEAD -- app.py tests/test_storage.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 202 passed
> ```
> If `app.py`'s `_reconcile_icons` changed since `1fd622e`, compare the "Current
> state" excerpt below against the live code first; on a semantic mismatch, STOP.

## Status

- **Priority**: P2 (fixes a live production 500 on a shipped feature)
- **Effort**: S
- **Risk**: LOW (defensive wrapping of a read-only scan; no behavior change on the happy path)
- **Depends on**: plan 040 (the `_reconcile_icons` it hardens — merged to `main`)
- **Category**: bugfix / robustness
- **Planned at**: commit `1fd622e`, 2026-08-18

## Why this matters

`POST /api/reconcile-icons` (plan 040) is returning **HTTP 500** in production
(`{"error":"reconcile failed"}`), even on a dry run. Every S3/upload operation in
`_reconcile_icons` is already `try/except`-wrapped and a dry run never uploads, so
those can't be the cause. The **only unguarded raise site** is the on-disk
directory walk itself:

```python
if os.path.isdir(ICON_FOLDER):
    for bundle in sorted(os.listdir(ICON_FOLDER)):          # <-- not wrapped
        ...
        for filename in sorted(os.listdir(bundle_dir)):     # <-- not wrapped
```

`os.listdir` raises `OSError` for filesystem-level problems (a volume-mount
permission issue, a broken symlink, an unreadable subdirectory, an `ENOTDIR`
race) — and on the Garage deployment `data/icons/` is exactly the kind of
leftover/oddly-mounted directory where that happens. That `OSError` propagates to
the route's `except Exception` and becomes a 500. A **read-only diagnostic/repair
action must never 500 because it couldn't read a directory** — it should report
the problem and carry on. This plan wraps the walk so filesystem errors degrade to
a reported failure, and adds a human-readable note when there's nothing on local
disk to reconcile (which is itself a common, confusing case here).

## Current state — `app.py`

`_reconcile_icons(apply=False)` (app.py ~line 2092) and its route (~2177):

```python
def _reconcile_icons(apply=False):
    if STORAGE_BACKEND != "garage":
        return {"backend": "local", "uploaded": 0, "note": "..."}
    checked = would_upload = uploaded = skipped_existing = failed = 0
    items = []
    if os.path.isdir(ICON_FOLDER):
        for bundle in sorted(os.listdir(ICON_FOLDER)):
            safe_bundle = secure_filename(bundle)
            if not safe_bundle or safe_bundle != bundle:
                continue
            bundle_dir = os.path.join(ICON_FOLDER, bundle)
            if not os.path.isdir(bundle_dir):
                continue
            for filename in sorted(os.listdir(bundle_dir)):
                path = os.path.join(bundle_dir, filename)
                if not os.path.isfile(path) or not filename.startswith("icon."):
                    continue
                ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
                safe_ext = secure_filename(ext)
                if filename != f"icon.{ext}" or not safe_ext or safe_ext != ext or ext not in ALLOWED_ICON_EXTENSIONS:
                    continue
                checked += 1
                try:
                    already_exists = icon_storage.exists(bundle, ext)
                except Exception as e:
                    ... failed += 1; items.append({... "status": "failed"}); continue
                if already_exists: skipped_existing += 1; ...; continue
                if not apply: would_upload += 1; ...; continue
                try:
                    ok = icon_storage.put(path, bundle, ext)
                except Exception: ok = False
                ... uploaded/failed accounting ...
    return {"backend": "garage", "checked": checked, "would_upload": would_upload,
            "uploaded": uploaded, "skipped_existing": skipped_existing,
            "failed": failed, "items": items}

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

The two `os.listdir(...)` calls (and, defensively, the `os.path.isdir`/`isfile`
checks) are the unguarded lines.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `202 passed` before changes |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| Focused | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_storage.py -q -k reconcile` | new + existing reconcile tests pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `205 passed` after |

## Scope

**In scope:**
- `app.py` — `_reconcile_icons` only: wrap the filesystem walk so `OSError` never
  escapes; add a `note` field for the empty/unreadable cases.
- `tests/test_storage.py` — new tests (model on the existing reconcile tests).

**Out of scope:**
- The storage classes (`GarageIconStorage`/`LocalIconStorage`) — unchanged.
- The `reconcile_icons` route — leave its outer `try/except` as a last-resort
  safety net; do not change its behavior. (After this fix, `_reconcile_icons`
  simply won't raise for filesystem reasons, so that net stops firing for this
  cause.)
- The UI (`templates/index.html`) — the summary shape stays backward-compatible
  (only additive fields), so the existing `renderReconcileSummary` keeps working.
- The migration script `scripts/migrate_icons_to_garage.py`.

## Steps

### Step 1: Confirm baseline → `202 passed`. If not, STOP.

### Step 2: Wrap the top-level icon-folder read
Guard `os.listdir(ICON_FOLDER)` so a failure returns the normal summary shape with
`checked: 0` and a human `note`, instead of raising. Sketch:
```python
if os.path.isdir(ICON_FOLDER):
    try:
        bundles = sorted(os.listdir(ICON_FOLDER))
    except OSError as e:
        logging.error(f"Could not read icon folder for reconcile: {e}")
        return {"backend": "garage", "checked": 0, "would_upload": 0,
                "uploaded": 0, "skipped_existing": 0, "failed": 0, "items": [],
                "note": "Could not read the local icon folder; nothing was reconciled."}
    for bundle in bundles:
        ...
```
(Do not put the raw path or full exception into the response `note` — keep it
generic; the detail goes only to the server log.)

### Step 3: Wrap each per-bundle read so one bad directory can't kill the scan
Guard the inner `os.listdir(bundle_dir)`:
```python
try:
    filenames = sorted(os.listdir(bundle_dir))
except OSError as e:
    logging.error(f"Could not read icon dir for {bundle}: {e}")
    failed += 1
    items.append({"bundleIdentifier": bundle, "ext": None, "status": "scan_failed"})
    continue
for filename in filenames:
    ...
```
Defensively, also treat an `OSError` from the `os.path.isfile(path)` check the same
way (skip that file) rather than letting it escape — a `try/except OSError` around
the per-file stat, or rely on `os.path.isfile` which itself swallows `OSError` and
returns False (it does in CPython — so `isfile`/`isdir` are already safe; you only
need to guard the two `listdir` calls). Prefer the minimal change: guard just the
two `listdir` calls.

### Step 4: Add an "empty" note (addresses the real operator confusion)
After the walk, when nothing was found on local disk, add a note so the operator
understands reconcile has nothing to upload (their icons aren't on this container's
disk):
```python
result = {"backend": "garage", "checked": checked, "would_upload": would_upload,
          "uploaded": uploaded, "skipped_existing": skipped_existing,
          "failed": failed, "items": items}
if checked == 0 and "note" not in result:
    result["note"] = ("No local icon files found to reconcile. Icons missing from "
                      "Garage that have no local copy must be re-uploaded via Edit App.")
return result
```

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 5: Tests (add to `tests/test_storage.py`, model on the existing reconcile tests: `garage_client` fixture, `_write_local_icon(app_module, bundle, ext)`, login then POST)

Use a **narrow** `os.listdir` patch that raises only for the target path and
delegates otherwise (patching `os.listdir` globally would break Flask internals
during the request):
```python
from unittest import mock
import os as _os

def test_reconcile_icon_folder_unreadable_does_not_500(garage_client):
    app_module = garage_client.app_module
    real_listdir = _os.listdir
    def fake_listdir(p):
        if str(p) == app_module.ICON_FOLDER:
            raise OSError("simulated unreadable icon folder")
        return real_listdir(p)
    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    with mock.patch.object(app_module.os, "listdir", side_effect=fake_listdir):
        resp = garage_client.post("/api/reconcile-icons", json={"apply": False})
    assert resp.status_code == 200          # <-- the bug: was 500
    body = json.loads(resp.data)
    assert body["backend"] == "garage"
    assert body["checked"] == 0
    assert "note" in body

def test_reconcile_one_unreadable_bundle_is_isolated(garage_client):
    app_module = garage_client.app_module
    _write_local_icon(app_module, "com.good.app", "png")
    _write_local_icon(app_module, "com.bad.app", "png")
    bad_dir = os.path.join(app_module.ICON_FOLDER, "com.bad.app")
    real_listdir = _os.listdir
    def fake_listdir(p):
        if str(p) == bad_dir:
            raise OSError("simulated")
        return real_listdir(p)
    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    with mock.patch.object(app_module.os, "listdir", side_effect=fake_listdir):
        resp = garage_client.post("/api/reconcile-icons", json={"apply": False})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    # the good bundle is still processed; the bad one is reported, not fatal
    assert body["would_upload"] >= 1
    assert any(i.get("status") == "scan_failed" and i["bundleIdentifier"] == "com.bad.app"
               for i in body["items"])

def test_reconcile_empty_folder_returns_note(garage_client):
    # garage backend, no local icons written at all
    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    resp = garage_client.post("/api/reconcile-icons", json={"apply": False})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["checked"] == 0
    assert "note" in body
```

**Prove discrimination**: temporarily remove the Step 2 `try/except` (let
`os.listdir(ICON_FOLDER)` raise) → `test_reconcile_icon_folder_unreadable_does_not_500`
fails with a 500. Restore.

**Verify**:
```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_storage.py -q -k reconcile
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # 205 passed
```

## Done criteria
- [ ] A filesystem `OSError` from reading `ICON_FOLDER` or a per-bundle dir no longer produces a 500 — `_reconcile_icons` catches it, logs the detail server-side, and returns a 200 summary with a generic `note` (and, for a per-bundle failure, a `scan_failed` item) — the scan continues past a bad bundle.
- [ ] A garage backend with no local icons returns 200 with an explanatory `note` (not a bare zero summary).
- [ ] The summary shape stays backward-compatible: existing keys unchanged; only `note` (str) and `scan_failed`-status items added. The 5 existing reconcile tests still pass.
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite `205 passed`.
- [ ] `git status --short` shows only `app.py` and `tests/test_storage.py`.

## STOP conditions
- The live production exception (from `docker logs … | grep "reconcile error"`) turns out to be something **other** than an `OSError` in the walk (e.g. `icon_storage` is `None`, or an `AttributeError`) — STOP and report; the fix would then target a different line, and this plan's premise would be wrong.
- The response `note`/`scan_failed` additions break `renderReconcileSummary` in the template (it should ignore unknown fields; if it hard-requires a fixed shape, that's a template bug to note, not to fix here) — report rather than editing the UI.

## Maintenance notes
- This only makes the scan *survivable*; it does not create missing icons. Icons
  that are absent from both Garage and local disk still require Edit App (or the
  importer's icon field) — the new `note` tells the operator exactly that.
- If a future change moves icon storage enumeration to the backend (listing Garage
  objects instead of walking local disk), this local-walk hardening becomes moot —
  but until then, any code that walks a user-controlled/mounted directory should
  wrap `os.listdir` the same way.
- Reviewer: confirm the happy-path summary is byte-for-byte the same as before for
  the existing tests (additive-only), and that the discrimination proof genuinely
  reproduces the 500 when the guard is removed.
