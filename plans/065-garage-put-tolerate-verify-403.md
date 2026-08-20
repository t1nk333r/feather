# Plan 065: Garage `put()` must not fail an upload when the verify-read is 403

> Backend only, `app.py` (`GarageIpaStorage.put` ~457, `GarageIconStorage.put` ~719).
> Adds tests. Drift check: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 236 passed at commit `1da169d`.

## Status
- Priority P1 (blocks all icon imports on a write-only Garage key). Effort S. Risk MED. Planned at `1da169d`.

## Why (root cause — observed in production)
On the live instance, importing an app fails with:
```
Failed to save uploaded icon for com.ryan.anymex
```
and the log shows:
```
ERROR · Uploaded icon but could not verify it (icons/com.ryan.anymex/icon.png): 403
```
`GarageIconStorage.put` (app.py ~719) uploads the object successfully via
`upload_fileobj` (no error), then calls `head_object` to verify the stored size
(added by plan 028). The app's Garage key has **write but not read/head**, so the
verify `head_object` returns **403**, the `except ClientError` branch logs "could not
verify" and returns **False** — so `save_icon_file` returns None and the route reports
"Failed to save uploaded icon", **even though the object was actually written.**

`GarageIpaStorage.put` (app.py ~457) has the identical pattern: on a verify 403 it
returns `None` (failure).

A 403 on the **verify** step means "I can't read it back to confirm," NOT "the write
failed" — the upload call already succeeded. Conflating the two makes every upload
fail on a write-only key. The verification (plan 028) should stay when reads ARE
allowed (it catches truncation / size mismatch), but a permission-denied verify must
degrade to success-with-warning, not failure.

`_classify_storage_error(e)` (app.py ~301) already returns `("forbidden", code)` for a
403 (by HTTP status or `AccessDenied`/`Forbidden` code) — reuse it.

## Scope
- **In scope:** the post-upload verify blocks of `GarageIconStorage.put` and
  `GarageIpaStorage.put`. New tests.
