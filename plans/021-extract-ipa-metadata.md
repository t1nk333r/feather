# Plan 021: Read the bundle identifier and version out of the IPA

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result. If anything in
> "STOP conditions" occurs, stop and report — do not improvise. When done,
> update the status row in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD                              # plan written against 45e6b3b
> md5sum scripts/telegram_bot_ingest.py                   # expect c287cd0d07d30c1ae3210211a891c426
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q   # expect 83 passed
> ```

## Status

- **Priority**: P2 — removes a class of silent data corruption, not an outage
- **Effort**: S
- **Risk**: LOW — additive; the existing `/add <id> <version>` form keeps working unchanged
- **Depends on**: 013, 020 (both DONE)
- **Category**: feature / correctness
- **Planned at**: 2026-08-12, `45e6b3b`

## Why this matters

Today the operator types the bundle identifier and version by hand:

```
/add org.futo.immich 3.1.0
```

That is a transcription step in the middle of publishing software to devices, and **it has already gone wrong.** In the observed session the operator typed `org.futo.immich` while the forwarded file's own caption declared `app.alextran.immich`. A wrong bundle identifier does not error — it silently creates a **new app** in the catalog instead of a new version of an existing one, and subscribed devices never see it as an update.

The IPA already contains both values, authoritatively. `Payload/<App>.app/Info.plist` carries `CFBundleIdentifier` and `CFBundleShortVersionString`, and that plist is what iOS itself reads. Extracting them removes the typing and makes the catalog agree with the binary.

### This is not hypothetical — the existing catalog already disagrees with its own files

Every IPA in `data/ipas/` was read during planning. **Three of eight catalogued apps carry a bundle identifier that does not match the binary they ship**:

| Catalog says | The IPA actually declares |
|---|---|
| `com.instagram.theta` | **`com.burbn.instagram`** |
| `com.instagram.ifgram` | **`com.burbn.instagram`** |
| `com.michael-128.qBitControl` | **`MikeMichael225.qBitControl`** |
| `com.zhiliaoapp.musically` | `com.zhiliaoapp.musically` ✓ |
| `com.google.ios.youtube` | `com.google.ios.youtube` ✓ |
| `com.ryan.anymex` | `com.ryan.anymex` ✓ |

Versions drift too. `YT_20.49.5_KP.ipa` and `YT_20.49.5_KP_Cracked.ipa` are catalogued as `20.49.5`, but both declare `CFBundleShortVersionString = 20.47.3`. **The filename lies.** Anything that parses the filename inherits that lie; the plist does not.

This plan does **not** repair those entries — that is a data decision for the operator, and two of them may be deliberate (renaming a patched Instagram build so it can sit beside the real one). It stops *new* mismatches from being introduced by typing.

## Current state

`scripts/telegram_bot_ingest.py`.

**`validate_ipa_file` already opens the archive** (`line 249`), so the extra read costs nothing structurally:

```python
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    if not any(name.startswith("Payload/") for name in names):
        raise ValidationError("archive has no Payload/ entry (not an IPA)")
```

**The pending record** built in `handle_document` (`line ~396`):

```python
    pending[user_id] = {
        "path": local_path,
        "filename": filename,
        "size": declared_size,
        "sha256": digest,
    }
```

**`handle_add_command`** (`line 409`) currently requires exactly three tokens:

```python
    parts = text.split()
    if len(parts) != 3:
        bot.send_message(chat_id, f"Usage: {USAGE_HINT.split(':', 1)[1].strip()}")
        return

    _, bundle_id, version = parts
```

`zipfile` is already imported. **`plistlib` is in the standard library** — no new dependency, which matters because `Dockerfile.bot` deliberately installs only `requests`.

## The one thing that will go wrong if you rush it

**A naive search for `Info.plist` picks the wrong file.** An IPA contains an `Info.plist` for every framework, app extension and plugin it bundles. Measured across the real IPAs on disk:

| IPA | nested `Info.plist` files besides the app's own |
|---|---|
| `43.4.0_AC.ipa` (TikTok) | **235** |
| `20.49.5.ipa` (YouTube) | 81 |
| `3.0.3.ipa` (AnyMex) | 77 |
| `409.0.0_TH.ipa` (Instagram) | 23 |
| `1.3.3.ipa` (qBitControl) | 0 |

Matching `name.endswith("Info.plist")` would return a *framework's* bundle identifier — plausible-looking and wrong. The match must be **exact**:

```python
_APP_INFO_PLIST = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")
```

`[^/]+` is what excludes `Payload/Foo.app/PlugIns/Bar.appex/Info.plist`. Verified: this regex returns exactly one match on all eight valid IPAs on disk.

## Design

**Extract, then confirm. Do not auto-publish.**

```
operator: [forwards Immich_vv3.1.0-AppAssassin.ipa]
bot:      Got Immich_vv3.1.0-AppAssassin.ipa — 31,145,000 bytes, sha256 4f2a…9c1b.
          Detected: app.alextran.immich  3.1.0

          Send /add to publish that, or /add <bundleIdentifier> <version> to override.
