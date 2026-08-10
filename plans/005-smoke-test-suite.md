# Plan 005: Establish a one-command smoke-test suite

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather && grep -c "os.environ.get(\"DATA_DIR\"" app.py
> ```
> Expected: `1`. If it returns `0`, **STOP** — Plan 001 has not landed and the
> app cannot be imported outside a container, which makes this plan impossible.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW (additive only — no application code changes)
- **Depends on**: `plans/001-configurable-paths-and-config.md` (mandatory)
- **Blocks**: Plans 006, 007, 008, 010 — all of them change behaviour and need a regression net
- **Category**: tests
- **Planned at**: no VCS at authoring time — 2026-08-10

## Why this matters

There is no test file, test runner, or CI configuration anywhere in this repository. The only regression record that exists is `data/app.log`, and it captures two production 500s from a previous revision:

```
2025-12-31 14:53:18,527 - ERROR - QR generation error: name 'io' is not defined
2025-12-31 14:53:21,456 - ERROR - Error serving source: send_file() got an unexpected keyword argument 'cache_timeout'
2025-12-31 14:53:21,456 - INFO - 127.0.0.1 - - "GET /source.json HTTP/1.1" 404 -
```

The second one repeated every 30 seconds as the container healthcheck failed against it. Both are errors a 20-line smoke test catches in milliseconds. Instead they reached a publicly-served endpoint and were discovered by someone tailing a log file — the development loop here is *edit → rebuild image → deploy → read logs → patch*.

Every subsequent plan (006 through 010) changes runtime behaviour on a service whose entire product is a single JSON file. Doing that without a regression net is what makes those plans risky. This plan is the thing that de-risks them.

## Current state

**Application entry point**: `app.py` — a single Flask app, 13 routes. After Plan 001, `DATA_DIR` is read from the environment with a default of `/app/data`, and the four path constants derive from it:

```python
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
SOURCE_FILE = os.path.join(DATA_DIR, "source.json")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
IPA_FOLDER = os.path.join(DATA_DIR, "ipas")
ICON_FOLDER = os.path.join(DATA_DIR, "icons")
```

**Critical import-order fact**: `app.py:718-719` runs at module scope —

```python
# Initialize source manager
source_manager = SourceManager(SOURCE_FILE)
```

`SourceManager.__init__` calls `ensure_data_directory()` (which creates `DATA_DIR`) and `initialize_source()` (which writes a default `source.json` if absent). **This means `DATA_DIR` must be set in `os.environ` *before* `import app` executes.** A normal pytest fixture runs too late. See Step 3 for the pattern that handles this.

**The 13 routes** (`app.py:2218-2519`):

| Route | Method | Line | Notes |
|---|---|---|---|
| `/` | GET | 2218 | serves the embedded HTML page |
| `/source.json` | GET | 2222 | **the entire product** — iOS clients poll this |
| `/ipas/<bundle_id>/<filename>` | GET | 2230 | payload delivery |
| `/icons/<bundle_id>/icon.<ext>` | GET | 2248 | allowlists ext against `ALLOWED_ICON_EXTENSIONS` |
| `/qr` | GET | 2279 | onboarding QR (see Plan 004) |
| `/api/apps` | GET | 2300 | |
| `/api/add-app` | POST | 2311 | mutating |
| `/api/delete-app` | POST | 2345 | mutating — also deletes IPA files from disk |
| `/api/app/<bundle_identifier>` | GET | 2365 | |
| `/api/update-app` | POST | 2376 | mutating |
| `/api/add-version` | POST | 2420 | mutating |
| `/api/update-version` | POST | 2462 | mutating |
| `/api/update-source` | POST | 2506 | mutating |

The mutating routes accept **either** `multipart/form-data` **or** JSON, branching on `request.content_type`. Example, `app.py:2345-2359`:

```python
@app.route('/api/delete-app', methods=['POST'])
def delete_app():
    try:
        data = request.json
        bundle_id = data.get('bundleIdentifier')

        if not bundle_id:
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400

        success, message = source_manager.delete_app(bundle_id)

        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
