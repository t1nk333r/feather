# Plan 037: Never publish an unparseable source — coerce empty `iconURL`, dedupe versions

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 3eeb0cc..HEAD -- app.py tests/test_routes.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 170 passed
> ```
> If `app.py` changed since `3eeb0cc`, compare the "Current state" excerpts below
> against the live code before proceeding; on a mismatch, STOP.

## Status

- **Priority**: P1 (the live source currently fails to load in Feather/AltStore)
- **Effort**: S–M
- **Risk**: LOW–MED (edits the serve-time normalizer and `add_version`; the normalizer never writes to disk)
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `3eeb0cc`, 2026-08-17

## Why this matters

The Feather/AltStore client rejects the whole source with "The data couldn't be
read because it isn't in the correct format" when the JSON is valid but a field
is the wrong shape. Two such shapes are being produced today:

1. **Empty `iconURL` (`""`).** Apps created without an icon (e.g. a repo import
   with no icon, or a hand-added entry) get `"iconURL": ""`. The client decodes
   `iconURL` into a `URL`, and an empty string is not a valid URL — so the decode
   throws and the *entire* source fails to load. Three live apps currently have
   this.
2. **Duplicate versions.** Re-adding the same `bundle+version` appends a second
   (identical) version entry via `add_version`. Duplicate version strings break
   the client's version handling — the same "isn't in the correct format" error.
   (This already broke the live source with three `12.17` entries on one app.)

The **serve path already has a normalizer** — `normalize_source()` (Plan 024) —
that backfills AltStore-required fields at serve time on a deep copy, never
touching disk, so it "fixes every existing entry on the next request without a
migration." This plan extends it to also **coerce an empty/missing `iconURL` to a
valid URL** and **dedupe versions**, so `/source.json` can never emit a document
its own client refuses to parse — including the currently-broken data, fixed the
moment this deploys. It also adds a guard in `add_version` so duplicates stop
being *stored* in the first place.

## Current state — `app.py`

- Module constant (app.py:32): `SOURCE_ARTWORK_URL = "https://f002.backblazeb2.com/file/S30000PUBLIC/MEDIA-PUBLIC/feather-tinker-1024.png"` — a valid, reachable image URL, used as the icon fallback.
- `normalize_source(source_data)` (app.py:140–180). It deep-copies, sets `nsfw`, then per app sets `appPermissions`, then per version sets `buildVersion`:
  ```python
  data = copy.deepcopy(source_data)
  data.setdefault("nsfw", False)
  apps = data.get('apps')
  if isinstance(apps, list):
      for app_entry in apps:
          if not isinstance(app_entry, dict):
              continue
          if 'appPermissions' not in app_entry:
              app_entry['appPermissions'] = {"entitlements": [], "privacy": {}}
          versions = app_entry.get('versions')
          if isinstance(versions, list):
              for version_entry in versions:
                  if not isinstance(version_entry, dict):
                      continue
                  if 'buildVersion' not in version_entry:
                      version_entry['buildVersion'] = str(version_entry.get('version', ''))
  return data
  ```
  It is called only by `serve_source()` (`GET /source.json`) and must never
  raise (every device polls it). It works on a deep copy — safe to mutate freely.
- `SourceManager.add_version(...)` (app.py:1127). After locating the app
  (`app_index`) and computing `version = version_data['version']` (app.py:1157),
  it unconditionally inserts a new version (app.py:1190):
  ```python
  version = version_data['version']
  ...
  # Insert at the beginning (latest version first)
  app['versions'].insert(0, new_version)
  ```
  There is no check for an already-present version.

### Repo conventions

- Tests: `tests/test_routes.py`. `serve_source` / `normalize_source` are tested
  there (search `normalize` and `source.json`); `add_version` via `/api/add-version`
  and directly on `source_manager`. Fixtures `client` / `authed_client` (lines
  73–120) reload `app` with an isolated `DATA_DIR` and seed a catalog. No network.
- Conventional commits.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `170 passed` before changes |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| Route tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q` | all pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `170 + N passed` |

## Scope

**In scope** (modify only these):
- `app.py` — `normalize_source()` and `SourceManager.add_version()` only.
- `tests/test_routes.py` — add cases.

**Out of scope** (do NOT touch):
- The stored `data/source.json` (this is a code fix; the normalizer repairs the
  served output without editing stored data).
- `update_version` (explicit replace stays as-is), `add_app_manual`, other methods.
- The UI, the release importer, the Telegram worker, container/CI, requirements.

## Git workflow

- Branch: `advisor/037-source-always-parseable`
- Commits: normalizer change, `add_version` guard, then tests. Do NOT push or open a PR.

## Steps

