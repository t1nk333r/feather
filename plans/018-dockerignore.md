# Plan 018: Add a deny-by-default `.dockerignore`

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result. If anything in
> "STOP conditions" occurs, stop and report — do not improvise. When done,
> update the status row in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD          # plan written against 5b28c9a or later
> test -e .dockerignore && echo "EXISTS — stop" || echo "absent, as expected"
> grep -n "^COPY" Dockerfile Dockerfile.bot
> ```

## Status

- **Priority**: P1 — precondition for making the published packages public
- **Effort**: XS (one new file)
- **Risk**: LOW, with one specific failure mode: excluding a path a `COPY` needs. Step 2 catches it.
- **Depends on**: nothing
- **Category**: security / DX
- **Planned at**: 2026-08-12

## Why this matters

There is no `.dockerignore`, so the Docker build context is **the entire repository**: `.env` (admin password, Garage keys, bot token), `data/` (1.3 GB of IPAs and the live catalog), `.git/`, and `.claude/worktrees/`.

Today nothing leaks, because both Dockerfiles use specific `COPY` lines rather than `COPY . .`. Audited on the real image:

```
/app  ->  app.py  requirements.txt  templates/  data/(empty)
ENV   ->  only stock python:3.11-slim vars
find / -name ".env" -o -name "source.json"  ->  nothing
```

**The risk is the next edit, not the current state.** `COPY app.py .` → `COPY . .` is among the most common Dockerfile "simplifications" anyone makes. The moment it happens:

- with a **private** package, the credentials and the whole catalog sit in a layer — bad, recoverable
- with a **public** package, they are world-readable and continuously scraped — irreversible, since pulled layers persist after you delete the package

The operator intends to make the packages public. This file is what makes that safe.

Secondary benefit: the build context currently includes 1.3 GB of IPAs and every agent worktree. Excluding them makes every build materially faster.

## Design: deny by default

Because the entire point is to survive a careless `COPY . .`, an allowlist is the right shape. Deny everything, then re-admit exactly what the two Dockerfiles need:

| Dockerfile | needs |
|---|---|
| `Dockerfile` | `requirements.txt`, `app.py`, `templates/` |
| `Dockerfile.bot` | `scripts/telegram_bot_ingest.py` |

**`scripts/` must not be blanket-excluded** — that would break the bot build. This is the single most likely way to get this plan wrong.

Deny-by-default fails *loudly* (a `COPY` of an excluded file errors at build time) rather than silently leaking, which is the correct trade for this file.

## Target `.dockerignore`

```
# Deny by default, then re-admit only what the Dockerfiles COPY.
# Rationale: this file's job is to make a future `COPY . .` harmless.
# Anything not explicitly re-admitted below cannot reach an image layer,
# which matters because the published packages are (or will be) public.
*

# Dockerfile needs these
!requirements.txt
!app.py
!templates/

# Dockerfile.bot needs this one script -- do NOT blanket-exclude scripts/
!scripts/telegram_bot_ingest.py

# Belt and braces: even if something above is loosened later, these must
# never enter a build context.
.env
.env.*
data/
.git/
.claude/
.venv/
venv/
__pycache__/
*.pyc
```

The trailing explicit denials are redundant against `*` but deliberate: they survive someone later replacing `*` with a permissive list, which is the realistic way this protection erodes.

## Scope

**In scope**: a new `.dockerignore` at the repo root.

**Out of scope — do NOT touch**:
- `Dockerfile` and `Dockerfile.bot`. Do **not** "simplify" their `COPY` lines now that a `.dockerignore` exists — the specific copies are a second, independent layer of protection. Keep both.
- `.gitignore`. Verify it (Step 3) but change nothing.
- `compose.yml`, `app.py`, `scripts/`, `tests/`, `.github/`, `requirements.txt`.
- Package visibility on GitHub. That is the operator's decision and is made in the GitHub UI, not in this repo.

## Steps

### Step 1: Create the file

Write `.dockerignore` exactly as above.

**Verify**: `test -f .dockerignore && wc -l < .dockerignore` → a positive line count.

### Step 2: Prove BOTH images still build, and that nothing sensitive is inside

This is the step that catches the `scripts/` trap.

```
docker build -q -t feather-018 .
docker build -q -f Dockerfile.bot -t feather-bot-018 .
```
Both must succeed. A failure here means the allowlist is missing something a `COPY` needs — fix the allowlist, **not** the Dockerfile.

Then confirm the images are still correct and still clean:

```
docker run --rm feather-018 sh -c 'ls /app'
```
→ `app.py  data  requirements.txt  templates`

```
docker run --rm feather-018 sh -c 'find / -name ".env" -o -name "source.json" 2>/dev/null | grep -v proc | head'
```
→ empty

```
docker run --rm feather-bot-018 sh -c 'ls /app'
```
→ `telegram_bot_ingest.py`

And the app must still actually work:
```
CID=$(docker run -d --rm -p 7092:5000 -e DATA_DIR=/tmp/fd \
      -e ADMIN_PASSWORD=not-a-real-password feather-018)
