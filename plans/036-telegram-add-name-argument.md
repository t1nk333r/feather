# Plan 036: Let the Telegram `/add` command set a custom app name (`/add <id> <ver> <name>`)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 3eeb0cc..HEAD -- scripts/telegram_bot_ingest.py tests/test_telegram_bot_ingest.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_telegram_bot_ingest.py -q   # expect: 32 passed
> ```
> If `scripts/telegram_bot_ingest.py` changed since `3eeb0cc`, compare the
> "Current state" excerpts below against the live code before proceeding; on a
> mismatch, STOP.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW (a Telegram-worker command-parsing change; no server/API change)
- **Depends on**: none
- **Category**: feature
- **Planned at**: commit `3eeb0cc`, 2026-08-17

## Why this matters

The operator hosts **patched app builds that share a display name and bundle
identifier** (e.g. two different patched Twitters that both declare
`com.atebits.Tweetie2` / "Twitter"). To keep them as *distinct* catalog entries,
the operator already assigns each a distinct bundle identifier via
`/add <bundleIdentifier> <version>` — but there is **no way to give them a
distinct name**, so both created apps end up named from the IPA's own
`CFBundleDisplayName` ("Twitter"), which is confusing and defeats the point.

This plan adds an optional third argument: `/add <bundleIdentifier> <version> <name>`.
When the bot *creates* a new catalog app, the operator-supplied name overrides
the IPA-detected name, so patched variants can be labeled distinctly ("Twitter —
Patched A", "Twitter — Nitter build", etc.). The name may contain spaces.

**Scope note (read this):** this feature only sets the *name* on **app
creation**. It does **not** prevent duplicate *versions* on the same bundle —
re-sending `/add` for a bundle+version that already exists still appends a
version through `/api/add-version`. That duplicate-version protection is a
separate, server-side concern (a proposed serve-path/`add_version` dedup fix) and
is out of scope here. State this in the maintenance notes so it isn't conflated.

## Current state — `scripts/telegram_bot_ingest.py`

- The usage string (line 60):
  ```python
  USAGE_HINT = "Send:  /add <bundleIdentifier> <version>"
  ```
- The command handler `handle_add_command(user_id, chat_id, text, config, bot, feather, pending)` (line 549). Its argument parsing (lines 550–577):
  ```python
  parts = text.split()
  if len(parts) not in (1, 3):
      bot.send_message(chat_id, f"Usage: {USAGE_HINT.split(':', 1)[1].strip()}")
      return
  ...
  doc = pending.get(user_id)
  if doc is None:
      bot.send_message(chat_id, "No pending file. Forward an IPA first, then " + USAGE_HINT.strip())
      return

  if len(parts) == 1:
      bundle_id = doc.get("bundle_id")
      version = doc.get("version")
      if not bundle_id or not version:
          bot.send_message(chat_id, "No bundle identifier/version was detected for this file. "
                           f"Usage: {USAGE_HINT.split(':', 1)[1].strip()}")
          return
  else:
      _, bundle_id, version = parts
  ```
- The create branch (lines 590–595) — the ONLY place a name is used:
  ```python
  if not ok and message == "App not found":
      app_name = doc.get("name") or bundle_id
      developer = config["telegram_default_developer"]
      ok, message = feather.add_app(bundle_id, version, app_name, developer, doc["path"])
  ```
- `FeatherClient.add_app(self, bundle_id, version, name, developer, path)` (line 429) already sends `"name": name` to `/api/add-app`, which uses it — so **no API or app.py change is needed**; the name just needs to flow from the command into `app_name`.

### Repo conventions

- Tests live in `tests/test_telegram_bot_ingest.py`. The exemplar for command
  parsing is `test_add_command_parses_bundle_and_version` (line 332). Tests use a
  `FakeFeatherClient` (line 112) whose `add_app_calls` records
  `(bundle_id, version, name, developer, path)` tuples — assert the name off
  that. Commands are delivered via `ingest.handle_update(text_update(ALLOWED_USER_ID, "<text>"), config, bot, feather, pending)`.
- No network in tests. Conventional commits.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline (bot) | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_telegram_bot_ingest.py -q` | `32 passed` before changes |
| Baseline (full) | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `170 passed` before changes |
| Syntax | `.venv/bin/python -m py_compile scripts/telegram_bot_ingest.py` | exit 0 |
| Bot tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_telegram_bot_ingest.py -q` | `32 + N passed` |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `170 + N passed` |

## Scope

**In scope** (modify only these):
- `scripts/telegram_bot_ingest.py` — `USAGE_HINT` and `handle_add_command` only.
- `tests/test_telegram_bot_ingest.py` — add cases.

**Out of scope** (do NOT touch):
- `app.py`, `/api/add-app`, `SourceManager`, the UI, the release importer — no server change is needed; the name already flows through `add_app`.
- `FeatherClient.add_app` / `add_version` signatures — unchanged.
- Any duplicate-**version** handling — explicitly a separate concern (see Why).
- `requirements.txt`, container/CI files.

## Git workflow

- Branch: `advisor/036-telegram-add-name-argument`
- Commits: parsing/handler change, then tests. Do NOT push or open a PR.

## Steps

### Step 1: Confirm baseline
```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_telegram_bot_ingest.py -q   # 32 passed
```
If not 32, STOP.

### Step 2: Update the usage hint

Change `USAGE_HINT` (line 60) to include the optional name:
```python
USAGE_HINT = "Send:  /add <bundleIdentifier> <version> [name]"
```

### Step 3: Parse the optional name in `handle_add_command`

Change the parsing so it accepts 1, 3, or 4 tokens, and captures the name
(everything after the version, spaces allowed) when present. Use
`text.split(maxsplit=3)` so the name keeps its internal spacing:
```python
parts = text.split(maxsplit=3)
if len(parts) not in (1, 3, 4):
    bot.send_message(chat_id, f"Usage: {USAGE_HINT.split(':', 1)[1].strip()}")
    return
