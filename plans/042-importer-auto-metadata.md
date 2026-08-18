# Plan 042: On import, auto-fill description from the release notes and developer from the repo owner

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 50fb1c9..HEAD -- scripts/release_source_ingest.py app.py tests/
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 178 passed
> ```
> If `scripts/release_source_ingest.py` or the import route in `app.py` changed
> since `50fb1c9`, compare the "Current state" excerpts below against the live
> code first; on a semantic mismatch, STOP.

## Status

- **Priority**: P3
- **Effort**: S–M
- **Risk**: LOW–MED (extends the release engine and the import route; the engine change must stay backward-compatible with 21 existing tests)
- **Depends on**: Plan 034/035 (the import route it extends — merged to `main`)
- **Category**: feature
- **Planned at**: commit `50fb1c9`, 2026-08-18

## Why this matters

When the UI imports an app from a GitHub/GitLab release, the new catalog entry
has an **empty description** and `developerName` defaults to `"Unknown"` — so
imported apps look bare in Feather. Yet the importer already fetches the full
release object, which carries the release notes (`body` on GitHub,
`description` on GitLab), and the repo owner is right there in the `owner/repo`
project string. This plan uses those to pre-fill `localizedDescription` and
`developerName` on import, so apps arrive described. It only fills fields the
user left blank, only on **creation** (never overwriting an existing app), and
the user can still edit everything afterward (plan 041).

## Current state

### The engine — `scripts/release_source_ingest.py`
- `ReleaseCandidate` (line 118) is a frozen dataclass with NO body field:
  ```python
  @dataclass(frozen=True)
  class ReleaseCandidate:
      provider: str
      project: str
      release_id: str
      release_tag: str
      release_time: str
      asset_id: str
      asset_name: str
      declared_size: object
      download_url: str
      auth_host: str
  ```
- `github_select_candidate` (line 377) builds it from the release; the release
  object has a `body` field (release notes) that is currently discarded:
  ```python
  return ReleaseCandidate(
      provider="github", project=job.project,
      release_id=str(release.get("id")), release_tag=release.get("tag_name"),
      release_time=release.get("published_at") or "",
      asset_id=str(asset.get("id")), asset_name=asset.get("name") or "",
      declared_size=asset.get("size"), download_url=asset.get("url"),
      auth_host="api.github.com",
  )
  ```
- `gitlab_select_candidate` (near line 448) builds the analogous candidate; the
  GitLab release object's notes are in its `description` field.
- The release `body`/`description` are already present in the list-releases
  response — **no extra API call is needed.**

### The import route — `app.py` (create branch, post-035, ~line 1750)
```python
new_name = name_in or detected_name or bundle_id
new_developer = developer_in or "Unknown"
new_app = {"name": new_name, "bundleIdentifier": bundle_id,
           "developerName": new_developer, "version": version}
if icon_url_in:
    new_app["iconURL"] = icon_url_in
ok, message = source_manager.add_app_manual(
    new_app, ipa_file=fs,
    download_icon_from_url=bool(icon_url_in), base_url=base_url)
```
`candidate` (the `ReleaseCandidate` from `select_candidate`) is in scope here.
`add_app_manual` already writes `data.get('localizedDescription', '')` into the
new app (`app.py:1003`), so setting `new_app["localizedDescription"]` is enough.

### Convention
- The import route reads engine functions off the module alias `release_ingest`.
- 029's engine has 21 tests (`tests/test_release_source_ingest.py`) that construct
  `ReleaseCandidate` and select candidates — they MUST stay green. The 034 import
  tests (`tests/test_import_release.py`) construct fake candidates too.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `178 passed` before changes |
| Syntax | `.venv/bin/python -m py_compile app.py scripts/release_source_ingest.py` | exit 0 |
| Engine tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q` | `21 passed` (unchanged) |
| Import tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py -q` | pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `178 + N passed` |

## Scope

**In scope:**
- `scripts/release_source_ingest.py` — add a defaulted `release_body: str = ""`
  field to `ReleaseCandidate`; populate it in `github_select_candidate`
  (`release.get("body")`) and `gitlab_select_candidate` (`release.get("description")`).
- `app.py` — the import route's create branch only, plus two small module-level
  helpers (`_repo_owner`, `_clean_description`).
- `tests/test_import_release.py` — tests.

**Out of scope:**
- The add-version (existing app) path — metadata is set on creation only, never overwrite.
- `add_app_manual` / other methods — unchanged (it already stores `localizedDescription`).
- An extra API call to fetch the repo's own `description` — MVP uses the release
  body already in hand. (A future plan could prefer the repo one-liner.)
- The Telegram bot — unchanged (out of scope; a bot has no release body).
- Markdown-to-text rendering — MVP is trim + length cap, not a markdown parser.

## Steps

### Step 1: Confirm baseline → `178 passed`. If not, STOP.

### Step 2: Carry the release body in `ReleaseCandidate`
Add `release_body: str = ""` as the LAST field of the dataclass (a default is
required and keeps every existing keyword construction valid). In
`github_select_candidate` add `release_body=release.get("body") or ""` to the
constructor; in `gitlab_select_candidate` add `release_body=release.get("description") or ""`.

**Verify**:
```bash
.venv/bin/python -m py_compile scripts/release_source_ingest.py
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q   # still 21 passed
```
If any engine test fails (e.g. a positional `ReleaseCandidate(...)` construction),
STOP and report — do not reorder existing fields; the new field must be last.

### Step 3: Helpers in `app.py`
Add two module-level helpers near the import route:
```python
def _repo_owner(project):
    """First path segment of an owner/repo (or group/project) string."""
    return (project or "").strip("/").split("/")[0].strip()

