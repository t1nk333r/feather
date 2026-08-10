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
> sed -n '664,690p' app.py
> grep -c "delete_icon_file" app.py
> ```
> The excerpt must match "Current state / Defect 2" below, and the count must
> be `1` (definition only, no call sites). Confirm Plan 005 landed and its
> suite is green before changing anything.

## Status

- **Priority**: P1
- **Effort**: S–M (four related defects in one code region)
- **Risk**: LOW–MED — some requests that currently return 200 will start returning 400. That is the point, but it is user-visible.
- **Depends on**: `plans/005-smoke-test-suite.md` (mandatory)
- **Recommended after**: `plans/006-atomic-catalog-writes.md` — both touch `SourceManager`; doing 006 first avoids conflicts
- **Category**: bug
- **Planned at**: no VCS at authoring time — `app.py` md5 `2d17cee45698fa4f062cd9b4114e20d0`, 2026-08-10

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

Four distinct defects live in this one code region. The most dangerous is #2: `update_version` deletes the existing IPA **before** fetching its replacement, so a transient network error permanently destroys a hosted binary while the catalog keeps advertising it — and returns `{"success": true}`.

## Current state

### Defect 1 — silent success

`app.py:66-76` — the helper's failure mode:

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

`app.py:268-291` — the caller, with no `else` on either branch:

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

`app.py:326` — and the return, which conflates the save result with a hardcoded success message:

```python
        return self.save_source(source_data), "App added successfully"
```

When `save_source` returns `False`, the route at `app.py:2338-2339` emits **HTTP 400 with `{"error": "App added successfully"}`**.

The same `if filepath:`-with-no-`else` shape also appears in `update_app` at `app.py:552-570`.

### Defect 2 — destructive ordering

`app.py:664-690` — the old file is removed *before* the replacement is fetched:

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

If the fetch fails: the file is gone, `filepath` is `None`, the `if filepath:` body is skipped, `version_obj['downloadURL']` still points at `/ipas/<bundle>/<version>.ipa`, and `app.py:702-703` returns success.

### Defect 3 — skipped content decoding

`app.py:78-94`:

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

`response.raw` is the **undecoded** urllib3 stream. If the origin serves `Content-Encoding: gzip` — many CDNs and object stores do, negotiated per request — the `.ipa` written to disk is a gzip stream, not an archive. `get_file_size` records the compressed size and publishes it. No exception is raised. The identical pattern is at `app.py:167-168` for icons.

### Defect 4 — bare `except:` clauses

Six sites: `app.py:38` (`get_file_size`), `108` and `188` (directory cleanup), `364` (date parsing), `671` and `684` (the `os.remove` calls above).

`app.py:34-39` is the one that reaches the catalog:

```python
def get_file_size(filepath):
    """Get file size in bytes"""
    try:
        return os.path.getsize(filepath)
    except:
        return 0
```

A permission error, a missing file, and a genuinely empty file are indistinguishable — and that `0` is written straight into the version record at `app.py:305`.

### Two adjacent one-line bugs

- **`delete_icon_file` is never called.** It is defined at `app.py:176` and `grep -c "delete_icon_file" app.py` returns `1` — the definition only. `delete_app` (`app.py:503-510`) loops versions calling `delete_ipa_file` but never touches icons, so every deleted app leaves its icon behind forever. On disk today: `data/icons/com.ryan.anymex` belongs to no catalogued app.
- **`get_ipa_path` creates directories as a side effect of computing a path.** `app.py:57-64`:

```python
    def get_ipa_path(self, bundle_id, version):
        """Get the file path for an IPA file"""
        # Create subdirectory for bundle ID
        bundle_folder = os.path.join(IPA_FOLDER, secure_filename(bundle_id))
        os.makedirs(bundle_folder, exist_ok=True)
        filename = f"{secure_filename(version)}.ipa"
        return os.path.join(bundle_folder, filename)
```

  It is called from read-only paths (including `delete_ipa_file` and the `os.path.exists` checks above), which is why `data/ipas/` contains three empty directories. `get_icon_path` at `app.py:124-129` has the same problem.

**Repo conventions**: `(bool, message)` tuples from `SourceManager` methods; `logging.error(f"...: {str(e)}")`; messages are plain user-facing sentences. Keep all three.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `.venv/bin/python -m pytest tests/ -q` | all pass |
| Syntax | `python3 -m py_compile app.py` | exit 0 |
| Bare excepts remaining | `grep -cE "^\s+except:\s*$" app.py` | `0` after Step 5 |
| Catalog validity | `python3 -c "import json; print(len(json.load(open('data/source.json'))['apps']))"` | `8` |

## Scope

**In scope**:
- `app.py` — `get_file_size` (34–39), `get_ipa_path` (57–64), `save_ipa_file` (66–76), `download_ipa_from_url` (78–94), `delete_ipa_file` (96–114), `get_icon_path` (124–129), `save_icon_file` (131–143), `download_icon_from_url` (145–174), `delete_icon_file` (176–195), `add_app_manual` (256–326), `delete_app` (484–512), `update_app` (525–573), `add_version` (575–633), `update_version` (635–703)
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

Give every `if filepath:` in `add_app_manual` (268–291) and `update_app` (552–570) an `else` that returns a specific failure:

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

Also fix `app.py:326` so a failed save does not return a success string as its error:

```python
        if self.save_source(source_data):
            return True, "App added successfully"
        return False, "Failed to save source data"
