# Plan 011: Move IPA storage to the self-hosted Garage S3 object store

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD          # plan refreshed against 309f882
> md5sum app.py                       # expect 2e939fe19fffc9726805ec298a3c3f8d
> wc -l < app.py                      # expect 2737
> .venv/bin/python -m pytest tests/ -q   # expect 39 passed
> ```
> On a mismatch, match the "Current state" excerpts against the live file
> before proceeding; the excerpts are authoritative, the line numbers are a
> convenience.

## Status

- **Priority**: P2 (architecture — no user-visible bug today, but it removes a whole class of them)
- **Effort**: M–L
- **Risk**: MED — this changes where the product's 1.3 GB of payload lives. Mitigated by a default-off config flag and by never deleting local files.
- **Depends on**: 005 (test suite — landed). Interacts with **008**; see "Interaction with Plan 008".
- **Category**: architecture
- **Planned at**: 2026-08-11, `36f27ce`. **Refreshed 2026-08-11** against `309f882` after Plans 006 and 008 landed (+93 lines); `app.py` md5 `2e939fe19fffc9726805ec298a3c3f8d`, 2737 lines.

## Why this matters

`data/ipas/` is 1.3 GB of bind-mounted local disk holding the only copy of every hosted binary. It is not in version control (correctly — it never should be), it is not backed up by anything in this repo, and `compose.yml:6-8` mounts it into a single container. If that disk fails, every hosted app is gone and the catalog keeps advertising URLs that 404.

The infrastructure to fix this already exists and is already running:

- **S3 API**: `https://s3.example.com` — confirmed Garage (`<Region>garage</Region>`, `Forbidden: Garage does not support anonymous access yet`)
- **Web endpoint**: `https://feather-repo.web.example.com` — confirmed serving the `feather-repo` bucket anonymously (a missing key returns `NoSuchKey`, **not** `AccessDenied`, so website access is already enabled)

Both are TLS-terminated by the same openresty on `<proxy-ip>` that already fronts the app, so anything that can fetch `source.json` can fetch an object.

Moving payload to Garage buys replication and durability from the object store instead of from a single ext4 directory, takes 350 MB downloads out of the Flask process entirely, and gives the app a storage layer it can point somewhere else later without rewriting the catalog.

**This plan does not delete anything from `data/ipas/`.** Local files stay exactly where they are until you have independently confirmed Garage is serving them. Reclaiming that disk is a separate, later decision.

## Current state

**Storage layout** — `data/ipas/<bundle_id>/<version>.ipa`, 11 files across 10 directories, 1.3 GB. Verified today with `zipfile.is_zipfile` and cross-referenced against the catalog:

```
ZIP   353664280 cat     com.zhiliaoapp.musically/43.4.0_AC.ipa
BAD        1719 cat     com.instagram.theta/408.1.0_TH.ipa
ZIP   304802859 cat     com.instagram.theta/409.0.0_TH.ipa
ZIP    41981913 ORPHAN  com.ryan.anymex/3.0.3.ipa
BAD        1724 cat     com.fouadraheb.watusi/B_25.36.10_WC.ipa
ZIP   124889851 cat     com.google.ios.youtube/20.49.5.ipa
ZIP   126853402 cat     com.google.ios.youtube/YT_20.49.5_KP.ipa
ZIP   126853402 cat     com.google.ios.youtube/YT_20.49.5_KP_Cracked.ipa
ZIP     4321496 ORPHAN  com.Michael-128.qbitControl/1.3.3.ipa
BAD        1719 cat     com.instagram.ifgram/408.1.0_IF.ipa
ZIP   257550694 cat     com.instagram.ifgram/409.0.0_IF.ipa
```

Three points that shape Step 5:

- **Three `BAD` files are not IPAs.** They are gzip-compressed HTML (a filebin landing page, 5411 bytes decompressed) written to disk by the pre-Plan-007 `shutil.copyfileobj(response.raw, f)` bug, which skipped content-decoding. They are *advertised in the live catalog* and any device that tries to install them fails. **Do not migrate them.** An IPA is a ZIP archive, so `zipfile.is_zipfile()` separates real from fake with no guesswork.
- **Two `ORPHAN` files** are on disk but unreachable from the catalog. `com.Michael-128.qbitControl` is the case-collision twin of the catalogued `com.michael-128.qBitControl` (whose entry points at an external GitHub URL). Migrate orphans, but report them — do not silently drop 46 MB.
- Bundle IDs differing only in case exist on disk. S3 keys are case-sensitive too, so this survives migration unchanged. **Do not normalise case here** — that is a data migration with its own rename step and is explicitly deferred (see `plans/README.md`).

