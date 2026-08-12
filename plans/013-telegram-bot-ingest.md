# Plan 013: Self-hosted Bot API server + forward-to-bot IPA ingest

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD          # plan written against da7ee42
> md5sum app.py                       # expect 97f5489786b0b4af47c063ecf4154411
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q   # expect 70 passed
> ```
> The `ADMIN_PASSWORD=x` prefix is required: Plan 010 landed and the app
> refuses to import without it.

## Status

- **Priority**: P3 (new capability)
- **Effort**: M
- **Risk**: MED — adds two containers and a path by which a Telegram message becomes an app installed on real devices. The allowlist in Step 4 is what keeps that safe.
- **Depends on**: 005 (tests), 010 (auth — the worker must log in). Both DONE.
- **Supersedes**: `plans/012-telegram-ipa-ingest.md` as the *primary* approach. **012 is not rejected** — it is the fallback if Step 0 fails. See "Relationship to Plan 012".
- **Category**: feature
- **Planned at**: 2026-08-12, `da7ee42`, `app.py` md5 `97f5489786b0b4af47c063ecf4154411`

## Why this shape, and what self-hosting does *not* fix

Two independent limits block a bot from fetching these files. **Self-hosting the Bot API server clears one of them, not both.** From the project's own README:

> "The local server **does not change which messages a bot can access** — it only modifies operational capabilities, particularly regarding file handling."

| Limit | Cloud Bot API | Self-hosted `--local` |
|---|---|---|
| `getFile` download size | **20 MB** | no limit |
| `getFile` result | a URL to download | **an absolute local path** |
| Upload size | 50 MB | 2000 MB |
| Read channel history | **never** | **still never** |
| Access a channel you don't admin | **never** | **still never** |

So a bot still cannot go and fetch `https://t.me/c/2162963305/14/194835` by itself, and since the operator is **not an admin of that channel** (confirmed 2026-08-12) a bot cannot be added to it at all.

**What does work is the operator forwarding the file to their own bot.** The bot then holds it as an ordinary document, and the local server lets it read that document at any size. That is a manual step per build — and that is a feature, not a defect: it is an explicit "publish this build" gate on a catalog that installs software onto real devices.

The files are 83–353 MB, so the local server is not optional here; on the cloud API every one of them fails at 20 MB.

## Step 0 — the test that decides whether this plan is viable at all

**Do this before writing any code, and it is the operator's job, not the executor's.**

In Telegram, open the source channel, long-press the IPA message, and try to forward it to **Saved Messages**.

- **Forward succeeds** → this plan works. Continue.
- **Forward is blocked / no forward option / "forwarding is restricted"** → the channel has content protection (`noforwards`) enabled. **This entire plan is dead**, because the file can never reach the bot. Fall back to `plans/012-telegram-ipa-ingest.md`, which uses the operator's own account and is not subject to that restriction. **STOP and report.**

Many channels distributing patched builds enable exactly this restriction, so treat it as likely until proven otherwise.

## Relationship to Plan 012

They solve the same problem two ways, and the choice is a security trade, not a preference:

| | 013 (this plan) | 012 (Telethon) |
|---|---|---|
| Credential | **bot token** — scoped to one bot, revocable in seconds via BotFather | **`.session` file** — full account access, revocation needs Telegram's Devices screen |
| Blast radius if the host is compromised | attacker can post as a bot nobody follows | attacker reads every chat the account is in |
| Historical messages | forward once by hand | fetch by link automatically |
| New builds | manual forward each time | can poll a topic unattended |
| Blocked by channel content protection | **yes, fatally** | no |
| Extra infrastructure | 2 containers | none |

**013 is the better default** purely on credential blast radius. Prefer it unless Step 0 rules it out.

Do **not** implement both. If 013 lands, mark 012 `REJECTED — superseded by 013` in the index.

## Architecture

```
  operator forwards IPA in Telegram
              │
              ▼
  ┌───────────────────────────┐   shared named volume
  │ telegram-bot-api (--local)│◄──────────────────────┐
  │  aiogram/telegram-bot-api │   /var/lib/telegram-bot-api
  └───────────┬───────────────┘                       │
              │ getUpdates / getFile                  │
              │ -> absolute local path                │
              ▼                                       │
  ┌───────────────────────────┐                       │
  │ ipa-ingest-bot (worker)   │───────────────────────┘
  │  reads the file directly  │   no HTTP download at all
  └───────────┬───────────────┘
              │ POST /api/login  then
              │ POST /api/add-version (multipart)
              ▼
  ┌───────────────────────────┐
  │ altstore-manager (feather)│
  └───────────────────────────┘
```

Two design points that matter:

1. **The worker reads the file off a shared volume, not over HTTP.** With `--local`, `getFile` returns `file_path` as an absolute path inside the Bot API server's data directory. Mount that same volume into the worker and a 353 MB "download" becomes a file read. No 350 MB round-trip through Python.
2. **Ingest goes through feather's HTTP API**, not by writing `data/ipas/` or S3 directly. That inherits Plan 006's atomic catalog writes, Plan 007's specific error messages, and Plan 011's `STORAGE_BACKEND` routing. A worker writing storage directly would race `save_source` and ignore whichever backend is selected.

**No new Python dependency.** Long-polling `getUpdates` and posting multipart are both plain `requests`, which is already pinned. Do not add `python-telegram-bot`, `aiogram`, or `telethon` — the framework surface is not worth it for two endpoints.

## The interaction model

Forwarding cannot carry a caption, so the bundle id and version arrive in a **second message**:

```
operator: [forwards Beegram.ipa]
bot:      Got Beegram.ipa — 83,042,753 bytes, sha256 4f2a…9c1b.
          Send:  /add <bundleIdentifier> <version>
operator: /add com.faceboo.instagram.beegram 1.0.1
bot:      Published com.faceboo.instagram.beegram 1.0.1 (83,042,753 bytes).
```

The worker holds **one pending document per allowlisted user**, in memory. If a second file arrives before `/add`, it replaces the first and the bot says so. Losing pending state on restart is fine — the operator forwards again.

**Bundle id and version are always explicit, never parsed from the filename.** Filenames in these channels are inconsistent, and a wrong bundle id silently creates a *new app* in the catalog rather than a new version of an existing one.

## Security — the allowlist is not optional

A bot's username is discoverable, and anyone can message it. Without a check, a stranger could forward an arbitrary IPA and have it published to a catalog that **installs software on real devices**.

- `TELEGRAM_ALLOWED_USER_IDS` — comma-separated numeric Telegram user ids. Every update is checked against it **before** anything else happens.
- Messages from anyone else: ignore silently. Do not reply — a reply confirms the bot exists and is live. Log the rejected id at `warning`.
- An empty or unset allowlist is a **fatal config error**, not "allow everyone".

The bot token, the allowlist, and the feather admin password all live in `.env`, names only in `.env.example`. Never log the token or the password; never echo them in an error.

## Validation — five hard checks, no warnings

`data/source.json` currently carries three catalogued versions that are gzip-compressed HTML rather than IPAs, because a pre-Plan-007 path wrote whatever bytes arrived. Do not repeat that.

Before calling `/api/add-version`, all must hold:

1. The update contains a document (`message.document`), else reply "that isn't a file".
2. Filename ends `.ipa`, case-insensitive.
3. `zipfile.is_zipfile(path)` — catches HTML-as-ipa.
4. The archive contains a `Payload/` entry: `any(n.startswith('Payload/') for n in ZipFile(path).namelist())`. A ZIP is not necessarily an IPA.
5. On-disk size equals `document.file_size` from the update.

Any failure: reply with **which** check failed, do not ingest, discard the pending document. **Never downgrade a check to a warning.**

## Configuration

Add to `.env.example` — names only, no values:

```
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_IDS=
BOT_API_BASE_URL=
BOT_API_FILE_ROOT=
FEATHER_BASE_URL=
FEATHER_ADMIN_PASSWORD=
```

| Variable | Value for this deployment |
|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | from <https://my.telegram.org> — **required by the server itself**, even though it serves bots |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | the operator's numeric id (from @userinfobot) |
| `BOT_API_BASE_URL` | `http://telegram-bot-api:8081` |
| `BOT_API_FILE_ROOT` | `/var/lib/telegram-bot-api` |
| `FEATHER_BASE_URL` | `http://altstore-manager:5000` — container-to-container, no TLS hop |
| `FEATHER_ADMIN_PASSWORD` | same value as `ADMIN_PASSWORD` |

Refuse to start if any is unset, naming the missing **names** only.

## Scope

**In scope**:
- `compose.yml` — add two services and one named volume
- `scripts/telegram_bot_ingest.py` (new) — the worker
- `Dockerfile.bot` (new) — a small image for the worker
- `.env.example`
- `tests/test_telegram_bot_ingest.py` (new)

**Out of scope — do NOT touch**:
- `app.py`. **This plan adds no routes and no app code.** Ingestion uses the existing API.
- `Dockerfile` (the feather one), `templates/`, `SourceManager`, the storage classes.
- The existing `altstore-manager` service definition, other than leaving it exactly as it is. Do not "tidy" it.
- `requirements.txt` — `requests` is already there and is all the worker needs.
- Any code that *sends* to the source channel, joins it, or reads its history. The worker only ever talks to its own bot's updates.
- `plans/012-telegram-ipa-ingest.md` — leave the file; the index records its status.

## Target `compose.yml`

The existing `altstore-manager` service stays **byte-identical**. Add alongside it:

