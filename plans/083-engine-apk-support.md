# Plan 083: Teach the release-import engine to select, validate, and publish APKs

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat dc43400..HEAD -- scripts/release_source_ingest.py app.py Dockerfile`
> If any of those changed since this plan was written, compare the "Current
> state" excerpts against the live code before proceeding; on a mismatch,
> treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: L
- **Risk**: MED
- **Depends on**: none (073 Android repo and 068 shared IPA preflight are both DONE)
- **Category**: direction
- **Planned at**: commit `dc43400`, 2026-09-01

## Why this matters

This repository serves both an iOS AltStore source and an Android F-Droid
repository (plan 073), and the Telegram bot already ingests both `.ipa` and
`.apk` (`scripts/telegram_bot_ingest.py:553-558`). The repo-import path did
not follow: `scripts/release_source_ingest.py:725-727` rejects any release
asset whose filename does not end in `.ipa`. So a GitHub project that ships
an Android build every release — the common case for the Flutter apps this
catalog tracks — can be imported by forwarding the APK to a Telegram bot by
hand, but not by the automation built for exactly that job.

This plan changes only the shared engine. It is the prerequisite for plan
084, which wires the same capability into the admin UI's "Import from Repo"
and the auto-import watcher. Splitting it this way mirrors how plans 048
(backend) and 049 (UI) were split for auto-import.

**A decided trade-off, not a finding**: the maintainer chose *infer the
platform from the asset's file extension, and let one job publish both*,
over an explicit per-job `platform` field. A cross-platform repo is one job,
not two. The cost is that the engine's current "exactly one matching asset"
rule has to become "at most one matching asset **per platform**" — do not
simplify this back to a single candidate.

## Current state

### The `.ipa` gate that blocks everything

`scripts/release_source_ingest.py:720-737`:

```python
def inspect_ipa_metadata(path, filename, job, candidate=None):
    """Run shared IPA preflight and reject binaries explicitly marked tvOS."""
    release_ref = ""
    if candidate is not None:
        release_ref = f" (release {candidate.release_tag or candidate.release_id})"
    if not filename.lower().endswith(".ipa"):
        raise ValidationError(
            f"job {job.id}{release_ref}: asset filename {filename!r} does not end in .ipa"
        )
    label = f"job {job.id}{release_ref}"
    try:
        inspection = inspect_ipa(path, label)
    except InspectionError as exc:
        raise ValidationError(str(exc))
    if inspection.platform == "tvos":
        raise ValidationError(f"{label}: tvOS binaries are not supported")
```

### The job schema is iOS-shaped

`scripts/release_source_ingest.py:107-121`:

```python
@dataclass(frozen=True)
class Job:
    """One validated manifest entry."""

    id: str
    provider: str
    project: str
    bundle_identifier: str
    asset_glob: str
    asset_exclude_glob: str = None
    include_prereleases: bool = False
    create_if_missing: bool = False
    allowed_download_hosts: frozenset = field(default_factory=frozenset)
    name: str = None
    developer_name: str = None
```

### Selection enforces exactly one asset, and returns exactly one candidate

`scripts/release_source_ingest.py:411-421` (GitHub; GitLab is the same shape
at `:485-495`, over `release["assets"]["links"]` instead of `["assets"]`):

```python
        matches = [a for a in assets if _asset_match_state(job, a.get("name"))["matched"]]
        if not matches:
            continue
        if len(matches) > 1:
            raise ProviderError(
                f"job {job.id}: release "
                f"{release.get('tag_name') or release.get('id')} has {len(matches)} "
                f"assets matching {job.asset_glob!r}, expected exactly one"
            )
        asset = matches[0]
