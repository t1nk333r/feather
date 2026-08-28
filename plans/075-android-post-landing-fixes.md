# Plan 075: Fix the six defects found in the Android/F-Droid code after plan 073 landed

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> ```
> cd <repo root>
> git rev-parse --short HEAD                        # plan written at 5e8f447
> git diff --stat 5e8f447..HEAD -- app.py templates/index.html scripts/fdroid_index_loop.sh .env.example README.md tests/test_android.py
> ```
> If any changed, compare the "Current state" excerpts against the live code
> before proceeding; on a mismatch, STOP.

## Status

- **Priority**: P1 — (a) destroys another app's binaries; (b) can publish a corrupt, signed APK with no API path to repair it
- **Effort**: S–M
- **Risk**: LOW — every change narrows or reorders behaviour inside code that landed two days ago; the iOS path is untouched
- **Depends on**: none (073 is DONE). Plan 076 (sidecar hardening) is independent and can land in either order.
- **Category**: bug
- **Planned at**: commit `5e8f447`, 2026-08-28

## Why this matters

Plan 073 added an Android/F-Droid repository: feather stores `repo/<package>_<versionCode>.apk` + `metadata/<package>.yml` under `data/fdroid/`, touches `.update-requested`, and a sidecar runs `fdroid update` to build and sign the index that F-Droid clients download. A post-landing audit found six defects. Two are serious:

- **(a) `delete_app` deletes the wrong app's files.** It matches `filename.startswith(f"{package}_")`. Android package names may contain `_` in any segment, so deleting `com.example.app` also removes every APK of `com.example.app_beta`. The victim's metadata survives, so the next index build lists an app with no packages. The confirm dialog says "cannot be undone" and it means it.
- **(b) The cross-device fallback publishes a truncated file.** `compose.yml` bind-mounts `./data/fdroid` separately from `./data`, so `os.replace()` from `uploads/` into `repo/` raises `EXDEV` on the real deployment and the code falls back to `shutil.copy2(src, dest)` — straight onto the final, sidecar-scanned name. If the copy fails (disk full, restart), the partial file stays; `add_apk` then reports `"already present"` forever, and the sidecar signs the corrupt APK into the index.

The other four: **(c)** `add-apk` moves the APK and requests a rebuild *before* validating the submitted metadata, so an over-long `Summary` yields HTTP 400 after the app is already published (and the retry says "already present — success"); **(d)** `update-app` copies any JSON value type into the YAML the sidecar parses; **(e)** `notify("android_add_apk", …)` can never fire because the event isn't in the default `TELEGRAM_NOTIFY_EVENTS` set and is documented nowhere; **(f)** when `PUBLIC_BASE_URL` is unset, the sidecar bakes `http://localhost:7000/fdroid/repo` into the *signed* index as the repo address, and `_ensure_repo_config_has_url` never refreshes an existing `repo-config.json` when the base URL later changes.

## Current state

`app.py:99` — package regex (note `_` is allowed):
```python
ANDROID_PACKAGE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$')
```

`app.py:1713-1719` — filename convention:
```python
    @staticmethod
    def apk_filename(package, version_code):
        return f"{package}_{int(version_code)}.apk"
```

`app.py:1767` — `list_apps` parses filenames with `apk_re = re.compile(r'^(.+)_(\d+)\.apk$')` (greedy: attributes `com.example.app_beta_5.apk` to `com.example.app_beta` — correct).

`app.py:2012-2032` — **(a)** the prefix delete:
```python
    def delete_app(self, package):
        if not ANDROID_PACKAGE_RE.match(package or ""):
            raise ValueError(f"Invalid package name: {package!r}")
        removed_something = False
        with self._lock:
            if os.path.isdir(FDROID_REPO_DIR):
                prefix = f"{package}_"
                for fn in os.listdir(FDROID_REPO_DIR):
                    if fn.startswith(prefix) and fn.endswith(".apk"):
                        try:
                            os.remove(os.path.join(FDROID_REPO_DIR, fn))
                            removed_something = True
                        except OSError:
                            pass
```

