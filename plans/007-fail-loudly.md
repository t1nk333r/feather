# Plan 007: Report failures instead of silently reporting success

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> md5sum app.py                       # expect 4e087dfa7451be26efbef3323c3c2dbd
> sed -n '679,705p' app.py
> grep -c "delete_icon_file" app.py   # expect 1 — definition only, no call sites
> .venv/bin/python -m pytest tests/ -q   # expect 21 passed
> ```
> The excerpt must match "Current state / Defect 2" below. **All line numbers in
> this plan were re-checked against `main` at `41c417a` on 2026-08-11**, after
> Plans 001, 004 and 005 landed; `app.py` is 2546 lines. If the md5 differs,
> match on the code excerpts rather than the line numbers.

## Status

- **Priority**: P1
- **Effort**: S–M (five related defects in one code region)
- **Risk**: LOW–MED — some requests that currently return 200 will start returning 400. That is the point, but it is user-visible.
- **Depends on**: `plans/005-smoke-test-suite.md` (mandatory)
- **Recommended after**: `plans/006-atomic-catalog-writes.md` — both touch `SourceManager`; doing 006 first avoids conflicts
- **Category**: bug
- **Planned at**: 2026-08-10. **Line numbers refreshed 2026-08-11** against `main` at `41c417a`; `app.py` md5 `4e087dfa7451be26efbef3323c3c2dbd`.

## Why this matters

The file-handling helpers return `(None, 0)` on any exception. Their callers test `if filepath:` and have **no `else` branch**, so a failed upload or download falls straight through to building the catalog entry and returning `"App added successfully"`.

**This is not theoretical — it has already corrupted the live catalog.** `data/source.json` currently publishes:

```json
{
  "name": "qBitControl",
  "bundleIdentifier": "com.michael-128.qBitControl",
  "versions": [{ "version": "1.3.3", "size": 0, ... }]
}
```

`"size": 0` for an app whose real binary is 4,321,496 bytes. The download failed, `get_file_size` returned `0`, nobody was told, and the wrong number was published to every client.

Five distinct defects live in this one code region — the fifth was found while writing the Plan 005 test suite. The most dangerous is #2: `update_version` deletes the existing IPA **before** fetching its replacement, so a transient network error permanently destroys a hosted binary while the catalog keeps advertising it — and returns `{"success": true}`.

## Current state

### Defect 1 — silent success

`app.py:81-91` — the helper's failure mode:

```python
    def save_ipa_file(self, file, bundle_id, version):
        """Save uploaded IPA file"""
        try:
            filepath = self.get_ipa_path(bundle_id, version)
            file.save(filepath)
            file_size = get_file_size(filepath)
            logging.info(f"Saved IPA file: {filepath} ({file_size} bytes)")
            return filepath, file_size
        except Exception as e:
            logging.error(f"Error saving IPA file: {str(e)}")
            return None, 0
```

`app.py:283-306` — the caller, with no `else` on either branch:

```python
        # Handle IPA file - upload, download, or use URL
        file_size = 0
        if ipa_file and allowed_file(ipa_file.filename):
            # Upload file
            filepath, file_size = self.save_ipa_file(ipa_file, bundle_id, version)
            if filepath:
                download_url = self.get_local_ipa_url(bundle_id, version, base_url)
        elif download_from_url and download_url:
            # Download from URL
            filepath, file_size = self.download_ipa_from_url(download_url, bundle_id, version)
            if filepath:
                download_url = self.get_local_ipa_url(bundle_id, version, base_url)

        # Handle icon file - upload, download, or use URL
        if icon_file and allowed_icon_file(icon_file.filename):
            # Upload icon file
            filepath = self.save_icon_file(icon_file, bundle_id)
            if filepath:
                icon_url = self.get_local_icon_url(bundle_id, base_url)
        elif download_icon_from_url and icon_url:
            # Download icon from URL
            filepath = self.download_icon_from_url(icon_url, bundle_id)
            if filepath:
                icon_url = self.get_local_icon_url(bundle_id, base_url)
```

`app.py:341` — and the return, which conflates the save result with a hardcoded success message:

```python
        return self.save_source(source_data), "App added successfully"
