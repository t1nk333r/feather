# Plan 016: Make Plan 013's Telegram services opt-in

> **Executor instructions**: Follow this plan step by step. If anything in
> "STOP conditions" occurs, stop and report — do not improvise. When done,
> update the status row in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD                       # plan written against 19e3ec2
> grep -c "profiles:" compose.yml                  # expect 0
> grep -n "telegram-bot-api:\|ipa-ingest-bot:" compose.yml
> ```

## Status

- **Priority**: P1 — this is a live regression. The deployed stack cannot come up cleanly.
- **Effort**: XS (four lines)
- **Risk**: LOW
- **Depends on**: 013 (DONE)
- **Category**: bug (deployment)
- **Planned at**: 2026-08-12, `19e3ec2`

## Why this matters

Plan 013 added two services to `compose.yml`: `telegram-bot-api` and `ipa-ingest-bot`. Both are **unconditional**, so `docker compose up -d` starts them on every deployment — including the operator's, which has none of the `TELEGRAM_*` variables set.

Observed against the live `.env`:

```
WARN  The "TELEGRAM_API_ID" variable is not set. Defaulting to a blank string.
WARN  The "TELEGRAM_API_HASH" variable is not set. Defaulting to a blank string.
```

`telegram-bot-api` then starts with a blank api-id/hash, and `ipa-ingest-bot` starts and immediately raises on its missing-config check — which is correct behaviour for that worker, but it means `restart: unless-stopped` turns it into a restart loop on a deployment that never asked for Telegram ingest at all.

**This is a design inconsistency, not a coding error.** Plan 011 got this right: `STORAGE_BACKEND` defaults to `local`, so merging it changed nothing at runtime and enabling Garage is a deliberate act. Plan 013 introduced two whole containers with no equivalent switch. The same discipline should apply.

Plan 013's Step 0 — whether the source channel even permits forwarding — is **still unverified**. Shipping containers that are enabled by default for a feature that might turn out to be unusable is the wrong default twice over.

## Current state

`compose.yml`, the two services added by Plan 013 (lines ~33 and ~50):

```yaml
  telegram-bot-api:
    image: aiogram/telegram-bot-api:latest
    container_name: telegram-bot-api
    command: ["--local", "--http-port=8081"]
    environment:
      - TELEGRAM_API_ID=${TELEGRAM_API_ID}
      - TELEGRAM_API_HASH=${TELEGRAM_API_HASH}
    ...
  ipa-ingest-bot:
    build:
      context: .
      dockerfile: Dockerfile.bot
    container_name: ipa-ingest-bot
    ...
```

`grep -c "profiles:" compose.yml` returns `0`.

## The fix: Compose profiles

A service with a `profiles:` key is **not started** unless that profile is explicitly activated. That is exactly the semantics wanted here: present in the file, documented, inert until asked for.

```
docker compose up -d                        # feather only — the two Telegram services are ignored
docker compose --profile telegram up -d     # feather + telegram-bot-api + ipa-ingest-bot
```

This also makes `docker compose build` skip `Dockerfile.bot` by default, so a deployment that does not want Telegram ingest does not build an image for it.

## Scope

**In scope**: `compose.yml` — adding one `profiles:` key to each of the two Telegram services. Four lines total.

**Out of scope — do NOT touch**:
- The `altstore-manager` service. It must remain byte-identical and must **not** get a profile — it is the product.
- `scripts/telegram_bot_ingest.py`, `Dockerfile.bot`, `tests/`, `.env.example`, `app.py`, `requirements.txt`. The worker's behaviour is correct; only *when it starts* is wrong.
- Removing or weakening the worker's fatal missing-config check. That check is right; it should simply not be reached on a deployment that did not opt in.
- The named volume `telegram-bot-api-data`. Volumes are lazily created and cost nothing when unused.

## Steps

### Step 1: Add the profile to both services

Add to `telegram-bot-api` and to `ipa-ingest-bot`, at the same indent level as `image:` / `build:`:

```yaml
    profiles: ["telegram"]
```

Add a short comment above the first one explaining the intent, in the style of the existing comments in that file:

```yaml
    # Opt-in (Plan 016): not started by a plain `docker compose up -d`.
    # Enable with `docker compose --profile telegram up -d` once the
    # TELEGRAM_* variables are set in .env.
```

**Verify**: `grep -c 'profiles: \["telegram"\]' compose.yml` → `2`

### Step 2: Prove the default no longer starts them

```
docker compose config --services
```
→ lists **only** `altstore-manager`.

```
docker compose --profile telegram config --services
```
→ lists all three.

Both must be run **without** a `.env` present containing `TELEGRAM_*`, to reproduce the operator's situation. If `docker compose config` errors because `altstore-manager`'s `env_file: .env` is missing, create a temporary empty `.env`, run the checks, and delete it — **never commit one**, and confirm afterwards with `git status --short` that none was left behind.

### Step 3: Confirm the warnings are gone

With no `TELEGRAM_*` variables set:

```
docker compose config 2>&1 | grep -c "TELEGRAM_API_ID.*not set"
```
→ `0`. Before this change it was non-zero, which is the symptom the operator reported.

## Done criteria

ALL must hold:

- [ ] `grep -c 'profiles: \["telegram"\]' compose.yml` returns `2`
- [ ] `docker compose config --services` lists exactly one service: `altstore-manager`
- [ ] `docker compose --profile telegram config --services` lists all three
- [ ] `docker compose config 2>&1 | grep -c "TELEGRAM_API_ID.*not set"` returns `0`
- [ ] `git diff compose.yml` touches only the two Telegram service blocks — the `altstore-manager` block is unchanged
- [ ] `git diff --name-only` shows only `compose.yml`
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 78, unchanged (this plan touches no Python)
- [ ] No `.env` was committed: `git ls-files | grep -c "^\.env$"` → `0`
- [ ] `plans/README.md` status row updated

## STOP conditions

- You are tempted to give `altstore-manager` a profile too. It is the product and must always start.
- You are tempted to remove the worker's fatal missing-config check so it can run unconfigured. That check is correct; the fix is not starting it.
- `docker compose config --services` still lists the Telegram services after adding the profiles. Report the compose version — `profiles` requires Compose v2 (the host has v5.4.0, so this should not happen).
- You find yourself editing anything other than `compose.yml`.

## Maintenance notes

- **The rule this encodes**: anything added to `compose.yml` for an optional feature gets a profile, and anything added to `app.py` for an optional feature gets a default-off environment flag. Plan 011 followed the second; Plan 013 followed neither, which is what caused this. Apply it to Plan 014 (`TELEGRAM_NOTIFY_*`) too — it is already designed disabled-by-default, so it only needs to stay that way.
- Once Plan 013's Step 0 is confirmed and the operator wants ingest, the switch is `docker compose --profile telegram up -d`. Nothing else changes.
- **Deployments track `main`.** Any change to `compose.yml` reaches the live stack on the next pull, so compose changes should be treated as deployment changes and default to inert.