```

**The `source.json` shape** — a minimal valid document for fixtures:

```json
{
  "name": "Test Source",
  "subtitle": "",
  "description": "",
  "iconURL": "",
  "headerURL": "",
  "website": "",
  "tintColor": "#4185A9",
  "featuredApps": [],
  "apps": [
    {
      "name": "Example App",
      "bundleIdentifier": "com.example.app",
      "developerName": "Example Dev",
      "localizedDescription": "",
      "iconURL": "",
      "addedDate": "2026-01-01",
      "versions": [
        {
          "version": "1.0.0",
          "date": "2026-01-01T00:00:00Z",
          "downloadURL": "http://example.test/ipas/com.example.app/1.0.0.ipa",
          "minOSVersion": "14.0",
          "size": 1234
        }
      ]
    }
  ],
  "news": []
}
```

**Repo conventions**: plain Python, 4-space indent, no type annotations, no linter or formatter config. There are no existing tests to model on, so this plan defines the pattern. Keep it plain `pytest` — no fixtures library, no factories, no mocking framework.

**Environment**: the host runs Python 3.14; the container runs Python 3.11 (`Dockerfile:1`). `pytest` is **not** installed on the host. See Step 1 — this matters, because `Flask==2.3.3` may not install cleanly on Python 3.14.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Create venv | `python3 -m venv .venv` | exit 0 |
| Install | `.venv/bin/pip install -r requirements.txt -r requirements-dev.txt` | exit 0 |
| Run tests | `.venv/bin/python -m pytest tests/ -q` | all pass, <5s |
| Run tests (container fallback) | `docker compose exec altstore-manager python -m pytest /app/tests -q` | all pass |
| Host Python | `python3 --version` | 3.14.x |

## Scope

**In scope** (files you create):
- `tests/test_routes.py` (create)
- `tests/__init__.py` (create, empty — keeps imports unambiguous)
- `requirements-dev.txt` (create)
- `.gitignore` — append `.venv/` if Plan 002 has run and it is not already there

**Out of scope** (do NOT touch):
- `app.py` — **this plan changes zero application code.** If a test fails because the app has a bug, that is a *finding to report*, not a thing to fix here. Plans 006 and 007 fix the known bugs; this plan documents current behaviour.
- `data/` — no test may read from or write to the real data directory. Everything goes to `tmp_path`.
- `requirements.txt` — dev dependencies live in `requirements-dev.txt`, so the production image stays lean.
- `Dockerfile` — do not add tests to the image.

## Git workflow

- Branch: `advisor/005-smoke-tests`
- One commit for the suite.

## Steps

### Step 1: Establish a working Python environment

The host has Python 3.14 and no pytest. `Flask==2.3.3` is from 2023 and may not install on 3.14.

```
cd /home/t1nk33r/Documents/feather
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**If that succeeds**: continue on the host. This is the preferred path — fast tests, no Docker.

**If it fails** (build errors, incompatible metadata, `altparse` refusing to install): do not fight it. Fall back to running tests inside the container, which already has a working Python 3.11 environment:
```
docker compose exec altstore-manager pip install pytest
```
and mount/copy `tests/` in. **Record which path you took** in your report and in `plans/README.md` — every later plan's "run the tests" instruction depends on it.

**Verify**: `.venv/bin/python -c "import flask, qrcode, requests, altparse; print('ok')"` → `ok`
(or the container equivalent)

### Step 2: Create `requirements-dev.txt`

```
pytest==8.3.4
```

Then install: `.venv/bin/pip install -r requirements-dev.txt`

**Verify**: `.venv/bin/python -m pytest --version` → prints a version, exit 0

### Step 3: Write the fixture that solves the import-order problem

Create `tests/__init__.py` (empty) and `tests/test_routes.py`.

The fixture must set `DATA_DIR` **before** `app` is imported, because `app.py:719` instantiates `SourceManager` at module scope. Use a module-scoped fixture that sets the env var and imports inside the fixture body:

```python
import json
import os
import importlib
import pytest


@pytest.fixture(scope="function")
def client(tmp_path):
    """Fresh app instance rooted at an isolated temp data dir.

    DATA_DIR must be set before `app` is imported: app.py instantiates
    SourceManager at module scope (app.py:719), which creates directories
    and writes a default source.json on construction.
    """
    os.environ["DATA_DIR"] = str(tmp_path)

    # Re-import so module-level constants pick up the new DATA_DIR.
    import app as app_module
    importlib.reload(app_module)

    # Seed a known catalog.
    seed = { ... }   # the JSON document from "Current state" above
    (tmp_path / "source.json").write_text(json.dumps(seed))

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c
```

Notes:
- `importlib.reload` is needed because pytest may have imported `app` already via collection. Reloading re-executes the module-level constants *and* `SourceManager(SOURCE_FILE)`.
- Seed the catalog **after** the reload, because `initialize_source()` writes a default `source.json` during construction and would otherwise overwrite your seed.
- Use `scope="function"` with `tmp_path` so every test gets a clean catalog. These tests are fast; isolation is worth more than speed here.

**Verify**: `.venv/bin/python -m pytest tests/ -q --collect-only` → collects without error

### Step 4: Cover the read paths (highest priority first)

Write these tests in `tests/test_routes.py`. They are ordered by what breaks worst.

1. **`GET /source.json`** — *the entire product; if this 500s, every subscribed iOS device breaks.*
   - status 200
   - body parses as JSON
   - has an `apps` key containing the seeded app
   - This is the direct regression test for the `cache_timeout` incident in `data/app.log`.

2. **`GET /ipas/<bundle>/<filename>`** — *payload delivery, 1.3 GB behind it.*
   - write a small fake file to `tmp_path/ipas/com.example.app/1.0.0.ipa`
   - request it → 200, and the body matches what you wrote
   - request a missing file → 404 (not 500)

3. **`GET /icons/<bundle>/icon.png`** — write a fake icon, expect 200.
   - `GET /icons/<bundle>/icon.exe` → **400** (the extension allowlist at `app.py:2256`)

4. **`GET /qr`** → 200, `image/png`, body starts with PNG magic bytes `b'\x89PNG\r\n\x1a\n'`.
   - This is the permanent guard for Plan 004. **If Plan 004 has not run, this test will fail** — that is correct and expected. Mark it `@pytest.mark.xfail(reason="fixed by plan 004")` and note it in your report rather than skipping it.

5. **`GET /api/apps`** → 200, a JSON list with one entry.

6. **`GET /api/app/com.example.app`** → 200. `GET /api/app/com.nonexistent`  → 404.

7. **`GET /`** → 200, and the body contains a marker string from the page. Use something stable — a heading or an element id such as `qrImage`. This becomes the guard for Plan 009's template extraction.

**Verify**: `.venv/bin/python -m pytest tests/ -q` → the read-path tests pass (with `/qr` xfail if Plan 004 is pending)

### Step 5: Cover the mutating routes

8. **Round trip**: `POST /api/add-app` with a JSON body → assert the new app appears in `GET /source.json` → `POST /api/delete-app` → assert it is gone.
   Use `client.post('/api/add-app', json={...})` — the JSON branch at `app.py:2333` is much easier to drive than multipart. A minimal valid body needs `name`, `bundleIdentifier`, `developerName`, `version`.

9. **`POST /api/add-version`** — add a version to the seeded app; assert `versions` grows from 1 to 2.
   Note `app.py:2448` requires one of `ipaFile` / `downloadURL` / `downloadFromUrl`, so include a `downloadURL`.

10. **`POST /api/update-version`** — change `minOSVersion` on the seeded version; assert it persisted.

11. **`POST /api/update-app`** — change `name`; assert it persisted.

12. **`POST /api/update-source`** — change the source `name`; assert `GET /source.json` reflects it.

13. **Validation**: each of `/api/delete-app`, `/api/update-app`, `/api/add-version`, `/api/update-version` with a body missing `bundleIdentifier` → **400** with a JSON error.

**Important — document current behaviour, do not fix it.** Some of these routes have known bugs (they report success when an operation failed; see Plan 007). If a test reveals that, assert what the code *actually does today* and add a comment naming the plan that will change it:

