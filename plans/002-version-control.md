# Plan 002: Put the repository under version control

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `ls -a /home/t1nk33r/Documents/feather`
> There must be **no** `.git` directory. If one exists, that is a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW (but see the warning in Step 1 — order genuinely matters here)
- **Depends on**: none. **Plan 003 depends on this one** and must not run first.
- **Category**: dx
- **Planned at**: no VCS — 2026-08-10

## Why this matters

There is no version control of any kind in this repository. What exists instead is a directory literally named `backup.old/` (a full frozen copy of the app) and `deepseek-app.py` (an alternate variant sitting in the Docker build context). Those two files *are* the current recovery mechanism for roughly 4,400 lines of hand-edited code.

That means: no diff, no blame, no bisect, no revert. There is no way to answer "when did `/qr` break" or "what did `save_source` look like before I touched it". Plan 003 deletes 3,737 lines of dead code — irreversibly, unless this plan lands first.

After this plan: changes are revertible, and the two dangerous things in this directory (a live credential and 1.3 GB of binaries) are permanently excluded from ever being committed.

## Current state

Directory contents:

```
app.py              108 KB, 2531 lines — the live application
deepseek-app.py      78 KB, 1903 lines — dead alternate variant (Plan 003 deletes)
backup.old/                            — dead frozen copy (Plan 003 deletes)
Dockerfile           64 lines
compose.yml          30 lines
requirements.txt      8 lines
.env                156 bytes, mode 0755
data/                                  — 1.3 GB of runtime state
plans/                                 — these plan files
```

`data/` contains:
- `source.json` — the hand-curated catalog, 8 apps. **This is the product.**
- `ipas/` — 1.3 GB of `.ipa` binaries across 10 directories
- `icons/`, `uploads/`, `backups/` (empty), `app.log`

`.env` declares five keys — `ADMIN_PASSWORD`, `PORT`, `SECRET_KEY`, `MAX_CONTENT_LENGTH`, `RATE_LIMIT`. **It contains a live plaintext admin password.** Do not read its values, do not print them, do not copy them into any file you create. You need only the key *names*, which are listed above.

The file is currently mode `0755` — readable by every local user on this host, and carrying a pointless execute bit.

**Repo conventions**: none established (no VCS to observe). Use conventional-commit-style messages (`chore:`, `feat:`, `fix:`) — they are a reasonable default and nothing conflicts.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Check for existing repo | `ls -a \| grep -c "^\.git$"` | `0` before Step 3 |
| Init | `git init` | exit 0 |
| What would be committed | `git status --short` | see Step 4 |
| Tracked file list | `git ls-files` | see Step 4 |
| Repo size sanity | `du -sh .git` | under 1 MB |

## Scope

**In scope** (files you create):
- `.gitignore` (create)
- `.env.example` (create)
- `.git/` (created by `git init`)

**Out of scope** (do NOT touch):
- `app.py`, `Dockerfile`, `compose.yml`, `requirements.txt` — commit them unchanged; do not edit.
- `data/` — never add, never modify, never delete anything under it.
- `.env` — you will `chmod` it, but you will **not** read, edit, print, or commit it.
- `deepseek-app.py` and `backup.old/` — Plan 003 deletes them. **Commit them first** in this plan, so that deletion is revertible. Do not delete them here.

## Steps

### Step 1: Write `.gitignore` FIRST

**This ordering is the single most important thing in this plan.** If `git add` runs before `.gitignore` exists, a live credential and 1.3 GB of binaries become permanent objects in git history — and removing them afterwards requires history rewriting.

Create `.gitignore` in the repo root:

```gitignore
# Runtime state — 1.3 GB of binaries and the live catalog. Never commit.
data/

# Secrets
.env

# Python
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.venv/
venv/
```

**Verify**: `test -f .gitignore && grep -c "^data/$" .gitignore` → `1`

### Step 2: Write `.env.example`

Create `.env.example` documenting the configuration contract — **key names only, no values**. Include the two keys Plans 001/008/010 add on top of the existing five.

```dotenv
# Required
ADMIN_PASSWORD=

# Optional
PORT=
SECRET_KEY=
MAX_CONTENT_LENGTH=
RATE_LIMIT=

# Added by plans 001 / 008
DATA_DIR=
PUBLIC_BASE_URL=
```

**Verify**: `grep -c "=$" .env.example` → `7` (every line ends with `=` and nothing after it — no values leaked)