`app.py:1965-1998` — **(b)** the `EXDEV` fallback inside `add_apk`:
```python
        dest = self.apk_path(package, info["version_code"])
        with self._lock:
            if os.path.exists(dest):
                return False, "already present"
            os.makedirs(FDROID_REPO_DIR, exist_ok=True)
            try:
                os.replace(src_path, dest)
            except OSError as e:
                # ... comment about compose bind mounts ...
                if e.errno != errno.EXDEV:
                    raise
                shutil.copy2(src_path, dest)
                os.remove(src_path)

        if not os.path.exists(self.metadata_path(package)):
            self.write_metadata(package, {"Name": info["app_name"]})
        self._ensure_repo_config_has_url()
        self.request_update()
        return True, info
```

`app.py:1919-1957` — `write_metadata(package, fields)`: enforces `_LIMITS = {"Name": 50, "Summary": 80, "Description": 4000}` via `len(val) > limit`, special-cases `Categories`, then `yaml.safe_dump(...)` + `os.replace`. No type check on values.

`app.py:3717-3725` — **(d)**:
```python
def _android_metadata_fields_from(source):
    fields = {}
    for api_key, meta_key in ANDROID_METADATA_FIELD_MAP.items():
        val = source.get(api_key)
        if val:
            fields[meta_key] = val
    return fields
```

`app.py:3846-3862` — **(c)** order inside `android_add_apk`:
```python
        success, result = android_repo.add_apk(temp_path, expected_package=package_hint)

        if not success:
            return jsonify({"success": True, "message": result})

        fields = _android_metadata_fields_from(request.form)
        if fields:
            android_repo.write_metadata(result["package"], fields)

        notify("android_add_apk", f"New Android APK published: {result['package']} {result['version_name']}")
        return jsonify({"success": True, "message": f"Added {result['package']} version {result['version_code']}", "package": ..., "versionCode": ..., "pending": True})
```

`app.py:132-137` — **(e)** default event set:
```python
TELEGRAM_NOTIFY_EVENTS = set(
    event.strip()
    for event in os.environ.get("TELEGRAM_NOTIFY_EVENTS", "add_app,add_version,delete_app").split(",")
    if event.strip()
)
```
`.env.example:60-67` documents the variable and the default string; `README.md` describes the feature without listing event names. The iOS `delete_version` route (`app.py:3363`) fires `notify("delete_version", …)`, also not in the default — leave it; only the Android event is in scope.

`app.py:1905-1917` — **(f)**:
```python
    def _ensure_repo_config_has_url(self):
        if os.path.exists(FDROID_REPO_CONFIG):
            return
        base = _safe_base_url()
        if not base:
            return
        cfg = {"name": "Feather Android", "description": "", "repo_url": base.rstrip('/') + '/fdroid/repo'}
        ...
```
`scripts/fdroid_index_loop.sh:36` — the sidecar's fallback chain: `cfg.get('repo_url') or os.environ.get('FDROID_REPO_URL') or 'http://localhost:7000/fdroid/repo'`.

`templates/index.html:1381-1405` — the `addApkForm` submit handler branches only on `result.success` and shows `result.message` as a success toast.

`tests/test_android.py` — fixtures: imports `client`, `authed_client` from `tests/test_routes.py`; `FAKE` dict + `fake_inspect` (lines 22-36) monkeypatched over `_inspect_apk`; helper pattern for uploading `b"fake-apk-bytes"` as `demo.apk` in `test_add_apk_upload_writes_apk_metadata_and_marker` (line 116). Model every new test on that file.

Conventions: `ValueError` messages are operator-facing and become HTTP 400 bodies; generic `Exception` → `logging.error` + 400/500; every mutation ends with `self.request_update()`; no emoji in `index.html` (regression guard in `tests/test_routes.py`).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| venv | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt` | exit 0 |
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | `259 passed, 1 skipped` |
| Focused | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_android.py -q -p no:cacheprovider` | all pass |

## Scope

**In scope**: `app.py` (Android region only: `AndroidRepoManager`, `_android_metadata_fields_from`, `android_add_apk`, the `TELEGRAM_NOTIFY_EVENTS` default), `templates/index.html` (the `addApkForm` handler only), `.env.example` (the notify comment), `README.md` (one sentence listing event names), `tests/test_android.py`.

**Out of scope — do NOT touch**: `scripts/fdroid_index_loop.sh` (plan 076 owns it); `SourceManager` and every iOS route; `_inspect_apk`; `notify()` itself; the iOS `delete_version` event; `compose.yml`.

## Git workflow