### Step 1: Confirm baseline
`ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → **170 passed**. If not, STOP.

### Step 2: Coerce empty/missing `iconURL` in `normalize_source`

Inside the per-app loop in `normalize_source` (after the `appPermissions`
backfill, app.py:169–170), add an icon-URL coercion. Use the source's own
top-level `iconURL` when it is a valid non-empty string, else the
`SOURCE_ARTWORK_URL` constant — both are valid reachable URLs:
```python
icon = app_entry.get('iconURL')
if not (isinstance(icon, str) and icon.strip()):
    top_icon = data.get('iconURL')
    app_entry['iconURL'] = top_icon if (isinstance(top_icon, str) and top_icon.strip()) else SOURCE_ARTWORK_URL
```
This runs on the deep copy, so it only changes the served output.

### Step 3: Dedupe versions in `normalize_source`

Still inside the per-app loop, after the `versions`/`buildVersion` handling, drop
duplicate version entries keeping the first occurrence of each `version` string
(order preserved):
```python
versions = app_entry.get('versions')
if isinstance(versions, list):
    seen = set()
    deduped = []
    for version_entry in versions:
        if not isinstance(version_entry, dict):
            deduped.append(version_entry)   # leave malformed entries untouched
            continue
        v = version_entry.get('version')
        if v in seen:
            continue
        seen.add(v)
        deduped.append(version_entry)
    app_entry['versions'] = deduped
```
(You may fold this into the existing `versions` loop that sets `buildVersion`, or
add it right after — either way, buildVersion must still be set on the kept
entries, and the function must still never raise on malformed input.)

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 4: Stop storing duplicate versions in `add_version`

In `SourceManager.add_version`, right after `version = version_data['version']`
(app.py:1157) and before the IPA-handling block, short-circuit when that version
already exists on the app — idempotent success, no re-download, no duplicate:
```python
version = version_data['version']
if any(isinstance(v, dict) and v.get('version') == version for v in app.get('versions', [])):
    return True, f"Version {version} already exists; nothing to add"
```
Leave the rest of the method unchanged.

**Verify**:
```bash
.venv/bin/python -m py_compile app.py
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```
**If an existing test fails here**, check whether it relied on `add_version`
appending a *duplicate* version (e.g. a concurrency test that adds the same
version repeatedly). If a test adds genuinely *distinct* versions, it must still
pass — that is the intended behavior. If a test truly asserts duplicate-append,
**STOP and report** (do not edit the existing test to match); the reviewer will
decide. If tests fail for any other reason, STOP.

### Step 5: Tests

Add to `tests/test_routes.py` (model on the existing `normalize`/`source.json`
tests and the `add_version` tests):

1. `test_source_json_coerces_empty_icon_url` — seed a catalog with an app whose
   `iconURL` is `""`; `GET /source.json`; assert that app's `iconURL` in the
   response is a non-empty string starting with `http`.
2. `test_source_json_dedupes_duplicate_versions` — seed an app with three version
   entries all `version="1.0"`; `GET /source.json`; assert that app now has
   exactly one version `"1.0"`.
3. `test_normalize_source_leaves_valid_icon_url_untouched` — an app with a real
   `iconURL` keeps it unchanged through `/source.json`.
4. `test_add_version_skips_existing_version` — via `source_manager.add_version`
   (or `/api/add-version`), add a version that already exists on the seeded app;
   assert it returns success AND the app still has exactly one entry for that
   version (no duplicate appended).

**Prove discrimination**: temporarily revert Step 2 → test 1 fails; temporarily
revert Step 4 → test 4 fails. Restore both.

**Verify**:
```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

## Test plan

- 4 new tests in `tests/test_routes.py`: empty-icon coercion, version dedupe at
  serve time, valid-icon untouched, and `add_version` duplicate skip.
- The whole suite must stay green (170 + 4). If any *existing* test breaks, it is
  a STOP condition (see Step 4).

## Done criteria

- [ ] `GET /source.json` never emits an app with an empty/missing `iconURL` — it is coerced to a valid URL.
- [ ] `GET /source.json` never emits duplicate version strings within one app.
- [ ] `normalize_source` still never raises on malformed input, and still runs on a deep copy (stored `source.json` is not modified by a read).
- [ ] `add_version` returns success without appending when the version already exists; adding a *new* version still works.
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (170 + 4); no existing test modified to accommodate the change.
- [ ] `git status --short` shows only `app.py` and `tests/test_routes.py`.

## STOP conditions

- An existing test fails because it depended on `add_version` appending a
  duplicate version — report; do not rewrite that test.
- Coercing `iconURL` or deduping would require `normalize_source` to raise or to
  write to disk (it must do neither).
- The stored `source.json` would need editing to make the fix work (it must not —
  the whole point is serve-time repair).

## Maintenance notes

- The serve-time normalizer is now the single guarantee that `/source.json` is
  always client-parseable regardless of what is stored. Any future required
  field or shape constraint the client enforces should be added here too.
- `add_version` is now idempotent for an already-present version. A deliberate
  same-version *replacement* remains an explicit `update_version` action.
- `normalize_source` runs on every `/source.json` poll; keep it O(apps×versions)
  and allocation-light. The dedupe adds one set per app — negligible at this scale.
- A reviewer should confirm: the normalizer still deep-copies and never raises;
  the icon fallback is always a valid URL; and no existing test was weakened.
