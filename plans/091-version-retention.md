# Plan 091: Keep an app's newest version and N before it; prune the rest on update

- **Priority**: P2
- **Effort**: S
- **Risk**: MEDIUM — it deletes binaries. It only ever acts on an app an
  admin gave a policy, and only when an update lands or on an explicit
  *Prune Now*.
- **Depends on**: 073 (Android repo)
- **Status**: DONE — branch `feat/version-retention`

## Why

Apps that ship often pile up versions nobody installs. NAS Player had 18 APKs
on the F-Droid repo (three per release since it split by ABI), and every one
stays downloadable and signed into the index. The operator wants a rule per
app: when an update arrives, keep it and the two versions before it, and
delete the ones before that.

## What changed

- **A per-app policy**, `keepPreviousVersions` (0–100), stored in
  `data/retention.json` as `{"ios": {bundleId: n}, "android": {package: n}}`.
  Not in `source.json`: that file is public and no client needs the rule.
  A missing or unreadable file means no app has a policy, so nothing is
  pruned by accident. Deleting an app drops its policy, so a re-created app
  never inherits a forgotten rule.
- **Pruning happens where versions are added**, so every ingest path gets it
  (admin upload, `/api/publish`, auto-import, the cron importer through
  `/api/add-version`, Telegram):
  - iOS, in `SourceManager._add_version_locked`: when the new version lands
    at `versions[0]`, the entries past the newest + N are removed in the same
    catalogue write; their IPAs are deleted only after that write succeeds,
    so a failed save loses no binary.
  - Android, in `AndroidRepoManager.add_apk`: APKs are grouped into
    releases by **versionName**, so NAS Player's per-ABI APKs of one version
    count once; releases rank by their highest versionCode (F-Droid's notion
    of newer). The policy applies once the added APK's release is the
    newest. Uploading the first ABI of a new version (whose code is still
    below the previous arm64 build) prunes nothing; the upload that makes it
    the newest does. versionName comes from the APK just inspected, then the
    published index, then the APK itself; an APK whose versionName cannot be
    read is never counted and never pruned. A pruning failure is logged, not
    raised: the new APK is already published.
  - An older version added later (a backport) is not an update and prunes
    nothing, on both platforms.
- **Routes**, all admin-session only (`requires_auth`), because they decide
  what gets deleted: `GET /api/retention`, `POST /api/retention`
  (`{platform, id, keepPreviousVersions}`, `null` clears),
  `POST /api/retention/apply` (prune to the saved policy now). A token's
  publish can still trigger a policy an admin set.
- **Reporting**: `/api/publish` returns `pruned`; the admin add-version
  message names what was removed; a new opt-in notification event
  `prune_versions`.
- **UI**: *Keep Previous Versions* with *Save* and *Prune Now* in the iOS
  Edit dialog and on each Android app card. Saving never deletes anything;
  the next update or *Prune Now* does.

## Verification

- `tests/test_retention.py`, 28 cases: newest + N kept and older IPAs
  deleted; no policy keeps everything; a backport and a re-publish prune
  nothing; a policy change waits for an update or *Prune Now*; a failed save
  deletes no IPA; per-ABI APKs of one version kept or removed together, and
  the first ABI of a new version prunes nothing; unindexed versionNames read
  from the APK; an unreadable APK is never pruned; deleting an app forgets
  its policy; malformed values and unknown apps refused; tokens refused on
  every retention route; a corrupt store prunes nothing.
- Thirteen sabotages, one at a time, each failed the tests aimed at it:
  update never prunes, backport prunes, IPAs deleted before the save, APKs
  grouped by versionCode, no newest-release check, unreadable APKs pruned,
  token allowed to set a policy, either `delete_app` keeping its policy, no
  value validation, missing app accepted, a corrupt store raising, `pruned`
  dropped from the publish response.
- Every test module that loads `app.py` (14 files): 484 passed, 1 skipped.
- The admin UI driven in Chromium against a local instance: Android card and
  iOS dialog save a policy, *Prune Now* removed the oldest Android release
  and iOS version with their files, and an out-of-range value is refused.