Also confirm you did not accidentally copy real values:
```
diff <(cut -d= -f1 .env | sort) <(cut -d= -f1 .env.example | grep -v "^#" | grep . | sort)
```
This compares key *names* only. Differences are expected (you added two keys); what matters is that `.env.example` has no text to the right of any `=`.

### Step 3: Initialise the repository

```
cd /home/t1nk33r/Documents/feather
git init
```

**Verify**: `git status --short | grep -c "^?? data/"` → `0` (i.e. `data/` is already ignored and does not appear)

**Also verify**: `git status --short | grep -c "^?? .env$"` → `0`

If either returns non-zero, `.gitignore` is wrong. **Fix it before Step 4.** Do not proceed.

### Step 4: Commit the baseline

Stage and commit everything that is not ignored. This deliberately **includes** `deepseek-app.py` and `backup.old/`, so that Plan 003's deletion is recoverable.

```
git add .
git status --short
```

Before committing, confirm the staged set. `git ls-files --cached` should list:
- `app.py`, `Dockerfile`, `compose.yml`, `requirements.txt`
- `.gitignore`, `.env.example`
- `deepseek-app.py`
- `backup.old/app.py`, `backup.old/Dockerfile`, `backup.old/compose.yml`, `backup.old/requirements.txt`, and `backup.old/data/source.json`
- the `plans/*.md` files

It must **not** list `.env` or anything starting with `data/`.

**Verify before committing**: `git diff --cached --name-only | grep -c "^data/\|^\.env$"` → `0`

If that returns anything other than `0`, run `git reset` and go back to Step 1.

Then commit:
```
git commit -m "chore: baseline commit of AltStore source manager

Pre-existing state before any remediation work. Includes the dead
deepseek-app.py and backup.old/ copies so their removal in plan 003
is revertible."
```

**Verify**: `git log --oneline | wc -l` → `1`

### Step 5: Fix `.env` permissions

`.env` is currently mode `0755`. Restrict it:

```
chmod 600 .env
```

**Verify**: `stat -c "%a" .env` → `600`

### Step 6: Sanity-check the repository size

**Verify**: `du -sh .git` → well under 1 MB (expect a few hundred KB).

If `.git` is hundreds of MB, IPA binaries got committed. That is a STOP condition — report it; the repository must be deleted and rebuilt from Step 1 rather than patched.

## Test plan

No code changes, so no automated tests. The verification commands above are the test. One additional end-to-end confirmation that nothing was disturbed:

```
docker compose up -d
sleep 10
curl -f http://localhost:7000/source.json | python3 -c "import json,sys; print(len(json.load(sys.stdin)['apps']))"
```
Expected: `8`

## Done criteria

ALL must hold:

- [ ] `.gitignore` exists and contains `data/` and `.env`
- [ ] `.env.example` exists with 7 keys and **no values**
- [ ] `git log --oneline | wc -l` returns `1`
- [ ] `git ls-files | grep -c "^data/\|^\.env$"` returns `0`
- [ ] `git ls-files | grep -c "deepseek-app.py"` returns `1` (committed, so Plan 003 is revertible)
- [ ] `stat -c "%a" .env` returns `600`
- [ ] `du -sh .git` is under 1 MB
- [ ] `data/source.json` still parses and lists 8 apps
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- A `.git` directory already exists. **Do not run `git init` over it** and do not delete it — report and wait.
- `git status` shows `.env` or any `data/` path as staged or untracked-but-not-ignored, and fixing `.gitignore` does not resolve it.
- `.git` exceeds 10 MB at any point.
- `git` is not installed on this host.
- You are tempted to run `git add -f` on anything. Never force-add here.

## Maintenance notes

**Action required outside this repo, by a human:** the admin password in `.env` must be **rotated**. It has been sitting in a world-readable file, and — per the audit — the application never actually enforced it, so it protected nothing while still being exposed. Treat it as burned. Rotate it before Plan 010 makes it load-bearing.

- The `.gitignore` `data/` rule is permanent and load-bearing. If someone later wants `source.json` in version control (a defensible idea — it is hand-curated and small), it needs an explicit negation (`!data/source.json`) *and* a deliberate decision, because it would then contain the catalog's full download URLs. Do not do this casually.
- `.env.example` must be updated whenever a new `os.environ.get` is added to `app.py`. Plans 008 and 010 both add consumers of keys already listed here, so no update should be needed for those.
- **Reviewer should scrutinise**: `git ls-files` output in the first commit. Anything under `data/` or a `.env` file appearing there is a security incident, not a style nit.
