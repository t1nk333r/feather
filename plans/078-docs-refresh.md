# Plan 078: Bring README and HANDOFF back in line with the code

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> ```
> cd <repo root>
> git rev-parse --short HEAD                        # plan written at 5e8f447
> git diff --stat 5e8f447..HEAD -- README.md plans/HANDOFF.md .env.example app.py Jenkinsfile
> ```
> `app.py`/`Jenkinsfile` changes are expected if plans 074–077 landed first;
> re-derive the numbers in Step 1 from the live code rather than from this
> plan. README/HANDOFF changes → compare excerpts; mismatch → STOP.

## Status

- **Priority**: P2 — docs that are actively wrong, one of them harmful (the old registry name)
- **Effort**: S
- **Risk**: LOW — documentation only, plus one comment line in `.env.example`
- **Depends on**: none. Best executed **after** 074–077 so it documents their outcome; if executed first, leave the HANDOFF trap plan 077 removes alone.
- **Category**: docs
- **Planned at**: commit `5e8f447`, 2026-08-28

## Why this matters

Three documents are the operator's entry points, and each now says something false:

- `README.md` opens with "It runs the Werkzeug **development server** (`app.run()` …)". It has served via Waitress since plan 052; `app.run()` appears nowhere. A reader either adds a redundant WSGI layer or — worse — discounts the two caveats beneath it that are still true.
- README's route table is 12 routes short of the 39 the app registers, says "the six mutating routes" (there are seven — `/api/delete-version`), and the "Contributing" section says "The four public routes … must stay public" while the Routes section, 100 lines earlier, correctly says the rule covers six.
- `plans/HANDOFF.md` is dated 2026-08-12 against `1b75e2c` and says: app.py is ~1,500 lines (3,975), 134 tests (260), 28 plans (78), "22 of 24 plans done", CI is GitHub Actions publishing two images under `ghcr.io/d7eeem/…` (it is Jenkins publishing three under `ghcr.io/t1nk333r/…` — `docker compose pull` against the old names pulls nothing of ours), and lists as a live trap "`/api/add-app` silently ignores icons", fixed by plan 032. Its verification runbook has nothing for the Android repo.
- `WAITRESS_THREADS` is read by `app.py` and documented nowhere.

## Current state

`README.md:11-13`:
```
- It runs the Werkzeug **development server** (`app.run()` in `app.py`), not a production
  WSGI server. Put a real reverse proxy in front of it for anything beyond a private
  network.
```
`app.py:3966-3975`:
```python
if __name__ == '__main__':
    logging.info("Starting AltStore Source Manager (Waitress)...")
    _start_auto_import_scheduler()
    # Single process, many threads: ...
    from waitress import serve
    serve(app, host='0.0.0.0', port=PORT, threads=int(os.environ.get("WAITRESS_THREADS", "8")))
```

`README.md:80-98` — the route table (12 rows) and the "first four routes" paragraph, which already adds `/fdroid/repo` and `/fdroid/qr` to the rule. `README.md:209-210` (Contributing): "The four public routes (`/source.json`, `/ipas/...`, `/icons/...`, `/qr`) must stay public."

All 39 registered routes at `5e8f447` (`command grep -n "@app.route" app.py`):
```
/                        GET   public   admin UI
/api/login               POST  public   session
/api/logout              POST  public   session
/api/session             GET   public   session
/source.json             GET   public   iOS catalog        (must stay public)
/ipas/<bundle_id>/<filename>  GET public (must stay public)
/icons/<bundle_id>/icon.<ext> GET public (must stay public)
/qr                      GET   public   (must stay public)
/sw.js                   GET   public   service worker
/api/apps                GET   public   read-only JSON
/api/app/<bundle_identifier>  GET public read-only JSON
/api/add-app             POST  auth
/api/delete-app          POST  auth
/api/update-app          POST  auth
/api/add-version         POST  auth
/api/update-version      POST  auth
/api/delete-version      POST  auth
/api/update-source       POST  auth
/api/import-release      POST  auth
/api/auto-import         GET   auth
/api/auto-import/config  POST  auth
/api/auto-import/job     POST  auth
/api/auto-import/job/delete POST auth
/api/auto-import/run     POST  auth
/api/health              GET   auth
/api/reconcile-icons     POST  auth
/api/storage-selftest    POST  auth
/api/diagnostics         GET   auth
/fdroid/repo/<path:filename>  GET public (must stay public)
/fdroid/qr               GET   public   (must stay public)
/api/android/status      GET   auth
/api/android/apps        GET   auth
/api/android/add-apk     POST  auth
/api/android/update-app  POST  auth
/api/android/delete-version POST auth
/api/android/delete-app  POST  auth
/api/android/repo-config POST  auth
/api/android/request-update POST auth
```
(Re-derive this list from the live file in Step 2 — do not paste it blindly.)