- **Out of scope:** `exists()`, `delete()`, `selftest()`, `_classify_storage_error`,
  local storage classes, the import routes, any Garage grant/config (that's the
  operator's separate `garage bucket allow --read` fix).

## Current state — `app.py`

`GarageIconStorage.put` verify block (~742-754), which precomputes `expected_size`:
```python
        try:
            head = self._client.head_object(Bucket=GARAGE_BUCKET, Key=key)
            actual_size = head.get("ContentLength")
            if actual_size != expected_size:
                logging.error(
                    f"Garage icon size mismatch ({key}): expected {expected_size}, got {actual_size}"
                )
                return False
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "unknown")
            logging.error(f"Uploaded icon but could not verify it ({key}): {code}")
            return False
```

`GarageIpaStorage.put` verify block (~481-487), which does NOT precompute a size:
```python
        try:
            head = self._client.head_object(Bucket=GARAGE_BUCKET, Key=key)
            return head.get("ContentLength")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "unknown")
            logging.error(f"Uploaded IPA but could not verify it ({key}): {code}")
            return None
```

## Step 1 — icon `put`: on a forbidden verify, treat the upload as succeeded
Replace the icon `except ClientError` block (~751-754) with:
```python
        except ClientError as e:
            status, code = _classify_storage_error(e)
            if status == "forbidden":
                # The upload above already succeeded; we simply lack read/head
                # permission to verify size. Do NOT fail the write on a
                # permission-denied verify (that breaks a write-only key).
                logging.warning(
                    f"Uploaded icon but cannot verify it ({key}): {code} -- "
                    f"treating upload as successful (grant the key read to re-enable verification)"
                )
                return True
            logging.error(f"Uploaded icon but could not verify it ({key}): {code}")
            return False
```
(Keep the size-mismatch `return False` in the `try` unchanged — that path only runs
when the head SUCCEEDS, i.e. reads are allowed.)

## Step 2 — IPA `put`: precompute an expected size, and on a forbidden verify return it
The IPA caller uses the returned value as the file size for `source.json`. Precompute
it before the upload (so a forbidden verify can still return a correct size), and
tolerate a 403 on verify.

Add, at the very top of `GarageIpaStorage.put` (right after `extra_args = {...}`, ~467):
```python
        try:
            expected_size = self._remaining_size(src)
        except OSError:
            expected_size = None
```
Then replace the IPA verify block (~481-487) with:
```python
        try:
            head = self._client.head_object(Bucket=GARAGE_BUCKET, Key=key)
            return head.get("ContentLength")
        except ClientError as e:
            status, code = _classify_storage_error(e)
            if status == "forbidden" and expected_size is not None:
                logging.warning(
                    f"Uploaded IPA but cannot verify it ({key}): {code} -- "
                    f"treating upload as successful (size {expected_size} from source; "
                    f"grant the key read to re-enable verification)"
                )
                return expected_size
            logging.error(f"Uploaded IPA but could not verify it ({key}): {code}")
            return None
```
(Confirm `_remaining_size` is a method on this class / shared — the icon `put` calls
`self._remaining_size(src)`, so it is available. If `_remaining_size` lives only on the
icon class, hoist it or duplicate the tiny helper; do NOT change its behavior.)

## Step 3 — tests (`tests/test_storage.py`)
Follow the existing Garage tests in `tests/test_storage.py` (they stub the boto3
client). Add tests asserting that a **403 on the verify `head_object`** — with a
**successful upload** — yields success, not failure:

- `test_garage_icon_put_succeeds_when_verify_forbidden`: stub the client so
  `upload_fileobj` succeeds and `head_object` raises a `ClientError` with
  `Error.Code = "AccessDenied"` / HTTP 403; assert `GarageIconStorage.put(...)` returns
  a truthy ext/`True` and does NOT raise.
- `test_garage_icon_put_still_fails_on_size_mismatch`: `head_object` returns a
  `ContentLength` different from the uploaded bytes; assert `put` returns `False`
  (verification still works when reads ARE allowed).
- `test_garage_ipa_put_returns_size_when_verify_forbidden`: `upload_fileobj` succeeds,
  `head_object` 403s; assert `put` returns the expected (precomputed) size, not `None`.

Model the `ClientError` construction on how the existing storage tests build one (look
for `ClientError(` or `botocore.exceptions` in `tests/test_storage.py`); reuse that
helper/pattern. If none exists, build it as
`ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "HeadObject")`.

## Verify
```bash
grep -c 'status == "forbidden"' app.py                         # >= 2 (both put methods)
grep -c "treating upload as successful" app.py                 # 2
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_storage.py -q
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q          # 239 passed (236 + 3 new)
```

## Done criteria
- [ ] A 403 on the post-upload verify `head_object` (after a successful upload) makes both `put`s report success (icon → `True`, IPA → the precomputed size), with a WARNING log — not a failure.
- [ ] A genuine size mismatch (verify head SUCCEEDS, sizes differ) still returns `False` for icons.
- [ ] Non-forbidden verify errors still fail as before.
- [ ] `git status --short` shows only `app.py`, `tests/test_storage.py`; suite `239 passed`.

## STOP conditions
- If `_remaining_size` is NOT available to `GarageIpaStorage` and hoisting it is
  non-trivial, STOP and report (do not silently change how sizes are computed).
- If an existing storage test asserts that a verify-403 returns failure, that test
  encodes the old (buggy) behavior — update it to the new contract and note it.

## Maintenance note
- This makes upload verification best-effort: strict when the key can read, skipped
  (with a warning) when it cannot. The operator's real fix is still to grant the
  Garage key read/ListBucket (`garage bucket allow --read --key <key> <bucket>`), which
  re-enables full verification; this plan just stops a write-only key from breaking
  every upload.
- Pairs with plan 064 (icon-on-import-of-existing-app): together they make
  re-importing an app set its icon even on the current Garage permissions.
