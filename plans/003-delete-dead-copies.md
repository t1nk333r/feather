# Plan 003: Delete the two dead copies of the application

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git log --oneline | wc -l` must return at least `1`.
> If this repo has no git history, **STOP** — Plan 002 has not run and this
> deletion would be irreversible.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW *once Plan 002 has landed*; HIGH if it has not
- **Depends on**: `plans/002-version-control.md` (mandatory), `plans/004-restore-pillow-qr.md` (harvests a value from `backup.old/` — do that first or capture it in Step 1 here)
- **Category**: tech-debt
- **Planned at**: no VCS at authoring time — 2026-08-10

## Why this matters

This repository contains three copies of the same application:

| | `app.py` | `deepseek-app.py` | `backup.old/app.py` |
|---|---|---|---|
| Lines | 2531 | 1903 (CRLF line endings) | 1834 |
| Routes | 13 | 12 | 11 |
| `SourceManager` methods | 25 | 18 | 15 |
| Icon upload/serve/delete | yes | **no** | **no** |
| Local IPA hosting (`/ipas/…`) | yes | yes | **no** |
| GitHub import (`altparse`) | yes | **no** | yes |
| AltSource import | yes | **no** | yes |

`deepseek-app.py` and `backup.old/` total 3,737 lines — **60% of the Python in this repo** — and both are *strict subsets* of `app.py`. The non-template regions were diffed bidirectionally during the audit: **neither contains a single route, method, validation, or safety check that `app.py` lacks.**

The concrete harm is not disk space. It is that every `grep` for a function name returns three hits across three drifted files, and `Dockerfile:16` (`COPY app.py .`) is the *only* evidence anywhere in the repo of which file is live. A contributor or coding agent can confidently edit the wrong copy and see no effect. `deepseek-app.py` also sits inside the Docker build context, so it is uploaded on every build for a file that is never copied.

There is exactly one thing of value in either directory, and Plan 004 harvests it: `backup.old/requirements.txt` pins `qrcode[pil]` and `pillow`, which the live `requirements.txt` dropped.

## Current state

`Dockerfile:16` — the sole proof of which file is live:

```dockerfile
COPY app.py .
```

`compose.yml` references neither `deepseek-app.py` nor `backup.old/`.

`backup.old/` contains:
```
app.py             72 KB, 1834 lines
compose.yml        484 bytes
Dockerfile         602 bytes
requirements.txt    80 bytes   <-- the one valuable file, see Plan 004
data/source.json               <-- an older catalog, 6 apps vs. the live 8
```

`backup.old/requirements.txt` in full (this is the thing worth keeping):
```
flask==3.0.0
altparse==0.3.0
qrcode[pil]==7.4.2
pillow==10.1.0
requests==2.31.0
```

Compare the live `requirements.txt`:
```
Flask==2.3.3
Flask-Limiter==3.5.0
qrcode==7.4.2
requests==2.31.0
atomicwrites==1.4.1
Werkzeug==2.3.7
altparse==0.3.0
```
Note `pillow` is absent and `qrcode` lost its `[pil]` extra — that is Plan 004's subject. Note also that `flask` went *backwards* from 3.0.0 to 2.3.3.

**Repo conventions**: conventional-commit messages, as established by Plan 002's baseline commit.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Confirm VCS exists | `git log --oneline \| wc -l` | ≥ 1 |
| Confirm files are tracked | `git ls-files \| grep -c "deepseek-app.py"` | `1` |
| Working tree clean | `git status --short` | empty before you start |
| Post-delete listing | `ls` | see Done criteria |

## Scope

**In scope** (files you delete):
- `deepseek-app.py`
- `backup.old/` (entire directory, including `backup.old/data/`)

**Out of scope** (do NOT touch):
- `app.py` — the live application. Do not edit a single line in this plan.
- `data/` — the top-level runtime directory with 1.3 GB of real binaries. **Do not confuse `data/` with `backup.old/data/`.** Deleting the wrong one destroys the product.
- `requirements.txt` — Plan 004 edits it. If Plan 004 has not run yet, capture the pins in Step 1 but do not apply them here.
- `Dockerfile`, `compose.yml`, `.env`.

## Git workflow

- Branch: `advisor/003-delete-dead-copies` (or work on the default branch if the repo has only one).
- **One single commit** for the whole deletion, with a message that records what was removed and why, so `git log` answers "where did `deepseek-app.py` go".

## Steps

### Step 1: Harvest the one valuable artefact

Before deleting anything, preserve `backup.old/requirements.txt`'s Pillow pins. Print them and record them where Plan 004 can find them:

```
cat backup.old/requirements.txt
```

Expected output is the five lines shown in "Current state" above. If Plan 004 has **already** run, confirm the pins survived:

```
grep -c "pillow\|qrcode\[pil\]" requirements.txt
```
→ `2` (meaning Plan 004 already applied them; nothing to harvest)

If that returns `0`, Plan 004 has not run. Copy the file somewhere safe before deleting:
```
cp backup.old/requirements.txt /tmp/backup-old-requirements.txt
```
and note in your final report that Plan 004 still needs to run.

**Verify**: either `grep -c "pillow" requirements.txt` → `1`, **or** `/tmp/backup-old-requirements.txt` exists.

### Step 2: Confirm the deletion is recoverable

**Verify**:
```
git ls-files | grep -c "^deepseek-app.py$"
```
→ `1`

```
git ls-files | grep -c "^backup.old/"
```
→ at least `4`

If either returns `0`, these files are untracked and deletion is **permanent**. That is a STOP condition — go back and complete Plan 002.

### Step 3: Confirm the working tree is clean

**Verify**: `git status --short` → produces no output.

If there are uncommitted changes, commit or stash them first. You want this deletion isolated in its own commit.

### Step 4: Delete

```
cd /home/t1nk33r/Documents/feather
rm deepseek-app.py
rm -rf backup.old/
```

**Immediately verify you deleted the right things**:
```
ls -d data/ && python3 -c "import json; print(len(json.load(open('data/source.json'))['apps']))"
```
Expected: `data/` listed, and `8` printed. If `data/` is gone or the count is wrong, **STOP immediately** and run `git checkout -- .` is *not* sufficient (data/ is gitignored) — report at once.

**Verify**: `ls -d backup.old 2>&1` → `ls: cannot access 'backup.old': No such file or directory`

### Step 5: Commit

```
git add -A
git commit -m "chore: remove dead application copies

