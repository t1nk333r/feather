# Plan 024: Emit the AltStore-required fields so the source imports

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result. If anything in
> "STOP conditions" occurs, stop and report — do not improvise. When done,
> update the status row in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD
> grep -n "def initialize_source" app.py
> grep -n "@app.route('/source.json')" app.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q   # expect 102 passed
> ```

## Status

- **Priority**: P1 — **the source will not import into Feather/AltStore on a device.** This is the product not working.
- **Effort**: S
- **Risk**: LOW — additive fields; nothing existing is removed or renamed
- **Depends on**: 005 (tests). 021 is relevant but not required.
- **Category**: bug (correctness)
- **Planned at**: 2026-08-12

## Why this matters

The operator reports the source will not import on iOS. The served document is valid JSON, HTTP 200, `application/json` — but it is missing three fields the AltStore source format lists as **required**.

Audited against the live document:

```
top-level missing:  ['nsfw']
app missing:        ['appPermissions']
version missing:    ['buildVersion']
```

Per <https://faq.altstore.io/developers/make-a-source>:

- **Required top-level**: `name`, `apps`, `news`, **`nsfw`**
- **Required per app**: `name`, `bundleIdentifier`, `developerName`, `localizedDescription`, `iconURL`, `versions`, **`appPermissions`**
- **Required per version**: `version`, **`buildVersion`**, `date`, `downloadURL`, `size`

A strict client decoder rejects the entire document when a non-optional key is absent, which is consistent with "won't import" rather than "imports but looks wrong".

**Confidence note, stated honestly**: this is a strong hypothesis, not a reproduction. Nobody has confirmed on-device that adding these three fields fixes the import — the executor cannot test that, and neither can the reviewer. The three fields *are* documented as required and *are* absent, which is enough to act on. If the source still fails to import after this lands, the next suspects are TLS/redirect behaviour and the client's own logs — not more schema guessing.

## Current state

`app.py`. The default catalog written by `initialize_source` has no `nsfw`:

```python
            initial_source = {
                "name": "My AltStore Source",
                "subtitle": "Custom iOS app repository",
                ...
                "featuredApps": [],
                "apps": [],
                "news": []
            }
