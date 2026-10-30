# Plan 089: Declare each iOS app's entitlements from its binary

- **Priority**: P1
- **Effort**: S–M
- **Risk**: MED — the inspector now reads executables an attacker with a
  publish token controls, and what it keeps goes into the public
  `source.json`. Every read is capped and best effort; nothing that was
  accepted before is refused.
- **Depends on**: 068 (shared IPA preflight), 086 §4c (the AltStore check)
- **Status**: IN REVIEW — branch `fix/declare-ipa-entitlements`

## Why

AltStore and SideStore refuse to install an app from a source unless the
source's `appPermissions` lists every entitlement key the app and its
extensions are signed with, and every `NS…UsageDescription` key of their
`Info.plist`s (`VerifyAppOperation.verifyPermissions`, quoted in 086 §4c). The
check skips `application-identifier` and `com.apple.developer.team-identifier`
and runs on the downloaded bundle before re-signing. Feather wrote
`"entitlements": []` for every app, so any app signed with, say, an App Group
could not be installed from it. MangaSync 0.3.5 is one: its app and both
extensions carry `com.apple.security.application-groups`.

068's maintenance note asked for a separate design review and fixtures proving
what is retained versus discarded before any entitlement work. This is that
record.

## What is read, and what is kept

- **Read:** the top-level app's executable and up to 64 app extensions
  (`PlugIns/` and `Extensions/`), each named by its own `Info.plist`'s
  `CFBundleExecutable` and required to be that exact archive member. Every
  architecture slice of a thin, fat or fat-64 Mach-O. From each slice, the
  XML entitlements blob (slot 5) of the embedded code signature. A DER-only
  signature (slot 7 without slot 5) yields nothing; `codesign`, `ldid` and
  `rcodesign` write both.
- **Kept:** entitlement **keys** only, never their values (team IDs, group
  names, keychain groups stay out of the catalogue), and the extensions'
  `NS…UsageDescription` keys and text. The app's own text wins over an
  extension's for the same key.
- **Discarded:** everything else in the binary, every value, keys over 256
  characters; beyond 256 entitlement and 64 privacy keys (first in sorted
  order); privacy text past 2000 characters.

## Hostile input

The inspector runs under the catalogue lock, on bytes a publish token or a
Telegram sender controls. The zip reader re-inflates a member from its start
on every backward seek, so:

- every read is forward-only (overlapping fat slices are skipped, the
  signature must follow the load commands, the entitlements blob must follow
  the signature's index), so one executable costs at most its declared size;
- one executable may declare at most 1 GiB, and all the executables one
  inspection reads at most 2 GiB together, app first;
- load commands at most 256 KiB and no more commands than they can hold,
  8 fat slices, 64 signature slots, 1 MiB of entitlements XML, 256 KiB per
  extension `Info.plist`.

Anything unreadable or over a cap contributes nothing; the inspection itself
never fails because of it. Measured on this branch (Python 3.14, one core):
nine 256 MiB deflated executables of eight overlapping slices each, in a
2.25 MiB IPA, 3.4 s; 65 executables of 8 slices of maximal 8-byte load
commands, 1.6 s; 64 maximal extension plists, 0.5 s; peak RSS 54 MiB. Before
the caps the first shape scaled to minutes.

## Writing the catalogue

`_declare_permissions` adds what an app lacks and never removes or rewrites
an entry (an operator's text and older `{"name": …}` objects stay). An app's
whole list is held to the same 256 / 64 caps, so many versions cannot grow
`source.json` without limit. It runs on every path that stores an IPA
(`add_app_manual`, `add_version`, `update_version`), and on iOS
`/api/publish` even when the version is already present, so re-sending a
published IPA repairs an app that was published before this change. The
admin form, importers and Telegram bot stop at "already present" before
reading the file, so they do not.

## Verification

- `tests/test_ipa_inspection.py` and `tests/test_publish_api.py`: thin,
  32-bit, fat and fat-64 binaries; extensions in both folders; unsigned,
  unsigned-with-slots, truncated, wrong-magic, encrypted and corrupt-deflate
  members; every bound and cap; values never reaching `source.json`; the
  three stale-inspection resets; the concurrent-retry call site; malformed
  hand-edited entries; the per-app caps; the 503 on a failed save.
- Each guard was broken in turn (26 mutations) and a test failed for every
  one, except the extension plist's size pre-check alone, which the length
  check on the bytes actually read backs up (breaking both is caught).
- Real IPAs: MangaSync 0.3.5 gives `com.apple.security.application-groups`;
  a 459 MB TikTok gives 22 keys and 13 privacy keys in 0.07 s.
