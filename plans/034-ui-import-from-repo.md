# Plan 034: Add a UI "Import from Repo" button (GitHub + GitLab) with a live progress bar

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 48c6454..HEAD -- app.py templates/index.html scripts/release_source_ingest.py tests/
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 160 passed
> ```
> If `app.py`, the template, or `scripts/release_source_ingest.py` changed since
> `48c6454`, compare the "Current state" excerpts below against the live code
> before proceeding; on a semantic mismatch (the reused engine functions or the
> `add_app_manual`/`add_version` signatures changed), treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: L
- **Risk**: MED–HIGH (new authenticated mutating route that fetches from the network, a streaming response, and new template JS)
- **Depends on**: Plan 029 (the release-import engine it reuses — already merged to `main`)
- **Category**: direction / feature
- **Planned at**: commit `48c6454`, 2026-08-17

## Why this matters

Feather can import a release from GitHub/GitLab only via the cron CLI
(`scripts/release_source_ingest.py`, Plan 029). The operator wants the same
capability **in the admin UI**: pick a provider, type a repo, click Import, and
watch a progress bar while the IPA downloads and publishes.

The heavy logic already exists in Plan 029's engine (release discovery, safe
streamed download with cross-host auth-stripping, IPA validation, `Info.plist`
metadata extraction). The CLI runs it in a *separate process*, which is exactly
why it can't touch the catalog directly — a second process would race the web
app's in-process lock. **A UI button runs inside the Flask process**, so it can
reuse that engine to fetch+validate, then publish through the *existing*
in-process `SourceManager.add_app_manual` / `add_version` — inheriting their
lock, atomic write, size handling, and Telegram notify for free. No second
catalog writer is introduced.

The dead `add_app_from_github` method (deleted in Plan 030) is **not** revived;
this uses Plan 029's far better engine.

## Current state

### The engine to reuse — `scripts/release_source_ingest.py` (do NOT rewrite; extend minimally)

All of these are importable with **no network at import time**:

- `Job` dataclass (line 102): `id, provider, project, bundle_identifier, asset_glob, include_prereleases=False, create_if_missing=False, allowed_download_hosts=frozenset(), name=None, developer_name=None`.
- `ReleaseCandidate` dataclass (line 118): `provider, project, release_id, release_tag, release_time, asset_id, asset_name, declared_size (int|None), download_url, auth_host`.
- `select_candidate(job, session, tokens, timeout=30) -> ReleaseCandidate` (line 496) — resolves the latest eligible release + single matching asset; raises `ProviderError` on none/ambiguous. `tokens` is a dict `{"github": <str|None>, "gitlab": <str|None>}`.
- `stream_download(session, candidate, job, dest_path, tokens, timeout, max_bytes) -> (total_bytes, sha256_hex)` (line 534) — the chunk loop is at lines 616–627 (`for chunk in resp.iter_content(chunk_size=1MB)`), tracking `total`.
- `validate_and_extract_metadata(path, filename, job, candidate=None) -> (bundle_id, version, name)` (line 640) — six structural checks, then at **line 694** it enforces `bundle_id == job.bundle_identifier` and raises `ValidationError` on mismatch.
- Exceptions: `ConfigError, ProviderError, ValidationError, FeatherAuthError` (lines 63–76).
- Constants: `DEFAULT_TIMEOUT = 900`, `DEFAULT_MAX_BYTES = 2*1024**3`.

`scripts/` is **not** a Python package (no `scripts/__init__.py`).

### The in-process publish path — `app.py` (reuse as-is)

- `SourceManager.add_app_manual(self, data, ipa_file=None, download_from_url=False, icon_file=None, download_icon_from_url=False, base_url=None)` (app.py:925) — creates a new app (or appends a version if the bundle already exists). `data` needs `name, bundleIdentifier, developerName, version` (+ optional `localizedDescription, minOSVersion, iconURL`). When `ipa_file` is a Werkzeug `FileStorage` with `.filename` ending `.ipa`, it stores it via the configured backend and sets `downloadURL`.
- `SourceManager.add_version(self, bundle_identifier, version_data, ipa_file=None, download_from_url=False, base_url=None)` (app.py:1120) — appends a version to an existing app. `version_data` needs `version` (+ optional `minOSVersion`).
- Both hold `self._lock` across the whole read-modify-write and call `save_source` (atomic). This is the safety we want to inherit — **publish through these, never write `source.json`/storage directly.**
- `resolve_base_url()` (app.py:117) — the base URL to bake into `downloadURL`.
- `requires_auth` decorator gates the six mutating routes; the new route MUST use it.
- `secure_filename` is already imported (from `werkzeug.utils`).

### The UI tab structure — `templates/index.html`

- Tab buttons at lines 511–514: `<button class="tab" onclick="switchTab('<id>', this)">Label</button>`.
- Tab panes: `<div id="<id>" class="tab-content">…</div>` (e.g. `add-app` at line 518, `manage-apps` at 574).
- `switchTab(id, btn)` toggles `.active`. There is a `showToast(msg, type)` helper used across the file. The "Add App" form's submit handler (search `addAppForm`) is the pattern for posting and showing a toast.

### Repo conventions

- Tests: `tests/test_routes.py` reloads `app` after setting `DATA_DIR`/`ADMIN_PASSWORD`/`SECRET_KEY` (see the `client`/`authed_client` fixtures, lines 73–120). No test makes a real network call — loopback stubs or monkeypatching only.
- Plan 029's tests (`tests/test_release_source_ingest.py`) show how to build fake sessions/responses and real temporary IPA ZIPs — reuse those helper patterns (copy, don't import private helpers) if useful.
- Conventional commits.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `160 passed` before changes |
| Syntax (app) | `.venv/bin/python -m py_compile app.py` | exit 0 |
| Syntax (engine) | `.venv/bin/python -m py_compile scripts/release_source_ingest.py` | exit 0 |
| Engine still green | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q` | `21 passed` (unchanged) |
| New route tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py -q` | new tests pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `160 + N passed` |

## Scope

**In scope** (create/modify only these):
- `scripts/release_source_ingest.py` — two small **backward-compatible** additions (progress callback; split the bundle-match out of validation). Existing behavior and its 21 tests must be unchanged.
- `app.py` — import the engine; add one authenticated streaming route `POST /api/import-release`.
- `templates/index.html` — a new "Import from Repo" tab, its form, a progress bar, and the JS that drives it.
- `tests/test_import_release.py` — new, no-network tests for the route (create).

**Out of scope** (do NOT touch):
- `SourceManager.add_app_manual` / `add_version` and every other existing method — reuse them, do not change them.
- The Plan 029 CLI entrypoints (`process_job`, `main`, `build_arg_parser`, `FeatherClient`) — the UI does not use the HTTP `FeatherClient`; it publishes in-process.
- `requirements.txt` / `requirements-dev.txt` — stdlib + `requests` + `werkzeug` (already deps) are enough. If you think you need a new dep, STOP.
- `compose.yml`, `Dockerfile`, `.dockerignore`, CI — no container changes are needed (the engine script is already in the image; `app.py` imports it via `sys.path`, see Step 2).
- `data/` and any live catalog/IPA.

## Git workflow

- Branch: `advisor/034-ui-import-from-repo`
- Separate logical commits: engine tweaks, then the route, then the template, then tests.
- Do NOT push or open a PR.

## Steps

### Step 1: Confirm baseline

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```
**Verify**: `160 passed`. If not, STOP.

