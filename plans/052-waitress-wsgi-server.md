# Plan 052: Serve behind Waitress (single-process WSGI) instead of the Flask dev server

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat c62e7da..HEAD -- app.py requirements.txt Dockerfile tests/
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 222 passed
> ```
> If the `__main__` block changed since `c62e7da`, compare the "Current state"
> excerpt against the live code first; on a mismatch, STOP.

## Status

- **Priority**: P2 (production-readiness — the app currently serves on a dev server)
- **Effort**: S–M
- **Risk**: LOW–MED (changes how the server is started; the app object is unchanged)
- **Depends on**: none (but interacts with plan 048's scheduler — see below)
- **Category**: direction / production hardening
- **Planned at**: commit `c62e7da`, 2026-08-19

## Why this matters

Production serves on **Flask's built-in development server**
(`app.run(host='0.0.0.0', port=PORT, threaded=True)`, app.py:2885; Docker
`CMD ["python", "app.py"]`) — which prints a "do not use in a production
deployment" warning and isn't built for it. **Waitress** is a pure-Python,
production-grade WSGI server (no C deps, trivial to add) that runs as a **single
process with a thread pool** — which is exactly what this app needs, because the
`SourceManager` in-process lock **and** the plan-048 auto-import **scheduler
thread** both assume one process. Switching to Waitress hardens serving *without*
breaking those single-process invariants.

## Critical invariant (read before touching anything)

This app is **single-process by design**. `SourceManager`'s `threading.Lock`
serializes catalog writes within one process (documented at app.py:611–613), and
the auto-import scheduler is one daemon thread started once from `__main__`. Waitress
with the default **one process, many threads** preserves both. **Do NOT** introduce
a multi-*process* server (gunicorn `-w >1`, uWSGI multiple workers, multiple
Waitress processes) — that would silently break the lock (each process has its own)
and spawn N schedulers. If someone later needs horizontal scaling, the lock and
scheduler must be externalized first; that is explicitly **out of scope** here.

## Current state — `app.py` (bottom of file)

```python
if __name__ == '__main__':
    logging.info("Starting AltStore Source Manager...")
    _start_auto_import_scheduler()
    app.run(host='0.0.0.0', port=PORT, debug=False)
```
- `app` is the module-level Flask instance (a WSGI callable). `PORT` is
  `int(os.environ.get("PORT", "5000"))`.
- `Dockerfile`: `CMD ["python", "app.py"]` (runs this `__main__`), and a healthcheck
  `curl -f http://localhost:5000/source.json`. `Dockerfile.bot` is the separate bot
  image — leave it alone.
- `requirements.txt` uses **exact pins** (e.g. `Flask==2.3.3`).
- The test suite imports `app` (it never runs `__main__`), so the serving change
  does not affect tests.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `222 passed` before AND after |
| Waitress installs | `.venv/bin/pip install waitress` (in the executor's worktree venv only, if needed to verify import) | resolves |
| Waitress importable | `.venv/bin/python -c "import waitress; from waitress import serve; print(waitress.__version__)"` | prints a version |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| App is a WSGI app | `.venv/bin/python -c "import app; assert callable(app.app)"` (with `DATA_DIR`+`ADMIN_PASSWORD` set) | no error |
| Full | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `222 + N passed` |

## Scope

**In scope:**
- `requirements.txt` — add a pinned `waitress`.
- `app.py` — the `__main__` block only: start the scheduler (unchanged), then serve
  via Waitress.
- `tests/test_routes.py` (or a small new `tests/test_server.py`) — a minimal test
  that `waitress.serve` is what `__main__` would call and the app is a WSGI callable.

**Out of scope:**
- `Dockerfile` CMD — it stays `python app.py` (which runs the new `__main__`); no
  change needed. Do NOT switch the CMD to a `waitress-serve` console script (that
  would bypass `_start_auto_import_scheduler()`).
- Any multi-process / multi-worker configuration (see the invariant above).
- Gunicorn/uWSGI, async, or ASGI — not this plan.

## Steps

### Step 1: Baseline → `222 passed`. If not, STOP.

### Step 2: Pin Waitress in `requirements.txt`
Add `waitress==<version>` matching the repo's exact-pin style. Determine the version
by what installs cleanly in the venv (`.venv/bin/pip install waitress` then
`pip show waitress`), and pin that exact version. (A current stable is the 3.x
line.)

### Step 3: Serve via Waitress from `__main__`
Replace ONLY the serve call; keep the scheduler start:
```python
if __name__ == '__main__':
    logging.info("Starting AltStore Source Manager (Waitress)...")
    _start_auto_import_scheduler()
    from waitress import serve
    serve(app, host='0.0.0.0', port=PORT, threads=int(os.environ.get("WAITRESS_THREADS", "8")))
```
- Single process, `threads` worker threads (default 8, overridable). This preserves
  the in-process lock + single scheduler.
- Do the `from waitress import serve` inside `__main__` (not at module top) so the
  test suite — which imports `app` but not `__main__` — doesn't hard-require waitress
  to be installed to run, and module import stays lean. (waitress is still a real
  runtime dependency via requirements.txt for the container.)

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0; the WSGI-callable check.

### Step 4: Test
Add a minimal test (no server actually bound):
1. `test_app_is_wsgi_callable` — `import app; assert callable(app.app)` (Flask app is a WSGI callable). Set `DATA_DIR`/`ADMIN_PASSWORD` as other tests do.
2. `test_waitress_importable` — `import waitress; from waitress import serve` succeeds (guards the dependency being present). If you'd rather not couple a unit test to the dependency, at minimum assert the `__main__` guard exists and references `serve` via reading the source — but the import test is simplest and real.

(The existing full suite already proves the app serves correctly via Flask's test
client; Waitress only changes the *entrypoint*, which the suite doesn't exercise —
so the real end-to-end proof is the CI Docker smoke test, see Maintenance.)

**Verify**: full suite still `222 + N passed`.

## Done criteria
- [ ] `requirements.txt` pins `waitress`; `__main__` starts the scheduler then serves via `waitress.serve(app, ..., threads=N)` — **single process**.
- [ ] `app.run(...)` is gone from `__main__`; the Flask dev-server warning no longer fires in production.
- [ ] `Dockerfile` CMD unchanged (`python app.py`), healthcheck unchanged.
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (222 + tests).
- [ ] `git status --short` shows only `requirements.txt`, `app.py`, and the test file.

## STOP conditions
- Waitress won't install/resolve in the venv — report (do not substitute gunicorn, which would tempt a multi-process config that breaks the invariant).
- You find the CMD or an entrypoint that would start the app *without* running
  `_start_auto_import_scheduler()` — STOP; the scheduler must still start under
  Waitress. The chosen approach (serve from `__main__`) keeps it; don't switch to a
  `waitress-serve` CLI that skips it.

## Maintenance notes
- **The single-process invariant is load-bearing.** Anyone who later reaches for
  `-w >1` / multiple processes must first externalize the `SourceManager` lock
  (e.g. a file lock or moving state to SQLite) and make exactly one process own the
  scheduler. Leave a comment to that effect near the serve call.
- CI already runs a Docker smoke test (`docker exec ... curl .../source.json`); after
  this lands, that smoke test is the real end-to-end proof the container serves via
  Waitress. Watch that it stays green on the first post-merge build.
- Reviewer: confirm it's single-process, the scheduler still starts, the dev-server
  call is gone, and the Docker CMD/healthcheck are untouched.
