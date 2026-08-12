# Plan 014: Telegram notifications on catalog changes

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD          # plan written against 62c8642
> md5sum app.py                       # expect d6299fef0f55f6b8bc4bd69f31fae283
> wc -l < app.py                      # expect 1587
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q   # expect 108 passed
> ```
> The `ADMIN_PASSWORD=x` prefix is required — Plan 010 landed and the app
> refuses to import without it.

## Status

- **Priority**: P3 (quality-of-life; nothing is broken without it)
- **Effort**: S
- **Risk**: LOW — additive, disabled by default, and structurally incapable of failing a catalog write. The risk that matters is the one this plan is built to avoid: a notification bug taking down publishing.
- **Depends on**: 005 (tests), 010 (auth — the routes being hooked are now gated). Both DONE.
- **Relationship to 013**: independent, but shares `TELEGRAM_BOT_TOKEN` and composes with 013's self-hosted server if present. Neither blocks the other. See "Composing with Plan 013".
- **Category**: feature
- **Planned at**: 2026-08-12, `62c8642`. **Refreshed 2026-08-12** against `c01db83`; `app.py` md5 `d6299fef0f55f6b8bc4bd69f31fae283`, 1587 lines. Plans 021/023/024 landed in between and moved every line reference below.

## Why this matters

Publishing a build is currently silent. Nobody learns a new version exists until a device happens to poll `source.json`, and the operator gets no confirmation that a 350 MB upload actually landed in the catalog rather than failing somewhere in the middle.

A short message on each catalog change closes both gaps: the operator sees "published", and anyone in the notify chat learns a new build is available without polling.

## The one rule this plan exists to enforce

**A notification failure must never fail, delay, or roll back a catalog write.**

`data/source.json` is the product. Plans 006 and 007 went to real trouble to make writes atomic and to make failures honest. Bolting a network call onto that path is exactly how you'd undo it — a hung Telegram API turning into a hung upload, or a notify exception turning a successful publish into an HTTP 400.

Three structural consequences, all non-negotiable:

1. **Notify from the route layer, after `SourceManager` returns** — never from inside `SourceManager`. Those methods hold `self._lock` (Plan 006) across their whole read-modify-write, and some of them download IPAs while holding it. Adding network I/O inside the lock would serialise every publish behind Telegram's latency.
2. **Send on a daemon thread**, so the HTTP response is not waiting on Telegram at all. A short timeout alone is not enough — a 5 s stall on every publish is still a regression.
3. **Swallow every exception** in the notifier and log at `warning`. There is no failure mode in which a notification problem should surface to the caller.

## Current state

**The hook point** — `app.py:1473` onward, `/api/add-version`. Every mutating route has this identical shape, and the `success, message = source_manager.…` line is where the lock has already been released:

```python
        success, message = source_manager.add_version(bundle_id, data, ipa_file=ipa_file if ipa_file and ipa_file.filename else None, download_from_url=download_from_url, base_url=base_url)

        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400

    except Exception as e:
        logging.error(f"Error adding version: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400
```

**The config block** to extend — `app.py:36-50`, following the pattern Plan 001 established:

```python
SECRET_KEY = os.environ.get("SECRET_KEY")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise RuntimeError(
        "ADMIN_PASSWORD is not set. Refusing to start with unauthenticated "
        "admin routes. Set it in .env (compose.yml loads it via env_file)."
    )
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL")
PORT = int(os.environ.get("PORT", "5000"))
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 2 * 1024 * 1024 * 1024))
```

Note the shape: read with `os.environ.get`, and only `raise` when the thing is genuinely required. **Notifications are optional — do not raise when they are unconfigured.**

**Already available**: `requests` and `threading` are both imported already. `resolve_base_url()` is at `app.py:92`, and `normalize_source()` — added by Plan 024 — sits just after it at `app.py:108`; put `notify()` near them. **No new dependency is needed and none may be added.**

**Repo conventions**: module-level constants for config; `logging.warning(f"...")` for degraded-but-working; `logging.error(f"...: {str(e)}")` for failures; 4-space indent; no type annotations.

## Design

```python
TELEGRAM_BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_NOTIFY_CHAT_ID = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID")
TELEGRAM_API_BASE      = os.environ.get("BOT_API_BASE_URL", "https://api.telegram.org")
TELEGRAM_NOTIFY_EVENTS = os.environ.get("TELEGRAM_NOTIFY_EVENTS", "add_app,add_version,delete_app")
```

**Disabled by default.** If either the token or the chat id is unset, `notify()` returns immediately. Log that fact **once at startup** at `info` — not on every call, or a normal unconfigured deployment fills its log with noise.

A single module-level helper:

```python
def notify(event, text):
    """Fire-and-forget Telegram message. Never raises, never blocks the caller."""