### Step 2: Two backward-compatible engine additions

In `scripts/release_source_ingest.py`:

**(a) Progress callback on `stream_download`.** Add an optional last parameter
`progress_cb=None`. Inside the chunk loop (after `total += len(chunk)` and the
max-bytes check), call it if set:
```python
if progress_cb is not None:
    progress_cb(total, candidate.declared_size)
```
Default `None` means the CLI path is unchanged.

**(b) Split the bundle-match out of validation.** Refactor so the structural
checks + extraction live in a new function that does **not** enforce the match,
and `validate_and_extract_metadata` becomes a thin wrapper that still does:

```python
def extract_ipa_metadata(path, filename, job=None, candidate=None):
    # ... the existing six structural checks + plist extraction ...
    # returns (bundle_id, version, name); raises ValidationError on the
    # structural failures and on missing bundle_id/version, but does NOT
    # compare against any configured bundleIdentifier.
    ...

def validate_and_extract_metadata(path, filename, job, candidate=None):
    bundle_id, version, name = extract_ipa_metadata(path, filename, job, candidate)
    if bundle_id != job.bundle_identifier:
        raise ValidationError(... existing message ...)
    return bundle_id, version, name
```

Keep the existing error messages verbatim so Plan 029's tests still pass.

**Verify**:
```bash
.venv/bin/python -m py_compile scripts/release_source_ingest.py
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q   # still 21 passed
```
If any of the 21 engine tests fail, your refactor changed behavior — STOP and fix so they pass unchanged.