sleep 8
curl -so /dev/null -w 'source.json %{http_code}\n' http://localhost:7092/source.json
curl -so /dev/null -w 'qr          %{http_code}\n' http://localhost:7092/qr
curl -so /dev/null -w 'index       %{http_code}\n' http://localhost:7092/
docker stop $CID
```
→ all three `200`. The `index` check matters most: it proves `templates/` survived the allowlist.

Clean up: `docker rmi -f feather-018 feather-bot-018`

### Step 3: Prove the protection actually works

The whole point is surviving a careless `COPY . .`. Demonstrate it rather than assuming:

```
cp Dockerfile /tmp/Dockerfile.orig
printf 'FROM python:3.11-slim\nWORKDIR /app\nCOPY . .\nCMD ["true"]\n' > /tmp/Dockerfile.evil
docker build -q -f /tmp/Dockerfile.evil -t leak-test-018 .
docker run --rm leak-test-018 sh -c 'ls -a /app; echo "---"; test -e /app/.env && echo "LEAKED .env" || echo "ok: no .env"; test -d /app/data && echo "LEAKED data/" || echo "ok: no data/"'
docker rmi -f leak-test-018
```
→ `ok: no .env` and `ok: no data/`.

**This is the criterion that matters.** If either says `LEAKED`, the `.dockerignore` is not doing its job — report rather than proceeding.

Confirm `Dockerfile` is untouched afterwards: `diff /tmp/Dockerfile.orig Dockerfile` → no output.

### Step 4: Verify `.gitignore` covers the same ground — report only

```
for p in .env data/ .claude/ .venv/ __pycache__/; do
  printf "%-14s " "$p"; git check-ignore -q "$p" && echo "ignored" || echo "NOT IGNORED"
done
```

All should report `ignored`. **Do not edit `.gitignore`** — if any says `NOT IGNORED`, report it.

## Done criteria

ALL must hold:

- [ ] `.dockerignore` exists and starts with a `*` deny-all line
- [ ] `grep -c "^!scripts/telegram_bot_ingest.py$" .dockerignore` returns `1`
- [ ] Both `docker build` commands succeed
- [ ] `feather-018` contains `app.py`, `requirements.txt`, `templates`, and no `.env` / `source.json`
- [ ] `feather-bot-018` contains `telegram_bot_ingest.py`
- [ ] The running app returns `200` for `/source.json`, `/qr` and `/`
- [ ] The `COPY . .` leak test reports `ok: no .env` **and** `ok: no data/`
- [ ] `Dockerfile` and `Dockerfile.bot` are unmodified — `git diff` on both is empty
- [ ] `git status --short` shows only the new `.dockerignore`
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 78, unchanged
- [ ] `plans/README.md` status row updated

## STOP conditions

- Either image fails to build. The allowlist is missing a path a `COPY` needs. **Fix the allowlist, never the Dockerfile** — loosening a `COPY` to work around the ignore file inverts the whole plan.
- The leak test shows `LEAKED`. Report; do not ship a `.dockerignore` that does not protect.
- You are tempted to simplify a `COPY` line now that the ignore file exists. Both layers stay.
- `git check-ignore` reports something not ignored in Step 4. Report it; do not edit `.gitignore` here.
- You are asked to, or tempted to, change package visibility. That is a GitHub UI decision for the operator.

## Maintenance notes

- **Deny-by-default means new files must be re-admitted explicitly.** Anyone adding a file a Dockerfile needs must add a `!` line, and will find out immediately via a failed build — the right failure mode.
- **The two protections are independent on purpose.** Specific `COPY` lines protect even without this file; this file protects even if a `COPY` is loosened. Removing either halves the safety margin.
- **This does not retroactively clean anything.** Images already published still contain whatever they contained — which, per the audit, is nothing sensitive. If a secret ever does reach a public layer, the response is rotation, not deletion; pulled layers persist elsewhere.
- **Build context also shrinks by ~1.3 GB**, so builds get noticeably faster. That is a side effect, not the goal.
