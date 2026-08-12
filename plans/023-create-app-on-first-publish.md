# Plan 023: Create the app when it isn't in the catalog yet

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result. If anything in
> "STOP conditions" occurs, stop and report — do not improvise. When done,
> update the status row in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD
> grep -n "def add_version" scripts/telegram_bot_ingest.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q   # expect 91 passed
> ```

## Status

- **Priority**: P1 — the bot cannot publish anything to an empty catalog, which is the state it is in
- **Effort**: S
- **Risk**: LOW–MED — it makes the bot able to *create* catalog entries, not just append versions. The confirmation gate is what keeps that safe.
- **Depends on**: 013, 020, 021 (all DONE)
- **Category**: bug (design gap)
- **Planned at**: 2026-08-12

## Why this matters

Observed end to end in production. Every stage now works — forward, `getFile` under `--local`, shared-volume read, validation, metadata extraction, login — and the last step fails:

```
Got Immich_vv3.1.0-AppAssassin.ipa — 32,593,548 bytes, sha256 c93af467…
Detected: app.alextran.immich  3.1.0
/add
Publish failed: App not found
```

The worker only ever calls `POST /api/add-version`, and `SourceManager.add_version` returns `(False, "App not found")` when no catalogue entry has that bundle identifier:

```python
            for app in source_data['apps']:
                if app['bundleIdentifier'] == bundle_identifier:
                    ...
            return False, "App not found"
```

The live catalog is currently `{"apps": []}`. **Every forward will fail this way until an app exists**, and nothing in the bot can create one. Plan 013 specified only the append-a-version path; that was my omission.

## What `/api/add-app` needs, and where each field comes from

From `app.py`'s `/api/add-app` multipart branch:

| Field | Required? | Source |
|---|---|---|
| `bundleIdentifier` | **yes** — 400 without it | Plan 021's extractor |
| `name` | yes in practice | `CFBundleDisplayName`, falling back to `CFBundleName` |
| `version` | yes in practice | Plan 021's extractor |
| `developerName` | yes in practice | **not in the plist** — see below |
| `localizedDescription`, `iconURL`, `minOSVersion` | optional, defaulted | leave to the app's defaults |

Verified against real IPAs — the name keys are reliably present:

```
com.google.ios.youtube   CFBundleDisplayName='YouTube'  CFBundleName='YouTube'
com.ryan.anymex          CFBundleDisplayName='AnymeX'   CFBundleName='anymex'
```

`CFBundleDisplayName` is the user-facing name and is the better choice; `CFBundleName` is the shorter internal one (`anymex` vs `AnymeX`). Prefer display name, fall back to name.

**`developerName` has no reliable source in an IPA.** Make it configurable: `TELEGRAM_DEFAULT_DEVELOPER`, **optional**, defaulting to the string `Unknown`. Do **not** add it to `REQUIRED_VARS` — that would break the running deployment on restart.

## Design

**`/add` creates the app if it does not exist, and says so.**

```
bot: Got Immich_vv3.1.0-AppAssassin.ipa — 32,593,548 bytes, sha256 c93af467…
     Detected: app.alextran.immich  3.1.0

     Send /add to publish that, or /add <bundleIdentifier> <version> to override.
you: /add
bot: app.alextran.immich is not in the catalog — creating it as "Immich".
     Published app.alextran.immich 3.1.0 (32,593,548 bytes).
```

Order of operations, and this ordering is deliberate:

1. `POST /api/add-version`.
2. If it succeeds — done. This is the common path once an app exists, and it stays a single request.
3. If it fails **specifically with `App not found`** — `POST /api/add-app` with the same file, then report that the app was created.
4. Any other failure — report it unchanged. Do **not** fall through to `add-app` on an arbitrary error.

**Match on the exact message `App not found`.** Anything looser turns an unrelated failure into a spurious app creation, which is worse than the failure it papers over.

**One `/add` still means one confirmation.** The existing gate is not weakened: nothing is created without the operator sending `/add`. That is the whole safety model for a catalog that installs software onto devices — do not add auto-publish on forward, and do not add a second confirmation step for creation either. One deliberate action, clearly reported.

**`add-app` must send the IPA file**, exactly as `add-version` does — the multipart `ipaFile` field. Creating an app with no binary would produce a catalogue entry pointing at nothing.

## Scope

**In scope**:
- `scripts/telegram_bot_ingest.py` — extend the extractor to return the display name; add the `add_app` client call; branch in the publish path
- `.env.example` — one optional key name
- `tests/test_telegram_bot_ingest.py`

**Out of scope — do NOT touch**:
- `app.py`. `/api/add-app` and `/api/add-version` already do what is needed; this is entirely worker-side.
- The allowlist check or its position.
- The five validation checks.
- Plan 021's regex or its version precedence. You are **adding** a name to what the extractor returns, not changing what it already returns.
- Plan 020's redaction, timeout, and error-reporting.
- `compose.yml`, `Dockerfile.bot`, `requirements.txt` — no new dependency; `plistlib` already gives you the name.
- `REQUIRED_VARS` — the new setting is optional.
- The 91 existing tests. If one needs editing to pass, report.

## Steps

### Step 1: Return the display name from the extractor

Plan 021 added `extract_ipa_metadata(path)` returning `(bundle_id, version)`. Extend it to return a third value, the app name:

- `name = plist.get("CFBundleDisplayName") or plist.get("CFBundleName")`
- Coerce to `str`, `.strip()`, and use `None` when empty.
- A missing name is **not** a failure — bundle id and version remain the values that matter. Return `(bundle_id, version, None)`.

**Every existing call site must be updated to unpack three values.** `grep -n "extract_ipa_metadata" scripts/telegram_bot_ingest.py` to find them all.

**Verify**: the Plan 021 tests still pass after being updated for the new arity — those updates are expected and allowed, since the function signature changed. Say clearly which tests you touched and why.

### Step 2: Store the name in the pending record

In `handle_document`, add `"name": name` alongside `bundle_id` and `version`.

Leave the "Detected: …" reply as it is — adding the name there is noise, and the name only matters at creation time.

### Step 3: Add the `add_app` client call

Alongside the existing `add_version` method on the feather client, add `add_app(bundle_id, version, name, developer, path)` posting multipart to `/api/add-app` with `bundleIdentifier`, `version`, `name`, `developerName` and `ipaFile`.

Mirror the existing method exactly — same session, same auth handling, same `(ok, message)` return shape. Stream the file rather than reading it into memory.

### Step 4: Branch on `App not found`

In the publish path:

```python
    ok, message = feather.add_version(bundle_id, version, doc["path"])
    if not ok and message == "App not found":
        name = doc.get("name") or bundle_id
        ok, message = feather.add_app(bundle_id, version, name, developer, doc["path"])
        if ok:
            created = True