```

`select_candidate` at `:572-591` dispatches by provider and raises when the
result is `None`.

### `process_job` is one long iOS pipeline

`scripts/release_source_ingest.py:983-1131`. The load-bearing parts:

- `:1004` `catalog_app = feather.get_app(job.bundle_identifier)`
- `:1013-1016` skip when `_catalog_has_version(catalog_app, recorded["version"])`
- `:1043` `tmp_path = os.path.join(tmp_dir, "download.ipa")` — the extension is hardcoded
- `:1051` `inspection = inspect_ipa_metadata(...)`, then a `bundle_id != job.bundle_identifier` check
- `:1093-1104` publish via `feather.add_version(...)` / `feather.add_app(...)`
- `:1114` `_advance_state(state, job, candidate, version, sha256_hex)`
- `:1078-1087` and `:1119-1129` `_report_provenance(...)` on skip and on publish

`_advance_state` at `:943-953` stores one record per job id:

```python
def _advance_state(state, job, candidate, version, sha256_hex):
    state.setdefault("jobs", {})[job.id] = {
        "provider": job.provider,
        "project": job.project,
        "releaseId": candidate.release_id,
        "assetId": candidate.asset_id,
        "bundleIdentifier": job.bundle_identifier,
        "version": version,
        "sha256": sha256_hex,
        "publishedAt": _utcnow_iso(),
    }
```

### The APK inspector exists, but in the wrong place

`app.py:2881-2911` defines `_inspect_apk(path)`, returning
`{package, version_code (int), version_name, min_sdk, target_sdk, app_name}`
and raising `ValueError` with an operator-readable message. It imports
`pyaxmlparser` lazily (`app.py:2889`) to keep app import fast for tests.
The engine cannot import from `app.py`.

**The convention to follow already exists**: plan 068 extracted the IPA
inspector to `scripts/ipa_inspection.py`, which exposes `InspectionError`,
the frozen `IpaInspection` dataclass, and `inspect_ipa(path, label="IPA")`.
The engine imports it at `scripts/release_source_ingest.py:48`; `app.py`
reaches it by putting `scripts/` on `sys.path` at `app.py:35-36`. Mirror
that exactly for APKs.

`Dockerfile:24-25` copies shared modules **individually**, by design (the
`.dockerignore` is deny-by-default):

```dockerfile
COPY scripts/release_source_ingest.py ./scripts/release_source_ingest.py
COPY scripts/ipa_inspection.py ./scripts/ipa_inspection.py
```

### The Android publish endpoint the engine must call

`app.py:4778-4840`, `POST /api/android/add-apk`, `@requires_auth`. It accepts
a multipart `apkFile`, optional form field `package` (a hint — mismatch is
rejected), and returns JSON. **It is already idempotent**: a re-post of a
version that exists returns HTTP 200 with `{"success": true, "added": false,
"message": "Already present: ..."}`. Do not build a second dedupe on top of
that; read `added`.

The engine's HTTP client to extend is `FeatherClient` at
`scripts/release_source_ingest.py:781`, whose existing methods are
`set_preflight` (`:790`), `get_app` (`:793`), `add_version` (`:814`), and
`add_app` (`:839`). The bot's equivalent, which you should match in shape,
is `scripts/telegram_bot_ingest.py:489-497`:

```python
    def add_apk(self, path):
        with open(path, "rb") as fh:
            files = {"apkFile": (os.path.basename(path), fh)}
            resp = self.session.post(
                f"{self.base_url}/api/android/add-apk", files=files
            )
        if resp.status_code == 401:
            raise FeatherAuthError("feather /api/android/add-apk returned 401")
```

### Conventions this plan must honour

- **Security invariants are not negotiable.** `stream_download` enforces an
  HTTPS + host-allowlist check on every redirect hop and drops
  `Authorization`/`PRIVATE-TOKEN` when a redirect crosses hosts
  (`scripts/release_source_ingest.py:617-630` and `:672`). You are changing
  *what* is downloaded, never *how*. Do not touch those functions.
- Errors that an operator should read are `ValidationError` / `ProviderError`
  / `ConfigError`; the CLI logs them through `_log_job_error`.
- Every publish and skip reports provenance via `_report_provenance`
  (plan 071). New Android outcomes must report it too.

## Commands you will need

There is no `.venv` in a fresh checkout; create one first.

| Purpose | Command | Expected on success |
|---|---|---|
| Setup | `python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt` | exit 0 |
| Focused tests | `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_release_source_ingest.py -q -p no:cacheprovider` | all pass |
| Full suite | `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | `307 passed, 1 skipped` before your changes; strictly more passing after |
| Scope check | `git status --porcelain` | only in-scope files |

