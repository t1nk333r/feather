# Plan 027 — Write the repository README

**Written against commit `fc1b72b`.** Drift check before starting:

```bash
git ls-files | grep -ic readme          # 1  -- that one hit is plans/README.md, NOT a repo README
test -f README.md && echo EXISTS        # prints nothing; the file must not exist yet
wc -l app.py templates/index.html       # 1650 and 1575
grep -c "def test_" tests/test_routes.py  # 64
```

If `README.md` already exists, **STOP** — someone else wrote one and this plan needs
reconciling rather than executing.

**Test command** (the `ADMIN_PASSWORD` prefix is mandatory — the app refuses to import
without it):

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q      # 119 passed today
```

---

## Why

The repository has **no README**. `git ls-files | grep -i readme` returns exactly one hit,
`plans/README.md`, which is the plan index — a different document for a different reader.

The cost is concrete and already paid twice. Nothing in the repo states which Python file is
live, so an earlier session had to diff three near-identical copies of the app to find out
(Plan 003 deleted two of them). Nothing states that `ADMIN_PASSWORD` is required to boot, so
a deployment came up dead until the cause was traced by hand. Nothing states that a fresh
`data/` bind mount must be `chown`ed, so a first start failed with `PermissionError` and
served nothing.

`plans/HANDOFF.md` captures much of this, but it is a session handoff written for someone
resuming *work in progress* — it opens with "read this before touching anything" and leads
with traps. Someone who just wants to run this needs a different first page.

---

## Audience and scope decisions

Resolved from the repo rather than asked, and stated here so the executor does not re-litigate:

- **Audience: someone deploying this from scratch**, including the author six months from
  now. Not contributors (there are none), not end users of the iOS source.
- **No real infrastructure values.** The repo is private today (`gh repo view` →
  `visibility=PRIVATE`), but a README is the file most likely to be read if that ever
  changes. `plans/HANDOFF.md` already contains the live hostname, a LAN IP, and TrueNAS
  paths; do **not** copy any of them into the README. Use `feather.example.com` and
  `s3.example.com` placeholders throughout.
- **Link to `plans/HANDOFF.md`, do not duplicate it.** Duplicated operational detail drifts
  apart, and then two documents disagree. The README covers what the project is and how to
  run it; the handoff keeps the traps and outstanding operator tasks.

---

## Scope

**In scope: one new file, `README.md`, at the repo root.** Nothing else.

**Explicitly out of scope — do not create or modify:**

- `app.py`, `templates/`, `static/`, `scripts/`, `tests/` — no code changes of any kind.
- `plans/README.md` — that is the plan index; leave it alone.
- `plans/HANDOFF.md` — it is stale in two places (it lists Plan 014 as TODO and says 108
  tests) but fixing it is not this plan's job. Do not edit it.
- **`LICENSE`.** The repo has none. Choosing one is the owner's legal decision, not an
  executor's. Do not add a license file and do not state a license in the README.
- `.github/`, `Dockerfile`, `compose.yml`, `.env.example`.

---

## Facts to use — all verified at `fc1b72b`

Use these. Do not re-derive them, and do not state anything not listed here or checked in
Step 2.

| Fact | Value |
|---|---|
| What it is | Self-hosted AltStore/Feather iOS app-source manager |
| Language / framework | Python, Flask `2.3.3`, Werkzeug `2.3.7` |
| Container base | `python:3.11-slim`, runs as non-root `USER altstore`, `EXPOSE 5000` |
| CI test matrix | Python `3.11` and `3.14` |
| Tests | 119, three files: `tests/test_routes.py`, `tests/test_storage.py`, `tests/test_telegram_bot_ingest.py` |
| Published images | `ghcr.io/d7eeem/feather` and `ghcr.io/d7eeem/feather-bot` (both public) |
| Healthcheck | `curl -f http://localhost:5000/source.json` |
| Container port | 5000, published on the host as 7000 in `compose.yml` |

**Source layout:**