**`app.py:103-111`** — path computation (read-only since Plan 007):

```python
    def get_ipa_path(self, bundle_id, version):
        """Get the file path for an IPA file.

        Read-only: does not create the bundle subdirectory. Callers that
        write to this path are responsible for creating it first.
        """
        bundle_folder = os.path.join(IPA_FOLDER, secure_filename(bundle_id))
        filename = f"{secure_filename(version)}.ipa"
        return os.path.join(bundle_folder, filename)
```

**`app.py:196-202`** — the URL written into the catalog:

```python
    def get_local_ipa_url(self, bundle_id, version, base_url=None):
        """Get the URL path for serving a local IPA file"""
        filename = f"{secure_filename(version)}.ipa"
        path = f"/ipas/{secure_filename(bundle_id)}/{filename}"
        if base_url:
            return f"{base_url.rstrip('/')}{path}"
        return path
```

**`app.py:2431-2447`** — the serving route:

```python
@app.route('/ipas/<bundle_id>/<filename>')
def serve_ipa(bundle_id, filename):
    """Serve IPA files"""
    try:
        # Security: ensure filename is safe
        safe_bundle_id = secure_filename(bundle_id)
        safe_filename = secure_filename(filename)

        filepath = os.path.join(IPA_FOLDER, safe_bundle_id, safe_filename)

        if not os.path.exists(filepath):
            return jsonify({"error": "IPA file not found"}), 404

        return send_file(filepath, mimetype='application/octet-stream', as_attachment=True, download_name=filename)
    except Exception as e:
        logging.error(f"Error serving IPA: {str(e)}")
        return jsonify({"error": str(e)}), 500
```

**The four write/delete sites** you will route through the new abstraction — `save_ipa_file` (`app.py:113-135`), `download_ipa_from_url` (`app.py:137-174`), `delete_ipa_file` (`app.py:176-194`), and `update_version`'s temp-path swap (the `dest_path=` / `os.replace` block Plan 007 introduced, whose two `os.replace` calls are now at `app.py:871` and `884`).

**Repo conventions**: `(bool, message)` tuples from `SourceManager` methods; `logging.error(f"...: {str(e)}")`; plain user-facing sentences for messages; 4-space indent; no type annotations. `requirements.txt` uses exact `==` pins, one per line, no comments. Match all of it.

## The design, and why

**Keep `/ipas/<bundle_id>/<filename>` as the permanent catalog URL. Have that route `302` to the Garage web endpoint.**

So `downloadURL` in `source.json` continues to read `http://feather.example.com/ipas/com.google.ios.youtube/20.49.5.ipa`, and a request to it answers `302 Location: https://feather-repo.web.example.com/ipas/com.google.ios.youtube/20.49.5.ipa`.

Three alternatives were considered and rejected:

- **Put the Garage web URL directly in `downloadURL`.** Simpler, one less hop — but it bakes the storage host permanently into every catalog entry, which is the Plan 008 `Host`-header bug one level up. Your catalog already contains entries frozen against a hostname (`http://<nas-ip>:7000/...`) that only works from one network; that is precisely the failure this avoids. It would also force a rewrite of every existing entry and a re-poll by every device before installs worked again. The redirect migrates with zero catalog changes and zero downtime.
- **Presigned URLs.** Unnecessary: the bucket's web endpoint already serves anonymously. Presigned URLs also expire, and a catalog is a durable document that devices cache — an expiring URL in a persisted `downloadURL` is a correctness bug waiting to happen. (This is also why the redirect must go to the *web* endpoint, not to a signed S3-API URL.)
- **Proxy the bytes through Flask.** Works regardless of client reachability, but pushes every 350 MB install through a single-process Werkzeug dev server plus an extra network hop. It was the fallback for "devices might not reach Garage" — and that turned out not to apply, since the web endpoint is on the same host and TLS terminator as the app itself.

