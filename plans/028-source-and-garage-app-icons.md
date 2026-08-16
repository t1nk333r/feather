# Plan 028: Publish the supplied source icon and store app icons in Garage

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If
> any STOP condition occurs, stop and report; do not improvise. When done,
> update this plan's row in `plans/README.md` unless the reviewer says they
> maintain the index.
>
> **Drift check (run first)**:
> ```bash
> cd /home/t1nk33r/Documents/feather
> git diff --stat f8a14b4..HEAD -- app.py templates/index.html tests/test_routes.py tests/test_storage.py README.md .env.example scripts/telegram_bot_ingest.py
> wc -l app.py templates/index.html tests/test_routes.py tests/test_storage.py README.md .env.example
> md5sum app.py templates/index.html tests/test_routes.py tests/test_storage.py README.md .env.example
> ```
> Planned-at values are 1,650 / 1,575 / 1,514 / 388 / 134 / 59 lines and md5 values
> `d367841124b85225ef14b6f367c751ea`,
> `b6964246c7414a2aacc45c83ea30f2aa`,
> `4f1761e0f0bd90c6309353c7aa80e806`,
> `c74dda435b563bfa3bb87d395632ad3a`,
> `dd2b90a79f4f803754850107e4be76a1`, and
> `2f85203e670ce876ec1eb6886543087f`. If an in-scope file changed, compare
> the excerpts below with the live code. A semantic mismatch is a STOP
> condition; line-number drift alone is not.

## Status

- **Priority**: P2 — removes the remaining Riley Testut placeholder and puts newly captured app icons on the same durable object store as IPAs
- **Effort**: M
- **Risk**: MED — icon writes, deletes, and the public icon route change backend; mitigated by keeping stable `/icons/...` catalog URLs, migrating before cutover, and retaining local files
- **Depends on**: Plan 011 (Garage storage), Plan 023 (Telegram thumbnail icon flow), Plan 025 (source artwork), all DONE
- **Category**: migration / architecture
- **Planned at**: commit `f8a14b4`, 2026-08-16

## Why this matters

There are two different icon concepts and both need an explicit outcome:

1. The source itself still defaults to Riley Testut's example `OctoSource.png` in
   `SourceManager.initialize_source()`. The requested replacement is
   `https://f002.backblazeb2.com/file/S30000PUBLIC/MEDIA-PUBLIC/feather-tinker-1024.png`.
   A read-only HEAD request on 2026-08-16 returned `200`, `Content-Type: image/png`,
   and `Content-Length: 29711`. Plan 025's old operator note pointed at the local
   static asset and incorrectly claimed the Source Information form exposed
   `iconURL`; it does not.
2. Per-app icons uploaded in the UI or captured from a Telegram document thumbnail
   still land only in `data/icons/<bundle>/icon.<ext>`. Plan 011 moved IPA payloads
   behind Garage but explicitly excluded icons. The current local inventory is four
   files totalling 265,298 bytes. New icons should instead be written to the existing
   bucket under `icons/<bundle>/icon.<ext>` when `STORAGE_BACKEND=garage`.

The source icon is an explicit external URL supplied by the operator. App icons are
owned payloads and continue to use stable app URLs such as
`https://feather.example.com/icons/com.example.app/icon.png`; under Garage that route
redirects to the bucket web endpoint. Do not conflate these two decisions.

## Current state

### Source metadata

`app.py:640-654` initializes a fresh catalog with third-party placeholder artwork:

```python
initial_source = {
    "name": "My AltStore Source",
    ...
    "iconURL": "https://f000.backblazeb2.com/file/rileytestut/ExampleSource/OctoSource.png",
    "headerURL": "https://f000.backblazeb2.com/file/rileytestut/ExampleSource/OceanHeader.png",
    ...
}
```

`app.py:1250-1262` accepts only five source fields, so posting `iconURL` currently
succeeds but silently ignores the value:

```python
for key in ['name', 'subtitle', 'description', 'website', 'tintColor']:
    if key in data and data[key]:
        source_data[key] = data[key]
```

`templates/index.html:710-735` likewise has no source-icon input, and
`loadSourceInfo()` populates only name, subtitle, description, website, and tint.

### App icon capture and serving