```

The live served document, in full, for reference:

```json
{
 "name": "T1nk33r's Store",
 "subtitle": "T1nk33r's Store",
 "description": "T1nk33r's Store",
 "iconURL": "https://f000.backblazeb2.com/.../OctoSource.png",
 "headerURL": "https://f000.backblazeb2.com/.../OceanHeader.png",
 "website": "https://feather.example.com",
 "tintColor": "#d02828",
 "featuredApps": [],
 "apps": [
  {
   "name": "Immich",
   "bundleIdentifier": "app.alextran.immich",
   "developerName": "Unknown",
   "localizedDescription": "",
   "iconURL": "https://feather.example.com/icons/app.alextran.immich/icon.jpg",
   "addedDate": "2026-08-12",
   "versions": [
    {"version": "3.1.0", "date": "2026-08-12T13:26:03Z",
     "downloadURL": "https://feather.example.com/ipas/app.alextran.immich/3.1.0.ipa",
     "minOSVersion": "14.0", "size": 32593548}
   ]
  }
 ],
 "news": []
}
```

## The field shapes, from the spec

| Field | Type | Safe default |
|---|---|---|
| `nsfw` | boolean, source level | `false` |
| `appPermissions` | `{"entitlements": [...], "privacy": {...}}` | `{"entitlements": [], "privacy": {}}` |
| `buildVersion` | string — the app's `CFBundleVersion` | see below |

`buildVersion` is **not** the same as `version`: `version` is `CFBundleShortVersionString` (`3.1.0`), `buildVersion` is `CFBundleVersion` (`25`). The spec says both "should match exactly what is in your `Info.plist`".

Verified against real IPAs in `data/ipas/`:

```
com.ryan.anymex           version=3.0.3     buildVersion=25
com.google.ios.youtube    version=20.49.5   buildVersion=20.49.5
```

They differ sometimes and coincide other times. **Where the true value is unknown, fall back to `version`** — a wrong-but-present string is what makes the document decode; an absent key is what breaks it.

## Design: normalise at serve time

Add one function, `normalize_source(source_data)`, and call it in exactly one place: the `/source.json` route, immediately before serialising.

Why serve-time rather than at write time:

- **It fixes every existing entry instantly**, with no migration over `data/source.json` and no hand-editing. The operator's catalog becomes importable on the next request.
- **It is one call site**, so it cannot drift between `add_app_manual`, `add_app_from_github`, `add_app_from_altsource` and `add_version` — four places that would each need the same additions.
- **It cannot corrupt stored data**, because it never writes. Given Plans 006 and 007 exist precisely because this catalog has been corrupted before, a read-only fix is the conservative choice.

The trade: `data/source.json` on disk still lacks the fields. That is acceptable — the **served document is the contract**, and the web UI reads the same stored file it always has.

`normalize_source` must:

- Work on a **deep copy**. It must not mutate the dict it was handed. `load_source` returns fresh data per call today, but a future cache would turn mutation into a silent corruption path.
- Add `nsfw: False` at the top level **only if absent**.
- For each app, add `appPermissions: {"entitlements": [], "privacy": {}}` **only if absent**.
- For each version, add `buildVersion` **only if absent**, defaulting to that version's `version` value coerced to `str`.
- **Never overwrite a value that is already present.** An operator or a future importer may set a real `buildVersion` or real permissions, and this must leave them alone.
- Tolerate malformed input — a missing `apps` key, an app without `versions`, a version that is not a dict — without raising. This runs on the request path for the one route that must never 500; every subscribed device polls it.

## Scope

**In scope**:
- `app.py` — the `normalize_source` function, its single call in the `/source.json` route, and `nsfw: False` added to `initialize_source`'s default document
- `tests/test_routes.py`

**Out of scope — do NOT touch**:
- `add_app_manual`, `add_app_from_github`, `add_app_from_altsource`, `add_version`, `update_version`, `update_app` — the whole point of serve-time normalisation is not to touch these four-plus creation paths.
- `save_source`, `load_source`, `_backup_source`, the `_lock` machinery (Plan 006). **`normalize_source` must not write.**
- The storage classes and `resolve_base_url` (Plans 008, 011).
- `scripts/telegram_bot_ingest.py` — the bot needs no change for this.
- `data/source.json` — no hand edits, no migration script.
- Removing or renaming any existing field, including `subtitle`, `description`, `headerURL`, `tintColor`, `featuredApps`, `addedDate`, `minOSVersion`. They are optional-but-valid and clients ignore what they do not use.
- The 102 existing tests. If one needs editing to pass, report.

## Steps

### Step 1: Add `normalize_source`

Place it near the other module-level helpers (after `resolve_base_url`). Follow the file's conventions: no type annotations, 4-space indent, plain functions.

Signature: `normalize_source(source_data)` returning a new dict. Use `copy.deepcopy` — add `import copy` if absent.

Guard every access: `isinstance(source_data, dict)`, `isinstance(app, dict)`, `isinstance(version, dict)`. On anything unexpected, leave that element untouched and carry on rather than raising.

**Verify**:
```
ADMIN_PASSWORD=x DATA_DIR=/tmp/n1 .venv/bin/python -c "
import app
src = {'name':'x','apps':[{'bundleIdentifier':'a.b','versions':[{'version':'1.0'}]}],'news':[]}
out = app.normalize_source(src)
print('nsfw:', out['nsfw'])
print('appPermissions:', out['apps'][0]['appPermissions'])
print('buildVersion:', out['apps'][0]['versions'][0]['buildVersion'])
print('input untouched:', 'nsfw' not in src)
"
```
→ `False`, `{'entitlements': [], 'privacy': {}}`, `1.0`, `True`.

The last line matters: it proves the deep copy.

### Step 2: Call it from `/source.json` only

In the `/source.json` route, pass the loaded data through `normalize_source` before returning it. Change nothing else about the route — not its caching, headers, or error handling.

**Verify**: `grep -c "normalize_source" app.py` → `2` (definition + one call). If it is 3 or more, it has been wired into a write path, which is out of scope.

### Step 3: Add `nsfw` to the default document

Add `"nsfw": False` to `initialize_source`'s `initial_source` dict, so a freshly created catalog is compliant on disk as well as over the wire. This is belt-and-braces; Step 2 already covers it at serve time.

**Verify**: `grep -c '"nsfw"' app.py` → `2` (the default document + `normalize_source`).

### Step 4: Tests

Add to `tests/test_routes.py`, reusing the existing `client` fixture.

1. `test_source_json_has_required_top_level_fields` — `GET /source.json` contains `name`, `apps`, `news`, `nsfw`.
2. `test_source_json_apps_have_app_permissions` — every app in the response has `appPermissions` with `entitlements` (list) and `privacy` (dict).
3. `test_source_json_versions_have_build_version` — every version has a `buildVersion` string.
4. `test_normalize_does_not_overwrite_existing_values` — seed a catalog whose app already has real `appPermissions` and whose version has `buildVersion: "25"`; assert both survive unchanged. **This is the important one** — it is what stops a future real value being clobbered.
5. `test_normalize_does_not_mutate_input` — call `normalize_source` and assert the input dict is unchanged.
6. `test_normalize_tolerates_malformed_catalog` — apps missing `versions`, a version that is a string rather than a dict, a missing `apps` key: no exception, and `GET /source.json` still returns 200.

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → **108** (102 + 6), the 102 existing unmodified.

### Step 5: Confirm the served document validates

```
ADMIN_PASSWORD=x DATA_DIR=/tmp/n2 .venv/bin/python - <<'PY'
import app, json
c = app.app.test_client()
d = json.loads(c.get('/source.json').data)
top=['name','apps','news','nsfw']
appk=['name','bundleIdentifier','developerName','localizedDescription','iconURL','versions','appPermissions']
verk=['version','buildVersion','date','downloadURL','size']
print('top missing:', [k for k in top if k not in d])
for a in d['apps']:
    print('app missing:', [k for k in appk if k not in a])
    for v in a['versions']:
        print('ver missing:', [k for k in verk if k not in v])
