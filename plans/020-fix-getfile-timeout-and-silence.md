# Plan 020: Fix the `getFile` timeout and the silent-failure UX

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result. If anything in
> "STOP conditions" occurs, stop and report — do not improvise. When done,
> update the status row in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD
> grep -n "timeout=30" scripts/telegram_bot_ingest.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q   # expect 78 passed
> ```

## Status

- **Priority**: P1 — the feature does not work at all in its intended use
- **Effort**: S
- **Risk**: LOW
- **Depends on**: 013 (DONE)
- **Category**: bug
- **Planned at**: 2026-08-12

## Why this matters

**Observed in production.** The operator forwarded a 31.1 MB IPA to the bot twice. The bot answered `/start` and `/add` normally but replied **nothing at all** to either forward — then answered `/add` with "No pending file".

Two independent defects, both mine, both introduced by Plan 013.

### Defect 1 — `getFile` times out

`scripts/telegram_bot_ingest.py`:

```python
    def get_file(self, file_id):
        resp = self.session.get(
            self._url("getFile"), params={"file_id": file_id}, timeout=30
        )
```

On the **cloud** Bot API, `getFile` returns metadata instantly. On a **`--local`** server it does not: the server must download the file from Telegram's datacenters into its working directory before it can return an absolute path. `getFile` therefore blocks for the whole transfer.

Plan 013 specified `timeout=30` without accounting for that. 30 seconds is marginal for 31 MB and hopeless for the 83–353 MB files this exists to handle. The operator reported a transfer sitting at `0B` after four minutes.

When it times out, `requests` raises, and Defect 2 turns that into silence.

### Defect 2 — every failure is invisible to the user

`handle_document` performs both blocking calls **before** it sends anything:

```python
    file_info = bot.get_file(file_id)
    local_path = resolve_local_path(
        file_info.get("file_path"), config["bot_api_file_root"]
    )

    try:
        validate_ipa_file(local_path, filename, declared_size)
    except ValidationError as e:
        ...
        bot.send_message(chat_id, f"Rejected: {e}")
```

Only `ValidationError` is caught and reported. A `get_file` timeout or a `MountMismatchError` propagates to the polling loop, which does exactly what Plan 013 told it to:

```python
            except Exception:
                logger.exception("Error handling update %s", update.get("update_id"))