### Step 3: Import the engine into `app.py`

Near the top of `app.py`, after the existing imports, make the sibling
`scripts/` directory importable and import the engine under a short alias. Use a
path relative to `app.py` so it resolves both on the host (repo root) and in the
container (`/app`, where the Dockerfile already copies the script):

```python
import sys
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import release_source_ingest as release_ingest
from werkzeug.datastructures import FileStorage
```

(`os` is already imported. Importing the engine runs only class/function
definitions — no network.)

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -c "import app; print(app.release_ingest.DEFAULT_MAX_BYTES)"` → prints the number, no traceback.

### Step 4: Add the `POST /api/import-release` streaming route

Add an authenticated route that streams newline-delimited JSON (NDJSON) progress
events. Because the body streams, HTTP status is already `200` before work
finishes — so **errors are reported as a final `{"stage":"error"}` line**, not an
HTTP status; the UI reads the last line.

Target shape (adapt names to match the file's style):

```python
@app.route('/api/import-release', methods=['POST'])
@requires_auth
def import_release():
    payload = request.get_json(silent=True) or {}
    provider = (payload.get('provider') or '').strip().lower()
    project = (payload.get('project') or '').strip()
    bundle_id_in = (payload.get('bundleIdentifier') or '').strip()   # optional
    asset_glob = (payload.get('assetGlob') or '*.ipa').strip()
    include_pre = bool(payload.get('includePrereleases'))
    create_if_missing = bool(payload.get('createIfMissing'))
    name_in = (payload.get('name') or '').strip()
    developer_in = (payload.get('developerName') or '').strip()
    hosts_in = payload.get('allowedDownloadHosts') or []
    base_url = resolve_base_url()

    def event(**kw):
        return json.dumps(kw) + "\n"

    def run():
        tmp_path = None
        try:
            if provider not in ('github', 'gitlab'):
                yield event(stage="error", error="Provider must be github or gitlab"); return
            if not project:
                yield event(stage="error", error="Repository is required"); return
            allowed = frozenset(h.strip().lower() for h in hosts_in if h.strip())
            if provider == 'gitlab' and not allowed:
                yield event(stage="error", error="GitLab imports require at least one allowed download host"); return

            # bundle_identifier is required by the engine Job; when the user
            # left it blank we resolve+download first, then read it from the
            # IPA. Use a two-phase approach: build the Job with a placeholder
            # only for selection/download, and enforce the match ourselves.
            job = release_ingest.Job(
                id="ui-import",
                provider=provider,
                project=project,
                bundle_identifier=bundle_id_in or "",
                asset_glob=asset_glob,
                include_prereleases=include_pre,
                create_if_missing=create_if_missing,
                allowed_download_hosts=allowed,
                name=name_in or None,
                developer_name=developer_in or None,
            )
            tokens = {"github": os.environ.get("GITHUB_TOKEN"),
                      "gitlab": os.environ.get("GITLAB_TOKEN")}
            session = requests.Session()

            yield event(stage="resolving")
            candidate = release_ingest.select_candidate(job, session, tokens, timeout=30)
            yield event(stage="resolved", asset=candidate.asset_name,
                        release=candidate.release_tag or candidate.release_id,
                        size=candidate.declared_size)

            fd, tmp_path = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=".ipa"); os.close(fd)

            progress = {"last": 0}
            # stream_download calls progress_cb(total, declared) per chunk; we
            # cannot yield from inside a callback, so record and emit between
            # chunks via a small generator wrapper is complex -- instead emit a
            # coarse-grained final size. (See note below for the streaming
            # variant the UI actually renders.)
            def cb(total, declared):
                progress["last"] = total
            release_ingest.stream_download(
                session, candidate, job, tmp_path, tokens,
                release_ingest.DEFAULT_TIMEOUT, release_ingest.DEFAULT_MAX_BYTES,
                progress_cb=cb,
            )
            yield event(stage="downloaded", bytes=progress["last"])

            yield event(stage="validating")
            bundle_id, version, detected_name = release_ingest.extract_ipa_metadata(
                tmp_path, candidate.asset_name, job, candidate)
            if bundle_id_in and bundle_id != bundle_id_in:
                yield event(stage="error", error=f"IPA bundle id {bundle_id} does not match the id you entered ({bundle_id_in})"); return

            yield event(stage="publishing")
            existing = source_manager.get_app(bundle_id)
            fs = FileStorage(stream=open(tmp_path, "rb"), filename=f"{secure_filename(version)}.ipa")
            if existing:
                ok, message = source_manager.add_version(
                    bundle_id, {"version": version}, ipa_file=fs, base_url=base_url)
            elif create_if_missing:
                if not (name_in and developer_in):
                    yield event(stage="error", error="New apps need a name and developer name"); return
                ok, message = source_manager.add_app_manual(
                    {"name": name_in, "bundleIdentifier": bundle_id,
                     "developerName": developer_in, "version": version},
                    ipa_file=fs, base_url=base_url)
            else:
                yield event(stage="error", error=f"App {bundle_id} is not in the catalog. Tick 'create if missing' with a name + developer to add it."); return

            if ok:
                notify("add_version", f"Imported {bundle_id} {version} from {provider}:{project}\n{base_url}/source.json")
                yield event(stage="done", success=True, message=message, bundleIdentifier=bundle_id, version=version)
            else:
                yield event(stage="error", error=message)
        except (release_ingest.ProviderError, release_ingest.ValidationError, release_ingest.ConfigError) as e:
            yield event(stage="error", error=str(e))
        except Exception as e:
            logging.exception("import-release failed")
            yield event(stage="error", error=str(e))
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try: os.remove(tmp_path)
                except OSError: pass

    return app.response_class(run(), mimetype="application/x-ndjson")
