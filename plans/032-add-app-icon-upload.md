# Plan 032: Make `/api/add-app` honor the icon the Add-App form already sends

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update this plan's status row in
> `plans/README.md` unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 88e6a11..HEAD -- app.py templates/index.html tests/test_routes.py
> ```
> At planning time this diff is empty. If `app.py` or `templates/index.html`
> changed since `88e6a11`, compare the "Current state" excerpts below against
> the live code before proceeding; on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW–MED (edits a mutating route)
- **Depends on**: none
- **Category**: bug / API consistency
- **Planned at**: commit `88e6a11`, 2026-08-17

## Why this matters

The admin UI's **Add App** form has an icon picker: a file input and a
"Use URL instead" toggle (`templates/index.html:541–547`). When the user
submits, the frontend appends `iconFile` (or `downloadIconFromUrl=true`) to the
multipart body (`templates/index.html:945–950`) and POSTs to `/api/add-app`.

But the `/api/add-app` route **never reads those fields** and never passes an
icon to `add_app_manual` — so the icon the user selected is **silently
dropped**. The app is created with no icon, giving no error. The user has to go
to a *second* screen (Edit App → `/api/update-app`) and set the icon again.

`add_app_manual` already fully supports this — it accepts `icon_file` and
`download_icon_from_url` parameters and handles upload, URL-download, and
cleanup (`app.py:926`, `app.py:962–977`, `app.py:1013–1014`). Only the route is
missing the wiring. `/api/update-app` shows exactly how to do it correctly
(`app.py:1675–1718`). This plan makes `/api/add-app` read the icon fields the
same way and pass them through — closing a real user-facing bug and removing the
create-then-edit dance the Telegram ingest path (Plan 023) had to work around.

## Current state

### The form sends an icon (frontend, for context — do NOT edit)

```html
<!-- templates/index.html:541 -->
<input type="file" id="iconFile" name="iconFile" accept=".png,.jpg,.jpeg,.webp,.gif" ...>
```

```javascript
// templates/index.html:945–950 (inside the addAppForm submit handler)
if (iconFile && !useIconUrl) {
    formData.append('iconFile', iconFile);
}
if (useIconUrl) {
    formData.append('downloadIconFromUrl', 'true');
}
```

The form field is `iconURL` (`templates/index.html:547`), sent as part of the
normal `FormData`, so it is already present in `request.form`.

### The route ignores it (backend — the file to change)

```python
# app.py:1601–1640  (/api/add-app)
@app.route('/api/add-app', methods=['POST'])
@requires_auth
def add_app():
    try:
        base_url = resolve_base_url()
        if request.content_type and 'multipart/form-data' in request.content_type:
            ipa_file = request.files.get('ipaFile')
            download_from_url = request.form.get('downloadFromUrl', 'false').lower() == 'true'
            data = {
                'name': request.form.get('name'),
                'bundleIdentifier': request.form.get('bundleIdentifier'),
                'developerName': request.form.get('developerName'),
                'localizedDescription': request.form.get('localizedDescription', ''),
                'iconURL': request.form.get('iconURL', ''),
                'version': request.form.get('version'),
                'downloadURL': request.form.get('downloadURL', ''),
                'minOSVersion': request.form.get('minOSVersion', '14.0')
            }
        else:
            data = request.get_json() if request.is_json else {}
            ipa_file = None
            download_from_url = False

        if not data.get('bundleIdentifier'):
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400

        success, message = source_manager.add_app_manual(data, ipa_file=ipa_file if ipa_file and ipa_file.filename else None, download_from_url=download_from_url, base_url=base_url)
        ...
```

Note: `icon_file` and `download_icon_from_url` are **never** read and **never**
passed to `add_app_manual`.

### The exemplar to copy (do NOT edit — this is how it should look)

```python
# app.py:1675–1709  (/api/update-app) — already wires the icon correctly
if request.content_type and 'multipart/form-data' in request.content_type:
    icon_file = request.files.get('iconFile')
    download_icon_from_url = request.form.get('downloadIconFromUrl', 'false').lower() == 'true'
    data = { ... 'iconURL': request.form.get('iconURL', '') }
    ...
else:
    ...
    icon_file = None
    download_icon_from_url = False
