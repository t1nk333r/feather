# Plan 046: On repo import, extract the app icon from the IPA (CgBI-aware), with GitHub-avatar fallback

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
> If the import-release route's create branch changed since `c052cb0`, compare the
> "Current state" excerpt against the live code first; on a mismatch, STOP.

## Status

- **Priority**: P2 (imported apps land with no icon — a visible gap)
- **Effort**: M–L (IPA icon extraction incl. Apple CgBI-PNG normalization)
- **Risk**: MED (image parsing of arbitrary IPAs; **fully bounded by a fallback** so an import never fails because of the icon)
- **Depends on**: plans 034/035/042 (the import route it extends — merged)
- **Category**: feature
- **Planned at**: commit `c052cb0`, 2026-08-18
- **Decision (with the maintainer)**: extract the **real app icon from inside the
  IPA**; if extraction yields nothing usable, fall back to the GitHub owner avatar;
  if that's unavailable, no icon. (The app **name** already comes from the IPA's
  `Info.plist` via `extract_ipa_metadata` — this plan adds the **icon**.)

## Why this matters

Imported apps (e.g. AnymeX) currently land with **no icon**. The right icon lives
inside the `.ipa` the importer already downloads. Note: the Telegram bot does NOT
read the icon from the IPA — it uses Telegram's own file thumbnail
(`scripts/telegram_bot_ingest.py:508`, plan 023) — so there is no existing
IPA-icon code to reuse; this plan writes it. The wrinkle: iOS app-icon PNGs inside
`Payload/*.app/` are Apple **"CgBI"-optimized** (a non-standard PNG variant with a
`CgBI` chunk, raw-DEFLATE `IDAT`, byte-swapped **BGRA** channels, and
**premultiplied alpha**) — `Pillow` (already a dependency, `pillow==11.3.0`)
**cannot open them directly**. So the extractor must normalize CgBI back to a
standard PNG. A clean shortcut exists when the IPA ships an `iTunesArtwork` file at
its root (a standard PNG) — prefer that. Everything is best-effort with a fallback,
so a weird IPA degrades to the repo avatar, never a failed import.

## Current state — `app.py`

- Import-release route (~line 1740–1830): downloads the IPA to a temp path
  `tmp_path`, then `bundle_id, version, detected_name = release_ingest.extract_ipa_metadata(tmp_path, candidate.asset_name, job, candidate)`, then the **create branch** (~1815):
  ```python
  new_name = name_in or detected_name or bundle_id
  new_developer = developer_in or _repo_owner(project) or "Unknown"
  new_app = {"name": new_name, "bundleIdentifier": bundle_id,
             "developerName": new_developer, "version": version}
  description = _clean_description(getattr(candidate, "release_body", ""))
  if description: new_app["localizedDescription"] = description
  if icon_url_in: new_app["iconURL"] = icon_url_in
  ok, message = source_manager.add_app_manual(
      new_app, ipa_file=fs, download_icon_from_url=bool(icon_url_in), base_url=base_url)
  ```
  `tmp_path` (the IPA) is in scope here. `provider`/`project` too. `_repo_owner(project)` (plan 042) gives the GitHub owner.
