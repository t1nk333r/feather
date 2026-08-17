# Plan 031: Stamp catalog dates in real UTC, and replace deprecated `datetime.utcnow()`

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update this plan's status row in
> `plans/README.md` unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 88e6a11..HEAD -- app.py tests/test_routes.py
> ```
> At planning time this diff is empty. If `app.py` changed since `88e6a11`,
> compare the "Current state" excerpts below against the live code before
> proceeding; on a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug + migration
- **Planned at**: commit `88e6a11`, 2026-08-17

## Why this matters

When Feather adds an app or version, it stamps the version's `date` field like
this (`app.py:918–924`):

```python
def get_current_dates(self):
    current_date = datetime.now()
    return {
        'feather_date': current_date.strftime("%Y-%m-%d"),
        'version_date': current_date.strftime("%Y-%m-%dT%H:%M:%SZ")
    }
```

`datetime.now()` returns a **naive local-time** value, but the format string
appends a literal `Z`, which in ISO-8601 means UTC. So the published timestamp
*claims* to be UTC while actually carrying the server's local wall-clock time.

Today this is correct only by luck: the production container
(`python:3.11-slim`) has no timezone configured, so it runs in UTC and
`datetime.now()` happens to equal UTC. The moment this app runs anywhere with a
`TZ` set — a developer's laptop, a differently-configured host, a future base
image — every new version's `date` is silently wrong by the UTC offset (hours).
AltStore/Feather clients order and track updates by that timestamp, so a wrong
value can misorder versions.

Separately, the backup path uses `datetime.utcnow()` (`app.py:866`), which
Python has **deprecated** and scheduled for removal; it already prints a
`DeprecationWarning` on 3.12+. Both problems have the same one-line fix:
timezone-aware UTC.

## Current state

- `app.py:16` imports `datetime` but not `timezone`:

  ```python
  from datetime import datetime
  ```

- `app.py:918–924` — `SourceManager.get_current_dates()`, the source of every
  `date`/`addedDate` written by `add_app_manual` (app.py:933) and `add_version`
  (app.py:1298). (Two other call sites, app.py:1041 and 1106, live in methods
  that `plans/030-remove-dead-import-methods.md` deletes — see "Interaction
  with Plan 030" below. Whether or not 030 has run, this plan changes only
  `get_current_dates` itself, so both are covered.)

- `app.py:866` — inside `_backup_source`, the deprecated call:

  ```python
  timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
  ```

- The deprecation is observable now:

  ```bash
  ADMIN_PASSWORD=x .venv/bin/python -W error::DeprecationWarning \
    -c "import app; app.SourceManager('/tmp/plan031-probe/source.json')._backup_source()" 2>&1 | tail -3
  ```

  (Creates `/tmp/plan031-probe`; harmless throwaway. On the current code this
  raises the `utcnow()` DeprecationWarning as an error. After the fix it does
  not.)

### Repo conventions to follow

- Tests live in `tests/`, import the app via `importlib.reload` after setting
  `DATA_DIR`/`ADMIN_PASSWORD`/`SECRET_KEY` env vars (see the `client` fixture in
  `tests/test_routes.py:73–100`). New assertions on `get_current_dates` can call
  it directly on the reloaded module's `source_manager` — model after existing
  direct-manager tests such as `test_update_source_persists_name`
  (`tests/test_routes.py:621`).
- Conventional-commit messages (see `git log`).

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Baseline tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `136 passed` before changes |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| Focused test | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k utc` | new test passes |
| Full tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `137 passed` |
| No utcnow left | `grep -n "utcnow" app.py` | no output |

## Scope

**In scope** (the only files you may modify):
- `app.py` — the import line, `get_current_dates`, and the `_backup_source`
  timestamp
- `tests/test_routes.py` — add one regression test (create)

**Out of scope** (do NOT touch):
- Any date *parsing* code (e.g. `datetime.fromisoformat(...)` used to reformat
  incoming external dates). This plan fixes only the two places the app
  *generates* a timestamp.
- The stored `data/source.json` and any existing catalog entry. This is a
  code-only fix; it does not rewrite historical dates.
- `get_current_dates`'s return keys or format strings — `feather_date` stays
  `%Y-%m-%d` and `version_date` stays `%Y-%m-%dT%H:%M:%SZ`. Only the *value's*
  timezone changes (now genuinely UTC), so the `Z` becomes truthful. Downstream
  code and existing tests that read these fields must keep working unchanged.

## Git workflow

- Branch: `advisor/031-utc-correct-dates`
- Example commit: `fix(source): stamp catalog dates in UTC, drop deprecated utcnow`
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Confirm the baseline is green

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

