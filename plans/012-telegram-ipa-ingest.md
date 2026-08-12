# Plan 012: Ingest IPAs from a Telegram channel

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD          # plan written against 632f40f
> md5sum app.py                       # expect 97f5489786b0b4af47c063ecf4154411
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q   # expect 70 passed
> ```
> Note the `ADMIN_PASSWORD=x` prefix: Plan 010 landed and the app now refuses
> to import without it.

## Status

- **Priority**: P3 (new capability — nothing is broken without it)
- **Effort**: M
- **Risk**: MED — introduces a credential that is strictly more powerful than anything else in this repo. See "The session file is the whole risk".
- **Depends on**: 005 (tests), 010 (auth — the script must log in). Both DONE.
- **Category**: feature
- **Planned at**: 2026-08-12, `632f40f`, `app.py` md5 `97f5489786b0b4af47c063ecf4154411`

## Why this matters

New patched builds are published to a private Telegram channel. Getting one into the catalog today is entirely manual: open Telegram, download an 80–350 MB file, open the web UI, upload it, fill in the version. This plan automates the fetch-and-ingest half.

## The decisive constraint: Bot API cannot do this

This was investigated before writing the plan; **do not re-litigate it or "simplify" to a bot.**

Two facts from `https://core.telegram.org/bots/api`:

1. **The cloud Bot API caps `getFile` downloads at 20 MB.** The files here are 83–353 MB. A bot physically cannot download them. Only a self-hosted local Bot API server lifts this ("Download files without a size limit"), which means running another container.
2. **Bots cannot read channel history.** A bot only receives messages posted *after* it joins, pushed via updates. There is no `getMessages` in the Bot API. The target is a specific existing message, so a bot could never fetch it.

On top of that, **the operator is not an admin of the channel** (confirmed 2026-08-12) and therefore cannot add a bot to it at all.

**Therefore: MTProto with a user account, via Telethon.** It is the only approach that can read history in a channel you are merely a member of, and it has no practical file-size limit.

## The session file is the whole risk

Telethon authenticates as **the user**, not as a scoped bot. It writes a `.session` file (a SQLite database) holding an authorised MTProto key.

**That file is equivalent to a logged-in Telegram session for the entire account.** Anyone who copies it can read every chat, impersonate the account, and — depending on 2FA configuration — potentially take it over. It is a materially bigger credential than `ADMIN_PASSWORD` or the Garage keys, both of which are scoped to this one service.

Non-negotiable handling, and these are done criteria:

- Store it under `DATA_DIR` (which is gitignored and already excluded), **never** in the repo root and never in the image.
- `chmod 600` on creation, verified by the script at every startup.
- Add an explicit `*.session` / `*.session-journal` line to `.gitignore` — belt and braces on top of `data/`.
- **Never print, log, or include its path contents in an error message or a report.**
- The `API_ID` / `API_HASH` from <https://my.telegram.org> go in `.env`, names only in `.env.example`.

**First login is interactive and cannot be automated.** Telethon will prompt for the phone number, the code Telegram sends, and the 2FA password if set. That is a one-time human step performed on the host; afterwards the session file persists. Do not attempt to script it, and do not store the phone number or 2FA password anywhere.

## Understanding the target link

```
https://t.me/c/2162963305/14/194835
        └┬┘ └────┬────┘ └┬┘ └──┬──┘
         │       │       │     └── message id      194835
         │       │       └──────── forum topic id  14
         │       └──────────────── internal chat id 2162963305
         └──────────────────────── private-chat form
```

`t.me/c/<id>/...` is the private form. The MTProto peer id is the internal id prefixed with `-100`:

```
2162963305  ->  -1002162963305
```

The three-segment form (`/c/<chat>/<topic>/<msg>`) means the chat is a **forum supergroup** and `14` is a topic. The two-segment form (`/c/<chat>/<msg>`) has no topic. **Handle both** — the parser must not assume three segments.

In Telethon, the topic is the `reply_to.reply_to_top_id` of messages in that thread; fetch a topic's messages with `client.iter_messages(peer, reply_to=<topic_id>)`.

## Scope