`ADMIN_PASSWORD` is mandatory — the app refuses to boot without it.

## Scope

**In scope**:
- `scripts/apk_inspection.py` (create)
- `scripts/release_source_ingest.py`
- `app.py` — **only** to replace the body of `_inspect_apk` with a delegation
  to the new shared module. No other edit to `app.py` belongs in this plan.
- `Dockerfile` — one `COPY` line for the new module
- `tests/test_release_source_ingest.py`
- `tests/test_apk_inspection.py` (create)

**Out of scope** (do NOT touch):
- `app.py`'s `/api/import-release` route, `_run_auto_import_job`,
  `_validate_auto_import_job`, and `_release_job_from_payload` — those are
  **plan 084**. This plan stops at the engine.
- `templates/index.html` — also plan 084.
- `scripts/telegram_bot_ingest.py` — it already does APKs; leave it alone.
- `stream_download`, `_host_allowed`, `_validate_download_host`, and the
  redirect logic — security-critical and already correct.
- `scripts/ipa_inspection.py` — the iOS path must not change behaviour.
- `Dockerfile.bot`, `Dockerfile.fdroid`, `compose.yml`.

## Git workflow

- Branch: `advisor/083-engine-apk-support`
- Conventional commits, one per step. Recent examples from `git log --oneline`:
  `fix(compose): run ingest worker as uid 101 (plan 082)`,
  `feat(bot): ingest Android APKs from Telegram`.
  Use `feat(import): ...` / `refactor(import): ...` with a `(plan 083)` suffix.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Extract the APK inspector into a shared module

Create `scripts/apk_inspection.py` modelled on `scripts/ipa_inspection.py`.
It must expose:

- `class ApkInspectionError(RuntimeError)`
- `@dataclass(frozen=True) class ApkInspection` with fields
  `package, version_code (int), version_name, min_sdk, target_sdk, app_name`
- `def inspect_apk(path, label="APK") -> ApkInspection`, raising
  `ApkInspectionError` with the label prefixed onto the message

Move the logic from `app.py:2881-2911` verbatim — the same validation order
(`is_valid_APK` → `ANDROID_PACKAGE_RE` → integer `version_code` → `> 0`) and
the same lazy `from pyaxmlparser import APK` import inside the function.
`ANDROID_PACKAGE_RE` must move or be duplicated into the new module; do not
import it back out of `app.py`.

Then rewrite `app.py`'s `_inspect_apk` to delegate, preserving its existing
contract exactly — it must still return a **dict** with the same keys and
still raise **`ValueError`**, because `app.py:4833` and
`android_repo.add_apk` depend on both:

```python
def _inspect_apk(path):
    """Read identity and version out of an APK's binary AndroidManifest.

    Thin wrapper over scripts/apk_inspection.py (shared with the release
    importer). Returns the same dict and raises the same ValueError as
    before, so every existing caller is unaffected.
    """
    try:
        inspection = inspect_apk(path)
    except ApkInspectionError as e:
        raise ValueError(str(e))
    return {
        "package": inspection.package,
        ...
    }
```

Add `COPY scripts/apk_inspection.py ./scripts/apk_inspection.py` to
`Dockerfile` immediately after the `ipa_inspection.py` line at `:25`.

