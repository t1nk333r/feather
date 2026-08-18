# Plan 046: On repo import, default the app icon to the GitHub owner avatar

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat c052cb0..HEAD -- app.py tests/test_import_release.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 205 passed
> ```
> If the import route's create branch changed since `c052cb0`, compare the
> "Current state" excerpt against the live code first; on a mismatch, STOP.

## Status

- **Priority**: P2 (imported apps currently land with no icon — a visible gap)
- **Effort**: S
- **Risk**: LOW (adds a default icon URL only when the user provided none, only on create)
- **Depends on**: plans 034/035/042 (the import route it extends — merged)
- **Category**: feature / polish
- **Planned at**: commit `c052cb0`, 2026-08-18

## Why this matters

When you import an app from a GitHub repo without pasting an "Icon URL", the new
catalog entry gets **no icon** (`iconURL` unset/empty) — that's why apps like
AnymeX show a placeholder. IPA icon extraction was deliberately rejected earlier
(plan 023). But every GitHub owner has a stable, always-available avatar at
`https://github.com/<owner>.png`, and the importer already knows the owner (it's
the first segment of the `owner/repo` project). This plan defaults the icon to
that avatar when the user didn't supply one — so GitHub imports arrive with a
sensible icon, downloaded and hosted through the existing icon pipeline. The user
can still override it (Edit App, or the import form's Icon URL field).

## Current state — `app.py` (import-release create branch, ~line 1815)

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
- `icon_url_in` is the icon URL from the form (empty string when the user leaves
  it blank). `provider` (`"github"`/`"gitlab"`) and `project` (`owner/repo`) are
  in scope. `_repo_owner(project)` (plan 042) returns the first path segment.
- `source_manager.download_icon_from_url(url, bundle_id)` fetches a URL and stores
  it, returning the stored extension; `add_app_manual(..., download_icon_from_url=True)`
  triggers it. Tests monkeypatch `download_icon_from_url` to avoid network
  (see `tests/test_import_release.py::test_import_new_app_sets_icon_url`, which does
  `mp.setattr(app_module.source_manager, "download_icon_from_url", lambda url, bundle_id: "png")`).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `205 passed` before |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| Focused | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py -q` | pass |
| Full | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `205 + N passed` |

## Scope

**In scope:**
- `app.py` — the import-release create branch only: default `icon_url_in` to the
  GitHub owner avatar when it's blank and provider is GitHub.
- `tests/test_import_release.py` — tests.

**Out of scope:**
- GitLab auto-icon (a GitLab avatar needs an API call / isn't a simple URL) — GitLab imports keep requiring a manual Icon URL. Note it; don't build it.
- IPA icon extraction (rejected, plan 023).
- The add-version (existing app) path — icons are set on create only.
- `download_icon_from_url` internals / the storage classes — unchanged.

## Steps

### Step 1: Baseline → `205 passed`. If not, STOP.

### Step 2: Default the icon to the owner avatar (create branch only)
Just before the `if icon_url_in:` block, add:
```python
if not icon_url_in and provider == "github":
    owner = _repo_owner(project)
    if owner:
        icon_url_in = f"https://github.com/{owner}.png"
```
The existing `if icon_url_in:` + `download_icon_from_url=bool(icon_url_in)` then
downloads and hosts it. (A user-supplied `icon_url_in` still wins — this only
fills the blank case.)

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 3: Tests (extend `tests/test_import_release.py`; follow the existing
`test_import_new_app_sets_icon_url` pattern — monkeypatch
`source_manager.download_icon_from_url` so no network happens)
1. `test_import_github_defaults_icon_to_owner_avatar` — github `project="RyanYuuki/AnymeX"`, blank icon URL, create → `download_icon_from_url` is called with `https://github.com/RyanYuuki.png`, and the created app's `iconURL` is the hosted `/icons/<bundle>/...` URL. (Assert the URL passed to the monkeypatched fn, e.g. capture it in a list.)
2. `test_import_explicit_icon_url_still_wins` — provide an icon URL in the form → that URL is used, not the avatar.
3. `test_import_gitlab_no_auto_icon` — gitlab provider, blank icon URL, create → `download_icon_from_url` is NOT called; created app has no/empty `iconURL` (no crash).

**Prove discrimination**: temporarily gate the new block behind `if False:` →
test 1 fails (no avatar used). Restore.

**Verify**: focused import tests, then full suite.

## Done criteria
- [ ] A GitHub import with a blank Icon URL creates an app whose icon is downloaded from `https://github.com/<owner>.png` and hosted.
- [ ] A user-supplied Icon URL still takes precedence; GitLab imports are unchanged (no auto-icon).
- [ ] Only on create (add-version path untouched).
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (205 + 3).
- [ ] `git status --short` shows only `app.py`, `tests/test_import_release.py`.

## STOP conditions
- The create branch no longer matches the excerpt (a refactor) — reconcile first.
- `download_icon_from_url` fetching the avatar would need auth/headers the current helper can't send — report; the avatar URL is public and needs none, so this shouldn't happen.

## Maintenance notes
- `https://github.com/<owner>.png` redirects to the avatar CDN; the existing
  `download_icon_from_url` follows redirects (it uses `requests` with defaults).
  If it ever stops following redirects, that's where to look.
- A future plan could add GitLab avatars (API call) or prefer a repo's own icon.
- Reviewer: confirm the avatar is used ONLY when the user left the field blank and
  ONLY for GitHub, and that no network call happens in the tests.