```python
# NOTE: currently returns 200 even though the download failed.
# Plan 007 changes this to 400; update this assertion then.
```

That keeps the suite green and makes Plan 007's diff self-documenting.

**Verify**: `.venv/bin/python -m pytest tests/ -q` → all pass

### Step 6: Confirm isolation

The suite must never touch real data.

**Verify**:
```
md5sum data/source.json > /tmp/before.md5
.venv/bin/python -m pytest tests/ -q
md5sum -c /tmp/before.md5
```
→ `data/source.json: OK`

**Also verify** no network access is attempted: the suite must not make real HTTP requests. `grep -c "requests.get\|http://" tests/test_routes.py` should only match the fake `downloadURL` strings in fixtures, which are never fetched because the tests do not set `downloadFromUrl=true`.

### Step 7: Commit

```
git add tests/ requirements-dev.txt .gitignore
git commit -m "test: add smoke-test suite covering all 13 routes

First tests in this repo. Covers the read paths (/source.json, /ipas,
/icons, /qr, /) and a round-trip through the six mutating routes.

Direct regression coverage for the two production 500s recorded in
data/app.log: 'name io is not defined' on /qr and
'send_file() got an unexpected keyword argument cache_timeout' on
/source.json.

Tests assert current behaviour, including known bugs — see NOTE
comments referencing plans 004 and 007."
```

## Test plan

This plan *is* the test plan. Target: **13 routes covered, roughly 20 test functions, full suite under 5 seconds, no Docker and no network required.**

Structural pattern for everything that follows: one `client` fixture, `tmp_path`-rooted, function-scoped; tests named `test_<route>_<condition>`; assertions on status code first, then response shape.

## Done criteria

ALL must hold:

- [ ] `.venv/bin/python -m pytest tests/ -q` exits 0 (or the container equivalent, if Step 1 fell back)
- [ ] The suite runs in under 5 seconds
- [ ] All 13 routes have at least one test
- [ ] `md5sum -c` confirms `data/source.json` is byte-identical before and after a test run
- [ ] No test requires Docker or network access
- [ ] `git status --short` shows `app.py` **unmodified**
- [ ] `.venv/` is gitignored
- [ ] `plans/README.md` status row updated, recording whether tests run on host or in container

## STOP conditions

Stop and report back (do not improvise) if:

- `grep -c "os.environ.get(\"DATA_DIR\"" app.py` returns `0` — Plan 001 has not landed; this plan cannot proceed.
- Neither the host venv **nor** the container can run pytest. Report both error messages.
- `importlib.reload(app_module)` does not pick up the new `DATA_DIR` (tests write to `/app/data` or fail with `PermissionError`). Do not work around this by editing `app.py` — report it; the fixture strategy needs rethinking.
- A test run modifies `data/source.json`. **Stop immediately** — that is the real catalog. Report before running anything else.
- More than three routes turn out to be genuinely broken (not just buggy). That suggests the deployed code differs from what is on disk; report rather than writing tests around it.
- You are tempted to change `app.py` to make a test pass. Out of scope — report the bug instead.

## Maintenance notes

- **The reload-based fixture is the fragile part.** It exists solely because `app.py:719` constructs `SourceManager` at import time. If a future refactor introduces an app factory (`create_app()`), this fixture should be simplified to call it directly — that would be a genuine improvement and the tests would get faster and less magical.
- Tests deliberately assert *current* behaviour including known bugs, with `NOTE` comments pointing at Plans 004 and 007. A reviewer should expect those assertions to be **inverted** by those plans; a plan that changes behaviour without updating the corresponding assertion has not been finished.
- Coverage is deliberately shallow-and-wide: one or two cases per route rather than deep coverage of `SourceManager`. That is the right shape for a smoke suite whose job is to catch import errors, signature changes, and 500s. Deeper unit tests on `SourceManager` become worthwhile after Plans 006 and 007 restructure it.
- **Reviewer should scrutinise**: that no test touches `data/`, and that the `/` test's marker string is something stable — if it asserts on text that Plan 009 or 010 legitimately changes, it becomes a false alarm.
