# Plan 050: Storage self-test — a write→read→delete round-trip that distinguishes 403 (permission) from 404 (missing)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat c62e7da..HEAD -- app.py templates/index.html tests/
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 222 passed
> ```
> If the storage classes changed since `c62e7da`, compare the "Current state"
> excerpts below against the live code first; on a semantic mismatch, STOP.

## Status

- **Priority**: P2 (directly fixes a real diagnostic blind spot that cost hours)
- **Effort**: M
- **Risk**: LOW (a new read-only-ish diagnostic; the probe object it writes is deleted)
- **Depends on**: plan 011/028 (the Garage storage classes) — merged
- **Category**: direction / DX / observability
- **Planned at**: commit `c62e7da`, 2026-08-19

## Why this matters

When Garage icon uploads failed, the symptom was misleading: `exists()` returns
`False` on a **403** exactly as it does on a **404**, so a *permission* problem
looked like a *missing icon*, and there was no way to ask the app "can I actually
read **and** write the storage backend right now?" — diagnosing it required SSH +
`docker logs`. This plan adds a **storage self-test**: a one-click
write→read→delete round-trip against the active backend that reports each
capability and **names a 403 as "permission denied," distinct from success or a
real 404**. It turns a multi-hour log hunt into a single button.

## Current state — `app.py`

- Storage instances (app.py:1378):
  ```python
  if STORAGE_BACKEND == "garage":
      ipa_storage = GarageIpaStorage(); icon_storage = GarageIconStorage()
  else:
      ipa_storage = LocalIpaStorage(); icon_storage = LocalIconStorage()
  ```
- `GarageIconStorage` (class at ~492) and `GarageIpaStorage` (~345) each hold
  `self._client` (a boto3 S3 client) and use the module-level `GARAGE_BUCKET`;
  `self._key(...)` builds an object key. Their `exists()` swallows a 403:
  ```python
  # GarageIconStorage.exists (app.py:589)
  def exists(self, bundle_id, ext):
      key = self._key(bundle_id, ext)
      try:
          self._client.head_object(Bucket=GARAGE_BUCKET, Key=key); return True
      except ClientError as e:
          status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
          code = e.response.get("Error", {}).get("Code", "unknown")
          if status == 404 or code in ("404", "NoSuchKey"): return False
          logging.error(f"Error checking icon existence ({key}): {code}"); return False
  ```
- `LocalIconStorage`/`LocalIpaStorage` store on disk under `ICON_FOLDER`/the IPA folder.
- The Health tab (`templates/index.html:902`, `id="health"`) already has a Scan
  button (`runHealthScan()`, JS ~1315) and a Reconcile Icons section — the
  self-test control goes here, same patterns (`showToast`, `setButtonLoading`).
- Auth: `@requires_auth`. `ClientError` is imported (`from botocore.exceptions import ClientError`).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `222 passed` before |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| New tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_storage.py -q -k selftest` | pass |
| Full | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `222 + N passed` |

## Scope

**In scope:**
- `app.py` — a `selftest()` method on each of the four storage classes, and a
  `POST /api/storage-selftest` route (`@requires_auth`) that runs it for both
  `icon_storage` and `ipa_storage` and returns a per-capability report.
- `templates/index.html` — a "Storage self-test" control on the Health tab.
- `tests/test_storage.py` — tests (reuse the `garage_client` / `FakeS3Client` fixture).

**Out of scope:**
- Changing `exists()`'s bool contract (038/040 depend on it) — the self-test is the
  new signal; you MAY additionally enrich the health scan's `missing-icon` detail
  to say "unverifiable — storage returned 403" when a 403 was observed, but do not
  change `exists()`'s return type.
- Deleting/altering any real catalog object — the self-test only touches its own
  probe key (and must delete it).

## Steps

### Step 1: Baseline → `222 passed`. If not, STOP.

### Step 2: `selftest()` on the storage classes
Add a `selftest(self)` method to each class returning a dict:
`{"backend": "garage"|"local", "write": <status>, "read": <status>, "delete": <status>, "detail": <str|None>}`
where each `<status>` is `"ok"`, `"forbidden"` (a 403), or `"error"`.

