# Plan 085: Let asset selection use a regular expression, so one APK can be pinned out of many

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat fd38864..HEAD -- scripts/release_source_ingest.py app.py templates/index.html`
> If any of those changed since this plan was written, compare the "Current
> state" excerpts against the live code before proceeding; on a mismatch,
> treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: 083, 084 (both DONE and merged)
- **Category**: direction
- **Planned at**: commit `fd38864`, 2026-09-02

## Why this matters

Asset selection matches with `fnmatch` globs, and plan 083 requires **at most
one matching asset per platform**. Real Android releases break that pairing.
The maintainer's own tracked project, `RyanYuuki/AnymeX`, ships one APK per
ABI in a single release — verified live against the running instance:

```
AnymeX-Android-arm64-v8a.apk        37702413 bytes   platform: android
AnymeX-Android-armeabi-v7a.apk      36621609 bytes   platform: android
```

A glob of `*.apk` therefore fails the whole job with "N android assets
matching ... expected at most one". A glob cannot express "the arm64 one and
only the arm64 one" without relying on the filename happening to be
prefix-distinguishable, and globs have no alternation across the varied
naming schemes upstream projects use.

A regular expression solves it directly: `.*arm64-v8a\.apk` pins exactly one
asset. **The one-per-platform rule stays** — it is the guard that stops an
ambiguous release publishing the wrong binary, and the maintainer explicitly
chose to keep it rather than publish every ABI variant.

Out of scope by that same decision: publishing multiple APKs per release.
Revisit only after a real single-APK import has landed.

## Current state

### The matcher — the only place patterns are applied

`scripts/release_source_ingest.py:423-431`:

```python
def _asset_match_state(job, name):
    """Return provider-neutral include/exclude selector state for one asset."""
    normalized = (name or "").lower()
    included = fnmatch.fnmatch(normalized, job.asset_glob.lower())
    excluded = bool(
        job.asset_exclude_glob
        and fnmatch.fnmatch(normalized, job.asset_exclude_glob.lower())
    )
    return {"included": included, "excluded": excluded, "matched": included and not excluded}
```

Both provider selectors and the `/inspect` preview go through this one
function, so changing it changes every consumer consistently. Note the
current semantics, which the new mode must mirror: **case-insensitive**, and
`fnmatch` matches the **whole** name, not a substring.

### Where the pattern is validated or defaulted

Four sites, all of which need the new field threaded through:

- `scripts/release_source_ingest.py:297` — `asset_glob = _require_nonempty_str(raw, "assetGlob", job_id)` (manifest path), constructed into `Job` at `:341`
- `app.py:3170-3172` — `_validate_auto_import_job`: `asset_glob = (raw.get('assetGlob') or '*.ipa').strip()`, then `raise ValueError("assetGlob is required")`
- `app.py:3484` — the watcher's `Job(...)` construction: `asset_glob=job.get('assetGlob') or "*.ipa"`
- `app.py:3689-3690` and `:3710` — `_release_job_from_payload`; and `app.py:3754` in the import route

### The `Job` dataclass

`scripts/release_source_ingest.py`, fields `asset_glob` and
`asset_exclude_glob` (plus `package`, added by 083). A new field must have a
default so every existing construction site keeps working.

### The UI

`templates/index.html:828` and `:832`:

```html
                    <input type="text" name="assetGlob" value="*.ipa" placeholder="*.ipa — for both platforms, e.g. *.{ipa,apk}">
...
                    <input type="text" name="assetExcludeGlob" placeholder="*-tvOS.ipa">
```

Both are read at `:1485-1486` (one-off import) and `:1661-1662` (save watcher
job), and repopulated at `:3039-3040` (`editAutoImportJob`). `:1505` writes
`f.assetGlob.value` from an asset-discovery button's `data-asset`.

### Conventions

- All user strings reaching the DOM go through `escapeHtml`.
- Optional features default off; stored jobs must keep working untouched.
- `data/auto-import.json` holds live jobs — every one has `assetGlob` and no
  new field, so the new field must default to glob behaviour.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Setup | `python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt` | exit 0 |
| Focused | `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_release_source_ingest.py tests/test_auto_import.py tests/test_import_release.py -q -p no:cacheprovider` | all pass |
| Full suite | `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | `345 passed, 1 skipped` before your changes; strictly more after |
| Scope | `git status --porcelain` | only in-scope files |

## Scope

**In scope**:
- `scripts/release_source_ingest.py`
- `app.py` — the four validation/construction sites listed above, and nothing else
- `templates/index.html`
- `tests/test_release_source_ingest.py`, `tests/test_auto_import.py`, `tests/test_import_release.py`

**Out of scope** (do NOT touch):
- `stream_download`, `_host_allowed`, `_validate_download_host`, and all
  redirect logic — security-critical, unrelated to matching.
- The one-per-platform rule in `github_select_candidate` /
  `gitlab_select_candidate`. It stays. Changing it is a STOP condition.
- `_platform_for_asset` — extension inference is unchanged.
- `scripts/apk_inspection.py`, `scripts/ipa_inspection.py`, `AndroidRepoManager`.
- Publishing more than one APK per release.

## Git workflow

- Branch: `advisor/085-regex-asset-matching`
- Conventional commits, one per step, `(plan 085)` suffix.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add the mode to `Job` and the matcher

Add `asset_match_mode: str = "glob"` to `Job`. Rewrite `_asset_match_state`
to branch on it, preserving glob behaviour byte-for-byte in the default case.

For `regex` mode, mirror the glob semantics deliberately:

- **whole-name match** — use `re.fullmatch`, because `fnmatch` matches the
  whole string. `re.search` would silently change what existing-style
  patterns mean.