```yaml
  telegram-bot-api:
    image: aiogram/telegram-bot-api:latest
    container_name: telegram-bot-api
    command: ["--local", "--http-port=8081"]
    environment:
      - TELEGRAM_API_ID=${TELEGRAM_API_ID}
      - TELEGRAM_API_HASH=${TELEGRAM_API_HASH}
    volumes:
      - telegram-bot-api-data:/var/lib/telegram-bot-api
    restart: unless-stopped
    # Deliberately NOT published to the host. Only the worker talks to it,
    # over the compose network. Exposing it would put an unauthenticated
    # file-serving endpoint on the LAN.

  ipa-ingest-bot:
    build:
      context: .
      dockerfile: Dockerfile.bot
    container_name: ipa-ingest-bot
    depends_on:
      - telegram-bot-api
      - altstore-manager
    env_file:
      - .env
    volumes:
      # Read-only: the worker reads files the API server wrote. It has no
      # business modifying that directory.
      - telegram-bot-api-data:/var/lib/telegram-bot-api:ro
    restart: unless-stopped

volumes:
  telegram-bot-api-data:
```

Note the existing file ends with `networks: {}` — keep it, and put the `volumes:` block at the same top level.

**Do not publish port 8081 to the host.** With `--local`, that server hands out absolute paths and serves files with no auth beyond the token in the URL. It belongs on the internal network only.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | 70 before, 78 after |
| Syntax | `.venv/bin/python -m py_compile scripts/telegram_bot_ingest.py` | exit 0 |
| Compose valid | `docker compose config >/dev/null && echo ok` | `ok` |
| Bot image builds | `docker build -q -f Dockerfile.bot -t ipa-ingest-bot .` | a sha |

## Git workflow

- Branch: `advisor/013-telegram-bot-ingest`
- Commits: (1) compose + Dockerfile.bot + `.env.example`, (2) the worker, (3) tests.

## Steps

### Step 1: Compose and the worker image

Add the two services and the named volume exactly as above. `Dockerfile.bot` should be minimal — `python:3.11-slim` to match the main image, `pip install requests`, copy the one script, non-root user, `CMD` running the worker.

**Verify**:
```
docker compose config >/dev/null && echo "compose ok"
docker compose config | grep -c "8081:8081"        # must be 0 — not published to the host
docker build -q -f Dockerfile.bot -t ipa-ingest-bot .
```

### Step 2: Config loading and the allowlist

Read the eight variables. Refuse to start if any is missing, naming the missing names. Parse `TELEGRAM_ALLOWED_USER_IDS` into a set of ints; **empty set ⇒ fatal**.

**Verify**: running with no env raises and the message lists the missing names and contains no values.

### Step 3: The update loop

Long-poll `GET {BOT_API_BASE_URL}/bot{token}/getUpdates?offset={n}&timeout=30`. Standard offset handling: `offset = last_update_id + 1`.

For each update, in this order:
1. Extract the sender id. **If not in the allowlist, log a warning with the id and skip.** No reply.
2. If it has a document → `getFile`, resolve the path, validate, store as pending, reply with filename/size/sha256 and the `/add` hint.
3. If it is `/add <bundleId> <version>` → ingest the pending document.
4. Anything else → a short usage reply.

Wrap the loop body in a broad `except` that logs and continues. A malformed update must not kill the worker; `restart: unless-stopped` would mask a crash-loop as "working".

**Resolving the file path**: `getFile` returns `result.file_path`. With `--local` this is an absolute path such as `/var/lib/telegram-bot-api/<token>/documents/file_1.ipa`. The worker sees it at the same path because the volume is mounted at the same mountpoint. If the path does not exist, **STOP and report** — that means the mounts disagree, and silently falling back to an HTTP download would reintroduce the size problem.

### Step 4: Validation

The five checks above, as hard failures, each with a distinct reply naming what failed.

Compute sha256 by streaming the file in chunks — do not read 353 MB into memory.

### Step 5: Ingest

```
POST {FEATHER_BASE_URL}/api/login          {"password": FEATHER_ADMIN_PASSWORD}
POST {FEATHER_BASE_URL}/api/add-version    multipart:
       bundleIdentifier, version, ipaFile=@<path>
```

Use one `requests.Session()` so the cookie carries. Treat `401` as "login failed" specifically — post-Plan-010 that is the most likely first failure, and a generic error would send someone hunting in the wrong place.

Stream the upload (`files={'ipaFile': open(path,'rb')}` — `requests` streams file objects) rather than loading it.

On success reply with the bundle id, version and byte count. On failure reply with the server's `error` field verbatim; Plan 007 made those messages specific and they are the useful diagnostic.

### Step 6: End-to-end, by hand

This step is the operator's, not the executor's — it needs a real bot token and a real forward.