```

Report creation distinctly from a plain publish, so the operator can see a new catalogue entry appeared rather than a version being appended.

If `doc["name"]` is `None`, fall back to the bundle identifier as the name rather than failing — a catalogue entry named after its bundle id is ugly but correct, and the operator can rename it in the web UI.

### Step 5: Config

Read `TELEGRAM_DEFAULT_DEVELOPER`, optional, default `"Unknown"`. Add the key name to `.env.example` with no value. **Not** in `REQUIRED_VARS`.

### Step 6: Tests

Add to `tests/test_telegram_bot_ingest.py`, reusing the existing fake-client pattern. No network.

1. `test_extract_returns_display_name` — plist with `CFBundleDisplayName` → returned as the third value.
2. `test_extract_prefers_display_name_over_bundle_name` — both present → display name wins.
3. `test_extract_name_none_when_absent` — neither key → `(id, version, None)`, and extraction still succeeds.
4. `test_add_creates_app_when_not_found` — fake feather whose `add_version` returns `(False, "App not found")` → `add_app` is called once with the detected id, version and name, and the reply mentions creation.
5. `test_add_does_not_create_app_on_other_errors` — `add_version` returns `(False, "Failed to save source data")` → `add_app` is **never** called, and the original error is reported verbatim. **Prove it discriminates**: loosen the match to `"not found" in message.lower()` and confirm this test still passes, then to a bare `if not ok` and confirm it fails.
6. `test_created_app_uses_bundle_id_when_name_missing` — pending with `name=None` → `add_app` called with the bundle id as the name.
7. `test_default_developer_is_configurable` — `TELEGRAM_DEFAULT_DEVELOPER` set → that value reaches `add_app`; unset → `"Unknown"`.

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → **98** (91 + 7), with only the Plan 021 extractor tests adjusted for the new arity.

## Done criteria

ALL must hold:

- [ ] `extract_ipa_metadata` returns three values; every call site unpacks three
- [ ] `add_app` posts multipart including `ipaFile` — a created app is never binary-less
- [ ] Creation triggers **only** on the exact message `App not found`
- [ ] Test 5 fails when the match is loosened to a bare `if not ok` (report both runs)
- [ ] `TELEGRAM_DEFAULT_DEVELOPER` is optional, defaults to `Unknown`, and is **not** in `REQUIRED_VARS`
- [ ] `grep -c "TELEGRAM_DEFAULT_DEVELOPER" .env.example` returns `1`, with no value
- [ ] `git diff requirements.txt` is empty
- [ ] One `/add` still performs at most one publish — no auto-publish on forward
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 98; name every pre-existing test you changed and why
- [ ] `git status --short` shows only the three in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

- You are tempted to call `add-app` on any failure rather than exactly `App not found`. That converts an unrelated error into a spurious catalogue entry.
- You are tempted to create the app without sending the IPA. A catalogue entry pointing at nothing is worse than no entry.
- You are tempted to auto-publish on forward, or to add a second confirmation for creation. One `/add`, one action.
- You are tempted to add a dependency, or to change Plan 021's regex or version precedence.
- More than the Plan 021 extractor tests need editing.

## Maintenance notes

- **This is the point where the bot gains the ability to create catalogue entries, not just append to them.** The allowlist and the single `/add` confirmation are the only things standing between a forwarded file and a new app that installs on devices. Neither should be weakened without a deliberate decision.
- **`developerName` is guesswork.** It is not in the IPA, and `Unknown` is a placeholder the operator is expected to correct in the web UI. If AltStore ever surfaces it prominently, revisit — a `/add <id> <version> <developer>` form would be the natural extension.
- **Matching on an error string is fragile.** `App not found` comes from `SourceManager.add_version` in `app.py`; if that message is ever reworded, this branch silently stops working and the bot goes back to "Publish failed: App not found". A shared constant would be better but spans two deployables; the test in step 5 is the guard.
- **The three corrupt entries and the bundle-id mismatches recorded in `plans/README.md` are unaffected** by this. Creating an app from the plist means *new* entries will always agree with their binaries, which is the opposite of the drift already in the catalog.