- **case-insensitive** — pass `re.IGNORECASE` and match against the original
  name rather than pre-lowercasing, so character classes behave predictably.

Compile patterns once per call at most; do not compile inside a per-asset
loop if you can hoist it.

**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_release_source_ingest.py -q -p no:cacheprovider` → all pass (glob behaviour unchanged)

### Step 2: Validate the pattern at save time, not at run time

A bad regex must be refused when the job is saved, with a readable message —
never surface a raw `re.error` traceback, and never let it fail hours later
inside the scheduler.

In **all four** validation sites, when the mode is `regex`:

- `re.compile` both `assetGlob` and `assetExcludeGlob`; on `re.error`, raise
  the layer's normal error type (`ValueError` in `app.py`, `ConfigError` in
  the manifest parser) with a message naming which field failed and why
- reject a pattern longer than **200 characters**

The length cap is a deliberate, cheap guard against catastrophic
backtracking. Python's `re` has no match timeout, so a pathological pattern
would block the scheduler thread. The input is admin-only (every one of these
routes is `@requires_auth`), so this is defence in depth rather than a
boundary against untrusted input — say so in a comment, and do not build
anything more elaborate.

Accept only the exact strings `"glob"` and `"regex"`; anything else is an
error naming both valid values.

**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/test_auto_import.py tests/test_import_release.py -q -p no:cacheprovider` → all pass

### Step 3: Thread the field through every construction site

Add `assetMatchMode` to `_validate_auto_import_job`'s **returned dict** — a
field absent there is silently dropped and the setting will not persist.
Thread it into the `Job(...)` constructions at `app.py:3484` and `:3710`, and
the manifest parser at `scripts/release_source_ingest.py:341`.

**Verify**: save a job with `assetMatchMode: "regex"`, read it back through
`GET /api/auto-import`, and confirm the value survives the round trip. Assert
this in a test, not by eye.

### Step 4: Surface it in the UI

In `templates/index.html`, add a mode selector to the shared `importRepoForm`
next to the Asset Glob field at `:828`, using the same `.form-group` markup
as its neighbours:

```html
                <div class="form-group">
                    <label>Match Mode:</label>
                    <select name="assetMatchMode">
                        <option value="glob">Glob</option>
                        <option value="regex">Regular expression</option>
                    </select>
                </div>
```

Relabel the two pattern inputs so they read correctly in both modes (they are
no longer always globs), and put a regex example in the helper text — e.g.
`.*arm64-v8a\.apk` — since pinning one ABI out of several is the reason this
exists.

Add the field to both request bodies (`:1485` and `:1661`) and to
`editAutoImportJob` (`:3039`), matching the neighbouring lines.

**Verify**: `grep -n 'assetMatchMode' templates/index.html` → at least 4 matches (markup, two bodies, edit-populate)
**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → 0 failures

### Step 5: Prove it against the real shape

Add a test using the **actual** AnymeX asset names recorded above —
`AnymeX-Android-arm64-v8a.apk`, `AnymeX-Android-armeabi-v7a.apk`, plus an
`.ipa` — asserting that:

- glob `*.apk` raises `ProviderError` (the ambiguity guard still fires)
- regex `.*arm64-v8a\.apk` selects exactly one android candidate
- that candidate is the arm64 asset by name

**Verify**: `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider`

## Test plan

`tests/test_release_source_ingest.py`:
- glob mode unchanged (regression) — an existing test asserting glob selection must pass untouched
- regex mode: full-match semantics (a pattern matching only part of the name does **not** match), case-insensitivity, exclude-pattern in regex mode
- the AnymeX multi-ABI scenario from Step 5
- an invalid regex surfaces the layer's error type, not `re.error`

`tests/test_auto_import.py`:
- a job with `assetMatchMode: "regex"` round-trips through save/read
- a job with an invalid regex is refused at save with a readable message
- a stored job with **no** `assetMatchMode` still behaves as glob (backward compatibility — this is the one that protects `data/auto-import.json`)
- a pattern over 200 characters is refused

`tests/test_import_release.py`:
- `/inspect` honours regex mode when previewing matches

No real network calls — follow the existing stubbing convention in these files.

**Prove each new test discriminates**: break the behaviour, watch it fail,
restore it. Report which tests you did this for.

## Done criteria

ALL must hold:

- [ ] `ADMIN_PASSWORD=x ./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → 0 failures, more passing than the 345 baseline
- [ ] `grep -n 'assetMatchMode' templates/index.html` → at least 4 matches
- [ ] A stored job with no `assetMatchMode` behaves exactly as before (a test asserts it)
- [ ] An invalid regex is refused at save time with a readable message (a test asserts it)
- [ ] The one-per-platform rule is unchanged: `git diff` shows no edit to `github_select_candidate` / `gitlab_select_candidate` beyond what threading the field requires
- [ ] `git status --porcelain` lists no file outside the in-scope list
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- Making regex work appears to require relaxing the one-per-platform rule.
  It does not — a correctly pinned pattern matches one asset.
- Any change appears to require touching `stream_download` or the redirect
  host-allowlist logic.
- The full suite does not report `345 passed, 1 skipped` **before** you make
  any edit — the baseline moved and this plan's numbers are stale.
- You find a fifth site constructing a `Job` that this plan does not list.

## Maintenance notes

- `_asset_match_state` stays the single place a pattern is applied. A future
  match mode belongs there and nowhere else.
- A reviewer should check three things: that glob behaviour is untouched,
  that regex uses `fullmatch` (not `search`), and that an invalid pattern is
  caught at save time rather than in the scheduler thread.
- Deliberately deferred: publishing every ABI variant of a release. That
  needs a decision about F-Droid versionCode collisions between same-version
  per-ABI APKs, and should follow a real single-APK import landing first.