**In scope** (all new files, plus two small additions):
- `scripts/telegram_ingest.py` (new) — the whole feature
- `requirements.txt` — add `telethon` with an exact `==` pin
- `.env.example` — key names only, no values
- `.gitignore` — add `*.session` and `*.session-journal`
- `tests/test_telegram_ingest.py` (new)

**Out of scope — do NOT touch**:
- `app.py`. **This plan adds no routes and no app code.** Ingestion goes through the existing HTTP API, which already handles atomic catalog writes (Plan 006), fail-loudly errors (Plan 007), and storage routing (Plan 011). Adding a Telegram code path inside `app.py` would bypass all three.
- `templates/`, the storage classes, `SourceManager`.
- Any attempt to *post*, reply, join, or leave anything on Telegram. This script **reads and downloads only**.
- Bot API, `python-telegram-bot`, a local Bot API server. Ruled out above.
- Scheduling. Getting one command right comes first; a timer is a follow-up (see Maintenance notes).

## Design

A standalone script, run manually or from cron, in three stages:

```
fetch          resolve the message -> download the document to a temp file
validate       it must be a real IPA before it goes anywhere near the catalog
ingest         POST multipart to /api/add-version, authenticated
```

**Why go through the HTTP API rather than writing to disk or S3 directly:** the app already owns catalog serialisation, backups, locking, size recording, and the local/Garage storage decision. A script that writes `data/ipas/` directly would race `save_source` and silently diverge from whichever backend `STORAGE_BACKEND` selects. Reuse the API and all of that comes free.

**CLI:**

```
scripts/telegram_ingest.py --url <t.me link> --bundle-id <id> --version <ver> [--apply]
scripts/telegram_ingest.py --watch-topic <n> --bundle-id <id> [--limit 5] [--apply]
```

**Dry-run by default**, exactly like `scripts/migrate_ipas_to_garage.py`. Without `--apply` it resolves, reports what it found (filename, size, message date, computed sha256) and exits 0 **without downloading**. `--apply` downloads and ingests.

## Validation — non-negotiable

`data/source.json` already carries the scars of skipping this: three catalogued versions are gzip-compressed HTML rather than IPAs, because a pre-Plan-007 download path wrote whatever bytes arrived. **Do not let a Telegram file into the catalog unchecked.**

Before ingesting, all of these must hold:

1. The message actually has a document (`message.document is not None`), else fail with the message id.
2. Filename ends `.ipa` (case-insensitive), **or** `--force-extension` is passed explicitly.
3. `zipfile.is_zipfile(path)` — an IPA is a ZIP. This is what catches HTML-error-page-as-ipa.
4. The archive contains a `Payload/` entry: `any(n.startswith('Payload/') for n in zipfile.ZipFile(path).namelist())`. A ZIP is not necessarily an IPA; this is what distinguishes them.
5. Downloaded byte count equals `message.document.size`. A short read must be a hard failure, not a warning.

Any failure: delete the temp file, report which check failed and the message id, exit non-zero. **Never ingest a file that failed a check, and never "warn and continue".**

## Configuration

Add to `.env.example` — **names only**:

```
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_SESSION_NAME=
FEATHER_BASE_URL=
FEATHER_ADMIN_PASSWORD=
```

| Variable | Notes |
|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | from <https://my.telegram.org>, per-account |
| `TELEGRAM_SESSION_NAME` | basename only; the script resolves it under `DATA_DIR` |
| `FEATHER_BASE_URL` | e.g. `https://feather.example.com` — where to POST |
| `FEATHER_ADMIN_PASSWORD` | for `POST /api/login`; same value as `ADMIN_PASSWORD` |

Refuse to run if any is unset, naming the missing **names** only.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | 70 before, more after |
| Syntax | `.venv/bin/python -m py_compile scripts/telegram_ingest.py` | exit 0 |
| Wheel check | see Step 1 | both exit 0 |
| Help | `.venv/bin/python scripts/telegram_ingest.py --help` | usage, exit 0 |

## Git workflow

- Branch: `advisor/012-telegram-ingest`
- Commits: (1) dependency + config + gitignore, (2) the script, (3) tests.

## Steps

### Step 1: Dependency

`telethon` is pure Python; verify wheels for **both** interpreters before pinning — the container is Python 3.11, the test host is 3.14, and a pin that fails on either breaks CI:

```
.venv/bin/pip download --no-deps -q --only-binary=:all: --platform manylinux_2_28_x86_64 --python-version 311 -d /tmp/tg telethon==<version>
.venv/bin/pip download --no-deps -q --only-binary=:all: --platform manylinux_2_28_x86_64 --python-version 314 -d /tmp/tg telethon==<version>
```

Both must exit 0. (Confirmed available at planning time for `telethon` 1.44.0.) Add it to `requirements.txt` with an exact `==` pin, matching the file's existing style — one per line, no comments.

**Do not add `cryptg`.** It is an optional compiled speedup; the extra build surface is not worth it for a handful of downloads.

**Verify**: `.venv/bin/pip install -q -r requirements.txt && .venv/bin/python -c "import telethon; print(telethon.__version__)"`

### Step 2: Config, gitignore, and the session-file guard

Add the five key names to `.env.example` (no values). Add to `.gitignore`:

```
# Telegram MTProto session — equivalent to a logged-in account. Never commit.
*.session
*.session-journal
```

**Verify**:
```
grep -c "TELEGRAM_API_ID" .env.example      # 1
grep -c "^\*.session$" .gitignore           # 1
git check-ignore -v test.session            # must report a match
```

### Step 3: URL parsing

Write `parse_telegram_url(url)` returning `(peer_id, topic_id_or_None, message_id)`.

- `https://t.me/c/2162963305/14/194835` → `(-1002162963305, 14, 194835)`
- `https://t.me/c/2162963305/194835` → `(-1002162963305, None, 194835)`
- Anything else → raise `ValueError` naming what was wrong.

The `-100` prefix is applied by string concatenation on the digits, not arithmetic: `int("-100" + "2162963305")`.

This function is pure and is the main thing worth unit-testing.

**Verify**: the parser tests from the test plan pass.

### Step 4: Fetch and validate

Connect with `TelegramClient(session_path, API_ID, API_HASH)`. Then:

- Resolve the message: `await client.get_messages(peer, ids=message_id)`.
- If it returns `None`, the account cannot see that message — **STOP**, report the id. Do not retry, do not attempt to join anything.
- Report filename, `document.size`, and `message.date` before downloading.
- Download with `client.download_media(message, file=<temp path under DATA_DIR>)`.
- Run all five validation checks. Compute and print the sha256 — it is the only way to tell two builds apart when filenames repeat.

Download to a temp path and only rename to a final name after validation passes, so a failed run never leaves a plausible-looking `.ipa` behind. This mirrors what `update_version` does in `app.py`.

### Step 5: Ingest through the API

```
POST {FEATHER_BASE_URL}/api/login       {"password": ...}     -> keep the session cookie
POST {FEATHER_BASE_URL}/api/add-version  multipart:
     bundleIdentifier=<--bundle-id>
     version=<--version>
     ipaFile=@<validated file>
```

Use `requests.Session()` so the cookie carries. Check for `401` explicitly and report "login failed" rather than a generic error — Plan 010 makes that the likely first failure.

Assert the response is `200` and `success: true`. On a non-2xx, print the server's `error` field verbatim; Plan 007 made those messages specific and they are the useful diagnostic.

**Never log `FEATHER_ADMIN_PASSWORD`**, and never include it in a `--verbose` dump.

### Step 6: Watch mode

`--watch-topic <n>` lists the most recent `--limit` documents in that topic (default 5) and reports which are already in the catalog (by `GET /source.json`, which needs no auth) versus new. With `--apply` it ingests the new ones.

Keep it dumb: no state file, no "last seen id". Idempotency comes from checking the catalog, which is the single source of truth. A state file would be a second source of truth that can drift.

**Verify**: `--watch-topic 14` without `--apply` prints a list and writes nothing.

## Test plan

New `tests/test_telegram_ingest.py`. **No test may touch the network or require credentials** — the existing 70 tests run in ~3.5 s with no network and that property is worth protecting.