```

That is right for keeping the worker alive, and wrong for the operator, who sees nothing. The design has no way to say "I am working on it" or "that failed".

## Current state

Relevant excerpts from `scripts/telegram_bot_ingest.py`:

- `get_file` — `timeout=30` (the bug)
- `handle_document` — sends its first message only *after* `get_file` and `resolve_local_path` succeed
- the polling loop — `except Exception: logger.exception(...)`, no user-facing reply

`REQUIRED_VARS` currently lists eight names. This plan adds one **optional** setting with a default; do not add it to `REQUIRED_VARS`.

## Design

Three changes, smallest first.

**1. A generous, configurable timeout.** Add `BOT_API_GETFILE_TIMEOUT`, default **900** seconds. Optional — absent means 900. At a pessimistic 1 MB/s, 900 s covers ~900 MB, comfortably past the largest file in the catalog (353 MB). It is a ceiling, not a delay: a fast transfer returns immediately.

**2. Acknowledge before blocking.** Send a message *before* calling `get_file`, so a multi-minute download looks like progress rather than a dead bot:

```
Fetching Immich_vv3.1.0-AppAssassin.ipa (31.1 MB) from Telegram — this can take a few minutes for large files.
```

**3. Report failures to the user, not just the log.** In the polling loop's catch-all, attempt a reply naming what went wrong, then carry on. The reply attempt must itself be wrapped — if Telegram is unreachable, the worker still must not die.

## Scope

**In scope**:
- `scripts/telegram_bot_ingest.py`
- `.env.example` — one optional key name
- `tests/test_telegram_bot_ingest.py`

**Out of scope — do NOT touch**:
- `app.py`, `compose.yml`, `Dockerfile.bot`, `.github/`, `requirements.txt`.
- The allowlist check, or its position as the first thing `handle_update` does. STOP condition.
- The five validation checks. Do not weaken any to a warning.
- `resolve_local_path`'s `MountMismatchError` behaviour — it must still raise rather than fall back to an HTTP download. This plan makes that failure *visible*, not tolerated.
- `REQUIRED_VARS` — the new setting is optional with a default; adding it there would break every existing deployment on restart.
- The 78 existing tests. If one needs editing to pass, report.

## Steps

### Step 1: Configurable timeout

Read `BOT_API_GETFILE_TIMEOUT` alongside the other config, defaulting to `900`, and pass it into `BotAPIClient` for use by `get_file` only. Leave `getUpdates` (long-poll, 30 s) and `sendMessage` at their current timeouts — they are not affected.

Add the key name to `.env.example` with **no value**.

**Verify**: `grep -c "timeout=30" scripts/telegram_bot_ingest.py` no longer matches the `getFile` call — confirm by reading `get_file`, not by the count alone, since other calls legitimately use 30.

### Step 2: Acknowledge before the blocking call

In `handle_document`, send the "Fetching …" message **before** `bot.get_file(...)`. Include the filename and the declared size.

Guard it: if `send_message` fails, log and continue to the fetch anyway — a failed courtesy message must not block the actual work.

### Step 3: Make failures visible

In the polling loop's `except Exception` handler, after `logger.exception(...)`, attempt to reply to the originating chat with a short error naming the exception type and message. Wrap that attempt in its own `try/except` that only logs — a failure to report must never kill the loop.

Extracting the chat id inside the handler is fiddly; the simplest correct approach is for `handle_update` to determine `chat_id` early and re-raise a wrapped error carrying it, or for the loop to re-derive `chat_id` from the update. **Either is acceptable — pick one and say which.** Do not restructure the dispatcher beyond what this needs.

**Never include a token, password, or file path root in a user-facing error.**

### Step 4: Tests

Add to `tests/test_telegram_bot_ingest.py`:

1. `test_getfile_timeout_defaults_to_900` — with the env var unset, the client is constructed with 900.
2. `test_getfile_timeout_is_configurable` — set it to `120`, assert that value reaches the `get_file` request.
3. `test_ack_sent_before_getfile` — with a fake client recording call order, assert a `sendMessage` occurs **before** `getFile`. This is the regression test for the silence.
4. `test_getfile_failure_replies_to_user` — make `get_file` raise, assert the user receives a message mentioning the failure and that the loop does not propagate the exception.
5. `test_reply_failure_does_not_kill_loop` — make both `get_file` **and** `send_message` raise; assert the loop survives.

No test may touch the network. Reuse the existing fake-client pattern.

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → **83** (78 + 5), the 78 existing unmodified.

## Done criteria

ALL must hold:

- [ ] `get_file` uses the configurable timeout, defaulting to 900
- [ ] `grep -c "BOT_API_GETFILE_TIMEOUT" .env.example` returns `1`, with no value
- [ ] `BOT_API_GETFILE_TIMEOUT` is **not** in `REQUIRED_VARS`
- [ ] A "Fetching …" message is sent before `get_file` — proven by the call-order test
- [ ] A `get_file` failure produces a user-facing reply, proven by test 4
- [ ] A failure to send that reply does not kill the loop, proven by test 5
- [ ] The allowlist is still the first thing `handle_update` does
- [ ] All five validation checks remain hard failures
- [ ] `resolve_local_path` still raises `MountMismatchError`; no HTTP-download fallback exists
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 83, the 78 pre-existing unmodified
- [ ] `git status --short` shows only the three in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

- You are tempted to add an HTTP-download fallback when the local path is missing. That reintroduces the 20 MB cap the whole feature exists to avoid.
- You are tempted to move or weaken the allowlist check.
- You are tempted to add `BOT_API_GETFILE_TIMEOUT` to `REQUIRED_VARS`. That would break the operator's running deployment on next restart.
- You find yourself making the worker download the file itself rather than reading it off the shared volume.
- Any of the 78 existing tests needs editing.

## Maintenance notes

- **`getFile` is synchronous over the download in `--local` mode.** That single fact is what made a 30-second timeout wrong, and it is not obvious from the Bot API docs, which describe the cloud behaviour. Anyone tuning timeouts here should start from expected file size ÷ realistic throughput.
- **The acknowledge-then-work shape matters more than the timeout.** Even with a correct timeout, a several-minute silent gap is indistinguishable from a dead bot. Any future long operation in this worker should follow the same pattern.
- **Progress reporting is deliberately not included.** The Bot API gives no download-progress callback in `--local` mode, so a percentage would have to be faked. A single honest "this can take a few minutes" beats an invented progress bar.
- **The real fix for very large files may be asynchrony**: acknowledge, hand the fetch to a worker thread, and reply on completion, so one 353 MB download does not block the polling loop for everyone. Not needed for a single-admin tool; revisit if a second allowlisted user is ever added.
