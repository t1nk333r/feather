# Plan 038: A read-only "Catalog Health" admin view that flags broken catalog entries

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 50fb1c9..HEAD -- app.py templates/index.html tests/
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 178 passed
> ```
> If `app.py` or the template changed since `50fb1c9`, compare the "Current
> state" excerpts below against the live code before proceeding; on a mismatch, STOP.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW (a new read-only route + a new UI tab; touches no write path)
- **Depends on**: none
- **Category**: direction / DX
- **Planned at**: commit `50fb1c9`, 2026-08-18

## Why this matters

Catalog problems in this project are found by hand, after a device fails: a
source that won't parse, icons that 404, duplicate versions, `"size": 0`
entries, `.ipa` files that are actually gzip-HTML, orphaned IPA directories, and
`downloadURL`s pointing at a LAN IP instead of the public host. Every one of
these has bitten this deployment and is recorded only as prose in
`plans/HANDOFF.md` / `plans/README.md` — never surfaced in the app. This plan
adds a **read-only** "Catalog Health" admin tab that scans the live catalog +
storage and lists what's wrong, so the operator can *see* problems instead of
discovering them downstream. It never repairs anything — diagnostics only.

**Scope discipline (MVP):** implement exactly the checks listed in Step 2. Do not
invent more. Each check reuses a primitive that already exists.

## Current state

- `SourceManager.load_source()` returns the parsed catalog dict; `get_app`,
  `get_ipa_path(bundle_id, version)` (read-only path builder), and the storage
  objects `ipa_storage` / `icon_storage` (with `.exists(...)`, and for local,
  `.public_url() is None`) are all available on the module. `get_file_size(path)`
  returns an int or `None`. `IPA_FOLDER` / `ICON_FOLDER` are module constants.
- `import zipfile` is NOT currently imported in `app.py` (the Telegram worker and
  the release importer import it themselves). This plan needs it for the
  is-a-real-ZIP check — add `import zipfile` to the top-of-file imports.
- Auth: the six mutating routes use the `requires_auth` decorator (see any
  `@app.route('/api/...', methods=['POST'])` + `@requires_auth`). The new route
  is GET but exposes internal paths, so it **must** be `@requires_auth` too.
- The admin UI is `templates/index.html`. Tabs are `<button class="tab"
  onclick="switchTab('<id>', this)">Label</button>` in the tab bar, with matching
  `<div id="<id>" class="tab-content">` panes. The "Import from Repo" tab (added
  in plan 034, `id="import-repo"`) is the most recent example to copy. There is a
  `showToast(msg, type)` helper.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `178 passed` before changes |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| New tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k health` | new tests pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `178 + N passed` |

## Scope

**In scope:**
- `app.py` — add `import zipfile`; add one `@requires_auth` route `GET /api/health` that returns JSON; add a small module-level `_scan_catalog_health()` helper it calls.
- `templates/index.html` — a "Health" tab that fetches `/api/health` and renders the report.
- `tests/test_routes.py` — tests for the scan.

**Out of scope:**
- Any repair/auto-fix. This is read-only. (Repairing icons is plan 040; deleting versions is plan 039.)
- Any change to `normalize_source`, the write paths, or storage classes.
- Scanning Garage object *contents* (a non-ZIP check on a remote object would mean downloading it). For the `garage` backend, do the ZIP-content check only for the **local** backend; for Garage, check `exists()` only and note the limitation in the report.

## Steps

### Step 1: Confirm baseline
`ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 178 passed. If not, STOP.

### Step 2: Add `_scan_catalog_health()` and `GET /api/health`

Add `import zipfile` to the imports. Add a helper that loads the catalog and
returns a list of issue dicts `{"bundleIdentifier", "version"|None, "kind",
"detail"}`. Implement exactly these checks (skip a check gracefully — never 500 —
if data is malformed):

1. **duplicate-version**: a bundle whose `versions` has a repeated `version` string.
2. **zero-size**: a version with `size` missing, `0`, or not an int.
3. **empty-or-nonpublic-downloadURL**: a version whose `downloadURL` is empty, or whose host is not the configured `PUBLIC_BASE_URL` host when `PUBLIC_BASE_URL` is set (informational — external URLs are legal, so mark severity "info").
4. **missing-icon**: an app whose `iconURL` is empty, OR (best-effort) whose hosted icon `icon_storage.exists(bundle, ext)` is False for the extension in its `iconURL` — only attempt the `exists` probe when the iconURL points at this host's `/icons/` path.
5. **missing-ipa**: for a version whose `downloadURL` points at this host's `/ipas/` path, `ipa_storage.exists(bundle, version)` is False.
6. **not-a-zip** (LOCAL backend only): a local IPA file that exists but `zipfile.is_zipfile(path)` is False (the gzip-HTML corruption class). For the Garage backend, skip this check and add one report note that content validation is unavailable remotely.

Add the route:
```python
@app.route('/api/health')
@requires_auth
def catalog_health():
    try:
        return jsonify({"issues": _scan_catalog_health()})
    except Exception as e:
        logging.error(f"Health scan error: {str(e)}")
        return jsonify({"issues": [], "error": "scan failed"}), 200
```
It must never 500 and must never mutate anything.

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 3: Add the "Health" tab

Add a `<button class="tab" onclick="switchTab('health', this)">Health</button>`
and a `<div id="health" class="tab-content">` pane with a "Scan" button that
`fetch('/api/health')` and renders `issues` as a list grouped by `kind` (show
bundle id, version, and detail; plain text, no emoji — plan 025). Empty issues →
"No problems found." Model the fetch/render on the existing `importRepoForm`
handler style.

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0; the Step 4 GET-`/`
test finds `id="health"`.

### Step 4: Tests

Add to `tests/test_routes.py` (use the `authed_client` fixture; seed catalogs
via the existing seeding pattern). Cover, one issue kind each where cheap:
1. `test_health_flags_duplicate_versions` — seed an app with two `1.0` versions → `/api/health` lists a `duplicate-version` issue for it.
2. `test_health_flags_zero_size` — a version with `size: 0` → a `zero-size` issue.
3. `test_health_flags_empty_icon` — an app with `iconURL: ""` → a `missing-icon` issue.
4. `test_health_requires_auth` — unauthenticated `GET /api/health` → 401.
5. `test_health_never_500s_on_malformed_catalog` — seed a catalog with a non-dict app / missing `versions` → returns 200 with an `issues` list (no exception).

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k health`; then full suite.

## Done criteria

- [ ] `GET /api/health` is `@requires_auth`, returns JSON, never 500s, never mutates.
- [ ] It reports the 6 check kinds in Step 2 (duplicate-version, zero-size, empty/non-public downloadURL, missing-icon, missing-ipa, not-a-zip[local only]).
- [ ] A "Health" tab renders the report; empty → "No problems found."
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (178 + new).
- [ ] `git status --short` shows only `app.py`, `templates/index.html`, `tests/test_routes.py`.

## STOP conditions
- A check would require downloading a Garage object to inspect its bytes — skip it for Garage and note the limitation (do not download).
- Implementing a check would need a write/mutating call — this route is read-only; report and stop.
- The scan can 500 on some catalog shape you can't guard — STOP (it must be exception-proof).

## Maintenance notes
- This is diagnostics only. The natural follow-ups are the *repairs*: plan 040
  (reconcile icons) fixes `missing-icon`, plan 039 (delete-version) fixes
  `duplicate-version`. Keep those actions out of this read-only view.
- New required-field/shape constraints the client enforces should get a check here.
- Reviewer scrutiny: the route must be auth-gated (it leaks internal paths) and
  the scan must be totally side-effect-free and exception-proof.
