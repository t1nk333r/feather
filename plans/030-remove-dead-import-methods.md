# Plan 030: Delete the two dead catalog-import methods and drop the `altparse` dependency

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update this plan's status row in
> `plans/README.md` unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 88e6a11..HEAD -- app.py requirements.txt
> ```
> At planning time this diff is empty. If `app.py` or `requirements.txt`
> changed since `88e6a11`, compare the "Current state" excerpts below against
> the live code before proceeding; on a mismatch, treat it as a STOP
> condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tech-debt + dependencies
- **Planned at**: commit `88e6a11`, 2026-08-17

## Why this matters

`SourceManager` carries two import methods — `add_app_from_github` and
`add_app_from_altsource` — that are **dead code**: no Flask route, no caller,
and no test reaches either of them. Together they are ~160 lines. The first is
also the *only* place the pinned `altparse==0.3.0` dependency is used, so the
dependency is dead weight too.

That pin is not free. This repo requires every `requirements.txt` pin to ship
wheels for **both** cp311 (the container's Python) and cp314 (the CI/dev host)
— a constraint that has already broken a build once (see `plans/HANDOFF.md`,
"Every pin in requirements.txt needs wheels for both cp311 and cp314"). Keeping
`altparse` means paying that maintenance tax forever for code nothing can run.

There is a second, subtler reason. `add_app_from_github` *looks* like a
GitHub-release importer, but it is inferior to the one specified in
`plans/029-cron-release-imports.md` (unauthenticated, no request timeout, no
GitLab support, and — fatally — it writes `source.json` in-process, which is
the wrong boundary for any out-of-process scheduler because the catalog lock is
process-local). Deleting it removes a booby-trap: nobody can later wire it up
believing it is the release-import feature. Plan 029 already anticipates this
cleanup and lists the method as out of scope for its own work.

## Current state

- `app.py` — the entire Flask app. The two dead methods live on the
  `SourceManager` class:
  - `add_app_from_github(self, data)` at **app.py:1018–1068**
  - `add_app_from_altsource(self, data)` at **app.py:1070–1177**
- The `altparse` import is at the top of the file:

  ```python
  # app.py:18
  from altparse import AltSourceManager, Parser, AltSource
  ```

  Its only uses are inside `add_app_from_github`:

  ```python
  # app.py:1026–1036 (inside add_app_from_github)
  src = AltSource(**source_data)
  sources_data = [{
      "parser": Parser.GITHUB,
      "kwargs": {
          "repo_author": data['repo_author'],
          "repo_name": data['repo_name']
      },
      "ids": [data.get('app_id', '')]
  }]
  srcmgr = AltSourceManager(src, sources_data)
  ```

  `add_app_from_altsource` does **not** use `altparse` at all — it merges JSON
  by hand with `requests` + stdlib — but it is equally unreachable, so it is
  deleted in the same pass.
- `requirements.txt` pins the dependency:

  ```
  # requirements.txt:6
  altparse==0.3.0
  ```

- Verification that nothing reaches these methods (run it yourself; expect the
  method definitions and the import line, and nothing that *calls* them):

  ```bash
  grep -rn "add_app_from_github\|add_app_from_altsource\|add-github\|add-altsource" \
    app.py templates/index.html tests/ scripts/
  ```

  At planning time this returns exactly the two `def` lines in `app.py` and
  nothing else — no routes, no template buttons, no tests.

### Repo conventions to follow

- Deletion only; do not reformat or "tidy" surrounding methods. This mirrors
  `plans/003-delete-dead-copies.md`, which was accepted precisely because it
  was pure subtraction with the test count unchanged.
- Commit style is conventional commits — see `git log` (e.g.
  `fix(storage): verify icon object sizes`). Use a `refactor` or `chore` type.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Baseline tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `136 passed` **before** any change |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| Import still works | `ADMIN_PASSWORD=x .venv/bin/python -c "import app"` | exit 0, no traceback |
| Full tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `136 passed` **after** the change |

`ADMIN_PASSWORD=x` is mandatory: `app.py` raises `RuntimeError` at import time
without it, and the test suite imports `app.py`.

## Scope

**In scope** (the only files you may modify):
- `app.py` — delete the two methods and the `altparse` import line
- `requirements.txt` — remove the `altparse==0.3.0` pin

**Out of scope** (do NOT touch, even though they look related):
- Any live route, the frontend template, or any other `SourceManager` method.
  This change removes code; it must not alter behavior of anything that runs.
- `plans/029-cron-release-imports.md` and anything it describes. **Do not build
  the release importer here.** This plan only deletes the old dead one.
- `requirements-dev.txt`, `Dockerfile`, `Dockerfile.bot`, `compose.yml`. The
  bot image installs only `requests` and never imported `altparse`; the main
  Dockerfile installs from `requirements.txt`, so removing the pin there is
  enough.
- `boto3`, `qrcode`, `pillow`, `requests`, `Werkzeug`, `Flask` — every other
  pin is still imported and used. `altparse` is the only one this plan removes.

## Git workflow

- Branch: `advisor/030-remove-dead-import-methods`
- One commit is fine (it is a single logical change). Example message:
  `refactor(source): remove dead GitHub/AltSource import methods and altparse`
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Confirm the baseline is green

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

**Verify**: `136 passed`. If it is not 136, STOP — the tree has drifted from
the plan's baseline.

### Step 2: Delete the two dead methods from `app.py`

Remove the entire method bodies:

- `add_app_from_github` — from its `def add_app_from_github(self, data):` line
  (app.py:1018) through the end of its `except` block (app.py:1068), inclusive.
- `add_app_from_altsource` — from `def add_app_from_altsource(self, data):`
  (app.py:1070) through the end of its final `except Exception` block
  (app.py:1177), inclusive.

These two methods are adjacent (1018–1177 with a blank line between), so this
is one contiguous deletion. The method *before* them is `add_app_manual`
(ends ~1016) and the method *after* is `delete_app` (starts at
`def delete_app(self, bundle_identifier):`, app.py:1179). After deletion,
`add_app_manual` should be immediately followed by `delete_app` with the class's
normal single blank-line spacing.

**Verify**:

```bash
grep -n "add_app_from_github\|add_app_from_altsource" app.py    # no output
.venv/bin/python -m py_compile app.py                           # exit 0
```

### Step 3: Remove the now-unused `altparse` import

Delete this line from `app.py` (line 18):

```python
from altparse import AltSourceManager, Parser, AltSource
```

Leave every other import untouched (`json`, `copy`, `requests`, `boto3`, etc.
are all still used).

**Verify**:

```bash
grep -n "altparse\|AltSourceManager\|AltSource(\|Parser\." app.py   # no output
.venv/bin/python -m py_compile app.py                               # exit 0
ADMIN_PASSWORD=x .venv/bin/python -c "import app"                    # exit 0, no traceback
```

> Note: `grep` may still match the word "AltSource" inside a docstring
> ("Manages the AltSource data...") or a log string in `add_app_manual` — those
> are plain English, not the dependency, and are fine to leave. The pattern
> above (`AltSource(` / `AltSourceManager` / `Parser.`) matches only real API
> usage, and must return nothing.

### Step 4: Drop the pin from `requirements.txt`

Remove the line `altparse==0.3.0` (requirements.txt:6). Do not touch any other
line.

**Verify**:

```bash
grep -n "altparse" requirements.txt     # no output
```

### Step 5: Re-run the full suite

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

**Verify**: `136 passed`. The count is unchanged because nothing tested the
deleted code.

> The `.venv` may still have `altparse` physically installed — that is fine and
> expected; this plan does not uninstall it locally. The proof that the app no
> longer needs it is Step 3's clean `import app` and the passing suite. CI
> installs from the edited `requirements.txt` on a fresh interpreter, which is
> where the removed pin is truly exercised.

## Test plan

- No new tests. This is a deletion of untested, unreachable code; the existing
  136-test suite is the regression net, and its count must stay 136.
- The load-bearing assertion is `import app` still succeeding **after** the
  `altparse` import line is gone (Step 3) — that is what proves nothing else in
  the module depended on it.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `grep -n "add_app_from_github\|add_app_from_altsource" app.py` — no output
- [ ] `grep -n "altparse\|AltSourceManager\|AltSource(\|Parser\." app.py` — no output
- [ ] `grep -n "altparse" requirements.txt` — no output
- [ ] `.venv/bin/python -m py_compile app.py` — exit 0
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -c "import app"` — exit 0, no traceback
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` — `136 passed`
- [ ] `git status --short` shows only `app.py` and `requirements.txt` modified
- [ ] `plans/README.md` status row for Plan 030 updated

## STOP conditions

Stop and report back (do not improvise) if:

- The baseline in Step 1 is not `136 passed` — the tree has drifted.
- `grep` in "Current state" finds a route, template control, or test that
  *calls* `add_app_from_github` or `add_app_from_altsource`. That would mean the
  code is not dead after all; report it rather than deleting a live path.
- `import app` fails after removing the `altparse` import — some other code
  imported a name from `altparse` that this plan did not account for. Report the
  traceback.
- The test count changes from 136 in either direction. A drop means you deleted
  something reachable; report it.

## Maintenance notes

- If `plans/029-cron-release-imports.md` is later executed, it must **not**
  resurrect these methods — it deliberately reimplements release polling
  through the authenticated HTTP API with GitLab support and redirect
  hardening. This deletion and plan 029 are complementary.
- After this lands, the `plans/README.md` "considered and rejected" entry
  "Migrating off `altparse==0.3.0`" is obsolete (the dependency is gone, not
  migrated) and can be retired in the index update.
- A reviewer should confirm the diff is pure subtraction: no line outside the
  two method bodies, the one import line, and the one requirements line changed.