All app icon sources converge in `app.py`:

- UI file uploads and Telegram's `FeatherClient.set_icon()` send multipart
  `iconFile` to `/api/update-app`.
- URL-backed icons with `downloadIconFromUrl=true` enter
  `SourceManager.download_icon_from_url()`.
- `save_icon_file()` and `download_icon_from_url()` write directly beneath
  `ICON_FOLDER`; `delete_icon_file()` and `get_local_icon_url()` inspect that
  local directory.
- `serve_icon()` checks `os.path.exists()` and calls `send_file()` directly.

The key local-only excerpt is `app.py:550-559`:

```python
ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'png'
filepath = os.path.join(ICON_FOLDER, secure_filename(bundle_id), f"icon.{ext}")
os.makedirs(os.path.dirname(filepath), exist_ok=True)
file.save(filepath)
```

Telegram icon extraction itself is already correct and remains out of scope.
`scripts/telegram_bot_ingest.py:508-528` resolves `document.thumbnail` from the
self-hosted Bot API and stores `thumb_path`; lines 598-605 call `set_icon()` only when
creating a new app. Once `/api/update-app` uses `icon_storage`, that existing path lands
in Garage without any worker change.

### Existing Garage pattern

Plan 011 established `LocalIpaStorage` / `GarageIpaStorage` and selects one using
`STORAGE_BACKEND`, which defaults to `local`. Garage configuration is already present:

```python
GARAGE_BUCKET = os.environ.get("GARAGE_BUCKET")
GARAGE_PUBLIC_BASE_URL = os.environ.get("GARAGE_PUBLIC_BASE_URL")
GARAGE_KEY_PREFIX = os.environ.get("GARAGE_KEY_PREFIX", "ipas")
```

Match its conventions: `secure_filename()` on every key segment, boto3
`upload_file` / `upload_fileobj`, post-upload `head_object`, log only S3 error codes,
return a clean `404` before redirecting a missing object, and keep local behavior as
the default. `tests/test_storage.py` already provides `FakeS3Client`; extend it rather
than adding `moto` or making network calls.

Current local icon inventory, recorded only as migration evidence:

```text
com.google.ios.youtube/icon.png       1,961 bytes
com.instagram.ifgram/icon.webp        9,792 bytes
com.instagram.theta/icon.webp         9,792 bytes
com.ryan.anymex/icon.png             243,753 bytes
total                                265,298 bytes
```

The verified baseline is:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
# 119 passed, 73 warnings in 3.58s
```

The six tests that bind loopback sockets fail with `PermissionError` in a restricted
sandbox. That is an environment limitation, not a repository failure; run the full
command where loopback binds are allowed. Do not weaken those tests.

## Design decisions

### Keep the app-owned URL stable

An app entry in `source.json` continues to contain `/icons/<bundle>/icon.<ext>` on
`PUBLIC_BASE_URL`. `serve_icon()` behaves like `serve_ipa()`:

- local backend: `200` via `send_file()`;
- Garage backend with an existing object: `302` to
  `<GARAGE_PUBLIC_BASE_URL>/icons/<bundle>/icon.<ext>`;
- missing icon: local `404`, with no redirect to a Garage `NoSuchKey` response.

Do not put Garage's hostname directly into app entries. Stable app-owned URLs keep the
backend reversible and avoid rewriting the catalog when storage moves again.

### Add a dedicated icon abstraction

Add `LocalIconStorage` and `GarageIconStorage`; do not overload `GarageIpaStorage` with
extension-specific behavior. Use this interface:

| Method | Returns | Contract |
|---|---|---|
| `put(src, bundle_id, ext)` | `True` / `False` | write `icon.<ext>` atomically; never removes another variant |
| `delete(bundle_id)` | `True` if any variant existed | delete every allowed `icon.<ext>` variant |
| `cleanup_variants(bundle_id, keep_ext)` | no return contract | best-effort removal of variants other than the catalogued one |
| `exists(bundle_id, ext)` | `bool` | no directory creation or writes |
| `public_url(bundle_id, ext)` | URL or `None` | Garage URL for redirect; `None` for local |

Expose one shared helper, used by the Garage class and migration script:

```python
def garage_icon_key(bundle_id, ext):
    return f"icons/{secure_filename(bundle_id)}/icon.{secure_filename(ext)}"
