# Plan 035: Make the UI repo-import forgiving — URL normalization, auto-named new apps, optional icon

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 5a2d10e..HEAD -- app.py templates/index.html tests/test_import_release.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 166 passed
> ```
> If `app.py` or `templates/index.html` changed since `5a2d10e`, compare the
> "Current state" excerpts below against the live code before proceeding; on a
> semantic mismatch, STOP.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW–MED (edits the new import route + its form + tests; no engine change)
- **Depends on**: Plan 034 (the import route this refines — already merged to `main`)
- **Category**: feature / UX
- **Planned at**: commit `5a2d10e`, 2026-08-17

## Why this matters

The Plan 034 "Import from Repo" feature works, but three rough edges surfaced in
real use:

1. **Pasting a repo URL fails cryptically.** Entering `https://github.com/RyanYuuki/AnymeX`
   in the Repository field produced `github API returned HTTP 404 for
   https://github.com/RyanYuuki/AnymeX` — the route sends the raw value straight
   to the GitHub API (`https://api.github.com/repos/<value>/releases`), so a URL
   becomes a nonsense path. Users naturally paste the browser URL.
2. **Creating a new app demands manual name + developer.** The route already
   reads the app's name from the IPA's `Info.plist` (`detected_name`) but ignores
   it, forcing the user to retype it and a developer name, or it errors with
   "New apps need a name and developer name".
3. **Imported apps have no icon.** New apps come in icon-less (no reliable icon
   source for a release IPA), leaving a blank/placeholder icon and no way to set
   one during import.

This plan makes the route **normalize** a pasted URL, **auto-name** new apps from
the IPA (defaulting the developer to `Unknown`, matching the Telegram path), and
accept an **optional icon URL** so a newly created app can get an icon in one
step. Engine code is untouched.

## Current state

### The route — `app.py` `import_release()` (starts line 1625)

Relevant lines on `main`:
- `project = (payload.get('project') or '').strip()` — app.py:1628 (raw, unnormalized).
- `name_in`, `developer_in` — app.py:1633–1634.
- The Job is built at app.py:1654–1658; `select_candidate` then uses `job.project` verbatim.
- Metadata extraction captures a third value that is currently unused:
  `bundle_id, version, detected_name = release_ingest.extract_ipa_metadata(...)` — app.py:1702.
- The publish branch — app.py:1708–1721:
  ```python
  existing = source_manager.get_app(bundle_id)
  fs = FileStorage(stream=open(tmp_path, "rb"), filename=f"{secure_filename(version)}.ipa")
  if existing:
      ok, message = source_manager.add_version(bundle_id, {"version": version}, ipa_file=fs, base_url=base_url)
  elif create_if_missing:
      if not (name_in and developer_in):
          yield event(stage="error", error="New apps need a name and developer name")
          return
      ok, message = source_manager.add_app_manual(
          {"name": name_in, "bundleIdentifier": bundle_id, "developerName": developer_in, "version": version},
          ipa_file=fs, base_url=base_url)
  else:
      yield event(stage="error", error=f"App {bundle_id} is not in the catalog. Tick 'create if missing' ...")
      return
  ```

### The engine (reuse, do NOT modify) — `scripts/release_source_ingest.py`

- `release_ingest._GITHUB_PROJECT_RE` is a compiled regex `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$` — the exact `owner/repo` shape GitHub needs. Reuse it for validation.
- `SourceManager.add_app_manual(self, data, ipa_file=None, download_from_url=False, icon_file=None, download_icon_from_url=False, base_url=None)` (app.py:932) already downloads an icon when `download_icon_from_url=True` and `data['iconURL']` is set (via `download_icon_from_url()` at app.py ~1104). Reuse this — no new icon code.

### The form — `templates/index.html` (`importRepoForm`, line 744)

Fields: `provider` (754), `project` (754), `bundleIdentifier` (758), `assetGlob` (762), `createIfMissing` (772), `allowedDownloadHosts` (778), `name` (782), `developerName` (786), progress area (789). The submit handler is at line 1028 and builds the JSON `body` object from `f.<name>.value`.

### Repo conventions

- Tests: `tests/test_import_release.py` (from Plan 034) monkeypatches `app_module.release_ingest.select_candidate` and `.stream_download`, POSTs, and reads the buffered NDJSON body via a `post_and_collect`-style helper (reads the full body while the monkeypatch is active — the response is a lazy generator, so the body MUST be consumed before undoing patches). Follow that exact pattern. No real network.
- Conventional commits.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `166 passed` before changes |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| Import tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py -q` | all pass |
| Engine untouched | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q` | `21 passed` |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `166 + N passed` |

