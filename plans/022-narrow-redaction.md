# Plan 022: Narrow the redaction to actual secrets

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result. If anything in
> "STOP conditions" occurs, stop and report — do not improvise. When done,
> update the status row in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD
> grep -n "bot_api_file_root" scripts/telegram_bot_ingest.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q   # expect 91 passed
> ```

## Status

- **Priority**: P2 — not a failure, but it actively obstructs diagnosis of failures
- **Effort**: XS
- **Risk**: LOW — narrowing what is scrubbed; the token and password stay scrubbed
- **Depends on**: 020 (DONE)
- **Category**: bug (observability)
- **Planned at**: 2026-08-12

## Why this matters

Plan 020 added central redaction because the Bot API puts the token in the URL path and `requests` echoes the URL in every exception. That part is right and must stay.

But I told it to scrub **three** values: the bot token, the feather admin password, **and `BOT_API_FILE_ROOT`**. The third is not a secret — it is `/var/lib/telegram-bot-api`, a mountpoint that appears in `compose.yml`, in this plan, and in the container's own startup line.

The cost showed up immediately in production. A real `MountMismatchError` reached the operator as:

```
MountMismatchError: path from getFile does not exist inside the worker:
'<REDACTED>/<REDACTED>/documents/file_0.ipa' (BOT_API_FILE_ROOT='<REDACTED>')
-- shared volume mounts disagree
```

Because the file path *begins with* the file root, redacting the root also mangled the path. The message names the failure and then hides every fact needed to act on it: which path, under which root. The one `<REDACTED>` that belongs there is the token embedded mid-path — that one is genuine and must remain.

The decoded message should have read:

```
path from getFile does not exist inside the worker:
'/var/lib/telegram-bot-api/<REDACTED>/documents/file_0.ipa'
(BOT_API_FILE_ROOT='/var/lib/telegram-bot-api') -- shared volume mounts disagree
```

Same security, and diagnosable.

## Current state

`scripts/telegram_bot_ingest.py`. `_redact` itself is correct and general:

```python
def _redact(text, *secrets):
    """Replace every occurrence of any given secret substring with <REDACTED>.
    ...
    Accepts multiple secrets (token, feather admin password, the local
    file-root config value) so callers can scrub everything sensitive that
    might appear in a single pass.
    """
```

The bug is in **what callers pass**, plus that docstring sentence describing the file root as sensitive. Call sites are at roughly lines 131, 323 and 580 (`grep -n "_redact(" scripts/telegram_bot_ingest.py`).

## What counts as a secret here

| Value | Secret? | Why |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **yes** | full control of the bot; sits in the URL path, so it leaks through any exception |
| `FEATHER_ADMIN_PASSWORD` | **yes** | publishes to the catalog |
| `TELEGRAM_API_HASH` | **yes** | account-scoped; redact if it can appear in any message |
| `BOT_API_FILE_ROOT` | **no** | a mountpoint, public in `compose.yml` and the container's startup line |
| `FEATHER_BASE_URL`, `BOT_API_BASE_URL` | **no** | internal service names |
| `TELEGRAM_ALLOWED_USER_IDS` | **no** | not a credential, and useful when diagnosing a rejected sender |

## Scope

**In scope**: `scripts/telegram_bot_ingest.py`, `tests/test_telegram_bot_ingest.py`.

**Out of scope — do NOT touch**:
- `_redact` itself. The function is fine; only its *arguments* change.
- Removing redaction of the token or the admin password. STOP condition.
- `compose.yml`, `app.py`, `.env.example`, `Dockerfile.bot`, `requirements.txt`, `.github/`.
- The allowlist, the five validation checks, `resolve_local_path`'s raising behaviour, the Plan 021 extractor.
- The 91 existing tests, **except** any that assert the file root is redacted — if one does, it encodes the bug and should be updated. Say clearly which test you changed and why; do not touch any other.

## Steps

### Step 1: Stop passing the file root as a secret

Find every `_redact(...)` call and every place that builds the secrets tuple. Remove `bot_api_file_root` (or `config["bot_api_file_root"]`) from the secrets passed.

Keep passing the bot token and the feather admin password. **Add `TELEGRAM_API_HASH`** if it is available at those call sites and could appear in a message — if it is not currently passed, leave it and note that in your report rather than plumbing new config through.

Fix the `_redact` docstring: it currently cites "the local file-root config value" as an example of something sensitive. Replace that with a note that only genuine credentials belong here, and that non-secret configuration must **not** be passed, because scrubbing a path prefix destroys the diagnostic value of path errors.

**Verify**: `grep -n "bot_api_file_root" scripts/telegram_bot_ingest.py` shows it used only for its real purpose (resolving and comparing paths), never as a `_redact` argument.

### Step 2: Prove the mount error is now readable

Add a test asserting a `MountMismatchError` surfaced to the user contains the real root and the real path, while a token embedded in that path is still `<REDACTED>`.

Construct it the way production did: file root `/var/lib/telegram-bot-api`, reported path `/var/lib/telegram-bot-api/<fake-token>/documents/file_0.ipa`, with the fake token also configured as the bot token. Use an obviously fake token such as `123456:FAKE_TOKEN_FOR_TESTS` — never a real one.

Assert all of:
- `"/var/lib/telegram-bot-api"` **is** present
- `"documents/file_0.ipa"` **is** present
- the fake token is **absent**
- `"<REDACTED>"` **is** present (the token position)

**Verify**: this test fails if you re-add the file root to the secrets list. Demonstrate that — break it, run, restore, run — and report both.

### Step 3: Full suite

`ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → **92** (91 + 1), or 91 + 1 with one pre-existing test corrected if one asserted the old behaviour. State which.

## Done criteria

ALL must hold:

- [ ] No `_redact(...)` call passes the file root
- [ ] The bot token is still redacted everywhere — proven by the existing Plan 020 redaction test still passing
- [ ] The feather admin password is still redacted
- [ ] A `MountMismatchError` message retains the real root and path while the token stays `<REDACTED>` — proven by the new test
- [ ] That new test fails when the file root is re-added to the secrets list (report both runs)
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` passes; state the count and name any pre-existing test you changed
- [ ] `git status --short` shows only the two in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

- You are tempted to stop redacting the token or the admin password. Both stay — the token is why Plan 020 exists.
- You are tempted to remove `_redact` entirely because "the file root was the only problem". The token leak is real and central redaction is what catches future call sites.
- More than one existing test asserts the file root is redacted. That would mean the behaviour is depended on somewhere unexpected — report the list rather than editing them all.

## Maintenance notes

- **The rule this encodes**: redact credentials, never configuration. Scrubbing a path prefix silently destroys the readability of every message containing a path under it — which is exactly the class of message you most need when a mount is wrong.
- **The token genuinely does appear mid-path**, because the Bot API's URL scheme is `/bot<token>/method`. A path-shaped `<REDACTED>` in the middle of a file path is correct and expected; that is not the bug.
- **Over-redaction is a real cost, not free safety.** It cost roughly one debugging round-trip here. When adding a value to the secrets list, ask whether it appears in `compose.yml` or a startup log line — if it does, it is not a secret.