```

The top-level prefix is deliberately the literal `icons`. Do not reuse
`GARAGE_KEY_PREFIX` (it defaults to `ipas`) and do not add a configurable icon-prefix
environment variable; the requested bucket layout is an exact contract pinned by tests.

### Preserve existing icons during replacement

Upload/write the new extension completely, persist its new URL to `source.json`, and only
then delete old extensions. If an existing `icon.png` is being replaced by `icon.webp` and
either the upload or catalog save fails, `icon.png` and the catalog's old URL must remain
usable. A failed catalog save may leave the new variant as an unreachable orphan; that is
safer than deleting the still-catalogued icon. After the catalog save succeeds, failure to
clean a stale old object should be warned about but must not turn a successful publish into
failure.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Source asset | `curl -fsSI https://f002.backblazeb2.com/file/S30000PUBLIC/MEDIA-PUBLIC/feather-tinker-1024.png` | `200`, `Content-Type: image/png` |
| Syntax | `.venv/bin/python -m py_compile app.py scripts/migrate_icons_to_garage.py` | exit 0 |
| Full tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | all 119 existing plus 15 named new tests pass |
| Focused tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_storage.py tests/test_migrate_icons.py -q` | all pass; no network |
| Plan-only diff check | `git diff --check` | exit 0 |

## Scope

**In scope — the only product files the executor may modify or create:**

- `app.py` — source icon default/allowlist; icon storage classes and wiring; icon route
- `templates/index.html` — source `iconURL` field and load/save binding
- `tests/test_routes.py` — source metadata and Source Information regressions
- `tests/test_storage.py` — local/Garage icon storage and route integration tests
- `scripts/migrate_icons_to_garage.py` — new dry-run-first migration
- `tests/test_migrate_icons.py` — migration tests
- `.env.example` — describe `STORAGE_BACKEND` as selecting IPA and app-icon storage; no new key
- `README.md` — document the icon migration script and link Plan 028 from Garage storage
- `plans/README.md` — status row only

**Out of scope — do not touch:**

- `scripts/telegram_bot_ingest.py` and its tests. Its thumbnail extraction and
  `set_icon()` request already converge on `/api/update-app`.
- `static/icon.svg`, `static/icon.png`, favicons, or Plan 026's theme-aware favicon.
  They remain admin-page assets; the supplied Backblaze PNG is the catalog source icon.
- `headerURL`. The operator supplied a source icon, not header artwork.
- Any `apps[*].iconURL` bulk rewrite. Existing stable `/icons/...` URLs are retained.
- `data/source.json` or any file under `data/`. The operator update goes through the
  authenticated API/UI, and the migration never deletes or modifies local files.
- IPA key layout, `GarageIpaStorage`, `serve_ipa()`, `GARAGE_KEY_PREFIX`, or
  `scripts/migrate_ipas_to_garage.py`.
- `requirements.txt`, `compose.yml`, Dockerfiles, or CI. boto3 and all Garage variables
  already exist; no dependency or configuration key is needed.
- Garage bucket policy, replication, credentials, or any secret value.
- Extracting icons from `Assets.car` or decoding CgBI PNGs. Plan 023 deliberately uses
  Telegram's normal thumbnail and that decision remains unchanged.

## Git workflow

- Branch: `advisor/028-garage-app-icons`
- Match the repository's conventional commits, for example
  `feat(storage): store app icons in Garage` and
  `test(storage): cover Garage icon lifecycle`.
- Keep source metadata, icon storage, migration/tests, and plan status as separate
  logical commits. Do not push or open a PR unless asked.

## Steps

### Step 1: Verify and publish the source icon field

1. Run the source-asset HEAD request from the command table. STOP if it is not a public
   PNG response.
2. In `SourceManager.initialize_source()`, replace only the source-level `iconURL` with
   the exact supplied URL. Leave `headerURL` unchanged.
3. Add `iconURL` to `SourceManager.update_source_info()`'s allowlist. Keep the existing
   lock, backup, and atomic `save_source()` path unchanged.
4. Add a `type="url"`, `name="iconURL"`, `id="sourceIconURL"` field to the Source
   Information form. Populate it in `loadSourceInfo()` and let the existing
   `FormData` submission send it; do not add a second fetch path.

**Verify**:

```bash
rg -n "f002\.backblazeb2\.com/file/S30000PUBLIC/MEDIA-PUBLIC/feather-tinker-1024\.png|sourceIconURL" app.py templates/index.html
```

Expected: one default URL in `app.py`, plus the form and load binding. The Riley
`OctoSource.png` URL must have zero matches in `app.py`. `OceanHeader.png` remains one.

### Step 2: Add local and Garage icon storage

1. Promote the route's extension-to-MIME map to one module-level mapping used by both
   storage and serving. Keep the existing extension allowlist.
2. Add `garage_icon_key()`, `LocalIconStorage`, and `GarageIconStorage` beside the IPA
   storage classes, matching their error-handling and fake-client injection shape.
3. `LocalIconStorage.put()` writes a temporary file in the destination directory and
   commits with `os.replace()`; an exception removes only the temp file. It must never
   truncate the previous final icon on a failed replacement.
4. `GarageIconStorage.put()` uses `upload_fileobj` for multipart/FileStorage input and
   `upload_file` for a staged path, sets the correct image `ContentType`, and verifies
   `ContentLength` with `head_object` before returning success.
5. Implement `cleanup_variants()`, but do **not** call it from `put()`. The storage class
   cannot know whether the caller has successfully persisted the new catalog URL; cleanup
   belongs after `save_source()` in Step 3.
6. Instantiate `icon_storage` from the same `STORAGE_BACKEND` switch as `ipa_storage`.
   Unset/`local` remains today's behavior; `garage` reuses the existing validated Garage
   configuration.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_storage.py -q \
  -k 'local_icon or garage_icon'
```