operator: /add
bot:      Published app.alextran.immich 3.1.0 (31,145,000 bytes).
```

- **`/add` with no arguments** publishes the detected values.
- **`/add <id> <version>` keeps working exactly as now** and overrides detection. Do not remove it — patched builds sometimes *should* be catalogued under a different identifier than the binary declares, and the operator must retain that control.
- If extraction fails, say so and require the explicit two-argument form.

Keeping a human confirmation step is deliberate. This is a catalog that installs software onto real devices; the value of this plan is removing *transcription errors*, not removing the gate.

**Version key precedence**: `CFBundleShortVersionString` first (the user-visible marketing version, e.g. `3.1.0`), falling back to `CFBundleVersion` (the build number, e.g. `434010`). AltStore sources display the marketing version. If both are missing, treat extraction as failed.

## Scope

**In scope**:
- `scripts/telegram_bot_ingest.py`
- `tests/test_telegram_bot_ingest.py`

**Out of scope — do NOT touch**:
- `app.py`, `compose.yml`, `Dockerfile.bot`, `requirements.txt`, `.env.example`, `.github/`.
- **`requirements.txt` must not gain anything.** `plistlib` and `zipfile` are both stdlib. Adding a dependency is a STOP condition.
- The allowlist check or its position as the first thing `handle_update` does.
- The five validation checks. Extraction happens **after** they pass, and a failed extraction must **not** become a sixth validation failure — an IPA with an unreadable plist is still a valid IPA and must still be publishable via the explicit form.
- `resolve_local_path` / `MountMismatchError`, the redaction helpers, the timeout config (Plan 020).
- Repairing the three mismatched catalog entries. Report them; changing `data/source.json` is a hand edit and an operator decision.
- Parsing the Telegram caption. AppAssassin's captions happen to carry the bundle id today, but that is a third party's formatting and can change without notice. The plist is authoritative.
- The 83 existing tests. If one needs editing to pass, report.

## Steps

### Step 1: The extractor

Add near `validate_ipa_file`:

```python
_APP_INFO_PLIST = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")


def extract_ipa_metadata(path):
    """Read (bundle_id, version) from an IPA's top-level Info.plist.

    Returns (None, None) if anything is unreadable -- a missing or odd
    plist is not a validation failure, it just means the operator must
    supply the values explicitly.

    The regex is deliberately exact: an IPA contains an Info.plist for
    every bundled framework and app extension (235 of them in one of the
    IPAs on disk), and a loose match would return a framework's identifier
    instead of the app's.
    """
```

- Open with `zipfile.ZipFile`, find names matching `_APP_INFO_PLIST`.
- Zero matches, or more than one → return `(None, None)`.
- Parse with `plistlib.loads(zf.read(name))`. `plistlib` reads **binary** plists as well as XML, and real IPAs are binary — do not add an XML-only path.
- `bundle_id = plist.get("CFBundleIdentifier")`.
- `version = plist.get("CFBundleShortVersionString") or plist.get("CFBundleVersion")`.
- Coerce both to `str` and `.strip()`; if either is falsy after that, return `(None, None)`.
- Wrap the whole body in `try/except Exception` returning `(None, None)`, and log at `warning` — via the redacted logger helper Plan 020 added, not a bare `logger`.

`import plistlib` and `import re` at the top if not already present.

**Verify** against real data — this is the check that matters:

```
.venv/bin/python -c "
import sys; sys.path.insert(0,'scripts')
from telegram_bot_ingest import extract_ipa_metadata
print(extract_ipa_metadata('data/ipas/com.google.ios.youtube/20.49.5.ipa'))
print(extract_ipa_metadata('data/ipas/com.ryan.anymex/3.0.3.ipa'))
"
```
→ `('com.google.ios.youtube', '20.49.5')` and `('com.ryan.anymex', '3.0.3')`.

If `data/` is absent (a worktree), skip this and say so — the unit tests below build their own fixtures.

### Step 2: Surface it on forward

In `handle_document`, after validation and the sha256, call the extractor and store the result in the pending record:

```python
    bundle_id, version = extract_ipa_metadata(local_path)
    pending[user_id] = {
        ...,
        "bundle_id": bundle_id,
        "version": version,
    }
```

Extend the reply. When both were found:

```
Detected: app.alextran.immich  3.1.0

