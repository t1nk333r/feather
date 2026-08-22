# Plan 067: Preview release assets and make selectors explicit before import

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If a STOP condition occurs, stop and report; do not improvise.
> When complete, update this plan's row in `plans/README.md` unless a reviewer
> says they maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat f8a8827..HEAD -- scripts/release_source_ingest.py app.py templates/index.html release-sources.example.json tests/test_release_source_ingest.py tests/test_import_release.py tests/test_auto_import.py`
> If any in-scope file changed, compare the excerpts below with live code. Stop
> if selection semantics or the import form have materially changed.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: direction
- **Planned at**: commit `f8a8827`, 2026-08-22

## Why this matters

The release importer requires exactly one matching asset, but the UI only
offers a blind free-text glob. SceneBox v1.0.2 publishes both
`SceneBox-1.0.2.ipa` and `SceneBox-1.0.2-tvOS.ipa`; `SceneBox-*.ipa` therefore
fails without showing the two matches in the form. Add a read-only inspection
flow and an optional exclusion glob so operators can test reusable rules before
running or scheduling them. Preserve the exactly-one invariant: ambiguity must
remain visible and must never become “pick the first asset.”

## Current state

- `scripts/release_source_ingest.py` owns provider release selection.
- `app.py` adapts that engine for one-off and scheduled in-app imports.
- `templates/index.html` contains the mobile-first vanilla-JS import form.
- Provider tests use fake response objects; route tests monkeypatch selection
  and download and must never contact GitHub or GitLab.

At `scripts/release_source_ingest.py:386-400`, GitHub uses one include glob and
fails on ambiguity:

```python
matches = [
    a for a in assets
    if fnmatch.fnmatch((a.get("name") or "").lower(), job.asset_glob.lower())
]
if len(matches) > 1:
    raise ProviderError(... expected exactly one)
```

GitLab repeats the same rule at `scripts/release_source_ingest.py:461-475`.
`Job` has `asset_glob` but no exclusion policy (`scripts/release_source_ingest.py:102-115`).

The form at `templates/index.html:800-803` exposes only:

```html
<label>Asset Glob:</label>
<input type="text" name="assetGlob" value="*.ipa">
```

The import route emits `resolved` only after selection has succeeded
(`app.py:2680-2683`), so an ambiguous selector cannot reveal candidate names.

Applicable conventions:

- Provider-specific API parsing stays in `scripts/release_source_ingest.py`.
- Provider data structures must never retain token-bearing headers.
- UI-returned asset metadata must exclude download URLs, authorization headers,
  and secrets.
- Auth-gate every new admin endpoint with `@requires_auth`.
- Existing jobs/manifests without the new field must behave exactly as before.
- Use `escapeHtml()` for every provider-supplied string rendered into HTML.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused engine tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q` | all pass |
| Focused UI/import tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py tests/test_auto_import.py -q` | all pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | all pass; baseline is 243 |
| Diff hygiene | `git diff --check` | no output, exit 0 |

## Scope

**In scope**:

- `scripts/release_source_ingest.py`
- `app.py`
- `templates/index.html`
- `release-sources.example.json`
- `tests/test_release_source_ingest.py`
- `tests/test_import_release.py`
- `tests/test_auto_import.py`

**Out of scope**:

- Downloading an IPA during inspection.
- Automatically choosing the first or smallest matching asset.
- Persisting a provider download URL, token, header, or temporary path.
- Supporting self-hosted GitHub/GitLab or CI job artifacts.
- Platform detection inside the IPA; Plan 068 owns that after download.
- Changing the public `/source.json` schema.

## Git workflow

- Branch: `advisor/067-release-asset-discovery`
- Use conventional commits, e.g. `feat(import): preview release assets`.
- Keep engine/API and UI/tests in separate logical commits if practical.
- Do not push or open a PR unless instructed.

## Target contract

Add an optional `assetExcludeGlob` string across standalone manifests, one-off
imports, and auto-import jobs. A name matches only when it matches `assetGlob`
case-insensitively and does **not** match the exclusion glob. Blank/missing
exclusion means “exclude nothing.”

Add authenticated `POST /api/import-release/inspect`. It accepts the same
provider/project/prerelease/selector/host fields as the import form and returns
up to the five newest eligible releases:

```json
{
  "releases": [{
    "release": "v1.0.2",
    "releasedAt": "...",
    "assets": [{
      "name": "SceneBox-1.0.2-tvOS.ipa",
      "size": 29500000,
      "included": true,
      "excluded": true,
      "matched": false,
      "hostAllowed": true
    }],
    "matchCount": 1
  }]
}
```

`size` may be `null` for GitLab. `hostAllowed` applies to GitLab links and may
be omitted for GitHub. Never include IDs or URLs unless the implementation can
prove they are non-sensitive and needed; they are not needed for this MVP.

## Steps

### Step 1: Centralize include/exclude matching in the engine

Add `asset_exclude_glob: str = None` to `Job` without breaking existing keyword
construction. Add one helper, for example `_asset_match_state(job, name)`, that
returns `included`, `excluded`, and `matched` booleans. Use it in both GitHub
and GitLab selectors; remove the two inline `fnmatch` expressions.

Extend `parse_manifest_dict()` to accept optional `assetExcludeGlob`. Reject
non-string values and normalize blank strings to `None`. Add the field to the
example manifest with a non-version-specific example such as `*-tvOS.ipa`.

**Verify**:

```bash
rg -n "asset_exclude_glob|assetExcludeGlob|_asset_match_state" scripts/release_source_ingest.py release-sources.example.json
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q
```

Expected: the new symbols are present and every focused test passes.

### Step 2: Add provider-neutral inspection without downloading

Create an engine function such as `inspect_release_assets(job, session, tokens,
timeout=30, limit=5)`. Reuse the same draft/prerelease/date ordering as candidate
selection. Do not fork a second interpretation of eligibility; extract small
private helpers if necessary and use them from both inspection and selection.

For each eligible release, return only sanitized release metadata and asset
match state. For GitLab, derive the hostname and report whether it belongs to
`allowed_download_hosts`, but do not return the URL. Include non-IPA assets so
the operator can understand a zero-match release, but cap results at five
releases and 100 assets per provider response.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q -k 'inspect or select or ambiguous or prerelease'
```