Expected: all focused icon-storage tests pass without a network call.

### Step 3: Route every app-icon lifecycle operation through `icon_storage`

1. Make `save_icon_file()` return the stored extension on success and `None` on failure.
2. Make `download_icon_from_url()` retain its 30-second timeout, content-type/URL
   extension selection, chunked reads, and `MAX_CONTENT_LENGTH` ceiling, but stage the
   response under `UPLOAD_FOLDER`, then call `icon_storage.put()`. Remove the staging file
   in `finally` on both success and failure. Under Garage, no durable file may remain in
   `data/icons/`.
3. Replace `delete_icon_file()`'s filesystem loop with `icon_storage.delete()`.
4. Replace `get_local_icon_url()` with
   `get_hosted_icon_url(bundle_id, ext, base_url=None)`, which constructs the stable
   `/icons/<safe bundle>/icon.<safe ext>` URL. Update all four callers to pass the
   extension returned by save/download.
5. Rewrite `serve_icon()` to validate both segments, check `icon_storage.exists()` first,
   return `302` when `public_url()` is non-`None`, and otherwise keep the current local
   `send_file()` behavior and MIME type. Do not add authentication.
6. In both `add_app_manual()` and `update_app()`, remember the newly stored extension.
   Call `icon_storage.cleanup_variants(bundle_id, keep_ext)` only after `save_source()`
   returns success. If `save_source()` fails, return failure with the old variants intact.
7. In `delete_app()`, move only the icon deletion until after the catalog removal has been
   saved successfully. Leave Plan 011's IPA deletion behavior untouched; refactoring that
   transaction is a separate task.

The Telegram worker needs no change: its multipart `iconFile` reaches
`save_icon_file()` through `/api/update-app` and therefore follows this storage switch.

**Verify**:

```bash
rg -n "os\.path\.(exists|join).*ICON_FOLDER|os\.listdir\(bundle_folder\)" app.py
```

Expected: filesystem icon operations exist only inside `LocalIconStorage` (and local
`send_file` path construction if needed), never in `SourceManager` lifecycle methods.

### Step 4: Add the dry-run-first icon migration

Create `scripts/migrate_icons_to_garage.py`, modelled on
`scripts/migrate_ipas_to_garage.py`, with these exact contracts:

1. Scan only `data/icons/<bundle_id>/icon.<allowed-ext>`; report and skip anything else.
2. Dry-run by default. Only `--apply` may write to Garage.
3. Use `garage_icon_key()` so migration and runtime key layout cannot drift.
4. Set the same image content type as runtime upload.
5. If a key exists with the same size, skip it. If size differs, upload and verify the
   resulting `ContentLength`; a mismatch is a failure.
6. Print uploaded / skipped-existing / skipped-invalid / failed counts and byte totals.
7. Never delete, move, or modify a local icon.
8. Update `.env.example`'s existing Plan 011 comment so `STORAGE_BACKEND` clearly selects
   both IPA and owned app-icon storage; add no key and no value. Update `README.md`'s
   optional Garage paragraph and repository layout to mention Plan 028 and
   `scripts/migrate_icons_to_garage.py`.

At planning time the dry run should report four uploadable files and 265,298 bytes. New
apps may legitimately change that count before execution; count drift alone is not a
failure. STOP if any existing catalog icon uses a self-hosted `/icons/...` URL but has
neither a matching local file nor an already-present Garage object.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python scripts/migrate_icons_to_garage.py
```

Expected: a dry-run summary and an explicit `nothing written` message. No Garage
credentials are required for dry-run.

```bash
rg -n "migrate_icons_to_garage|028-source-and-garage-app-icons|IPA.*icon" README.md .env.example
```

Expected: the migration script and Plan 028 are discoverable from the README, and the
configuration comment no longer claims the backend is IPA-only.

### Step 5: Add regression tests

Add exactly these tests, following `tests/test_routes.py` fixtures and extending
`tests/test_storage.py::FakeS3Client`. Tests must not call real Garage or the supplied
Backblaze URL.

In `tests/test_routes.py`:

1. `test_fresh_source_uses_feather_tinker_icon`
2. `test_update_source_persists_icon_url_and_preserves_header`
3. `test_source_info_form_exposes_icon_url`

In `tests/test_storage.py`:

4. `test_local_icon_storage_roundtrip`
5. `test_garage_icon_key_and_content_type`
6. `test_garage_icon_public_url`
7. `test_serve_icon_redirects_for_garage`
8. `test_serve_icon_404_when_garage_object_missing`
9. `test_update_app_icon_upload_writes_garage_and_stable_catalog_url` — post multipart
   exactly as Telegram's `set_icon()` does; assert the object key begins with `icons/`,
   while catalog `iconURL` begins with `PUBLIC_BASE_URL + '/icons/'`
10. `test_update_app_icon_replacement_removes_old_extension_after_success`
11. `test_failed_icon_upload_preserves_previous_object_and_catalog_url`
12. `test_failed_catalog_save_preserves_old_icon_and_catalog_url`
13. `test_delete_app_removes_garage_icon`

In `tests/test_migrate_icons.py`:

14. `test_migrate_icons_dry_run_writes_nothing`
15. `test_migrate_icons_apply_is_idempotent_and_verifies_size`

Prove the two load-bearing tests discriminate:

- Temporarily change `garage_icon_key()` from `icons/` to `ipas/`; test 5 must fail.
- Temporarily delete the old object before attempting its replacement; tests 11 and 12
  must fail.
- Restore both changes and rerun the full suite.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

Expected: **134 passed** (119 existing + 15 new), with no existing test edited merely to
accommodate changed behavior. Adjust only fixtures that must inject `icon_storage`, and
name each such adjustment in the execution report.

### Step 6: Migrate before cutover, then update the live source icon

This is an operator step against the real deployment, not a unit-test fixture.

1. Check the live `.env` without printing secrets. If `STORAGE_BACKEND=garage`, run the
   icon migration `--apply` **before restarting onto the new image**. Otherwise the four
   existing stable `/icons/...` URLs would switch to an empty Garage prefix and 404.
2. Run dry-run, then `--apply`, then rerun `--apply`; the second apply must report every
   object as skipped-existing with zero failures.
3. Verify one object directly and through the stable route:

```bash
curl -fsSI https://feather-repo.web.example.com/icons/com.google.ios.youtube/icon.png
curl -sI https://feather.example.com/icons/com.google.ios.youtube/icon.png | sed -n '1p;/^[Ll]ocation:/p'
```

Expected after Garage cutover: direct object `200`; stable route `302` with a Location
under the Garage `icons/` prefix. Under local backend the stable route remains `200`.

4. Log in to the admin UI. In Source Information set only `iconURL` to:

```text
https://f002.backblazeb2.com/file/S30000PUBLIC/MEDIA-PUBLIC/feather-tinker-1024.png
```

5. Submit once, then verify the live document:

```bash
curl -fsS https://feather.example.com/source.json | .venv/bin/python -c \
  "import json,sys; d=json.load(sys.stdin); print(d['iconURL']); print(d['headerURL'])"