**Storage abstraction.** Add two small classes with the same interface, selected by config:

```python
class LocalIpaStorage:   # exactly today's behaviour
class GarageIpaStorage:  # boto3 against https://s3.example.com
```

Interface — keep it this small:

| Method | Returns | Notes |
|---|---|---|
| `put(src, bundle_id, version)` | `size` (int) or `None` on failure | `src` is a path or a file-like object |
| `delete(bundle_id, version)` | `True` / `False` | |
| `exists(bundle_id, version)` | `bool` | |
| `public_url(bundle_id, version)` | `str` or `None` | `None` for local → `serve_ipa` uses `send_file`; a URL for Garage → `serve_ipa` returns `302` |

`public_url` returning `None` is what lets one `serve_ipa` implementation cover both backends without a branch on backend name.

**`STORAGE_BACKEND` defaults to `local`**, so merging this plan changes nothing at runtime. Flipping to `garage` is a deliberate, reversible act — set the env var, restart, and if anything is wrong set it back. That rollback path is the main reason this plan is MED risk and not HIGH.

## Configuration

Add to `.env.example` — **names only, never values, and never print or log the secret**:

```
STORAGE_BACKEND=
GARAGE_S3_ENDPOINT=
GARAGE_S3_REGION=
GARAGE_S3_ACCESS_KEY_ID=
GARAGE_S3_SECRET_ACCESS_KEY=
GARAGE_BUCKET=
GARAGE_PUBLIC_BASE_URL=
GARAGE_KEY_PREFIX=
```

Read them in the config block at `app.py:21-42`, following the pattern Plan 001 established. That block now reads:

```python
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
SOURCE_FILE = os.path.join(DATA_DIR, "source.json")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
IPA_FOLDER = os.path.join(DATA_DIR, "ipas")
ICON_FOLDER = os.path.join(DATA_DIR, "icons")
BACKUP_FOLDER = os.path.join(DATA_DIR, "backups")
...
SECRET_KEY = os.environ.get("SECRET_KEY")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL")
PORT = int(os.environ.get("PORT", "5000"))
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 2 * 1024 * 1024 * 1024))
```


| Variable | Default | Value for this deployment |
|---|---|---|
| `STORAGE_BACKEND` | `local` | `garage` once Step 7 passes |
| `GARAGE_S3_ENDPOINT` | — | `https://s3.example.com` |
| `GARAGE_S3_REGION` | `garage` | `garage` |
| `GARAGE_S3_ACCESS_KEY_ID` | — | from `garage key create` |
| `GARAGE_S3_SECRET_ACCESS_KEY` | — | from `garage key create` |
| `GARAGE_BUCKET` | — | `feather-repo` |
| `GARAGE_PUBLIC_BASE_URL` | — | `https://feather-repo.web.example.com` |
| `GARAGE_KEY_PREFIX` | `ipas` | `ipas` |

**Refuse to start with `STORAGE_BACKEND=garage` and any of endpoint / bucket / key / secret / public base URL unset.** Log which names are missing — never the values — and raise. A half-configured object store that silently falls back to local disk is how you get a catalog pointing at files nobody wrote.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `.venv/bin/python -m pytest tests/ -q` | 29 passing before, more after |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| S3 endpoint alive | `curl -s https://s3.example.com/ \| grep -o '<Region>garage</Region>'` | `<Region>garage</Region>` |
| Web endpoint alive | `curl -so /dev/null -w '%{http_code}\n' https://feather-repo.web.example.com/` | `404` (bucket reachable, key absent) |
| Object readable | `curl -so /dev/null -w '%{http_code} %{size_download}\n' <GARAGE_PUBLIC_BASE_URL>/ipas/<bundle>/<version>.ipa` | `200` + exact byte count |

## Scope

**In scope**:
- `app.py` — config block; new `LocalIpaStorage` / `GarageIpaStorage`; `save_ipa_file`, `download_ipa_from_url`, `delete_ipa_file`, `update_version`'s swap block, and `serve_ipa`
- `requirements.txt` — add `boto3` (wheels confirmed for **both** cp311 and cp314 — see `plans/README.md` on why that constraint is non-negotiable)
- `.env.example` — the key names above
- `tests/test_routes.py` and/or a new `tests/test_storage.py`
- A new `scripts/migrate_ipas_to_garage.py`