def _clean_description(text, limit=800):
    """Trim release notes to a catalog-friendly description."""
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text
```

### Step 4: Use them in the import create branch
In the create branch (app.py ~1750), fill developer and description only when the
user left them blank:
```python
new_name = name_in or detected_name or bundle_id
new_developer = developer_in or _repo_owner(project) or "Unknown"
new_app = {"name": new_name, "bundleIdentifier": bundle_id,
           "developerName": new_developer, "version": version}
description = _clean_description(getattr(candidate, "release_body", ""))
if description:
    new_app["localizedDescription"] = description
if icon_url_in:
    new_app["iconURL"] = icon_url_in
ok, message = source_manager.add_app_manual(
    new_app, ipa_file=fs,
    download_icon_from_url=bool(icon_url_in), base_url=base_url)
```
(`developer_in` from the form still wins; `_repo_owner` is the fallback before
`"Unknown"`. Description is set only if the release had notes.)

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 5: Tests
Extend `tests/test_import_release.py` (its fake `select_candidate` returns a
`ReleaseCandidate` — add `release_body=...` to that fake; follow the file's
`post_and_collect` NDJSON pattern, no network):
1. `test_import_new_app_uses_release_body_as_description` — candidate with `release_body="AnymeX is an anime streaming app."`, create-if-missing, no user description → the created app's `localizedDescription` == that text.
2. `test_import_new_app_developer_from_repo_owner` — project `"RyanYuuki/AnymeX"`, blank developer → created app `developerName == "RyanYuuki"`.
3. `test_import_user_developer_overrides_repo_owner` — developer field `"Custom Dev"` → `developerName == "Custom Dev"` (not the owner).
4. `test_import_long_release_body_is_truncated` — a 2000-char `release_body` → `localizedDescription` length ≤ 801 and ends with the ellipsis.
5. `test_import_empty_release_body_leaves_description_blank` — `release_body=""` → the app has no/empty `localizedDescription` (no crash).

**Prove discrimination**: temporarily hardcode `new_developer = developer_in or "Unknown"` → test 2 fails. Restore.

**Verify**:
```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py -q
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q   # still 21
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

## Done criteria
- [ ] `ReleaseCandidate` carries `release_body` (defaulted); GitHub uses `body`, GitLab uses `description`; engine's 21 tests unchanged and green.
- [ ] On import-create, `developerName` falls back to the repo owner (then `"Unknown"`), and `localizedDescription` is filled from the (trimmed, capped) release notes — both only when the user left them blank, only on creation.
- [ ] Existing-app imports (add-version path) do not touch metadata.
- [ ] `.venv/bin/python -m py_compile app.py scripts/release_source_ingest.py` exit 0; full suite green (178 + 5).
- [ ] `git status --short` shows only the three in-scope files.

## STOP conditions
- Adding `release_body` breaks a `ReleaseCandidate` construction that used positional args — STOP (make it the last field with a default; do not reorder).
- The GitLab release object's notes field isn't `description` in the code you see — verify against `gitlab_select_candidate` before wiring it.
- Filling the description would require an extra provider API call — it must not; use the body already in the releases response.

## Maintenance notes
- The release `body` is changelog-style; a future refinement could prefer the
  repo's own one-line `description` (an extra `GET /repos/{owner}/{repo}` call).
  MVP avoids that call.
- Users edit all of this afterward via Edit App (plan 041). This only sets sane
  defaults at import time; it never overwrites an existing app or a user entry.
- Reviewer: confirm developer/description are set ONLY on creation and ONLY when
  blank, and the engine's new field is last-with-default so the 21 tests hold.