- Branch: `advisor/075-android-post-landing-fixes`; one commit per lettered defect, e.g. `fix(android): delete_app matches package exactly, not by prefix (plan 075)`.
- Do not push.

## Steps

### Step 1 — (a) exact-match deletion

In `delete_app`, replace the `startswith(prefix)` test with the same regex `list_apps` uses and compare the captured package exactly:

```python
                apk_re = re.compile(r'^(.+)_(\d+)\.apk$')
                for fn in os.listdir(FDROID_REPO_DIR):
                    m = apk_re.match(fn)
                    if not m or m.group(1) != package:
                        continue
```
Hoist that regex to a module constant `ANDROID_APK_FILENAME_RE` and use it in both `list_apps` and `delete_app` so they cannot disagree again.

**Test** `test_delete_app_does_not_touch_prefix_sibling`: create `repo/com.example.app_1.apk` and `repo/com.example.app_beta_5.apk` plus both yml files; `POST /api/android/delete-app {"package": "com.example.app"}` → 200; the `_beta_5` APK and yml still exist; the `_1` APK and `com.example.app.yml` are gone.

**Verify**: focused suite passes; the new test fails if you temporarily restore `startswith` (do it, run, revert, report both).

### Step 2 — (b) atomic cross-device publish

In the `EXDEV` branch, copy to a temp name that the sidecar's `*.apk` scan cannot match, then rename:

```python
                tmp_dest = os.path.join(FDROID_REPO_DIR, f".{os.path.basename(dest)}.part")
                try:
                    shutil.copy2(src_path, tmp_dest)
                    with open(tmp_dest, 'rb') as f:
                        os.fsync(f.fileno())
                    os.replace(tmp_dest, dest)
                finally:
                    if os.path.exists(tmp_dest):
                        try:
                            os.remove(tmp_dest)
                        except OSError:
                            pass
                os.remove(src_path)
```
Also make `list_apps`'s filename regex ignore dotfiles (it already requires `^(.+)_(\d+)\.apk$` — a leading `.`+`.part` suffix will not match; confirm with the test below).

**Test** `test_add_apk_exdev_fallback_is_atomic`: monkeypatch `app_module.os.replace` so the *first* call raises `OSError(errno.EXDEV, "cross-device")` and subsequent calls pass through; upload → 200; the final APK exists with the right bytes; no `.part` file remains. Second test `test_add_apk_exdev_copy_failure_leaves_no_partial`: monkeypatch `shutil.copy2` to write half the bytes then raise `OSError`; upload → 400; **neither** `dest` nor any `.part` exists; a retry after restoring `copy2` succeeds (not "already present").

**Verify**: both tests pass; the second fails against the current code (report).

### Step 3 — (c) validate metadata before mutating, and give "already present" its own shape

In `android_add_apk`:
1. Compute `fields = _android_metadata_fields_from(request.form)` **before** `add_apk`, and validate them by calling a new `AndroidRepoManager.validate_metadata(fields)` (extract the limit/type checks from `write_metadata` into it; `write_metadata` calls it too). A `ValueError` here returns 400 with nothing written.
2. Change the not-added branch to `return jsonify({"success": True, "added": False, "message": "Already present: <package> versionCode <n>", "package": ..., "versionCode": ...})` — `add_apk` must return the inspected `info` in the not-added case as well (change its return to `(False, info)` and build the message in the route).
3. Set `"added": True` on the success branch.

In `templates/index.html`, in the `addApkForm` handler: if `result.success && result.added === false` → `showToast(result.message, 'info')` and do **not** reset the form; otherwise unchanged.

**Tests**: update `test_add_apk_idempotent_on_existing_version` to assert `body2["added"] is False` and that `package`/`versionCode` are present; add `test_add_apk_rejects_bad_metadata_before_publishing`: `summary` of 81 chars → 400 and **no** APK, no yml, no marker written.

**Verify**: focused suite passes; `command grep -n "added === false" templates/index.html` → one hit.

### Step 4 — (d) type-check metadata values

In `validate_metadata` (from Step 3): every key except `Categories` must be `str` (raise `ValueError(f"{key} must be a string")`); `Categories` must be a `str` (comma-split) or a `list` of `str`. `_android_metadata_fields_from` stays a pure picker.