**Out of scope — do NOT touch**:
- **Icons.** `data/icons/` is 268 KB and its own code path. The ask was IPA storage. Moving icons later is easy once this abstraction exists; doing both at once doubles the review surface for 0.02% of the bytes.
- **Deleting anything from `data/ipas/`.** Not in this plan, not "just the corrupt ones", not as cleanup. Local disk is the rollback.
- **Repairing the three corrupt catalog entries.** Step 5 *reports* them; fixing them is a hand edit of `data/source.json` plus re-uploading real binaries, which is a data task, not a code change.
- **Bundle-ID case normalisation.** Deferred migration; never add `.lower()`.
- **`save_source` / `load_source` and the `_lock` / `_backup_source` machinery** — Plan 006 owns those and has landed. Do not touch them, and do not take `self._lock` from inside the storage classes.
- **`resolve_base_url` and `get_local_ipa_url`** — Plan 008 owns those and has landed.
- **Rewriting existing `downloadURL` values.** The whole point of the redirect design is that you don't have to. If you find yourself writing a catalog-rewrite loop, stop — you have taken a wrong turn.
- **Garage cluster configuration** (layout, replication, zones). The cluster is running; this plan consumes it.

## Interaction with Plan 008

**Plan 008 has now landed** (`309f882`). It added a module-level `resolve_base_url()` helper at `app.py:64-77` and switched five call sites to it; `get_local_ipa_url` itself was deliberately left unchanged. So: **leave `get_local_ipa_url` and `resolve_base_url` entirely alone.** This plan does not touch either.

The two base URLs are different and both are needed:

- `PUBLIC_BASE_URL` → the app's own origin, what goes **into the catalog** (`http://feather.example.com`)
- `GARAGE_PUBLIC_BASE_URL` → the object store's origin, what `serve_ipa` **redirects to** (`https://feather-repo.web.example.com`)

They are independent. **Do not collapse them into one variable**, and do not route the Garage URL through `resolve_base_url()` — that helper answers "where is this app?", not "where is the object store?".

## Git workflow

- Branch: `advisor/011-garage-s3`
- **One commit per step group**: (1) config + dependency, (2) storage abstraction + local backend, (3) Garage backend, (4) wire the call sites + `serve_ipa`, (5) migration script, (6) tests. A reviewer needs to read the abstraction separately from the rewiring.

## Steps

### Step 1: Confirm the infrastructure before writing any code

```
curl -s https://s3.example.com/ | grep -o '<Region>garage</Region>'
curl -so /dev/null -w 'web endpoint: %{http_code}\n' https://feather-repo.web.example.com/
```

Expected: `<Region>garage</Region>`, and `web endpoint: 404`.

A `404` here is the **success** case — it is Garage's `NoSuchKey`, which proves the bucket is reachable *and* anonymously readable. A `403` would mean website access is not enabled on the bucket; **STOP and report** if you see one, because the entire redirect design depends on anonymous reads.

### Step 2: Add the dependency and the config

Add `boto3` to `requirements.txt` with an exact `==` pin, matching the file's existing style. Pick the current release and **verify it has wheels for both interpreters before pinning it**:

```
.venv/bin/pip download --no-deps -q --only-binary=:all: --platform manylinux_2_28_x86_64 --python-version 311 -d /tmp/b3 boto3==<version>
.venv/bin/pip download --no-deps -q --only-binary=:all: --platform manylinux_2_28_x86_64 --python-version 314 -d /tmp/b3 boto3==<version>
```

Both must exit 0. (`boto3` is pure Python, so this should pass trivially — run it anyway; it is the check that a bad pin never reaches CI.)

Add the config block per "Configuration" above, including the refuse-to-start validation.

**Verify**:
```
.venv/bin/pip install -q -r requirements.txt && .venv/bin/python -c "import boto3; print(boto3.__version__)"
STORAGE_BACKEND=local DATA_DIR=/tmp/f1 .venv/bin/python -c "import app; print(app.STORAGE_BACKEND)"   # -> local
STORAGE_BACKEND=garage DATA_DIR=/tmp/f2 .venv/bin/python -c "import app"                              # -> raises, naming the missing vars
```
The second must fail loudly, and its message must contain the missing **variable names** and no secret material.

