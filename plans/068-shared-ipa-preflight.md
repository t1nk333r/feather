# Plan 068: Introduce one IPA preflight model and publish verified platform metadata

> **Executor instructions**: Execute every step and verification gate in order.
> Stop on any condition listed below; do not guess at signing or AltStore schema
> semantics. Update this plan's status row in `plans/README.md` when complete
> unless a reviewer owns the index.
>
> **Drift check (run first)**:
> `git diff --stat f8a8827..HEAD -- scripts/ipa_inspection.py scripts/release_source_ingest.py scripts/telegram_bot_ingest.py app.py templates/index.html tests/test_ipa_inspection.py tests/test_release_source_ingest.py tests/test_telegram_bot_ingest.py tests/test_import_release.py tests/test_auto_import.py tests/test_routes.py`
> `scripts/ipa_inspection.py` and its test are new. If existing extractors or
> catalog write shapes changed, compare them with Current state and stop on a
> material mismatch.

## Status

- **Priority**: P2
- **Effort**: L
- **Risk**: MED
- **Depends on**: none; execute after Plan 067 when following recommended order
- **Category**: direction
- **Planned at**: commit `f8a8827`, 2026-08-22

## Why this matters

Repo import and Telegram independently parse the same top-level `Info.plist`,
but return only bundle ID, marketing version, and name. The catalog consequently
defaults minimum iOS to `14.0`, synthesizes `buildVersion` from the marketing
version, and cannot reject an explicitly tvOS binary. Build one shared preflight
model, wire it through repo, auto, and Telegram ingestion, reject known non-iOS
platforms, and publish real build/minimum-OS metadata. Permission extraction is
a bounded spike in this plan: privacy descriptions may be stored only from the
top-level app plist; entitlement mapping must remain deferred unless it can be
implemented without parsing or persisting sensitive provisioning-profile data.

## Current state

`scripts/release_source_ingest.py:647-706` validates an IPA and returns a tuple:

```python
bundle_id = plist.get("CFBundleIdentifier")
version = plist.get("CFBundleShortVersionString") or plist.get("CFBundleVersion")
name = plist.get("CFBundleDisplayName") or plist.get("CFBundleName")
return bundle_id, version, name
```

`scripts/telegram_bot_ingest.py:288-339` reimplements the same parsing but
returns `(None, None, None)` on failure. Its pending record stores only those
three fields (`scripts/telegram_bot_ingest.py:505-529`).

At serve time, `app.py:211-232` fills absent metadata with placeholders:

```python
app_entry['appPermissions'] = {"entitlements": [], "privacy": {}}
...
version_entry['buildVersion'] = str(version_entry.get('version', ''))
```

New app versions default minimum OS to `14.0` (`app.py:1295-1308`), while
`SourceManager.add_version()` stores only version/date/URL/minimum OS/size
(`app.py:1545-1551`).

The official AltStore source documentation defines `buildVersion`,
`minOSVersion`, and `appPermissions.privacy` as source fields and says privacy
is the dictionary of `UsageDescription` keys from `Info.plist`:
<https://faq.altstore.io/developers/make-a-source>. Treat that page as the
authoritative schema reference; do not infer a different shape.

Applicable conventions:

- Match only `^Payload/[^/]+\.app/Info\.plist$`; nested framework/extension
  plists must never win.
- Repo import failures are hard validation errors. Telegram metadata failure is
  recoverable because `/add <bundle> <version>` can override it.
- Optional metadata is best-effort and must not prevent a valid iOS import.
- Never store raw `embedded.mobileprovision`, signing identities, team IDs,
  certificates, or token-bearing data.
