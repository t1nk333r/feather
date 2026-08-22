# Plan 069: Turn Catalog Health into an actionable, scheduled watchtower

> **Executor instructions**: Follow the steps and verification gates exactly.
> This plan adds navigation, history, and alerts—not automatic repair. Stop if
> implementation requires mutating catalog/storage objects. Update the status
> row in `plans/README.md` when complete unless review owns the index.
>
> **Drift check (run first)**:
> `git diff --stat f8a8827..HEAD -- app.py templates/index.html .env.example README.md tests/test_routes.py tests/test_auto_import.py`
> Stop if the health response shape, app editor hooks, scheduler startup, or
> notification contract no longer matches Current state.

## Status

- **Priority**: P2
- **Effort**: M–L
- **Risk**: MED
- **Depends on**: none
- **Category**: direction
- **Planned at**: commit `f8a8827`, 2026-08-22

## Why this matters

Catalog Health already detects concrete failures, but renders them as inert
text and runs only when an operator remembers to press Scan. Recent errors are
in memory and disappear on restart. Add issue-to-editor navigation, a bounded
health snapshot history, an optional default-off scheduler, and Telegram alerts
only when state moves between healthy and degraded. Preserve the scanner's
read-only guarantee and explicitly reject “Fix all.”

## Current state

`app.py:2973-3108` returns structured issues with bundle/version/kind/detail/
severity. The route at `app.py:3111-3123` is authenticated and read-only.

`templates/index.html:1432-1444` renders each issue as a plain list item:

```javascript
html += `<li ...><strong>${bundle}${version}</strong> - ${detail}</li>`;
```

The app editor is already reachable through `editApp(bundleId)`
(`templates/index.html:1746-1806`), and `displayVersions()` renders every version
(`templates/index.html:1808-1851`). These are the correct reuse points; do not
create a second editor.

Diagnostics are a 200-entry in-memory deque (`app.py:36-65`). Telegram
notifications are best-effort and event-filtered (`app.py:239-273`). The only
background thread starts from `__main__`, never at import time
(`app.py:2586-2634`, `app.py:3280-3289`). Match that convention.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Health/routes | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'health or diagnostic'` | all selected tests pass |
| Scheduler safety | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_auto_import.py -q -k 'thread or scheduler'` | all selected tests pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | all pass; baseline 243 |
| Diff hygiene | `git diff --check` | no output |

## Scope

**In scope**:

- `app.py`
- `templates/index.html`
- `.env.example`
- `README.md`
- `tests/test_routes.py`
- `tests/test_auto_import.py`

**Out of scope**:

- Any automatic catalog, icon, or IPA repair.
- Downloading full Garage IPAs to validate ZIP contents.
- A Docker socket, container restart, deployment control, or self-update button.
- Persisting raw exception traces, secrets, URLs, or filesystem details.
- Replacing the existing diagnostics ring buffer.
- Multiple process workers; the app remains single-process Waitress.

## Git workflow

- Branch: `advisor/069-actionable-health-watchtower`
- Conventional commit example: `feat(health): persist scheduled health snapshots`.
- Keep navigation and monitoring changes logically separated if practical.
- Do not push/open a PR unless instructed.

## Target data contract

Store `DATA_DIR/health-monitor.json` atomically:

```json
{
  "schemaVersion": 1,
  "enabled": false,
  "intervalHours": 6,
  "lastRunAt": null,
  "lastState": null,
  "snapshots": []
}
```

Retain at most 30 snapshots. Each snapshot stores timestamp, trigger
(`manual`/`scheduled`), state (`healthy`/`degraded`), severity counts, and issue
identities (`kind`, `severity`, `bundleIdentifier`, `version`) only. Do **not**
persist `detail`: it may contain container paths or operational URLs. Loading a
missing/corrupt store returns the safe disabled default and logs a warning.

## Steps

### Step 1: Add direct, non-mutating remediation navigation

Change health rendering so issues with a bundle ID include an “Open app” action.
Extend `editApp(bundleId, focusVersion = null)` and/or `displayVersions()` so a
version issue opens the existing modal, highlights that version, and scrolls it
into view after the modal is visible. App-level issues open the app without a
version focus. Source-level issues show concise manual guidance and no fake action.

Use event handlers or safely escaped arguments; provider/catalog strings must
pass through `escapeHtml()` and URL path values through `encodeURIComponent()`.
On close, keep the health results intact so the operator returns to the queue.

**Verify**:

```bash
rg -n "Open app|focusVersion|health.*issue" templates/index.html
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k health
```

Expected: markup/hooks exist and health tests pass.

### Step 2: Add bounded atomic health history

In `app.py`, add load/save helpers mirroring `load_auto_import()` and
`save_auto_import()` temp-file + `fsync` + `os.replace` behavior. Add a lock
dedicated to the health store. Create `_health_snapshot(issues, trigger)` and
append/prune logic. Redact/sanitize before persistence as specified above.