### Step 3: Write the storage abstraction with the local backend

Add both classes near `SourceManager`, and instantiate the selected one once at module scope alongside `source_manager`, which is now at `app.py:920`.

`LocalIpaStorage` must reproduce today's behaviour exactly, including Plan 007's guarantees: create the bundle directory only on write, clean up a partial file if the write raises, and return `None` (not `0`) when the size cannot be determined. Its `public_url()` returns `None`.

**Verify**: `.venv/bin/python -m pytest tests/ -q` → still 29 passing. Nothing is wired up yet, so nothing should change.

### Step 4: Write the Garage backend

Build one module-scope boto3 client:

```python
boto3.client(
    "s3",
    endpoint_url=GARAGE_S3_ENDPOINT,
    region_name=GARAGE_S3_REGION,
    aws_access_key_id=GARAGE_S3_ACCESS_KEY_ID,
    aws_secret_access_key=GARAGE_S3_SECRET_ACCESS_KEY,
)
```

Notes that will save you a debugging session:

- **Key layout**: `f"{GARAGE_KEY_PREFIX}/{secure_filename(bundle_id)}/{secure_filename(version)}.ipa"`. Use `secure_filename` exactly as the local path code does, so keys and paths stay one-to-one and the migration is a straight copy.
- **Use `upload_file` / `upload_fileobj`**, not `put_object`. They multipart automatically above 8 MB; the largest object here is 353 MB and `put_object` would buffer it whole.
- **Set `ExtraArgs={"ContentType": "application/octet-stream"}`** on upload. The web endpoint serves whatever content type the object carries, and iOS is fetching an installable payload.
- **Catch `botocore.exceptions.ClientError`**, and use `head_object` for `exists()` — treat a `404`/`NoSuchKey` as `False` rather than letting it raise.
- `public_url()` returns `f"{GARAGE_PUBLIC_BASE_URL.rstrip('/')}/{key}"`.
- Never log the secret key, and never include response headers that echo credentials in an error message.

**Verify** (needs real credentials in `.env`; skip and say so if you do not have them):
```
.venv/bin/python -c "
import app
s = app.GarageIpaStorage()
print('exists(nonexistent):', s.exists('com.plan011.probe', '0.0.0'))
"
```
→ prints `False`, exit 0. An exception here means credentials or endpoint are wrong — **STOP and report**, do not start guessing at region names.

### Step 5: Write the migration script

`scripts/migrate_ipas_to_garage.py`. Requirements, in priority order:

1. **Dry-run by default.** Uploading requires an explicit `--apply`. Print exactly what would happen and exit 0.
2. **Integrity gate**: skip any file where `zipfile.is_zipfile(path)` is `False`. An IPA is a ZIP; the three known-corrupt files fail this. **Report each skipped file with its path and size** — do not delete it, do not upload it.
3. **Idempotent**: if the key already exists with the same size, skip it. Re-running must be safe and cheap.
4. **Verify after each upload**: `head_object` and compare `ContentLength` against the local size. A mismatch is a hard failure for that file; keep going with the rest and report at the end.
5. **Report orphans** (files on disk not referenced by any `downloadURL` in `data/source.json`) — migrate them, but list them separately so the operator can decide later.
6. **Never delete, move, or modify anything under `data/`.** The script's only write is to Garage.
7. Print a final summary: uploaded / skipped-existing / skipped-corrupt / failed, with byte totals.

**Verify**:
```
.venv/bin/python scripts/migrate_ipas_to_garage.py            # dry run
```
Expected against today's data: **8 files to upload (~1.34 GB), 3 skipped as corrupt, 2 flagged as orphans**, nothing written. If the corrupt count is not 3, re-read "Current state" before continuing — the data has changed since this plan was written.

Then run it for real:
```
.venv/bin/python scripts/migrate_ipas_to_garage.py --apply
```

Then confirm one object end-to-end, out of band:
```
curl -so /dev/null -w '%{http_code} %{size_download}\n' \
  https://feather-repo.web.example.com/ipas/com.google.ios.youtube/20.49.5.ipa
```
→ `200 124889851`. The byte count must match the local file exactly.