```
app.py                          the entire Flask app (1650 lines)
templates/index.html            the admin UI (1575 lines)
static/                         source icon and favicons
scripts/telegram_bot_ingest.py  optional: forward-an-IPA-to-a-bot ingest worker
scripts/migrate_ipas_to_garage.py  one-shot local-disk -> Garage S3 migration
tests/                          119 tests; no network, no Docker
plans/                          numbered implementation plans; README.md is the index
```

**Routes — this table must appear in the README.** Read off `app.py` at the listed lines:

| Route | Auth | Purpose |
|---|---|---|
| `GET /source.json` | **public** | the catalog every iOS client polls |
| `GET /ipas/<bundle_id>/<filename>` | **public** | IPA download (302 to Garage when enabled) |
| `GET /icons/<bundle_id>/icon.<ext>` | **public** | app icons |
| `GET /qr` | **public** | QR code for the `feather://` source URL |
| `GET /` | public | admin UI |
| `POST /api/login`, `/api/logout`, `GET /api/session` | public | session auth |
| `GET /api/apps`, `GET /api/app/<id>` | public | read-only JSON |
| `POST /api/add-app`, `/api/delete-app`, `/api/update-app`, `/api/add-version`, `/api/update-version`, `/api/update-source` | **session required** | the six mutating routes |

State plainly that **the first four must never require authentication** — iOS clients fetch
them with no credentials, and gating any of them breaks every subscribed device.

**Environment variables.** Do not invent descriptions; `.env.example` already documents each
one and the README should summarise, not replace it. Required vs optional:

- **Required:** `ADMIN_PASSWORD` — **the app raises `RuntimeError` and refuses to start if
  it is unset** (`app.py:38-42`). This is the single most likely first-run failure and must
  be called out, not buried in a list.
- **Commonly set:** `PUBLIC_BASE_URL` (canonical origin baked into catalog URLs; without it
  the app falls back to the client-supplied `Host` header and logs a warning), `DATA_DIR`,
  `PORT`, `SECRET_KEY`, `MAX_CONTENT_LENGTH`.
- **Optional feature groups:** `STORAGE_BACKEND` + `GARAGE_*` (S3 storage, defaults to
  `local`), `TELEGRAM_*` + `BOT_API_*` + `FEATHER_*` (bot ingest and notifications, both
  off unless configured).

Point the reader at `.env.example` for the full annotated list.

---

## Required sections, in this order

1. **Title + one-paragraph description.** What it is and who it is for. Factual and neutral —
   describe it as a self-hosted app-source manager. Do not editorialise about what people
   host on it.
2. **Status / caveats — put this near the top, not at the bottom.** Three honest statements:
   it runs the **Werkzeug development server** (`app.py` `app.run()`), so put a real reverse
   proxy in front for anything beyond a private network; **there is no rate limiting**
   (`Flask-Limiter==3.5.0` is declared in `requirements.txt` and imported nowhere — verify
   with the command in Step 2 before writing this); and the in-process write lock assumes a
   single worker, so `gunicorn -w >1` would silently break it.
3. **Quick start (Docker).** `cp .env.example .env`, set `ADMIN_PASSWORD`,
   `chown -R 999:999 data/`, `docker compose up -d`, browse to `http://localhost:7000`.
   The `chown` step is **not optional** — a fresh bind mount is created as root and the
   non-root container user cannot write to it, which fails as `PermissionError` with no
   useful message.
4. **Quick start (local, no Docker).** venv, `pip install -r requirements.txt -r
   requirements-dev.txt`, then `DATA_DIR=/tmp/feather ADMIN_PASSWORD=x python app.py`.
5. **Running the tests.** The exact command, including the mandatory `ADMIN_PASSWORD=x`
   prefix, and why it is needed.
6. **Configuration.** The environment-variable summary above, pointing at `.env.example`.
7. **Routes.** The table above, with the never-gate-these note.
8. **Updating a deployment.** `docker compose pull && docker compose up -d` — and state
   explicitly **not `--build`**: building locally produces a different artifact from the
   CI-verified image, silently bypassing what CI checked.
9. **Optional features.** Two short subsections — Garage S3 storage and Telegram ingest /
   notifications — each one or two sentences saying it is off by default and pointing at the
   relevant plan file for detail.