Expected: tests prove ordering, include/exclude behavior, ambiguity reporting,
GitLab host status, the five-release cap, and absence of URL/token fields.

### Step 3: Wire inspection and the new selector field through Flask

In `app.py`, avoid duplicating import-form normalization between inspect and
run. Extract only the small payload-to-`Job` adapter needed by both routes; do
not refactor unrelated import/publish logic.

Add `POST /api/import-release/inspect` with `@requires_auth`. It must:

1. validate provider/project/GitLab allowed hosts using the same rules as import;
2. build the same `Job` selector used by execution;
3. call the engine inspection function once;
4. return JSON errors with status 400 for validation/provider failures;
5. redact unexpected errors and log the detail server-side.

Persist `assetExcludeGlob` in `_validate_auto_import_job()` and reconstruct it
in `_run_auto_import_job()`. Missing fields in existing `auto-import.json`
remain backward compatible.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py tests/test_auto_import.py -q -k 'inspect or selector or exclude or auth'
```

Expected: unauthenticated inspection is 401; provider calls are mocked; saved
jobs round-trip the exclusion and old jobs still run.

### Step 4: Add a mobile-friendly selector preview

In `templates/index.html`:

- add optional “Exclude Glob” below Asset Glob;
- add a secondary “Discover assets” button;
- render releases/assets inline with matched/excluded status and sizes;
- show explicit zero/one/many match state per release;
- provide an action that copies an asset name into Asset Glob for a one-off
  exact selection;
- include `assetExcludeGlob` in one-off and saved-job payloads and populate it
  when editing an existing job.

Disable neither Import nor Save globally when the provider is temporarily
unreachable. After a successful preview, visually warn and require confirmation
when the latest release has zero or multiple matches; execution remains the
authoritative fail-safe. Escape all provider strings and use existing tokens,
buttons, toasts, and loading helpers.

**Verify**:

```bash
rg -n "Discover assets|assetExcludeGlob|importAsset" templates/index.html
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py tests/test_auto_import.py -q
```

Expected: form hooks and tests are present; all focused tests pass.

### Step 5: Add regression coverage and run the full gate

Add at least these tests:

1. GitHub include glob matches iOS and tvOS; exclusion removes tvOS.
2. GitLab follows identical case-insensitive semantics.
3. Missing exclusion preserves old behavior.
4. Inspection returns ambiguous names without raising.
5. Inspection never returns provider URLs or credentials.
6. Draft/prerelease/future release rules match execution.
7. Inspect route requires auth.
8. Inspect route exposes both SceneBox names and `matchCount == 2` before exclusion.
9. Auto-import jobs round-trip `assetExcludeGlob`.
10. Index markup contains preview controls and escaped rendering path.

Prove discrimination: temporarily bypass the exclusion check; the SceneBox
test must fail. Restore it before continuing.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
git diff --check
git status --short
```

Expected: all tests pass; diff check is clean; only in-scope files plus
`plans/README.md` are modified.

## Done criteria

- [ ] One read-only authenticated request previews up to five eligible releases.
- [ ] Preview and execution use one shared include/exclude matcher.
- [ ] SceneBox’s iOS and tvOS assets are visible before import; `*-tvOS.ipa`
      exclusion makes the match count exactly one.
- [ ] Ambiguity still fails execution; no implicit first-match behavior exists.
- [ ] Existing manifests/jobs without `assetExcludeGlob` are unchanged.
- [ ] No response/history/config contains provider download URLs or credentials.
- [ ] At least ten focused regression cases exist and the full suite passes.
- [ ] No files outside Scope (plus the status row) changed.

## STOP conditions

- The provider API cannot list asset names without also exposing credentials to
  the browser. Stop; do not weaken credential handling.
- Inspection and execution cannot share eligibility/matching helpers without a
  broad importer rewrite. Stop and propose a smaller engine extraction.
- GitLab preview would bypass `allowedDownloadHosts` or display an unapproved URL.
- A proposed selector can silently resolve multiple matches by ordering.
- Tests require live GitHub/GitLab access.

## Maintenance notes

- Preview is advisory; execution must always re-fetch and revalidate because a
  release may change between the two requests.
- If another provider is added, it must implement the same sanitized inspection
  contract and the shared matcher.
- Plan 068 can later add platform validation after download; it must not weaken
  the pre-download exactly-one rule delivered here.