1. `test_parse_url_with_topic` — the three-segment form → `(-1002162963305, 14, 194835)`.
2. `test_parse_url_without_topic` — two-segment → `(-1002162963305, None, 194835)`.
3. `test_parse_url_rejects_garbage` — public `t.me/somechannel/5`, a non-Telegram URL, and an empty string all raise `ValueError`.
4. `test_validate_rejects_non_zip` — write gzip'd HTML to a temp file, assert rejection. **This is the regression test for the three corrupt catalog entries**; build the fixture to match them (gzip of a short HTML string).
5. `test_validate_rejects_zip_without_payload` — a plain ZIP with one text file is rejected.
6. `test_validate_accepts_minimal_ipa` — a ZIP containing `Payload/App.app/Info.plist` is accepted.
7. `test_validate_rejects_size_mismatch` — declared size ≠ actual bytes is rejected.
8. `test_missing_config_refuses_to_run` — unset vars raise, message names the missing names and contains no values.

Stub Telethon entirely — a fake client object with `get_messages` / `download_media`. Do not import a real `TelegramClient` in tests.

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 70 + 8 = **78**, and the 70 existing tests unmodified.

## Done criteria

ALL must hold:

- [ ] `.venv/bin/python -m py_compile scripts/telegram_ingest.py` exits 0
- [ ] `grep -c "^telethon==" requirements.txt` returns `1`, wheels verified for cp311 **and** cp314
- [ ] `git check-ignore -v test.session` reports a match; `git ls-files | grep -c "\.session$"` returns `0`
- [ ] `grep -c "TELEGRAM_API_ID" .env.example` returns `1`; `.env.example` contains no values
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 78, the 70 pre-existing unmodified
- [ ] `--help` works and dry-run is the default (no `--apply` ⇒ no download, no POST)
- [ ] No test performs a network call or needs credentials
- [ ] `grep -rn "api_hash\|API_HASH" scripts/ tests/ | grep -v "os.environ\|getenv\|TELEGRAM_API_HASH\"" ` finds no literal value
- [ ] `git status --short` shows only the five in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report (do not improvise) if:

- **You need to authenticate to Telegram.** The first login is interactive and is the operator's job. Never ask for, store, or transcribe a phone number, login code, or 2FA password. Build and test everything against stubs; the operator runs the first real fetch.
- `get_messages` returns `None` for the target id. The account cannot see it. Report; do not try to join the channel, resolve invite links, or search for the file elsewhere.
- The channel has content protection (`noforwards`) and downloads fail. Report what Telegram returned; do not attempt a workaround.
- You are tempted to add a route or any Telegram code to `app.py`. Out of scope — it would bypass Plans 006, 007 and 011.
- You are tempted to write to `data/ipas/` or Garage directly instead of POSTing to the API. Same reason.
- A validation check is inconvenient and you consider downgrading it to a warning. That is precisely how the three corrupt entries got into the live catalog.
- Telethon requires `cryptg` to function. It should not — report rather than adding a compiled dependency.

## Maintenance notes

- **The session file is the highest-value secret in this deployment.** It outranks `ADMIN_PASSWORD` and the Garage keys because it is not scoped to this service. If the host is ever compromised, revoke it from Telegram (Settings → Devices → terminate session) — deleting the file locally is not revocation.
- **This is automation on a user account.** Telegram tolerates read-only userbots in practice, but it is not the Bot API's supported path and an account can be limited. Keeping the script read-only, low-frequency, and never posting is what keeps it uncontroversial. Do not add "helpful" features that send messages.
- **Scheduling is deliberately not included.** Once one manual run is proven, a cron entry or a `docker compose` sidecar with a sleep loop is the obvious next step. Do it after, not during.
- **Provenance is not verified.** Nothing here checks that a build is what it claims to be — it validates that the bytes are a well-formed IPA, not that they are safe or unmodified. Anyone with posting rights in that channel effectively controls what lands in the catalog, and from there what installs on subscribed devices. That is an accepted property of the design, not an oversight; the sha256 print exists so a human can at least compare builds across runs.
- **Bundle ID and version are supplied by the operator, not parsed from the filename.** Filenames in these channels are inconsistent and a wrong bundle ID silently creates a second catalog entry rather than a new version of the existing app. Keep them explicit.
- **Reviewer should scrutinise**: that all five validation checks are hard failures; that the session file path resolves under `DATA_DIR` and is chmod 600; and that no test reaches the network.