### Step 6: Wire the call sites and `serve_ipa`

Route the four write/delete sites through `ipa_storage` instead of touching the filesystem directly. Preserve Plan 007's contracts exactly — in particular `update_version` must still stage the replacement and only swap it in after a fully successful upload, so a failed fetch leaves the previously hosted object intact. For the Garage backend the "staging" is an upload to a `.new` key followed by a server-side copy-then-delete, or an upload straight to the final key only after the source has been fully and successfully read to a local temp file. **Pick the second — it is simpler and keeps the "never destroy the old object until the new one is complete" guarantee.** Say in your report which you chose.

Then `serve_ipa`:

```python
    url = ipa_storage.public_url(safe_bundle_id, version_from(safe_filename))
    if url:
        return redirect(url, code=302)
    # ... existing send_file path, unchanged
```

Keep `secure_filename` on **both** URL segments — that sanitising is what makes the existing path-traversal audit finding a non-finding, and it must survive this refactor. Keep the `404` when the object does not exist: check `exists()` before redirecting, so a missing object still gives a clean `404` rather than a redirect into a Garage `NoSuchKey` page.

`redirect` comes from `flask`; add it to the existing import line.

**Verify**: `.venv/bin/python -m pytest tests/ -q` → all green with `STORAGE_BACKEND` unset (i.e. `local`), because default behaviour is unchanged.

### Step 7: Switch the deployment over

Set the Garage variables and `STORAGE_BACKEND=garage` in `.env` (never commit it), then:

```
docker compose up --build -d && sleep 15
curl -si http://localhost:7000/ipas/com.google.ios.youtube/20.49.5.ipa | head -5
```
→ `HTTP/1.1 302 FOUND` with a `Location:` header pointing at the Garage web endpoint.

```
curl -sL -o /dev/null -w '%{http_code} %{size_download}\n' \
  http://localhost:7000/ipas/com.google.ios.youtube/20.49.5.ipa
```
→ `200 124889851` — the full object, following the redirect.

```
curl -f http://localhost:7000/source.json | python3 -c "import json,sys; print(len(json.load(sys.stdin)['apps']))"
```
→ `8`, and no `downloadURL` anywhere in it should have changed.

**Then the check that actually matters**: on a real device, refresh the source in AltStore/Feather and **start one install**. Nothing else in this plan proves the client follows the redirect. If the install does not start, set `STORAGE_BACKEND=local`, restart, and report — that rollback is why the flag exists.

## Test plan

Add to `tests/` (a new `tests/test_storage.py` is cleaner than growing `test_routes.py`; reuse the `client` fixture pattern from `tests/test_routes.py:51-77`).

**Do not hit real Garage from the test suite.** The existing suite is 29 tests in ~1.2 s with no network, and that property is worth protecting. Use a hand-rolled fake client injected into `GarageIpaStorage` (a small class with `upload_file`, `head_object`, `delete_object`, recording calls). `moto` would also work but pulls in a large dependency for this much coverage — prefer the fake and say so if you disagree.

1. `test_local_storage_roundtrip` — put, exists, size, delete.
2. `test_local_storage_public_url_is_none` — the branch that keeps `serve_ipa` on `send_file`.
3. `test_garage_key_layout` — `put` for bundle `com.example.app` version `1.0.0` produces key `ipas/com.example.app/1.0.0.ipa`. Pin this; it is the contract the migration script depends on.
4. `test_garage_public_url` — returns `<base>/ipas/<bundle>/<version>.ipa`, with no double slash when the base has a trailing slash.
5. `test_serve_ipa_redirects_when_backend_is_garage` — 302, and `Location` is the expected URL.
6. `test_serve_ipa_404_when_object_missing` — still `404`, not a redirect.
7. `test_serve_ipa_uses_send_file_when_backend_is_local` — 200 with the file body; guards the default path.
8. `test_garage_backend_refuses_to_start_unconfigured` — missing config raises, and the message names the missing variables and contains no secret.
9. `test_update_version_preserves_original_on_failed_upload` — the Garage analogue of Plan 007's most important test. A failed upload must leave the existing object in place and return `(False, ...)`.