- `SourceManager.add_app_manual(self, data, ipa_file=None, download_from_url=False, icon_file=None, download_icon_from_url=False, base_url=None)` (app.py:948) — it **already accepts `icon_file`**. Read how it consumes `icon_file` (whether a path, an open file, or a werkzeug `FileStorage`) around app.py:985–1000 and pass the extracted icon in exactly that form. `download_icon_from_url` + `data["iconURL"]` is the URL path (used by the avatar fallback).
- `ICON_MIME_TYPES` maps an extension → MIME; `ALLOWED_ICON_EXTENSIONS = {'png','jpg','jpeg','webp','gif'}`. The extracted icon will be a **PNG**, so store it as `png`.
- Tests avoid network by monkeypatching `release_ingest.select_candidate` / `stream_download` / `extract_ipa_metadata` and `source_manager.download_icon_from_url` (see `tests/test_import_release.py`).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `205 passed` before |
| Pillow present | `.venv/bin/python -c "import PIL, PIL.Image; print(PIL.__version__)"` | prints a version |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| Focused | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py -q` | pass |
| Full | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `205 + N passed` |

## Scope

**In scope:**
- `app.py` — a new `_extract_ipa_icon(ipa_path)` helper (module-level, near the
  import route) and its use in the import-release create branch, with the avatar
  fallback.
- `tests/test_import_release.py` — tests (build tiny in-memory IPA zips; no network).

**Out of scope:**
- The add-version (existing app) path — icon set on create only.
- GitLab avatar fallback (GitLab avatars need an API call) — GitLab imports use the
  IPA icon if extractable, else no icon (no avatar).
- `Assets.car`-only apps (modern apps whose icons live only in the compiled asset
  catalog, with no loose `AppIcon*.png`) — if no loose PNG and no `iTunesArtwork`,
  fall through to the avatar. Do NOT add an `Assets.car` parser.
- Changing `add_app_manual`/storage internals, or the standalone scripts.

## The extractor — `_extract_ipa_icon(ipa_path) -> str | None`

Return a path to a **temporary standard-PNG file** (caller deletes it), or `None`.
Never raise. Steps, in order:

1. Open `ipa_path` as a zip (`zipfile.ZipFile`). On any failure → return `None`.
2. **iTunesArtwork shortcut** — if the archive has a root `iTunesArtwork@2x`,
   `iTunesArtwork.png`, or `iTunesArtwork` (case-insensitive, no `Payload/` prefix),
   read its bytes and try `Image.open(BytesIO(bytes))`; if it opens, re-save it as
   PNG to a temp file and return that path.
3. **AppIcon path** — read the app `Info.plist` (`Payload/<X>.app/Info.plist`,
   parsed with `plistlib.loads`). Collect candidate icon base names from, in order:
   `CFBundleIcons.CFBundlePrimaryIcon.CFBundleIconFiles` (list), top-level
   `CFBundleIconFiles` (list, older), `CFBundleIconFile` (string, oldest). For each
   base name, match archive members `Payload/<X>.app/<base>*.png` (covers
   `AppIcon60x60@2x.png` etc.). If no plist names resolve, fall back to matching any
   `Payload/<X>.app/AppIcon*.png`.
4. Pick the **largest** candidate by decoded pixel area (or, if it won't decode
   yet, by uncompressed/declared size), preferring `@3x`/`@2x`.
5. Try `Image.open(BytesIO(bytes))` on it. If it opens (a minority of IPAs ship
   standard PNGs) → re-save as PNG to a temp file, return.
6. If Pillow fails, treat it as **CgBI** and normalize via `_decode_cgbi_png(bytes)`
   (below) → returns an RGBA `PIL.Image`; save as PNG to a temp file, return.
7. Any failure anywhere → return `None`.

### `_decode_cgbi_png(data) -> PIL.Image` (the CgBI reversal)

Apple's `-iphone` pngcrush transform, reversed:
- Parse PNG chunks. Confirm a `CgBI` chunk is present (else raise — not CgBI).
- Read `IHDR` for width/height/bit-depth/color-type (expect 8-bit, color type 6 = RGBA).
- Concatenate all `IDAT` chunk data and **raw-inflate** it:
  `zlib.decompress(idat, -zlib.MAX_WBITS)` (CgBI strips the zlib header).
- **Un-filter** the scanlines per the PNG spec (filter types 0–4: None/Sub/Up/Average/Paeth), 4 bytes/pixel, to get raw pixel rows.
- For every pixel: the stored order is **B,G,R,A** with **premultiplied** RGB.
  Swap to R,G,B and **un-premultiply**: for a in (1..255),
  `r = min(255, r*255 // a)` (and g,b likewise); if `a == 0`, leave `r=g=b=0`.
- Build `Image.frombytes("RGBA", (w, h), bytes(rgba))` and return it.

This routine is fiddly; a reference implementation is the widely-known
"iphone-png-normalizer"/"pngdefry" algorithm — follow that logic. Keep it a single
self-contained function. **Wrap all of `_extract_ipa_icon` so any exception here
just yields `None`** and the caller falls back — correctness of odd IPAs is not
worth a crash.

## Steps

### Step 1: Baseline → `205 passed`. If not, STOP.

### Step 2: Add `_decode_cgbi_png` and `_extract_ipa_icon` (module-level). `py_compile` clean.

### Step 3: Use it in the import create branch
Replace the icon handling in the **create branch** with:
```python
extracted_icon = None
if not icon_url_in:
    extracted_icon = _extract_ipa_icon(tmp_path)   # returns a temp PNG path or None
try:
    if icon_url_in:
        new_app["iconURL"] = icon_url_in
        ok, message = source_manager.add_app_manual(
            new_app, ipa_file=fs, download_icon_from_url=True, base_url=base_url)
    elif extracted_icon:
        # pass the extracted PNG in whatever form add_app_manual's icon_file expects
        ok, message = source_manager.add_app_manual(
            new_app, ipa_file=fs, icon_file=<extracted_icon in the right form>, base_url=base_url)
    else:
        # fallback: GitHub owner avatar (github only)
        owner = _repo_owner(project) if provider == "github" else ""
        if owner:
            new_app["iconURL"] = f"https://github.com/{owner}.png"
            ok, message = source_manager.add_app_manual(
                new_app, ipa_file=fs, download_icon_from_url=True, base_url=base_url)
        else:
            ok, message = source_manager.add_app_manual(new_app, ipa_file=fs, base_url=base_url)
finally:
    if extracted_icon:
        try: os.remove(extracted_icon)
        except OSError: pass
```
Match `icon_file`'s expected form to how `add_app_manual` consumes it (Step's
"Current state" note). Keep the `fs` IPA-upload behavior identical to today.