...
success, message = source_manager.update_app(
    bundle_id, data,
    icon_file=icon_file if icon_file and icon_file.filename else None,
    download_icon_from_url=download_icon_from_url,
    base_url=base_url
)
```

### The method already accepts the icon (do NOT edit — just confirming)

```python
# app.py:926
def add_app_manual(self, data, ipa_file=None, download_from_url=False,
                   icon_file=None, download_icon_from_url=False, base_url=None):
```

### Repo conventions to follow

- Match `/api/update-app`'s exact idioms: the `request.files.get('iconFile')`
  read, the `.lower() == 'true'` flag parse, `None` in the JSON branch, and the
  `icon_file=icon_file if icon_file and icon_file.filename else None` guard at
  the call site. Consistency between the two routes is the point.
- Tests: icon-upload-through-a-route is exercised in
  `tests/test_storage.py:393–410` (`/api/update-app` with an
  `io.BytesIO` `iconFile`). Model the new test on that shape, but use the
  local-backend `authed_client` fixture from `tests/test_routes.py`.
- Conventional-commit messages.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Baseline tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `136 passed` before changes |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| Focused test | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k add_app_icon` | new test(s) pass |
| Full tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `138 passed` |

## Scope

**In scope** (the only files you may modify):
- `app.py` — the `/api/add-app` route (app.py:1601–1640) only
- `tests/test_routes.py` — add regression tests (create)

**Out of scope** (do NOT touch):
- `add_app_manual` and every other `SourceManager` method — they already
  support the icon; changing them is unnecessary and risky.
- `/api/update-app`, `/api/add-version`, `/api/update-version` — leave them
  exactly as they are; this plan only fixes `add-app`.
- `templates/index.html` — the frontend already sends the icon correctly. Do
  not change the form or its JavaScript.
- The IPA-handling lines of `add_app`. Only *add* icon wiring; do not alter how
  `ipa_file` / `download_from_url` are read or passed.

## Git workflow

- Branch: `advisor/032-add-app-icon-upload`
- Example commit: `fix(api): honor icon upload on /api/add-app`
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Confirm the baseline is green

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

**Verify**: `136 passed`. If not, STOP.

### Step 2: Read the icon fields in the multipart branch

In `add_app` (app.py:1601), inside the
`if request.content_type and 'multipart/form-data' in request.content_type:`
block, add reads for the icon file and flag alongside the existing `ipa_file` /
`download_from_url` reads:

```python
ipa_file = request.files.get('ipaFile')
download_from_url = request.form.get('downloadFromUrl', 'false').lower() == 'true'
icon_file = request.files.get('iconFile')
download_icon_from_url = request.form.get('downloadIconFromUrl', 'false').lower() == 'true'
```

`data['iconURL']` is already populated from `request.form.get('iconURL', '')` —
leave that line as-is.

### Step 3: Default the icon fields in the JSON branch

In the `else:` (JSON) branch, alongside the existing `ipa_file = None` /
`download_from_url = False`, add:

```python
ipa_file = None
download_from_url = False
icon_file = None
download_icon_from_url = False
```

This keeps the JSON (backward-compatibility) branch behaving exactly as before —
no icon handling — matching how `/api/update-app`'s JSON branch does it.

### Step 4: Pass the icon through to `add_app_manual`

Change the call from:

```python
success, message = source_manager.add_app_manual(data, ipa_file=ipa_file if ipa_file and ipa_file.filename else None, download_from_url=download_from_url, base_url=base_url)
```

to:

```python
success, message = source_manager.add_app_manual(
    data,
    ipa_file=ipa_file if ipa_file and ipa_file.filename else None,
    download_from_url=download_from_url,
    icon_file=icon_file if icon_file and icon_file.filename else None,
    download_icon_from_url=download_icon_from_url,
    base_url=base_url,
)
```

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 5: Add regression tests

Append to `tests/test_routes.py`. Two cases: a direct file upload, and the
URL-download flag. Both assert the created app's `iconURL` becomes a
self-hosted `/icons/...` URL — which can only happen if the route actually
passed the icon through.