- Preserve the public route/auth contracts and single-process write lock.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| New inspector tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_ipa_inspection.py -q` | all pass |
| Ingest regression tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py tests/test_telegram_bot_ingest.py tests/test_import_release.py tests/test_auto_import.py -q` | all pass |
| Catalog tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q` | all pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | all pass; baseline 243 |
| Diff hygiene | `git diff --check` | no output |

## Scope

**In scope**:

- Create `scripts/ipa_inspection.py`.
- Create `tests/test_ipa_inspection.py`.
- Adapt `scripts/release_source_ingest.py` and `scripts/telegram_bot_ingest.py`.
- Wire preflight fields through `app.py` and the import progress UI.
- Update focused tests in `tests/test_release_source_ingest.py`,
  `tests/test_telegram_bot_ingest.py`, `tests/test_import_release.py`,
  `tests/test_auto_import.py`, and `tests/test_routes.py`.

**Out of scope**:

- Manual Add-App/Add-Version upload preflight.
- Decrypting binaries or parsing Mach-O code signatures.
- Persisting raw provisioning profiles or signing data.
- Guessing entitlements when no safe, dependency-free source is proven.
- Populating `appPermissions.entitlements` in this plan.
- Per-extension permissions; MVP reads the top-level app plist only.
- Supporting tvOS catalogs. Feather remains an iOS/iPadOS source manager.

## Git workflow

- Branch: `advisor/068-shared-ipa-preflight`
- Conventional commits, e.g. `feat(import): share IPA preflight metadata`.
- Land the standalone inspector and its tests before switching callers.
- Do not push or open a PR unless instructed.

## Target model

Create an immutable `IpaInspection` dataclass with at least:

```python
bundle_identifier: str
version: str                 # CFBundleShortVersionString fallback CFBundleVersion
build_version: str           # CFBundleVersion fallback version
name: str | None
minimum_os_version: str | None
supported_platforms: tuple[str, ...]
device_families: tuple[int, ...]
privacy: dict[str, str]
```

Expose a normalized `platform` property/value with `ios`, `tvos`, or `unknown`.
Detection order:

1. `CFBundleSupportedPlatforms` (`iPhoneOS` => iOS, `AppleTVOS` => tvOS);
2. `DTPlatformName` only if the supported-platform list is absent;
3. `UIDeviceFamily == 3` may corroborate tvOS but must not alone override an
   explicit iPhoneOS platform;
4. missing signals => `unknown`, not a guessed failure.

Privacy includes only keys that start with `NS`, end with `UsageDescription`,
and have a non-empty string value. Return a new plain dictionary; do not retain
the whole plist.

## Steps

### Step 1: Build and characterize the shared inspector

In `scripts/ipa_inspection.py`, implement exact archive/plist validation and
return `IpaInspection`. Define module-specific `InspectionError` messages that
contain a safe filename/release label supplied by the caller, never a secret or
raw plist. Keep the module dependency-free within the standard library.

Add synthetic ZIP fixtures for:

- iPhoneOS, iPhone+iPad device families, build != marketing version;
- AppleTVOS/device family 3;
- unknown platform with otherwise valid metadata;
- `MinimumOSVersion` absent;
- valid and non-string `NS*UsageDescription` fields;
- nested framework plist ignored;
- zero/multiple top-level app plists;
- invalid ZIP/missing Payload/malformed plist.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_ipa_inspection.py -q
rg -n "embedded.mobileprovision|Signing|Certificate" scripts/ipa_inspection.py
```

Expected: all inspector tests pass; the second command returns no parsing or
persistence implementation for those sensitive structures.

### Step 2: Migrate release import behind compatibility wrappers

Import the shared module from `scripts/release_source_ingest.py`. Keep
`extract_ipa_metadata()` and `validate_and_extract_metadata()` callable for
existing tests/callers, but implement them via the shared inspector. Add a new
function returning the full inspection for new call sites.

Repo/auto import rules:

- explicit tvOS => hard `ValidationError` before catalog login/write;
- explicit iOS => continue;
- unknown platform => continue with a warning/preflight field, not a failure;
- configured bundle mismatch remains a hard failure;
- preserve the candidate/release context in errors.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q -k 'metadata or plist or platform or bundle'
```

Expected: legacy tuple tests and new platform tests all pass.

### Step 3: Migrate Telegram without removing its override escape hatch

Replace Telegram's duplicate plist parser with the shared inspector. Preserve
its recoverable behavior: on inspection failure, store `inspection=None`, tell
the operator metadata could not be read, and allow explicit `/add` arguments.
For a detected tvOS binary, reject during document handling and do not create a
pending item. Add platform/build/minimum-OS to the detected confirmation.

Do not include privacy-description values in Telegram messages; at most report
the number or names of permission keys.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_telegram_bot_ingest.py -q -k 'metadata or platform or pending or add'
```