```

**Verify**: `python3 -m py_compile app.py` → exit 0, and `.venv/bin/python -m pytest tests/ -q` → the Plan 005 tests carrying `NOTE: ... Plan 007` comments now fail. **That is expected** — update those assertions to the new correct behaviour as part of this step.

### Step 2: Fix the destructive ordering in `update_version`

Restructure `app.py:664-690` so the replacement is fully in place before the original is removed. Never delete first.

The shape:
1. Fetch or save the new file to a **temporary path** (e.g. `<final>.new`).
2. If that fails → return `(False, "...")`. **The original is untouched and still serving.**
3. On success, `os.replace(tmp, final)` — atomic, and it removes the old file as a side effect.
4. Only then update `version_obj['downloadURL']` and `version_obj['size']`.

This needs a way to write to a caller-chosen path. Either add an optional `dest_path` argument to `save_ipa_file` / `download_ipa_from_url`, or add a small private `_fetch_to_path` helper. Either is fine; pick one and use it consistently for both branches.

**Verify**: `grep -c "delete old one first" app.py` → `0` (those comments describe the removed behaviour and must go with it)

### Step 3: Decode downloaded content correctly

Replace both `shutil.copyfileobj(response.raw, f)` calls (`app.py:87` and `app.py:168`) with a chunked loop over `response.iter_content()`, which applies content decoding:

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

1. In `delete_app` (`app.py:503-510`), call `self.delete_icon_file(bundle_identifier)` alongside the existing `delete_ipa_file` loop.
2. Remove the `os.makedirs(...)` side effect from `get_ipa_path` (`app.py:61`) and `get_icon_path` (`app.py:127`). Move directory creation into the functions that actually *write*: `save_ipa_file`, `download_ipa_from_url`, `save_icon_file`, `download_icon_from_url`. Those already call `os.makedirs(os.path.dirname(filepath), exist_ok=True)` in the icon cases (`app.py:137`, `165`); mirror that for the IPA cases.

**Verify**: `grep -c "delete_icon_file" app.py` → `2` (definition + one call site)

### Step 5: Narrow the bare `except:` clauses

Replace all six with specific exception types and a log line:

- `get_file_size` (38): catch `OSError`, and return `None` rather than `0` so "unknown" is distinguishable from "empty". **Then check every caller** — `app.py:305` and `app.py:625` write this value into the version record as `size`. Decide explicitly what a `None` size means there (omitting the key, or keeping `0`, are both defensible — pick one and comment it). Do not let `None` reach the JSON silently.
- Directory cleanup (108, 188): `except OSError as e:` + `logging.warning(...)`.
- Date parsing (364): `except (ValueError, TypeError) as e:` + `logging.warning(...)`. Note this branch silently rewrites an unparseable date to "now", which reorders versions for clients that sort by date — the warning makes it visible.
- The two `os.remove` calls (671, 684) disappear entirely in Step 2.

**Verify**: `grep -cE "^\s+except:\s*$" app.py` → `0`

### Step 6: End-to-end confirmation

```
.venv/bin/python -m pytest tests/ -q
docker compose up --build -d
sleep 15
curl -f http://localhost:7000/source.json | python3 -c "import json,sys; print(len(json.load(sys.stdin)['apps']))"
```
→ tests pass, `8` apps.

Then verify the headline behaviour change — a failing download must now report failure:
```
curl -s -X POST http://localhost:7000/api/add-app \
  -H 'Content-Type: application/json' \
  -d '{"name":"Broken","bundleIdentifier":"com.test.broken","developerName":"T","version":"1.0","downloadURL":"http://127.0.0.1:9/nope.ipa","downloadFromUrl":"true"}'
```
→ HTTP 400 with a specific error message (**not** `"App added successfully"`), and `com.test.broken` must **not** appear in `/source.json`.

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

Also **update the Plan 005 assertions carrying `NOTE: ... Plan 007` comments** — they currently assert the buggy behaviour and must be inverted. Removing those NOTE comments is part of finishing this plan.

Verification: `.venv/bin/python -m pytest tests/ -q` → all pass, 7 new tests, zero remaining `Plan 007` NOTE comments.

## Done criteria

ALL must hold:

- [ ] `python3 -m py_compile app.py` exits 0
- [ ] `grep -cE "^\s+except:\s*$" app.py` returns `0`
- [ ] `grep -c "copyfileobj" app.py` returns `0`
- [ ] `grep -c "delete_icon_file" app.py` returns `2`
- [ ] `grep -c "delete old one first" app.py` returns `0`
- [ ] `grep -c "Plan 007" tests/test_routes.py` returns `0`
- [ ] `.venv/bin/python -m pytest tests/ -q` exits 0 with 7 new tests
- [ ] The live failing-download `curl` returns 400 with a specific message
- [ ] `data/source.json` still lists 8 apps and parses
- [ ] `git status --short` shows only `app.py` and `tests/test_routes.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The `get_file_size` → `None` change (Step 5) turns out to reach more than the two call sites at `app.py:305` and `625`. Report the full list before proceeding — a `None` leaking into `source.json` as a version `size` would break clients.
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
