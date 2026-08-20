# Plan 064: Apply the icon on repo-import of an *existing* app (URL + auto-detect)

> Backend + a one-line template label. `app.py` (`/api/import-release` and the
> auto-import scheduler) + `templates/index.html`. Adds tests.
> Drift check: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 236 passed at commit `1da169d`.

## Status
- Priority P2 (user-reported: icon never set on re-import). Effort S–M. Risk MED. Planned at `1da169d`.

## Why (root cause)
On repo-import, the icon is only ever set in the **create-if-missing** branch. When
the app **already exists**, both import paths just call `add_version(...)` and never
touch the icon:

- `app.py` `import_release()` (`/api/import-release`), ~line 2628-2631:
  ```python
  existing = source_manager.get_app(bundle_id)
  fs = FileStorage(stream=open(tmp_path, "rb"), filename=f"{secure_filename(version)}.ipa")
  if existing:
      ok, message = source_manager.add_version(bundle_id, {"version": version}, ipa_file=fs, base_url=base_url)
  elif create_if_missing:
      ...   # <- ALL the icon logic (icon_url_in / _extract_ipa_icon / avatar) lives here only
  ```
- `app.py` auto-import scheduler, ~line 2398-2400: same shape — `if existing: add_version(...)`
  with the icon logic only in the `elif ... create_if_missing:` branch below it.

So a user re-importing an app they already have (e.g. **AnymeX**) can neither supply
an `iconURL` nor get auto-detection — the icon is silently ignored. (The download
itself is fine: `download_icon_from_url` uses `requests.get(..., stream=True)`, which
follows GitHub's `github.com/.../raw/...` → `raw.githubusercontent.com` 302 redirect
to the real `image/png`.) The import form even labels the field *"Icon URL (optional,
for new apps)"* (line ~829) — accurate today, but the maintainer wants it to apply to
existing apps too.

`SourceManager.update_app(bundle_id, data, icon_file=?, download_icon_from_url=?, base_url=?)`
(line 1369) is the right tool: it updates only the fields present in `data`, preserves
everything else, and already downloads-and-hosts an icon URL or an uploaded file.

## Scope
- **In scope:** the `existing` branch of `import_release()` and of the auto-import
  scheduler; the template label. New tests.
- **Out of scope:** the create-if-missing branches (already correct); `update_app`
  itself; `_extract_ipa_icon`/`download_icon_from_url` internals; any other route.

## Behavior to implement
On an **existing** app, after `add_version` succeeds, apply the icon with this
precedence — **best-effort** (a failed icon step must NOT fail the import; log it):
1. **Explicit `iconURL` (import-release only):** if the user supplied one, always
   apply it (download + host via `update_app(..., download_icon_from_url=True)`).
   The user asked for this icon, so it wins even over an existing icon.
2. **Auto-detect, only when the current icon is missing/placeholder** (never clobber
   a good icon on a routine version bump): if `existing`'s `iconURL` is empty or
   equals `SOURCE_ARTWORK_URL` → `_extract_ipa_icon(tmp_path)`; if that yields a file,
   host it via `update_app(bundle_id, {}, icon_file=icon_fs, base_url=base_url)`; else
   GitHub-owner-avatar fallback (`https://github.com/<owner>.png`) via
   `update_app(..., download_icon_from_url=True)`. (GitLab: no avatar → iconless, same
   as the create branch.)

`tmp_path` (the downloaded IPA) is still on disk in both branches (removed only in the
outer `finally`), so `_extract_ipa_icon(tmp_path)` is valid here.

## Step 1 — `import_release()` existing branch (`app.py` ~2630)
Replace:
```python
            if existing:
                ok, message = source_manager.add_version(bundle_id, {"version": version}, ipa_file=fs, base_url=base_url)
```
with:
```python
            if existing:
                ok, message = source_manager.add_version(bundle_id, {"version": version}, ipa_file=fs, base_url=base_url)
                if ok:
                    _apply_icon_to_existing(
                        bundle_id, existing, tmp_path,
                        provider=provider, project=project,
                        icon_url_in=icon_url_in, base_url=base_url,
                    )
```

## Step 2 — auto-import scheduler existing branch (`app.py` ~2398)
Replace:
```python
            if existing:
                ok, message = source_manager.add_version(
                    bundle_id, {"version": version}, ipa_file=fs, base_url=base_url)
```
with:
```python
            if existing:
                ok, message = source_manager.add_version(
                    bundle_id, {"version": version}, ipa_file=fs, base_url=base_url)
                if ok:
                    _apply_icon_to_existing(
                        bundle_id, existing, tmp_path,
                        provider=job_obj.provider, project=job_obj.project,
                        icon_url_in="", base_url=base_url,
                    )
```