## Scope

**In scope** (modify only these):
- `app.py` — the `import_release()` route only, plus one small module-level helper `_normalize_repo_project`.
- `templates/index.html` — the `importRepoForm` (add an Icon URL field; the JS body).
- `tests/test_import_release.py` — extend with new cases.

**Out of scope** (do NOT touch):
- `scripts/release_source_ingest.py` — no engine change this time (reuse `_GITHUB_PROJECT_RE`).
- `SourceManager.add_app_manual` / `add_version` and every other method.
- The Add App / Edit App / other routes and forms.
- `requirements.txt`, container/CI files.

## Git workflow

- Branch: `advisor/035-import-repo-usability`
- Separate commits: route logic, then template, then tests. Do NOT push or open a PR.

## Steps

### Step 1: Confirm baseline
`ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → **166 passed**. If not, STOP.

### Step 2: Normalize the pasted repo reference

Add a module-level helper in `app.py` (near the route):
```python
def _normalize_repo_project(raw):
    """Strip browser-URL chrome from a pasted repo reference.

    Accepts e.g. 'https://github.com/Owner/Repo', 'github.com/Owner/Repo.git',
    or 'Owner/Repo' and returns 'Owner/Repo'. GitLab nested groups (extra
    slashes) and numeric IDs pass through unchanged after prefix stripping.
    """
    p = (raw or "").strip()
    for prefix in ("https://", "http://"):
        if p.lower().startswith(prefix):
            p = p[len(prefix):]
    if p.lower().startswith("www."):
        p = p[4:]
    for host in ("github.com/", "gitlab.com/"):
        if p.lower().startswith(host):
            p = p[len(host):]
    p = p.strip("/")
    if p.endswith(".git"):
        p = p[:-4]
    return p.strip("/")
```

In `import_release()`, after reading `project` (app.py:1628), normalize it and
validate the GitHub shape with a clear message. Do this INSIDE the `run()`
generator, alongside the existing provider/project checks (so the error streams
as an NDJSON `error` event), e.g. right after the `if not project:` check:
```python
project = _normalize_repo_project(project)
if not project:
    yield event(stage="error", error="Repository is required"); return
if provider == 'github' and not release_ingest._GITHUB_PROJECT_RE.match(project):
    yield event(stage="error", error="GitHub repository must be owner/repo, e.g. RyanYuuki/AnymeX"); return
```
(Use the normalized `project` when building the `Job`.) Note: `project` is
read outside `run()` at 1628; either normalize it there before `run()` is
defined, or reassign inside `run()` — pick whichever keeps the closure correct.
Simplest: normalize at 1628 (`project = _normalize_repo_project(payload.get('project') or '')`) and keep only the two `yield` validation checks inside `run()`.

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 3: Auto-name new apps; default developer to "Unknown"

Replace the create-if-missing branch (app.py:1712–1718) so it never demands
manual name/developer — it uses, in order: the user's entry, the IPA's detected
name, then the bundle id; and defaults the developer to `Unknown`:
```python
elif create_if_missing:
    new_name = name_in or detected_name or bundle_id
    new_developer = developer_in or "Unknown"
    new_app = {"name": new_name, "bundleIdentifier": bundle_id,
               "developerName": new_developer, "version": version}
    # (icon wiring added in Step 4)
    ok, message = source_manager.add_app_manual(new_app, ipa_file=fs, base_url=base_url)
```
Remove the `if not (name_in and developer_in): yield error` guard entirely.
Leave the `if existing:` (add_version) and final `else:` branches unchanged.

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 4: Accept an optional icon URL (new apps only)

Read it from the payload near the other fields (app.py ~1628–1635):
```python
icon_url_in = (payload.get('iconURL') or '').strip()
```
In the create-if-missing branch from Step 3, when an icon URL was provided, ask
`add_app_manual` to fetch it (it already supports this):
```python
    new_app = {"name": new_name, "bundleIdentifier": bundle_id,
               "developerName": new_developer, "version": version}
    if icon_url_in:
        new_app["iconURL"] = icon_url_in
    ok, message = source_manager.add_app_manual(
        new_app, ipa_file=fs,
        download_icon_from_url=bool(icon_url_in), base_url=base_url)