- **Garage classes** — use a probe key under a dedicated prefix, e.g.
  `self._key("__selftest__", "probe")` (or a literal `"selftest/feather-probe"` on
  the client), and via `self._client`:
  1. `put_object(Bucket=GARAGE_BUCKET, Key=probe, Body=b"feather-selftest")` → `write`.
  2. `head_object(...)` (and optionally `get_object`) → `read`.
  3. `delete_object(...)` → `delete`.
  Wrap each in `try/except ClientError`; map an HTTP 403 / `AccessDenied` /
  `Forbidden` to `"forbidden"`, other `ClientError`/`Exception` to `"error"`, and
  put the S3 error code in `detail`. Always attempt the delete in a `finally` so a
  successful write is cleaned up even if read fails. Never raise.
- **Local classes** — write a temp file under the storage folder, read it back,
  delete it; all `"ok"` unless an `OSError` occurs (then `"error"` with the errno in
  `detail`). This confirms the mount is writable.

### Step 3: `POST /api/storage-selftest`
```python
@app.route('/api/storage-selftest', methods=['POST'])
@requires_auth
def storage_selftest():
    try:
        return jsonify({"icon": icon_storage.selftest(), "ipa": ipa_storage.selftest()})
    except Exception as e:
        logging.error(f"Storage self-test error: {str(e)}")
        return jsonify({"error": "self-test failed"}), 500
```
(The `selftest()` methods never raise, so the 500 path is a belt-and-suspenders.)

### Step 4: Health-tab control
Add a "Storage self-test" button + result area to the Health tab (near the Scan /
Reconcile controls). JS `runStorageSelftest()` POSTs, then renders the icon/ipa
capability grid — **make a `"forbidden"` result visually distinct** (e.g. the word
"PERMISSION DENIED (403)") so a read-vs-write permission gap is unmistakable. Plain
text, no emoji (plan 025); iOS tokens (plan 043).

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 5: Tests (`tests/test_storage.py`, `garage_client` fixture / `FakeS3Client`)
1. `test_selftest_garage_all_ok` — fake client accepts put/head/delete → route returns `icon.write/read/delete == "ok"`, and the probe key is NOT left in `fake_client.objects` afterward.
2. `test_selftest_reports_403_read_as_forbidden` — make `FakeS3Client.head_object` raise a `ClientError` with HTTP 403 (mirror the real bug) → the report shows `read == "forbidden"` while `write == "ok"`. (This is the exact scenario that confused everyone — the test pins it.)
3. `test_selftest_local_backend_ok` — local backend → all `"ok"`; no probe file left behind.
4. `test_selftest_requires_auth` — unauthenticated → 401.

You may need to extend `FakeS3Client` with `put_object`/`get_object`/`delete_object`
if it only implements the subset used elsewhere — add them minimally, matching its
existing style, and raise a `ClientError` on the simulated-403 path.

**Prove discrimination**: temporarily make the Garage `selftest` map 403 → `"ok"`
→ test 2 fails. Restore.

**Verify**: focused `-k selftest`; then full suite.

## Done criteria
- [ ] `POST /api/storage-selftest` (`@requires_auth`) runs a write→read→delete round-trip on both `icon_storage` and `ipa_storage` and reports each capability, mapping a 403 to `"forbidden"` distinctly from `"ok"`/`"error"`; the probe object is always cleaned up.
- [ ] The Health tab surfaces it, with `forbidden` visually unmistakable.
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (222 + 4).
- [ ] `git status --short` shows only `app.py`, `templates/index.html`, `tests/test_storage.py`.

## STOP conditions
- `FakeS3Client` can't be extended to simulate a 403 head without a real network call — report rather than reaching the network.
- A `selftest` probe would risk colliding with a real object key — use a clearly namespaced probe prefix (`__selftest__`/`selftest/`) that no real bundle id produces.

## Maintenance notes
- This is the tool that would have diagnosed the 2026-08-19 "403 read-denied"
  incident in one click. Keep the `forbidden` labeling — that distinction is the
  whole point.
- If a later plan changes `exists()` to raise on 403 instead of returning False,
  update the health scan (038) accordingly; the self-test stays independent.
- Reviewer: confirm the probe is always deleted (even when read fails), 403 is
  labeled distinctly, and nothing touches real catalog objects.