```

- Return immediately if disabled, or if `event` is not in the configured event set.
- Spawn `threading.Thread(target=..., daemon=True).start()`.
- Inside the thread: `requests.post(f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": ..., "text": ..., "disable_web_page_preview": True}, timeout=10)`, wrapped in `try/except Exception` that logs at `warning`.
- `daemon=True` matters: a hung request must not stop the process exiting.

### Send plain text — do not use `parse_mode`

Telegram's `MarkdownV2` requires escaping `_ * [ ] ( ) ~ \` > # + - = | { } . !`, and app names and version strings are **attacker-supplied and stored**. A version string containing `_` or `[` would either break the message or, with sloppy escaping, let a stored value inject formatting.

This codebase has already been bitten by exactly this class of bug: Plan 010 had to fix a version string interpolated into `innerHTML` without escaping while every sibling field was escaped.

**Send with no `parse_mode` at all.** Plain text needs no escaping, and the value here is knowing a build shipped — not bold text.

### Events

| Event | Message |
|---|---|
| `add_app` | `New app published: <name> (<bundleId>) v<version>` |
| `add_version` | `New version: <name> <version> — <size> bytes` |
| `delete_app` | `App removed: <bundleId>` |

Include the source URL from `resolve_base_url()` on `add_app`/`add_version` so the message is actionable.

**Deliberately not notified**: `update_app`, `update_version`, `update_source`. They fire on every metadata tweak and would train the operator to ignore the channel. `TELEGRAM_NOTIFY_EVENTS` makes that reversible without a code change.

## Composing with Plan 013

013 (self-hosted Bot API server + forward-to-bot ingest) is independent of this plan and neither blocks the other. They overlap in one place: both use `TELEGRAM_BOT_TOKEN`.

- If 013 has landed, set `BOT_API_BASE_URL=http://telegram-bot-api:8081` and notifications go through the local server too — one code path, no special-casing.
- If 013 has not landed, the default `https://api.telegram.org` is correct. Text messages are far below any size limit, so the cloud API is fine.

**Do not make this plan depend on 013**, and do not add any of 013's containers or config here. If 013 has already landed and `TELEGRAM_BOT_TOKEN` is already in `.env.example`, do not duplicate the line.

## Interaction with what landed since this plan was written

Plans 021, 023 and 024 all touched this area. None conflict, but:

- **`/api/add-app` is now reachable from the Telegram bot** (Plan 023), so an `add_app` notification will fire for bot-created apps as well as web-UI ones. That is desirable — it is the case where you most want to know.
- **`normalize_source()` (Plan 024) runs on the `/source.json` serve path.** `notify()` runs on the mutating routes. They never interact; do not call one from the other.
- **The three route line numbers**: `/api/add-app` at `app.py:1356`, `/api/delete-app` at `app.py:1396`, `/api/add-version` at `app.py:1473`.

## Scope

**In scope**:
- `app.py` — the config block, one `notify()` helper, and one call in each of three route handlers
- `.env.example` — the new key names, no values
- `tests/test_routes.py` — the cases in the test plan

**Out of scope — do NOT touch**:
- `SourceManager` and anything inside it. **The notify call goes in the route, never in the manager** — see "The one rule".
- `save_source`, `load_source`, `_backup_source`, the `_lock` machinery (Plan 006).
- `update_app`, `update_version`, `update_source` routes — not notified, by design.
- `resolve_base_url` / `get_local_ipa_url` (Plan 008) beyond *calling* the former.
- The storage classes (Plan 011), `templates/`, `Dockerfile`, `compose.yml`.
- `requirements.txt` — `requests` and `threading` are already available. **Adding a dependency is a STOP condition.**
- Retries, queues, delivery guarantees. This is fire-and-forget; a dropped notification is acceptable and a retry loop is not worth the complexity on a single-admin tool.
- The 108 existing tests. If one needs editing to pass, you changed behaviour you should not have — report.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | 70 before, 75 after |
| Syntax | `ADMIN_PASSWORD=x .venv/bin/python -m py_compile app.py` | exit 0 |
| Disabled by default | see Step 1 | no network call |

## Git workflow

- Branch: `advisor/014-telegram-notifications`
- Commits: (1) config + `notify()` helper, (2) the three route hooks, (3) tests.

## Steps

### Step 1: Config and the helper

Add the four variables to the config block after `MAX_CONTENT_LENGTH`, and the `notify()` helper near `resolve_base_url()` (`app.py:92`) / `normalize_source()` (`app.py:108`).

**Do not raise when unconfigured.** Log once at startup:

```python
if TELEGRAM_BOT_TOKEN and TELEGRAM_NOTIFY_CHAT_ID:
    logging.info("Telegram notifications enabled for events: %s", TELEGRAM_NOTIFY_EVENTS)
else:
    logging.info("Telegram notifications disabled (TELEGRAM_BOT_TOKEN / TELEGRAM_NOTIFY_CHAT_ID not set)")
```

Never log the token, never include it in an error message, and never put it in a test fixture as anything but an obviously-fake value.

**Verify**:
```
ADMIN_PASSWORD=x .venv/bin/python -m py_compile app.py
ADMIN_PASSWORD=x DATA_DIR=/tmp/n1 .venv/bin/python -c "
import app
app.notify('add_version', 'test')   # must return immediately, no exception, no network
print('ok')
"
```
→ prints `ok`.

### Step 2: Hook the three routes

In `/api/add-app`, `/api/add-version` and `/api/delete-app`, add a single `notify(...)` call **inside the `if success:` branch only**, immediately before the `return jsonify(...)`.

Never notify on the failure branch — a failed publish is not news, and Plan 007 already reports it to the caller.

**Verify**: `grep -n "notify(" app.py` — one definition plus three call sites, each read and confirmed to sit inside an `if success:` branch.

### Step 3: Tests

Per the test plan below.

## Test plan

Add to `tests/test_routes.py`, reusing the existing `authed_client` fixture (Plan 010 — the mutating routes now require a session).

1. **`test_notify_is_noop_when_unconfigured`** — with the env vars unset, call a mutating route and assert `requests.post` was never called. Monkeypatch `app.requests.post` with a recorder. This is the default-path guard.
2. **`test_notify_sends_on_add_version`** — set token and chat id, call `/api/add-version`, assert a `sendMessage` POST happened with the right `chat_id` and a body containing the version string. **Join the thread** or monkeypatch `threading.Thread` to run synchronously so the assertion is not racy.
3. **`test_notify_failure_does_not_fail_the_request`** — the critical one. Make `requests.post` raise, then call `/api/add-version` and assert the route still returns **200** and the version still lands in the catalog. **This test must fail if the `try/except` in the notifier is removed** — prove that.
4. **`test_notify_respects_event_filter`** — with `TELEGRAM_NOTIFY_EVENTS=add_app`, an `add_version` call sends nothing.
5. **`test_notify_sends_no_parse_mode`** — assert the POST payload has no `parse_mode` key. This pins the escaping decision; a future "let's make it bold" change should have to delete this test deliberately.

**No test may make a real network call.** The suite runs in ~3.5 s with no network and that property is worth protecting.

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → **113** (108 + 5), the 108 existing unmodified.

## Done criteria

ALL must hold:

- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m py_compile app.py` exits 0
- [ ] `grep -n "notify(" app.py` shows exactly one `def` and **three** call sites, each inside an `if success:` branch. Count the call sites by reading, not by a bare `grep -c` — a docstring mentioning `notify(` would inflate it, and this repo has already had three done-criteria fail on exactly that kind of arithmetic.
- [ ] All three call sites are inside an `if success:` branch — verified by reading, not just grepping
- [ ] `grep -c "parse_mode" app.py` returns `0`
- [ ] `grep -c "daemon=True" app.py` returns `1`
- [ ] With the env vars unset, a mutating route makes no `requests.post` call
- [ ] A raising `requests.post` still yields HTTP 200 and a persisted version — and that test fails if the notifier's `try/except` is removed
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 113, the 108 pre-existing unmodified
- [ ] `git diff requirements.txt` is empty — no dependency added
- [ ] `grep -c "TELEGRAM_NOTIFY_CHAT_ID" .env.example` returns `1`; `.env.example` contains no values
- [ ] No `SourceManager` method was modified (`git diff app.py` shows no change inside the class)
- [ ] `git status --short` shows only `app.py`, `.env.example`, `tests/test_routes.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report (do not improvise) if:

- You find yourself adding a Python dependency. `requests` and `threading` are already imported; anything else is out of scope.
- You find yourself putting a `notify()` call inside `SourceManager`, or anywhere that runs while `self._lock` is held. That would serialise every publish behind Telegram's latency.
- You find yourself adding `parse_mode`. See "Send plain text".
- A notification failure can reach the HTTP response in any code path. That is the single thing this plan must not do.
- You need retries, a queue, or persistence to make a test pass. Out of scope — the design is fire-and-forget.
- Any of the 70 existing tests needs editing.

## Maintenance notes

- **Fire-and-forget means messages can be lost** — a restart mid-send, a network blip, a Telegram outage. That is the deliberate trade: the catalog is the source of truth and the notification is a convenience. If delivery ever needs to be guaranteed, that is a queue and a different plan, and it should not be bolted onto the request path.
- **One daemon thread per notification.** Fine at this volume (a handful of publishes a week, admin-initiated). If this ever becomes high-frequency, replace it with a single worker thread and a `queue.Queue` — do not raise the thread count.
- **The `parse_mode`-free choice is load-bearing.** App names and version strings are attacker-supplied and stored. Anyone adding formatting must escape every MarkdownV2 special character, and test 5 exists to make that a conscious decision rather than an accident.
- **Plan 013's bot token is the same credential.** If both land and the token is rotated, both break together. Worth a line in the README when 013 ships.
- **Reviewer should scrutinise**: that no `notify()` call sits inside `SourceManager` or the lock; that the notifier cannot raise into the request path; and that the disabled-by-default behaviour makes no network call at all rather than making one and discarding the result.