```

**Real progress bar note**: the download is one blocking `stream_download`
call, so the `cb` above only records bytes — it cannot itself `yield` NDJSON. To
drive a *live* bar, refactor the download to emit progress between chunks. The
simplest faithful way: implement the download loop **inside `run()`** using the
same rules as `stream_download` BUT that duplicates the security-critical
redirect/host logic — **do NOT do that.** Instead, have `stream_download`'s
`progress_cb` push onto a `queue.Queue` from a worker thread, and the generator
drains the queue to yield `downloading` events. Target:

```python
import threading, queue
q = queue.Queue()
def cb(total, declared): q.put(("downloading", total, declared))
def worker():
    try:
        release_ingest.stream_download(session, candidate, job, tmp_path, tokens,
            release_ingest.DEFAULT_TIMEOUT, release_ingest.DEFAULT_MAX_BYTES, progress_cb=cb)
        q.put(("ok", None, None))
    except Exception as e:
        q.put(("err", str(e), None))
t = threading.Thread(target=worker, daemon=True); t.start()
while True:
    kind, a, b = q.get()
    if kind == "downloading":
        pct = int(a * 100 / b) if b else None
        yield event(stage="downloading", downloaded=a, total=b, pct=pct)
    elif kind == "ok":
        break
    else:  # err
        yield event(stage="error", error=a); return