**Verify**: `py_compile app.py` → exit 0.

### Step 4: Tests (`tests/test_import_release.py`; build tiny IPAs in-memory as zip
bytes; monkeypatch the engine so no network — reuse the file's existing pattern)
1. `test_import_extracts_itunesartwork_icon` — IPA zip with a root `iTunesArtwork` that is a real (Pillow-generated) PNG → created app gets a hosted `/icons/<bundle>/...` icon; assert the stored icon exists.
2. `test_import_extracts_appicon_png` — IPA with `Payload/X.app/Info.plist` naming `AppIcon` and a standard `Payload/X.app/AppIcon60x60@2x.png` → icon extracted and hosted.
3. `test_extract_ipa_icon_cgbi_roundtrips` — a **unit** test of `_decode_cgbi_png`: take a known RGBA image, encode it into a minimal CgBI PNG (BGRA + premultiplied + raw-deflate IDAT + `CgBI` chunk) with a small helper in the test, decode it, and assert the pixels round-trip (within ±1 for the un-premultiply rounding). This proves the reversal without needing a real IPA.
4. `test_import_no_icon_falls_back_to_avatar` — github import, IPA with **no** extractable icon (no iTunesArtwork, no AppIcon PNG) → `download_icon_from_url` is called with `https://github.com/<owner>.png` (monkeypatch it, capture the URL).
5. `test_import_gitlab_no_icon_no_avatar` — gitlab, no extractable icon → no icon set, no crash.
6. `test_import_explicit_icon_url_still_wins` — a form Icon URL beats IPA extraction.

**Prove discrimination**: temporarily make `_extract_ipa_icon` always return `None`
→ test 1/2 fall back (assert they now use the avatar, not the IPA icon), and the
CgBI unit test still stands alone. Restore.

**Verify**: focused import tests; then full suite.

## Done criteria
- [ ] A repo import with no user-supplied icon extracts the app icon from the IPA (iTunesArtwork preferred; else the largest AppIcon PNG, CgBI-normalized) and hosts it; `_decode_cgbi_png` round-trips in a unit test.
- [ ] If no icon is extractable: GitHub → owner avatar; GitLab → no icon. A user-supplied Icon URL always wins. Extraction never crashes an import (all failures → fallback).
- [ ] Temp icon files are always cleaned up.
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (205 + 6).
- [ ] `git status --short` shows only `app.py`, `tests/test_import_release.py`.

## STOP conditions
- `add_app_manual`'s `icon_file` cannot accept a locally-extracted file in any form
  (it only supports an upload/URL) — STOP and report; the fix would need a small
  `add_app_manual` change that's out of this plan's scope.
- Correct CgBI reversal proves infeasible within reason — do NOT ship a
  half-working decoder. Ship steps 1–5 (iTunesArtwork + Pillow-openable AppIcons) +
  the avatar fallback, mark `_decode_cgbi_png` as returning `None`/unsupported, and
  **report that CgBI-encoded icons currently fall back to the avatar** so the
  maintainer can decide. (The fallback keeps every import iconed.)

## Maintenance notes
- Modern apps increasingly ship icons only in `Assets.car` (no loose PNG) — those
  hit the avatar fallback. A future plan could add an asset-catalog reader.
- If icons come out with wrong colors/edges, the suspect is the CgBI un-premultiply
  or the BGRA swap in `_decode_cgbi_png` — the unit test guards it.
- Reviewer: confirm the fallback chain (IPA → avatar → none) always yields a valid
  or absent icon (never a broken `iconURL`), temp files are cleaned up, and the
  CgBI unit test genuinely exercises the reversal.