```
Do NOT attempt to set an icon on the `if existing:` (add_version) path — adding a
version must not silently overwrite an existing app's icon; that stays an
Edit-App action. (Document this in the form label.)

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 5: Add the Icon URL field to the form + JS

In `templates/index.html`, in `importRepoForm` (after the `developerName` input,
~line 786), add:
```html
<label>Icon URL (optional, for new apps)</label>
<input type="url" name="iconURL" placeholder="https://example.com/icon.png">
```
Match the existing field markup/indentation in that form. In the submit handler
(line 1028), add `iconURL` to the `body` object:
```javascript
iconURL: f.iconURL.value.trim(),
```
Also relax the `project` field to accept a pasted URL: change its placeholder to
`owner/repo or a full repo URL` (the input at ~line 754). Keep the no-emoji rule.

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0 (template unaffected by py_compile, but the Step 6 GET-`/` test covers rendering).

### Step 6: Tests

Extend `tests/test_import_release.py`. Follow the existing `post_and_collect`
pattern (consume the NDJSON body while patches are active). Add:

1. `test_import_normalizes_pasted_github_url` — POST `project="https://github.com/Owner/Repo"`; monkeypatch `select_candidate` to record the `job.project` it receives and return a fake candidate + a `stream_download` that writes a real IPA whose bundle matches the seeded catalog; assert the recorded `job.project == "Owner/Repo"` and the stream ends `done`.
2. `test_import_rejects_unparseable_github_project` — POST `project="not a repo"` (github); assert the stream ends `error` with a message mentioning `owner/repo`, and `select_candidate` was never called.
3. `test_import_new_app_auto_names_from_ipa` — create-if-missing true, blank `name`/`developerName`, IPA whose plist has `CFBundleDisplayName="AnymeX"`; assert `done` and the created catalog app has `name == "AnymeX"` and `developerName == "Unknown"`.
4. `test_import_new_app_sets_icon_url` — create-if-missing true with `iconURL` provided; monkeypatch `source_manager.download_icon_from_url` to return `"png"` (no network); assert the created app's `iconURL` ends with `/icons/<bundle>/icon.png`.

Reuse the IPA-zip fixture already in the file (Plan 034). For test 3, ensure the
fixture's plist sets a display name.

**Prove discrimination**: temporarily revert Step 2's normalization (send raw
`project`) → test 1 fails (recorded project keeps the URL). Restore.

**Verify**:
```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py -q
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q   # still 21
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

## Test plan

- 4 new tests in `tests/test_import_release.py`, all no-network (monkeypatch the
  engine + `download_icon_from_url`).
- Verify the full suite is `166 + 4 = 170` (or however many the existing file
  had + 4) and the engine suite stays `21 passed`.
- Run the whole suite with real DNS blocked once to confirm no new network leak
  (as the reviewer will): the new tests must pass with outbound DNS blocked.

## Done criteria

- [ ] A pasted `https://github.com/Owner/Repo` (or `.git`) is normalized to `Owner/Repo` before the API call.
- [ ] An unparseable GitHub project yields a clear `owner/repo` error before any network.
- [ ] Create-if-missing needs no manual name/developer: name falls back to IPA detected name then bundle id; developer defaults to `Unknown`. The old "New apps need a name and developer name" error is gone.
- [ ] An optional `iconURL` sets the icon on a newly created app (via `add_app_manual`'s existing download path); the add-version path does not touch icons.
- [ ] The form has an Icon URL field and the JS sends it; the project field accepts a pasted URL.
- [ ] `scripts/release_source_ingest.py` unchanged (`git diff` empty for it); engine suite `21 passed`.
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (166 + new).
- [ ] New import tests pass with outbound DNS blocked (no real network).
- [ ] `git status --short` shows only the three in-scope files.

## STOP conditions

- Setting an icon would require modifying `add_app_manual` or `add_version` (it should not — `add_app_manual` already supports `download_icon_from_url`).
- `_GITHUB_PROJECT_RE` no longer exists in the engine (import path drifted).
- A test needs real network to pass.
- Normalization would need to handle self-hosted GitHub/GitLab hosts (out of scope — v1 is github.com / gitlab.com only, as Plan 029 fixed the API origins).

## Maintenance notes

- Normalization only strips `github.com` / `gitlab.com` chrome. Self-hosted forge
  URLs are out of scope (the engine pins the API origins) — a pasted self-hosted
  URL will still error clearly on the GitHub shape check or as a provider error.
- Icons are set only when creating a new app. Changing an existing app's icon
  stays an Edit-App action, by design (add-version must not clobber it).
- Reviewer should confirm: normalization runs before `select_candidate`; the
  add-version path is icon-free; and the new tests make no real network call.