```
Keep the `doc = pending.get(user_id)` / "No pending file" block exactly as is.
Then set `bundle_id` / `version` / `name_override`:
```python
name_override = None
if len(parts) == 1:
    bundle_id = doc.get("bundle_id")
    version = doc.get("version")
    if not bundle_id or not version:
        bot.send_message(chat_id, "No bundle identifier/version was detected for this file. "
                         f"Usage: {USAGE_HINT.split(':', 1)[1].strip()}")
        return
else:
    bundle_id = parts[1]
    version = parts[2]
    if len(parts) == 4:
        name_override = parts[3].strip() or None
```
(Replaces the old `_, bundle_id, version = parts`, which fails for the 4-token
form.)

### Step 4: Use the name override on creation

In the create branch (line 591), prefer the operator-supplied name:
```python
if not ok and message == "App not found":
    app_name = name_override or doc.get("name") or bundle_id
    developer = config["telegram_default_developer"]
    ok, message = feather.add_app(bundle_id, version, app_name, developer, doc["path"])
```
Do not change anything else in that block (icon-on-create, error handling).

**Verify**: `.venv/bin/python -m py_compile scripts/telegram_bot_ingest.py` → exit 0.

### Step 5: Tests

Add to `tests/test_telegram_bot_ingest.py`, modeled on
`test_add_command_parses_bundle_and_version` (line 332) and using
`FakeFeatherClient` (record via `add_app_calls`). A new app is created only when
`add_version` returns `(False, "App not found")`, so construct the fake with
`add_version_result=(False, "App not found")` for the create cases.

1. `test_add_command_name_argument_sets_created_app_name` — pending file whose
   detected `name` is `"Twitter"`; send `/add com.custom.patched 12.17 My Patched Twitter`;
   `add_version` returns `(False, "App not found")`; assert `add_app` was called
   once with `bundle_id="com.custom.patched"`, `version="12.17"`, and
   `name="My Patched Twitter"` (NOT the detected "Twitter").
2. `test_add_command_name_defaults_to_detected_when_omitted` — same setup but
   `/add com.custom.patched 12.17` (no name); assert `add_app` name is the
   detected `"Twitter"` (unchanged behavior).
3. `test_add_command_name_ignored_when_app_exists` — `add_version` returns
   `(True, "ok")` (app already exists); send `/add com.x 1.0 Some Name`; assert
   `add_app` was NOT called (`add_app_calls == []`) and the publish succeeded.
4. `test_add_command_two_tokens_is_usage_error` — send `/add com.x` (2 tokens);
   assert a usage message was sent and neither `add_version` nor `add_app` ran,
   and the pending doc is untouched.

**Prove discrimination**: temporarily revert Step 4 (`app_name = doc.get("name") or bundle_id`)
→ test 1 fails (name is "Twitter", not "My Patched Twitter"). Restore.

**Verify**:
```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_telegram_bot_ingest.py -q
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

## Test plan

- 4 new tests in `tests/test_telegram_bot_ingest.py` covering: name sets the
  created app's name, name-omitted falls back to detected, name ignored on an
  existing app, and the 2-token usage error.
- Verify the bot suite is `32 + 4` and the full suite is `170 + 4`.

## Done criteria

- [ ] `/add <id> <ver> <name>` creates a new app with `<name>` (spaces allowed) as the app name.
- [ ] `/add` and `/add <id> <ver>` behave exactly as before (name from detection).
- [ ] A name is never applied when the app already exists (add-version path) — `add_app` isn't called.
- [ ] Invalid token counts (2, or 5+ that don't parse) give the usage message; nothing is published; pending is untouched.
- [ ] `USAGE_HINT` mentions `[name]`.
- [ ] `.venv/bin/python -m py_compile scripts/telegram_bot_ingest.py` → exit 0.
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → all pass (170 + 4).
- [ ] `git diff app.py templates/index.html` empty (no server/UI change).
- [ ] `git status --short` shows only the two in-scope files.

## STOP conditions

- `handle_add_command` no longer parses `/add` as shown (the worker was refactored) — reconcile before editing.
- Sending the name would require changing `FeatherClient.add_app` or `/api/add-app` (it should not — `add_app` already forwards `name`).
- A test needs real network or a real Telegram/Feather endpoint.

## Maintenance notes

- This sets the name **only on creation**. Renaming an existing app stays an
  Edit-App action; and this does **not** dedupe versions — re-sending `/add` for
  an already-catalogued bundle+version still appends a duplicate version. If
  duplicate versions are a problem, that belongs in a separate serve-path /
  `add_version` dedup fix, not here.
- If a future change adds more `/add` flags, keep `text.split(maxsplit=3)` so the
  trailing name argument keeps its spaces; adding flags after the name would
  require a real option parser.
- A reviewer should confirm: the name flows only into the create branch, the
  1- and 3-token paths are byte-for-byte behavior-preserving, and no server file
  changed.
