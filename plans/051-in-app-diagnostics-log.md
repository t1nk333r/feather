# Plan 051: In-app diagnostics — a recent-errors ring buffer surfaced in the admin UI

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat c62e7da..HEAD -- app.py templates/index.html tests/test_routes.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 222 passed
> ```

## Status

- **Priority**: P2 (cuts the SSH-+-`docker logs` loop on every future issue)
- **Effort**: M
- **Risk**: LOW (an in-memory log handler + a read-only auth-gated route)
- **Depends on**: none
- **Category**: direction / DX / observability
- **Planned at**: commit `c62e7da`, 2026-08-19

## Why this matters

Every server-side problem this deployment hit (the reconcile 500, the Garage 403)
required the operator to SSH in and run `docker logs`, because there is **no
in-app way to see recent errors** — the admin UI shows nothing. This plan adds a
bounded in-memory **ring buffer of recent WARNING/ERROR log records** and exposes
it (auth-gated, secrets redacted) on the Health tab, so the next incident is
visible in the browser instead of over SSH.

## Current state — `app.py`

- Logging is configured once at import: `logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")` (app.py:34). Everything logs via the root logger (`logging.error(...)`, `logging.warning(...)`). There is **no** custom handler and **no** diagnostics route (`grep -n "/api/diagnostics" app.py` = empty).
- The app is a single process serving multiple threads (request threads + the plan-048 auto-import scheduler thread), so the buffer must be thread-safe. `collections.deque(maxlen=N)` `.append()` is atomic in CPython — safe for this.
- Secret env values that must never be echoed into the buffer: `ADMIN_PASSWORD`, `SECRET_KEY`, and the Garage secret (`GARAGE_S3_SECRET_ACCESS_KEY`). Reference them by name; **never** put their values in any file (Hard Rule 4). Redact them from messages if they ever appear.
- The Health tab (`templates/index.html:902`, `id="health"`) is where the UI goes;
  reuse `showToast` / `setButtonLoading` and iOS tokens (plan 043), no emoji.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `222 passed` before |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| New tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k diagnostics` | pass |
| Full | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `222 + N passed` |

## Scope

**In scope:**
- `app.py` — a `logging.Handler` subclass keeping a bounded ring buffer of
  WARNING+ records (installed on the root logger right after `basicConfig`), a
  small redactor, and a `GET /api/diagnostics` route (`@requires_auth`).
- `templates/index.html` — a "Recent errors" section on the Health tab.
- `tests/test_routes.py` — tests.

**Out of scope:**
- Changing the logging *format* or level for the console handler (leave `basicConfig` as-is; just ADD the ring handler).
- Persisting logs to disk / external log shipping (in-memory only; it resets on restart — that's fine and intended).
- Capturing INFO/DEBUG (WARNING+ only — keep the buffer signal-dense and small).

## Steps

### Step 1: Baseline → `222 passed`. If not, STOP.

### Step 2: Ring-buffer handler + redactor (module scope, right after `basicConfig`)
```python
import collections

_DIAG_BUFFER = collections.deque(maxlen=200)

def _redact_secret(text):
    """Never echo secret env VALUES into the diagnostics buffer (Hard Rule 4)."""
    s = text
    for name in ("ADMIN_PASSWORD", "SECRET_KEY", "GARAGE_S3_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "GITLAB_TOKEN"):
        val = os.environ.get(name)
        if val and len(val) >= 4 and val in s:
            s = s.replace(val, "***REDACTED***")
    return s

class _RingBufferLogHandler(logging.Handler):
    def emit(self, record):
        try:
            _DIAG_BUFFER.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "message": _redact_secret(self.format(record)),
            })
        except Exception:
            pass  # a logging handler must never raise

_ring_handler = _RingBufferLogHandler()
_ring_handler.setLevel(logging.WARNING)
_ring_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_ring_handler)
```
(Installing at module scope is fine — it's a passive handler, no thread; the test
suite gets it too, which is what test 1 exercises. `datetime`/`timezone` are already imported.)

### Step 3: `GET /api/diagnostics`
```python
@app.route('/api/diagnostics', methods=['GET'])
@requires_auth
def diagnostics():
    # newest first, capped
    return jsonify({"entries": list(_DIAG_BUFFER)[::-1]})
```

### Step 4: Health-tab UI
Add a "Recent errors" section to the Health tab with a Refresh button →
`GET /api/diagnostics`, rendering each entry as `ts · LEVEL · message` (monospace,
`escapeHtml` the message). Show "No recent warnings or errors." when empty. Plain
text, no emoji.

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0.

### Step 5: Tests (`tests/test_routes.py`, `authed_client`)
1. `test_diagnostics_captures_recent_error` — call `app_module.logging.error("plan051-probe-marker")` (or `app_module.logging.getLogger().error(...)`), then `GET /api/diagnostics` → an entry with `level == "ERROR"` and the marker in `message` is present.
2. `test_diagnostics_ignores_info` — log at INFO → it does NOT appear (WARNING+ only).
3. `test_diagnostics_redacts_secret` — with `ADMIN_PASSWORD` set to a known test value, `logging.error("leak " + <that value>)` → the returned entry shows `***REDACTED***`, not the value. (Do not hardcode a real secret; use the test's configured `ADMIN_PASSWORD`.)
4. `test_diagnostics_requires_auth` — unauthenticated → 401.

**Prove discrimination**: temporarily set the handler level to `logging.ERROR` only,
or remove the redactor call → the relevant test fails. Restore.

**Verify**: focused `-k diagnostics`; then full suite.

## Done criteria
- [ ] WARNING+ log records land in a bounded (maxlen 200) in-memory buffer; `GET /api/diagnostics` (`@requires_auth`) returns them newest-first.
- [ ] Secret env values are redacted from stored messages; INFO/DEBUG are excluded.
- [ ] The Health tab shows recent errors with a refresh.
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (222 + 4).
- [ ] `git status --short` shows only `app.py`, `templates/index.html`, `tests/test_routes.py`.

## STOP conditions
- Adding the handler at module scope makes existing tests flaky (e.g. cross-test buffer bleed causes an assertion elsewhere) — if so, clear/adjust in a fixture rather than removing the feature, and report.
- The redactor would need a real secret value written anywhere to test — it must not; use the env-configured test password only.

## Maintenance notes
- The buffer is per-process and resets on restart — intended; it's a live-tail, not
  an audit log. If durable logs are ever needed, that's a separate concern (ship to
  a file/loki), not this.
- If a new secret env var is added, add its name to `_redact_secret`.
- Reviewer: confirm WARNING+ only, secrets are redacted, the handler never raises,
  and the route is auth-gated (the buffer can contain internal paths/hostnames).