Also confirm the **29 existing tests still pass unchanged**. If any needs editing, that is a signal you changed default behaviour — stop and re-read Step 3.

## Done criteria

ALL must hold:

- [ ] `.venv/bin/python -m py_compile app.py` exits 0
- [ ] `grep -c "^boto3==" requirements.txt` returns `1`
- [ ] `boto3` pin verified to have both cp311 and cp314 wheels (paste both `pip download` exit codes)
- [ ] `grep -c "STORAGE_BACKEND" .env.example` returns `1`; `.env.example` contains **no values**
- [ ] `git ls-files | grep -c "^\.env$"` returns `0` — the real `.env` is still untracked
- [ ] `grep -c "resolve_base_url" app.py` is unchanged from baseline (`6`) — Plan 008's helper was not touched
- [ ] With `STORAGE_BACKEND` unset: `.venv/bin/python -m pytest tests/ -q` passes, and the **39** pre-existing tests are unmodified
- [ ] With `STORAGE_BACKEND=garage` and config missing: import raises, naming the missing variables, leaking no secret
- [ ] Migration dry run reports 8 uploadable, 3 corrupt-skipped, 2 orphans, and writes nothing
- [ ] After `--apply`: `curl` of the Garage web URL for `com.google.ios.youtube/20.49.5.ipa` returns `200` and exactly `124889851` bytes
- [ ] `data/ipas/` still contains all 11 original files, byte-identical (`find data/ipas -name '*.ipa' | wc -l` → `11`)
- [ ] `/ipas/<bundle>/<file>` returns `302` under `garage`, `200` under `local`
- [ ] `/source.json` still lists 8 apps and **no `downloadURL` value changed** — diff it against a copy taken before Step 7
- [ ] New tests added per the test plan; full suite green
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- Step 1's web endpoint returns `403` rather than `404`. Anonymous read is off and the redirect design does not work; that is a Garage bucket-policy decision, not something to code around.
- The migration dry run reports a corrupt count other than 3, or a total file count other than 11. The data has drifted from what this plan was written against.
- Any uploaded object's `ContentLength` does not match its local size. Do not retry blindly — report which file.
- You need credentials that are not in `.env`. Ask; never invent, and never paste a secret into a report, a commit message, or a test fixture.
- You find yourself rewriting `downloadURL` values in `data/source.json`. Explicitly out of scope — the redirect exists so this is unnecessary.
- You find yourself deleting from `data/ipas/`, including the corrupt files. Out of scope in every case.
- A test needs a live network call to Garage. Restructure with the fake client instead.
- The device install in Step 7 fails. Set `STORAGE_BACKEND=local`, restart, report — do not debug forward on a live service.

## Maintenance notes

- **The rollback is the flag.** `STORAGE_BACKEND=local` plus a restart returns to local disk instantly, and it keeps working only for as long as `data/ipas/` still holds the files. Anyone who later reclaims that disk is removing the rollback — that should be a deliberate decision made after Garage has served real installs for a while, not a tidy-up.
- **Local and Garage will drift once the flag is flipped.** New uploads go only to Garage. If you flip back to `local` after that, recently added apps will 404. Worth a line in the README when the switch is made permanent.
- **This plan makes the app's dependency on Garage's availability a hard one.** Today a dead disk breaks installs; afterwards, a dead Garage or a dead openresty vhost does. That is a better trade (replicated storage vs one ext4 directory) but it is a real change in failure modes, not a pure win.
- **The three corrupt catalog entries remain corrupt.** Plan 007 stopped the code from creating new ones; this plan stops them being copied into the object store; nothing has repaired the live catalog. Repairing means sourcing real binaries for `com.instagram.theta 408.1.0_TH`, `com.instagram.ifgram 408.1.0_IF`, and `com.fouadraheb.watusi B_25.36.10_WC`, or removing those three versions from `data/source.json`. Track it separately.
- **Icons are the obvious follow-up** and become nearly free once this abstraction exists — same interface, `ICON_FOLDER`, an `icons/` key prefix.
- **Reviewer should scrutinise**: that `secure_filename` is still applied to both URL segments in `serve_ipa`; that the default backend really is `local` and the 29 existing tests were not edited to accommodate the change; and that `update_version` still cannot destroy an existing object before its replacement is confirmed complete.