## Step 3 — add the shared helper (`app.py`, module level, near `_extract_ipa_icon`)
Add a module-level function (NOT a method). Place it just after `_extract_ipa_icon`
(after line ~2234) so both call sites can use it:
```python
def _apply_icon_to_existing(bundle_id, existing, ipa_path, *, provider, project,
                            icon_url_in="", base_url=None):
    """Best-effort: (re)apply an icon to an already-existing app during import.

    An explicit ``icon_url_in`` (user-supplied) always wins. Otherwise auto-detect
    ONLY when the current icon is missing/placeholder (never clobber a good icon on
    a routine version import): IPA-extracted icon, else GitHub owner avatar.
    Never raises — a failed icon step must not fail the version import.
    """
    try:
        if icon_url_in:
            ok, msg = source_manager.update_app(
                bundle_id, {"iconURL": icon_url_in},
                download_icon_from_url=True, base_url=base_url)
            if not ok:
                logging.warning("import: icon URL update failed for %s: %s", bundle_id, msg)
            return
        cur_icon = (existing.get("iconURL") or "").strip()
        if cur_icon and cur_icon != SOURCE_ARTWORK_URL:
            return  # already has a real icon — leave it
        extracted_icon = _extract_ipa_icon(ipa_path)
        try:
            if extracted_icon:
                icon_fs = FileStorage(stream=open(extracted_icon, "rb"), filename="icon.png")
                source_manager.update_app(bundle_id, {}, icon_file=icon_fs, base_url=base_url)
            else:
                owner = _repo_owner(project) if provider == "github" else ""
                if owner:
                    source_manager.update_app(
                        bundle_id, {"iconURL": f"https://github.com/{owner}.png"},
                        download_icon_from_url=True, base_url=base_url)
        finally:
            if extracted_icon:
                try:
                    os.remove(extracted_icon)
                except OSError:
                    pass
    except Exception:
        logging.exception("import: applying icon to existing app %s failed", bundle_id)
```
(Confirm the names `source_manager`, `SOURCE_ARTWORK_URL`, `_extract_ipa_icon`,
`_repo_owner`, `FileStorage`, `logging`, `os` are all in module scope — they are, per
existing usage in `import_release`.)

## Step 4 — template label (`templates/index.html` ~829)
`Icon URL (optional, for new apps)` → `Icon URL (optional — applied to new and existing apps)`.

## Step 5 — tests (`tests/test_import_release.py`)
Follow the existing patterns: `test_import_existing_app_adds_version` (line 253) for
the existing-app flow and `test_import_new_app_sets_icon_url` (line 511) for stubbing
the download. The seeded existing app is `com.example.app` with `iconURL: ""`.

Add:
```python
def test_import_existing_app_sets_icon_from_url(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest
    ipa_bytes = build_ipa_bytes(bundle_id="com.example.app", version="2.0.0")
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    mp = pytest.MonkeyPatch()
    mp.setattr(app_module.source_manager, "download_icon_from_url", lambda url, bundle_id: "png")
    try:
        status, events = post_and_collect(
            authed_client,
            {"provider": "github", "project": "owner/repo", "bundleIdentifier": "",
             "createIfMissing": False, "iconURL": "https://example.test/logo.png"},
            {"select_candidate": make_fake_select_candidate(candidate),
             "stream_download": make_fake_stream_download(ipa_bytes)},
        )
    finally:
        mp.undo()

    assert status == 200
    assert events[-1]["stage"] == "done", events
    app_info = app_module.source_manager.get_app("com.example.app")
    assert "2.0.0" in [v["version"] for v in app_info["versions"]]
    assert app_info["iconURL"].endswith("/icons/com.example.app/icon.png")


def test_import_existing_app_auto_extracts_icon_when_missing(authed_client, tmp_path):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest
    ipa_bytes = build_ipa_bytes(bundle_id="com.example.app", version="2.0.0")
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    # a real 1x1 PNG on disk for _extract_ipa_icon to return
    png = tmp_path / "extracted.png"
    png.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000154a24f9f0000000049454e44ae426082"))
    mp = pytest.MonkeyPatch()
    mp.setattr(app_module, "_extract_ipa_icon", lambda p: str(png))
    try:
        status, events = post_and_collect(
            authed_client,
            {"provider": "github", "project": "owner/repo", "bundleIdentifier": "",
             "createIfMissing": False},
            {"select_candidate": make_fake_select_candidate(candidate),
             "stream_download": make_fake_stream_download(ipa_bytes)},
        )
    finally:
        mp.undo()

    assert status == 200
    assert events[-1]["stage"] == "done", events
    app_info = app_module.source_manager.get_app("com.example.app")
    assert app_info["iconURL"].endswith("/icons/com.example.app/icon.png")
```
(If `mp.setattr(app_module, "_extract_ipa_icon", ...)` doesn't take effect because the
call site captured the name at def-time, patch it where it's looked up — it's a
module-global referenced by name at call time, so patching `app_module._extract_ipa_icon`
is correct. The 1×1 PNG hex above is a valid file `save_icon_file` will accept.)

## Verify
```bash
grep -c "_apply_icon_to_existing" app.py                                  # 3 (def + 2 calls)
grep -c "applied to new and existing apps" templates/index.html           # 1
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py -q
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q                      # 238 passed (236 + 2 new)
```

## Done criteria
- [ ] Re-importing an existing app with an `iconURL` sets that icon; without one, an icon is auto-detected (IPA → avatar) ONLY when the app had none; a real existing icon is preserved on routine version imports.
- [ ] Auto-import scheduler applies the same auto-detect-when-missing logic for existing apps.
- [ ] A failed icon step never fails the import (helper never raises).
- [ ] `git status --short` shows only `app.py`, `templates/index.html`, `tests/test_import_release.py`; suite `238 passed`.

## STOP conditions
- If `update_app` with `data={}` + `icon_file` turns out to wipe other fields (it should not — it only writes provided keys), STOP and report.
- If the 1×1 PNG is rejected by `save_icon_file`/`allowed_icon_file`, adjust the test fixture (use a real small PNG) rather than changing app code.

## Maintenance note
- The "auto-detect only when missing" guard is deliberate: routine version imports
  must not re-download an avatar or overwrite a hand-set icon every time. Explicit
  `iconURL` is the only thing that overrides an existing icon.
- Note the avatar fallback is the repo **owner's** avatar (e.g. `RyanYuuki.png`), not
  the app's logo — to get the actual logo, supply the raw `.../logo.png` URL.