`plans/HANDOFF.md:1-35` — header, "What this is" bullets with the stale counts, "State" table listing 014/022/012, and the CI paragraph naming `ghcr.io/d7eeem/feather` / `-bot` and "Four jobs". `plans/HANDOFF.md:86` — the add-app-icons trap. `plans/HANDOFF.md:94-110` — "Verifying a deployment" (no Android lines).

`.env.example:6-10`:
```
# Optional
PORT=
SECRET_KEY=
MAX_CONTENT_LENGTH=
RATE_LIMIT=
```
(`RATE_LIMIT` is a documented no-op — leave it; its fate is a separate decision recorded in `plans/README.md`.)

Facts to use: Jenkins (`Jenkinsfile`) runs tests on Python 3.11 and 3.14, a `requests` pin-drift check, and builds/smokes/pushes `ghcr.io/t1nk333r/feather`, `feather-bot`, `feather-fdroid` on `main`. Deployment: TrueNAS + Dockge, stack `/path/to/feather`, public `https://feather.example.com`, Garage at `https://s3.example.com` (all already in HANDOFF; keep). Test count: `command grep -c "def test_" tests/*.py` summed; plan count: `ls plans/0*.md | wc -l`.

Style: README uses `##` sections, tables for routes, fenced bash blocks; HANDOFF uses bold-lead paragraphs under "Traps" and a table under "State". Match them.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Live numbers | `wc -l app.py; ls plans/0*.md \| wc -l; command grep -h -c "def test_" tests/*.py \| paste -sd+ \| bc` | current line/plan/test counts |
| Route inventory | `command grep -n "@app.route" app.py` | 39 lines at `5e8f447` (more if 074–077 added none — they don't) |
| Auth inventory | `command grep -n -B1 "@requires_auth" app.py` | which routes are gated |
| Env inventory | `command grep -ho 'environ.get("[A-Z_]*"' app.py scripts/*.py \| sort -u` vs `command grep -o '^[A-Z_]*=' .env.example \| sort -u` | the diff is what `.env.example` is missing |
| Suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | unchanged count |

## Scope

**In scope**: `README.md`, `plans/HANDOFF.md`, `.env.example` (add `WAITRESS_THREADS=` with a comment under "Optional").

**Out of scope — do NOT touch**: any code; `plans/README.md` beyond your own status row; other plan files; `compose.yml`.

## Git workflow

- Branch: `advisor/078-docs-refresh`; commits `docs(readme): Waitress, full route table, six public routes (plan 078)`, `docs(handoff): re-date against <sha>; Jenkins, three images, Android runbook (plan 078)`, `docs(env): document WAITRESS_THREADS (plan 078)`.
- Do not push.

## Steps

### Step 1: README "Status and caveats"

Replace the first bullet with:
```
- It is served by **Waitress** (single process, `WAITRESS_THREADS` threads, default 8) from
  `app.py`'s `__main__`. Keep TLS termination and any rate limiting at a reverse proxy in
  front of it.
```
Leave the rate-limiting and single-process bullets unchanged (both verified still true).

**Verify**: `command grep -n "development server\|app.run()" README.md` → none.

### Step 2: README route table — complete and consistent

Regenerate the table from the live `@app.route`/`@requires_auth` inventory. Group rows: public catalog routes (the six that must stay public, marked), public app-shell routes (`/`, `/sw.js`, session, read-only JSON), iOS admin (seven mutating + import-release + auto-import ×5 + health/reconcile/selftest/diagnostics), Android admin (eight). Fix "the six mutating routes" → seven. Change the Contributing bullet to name **six** public routes, listing them, and cross-reference the Routes section.

Add a regression test? **No** — code is out of scope. Instead put the exact grep in a comment line under the table: `<!-- regenerate: command grep -n "@app.route" app.py -->`.

**Verify**: every path printed by `command grep -o "@app.route('[^']*'" app.py` appears in `README.md` (`for p in $(...); do command grep -q -- "$p" README.md || echo MISSING $p; done` prints nothing); `command grep -c "four public routes" README.md` → `0`.

### Step 3: `.env.example`

Under `# Optional`, after `PORT=`, add:
```
# Waitress worker threads for the single app process (default 8). Raise only
# if long URL-imports are starving /source.json; never run multiple processes.
WAITRESS_THREADS=
```
Run the env-inventory diff from the commands table; if it reports other variables read by code and absent from `.env.example`, list them in NOTES (do not add them — each belongs to its plan's block).

**Verify**: `command grep -n "^WAITRESS_THREADS=" .env.example` → one hit.

### Step 4: HANDOFF rewrite (targeted, not wholesale)

Keep the file's structure and every trap that is still true (`command:` ignored by the Bot API image; uid 101; Dockge and `--profile`; Dockge/repo compose divergence; wheel pins for cp311+cp314; `getFile` synchronous; filenames lie; per-framework `Info.plist`; don't extract IPA icons; `.dockerignore` deny-by-default; fresh bind mount root-owned). Change:

1. Header: `Written 2026-08-12 against 1b75e2c` → `Written 2026-08-12 against 1b75e2c; refreshed <today> against <HEAD>`.
2. "What this is": iOS **and Android** (one sentence on the F-Droid repo + sidecar); bullets with live counts; add `scripts/release_source_ingest.py`, `scripts/fdroid_index_loop.sh`, `Dockerfile.bot`, `Dockerfile.fdroid`, `Jenkinsfile` to the file list.
3. "State": replace the three-row table with "N of M plans done; open: <list TODO rows from plans/README.md>" — derive from the index at execution time.
4. CI paragraph: Jenkins, tests on 3.11 + 3.14, pin-drift check, three images under `ghcr.io/t1nk333r/`, pushed only after smoke if plan 077 has landed (check the Jenkinsfile; describe what is true).
5. Deployment table: add the `feather-fdroid-index` row (profile `android`, root by necessity, mounts only `./data/fdroid`).
6. Operator tasks: remove ones now done (check each against the code/index — e.g. the source-icon one-time update from plan 028 if the index says it's done); add **back up `data/fdroid/keystore.p12` + `FDROID_KEYSTORE_PASSWORD`**, set `COMPOSE_PROFILES=android`, `chown -R 999:999 data/fdroid`, make the three GHCR packages public after the first `t1nk333r` publish.
7. Traps: delete the `/api/add-app` icons trap (fixed, plan 032) and — only if plan 077 has landed — the push-before-smoke trap; add: "The fdroid sidecar must run as root; `user:` on it breaks `fdroid`" and "`./data/fdroid` is a separate bind mount: `os.replace` across it raises `EXDEV` (handled in `add_apk`)".
8. "Verifying a deployment": add
   ```bash
   # the Android repo
   curl -sI https://feather.example.com/fdroid/repo/index-v1.jar | head -1     # 200
   curl -s  https://feather.example.com/fdroid/repo/index-v1.json | python3 -c 'import json,sys; print(list(json.load(sys.stdin)["packages"]))'
   docker logs --tail 5 feather-fdroid-index                                    # "INFO: Finished"
   cat data/fdroid/last-update.json                                             # "ok": true
   ```

**Verify**: `command grep -n "d7eeem\|GitHub Actions\|Four jobs\|silently ignores icons\|~1,500 lines\|134 tests\|28 numbered" plans/HANDOFF.md` → none; `command grep -c "fdroid" plans/HANDOFF.md` → ≥5.

## Test plan

No code changes; the suite must remain at its pre-plan count. The Step 2 "every route appears" loop is the machine check for the table.

## Done criteria

- [ ] README: no "development server"/`app.run()`; route table covers every `@app.route`; "six public routes" everywhere, "seven mutating"
- [ ] `.env.example` has `WAITRESS_THREADS=`
- [ ] HANDOFF: refreshed header, live counts, Jenkins + three `t1nk333r` images, Android rows/traps/runbook, no fixed traps listed as live
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → unchanged count
- [ ] `git status --short` shows only the three in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

- A fact you need (e.g. whether push-after-smoke landed) is ambiguous from the repo — check `plans/README.md`'s status column; if it's still unclear, describe the ambiguity in NOTES and document what the *current* `Jenkinsfile` does.
- You are tempted to fix code you notice while documenting (e.g. `RATE_LIMIT`) — don't; note it.

## Maintenance notes

- README's route table now carries its regeneration command; any plan adding a route must update the table (add that to the plan template's done criteria if this drifts again).
- HANDOFF should be re-dated whenever counts or traps change; a quarterly re-read is cheaper than the debugging it prevents.
- Two documentation debts deliberately left: `RATE_LIMIT`/`Flask-Limiter` (decision pending — see index) and a `CLAUDE.md` (finding #9 in the 2026-08-28 audit, unselected).