```python
def test_add_app_icon_upload_sets_hosted_icon_url(authed_client):
    """Regression for Plan 032: /api/add-app must honor an uploaded iconFile.
    Before the fix the route dropped it and the app was created icon-less."""
    import io

    resp = authed_client.post(
        "/api/add-app",
        data={
            "name": "Iconic",
            "bundleIdentifier": "com.example.iconic",
            "developerName": "Dev",
            "version": "1.0",
            "downloadURL": "http://example.test/x.ipa",
            "iconFile": (io.BytesIO(b"\x89PNG fake bytes"), "icon.png"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True

    app_entry = json.loads(authed_client.get("/api/app/com.example.iconic").data)
    assert app_entry["iconURL"].endswith("/icons/com.example.iconic/icon.png")


def test_add_app_icon_download_from_url_sets_hosted_icon_url(authed_client, gzip_ipa_server):
    """Plan 032: the downloadIconFromUrl flag must also be honored on add-app.
    Reuses gzip_ipa_server (an in-process loopback HTTP stub) as a stand-in
    downloadable body, exactly as the update-app icon tests do."""
    resp = authed_client.post(
        "/api/add-app",
        data={
            "name": "Fetched",
            "bundleIdentifier": "com.example.fetched",
            "developerName": "Dev",
            "version": "1.0",
            "downloadURL": "http://example.test/x.ipa",
            "iconURL": gzip_ipa_server,
            "downloadIconFromUrl": "true",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True

    app_entry = json.loads(authed_client.get("/api/app/com.example.fetched").data)
    assert "/icons/com.example.fetched/" in app_entry["iconURL"]
```

> `gzip_ipa_server` is an existing fixture (a loopback HTTP server) used by the
> URL-download tests — search `tests/test_routes.py` for its definition to
> confirm its return shape (a URL string) before relying on it. If it does not
> exist or its shape differs, STOP and report; do not invent a network call.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k add_app_icon
```

**Prove the first test discriminates**: temporarily revert Step 4 (drop the
`icon_file=` / `download_icon_from_url=` arguments), rerun
`test_add_app_icon_upload_sets_hosted_icon_url`, and confirm it **fails** (the
`iconURL` will not be a `/icons/...` path because the icon was dropped). Restore
the fix and confirm it passes. Do not leave the revert in place.

### Step 6: Run the full suite

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

**Verify**: `138 passed` (136 existing + 2 new).

## Test plan

- New tests in `tests/test_routes.py`:
  - `test_add_app_icon_upload_sets_hosted_icon_url` — uploaded `iconFile`
    produces a self-hosted `/icons/.../icon.png` URL.
  - `test_add_app_icon_download_from_url_sets_hosted_icon_url` — the
    `downloadIconFromUrl` flag path produces a `/icons/...` URL.
- Model: `tests/test_storage.py:393–410` for the multipart `iconFile` upload
  shape; the `authed_client` and `gzip_ipa_server` fixtures from
  `tests/test_routes.py` for the harness.
- Verification: full suite → `138 passed`, and the Step 5 discrimination revert
  fails the first new test.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `grep -n "icon_file = request.files.get('iconFile')" app.py` — at least
      two matches (the existing `/api/update-app` and the new `/api/add-app`)
- [ ] The `add_app_manual` call in `add_app` passes `icon_file=` and
      `download_icon_from_url=` (visible in `git diff app.py`)
- [ ] `.venv/bin/python -m py_compile app.py` — exit 0
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` — `138 passed`
- [ ] Reverting Step 4 fails `test_add_app_icon_upload_sets_hosted_icon_url`;
      restoring it passes
- [ ] `git status --short` shows only `app.py` and `tests/test_routes.py`
      modified
- [ ] `plans/README.md` status row for Plan 032 updated

## STOP conditions

Stop and report back (do not improvise) if:

- The baseline in Step 1 is not `136 passed`.
- `add_app_manual`'s signature (app.py:926) does not accept `icon_file` and
  `download_icon_from_url` — the code has drifted from this plan; report it.
- The `gzip_ipa_server` fixture does not exist or returns something other than a
  URL string — do not fabricate a network call; report and skip the second
  test, keeping the first.
- Any existing test fails after the change. Wiring the icon must not alter any
  other behavior; if it does, report it rather than editing existing tests.

## Maintenance notes

- After this lands, the HANDOFF.md trap note "`/api/add-app` silently ignores
  icons" (HANDOFF.md:86) is resolved and can be retired.
- The Telegram ingest path (Plan 023) creates an app then sets the icon in a
  separate `/api/update-app` call. That two-step flow still works and is out of
  scope here — but a future cleanup could let it create-with-icon in one call
  now that `/api/add-app` supports it. Do not make that change in this plan.
- A reviewer should confirm the JSON branch still sets both icon variables to
  their no-op defaults, so a JSON `POST /api/add-app` behaves exactly as before.