**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_android.py -q -p no:cacheprovider`
→ all pass, unchanged count. The Android tests exercise `_inspect_apk` through
`add_apk`; they are your proof the extraction was behaviour-preserving.

**Verify**: `./.venv/bin/python -c "import sys; sys.path.insert(0,'scripts'); import apk_inspection; print(apk_inspection.inspect_apk.__name__)"` → prints `inspect_apk`

### Step 2: Add `package` to `Job` and make the identity fields optional-but-required-in-pairs

In `scripts/release_source_ingest.py`, add `package: str = None` to the `Job`
dataclass and change `bundle_identifier` to default to `None`.

Update `parse_manifest_dict` (`:228`) so that:

- `bundleIdentifier` is required **only** if the job can publish iOS
- `package` is a new optional string, validated against the same Android
  package rule the inspector uses
- **at least one of the two must be present** — a job with neither is a
  `ConfigError` naming the job id
- `assetGlob` keeps its current default of `*.ipa`; a job that wants both
  platforms sets something like `*.{ipa,apk}` or `AnymeX*`

**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_release_source_ingest.py -q -p no:cacheprovider`
→ all pass. Existing manifests that specify only `bundleIdentifier` must keep
working unchanged — that is the backward-compatibility gate.

### Step 3: Make selection return at most one candidate per platform

Add a module-level helper:

```python
def _platform_for_asset(name):
    """ios | android | None, inferred from the asset filename extension."""
```

Give `ReleaseCandidate` a `platform` field (`"ios"` or `"android"`).

Rewrite `github_select_candidate` and `gitlab_select_candidate` to return a
**list** of candidates for the first eligible release that yields any match:

- group matched assets by `_platform_for_asset`
- an asset whose extension is neither `.ipa` nor `.apk` is **not** a match —
  ignore it rather than failing, so `*` globs and checksum files coexist
- more than one match **within a single platform** keeps the current
  `ProviderError`, with the message naming the platform
- skip a platform the job has no identity for (no `package` → ignore APKs)

Update `select_candidate` (`:572`) to return that list and to raise its
existing `ProviderError` only when the list is empty. Keep the exception type
and keep the message recognisable.

**STOP** if you find a caller of `select_candidate` outside this file that
this plan does not list — plan 084 owns `app.py`'s two callers
(`app.py:3249` region and `app.py:3610` region) and they must keep working
against the **old** single-candidate contract until 084 lands. To guarantee
that, keep a thin `select_candidate_single(job, ...)` that returns the first
candidate and have `app.py`'s existing callers bind to it. Do not edit
`app.py` beyond that one-symbol rename.

**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_release_source_ingest.py tests/test_import_release.py tests/test_auto_import.py -q -p no:cacheprovider`
→ all pass. The last two prove you did not break the app's callers.

### Step 4: Add the APK validation path

Add `inspect_apk_metadata(path, filename, job, candidate=None)` beside the
existing `inspect_ipa_metadata`, symmetrical with it:

- reject a filename not ending in `.apk` with the same message shape
- call `inspect_apk` from the new shared module, translating
  `ApkInspectionError` into `ValidationError`
- when `job.package` is set and the APK's package differs, raise
  `ValidationError` naming both — mirroring the existing
  `bundle_id != job.bundle_identifier` check at `:1053-1057`

Leave `inspect_ipa_metadata` untouched.

**Verify**: covered by the tests you write in the Test plan; run
`ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_release_source_ingest.py -q -p no:cacheprovider`

### Step 5: Add `FeatherClient.add_apk`

Add `add_apk(self, path, package=None)` to `FeatherClient`
(`scripts/release_source_ingest.py:781`), posting multipart to
`/api/android/add-apk` exactly as `scripts/telegram_bot_ingest.py:489-497`
does, sending `package` as a form field when set.

Return `(ok, message, added)` where `added` is the route's `added` boolean, so
the caller can tell "published" from "already present" without guessing.
Raise `FeatherAuthError` on 401, matching the sibling methods.

**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_release_source_ingest.py -q -p no:cacheprovider`

### Step 6: Branch `process_job` per platform

Restructure `process_job` (`:983`) to iterate the candidate list. Extract the
per-candidate work into a helper so the two platform paths do not become one
tangled function.

For each candidate:

- **iOS** — the existing path, unchanged in behaviour.
- **Android** —
  - temp file suffix `.apk`, not the hardcoded `download.ipa` at `:1043`
  - validate with `inspect_apk_metadata`
  - publish with `feather.add_apk`; treat `added == false` as `summary.skipped`,
    not a failure — the endpoint is already idempotent
  - never call `feather.get_app` or `_catalog_has_version`; those read the
    **iOS** catalog and mean nothing for an APK
  - report provenance with `"platform": "android"` and the APK's
    `versionCode`/`versionName`

Make `_advance_state` (`:943`) key its record per platform so an iOS publish
cannot mask a pending Android one. Bump the state structure deliberately and
**tolerate reading an old-shape state file** — a deployment already has one
at `data/release-import/state.json`.

`--dry-run` must report each platform separately and, as today, perform no
network write and no state write.

**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider`
→ strictly more than 307 passing, 1 skipped, zero failures.

## Test plan

Add to `tests/test_release_source_ingest.py`, matching its existing fixture
style (no real network — the suite makes no real provider calls, and new tests
must not either):

- **Selection**: a release with both an `.ipa` and an `.apk` yields two
  candidates with the right `platform` values; one with two `.apk`s raises
  `ProviderError`; one with an `.apk` but a job with no `package` yields only
  the iOS candidate; a release with `.ipa`, `.apk`, and `.sha256` ignores the
  checksum file rather than failing.
- **Validation**: `inspect_apk_metadata` rejects a non-`.apk` filename;
  rejects a package mismatch against `job.package`; accepts the happy path.
- **Publish**: `process_job` with a stub `FeatherClient` publishes an APK and
  advances state; a second run with `added: false` counts `skipped` and does
  not fail; a job publishing both platforms from one release records both.
- **Backward compatibility**: an iOS-only manifest and an old-shape
  `state.json` still work end to end.

New `tests/test_apk_inspection.py`: the shared module raises
`ApkInspectionError` on a non-APK file and on an invalid package name. Model
it on however `tests/test_ipa_inspection.py` builds its fixtures.

**Prove each new test discriminates** — this repo's convention, and it has
caught real bugs: break the behaviour the test guards, watch it fail, restore
it. Say in your report which tests you did this for.

## Done criteria

ALL must hold:

- [ ] `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → 0 failures, more than 307 passing
- [ ] `grep -n "download.ipa" scripts/release_source_ingest.py` returns nothing
- [ ] `scripts/apk_inspection.py` exists and `app.py` imports from it rather than defining the pyaxmlparser logic itself: `grep -c pyaxmlparser app.py` → `0`
- [ ] `grep -n "apk_inspection" Dockerfile` returns the new COPY line
- [ ] A job manifest with only `bundleIdentifier` behaves exactly as before (a test asserts it)
- [ ] `git status --porcelain` lists no file outside the in-scope list
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- Making selection return a list would require editing `app.py` beyond the
  single `select_candidate_single` binding described in Step 3. That means
  plan 084's boundary is wrong and both plans need rewriting.
- `pyaxmlparser` cannot be imported in the test environment, or has no wheel
  for the interpreter you are on. Every pin here must have wheels for **both**
  cp311 (the container) and cp314 (the test host); report rather than
  swapping the library.
- The full suite does not report `307 passed, 1 skipped` **before** you make
  any edit — the baseline moved and this plan's numbers are stale.
- Any change appears to require touching `stream_download` or the redirect
  host-allowlist logic.
- You discover the `/api/android/add-apk` route is not in fact idempotent —
  Step 6's skip accounting depends on it.

## Maintenance notes

- The extension→platform inference is the single point where a new artifact
  type would be added. Keep `_platform_for_asset` the only place that knows
  about file extensions.
- A reviewer should scrutinise three things: that the iOS path's behaviour is
  byte-for-byte unchanged, that no new code path can skip the redirect
  host-allowlist, and that the new tests assert something that actually fails
  when the feature is removed.
- Deferred to plan 084 on purpose: the admin UI, the auto-import watcher's job
  schema, and the `/api/import-release` route.
- Not addressed here: Android metadata (name, summary, categories) on a
  created F-Droid app still defaults the way `add_apk` defaults it. Enriching
  that from the release body is a separate, optional idea.