**Verify**: `136 passed`. If not, STOP — the tree has drifted.

### Step 2: Import `timezone`

Change `app.py:16` from:

```python
from datetime import datetime
```

to:

```python
from datetime import datetime, timezone
```

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 3: Make `get_current_dates` use UTC

In `get_current_dates` (app.py:918–924), change only the first line of the body:

```python
current_date = datetime.now(timezone.utc)
```

Leave both `strftime` lines exactly as they are. `datetime.now(timezone.utc)`
is timezone-aware UTC, so `%Y-%m-%dT%H:%M:%SZ` now produces a value whose `Z`
is accurate regardless of the host's timezone.

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 4: Replace the deprecated `utcnow()` in `_backup_source`

Change `app.py:866` from:

```python
timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
```

to:

```python
timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
```

The produced string is identical in format; only the deprecated API is
replaced.

**Verify**:

```bash
grep -n "utcnow" app.py            # no output
.venv/bin/python -m py_compile app.py   # exit 0
```

### Step 5: Add a regression test that discriminates on timezone

Append this test to `tests/test_routes.py`. It sets a non-UTC process timezone,
then asserts the stamped `version_date`, interpreted as UTC, is within two
minutes of the real UTC now. With the old `datetime.now()` code this value would
be off by the America/New_York offset (~4–5 hours) and the test fails; with the
fix it passes.

```python
def test_get_current_dates_stamps_utc_not_local_time(client, monkeypatch):
    """Regression for Plan 031: version_date carries a literal 'Z' (UTC), so
    the value must be real UTC regardless of the process timezone. Under a
    non-UTC TZ, the pre-fix datetime.now() (naive local time) would be off by
    the offset."""
    import time
    from datetime import datetime, timezone

    app_module = client.app_module
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        dates = app_module.source_manager.get_current_dates()
        stamped = datetime.strptime(
            dates["version_date"], "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        assert abs((now - stamped).total_seconds()) < 120
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()
```

> `time.tzset()` applies the `TZ` change to the running process (Unix only,
> which this project targets). The `finally` restores the previous timezone so
> the change does not leak into later tests.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k utc
```

**Prove it discriminates**: temporarily revert Step 3 (put back
`current_date = datetime.now()`), rerun the focused test, and confirm it
**fails**. Then restore the fix and confirm it passes again. Do not leave the
revert in place.

### Step 6: Run the full suite

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

**Verify**: `137 passed` (136 existing + 1 new).

## Test plan

- New test: `test_get_current_dates_stamps_utc_not_local_time` in
  `tests/test_routes.py`, covering the core regression (local-time-labeled-as-Z).
- Model: existing direct-`source_manager` tests such as
  `test_update_source_persists_name` (`tests/test_routes.py:621`) for how to
  reach the reloaded module via `client.app_module`.
- The deprecation removal is verified structurally by `grep -n "utcnow" app.py`
  returning nothing; no separate test needed.
- Verification: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` →
  `137 passed`, and the Step 5 discrimination check fails on the reverted code.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `grep -n "utcnow" app.py` — no output
- [ ] `grep -n "datetime.now(timezone.utc)" app.py` — at least two matches
      (`get_current_dates` and `_backup_source`)
- [ ] `grep -n "from datetime import datetime, timezone" app.py` — one match
- [ ] `.venv/bin/python -m py_compile app.py` — exit 0
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` — `137 passed`
- [ ] Reverting Step 3 makes `test_get_current_dates_stamps_utc_not_local_time`
      fail; restoring it makes it pass
- [ ] `git status --short` shows only `app.py` and `tests/test_routes.py`
      modified
- [ ] `plans/README.md` status row for Plan 031 updated

## STOP conditions

Stop and report back (do not improvise) if:

- The baseline in Step 1 is not `136 passed`.
- Any existing test starts failing after Step 3 or Step 4 — that would mean a
  test was asserting the buggy local-time behavior; report it rather than
  editing the existing test to match.
- `time.tzset()` raises `AttributeError` (a non-Unix platform). Report it; the
  discrimination test needs a different strategy there and this deployment is
  Linux, so it should not happen.

## Maintenance notes

- This fixes only *newly generated* timestamps. Existing `data/source.json`
  entries keep whatever value they were written with; correcting those is a
  hand edit and an operator decision, not part of this code change.
- Any future code that stamps a date must use `datetime.now(timezone.utc)`, not
  `datetime.now()` or `datetime.utcnow()`. The grep done-criteria guard the
  latter.
- A reviewer should confirm the format strings were left untouched — the fix is
  the timezone of the *value*, not the shape of the string.