```

Expected first line: the exact supplied URL. The second line must be the same header URL
recorded before the edit. Do not bulk-rewrite app entries.

## Done criteria

All must hold:

- [ ] Supplied source PNG returns `200 image/png`; its exact URL is the fresh-catalog default
- [ ] Authenticated Source Information can persist source-level `iconURL`
- [ ] `headerURL` is unchanged, and no app-level `iconURL` is set to the source PNG
- [ ] Runtime Garage key is exactly `icons/<secure bundle>/icon.<allowed ext>`
- [ ] App catalog URLs remain on `PUBLIC_BASE_URL/icons/...`, not Garage's hostname
- [ ] UI uploads, downloaded icons, and Telegram thumbnail uploads all converge on `icon_storage`
- [ ] New extension and catalog URL are committed before stale variants are deleted; upload or catalog-save failure preserves the old icon and URL
- [ ] `serve_icon()` returns local `200`, Garage `302`, and missing-object `404`, with no auth requirement
- [ ] Migration is dry-run-first, idempotent, size-verified, and never changes `data/icons/`
- [ ] `.env.example` changes comments only; it adds no key or value
- [ ] `git diff requirements.txt compose.yml scripts/telegram_bot_ingest.py` is empty
- [ ] `.venv/bin/python -m py_compile app.py scripts/migrate_icons_to_garage.py` exits 0
- [ ] Full suite reports 134 passed; both deliberate break tests fail as described, then pass after restoration
- [ ] `git status --short` contains only in-scope files plus the executor's permitted `plans/README.md` status update

## STOP conditions

Stop and report instead of improvising if:

- The supplied source URL does not return a public PNG.
- Implementing source `iconURL` requires a direct or scripted rewrite of `data/source.json`.
  The authenticated update path must own the change and preserve backups/atomic writes.
- You are about to change `headerURL` or any app entry to the supplied source-icon URL.
- `STORAGE_BACKEND=garage` is live and existing icons have not been migrated before
  restart. Do not deploy into a known icon outage.
- An app has a self-hosted catalog icon URL but no corresponding local or Garage object.
  Report the bundle and URL; do not invent an icon.
- Any icon key begins with `ipas/`, omits the literal `icons/` prefix, or contains an
  unsanitized bundle/extension segment.
- A failed replacement deletes or changes the prior icon.
- A test needs real Garage, real Backblaze, or any secret value.
- You are tempted to change Telegram thumbnail extraction, add a CAR parser, or decode
  CgBI files. Those are rejected/out of scope.
- Any of the 119 existing tests fails outside an explicitly documented fixture injection.
- Completing the work appears to require a dependency, new environment variable, bucket
  policy change, or IPA-storage refactor.

## Maintenance notes

- `STORAGE_BACKEND` now selects both IPA and owned app-icon storage. Changing it back to
  `local` after new Garage-only uploads may make recent icons unavailable locally, just as
  Plan 011 warns for recent IPAs. Keep local files until rollback is deliberately retired.
- App icon URLs stay stable across storage moves because the catalog points at Feather's
  `/icons/...` route. Preserve that indirection in future storage work.
- The direct Backblaze URL is intentionally different: it is the operator-selected source
  artwork, not an app-owned payload. If it ever changes, update the fresh-catalog default
  and live source metadata together, but never bulk-rewrite app icons.
- Garage must serve the object `ContentType` written at upload. Reviewers should check the
  MIME mapping, post-upload size verification, and that missing objects 404 before redirect.
- Replacement cleanup is best-effort only after success. Stale variants cost a few KB and
  are unreachable; preserving the visible new icon is more important than turning cleanup
  into a publish failure.