Expected: existing override behavior remains; tvOS never becomes pending.

### Step 4: Publish build, minimum OS, and safe privacy metadata

In the one-off and auto-import paths in `app.py`, consume the full inspection.
Pass `buildVersion` and detected `minOSVersion` into both new-app and new-version
catalog writes. Extend `SourceManager.add_app_manual()` and `add_version()` to
store provided `buildVersion` without overwriting an existing real value.

For new apps only, set:

```python
"appPermissions": {"entitlements": [], "privacy": inspection.privacy}
```

when privacy is non-empty. Existing apps' manually curated permissions must not
be replaced during version import. Keep `normalize_source()` fallbacks for old
catalog entries.

Emit a one-off NDJSON `preflight` event before `publishing`, containing platform,
bundle identifier, name, version, build version, minimum OS, device families,
and privacy key names only. Render it in the import progress area with escaped
text. Auto-import `lastResult` may include the same non-sensitive summary.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_import_release.py tests/test_auto_import.py tests/test_routes.py -q -k 'preflight or build or min_os or privacy or platform'
```

Expected: accurate metadata lands for new versions; existing curated app
permissions survive subsequent imports.

### Step 5: Prove cross-path consistency and run the full gate

Add at least these integration cases:

1. One synthetic IPA inspected through release and Telegram yields identical
   core fields.
2. iOS build/minimum OS are stored in a new app version.
3. Build version differing from marketing version is preserved in `/source.json`.
4. Missing minimum OS uses the existing fallback, not `None`.
5. Explicit tvOS fails one-off import before publishing.
6. Explicit tvOS is rejected by Telegram before pending state.
7. Unknown platform is visible but remains importable.
8. Privacy descriptions populate a newly created app.
9. Re-import does not overwrite existing `appPermissions`.
10. Preflight UI escapes provider/plist-derived strings.

Prove discrimination: temporarily map AppleTVOS to iOS; both rejection tests
must fail. Restore the code before the final run.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
git diff --check
git status --short
```

Expected: all tests pass and only in-scope files plus the status row changed.

## Done criteria

- [ ] Repo, auto, and Telegram use one top-level IPA inspection module.
- [ ] Explicit tvOS is rejected before any Feather catalog mutation.
- [ ] Unknown platform remains a visible, non-fatal state.
- [ ] Real build/minimum OS are published when present.
- [ ] New-app privacy is sourced only from safe top-level plist strings.
- [ ] Existing curated app permissions are never overwritten by import.
- [ ] No raw provisioning/signing data is read, logged, returned, or stored.
- [ ] Compatibility wrappers keep current callers/tests working.
- [ ] Full suite passes with at least ten new targeted cases.

## STOP conditions

- Platform cannot be determined from plist keys without parsing Mach-O data.
  Keep `unknown`; do not add a binary parser in this plan.
- Entitlements require decoding `embedded.mobileprovision`, CMS, certificates,
  or code signatures. Stop that subtask and leave entitlements empty.
- The official AltStore privacy schema conflicts with the dictionary contract
  above. Stop and report with the official documentation link.
- A migration would wipe or replace existing `appPermissions`.
- Telegram override behavior would be removed.
- Tests require real IPAs, real provider access, or secrets.

## Maintenance notes

- New ingress paths should consume `IpaInspection`; do not add another plist
  parser.
- Add future platform mappings only from fixtures and documented plist signals.
- Permission values may contain user-facing text; keep them out of logs and
  Telegram even though they are not credentials.
- A later entitlement plan needs a separate security/design review and explicit
  fixtures proving what is retained versus discarded.