```

Use this queue+thread form so the bar updates live while keeping the vetted
download logic in the engine. (The download thread does not touch the catalog;
publication still happens on the request thread after the join, under the lock.)

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 5: Add the "Import from Repo" tab and progress UI

In `templates/index.html`:

1. Add a tab button next to the others (after line 514's Source Info button):
   `<button class="tab" onclick="switchTab('import-repo', this)">Import from Repo</button>`
2. Add a `<div id="import-repo" class="tab-content">` pane with a form
   (`id="importRepoForm"`) containing: a provider `<select>` (github/gitlab);
   `project` text; `bundleIdentifier` text (placeholder "leave blank to
   auto-detect"); `assetGlob` text (value `*.ipa`); `includePrereleases`
   checkbox; `allowedDownloadHosts` text (comma-separated, label it "required
   for GitLab"); `name` + `developerName` text (label "for new apps"); a submit
   button; a hidden progress area:
   `<div id="importProgress" style="display:none"><progress id="importBar" max="100" value="0"></progress> <span id="importStatus"></span></div>`.
3. Add a submit handler that POSTs JSON and reads the NDJSON stream:

```javascript
document.getElementById('importRepoForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = {
        provider: f.provider.value,
        project: f.project.value.trim(),
        bundleIdentifier: f.bundleIdentifier.value.trim(),
        assetGlob: f.assetGlob.value.trim() || '*.ipa',
        includePrereleases: f.includePrereleases.checked,
        createIfMissing: f.createIfMissing.checked,
        name: f.name.value.trim(),
        developerName: f.developerName.value.trim(),
        allowedDownloadHosts: f.allowedDownloadHosts.value.split(',').map(s => s.trim()).filter(Boolean),
    };
    const prog = document.getElementById('importProgress');
    const bar = document.getElementById('importBar');
    const status = document.getElementById('importStatus');
    prog.style.display = ''; bar.value = 0; status.textContent = 'Starting…';
    try {
        const resp = await fetch('/api/import-release', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        while (true) {
            const {value, done} = await reader.read();
            if (done) break;
            buf += dec.decode(value, {stream: true});
            let nl;
            while ((nl = buf.indexOf('\n')) >= 0) {
                const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
                if (!line) continue;
                const ev = JSON.parse(line);
                if (ev.stage === 'downloading') {
                    if (ev.pct != null) { bar.removeAttribute('value'); bar.value = ev.pct; status.textContent = `Downloading ${ev.pct}%`; }
                    else { bar.removeAttribute('value'); status.textContent = `Downloading ${(ev.downloaded/1048576).toFixed(1)} MB`; }
                } else if (ev.stage === 'error') {
                    showToast(ev.error || 'Import failed', 'error'); status.textContent = 'Failed';
                } else if (ev.stage === 'done') {
                    bar.value = 100; showToast(ev.message || 'Imported', 'success'); status.textContent = `Imported ${ev.bundleIdentifier} ${ev.version}`; f.reset();
                } else {
                    status.textContent = ev.stage.charAt(0).toUpperCase() + ev.stage.slice(1) + '…';
                }
            }
        }
    } catch (err) {
        showToast('Import failed: ' + err, 'error');
    }
});
```

(Match the file's existing indentation and the way `addAppForm` is wired. Keep
the no-emoji rule from Plan 025 — plain text only.)

**Verify**: `.venv/bin/python -m py_compile app.py` still 0; and the template
renders (covered by the test in Step 6 that GETs `/`).

### Step 6: Tests — `tests/test_import_release.py` (no network)

Create the file. Monkeypatch the engine's network+download functions on the
reloaded app module so no real network happens. Use `authed_client`-style setup
(copy the fixture pattern from `tests/test_routes.py`). Build a real tiny IPA
zip on disk (a `Payload/X.app/Info.plist` with `CFBundleIdentifier` +
`CFBundleShortVersionString`) — copy the helper shape from
`tests/test_release_source_ingest.py`.

Cover at least:
1. `test_import_existing_app_adds_version` — monkeypatch `release_ingest.select_candidate` to return a fake `ReleaseCandidate`, and `release_ingest.stream_download` to write a real IPA zip to `dest_path` and call `progress_cb`; POST for a bundle already in the seeded catalog; assert the NDJSON stream ends with a `done` event and the catalog gained the version.
2. `test_import_new_app_requires_create_flag` — same but bundle not in catalog and `createIfMissing` false → final event is `error`; catalog unchanged.
3. `test_import_new_app_with_create_flag_and_metadata` — `createIfMissing` true + name/developer → `done`; app created.
4. `test_import_bundle_mismatch_is_rejected` — user-entered `bundleIdentifier` differs from the IPA's → `error`, no catalog change.
5. `test_import_gitlab_requires_allowed_hosts` — provider gitlab, empty `allowedDownloadHosts` → `error` before any network (select_candidate not called).
6. `test_import_provider_error_is_reported` — monkeypatch `select_candidate` to raise `release_ingest.ProviderError` → stream ends with `error`, HTTP 200 (streaming).

Read the streamed body via `resp.data` (test client buffers it) and split on
`\n` into JSON events. Assert on the last non-empty event's `stage`.

**Prove it discriminates**: temporarily make the route ignore `create_if_missing`
(always create) → test 2 fails. Restore.

**Verify**:
```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py -q
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```
Expected: new tests pass; engine suite still `21 passed`; full suite green.

## Test plan

- New file `tests/test_import_release.py`, monkeypatching `select_candidate` /
  `stream_download` on the reloaded `app.release_ingest` so no network runs.
- Reuse the IPA-zip fixture shape from `tests/test_release_source_ingest.py`.
- Assert on the parsed NDJSON event stream (last event `stage`) and on the
  resulting catalog state via `source_manager.get_app(...)`.
- Engine's own 21 tests must remain green (the Step 2 refactor is behavior-preserving).

## Done criteria

- [ ] `POST /api/import-release` exists, is `@requires_auth`, streams NDJSON, and never writes `source.json`/storage directly (publishes via `add_app_manual`/`add_version`).
- [ ] GitHub and GitLab both work; GitLab without `allowedDownloadHosts` is rejected before any network.
- [ ] Blank bundle id auto-detects from the IPA; a supplied bundle id that mismatches is rejected.
- [ ] New apps are created only with `createIfMissing` + name + developer; otherwise a clear error.
- [ ] The temp IPA is deleted in `finally` on every path.
- [ ] `scripts/release_source_ingest.py` change is backward-compatible: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q` → `21 passed`.
- [ ] `.venv/bin/python -m py_compile app.py scripts/release_source_ingest.py` → exit 0.
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → all pass (160 + new).
- [ ] New tab renders (a test GETs `/` and finds `import-repo`).
- [ ] `git status --short` shows only the four in-scope files; `plans/README.md` row updated.