```

When `save_source` returns `False`, the route at `app.py:2353-2354` emits **HTTP 400 with `{"error": "App added successfully"}`**.

The same `if filepath:`-with-no-`else` shape also appears in `update_app` at `app.py:567-585`.

### Defect 2 — destructive ordering

`app.py:679-705` — the old file is removed *before* the replacement is fetched:

```python
        # Handle IPA file update - upload, download, or use URL
        if ipa_file and allowed_file(ipa_file.filename):
            # Upload new file - delete old one first
            old_filepath = self.get_ipa_path(bundle_identifier, version)
            if os.path.exists(old_filepath):
                try:
                    os.remove(old_filepath)
                except:
                    pass

            filepath, file_size = self.save_ipa_file(ipa_file, bundle_identifier, version)
            if filepath:
                version_obj['downloadURL'] = self.get_local_ipa_url(bundle_identifier, version, base_url)
                version_obj['size'] = file_size
        elif download_from_url and version_data.get('downloadURL'):
            # Download from URL - delete old one first
            old_filepath = self.get_ipa_path(bundle_identifier, version)
            if os.path.exists(old_filepath):
                try:
                    os.remove(old_filepath)
                except:
                    pass

            filepath, file_size = self.download_ipa_from_url(version_data['downloadURL'], bundle_identifier, version)
            if filepath:
                version_obj['downloadURL'] = self.get_local_ipa_url(bundle_identifier, version, base_url)
                version_obj['size'] = file_size
```

If the fetch fails: the file is gone, `filepath` is `None`, the `if filepath:` body is skipped, `version_obj['downloadURL']` still points at `/ipas/<bundle>/<version>.ipa`, and `app.py:717-718` returns success.

### Defect 3 — skipped content decoding

`app.py:93-109`:

```python
    def download_ipa_from_url(self, url, bundle_id, version):
        """Download IPA file from URL and save it locally"""
        try:
            logging.info(f"Downloading IPA from: {url}")
            response = requests.get(url, stream=True, timeout=300)
            response.raise_for_status()

            filepath = self.get_ipa_path(bundle_id, version)
            with open(filepath, 'wb') as f:
                shutil.copyfileobj(response.raw, f)
            ...
```

`response.raw` is the **undecoded** urllib3 stream. If the origin serves `Content-Encoding: gzip` — many CDNs and object stores do, negotiated per request — the `.ipa` written to disk is a gzip stream, not an archive. `get_file_size` records the compressed size and publishes it. No exception is raised. The identical pattern is at `app.py:182-183` for icons.

### Defect 4 — bare `except:` clauses

Six sites, confirmed by `grep -nE "^\s+except:\s*$" app.py`: **53** (`get_file_size`), **123** and **203** (directory cleanup), **379** (date parsing), **686** and **699** (the `os.remove` calls above).

`app.py:49-54` is the one that reaches the catalog:

```python
def get_file_size(filepath):
    """Get file size in bytes"""
    try:
        return os.path.getsize(filepath)
    except:
        return 0
```

A permission error, a missing file, and a genuinely empty file are indistinguishable — and that `0` is written straight into the version record at `app.py:320`.

### Two adjacent one-line bugs

- **`delete_icon_file` is never called.** It is defined at `app.py:191` and `grep -c "delete_icon_file" app.py` returns `1` — the definition only. `delete_app` (`app.py:518-525`) loops versions calling `delete_ipa_file` but never touches icons, so every deleted app leaves its icon behind forever. On disk today: `data/icons/com.ryan.anymex` belongs to no catalogued app.
- **`get_ipa_path` creates directories as a side effect of computing a path.** `app.py:72-79`:

```python
    def get_ipa_path(self, bundle_id, version):
        """Get the file path for an IPA file"""
        # Create subdirectory for bundle ID
        bundle_folder = os.path.join(IPA_FOLDER, secure_filename(bundle_id))
        os.makedirs(bundle_folder, exist_ok=True)
        filename = f"{secure_filename(version)}.ipa"
        return os.path.join(bundle_folder, filename)
```

  It is called from read-only paths (including `delete_ipa_file` and the `os.path.exists` checks above), which is why `data/ipas/` contains three empty directories. `get_icon_path` at `app.py:139-144` has the same problem.

### Defect 5 — `/api/add-app` leaks a raw `KeyError` as its error message

Found while writing the Plan 005 suite, and **verified by hand**:

```
$ curl -X POST .../api/add-app -H 'Content-Type: application/json' \
       -d '{"name":"X","developerName":"Y","version":"1.0"}'
