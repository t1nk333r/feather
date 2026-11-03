# Plan 090: Let a repo import publish an IPA under a catalog ID of the operator's choosing

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW — an opt-in field; with it unset every path behaves exactly
  as before. Nothing about how an IPA is validated changes.
- **Depends on**: 048 (auto-import), 084 (per-platform import helpers)
- **Status**: DONE — branch `feat/catalog-bundle-override`

## Why

The operator keeps patched builds that share an upstream bundle ID as
**distinct catalog apps**: NeoFreeBird's IPA declares `com.atebits.Tweetie2`,
but the catalog keeps it as `com.atebits.Tweetie2.nfb`. The Telegram bot can
already do this (`/add <bundleIdentifier> <version>`), and `check_ipa` only
*warns* when an upload's ID differs from its catalog entry, because "renamed
patched builds are published under their own catalog ID on purpose".

The release importer could not. It always filed the IPA under the ID inside it,
and `bundleIdentifier` is an assertion, not a destination. The live NeoFreeBird
job was configured with `bundleIdentifier: com.atebits.Tweetie2xxx` to try to
steer it, and failed every run from 2026-10-10 with "IPA bundle id
com.atebits.Tweetie2 does not match the job's bundleIdentifier".

## What changed

An optional **`catalogBundleIdentifier`** on auto-import jobs
(`POST /api/auto-import/job`) and on the one-off import
(`POST /api/import-release`). The admin form's **Publish Under Catalog ID**
field feeds both buttons.

- When set, the IPA is published into that catalog entry: the already-present
  check, `add_version`, icon backfill, create-if-missing, and the notification
  all use it. When unset, the IPA's own ID is used, as before.
- `bundleIdentifier` keeps its meaning. It is checked against the **IPA's**
  ID, so a job can assert "this is NeoFreeBird's X build" and still file it
  under `.nfb`. The override never relaxes that check.
- The preflight (job `lastResult` and the one-off stream) keeps the IPA's own
  `bundleIdentifier` and adds `catalogBundleIdentifier` when they differ.
- Import history records the catalog ID as `bundleIdentifier`, because
  `/api/import-provenance/<bundle_id>/<version>` is looked up by catalog ID.
  The IPA's ID goes into the new `artifactBundleIdentifier` field.
- Validation at save time: reverse-DNS with at least two segments, only
  `[A-Za-z0-9-]`, at most 255 characters. That is the CFBundleIdentifier
  character set, and it guarantees the value survives `secure_filename`,
  which names the `ipas/<id>/` and `icons/<id>/` storage paths.
- iOS only. An APK's package is its F-Droid identity and its signature, so it
  cannot be renamed by the catalog.

**Not changed:** the cron `release-import` manifest (`parse_manifest_dict`)
and `/api/publish` do not take the field. The cron importer publishes over HTTP
through `/api/add-version`, which already accepts any catalog ID; add the
field there if a cron job ever needs it.

## Verification

- Nine new test functions, 17 cases (`tests/test_auto_import.py`,
  `tests/test_import_release.py`, 61 → 78 passed):
  round trip through the store; blank values normalize to `None`; nine
  malformed values are refused at save; publish into the named entry with
  provenance under the catalog ID; a rerun is a skip, not a duplicate; the
  `bundleIdentifier` assertion still refuses a wrong IPA; create-if-missing
  creates the entry under the catalog ID; a malformed value on the one-off path
  is reported before any download.
- Each behaviour was sabotaged in turn: field dropped from the stored job,
  override ignored on each path, history keeping the IPA's ID, format check
  removed, one-off validation skipped. Each sabotage made the tests aimed at
  it fail.