Send /add to publish that, or /add <bundleIdentifier> <version> to override.
```

When not:

```
Could not read the bundle identifier from the IPA.
Send: /add <bundleIdentifier> <version>
```

### Step 3: Make `/add` accept no arguments

In `handle_add_command`, replace the strict `len(parts) != 3` check:

- `len(parts) == 1` → use `doc["bundle_id"]` / `doc["version"]`. If either is `None`, reply that detection failed and the explicit form is required; do not publish.
- `len(parts) == 3` → behave exactly as today, using the supplied values.
- Anything else → the usage message.

**Check the pending document exists before reading detected values** — the current code fetches `pending.get(user_id)` after parsing, and a bare `/add` with nothing pending must still produce "No pending file", not a crash.

**Verify**: `grep -c "len(parts)" scripts/telegram_bot_ingest.py` shows the branch exists; the tests below are the real check.

### Step 4: Tests

Add to `tests/test_telegram_bot_ingest.py`. Build IPA fixtures with `zipfile` + `plistlib.dumps(..., fmt=plistlib.FMT_BINARY)` under `tmp_path` — no network, no real IPA needed.

1. `test_extract_reads_bundle_id_and_version` — a minimal IPA with `Payload/App.app/Info.plist` → returns the expected pair.
2. `test_extract_ignores_nested_plists` — **the important one.** Build an IPA containing *both* `Payload/App.app/Info.plist` (`com.example.app`) **and** `Payload/App.app/PlugIns/Ext.appex/Info.plist` (`com.example.app.ext`), and assert the result is `com.example.app`. **Prove it discriminates**: loosen the regex to `Info.plist$`, confirm this test fails, restore it.
3. `test_extract_prefers_short_version_string` — plist with both keys → the marketing version wins.
4. `test_extract_falls_back_to_bundle_version` — only `CFBundleVersion` present → that is used.
5. `test_extract_returns_none_on_unreadable_plist` — garbage bytes at the plist path → `(None, None)`, no exception.
6. `test_add_with_no_args_uses_detected_values` — pending with detected values, bare `/add` → publishes with them.
7. `test_add_with_args_overrides_detection` — pending with detected values, `/add other.id 9.9.9` → publishes `other.id 9.9.9`.
8. `test_add_with_no_args_and_no_detection_refuses` — pending with `bundle_id=None`, bare `/add` → does **not** publish, replies asking for the explicit form.

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → **91** (83 + 8), the 83 existing unmodified.

## Done criteria

ALL must hold:

- [ ] `grep -c "import plistlib" scripts/telegram_bot_ingest.py` returns `1`
- [ ] `git diff requirements.txt` is empty — no dependency added
- [ ] The `Payload/[^/]+\.app/Info\.plist` regex is exact; test 2 fails when it is loosened (report both runs)
- [ ] Bare `/add` publishes detected values; `/add <id> <version>` still overrides
- [ ] A failed extraction does **not** block publishing via the explicit form
- [ ] Extraction failure is not raised as a `ValidationError`
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 91, the 83 pre-existing unmodified
- [ ] `git status --short` shows only the two in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

- You are tempted to add a dependency. `plistlib` and `zipfile` are stdlib.
- You are tempted to make extraction failure a validation failure. An IPA whose plist cannot be read is still a valid IPA.
- You are tempted to auto-publish on forward without the `/add` confirmation. The human gate is deliberate.
- You are tempted to remove the explicit `/add <id> <version>` form. Patched builds sometimes need a different identifier than the binary declares.
- You are tempted to parse the Telegram caption for these values. Third-party formatting, changes without notice.
- The regex matches more than one file in a real IPA. Report which — that would mean an IPA shape this plan did not anticipate.
- Any of the 83 existing tests needs editing.

## Maintenance notes

- **Real IPAs use binary plists.** `plistlib.loads` handles both formats transparently; anyone "simplifying" this to an XML parser will break every real file.
- **`CFBundleShortVersionString` and `CFBundleVersion` mean different things** — marketing version vs build number. The precedence here is deliberate; flipping it would put `434010` in the catalog where `43.4.0` belongs.
- **The filename is not a source of truth.** Two YouTube IPAs on disk are named `20.49.5` and declare `20.47.3`. Never parse the filename for a version.
- **Three existing catalog entries disagree with their binaries** (`com.instagram.theta` and `com.instagram.ifgram` both ship `com.burbn.instagram`; `com.michael-128.qBitControl` ships `MikeMichael225.qBitControl`). This plan does not touch them. Some may be deliberate — a renamed patched build can sit beside the real app — but they are worth an explicit decision rather than an accident.
- **Extraction only reads the top-level app bundle.** If an IPA ever legitimately contains two `.app` bundles at `Payload/` level, the extractor returns `(None, None)` rather than guessing, and the operator falls back to the explicit form. That is the intended failure mode.
- **The app name is also in that plist** (`CFBundleDisplayName` / `CFBundleName`) and would let a future `/newapp` command prefill everything. Out of scope here, but the extractor is the natural place for it.