## STOP conditions

Stop and report if:
- Reusing the engine would require duplicating the redirect/host-allowlist download logic into `app.py` (it must stay in `stream_download` — use the queue+thread progress bridge instead).
- `add_app_manual` / `add_version` no longer accept a `FileStorage` `ipa_file` or their signatures differ from "Current state".
- The engine's 21 tests can't be kept green after the Step 2 refactor.
- A new dependency seems required.
- Streaming a response doesn't work under the dev server in tests (the test client should still buffer the full body into `resp.data`).

## Maintenance notes

- This publishes **in-process**, so it is bound to the single-process assumption
  the whole app already documents. If the app ever moves to multi-process WSGI,
  this route is fine (it uses the same in-process lock as every other write) —
  but the cron CLI (Plan 029) remains the right tool for out-of-process use.
- Icons are not fetched here (releases have no reliable icon; Plan 023 rejected
  IPA icon extraction). New apps import icon-less; set the icon afterward via the
  Edit App flow (Plan 032 wired `/api/add-app` icons; this route does not).
- The progress bar reflects the asset's *declared* size; when a provider omits
  it, the bar is indeterminate and shows MB downloaded instead of a percent.
- A reviewer should scrutinize: the temp-file cleanup on every branch, that the
  download thread never touches the catalog, that `select_candidate`/`stream_download`
  were reused (not reimplemented), and that GitLab's host allowlist is enforced.