PY
```
→ `top missing: []`, and empty lists for any app present.

## Done criteria

ALL must hold:

- [ ] `grep -c "normalize_source" app.py` returns `2` — one definition, one call site
- [ ] `grep -c '"nsfw"' app.py` returns `2`
- [ ] `normalize_source` does not mutate its argument — proven by test 5
- [ ] Existing `appPermissions` / `buildVersion` values are never overwritten — proven by test 4
- [ ] A malformed catalog does not make `/source.json` raise — proven by test 6
- [ ] `git diff` shows no change inside `save_source`, `load_source`, or any `add_*` / `update_*` method
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 108, the 102 pre-existing unmodified
- [ ] `git status --short` shows only `app.py` and `tests/test_routes.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

- You are tempted to make `normalize_source` write, or to call it from `save_source` or any mutating method. It is read-only, one call site.
- You are tempted to overwrite an existing `buildVersion` or `appPermissions`. Only fill absences.
- You are tempted to remove or rename an existing field to "clean up" the document. Optional extras are harmless; a removed field may not be.
- `/source.json` can raise on any input after your change. Every subscribed device polls that route; it must not 500.
- Any of the 102 existing tests needs editing.

## Maintenance notes

- **This is a strong hypothesis, not a confirmed fix.** The three fields are documented as required and are absent, which is enough to act on — but the on-device import has not been retested. If it still fails afterwards, look at TLS/redirect behaviour and the client's own error output rather than adding more speculative fields.
- **Real values would be better than defaults, and are obtainable.** `buildVersion` is `CFBundleVersion`, which Plan 021's extractor already reads and discards. `appPermissions.privacy` is directly available too — YouTube's `Info.plist` carries 9 `NS*UsageDescription` keys, AnyMex's carries none. Wiring those through the bot would make the catalog genuinely accurate rather than merely valid. That is a follow-up, deliberately not bundled here: this plan's job is to make the source importable, and it must stay small enough to land immediately.
- **Serve-time normalisation means the stored file stays non-compliant.** Anyone reading `data/source.json` directly, or migrating to another tool, gets a document missing these keys. If that ever matters, the fix is a one-off migration, not moving normalisation into the write path.
- **`developerName` is currently `"Unknown"`** for the app the bot created (Plan 023's default). Valid, but worth correcting in the web UI.