**Test** `test_update_app_rejects_non_string_values`: `POST /api/android/update-app {"package": "org.example.demo", "name": ["x"]}` → 400 mentioning `name`; `{"package": ..., "summary": 5}` → 400; the yml is unchanged.

### Step 5 — (e) make the Android publish event real

Add `android_add_apk` to the default string at `app.py:135` → `"add_app,add_version,delete_app,android_add_apk"`. Update `.env.example:63`'s comment to list **all** event names that exist in `app.py` (grep `notify("` — today: `add_app`, `add_version`, `delete_app`, `delete_version`, `update_version`?, … — list exactly what the grep returns) and state the new default. Add one sentence to README's Telegram-notifications paragraph naming the events.

**Test** `test_add_apk_fires_notify_event`: monkeypatch `app_module.notify` with a recorder; upload → recorder called with event `"android_add_apk"`. (Existing `client_with_telegram`-style fixtures are not needed — `notify` is patched out.)

**Verify**: `command grep -n '"add_app,add_version,delete_app,android_add_apk"' app.py` → one hit.

### Step 6 — (f) keep `repo_url` in `repo-config.json` current

Replace `_ensure_repo_config_has_url` with `_sync_repo_config_url()`:
- compute `base = _safe_base_url()`; if `None`, return `False`;
- `want = base.rstrip('/') + '/fdroid/repo'`;
- load `repo_config()`; if its `repo_url` already equals `want`, return `False`;
- otherwise write the config with `repo_url = want` (preserving `name`/`description`) and return `True`.
Call it from `add_apk` (as now) **and** from `status()` — `status()` runs on every Android-tab open inside a request, so a changed `PUBLIC_BASE_URL` is picked up the first time an admin looks; when it returns `True`, call `self.request_update()` so the index is re-signed with the new address. Log at `warning` when `_safe_base_url()` is `None` and no `repo_url` is stored yet ("index will advertise the sidecar's localhost fallback until PUBLIC_BASE_URL is set").

**Test** `test_status_refreshes_repo_url_and_requests_update`: write `repo-config.json` with `repo_url: "http://old.example/fdroid/repo"`; `GET /api/android/status` with the `client_with_base_url`-style fixture (`PUBLIC_BASE_URL=http://feather.example.com`) → the file now has the new URL and `.update-requested` exists.

## Test plan

Seven new tests in `tests/test_android.py`, named above; one existing test updated (idempotency shape). Pattern: `tests/test_android.py` (`fake_inspect`, upload helper). Expect `16 + 7 = 23` test ids in that file.

## Done criteria

- [ ] `command grep -n "startswith(prefix)" app.py` → no hits in `AndroidRepoManager`
- [ ] `command grep -n "ANDROID_APK_FILENAME_RE" app.py` → ≥3 hits (definition + two uses)
- [ ] `command grep -n '\.part' app.py` → the EXDEV temp-name logic present
- [ ] `command grep -n "def validate_metadata" app.py` → one hit; `write_metadata` calls it
- [ ] `command grep -n '"added":' app.py` → both branches of `android_add_apk`
- [ ] Default `TELEGRAM_NOTIFY_EVENTS` string contains `android_add_apk`; `.env.example` and `README.md` list event names
- [ ] `command grep -n "def _sync_repo_config_url" app.py` → one hit; `_ensure_repo_config_has_url` gone
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → `266 passed, 1 skipped`
- [ ] Both break-and-restore demonstrations (Steps 1 and 2) reported
- [ ] `git status --short` shows only the in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

- The `EXDEV` branch or `delete_app` no longer match the excerpts (someone fixed them in passing) — report; do not re-fix.
- `list_apps`'s regex has changed from `^(.+)_(\d+)\.apk$`.
- You find yourself editing `scripts/fdroid_index_loop.sh` — that is plan 076's file.
- Any iOS test changes behaviour.

## Maintenance notes

- Filename ↔ package attribution now has one regex. Any new code that scans `repo/` must use `ANDROID_APK_FILENAME_RE`.
- The `.part` convention is also the answer if a future writer (Telegram APK ingest, plan TBD) lands files in `repo/` from another process: never write the final name directly.
- The "already present" response now carries `added: false`; clients (UI, future scripts) should branch on it, not on the message text.
- `_sync_repo_config_url` makes feather the owner of `repo_url`; the sidecar's `FDROID_REPO_URL` env fallback remains only as a last resort and should be documented as such in plan 076.
