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
  `CFBundleExecutable` and required to be that exact archive member
  (frameworks are not read: AltStore checks apps and extensions only). Every
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

The inspector runs under the catalogue lock (and once more before it in
`/api/publish`), on bytes a publish token or a Telegram sender controls. Its
work is budgeted per inspection, deterministically: a wall-clock deadline
would make a busy host silently drop permissions and block installs.

- **Inflating.** The zip reader re-inflates a member from its start on every
  backward seek, so reads only move forward (overlapping fat slices are
  skipped, the signature must follow the load commands, the entitlements
  blob must follow the signature's index; the one exception is re-reading
  the 8-byte head of a thin binary). One executable costs at most its
  declared size, at most 1 GiB, and all the executables one inspection reads
  at most 2 GiB together, app first. Only stored and deflated members are
  read: bzip2 and lzma inflate at a few dozen MiB/s, deflate at 0.4-1.5 GiB/s.
- **Parsing.** XML costs about 0.1 s per MiB, so an entitlements blob may be
  64 KiB (real ones are a few KB) and all of one inspection's blobs 4 MiB.
- **Holding.** Keys are trimmed to the first 256 as they accumulate, not at
  the end, so a binary full of distinct keys cannot fill memory.
- **Looping.** Load commands at most 256 KiB and no more commands than they
  can hold, 8 fat slices, 64 signature slots, 64 extensions, 256 KiB per
  extension `Info.plist`.

Anything unreadable or over a cap contributes nothing; the inspection itself
never fails because of it. Measured on this branch (Python 3.14, one core),
each shape an app plus 64 extensions unless noted:

| Shape | IPA | Inspect | Peak RSS |
|---|---|---|---|
| nine 256 MiB executables of 8 overlapping slices | 2.25 MiB | 3.4 s | 54 MiB |
| 8 slices of maximal 8-byte load commands each | 0.24 MiB | 1.6 s | 54 MiB |
| 64 maximal extension plists | 0.72 MiB | 0.5 s | 54 MiB |
| 8 slices of 1 MiB entitlements XML each | 1.34 MiB | 0.2 s | 45 MiB |
| 8 slices of 60 KiB of distinct keys each | 2.92 MiB | 0.2 s | 45 MiB |
| 8 slices of 1 MiB of distinct keys each | 47.8 MiB | 0.3 s | 62 MiB |

A reviewer measured the fourth shape at 31.7 s and the last at 35 s and
2.1 GiB before the parse budget and the running trim; the first scaled to
minutes before the inflate budget.

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
- Each guard was broken in turn (40 mutations, among them each default cap,
  which `test_default_caps` pins) and a test failed for every one, except
  the extension plist's size pre-check alone, which the length check on the
  bytes actually read backs up (breaking both is caught).
- Real IPAs: MangaSync 0.3.5 (30 MiB deflated executable) gives
  `com.apple.security.application-groups` in 0.07 s; a 459 MB TikTok gives
  22 keys and 13 privacy keys in 0.07 s (its main executable is under 1 MiB;
  its code lives in frameworks, which are not read).
- Not done: a publish response warning when an executable is signed but
  yields no readable entitlements (a DER-only signature, or one over a cap).