400 {"error":"'bundleIdentifier'","success":false}
```

Unlike `/api/delete-app`, `/api/update-app`, `/api/add-version` and `/api/update-version` — which all begin with an explicit `if not bundle_id: return jsonify(...), 400` guard — `/api/add-app` (`app.py:2326-2358`) has no such check. A missing `bundleIdentifier` reaches `data['bundleIdentifier']` inside `add_app_manual` (`app.py:278`), raises `KeyError`, is caught by the route's outer `except Exception as e`, and `str(e)` — the bare repr `'bundleIdentifier'` — is handed to the client as the error message.

The status code happens to be right; the message is a Python internal. This is the same family of defect as the rest of this plan: the failure is real but the report is useless. Fix it with an explicit guard matching the four sibling routes, so the client gets `"Bundle identifier is required"`.

(The broader "`str(e)` returned to clients at 10 handlers" issue is a separate deferred finding — see `plans/README.md`. Fix only this one guard here; do not sweep the other handlers.)

**Repo conventions**: `(bool, message)` tuples from `SourceManager` methods; `logging.error(f"...: {str(e)}")`; messages are plain user-facing sentences. Keep all three.

## What the Plan 005 suite already pins

The suite (21 tests, `tests/test_routes.py`) is green today and asserts **current** behaviour, including bugs this plan fixes. Two tests carry markers, and they are **not** symmetrical — read both before changing either:

1. **`test_add_version_downloadurl_never_fetched_without_download_flag`** (marked `NOTE (plans/007)`) pins that a bogus `downloadURL` is stored verbatim, unvalidated, and reported as success when `downloadFromUrl` is not set.

   **This plan does not change that behaviour, and you must not make it.** Validating that a `downloadURL` resolves means making an outbound request to a caller-supplied URL — which is the deferred SSRF finding, explicitly out of scope below. This plan only adds `else` branches for fetches that were *attempted and failed*. So: **keep the assertion as-is** and reword its comment from a `plans/007` NOTE to a plain statement of intended behaviour, since 007 is no longer the plan that would change it. This test also doubles as the suite's guard that no test makes a real network call — do not weaken it.

2. **`test_delete_app_nonexistent_bundle_id_400`** mentions "Plan 007" only to record that `delete_app` is *correct* — it genuinely reports failure for an unknown bundle id. **Leave this test and its docstring alone.**

Consequently the old done-criterion `grep -c "Plan 007" tests/test_routes.py` → `0` is wrong twice over: it would be satisfied by editing the one test that should not change, and it misses the actual marker, which is spelled `plans/007`. The corrected criteria are in "Done criteria" below.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `.venv/bin/python -m pytest tests/ -q` | all pass |
| Syntax | `python3 -m py_compile app.py` | exit 0 |
| Bare excepts remaining | `grep -cE "^\s+except:\s*$" app.py` | `0` after Step 5 |
| Catalog validity | `python3 -c "import json; print(len(json.load(open('data/source.json'))['apps']))"` | `8` |

## Scope

**In scope**:
- `app.py` — `get_file_size` (49–54), `get_ipa_path` (72–79), `save_ipa_file` (81–91), `download_ipa_from_url` (93–109), `delete_ipa_file` (111–129), `get_icon_path` (139–144), `save_icon_file` (146–158), `download_icon_from_url` (160–189), `delete_icon_file` (191–210), `add_app_manual` (271–341), `delete_app` (499–527), `update_app` (540–588), `add_version` (590–648), `update_version` (650–718)
- `tests/test_routes.py`

**Out of scope** (do NOT touch):
- `save_source` / `load_source` and the locking — **Plan 006 owns those.** If 006 has already landed, leave its code alone.
- Any `@app.route` handler. The routes already translate `(False, message)` into HTTP 400 correctly; the bug is that they are handed `(True, "...")`.
- **Do not add URL validation, allowlisting, or SSRF protection** to `download_ipa_from_url`. That is a deliberately deferred finding (the deployment is on a private network). Adding it here expands the blast radius and mixes concerns.
- `HTML_TEMPLATE` and anything frontend.
- The case-sensitivity of bundle-ID directories (`com.michael-128.qBitControl` vs `com.Michael-128.qbitControl` both exist on disk). That is a **migration**, not a patch — it needs a rename step for existing data and is explicitly deferred. Do not add `.lower()` anywhere.

## Git workflow

- Branch: `advisor/007-fail-loudly`
- **One commit per defect** (five commits). These are logically independent and a reviewer will want to read them separately.

## Steps

### Step 1: Make helper failures distinguishable, and propagate them

Give every `if filepath:` in `add_app_manual` (283–306) and `update_app` (567–585) an `else` that returns a specific failure:

```python
        if ipa_file and allowed_file(ipa_file.filename):
            filepath, file_size = self.save_ipa_file(ipa_file, bundle_id, version)
            if filepath:
                download_url = self.get_local_ipa_url(bundle_id, version, base_url)
            else:
                return False, f"Failed to save uploaded IPA for {bundle_id} {version}"
        elif download_from_url and download_url:
            filepath, file_size = self.download_ipa_from_url(download_url, bundle_id, version)
            if filepath:
                download_url = self.get_local_ipa_url(bundle_id, version, base_url)
            else:
                return False, f"Failed to download IPA from {download_url}"