Delete deepseek-app.py (1903 lines) and backup.old/ (1834 lines) —
3737 lines, 60% of the repo's Python. Both were strict subsets of
app.py: deepseek-app.py lacked GitHub import, AltSource import, and
all icon support; backup.old/app.py lacked those plus all local IPA
hosting. Neither contained any route, method, or safety check absent
from app.py.

Dockerfile:16 (COPY app.py .) confirms app.py is the only live file.
Recoverable from the baseline commit if ever needed."
```

**Verify**: `git log --oneline | wc -l` → `2` or more, and `git show --stat HEAD | grep -c "deepseek-app.py"` → `1`

### Step 6: Confirm the application still runs

The deleted files were never referenced, so nothing should change. Prove it:

```
docker compose up --build -d
sleep 10
curl -f http://localhost:7000/source.json | python3 -c "import json,sys; print(len(json.load(sys.stdin)['apps']))"
```
Expected: `8`

## Test plan

No code changed, so no new tests. If Plan 005 has already run, its suite is the regression check:

```
python -m pytest tests/ -q
```
Expected: all pass, unchanged from before this plan.

Otherwise the Step 6 container check is the verification.

## Done criteria

ALL must hold:

- [ ] `ls -d backup.old 2>&1` reports "No such file or directory"
- [ ] `test -f deepseek-app.py` returns non-zero (file absent)
- [ ] `ls` shows exactly: `app.py`, `Dockerfile`, `compose.yml`, `requirements.txt`, `data`, `plans` (plus dotfiles `.env`, `.env.example`, `.gitignore`, `.git`)
- [ ] `data/source.json` parses and lists 8 apps
- [ ] `git log --oneline` shows the deletion commit
- [ ] `git show HEAD --stat` lists both `deepseek-app.py` and `backup.old/` files as deleted
- [ ] `curl -f http://localhost:7000/source.json` returns the catalog
- [ ] `grep -rn "deepseek" . --exclude-dir=.git --exclude-dir=plans` returns nothing
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `git log` shows zero commits, or `deepseek-app.py` / `backup.old/` are untracked. Deletion would be permanent. Complete Plan 002 first.
- `data/source.json` reports anything other than 8 apps at any point, or the top-level `data/` directory is missing. **This is the catastrophic failure mode of this plan** — `data/` and `backup.old/data/` are easy to confuse. Report immediately; do not attempt recovery yourself.
- `grep -rn "deepseek-app\|backup.old" Dockerfile compose.yml` returns any match — that would mean one of them *is* referenced after all, contradicting the audit. Report before deleting.
- `du -sh data/` returns anything under 1 GB (it should be ~1.3 GB). Something was deleted that shouldn't have been.

## Maintenance notes

- After this plan, `app.py` is unambiguously the only application file — but nothing *states* that. Consider a short `README.md` or `CLAUDE.md` saying so; it was deferred from the audit as low priority, and this deletion removes most of the ambiguity structurally.
- The `flask==3.0.0` pin in the deleted `backup.old/requirements.txt` is a hint that the live downgrade to `2.3.3` was arbitrary rather than deliberate. A Flask 3 upgrade is a deferred finding — it should only be attempted after Plan 005's test suite exists, because `data/app.log:33` records this app already having been broken once by a Flask API removal (`send_file(cache_timeout=…)`).
- **Reviewer should scrutinise**: the `git show --stat` output for the deletion commit — confirm only the intended 3,737 lines and `backup.old/data/source.json` were removed, and that no top-level `data/` path appears.