10. **Repository layout.** The tree above.
11. **Contributing / conventions.** Four things a change must respect: the four public routes
    stay public; `data/` is gitignored and irreplaceable, never `git add` it; `.dockerignore`
    is **deny-by-default**, so a new file a Dockerfile needs must be re-admitted with a `!`
    line; and every pin in `requirements.txt` needs wheels for **both** cp311 (container) and
    cp314 (CI) — `pillow==10.1.0` publishes no cp314 wheel and this has already broken a
    build once.
12. **Further reading.** Link `plans/README.md` (index of implementation plans, and the record
    of what was considered and rejected) and `plans/HANDOFF.md` (deployment traps and
    outstanding operator tasks).

---

## Step 1 — Write `README.md`

Follow the section list above. Style guidance:

- GitHub-flavoured Markdown. One `#` title, `##` for the sections above.
- Every shell command in a fenced block with a language tag.
- Keep it **under 200 lines**. This is a front page, not a manual — depth lives in
  `.env.example` and `plans/`.
- Match the existing documentation voice in `plans/HANDOFF.md`: direct, second person,
  concrete. Read its first 30 lines before writing to calibrate tone.

## Step 2 — Verify every factual claim before finishing

Do not skip this. A README that lies is worse than none, because it is believed.

```bash
# no rate limiting -- must print 0, or do not write the claim
grep -c "limiter\|Limiter" app.py

# ADMIN_PASSWORD really is fatal -- must print the RuntimeError, not a running server
.venv/bin/python -c "import app" 2>&1 | tail -2

# the four public routes really are ungated (no @requires_auth above them)
grep -A1 "@app.route('/source.json')\|@app.route('/qr')" app.py

# test count matches what the README states
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q | tail -1

# host port really is 7000
grep -n "7000" compose.yml
```

## Step 3 — Check for leaked infrastructure detail

```bash
grep -nE "example|<lan-ip>|/path/to|truenas|dockge" README.md
```

**Must produce no output.** If it matches, replace the value with a placeholder. This is the
one check that matters if the repository is ever made public.

---

## Done criteria — all must hold

```bash
# 1. the file exists and is a reasonable length
test -f README.md && wc -l README.md            # between 100 and 200 lines

# 2. no infrastructure disclosure
grep -cE "example|<lan-ip>|/path/to|truenas|dockge" README.md   # 0

# 3. no secret-shaped content
grep -ciE "ADMIN_PASSWORD=[^\s]|BOT_TOKEN=[0-9]|SECRET_KEY=[A-Za-z0-9]{8}" README.md   # 0

# 4. the load-bearing facts are present
grep -c "ADMIN_PASSWORD" README.md              # >= 2
grep -c "source.json" README.md                 # >= 2
grep -c "docker compose pull" README.md         # >= 1
grep -c "chown" README.md                       # >= 1
grep -c "plans/HANDOFF.md" README.md            # >= 1

# 5. nothing else changed -- this plan touches exactly one file
git status --short                              # only "?? README.md"

# 6. the repo still works (this plan changes no code, so this must be unaffected)
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q | tail -1   # 119 passed
```

Criterion 5 is the important one: a docs plan that touches code has gone wrong.

---

## STOP and report instead of improvising if

- `README.md` already exists.
- Any command in Step 2 contradicts a fact in this plan's table — for example if
  `grep -c "limiter" app.py` returns non-zero, meaning rate limiting *was* wired up since
  this plan was written. **Report the discrepancy; do not write the claim either way, and do
  not "fix" `app.py` to match the plan.**
- The test count is not 119. Report the actual number and use it in the README rather than
  the stale one.
- You find yourself wanting to add a `LICENSE`, edit `plans/HANDOFF.md`, or "quickly fix"
  something you noticed while reading the code. Out of scope — note it in your report
  instead.

---

## Maintenance note

The README now carries a handful of numbers that will drift: the test count, line counts,
and the Flask/Werkzeug pins. Prefer phrasing that degrades gracefully ("the full suite runs
in a few seconds with no network or Docker") over precise figures wherever the precision
buys nothing. Where a number genuinely helps, expect to update it.

The routes table is the part most worth keeping correct — it encodes the
never-gate-the-public-routes contract, which is the single constraint that breaks every
subscribed iOS device if violated.