```
docker compose up -d --build
docker compose logs -f ipa-ingest-bot
```

Forward an IPA to the bot, then `/add <bundleId> <version>`. Confirm the new version appears in `GET /source.json`.

## Test plan

New `tests/test_telegram_bot_ingest.py`. **No test may touch the network, Telegram, or Docker** — the existing 70 tests run in ~3.5 s with no network and that property is worth keeping.

1. `test_allowlist_rejects_unknown_sender` — an update from an id not in the allowlist produces no ingest call and no reply.
2. `test_allowlist_empty_is_fatal` — empty/unset allowlist raises at startup.
3. `test_missing_config_names_missing_vars` — the error lists names, contains no values.
4. `test_validate_rejects_non_zip` — gzip'd HTML rejected. **This is the regression test for the three corrupt catalog entries** — build the fixture to match: gzip of a short HTML string.
5. `test_validate_rejects_zip_without_payload` — a plain ZIP with one text file rejected.
6. `test_validate_accepts_minimal_ipa` — a ZIP containing `Payload/App.app/Info.plist` accepted.
7. `test_validate_rejects_size_mismatch` — declared ≠ actual rejected.
8. `test_add_command_parses_bundle_and_version` — `/add com.x.y 1.2.3` parses; `/add` with too few args replies with usage and does not ingest.

Stub the Bot API with a fake `requests` session returning canned `getUpdates` / `getFile` payloads, and point `BOT_API_FILE_ROOT` at `tmp_path`.

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → **78**, the 70 existing unmodified.

## Done criteria

ALL must hold:

- [ ] `docker compose config` exits 0 and `grep -c "8081:8081"` on its output returns `0`
- [ ] `docker build -f Dockerfile.bot` succeeds
- [ ] The `altstore-manager` service block is byte-identical to before (`git diff compose.yml` shows only additions)
- [ ] `.venv/bin/python -m py_compile scripts/telegram_bot_ingest.py` exits 0
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 78, the 70 pre-existing unmodified
- [ ] `grep -c "TELEGRAM_BOT_TOKEN" .env.example` returns `1`; `.env.example` contains no values
- [ ] `git ls-files | grep -c "^\.env$"` returns `0`
- [ ] No test performs a network call
- [ ] `grep -rniE "bot[0-9]{6,}:" scripts/ tests/ compose.yml` finds nothing — no token literal anywhere
- [ ] `grep -c "requests" requirements.txt` unchanged — no new Python dependency added
- [ ] `git status --short` shows only the five in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report (do not improvise) if:

- **Step 0 fails** — forwarding from the source channel is blocked. This plan cannot work; Plan 012 is the fallback.
- `getFile` returns a path that does not exist inside the worker. The volume mounts disagree. **Do not fall back to downloading over HTTP** — that reintroduces the size limit this whole plan exists to avoid.
- You are tempted to publish port 8081 to the host to "make testing easier". It serves files with no real auth; keep it internal.
- You are tempted to skip the allowlist while testing. That is the one control preventing a stranger from publishing an app to devices.
- You find yourself adding `python-telegram-bot`, `aiogram`, or `telethon`. Two endpoints, `requests` is enough.
- You find yourself editing `app.py` or writing to `data/ipas/` or S3 directly.
- A validation check is inconvenient and you consider making it a warning. That is exactly how the three corrupt entries got into the live catalog.
- The bot needs to read the source channel's history. It cannot, by design — see the table at the top. Report rather than trying.

## Maintenance notes

- **The bot token is the credential here, and that is the point.** Revoke it in @BotFather in seconds. Compare Plan 012, where the equivalent credential is a session file granting full account access. If this plan is ever abandoned for 012, that trade is the thing to re-read first.
- **The local Bot API server keeps every file it downloads**, under its volume, forever. There is no automatic cleanup. Expect it to grow at the size of every build ingested; add a prune step or a periodic `docker volume` cleanup once a few builds have gone through. This is a slow disk leak, not an error.
- **`TELEGRAM_API_ID`/`API_HASH` are still required even though this serves bots** — the server itself is an MTProto client. They are account-scoped values from my.telegram.org, so treat them as secrets even though they are not, on their own, account access.
- **Provenance is not verified.** The five checks prove the bytes are a well-formed IPA, not that the build is safe or unmodified. Whoever the operator forwards from effectively decides what installs on subscribed devices. That is an accepted property of the design; the sha256 in the bot's reply exists so a human can compare builds across runs.
- **Manual forwarding is deliberate.** If this is ever made unattended, the "publish this build" gate disappears and the security model becomes "whatever appears in that channel installs on devices". Think hard before automating that away.
- **Reviewer should scrutinise**: that the allowlist is checked before any other processing; that port 8081 is not published; that all five validation checks are hard failures; and that `requirements.txt` gained nothing.