```

Apply the same treatment to the icon branches. Icon failures are less severe than IPA failures — an app with a missing icon is still installable — but they must still be reported, not swallowed.

Also fix `app.py:341` so a failed save does not return a success string as its error:

```python
        if self.save_source(source_data):
            return True, "App added successfully"
        return False, "Failed to save source data"
```

**Verify**: `python3 -m py_compile app.py` → exit 0, and `.venv/bin/python -m pytest tests/ -q` → **all 21 existing tests still pass.** None of them exercise a *failing* upload or download, so none should break here. If one does, trace it before continuing — see "What the Plan 005 suite already pins" above for the two marked tests and why neither should be inverted.

### Step 1b: Add the missing `bundleIdentifier` guard to `/api/add-app`

Defect 5. In the `/api/add-app` route (`app.py:2326-2358`), after the body is parsed into `data` and before `source_manager.add_app_manual(...)` is called, add the same guard the four sibling mutating routes already have:

```python
        if not data.get('bundleIdentifier'):
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400
```

Match the sibling routes' wording exactly — compare against `/api/delete-app` (`app.py:2361`) and copy its message string.

Note the route branches on `request.content_type`, so place the guard after both the multipart and JSON branches have populated `data`, not inside one of them.

**Verify**: add `test_add_app_missing_bundle_id_400` asserting status `400` and that the error message is `"Bundle identifier is required"` — specifically **not** `"'bundleIdentifier'"`, which is what it returns today.

### Step 2: Fix the destructive ordering in `update_version`

Restructure `app.py:679-705` (the block quoted under Defect 2) so the replacement is fully in place before the original is removed. Never delete first.

The shape:
1. Fetch or save the new file to a **temporary path** (e.g. `<final>.new`).
2. If that fails → return `(False, "...")`. **The original is untouched and still serving.**
3. On success, `os.replace(tmp, final)` — atomic, and it removes the old file as a side effect.
4. Only then update `version_obj['downloadURL']` and `version_obj['size']`.

This needs a way to write to a caller-chosen path. Either add an optional `dest_path` argument to `save_ipa_file` / `download_ipa_from_url`, or add a small private `_fetch_to_path` helper. Either is fine; pick one and use it consistently for both branches.

**Verify**: `grep -c "delete old one first" app.py` → `0` (those comments describe the removed behaviour and must go with it)

### Step 3: Decode downloaded content correctly

Replace both `shutil.copyfileobj(response.raw, f)` calls (`app.py:102` for IPAs and `app.py:183` for icons) with a chunked loop over `response.iter_content()`, which applies content decoding:

```python
            total = 0
            limit = app.config.get("MAX_CONTENT_LENGTH") or (2 * 1024 * 1024 * 1024)
            with open(tmp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        f.close()
                        os.remove(tmp_path)
                        raise ValueError(f"Download exceeded size limit of {limit} bytes")
                    f.write(chunk)
```

The size ceiling uses `MAX_CONTENT_LENGTH`, which Plan 001 wired from `.env`. Flask enforces that limit on *inbound uploads* but not on *outbound downloads* — this closes that half. `data/ipas/` is already 1.3 GB with no cap of any kind.

If `shutil` ends up unused in `app.py` afterwards, remove the import. Check first: `grep -c "shutil\." app.py`.

**Verify**: `grep -c "copyfileobj" app.py` → `0`

### Step 4: Call `delete_icon_file`, and stop `mkdir`-ing during path computation

Two small fixes:

1. In `delete_app` (`app.py:518-525`), call `self.delete_icon_file(bundle_identifier)` alongside the existing `delete_ipa_file` loop.
2. Remove the `os.makedirs(...)` side effect from `get_ipa_path` (`app.py:76`) and `get_icon_path` (`app.py:142`). Move directory creation into the functions that actually *write*: `save_ipa_file`, `download_ipa_from_url`, `save_icon_file`, `download_icon_from_url`. Those already call `os.makedirs(os.path.dirname(filepath), exist_ok=True)` in the icon cases (`app.py:152` and `app.py:180`); mirror that for the IPA cases. Note `ensure_data_directory` (`app.py:66-69`) still creates the four top-level directories at startup — leave that alone; it is the per-bundle subdirectory creation at `76` and `142` that is misplaced.

**Verify**: `grep -c "delete_icon_file" app.py` → `2` (definition + one call site)

### Step 5: Narrow the bare `except:` clauses

Replace all six with specific exception types and a log line:

- `get_file_size` (53): catch `OSError`, and return `None` rather than `0` so "unknown" is distinguishable from "empty". **Then check every caller.** There are **four** sites that write this value into a version record as `size`, not two: `app.py:320` (`add_app_manual`), `app.py:640` (`add_version`), and `app.py:692` and `705` (both in `update_version`). The last two are inside the block Step 2 restructures, so handle them there. Decide explicitly what a `None` size means (omitting the key, or keeping `0`, are both defensible — pick one and comment it). Do not let `None` reach the JSON silently.

  `get_file_size` itself is called from only two places — `app.py:86` (`save_ipa_file`) and `app.py:104` (`download_ipa_from_url`) — so the `None` originates in exactly two spots and fans out to the four writes above.
- Directory cleanup (123, 203): `except OSError as e:` + `logging.warning(...)`.
- Date parsing (379): `except (ValueError, TypeError) as e:` + `logging.warning(...)`. Note this branch silently rewrites an unparseable date to "now", which reorders versions for clients that sort by date — the warning makes it visible.
- The two `os.remove` calls (686, 699) disappear entirely in Step 2.

**Verify**: `grep -cE "^\s+except:\s*$" app.py` → `0`

### Step 6: End-to-end confirmation

**If you are working in a git worktree** — the usual case for an executor — `data/` and `.env` are gitignored and therefore **absent**, so `docker compose up` fails on the missing env file and there is no `data/source.json` to count. Do **not** improvise a `.env` or copy the real `data/` in. Instead:

- Run the pytest suite (which is the substantive check — tests 1, 2 and 5 below cover the behaviour change directly).
- Reproduce the failing-download case in-process rather than over HTTP, using the `client` fixture with an unreachable `downloadURL` and `downloadFromUrl` true.
- Then **state clearly in your report** that the Docker and live-`curl` checks were not run and why. The reviewer runs them against the real deployment.

**If you are in the main checkout**, run the full sequence:

```
.venv/bin/python -m pytest tests/ -q
docker compose up --build -d
sleep 15
curl -f http://localhost:7000/source.json | python3 -c "import json,sys; print(len(json.load(sys.stdin)['apps']))"
```
→ tests pass, `8` apps.

Then verify the headline behaviour change — a failing download must now report failure:
**Use `multipart/form-data`, not JSON.** The JSON branch of every mutating route hardcodes `download_from_url = False` (it is labelled "backward compatibility"), so a JSON body never attempts a download and can never exercise this path. An earlier version of this plan used a JSON repro here and it could not have passed regardless of the fix.

```
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:7000/api/add-app \
  -F 'name=Broken' -F 'bundleIdentifier=com.test.broken' -F 'developerName=T' \
  -F 'version=1.0' -F 'downloadURL=http://127.0.0.1:9/nope.ipa' -F 'downloadFromUrl=true'
```
→ HTTP 400 with a specific error message (**not** `"App added successfully"`), and `com.test.broken` must **not** appear in `/source.json`.

Confirmed during review against a container built from the merged branch:
```
HTTP 400
{"error":"Failed to download IPA from http://127.0.0.1:9/nope.ipa","success":false}
catalog: []
```

Clean up any test app you created via `/api/delete-app`.

## Test plan

Add to `tests/test_routes.py`:

1. **`test_failed_download_reports_error`** — point `downloadURL` at an unreachable address with `downloadFromUrl` true; assert 400 and that the error message is specific, not `"App added successfully"`. Assert the app is absent from the catalog.
2. **`test_update_version_preserves_original_on_failed_fetch`** — the critical one. Seed a real IPA file, call `update_version` with a download that fails, then assert **the original file still exists with its original bytes** and the route returned 400. This test fails before the change and passes after.
3. **`test_failed_save_does_not_return_success_message`** — patch `save_source` to return `False`; assert the error string is not `"App added successfully"`.
4. **`test_gzip_encoded_download_is_decoded`** — serve a gzip-encoded body from a local stub (`http.server` on a thread, or monkeypatch `requests.get`) and assert the bytes on disk are the *decoded* payload.
5. **`test_download_over_size_limit_is_rejected`** — set a small `MAX_CONTENT_LENGTH`, feed a larger body, assert failure and that no partial file remains.
6. **`test_delete_app_removes_icon`** — seed an icon, delete the app, assert the icon directory is gone.
7. **`test_get_ipa_path_does_not_create_directories`** — call `get_ipa_path` for a novel bundle id, assert no directory was created.
8. **`test_add_app_missing_bundle_id_400`** — Defect 5; assert the message is `"Bundle identifier is required"`, not the raw `KeyError` string.

Reuse the existing `client` fixture and its `tmp_path` rooting. Do not add a new fixture pattern, and do not let any new test make a real network call — see the STOP condition on the gzip test.

Also **reword the `NOTE (plans/007)` comment** on `test_add_version_downloadurl_never_fetched_without_download_flag` to a plain statement of intended behaviour. **Do not change its assertions**, and do not touch `test_delete_app_nonexistent_bundle_id_400` at all. Both are explained in "What the Plan 005 suite already pins" above.

Verification: `.venv/bin/python -m pytest tests/ -q` → all pass; 21 existing + 8 new = **29 tests**; `grep -c "plans/007" tests/test_routes.py` → `0`.

## Done criteria

ALL must hold:

- [ ] `python3 -m py_compile app.py` exits 0
- [ ] `grep -cE "^\s+except:\s*$" app.py` returns `0`
- [ ] `grep -c "copyfileobj" app.py` returns `0`
- [ ] `grep -c "delete_icon_file" app.py` returns `2`
- [ ] `grep -c "delete old one first" app.py` returns `0`
- [ ] `grep -c "plans/007" tests/test_routes.py` returns `0` (the marker is lowercase `plans/007`, not `Plan 007`)
- [ ] `grep -c "test_delete_app_nonexistent_bundle_id_400" tests/test_routes.py` returns `1` and that test is **unchanged** (`git diff` shows no edit to its body)
- [ ] `test_add_version_downloadurl_never_fetched_without_download_flag` still exists and its **assertions are unchanged** — only its comment was reworded
- [ ] `.venv/bin/python -m pytest tests/ -q` exits 0 with **29 tests** (21 existing + 8 new)
- [ ] `POST /api/add-app` with no `bundleIdentifier` returns 400 with `"Bundle identifier is required"`, not `"'bundleIdentifier'"`
- [ ] The live failing-download `curl` returns 400 with a specific message
- [ ] `data/source.json` still lists 8 apps and parses (this needs the real checkout — see the note under Step 6)
- [ ] `git status --short` shows only `app.py` and `tests/test_routes.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The `get_file_size` → `None` change (Step 5) turns out to reach any `size` write beyond the four already identified (`app.py:320`, `640`, `692`, `705`). Report the full list before proceeding — a `None` leaking into `source.json` as a version `size` would break clients.
- Restructuring `update_version` requires changing a route handler's signature. It should not; report.
- Any test in the Plan 005 suite fails for a reason you cannot trace to an intended behaviour change from this plan.
- You cannot construct the gzip test without a real network call. Skip that one test, mark it clearly, and report — do **not** add a real outbound request to the suite.
- You find yourself adding URL validation or an IP allowlist. Explicitly out of scope; report instead.
- `data/source.json` reports anything other than 8 apps.

## Maintenance notes

- **This plan changes the API contract**: requests that previously returned `{"success": true}` on a failed upload now return 400. The frontend's `fetch()` handlers already branch on the response, so they should surface the new errors correctly — but a reviewer should click through an add-app and an update-version in the UI, not just read the diff.
- The `"size": 0` already sitting in `data/source.json` for `com.michael-128.qBitControl` is **pre-existing corrupt data**. This plan stops new instances; it does not repair old ones. Fix that entry by hand afterwards — the real file is 4,321,496 bytes and lives under the differently-cased `data/ipas/com.Michael-128.qbitControl/`.
- Downloads still happen while holding Plan 006's lock. Step 2's temp-file restructuring makes it straightforward to move the download outside the lock later, if concurrent uploads ever matter.
- Two related findings remain deliberately open: **bundle-ID case normalisation** (needs a data migration) and **SSRF protection on the three outbound fetch sites** (deferred because the deployment is private — revisit immediately if it is ever exposed publicly).
- **Reviewer should scrutinise**: Step 2 above all. The correct ordering is fetch → verify → replace → update catalog. Any version of that code where an `os.remove` or a truncating open precedes a network call reintroduces permanent data loss.