Refactor `_scan_catalog_health()` to accept an optional explicit base URL or
host context so it can run outside a request. In scheduled mode with no
`PUBLIC_BASE_URL`, skip same-host comparisons and record an informational
limitation; never create a Flask request context from guessed data.

Manual `GET /api/health` may record a snapshot after a successful scan. A scan
failure must not create a false healthy snapshot.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'health and (history or snapshot or scan)'
```

Expected: atomic/pruning/corrupt-file tests pass; no snapshot includes `detail`.

### Step 3: Add authenticated monitor configuration/history API

Add:

- `GET /api/health-monitor` — config, last run/state, newest-first snapshots;
- `POST /api/health-monitor/config` — `enabled` and bounded `intervalHours`
  (1–168), using the same JSON/error conventions as auto-import config.

Both routes require auth. The GET response must not expose stored paths, URLs,
or raw diagnostic messages. Saving config should wake the monitor thread so a
change is picked up promptly.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k health_monitor
```

Expected: auth, bounds, round-trip, corrupt-store fallback, and response
minimization tests pass.

### Step 4: Run scheduled scans and alert only on state transitions

Add `_health_monitor_loop(stop_event)` and `_start_health_monitor()` following
the auto-import scheduler pattern. Start it only under `if __name__ == '__main__'`.
It must be disabled by default, wake at most every five minutes to re-read
config, and never overlap its own scan.

Call `notify("health_transition", ...)` only for:

- previous known state `healthy` -> `degraded`;
- previous known state `degraded` -> `healthy`.

The message may contain counts and issue kinds, not full detail/path/URL data.
First-ever state establishment sends no alert. Failures are logged and do not
advance `lastState`. Document that operators must include `health_transition`
in `TELEGRAM_NOTIFY_EVENTS` to receive these alerts; keep it out of the default
event set to avoid surprising existing deployments.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py tests/test_auto_import.py -q -k 'health_monitor or importing_app_module_starts_no_thread'
```

Expected: transition de-duplication, recovery alert, disabled default, no
overlap, and no-thread-on-import tests pass.

### Step 5: Add Health-tab controls and snapshot history

Below Catalog Health, add:

- enable checkbox and 1–168 hour interval;
- Save Monitoring button;
- last run/state summary;
- newest-first bounded snapshot list with timestamp, trigger, state, and counts.

Reuse existing cards, tokens, toasts, loading helpers, and safe-area layout.
Do not render absent `detail` fields or invent a Fix button.

**Verify**:

```bash
rg -n "health-monitor|Health Monitoring|healthTransition" templates/index.html
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'index or health'
```

Expected: UI hooks exist and focused tests pass.

### Step 6: Complete regression coverage

Add at least these tests:

1. Health issue deep-link markup exists.
2. App/version focus uses existing editor and escapes identifiers.
3. Missing store returns disabled defaults.
4. Store writes atomically and retains only 30 snapshots.
5. Snapshot never persists `detail` or configured secret values.
6. Config endpoints require auth and enforce 1–168.
7. Disabled scheduler performs no scan.
8. First state sends no alert.
9. Healthy→degraded sends one alert; repeated degraded sends none.
10. Degraded→healthy sends one recovery alert.
11. Scan failure does not overwrite last known state.
12. Importing `app.py` starts no health thread.

Prove discrimination by temporarily sending an alert on every degraded scan;
the repeated-degraded test must fail. Restore before the final run.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
git diff --check
git status --short
```

Expected: all tests pass; only in-scope files and the index row changed.

## Done criteria

- [ ] Every actionable health issue can open the existing app/version editor.
- [ ] No new health action directly mutates catalog or storage.
- [ ] Health history is atomic, sanitized, bounded to 30, and survives restart.
- [ ] Monitoring is disabled by default and configured in-app.
- [ ] Only healthy/degraded transitions notify; first/repeated states are quiet.
- [ ] New routes require auth.
- [ ] No background thread starts when the module is imported.
- [ ] Full test suite passes with at least twelve targeted cases.

## STOP conditions

- A proposed action needs to delete, rewrite, or upload data. Split that into a
  separately reviewed repair plan; do not add it here.
- Scheduled scanning requires a fabricated request/Host header. Refactor the
  scanner input or skip host-dependent checks.
- Garage content validation requires downloading full IPAs.
- The history model would persist issue `detail`, exception text, secrets, or
  full URLs.
- Monitoring requires a second process or external queue.

## Maintenance notes

- New health issue kinds should define whether app/version navigation applies.
- Keep transition identity stable when wording in `detail` changes; history
  deliberately ignores detail text.
- If the app becomes multi-process, both scheduler ownership and file locks
  need redesign before enabling monitoring.
- Automatic repair remains rejected for this plan; prefer explicit, separately
  tested operations with dry-run semantics.
