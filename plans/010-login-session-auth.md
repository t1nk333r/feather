# Plan 010: Gate the mutating routes behind a login form

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> grep -c "ADMIN_PASSWORD" app.py          # expect 1 (Plan 001 read it)
> test -f templates/index.html && echo ok  # expect ok (Plan 009 landed)
> .venv/bin/python -m pytest tests/ -q     # expect green
> ```
> If the template is still embedded in `app.py`, **STOP** — complete Plan 009
> first. Editing a 1,493-line Python string literal by hand is how this plan
> goes wrong.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED — this is the only plan that can break AltStore clients if scoped wrongly
- **Depends on**: `plans/001-configurable-paths-and-config.md`, `plans/005-smoke-test-suite.md`, `plans/009-extract-html-template.md`
- **Category**: security
- **Planned at**: 2026-08-10. **Refreshed 2026-08-11** against `bac2d55`; `app.py` md5 `c93c7c8d725a3574364cc74cc6c14a4f`, now only 1491 lines because Plan 009 moved the template to `templates/index.html` (1493 lines). Every frontend line reference in this plan therefore now points at `templates/index.html`, not `app.py`.


## REFRESHED REFERENCES — read this before the steps below (2026-08-11)

Plans 001–009 and 011 have all landed. Verified against `bac2d55`:

**The six mutating routes to gate** (`app.py`):

| Route | Line |
|---|---|
| `/api/add-app` | 1266 |
| `/api/delete-app` | 1305 |
| `/api/update-app` | 1336 |
| `/api/add-version` | 1380 |
| `/api/update-version` | 1422 |
| `/api/update-source` | 1466 |

**The four routes that MUST stay unauthenticated** — gating any one breaks every subscribed iOS device:

| Route | Line |
|---|---|
| `/source.json` | 1166 |
| `/ipas/<bundle_id>/<filename>` | 1174 |
| `/icons/<bundle_id>/icon.<ext>` | 1203 |
| `/qr` | 1234 |

`GET /`, `/api/apps` and `/api/app/<id>` are **also read-only** — leave them public too. Only the six POST routes get the decorator.

**Config already present** (Plan 001), `app.py:33-56`:

```python
SECRET_KEY = os.environ.get("SECRET_KEY")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
...
if SECRET_KEY:
    app.secret_key = SECRET_KEY
else:
    app.secret_key = os.urandom(32)
    logging.warning("SECRET_KEY not set — using a random key; sessions will not survive restart")
```

**Frontend work is now in `templates/index.html`, not `app.py`.** Plan 009 extracted it. Confirmed there:
- `editAppModal` pattern to copy: `templates/index.html:594` (`<div id="editAppModal" class="modal">`)
- `.modal` / `.modal-content` / `.modal-header` CSS all exist already
- **13** `fetch(` call sites, all same-origin — **none needs credential changes**, the session cookie is sent automatically

**The bundled escapeHtml fix** moved too. It is now `templates/index.html:1006`:

```
<strong>Latest:</strong> ${app.versions?.[0]?.version || 'N/A'} |
```

Every sibling field on lines 1001–1004 *is* escaped (`escapeHtml(app.name)`, `escapeHtml(app.bundleIdentifier)`, …), so this is an omission, not a decision. Wrap it: `${escapeHtml(app.versions?.[0]?.version || 'N/A')}`. Version strings are attacker-supplied and stored.

**Healthcheck reconciliation** (Step 5). `Dockerfile:31-32` still embeds a credential in a process argument:

```
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f -u "admin:${ADMIN_PASSWORD}" http://localhost:5000/source.json || exit 1
```

`compose.yml:22-28` already defines a credential-free healthcheck against the same URL and **silently overrides the Dockerfile one at runtime**. Fix the Dockerfile to match compose (drop `-u`), so the two agree and no credential appears in `docker inspect` or the process table.

## Worktree caveat

`data/` and `.env` are gitignored and absent from an executor worktree, so `docker compose` cannot start. Do not fabricate a `.env`. Verify with the pytest suite plus a `docker build` + one-off container (`-e DATA_DIR=/tmp/fd`), which needs neither. The live cutover is an operator task.

**`ADMIN_PASSWORD` is not set in `.env` today** — the key exists in `.env.example` but the operator has not confirmed a value. Your code must refuse to start when `STORAGE_BACKEND`-style required config is missing... but for auth specifically: **refuse to boot if `ADMIN_PASSWORD` is unset**, per the plan. Say clearly in your report that this makes setting `ADMIN_PASSWORD` a hard prerequisite for the next deploy.

## Why this matters

Six state-changing routes have no authentication of any kind. A grep across the whole file for `os.environ`, `before_request`, `Authorization`, `session`, `Limiter`, or `app.config` returns **zero hits** before Plan 001. There is no decorator, no hook, no check inside any handler.

Anyone who can reach the host can add apps, delete any app (which also deletes its `.ipa` files from disk), and rewrite any `downloadURL`. Since AltStore/Feather clients install whatever the catalog's `downloadURL` points at, control of that field is control of what gets installed on every subscribed device.

The deployment is on a private network, so this is defence-in-depth rather than an active emergency. But two things make it worth doing now: `Dockerfile:31` already health-checks with `curl -u "admin:${ADMIN_PASSWORD}"`, **claiming an authentication scheme the app never implemented** — configuration that lies about the security posture is worse than no configuration — and `.env` has declared `ADMIN_PASSWORD` all along.

**Chosen design** (decided by the operator): a single admin password compared with `hmac.compare_digest`, backed by a Flask session cookie, presented as a **login form** rather than the browser's native Basic-auth prompt.

## Current state

### The routes and their exposure

| Route | Line | Method | Must be gated? |
|---|---|---|---|
| `/` | 2218 | GET | No — it must render so the login form can be shown |
| `/source.json` | 2222 | GET | **NO — never gate this** |
| `/ipas/<bundle_id>/<filename>` | 2230 | GET | **NO — never gate this** |
| `/icons/<bundle_id>/icon.<ext>` | 2248 | GET | **NO — never gate this** |
| `/qr` | 2279 | GET | **NO — never gate this** |
| `/api/apps` | 2300 | GET | Optional — read-only; leaving it open is fine |
| `/api/app/<bundle_identifier>` | 2365 | GET | Optional — read-only |
| `/api/add-app` | 2311 | POST | **YES** |
| `/api/delete-app` | 2345 | POST | **YES** |
| `/api/update-app` | 2376 | POST | **YES** |
| `/api/add-version` | 2420 | POST | **YES** |
| `/api/update-version` | 2462 | POST | **YES** |
| `/api/update-source` | 2506 | POST | **YES** |

(Line numbers are pre-Plan-009; after the template extraction they shift down by ~1,495.)

**The four "never gate" routes are the product.** iOS devices fetch them with no credentials and no browser. Gating any one of them breaks every subscribed device, silently, until someone notices an app won't install.

### What Plan 001 provides

```python
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
SECRET_KEY = os.environ.get("SECRET_KEY")
...
if SECRET_KEY:
    app.secret_key = SECRET_KEY
else:
    app.secret_key = os.urandom(32)
    logging.warning("SECRET_KEY not set — using a random key; sessions will not survive restart")
```

Both are read; neither is used yet. This plan consumes them.

### Frontend structure (`templates/index.html` after Plan 009)

An existing modal to pattern-match — original `app.py:1315-1320`:

```html
<div id="editAppModal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            <h2>✏️ Edit App</h2>
            <button class="close" onclick="closeEditModal()">&times;</button>
        </div>
        <form id="editAppForm">
```

The `.modal`, `.modal-content`, `.modal-header` CSS classes already exist (original lines 941, 953, 964). **Reuse them** — do not write new modal styling.

**The 13 `fetch()` call sites** (original line numbers): 1645, 1672, 1799, 1947, 1959, 1965, 2027, 2077, 2092, 2114, 2150, 2170, 2189.

**Critically: none of these need credential changes.** All are same-origin, so the browser attaches the session cookie automatically. This is the main practical advantage of the session design over HTTP Basic — you do not have to touch 13 call sites. What you *do* need is a shared way to react to a 401.

The async functions wrapping those calls: `loadApps()` (1665), `editApp()` (1797), `updateVersion()` (1908), `addNewVersion()` (1992), `deleteApp()` (2146), `loadSourceInfo()` (2168).

### The escaping bug to fix alongside

Original `app.py:1722-1727`, inside the app-list renderer that assigns to `innerHTML`:

```javascript
<img src="${escapeHtml(iconUrl || svgFallback)}" alt="${escapeHtml(app.name)}" class="app-icon" onerror="...">
<div class="app-info">
    <h3>${escapeHtml(app.name)}</h3>
    <p><strong>Bundle ID:</strong> ${escapeHtml(app.bundleIdentifier)}</p>
    <div class="version-info">
        <strong>Latest:</strong> ${app.versions?.[0]?.version || 'N/A'} |
```

Every sibling field is wrapped in `escapeHtml`; the version string is not. It is an omission, not a design choice. `escapeHtml` is defined at original `app.py:1787-1795`. Version strings are stored, attacker-supplied values — once a login session exists, this becomes the path back into an authenticated session, which is why it belongs in this plan.

**Repo conventions**: module-level config constants; `(bool, message)` tuples from `SourceManager`; routes return `jsonify(...)` with an explicit status code; frontend is vanilla JS with `async`/`await` and `fetch`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `.venv/bin/python -m pytest tests/ -q` | all pass |
| Syntax | `python3 -m py_compile app.py` | exit 0 |
| Unauthenticated read | `curl -s -o /dev/null -w "%{http_code}" http://localhost:7000/source.json` | `200` |
| Unauthenticated write | `curl -s -o /dev/null -w "%{http_code}" -X POST -H 'Content-Type: application/json' -d '{"bundleIdentifier":"x"}' http://localhost:7000/api/delete-app` | `401` |

## Scope

**In scope**:
- `app.py` — imports (`hmac`, `session` from flask), a `requires_auth` decorator, `POST /api/login`, `POST /api/logout`, `GET /api/session`, the decorator applied to six routes, session cookie config
- `templates/index.html` — login modal, a 401 handler, the `escapeHtml` fix
- `Dockerfile` — fix the healthcheck at line 31
- `compose.yml` — reconcile the duplicate healthcheck
- `tests/test_routes.py`

**Out of scope** (do NOT touch):
- **Gating `/source.json`, `/ipas/*`, `/icons/*`, or `/qr`.** Non-negotiable. Breaking these breaks every user's device.
- Multi-user accounts, password hashing at rest, registration, password reset. One shared admin password is the chosen design; `ADMIN_PASSWORD` comes from the environment and is compared directly.
- Rate limiting / `Flask-Limiter`. Declared in `requirements.txt` and never imported — a deliberately deferred finding. Do not wire it here.
- HTTPS / TLS termination / `SESSION_COOKIE_SECURE=True`. The deployment is plain HTTP on a private network; setting `Secure` would break login entirely. See Step 4.
- `SourceManager` internals.

## Git workflow

- Branch: `advisor/010-login-auth`
- Suggested commits: (1) backend auth, (2) frontend login UI, (3) escapeHtml fix, (4) Dockerfile/compose healthcheck.

## Steps

### Step 1: Fail fast when `ADMIN_PASSWORD` is unset

Near the config block, after `ADMIN_PASSWORD` is read, refuse to start without it:

```python
if not ADMIN_PASSWORD:
    raise RuntimeError(
        "ADMIN_PASSWORD is not set. Refusing to start with unauthenticated "
        "admin routes. Set it in .env (compose.yml loads it via env_file)."
    )
```

Defaulting to open is how a redeployment silently loses its protection. Fail loudly instead.

**Note for tests**: the Plan 005 fixture must now set `ADMIN_PASSWORD` in `os.environ` before importing `app`. Update it in Step 7.

**Verify**: `python3 -m py_compile app.py` → exit 0

### Step 2: Add the auth decorator and session endpoints

Add `import hmac` and add `session` to the flask import on line 1.

The decorator:

```python
from functools import wraps

def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('authed'):
            return jsonify({"success": False, "error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return wrapper
```

`@wraps` is required — without it every decorated view shares the name `wrapper` and Flask raises a duplicate-endpoint error at import.

The endpoints:

```python
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    supplied = data.get('password', '')
    if hmac.compare_digest(supplied, ADMIN_PASSWORD):
        session['authed'] = True
        session.permanent = True
        return jsonify({"success": True})
    logging.warning("Failed login attempt from %s", request.remote_addr)
    return jsonify({"success": False, "error": "Invalid password"}), 401


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"success": True})


@app.route('/api/session')
def session_status():
    return jsonify({"authed": bool(session.get('authed'))})
```

`hmac.compare_digest` is constant-time, which is why it is used instead of `==`. Both arguments must be `str` (or both `bytes`) or it raises `TypeError` — `data.get('password', '')` guarantees a string.

**Never log the supplied or expected password.** The warning above logs only the source address.

`/api/session` exists so the frontend can decide whether to show the login modal on page load without attempting a write.

**Verify**: `grep -c "compare_digest" app.py` → `1`

### Step 3: Apply the decorator to exactly six routes

Add `@requires_auth` **below** `@app.route(...)` and above the function on: `add_app`, `delete_app`, `update_app`, `add_version`, `update_version`, `update_source`.

Order matters — `@app.route` must be the outermost decorator.

```python
@app.route('/api/add-app', methods=['POST'])
@requires_auth
def add_app():
```

**Verify**:
```
grep -c "@requires_auth" app.py
```
→ `6`

```
grep -B2 "@requires_auth" app.py | grep -c "source.json\|/ipas/\|/icons/\|/qr"
```
→ `0` — proves no public route was accidentally gated.

### Step 4: Configure the session cookie

Near the other `app.config` assignments:

```python
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# SESSION_COOKIE_SECURE is intentionally left False: this deployment serves
# plain HTTP on a private network, and setting it would prevent the cookie
# from ever being stored. Set it to True as soon as TLS terminates in front.
app.config["SESSION_COOKIE_SECURE"] = False
```

`SameSite=Lax` is the CSRF defence here: browsers do not attach the cookie to cross-site POST requests, so a malicious page cannot drive the admin API using the admin's session. That is why this design does not need per-request CSRF tokens.

**Verify**: `grep -c "SESSION_COOKIE_SAMESITE" app.py` → `1`

### Step 5: Add the login modal to `templates/index.html`

Follow the existing `editAppModal` structure and reuse the `.modal` / `.modal-content` / `.modal-header` classes. Add near the other modals:

```html
<div id="loginModal" class="modal">
    <div class="modal-content">
        <div class="modal-header">
            <h2>🔒 Sign in</h2>
        </div>
        <form id="loginForm">
            <div class="form-group">
                <label for="loginPassword">Admin password</label>
                <input type="password" id="loginPassword" name="password" autocomplete="current-password" required>
            </div>
            <div id="loginError" style="display:none; color:#d02828;"></div>
            <button type="submit">Sign in</button>
        </form>
    </div>
</div>
```

Deliberately **no close button** — unlike `editAppModal`, dismissing this one leaves the user in a state where every action fails.

JavaScript to add:

1. `showLogin()` / `hideLogin()` toggling the modal's display, matching how `closeEditModal()` (original line 1900) does it.
2. A submit handler on `#loginForm` that POSTs `{password}` as JSON to `/api/login`; on 200 hide the modal and call `loadApps()`; on 401 show "Invalid password" in `#loginError`. **Never log the password to the console.**
3. A shared 401 handler. The cleanest approach that avoids editing all 13 call sites: wrap `fetch` once, near the top of the `<script>` block, before any other function uses it:

```javascript
const _origFetch = window.fetch;
window.fetch = async function (...args) {
    const response = await _origFetch(...args);
    if (response.status === 401) {
        showLogin();
    }
    return response;
};
```

This is a deliberate trade — one interception point instead of 13 edits, in a codebase with no module system. If you prefer explicit handling, editing each `async function` is also acceptable; say which you chose in your report.

4. On page load, call `/api/session` and show the login modal if `authed` is false.
5. A "Sign out" control that POSTs to `/api/logout` and then calls `showLogin()`.

**Verify**: `grep -c "loginModal" templates/index.html` → at least `2` (the markup and a JS reference)

### Step 6: Fix the unescaped version string

In `templates/index.html`, find the line rendering the latest version (original `app.py:1727`):

```javascript
<strong>Latest:</strong> ${app.versions?.[0]?.version || 'N/A'} |
```

Wrap it:

```javascript
<strong>Latest:</strong> ${escapeHtml(app.versions?.[0]?.version || 'N/A')} |
```

`escapeHtml` handles a falsy input by returning `''`, and `|| 'N/A'` runs first, so the guard is safe.

**Verify**: `grep -c 'versions?.\[0\]?.version || .N/A.' templates/index.html` → the only remaining occurrence is inside an `escapeHtml(` call. Confirm by eye.

### Step 7: Update the test fixture and add auth tests

The Plan 005 `client` fixture must set `ADMIN_PASSWORD` before importing `app` (Step 1 now makes import fail without it):

```python
os.environ["DATA_DIR"] = str(tmp_path)
os.environ["ADMIN_PASSWORD"] = "test-password-not-a-real-secret"
os.environ["SECRET_KEY"] = "test-secret-key"
```

Add a second fixture `authed_client` that performs the login POST and returns the same client with a session cookie set.

**Every existing mutating-route test from Plans 005–008 must switch to `authed_client`**, or it will start failing with 401. Expect to touch most of the suite; that is normal and is the point.

**Verify**: `.venv/bin/python -m pytest tests/ -q` → all pass

### Step 8: Fix the healthchecks

`Dockerfile:29-31` currently embeds a credential in a process argument for auth that never existed:

```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f -u "admin:${ADMIN_PASSWORD}" http://localhost:5000/source.json || exit 1
```

`ADMIN_PASSWORD` is not an `ARG` or `ENV` in the Dockerfile, so it expands empty anyway. Replace it with an unauthenticated check against a route that stays public:

```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/source.json || exit 1
```

`compose.yml:20-25` defines a second healthcheck that silently overrides the Dockerfile's. Now that they agree, delete the compose one so there is a single definition.

**Verify**: `grep -c "ADMIN_PASSWORD" Dockerfile` → `0`

### Step 9: End-to-end verification

```
docker compose up --build -d
sleep 15
```

**The public routes must still work with no credentials** — this is the check that matters most:
```
for p in /source.json /qr; do
  echo -n "$p: "; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:7000$p
done
```
→ both `200`

**The mutating routes must reject anonymous callers**:
```
curl -s -o /dev/null -w "%{http_code}\n" -X POST \
  -H 'Content-Type: application/json' -d '{"bundleIdentifier":"x"}' \
  http://localhost:7000/api/delete-app
```
→ `401`

**And accept an authenticated one** — replace `<pw>` with the value from `.env` (do not paste it into any file or commit message):
```
curl -s -c /tmp/cj.txt -X POST -H 'Content-Type: application/json' \
  -d '{"password":"<pw>"}' http://localhost:7000/api/login
curl -s -b /tmp/cj.txt -o /dev/null -w "%{http_code}\n" -X POST \
  -H 'Content-Type: application/json' -d '{"subtitle":"auth smoke test"}' \
  http://localhost:7000/api/update-source
rm /tmp/cj.txt
```
→ login `{"success": true}`, update `200`

Confirm the health status:
```
docker compose ps
```
→ `healthy`

**Finally, the check no automated test covers**: on a real iOS device, refresh the source in AltStore/Feather. The app list must load and one install must start. If that fails, a public route got gated — revert immediately.

## Test plan

Add to `tests/test_routes.py`:

1. **`test_mutating_routes_require_auth`** — parametrised over all six routes; each returns **401** without a session.
2. **`test_mutating_routes_work_when_authed`** — the same six via `authed_client`, each returns 200 or a validation 400, **never 401**.
3. **`test_public_routes_never_require_auth`** — parametrised over `/source.json`, `/ipas/<b>/<f>`, `/icons/<b>/icon.png`, `/qr`, `/`; each returns 200 with **no** session. **This is the most important test in the suite** — it is what stands between a future refactor and every user's device breaking.
4. **`test_login_rejects_wrong_password`** → 401, and no session cookie is set.
5. **`test_login_accepts_correct_password`** → 200, subsequent mutating request succeeds.
6. **`test_logout_clears_session`** → after logout, a mutating route returns 401 again.
7. **`test_session_endpoint_reports_state`** → `{"authed": false}` before login, `true` after.
8. **`test_version_string_is_escaped`** — seed an app whose version contains markup (e.g. `1.0<script>`), request `/`, and assert the raw markup does not appear unescaped in any rendered app list. Since rendering is client-side, this may be better asserted as a unit test on the template source: `assert 'escapeHtml(app.versions' in template_source`. Either is acceptable — state which you used.

Verification: `.venv/bin/python -m pytest tests/ -q` → all pass, ~8 new tests, and every pre-existing mutating-route test migrated to `authed_client`.

## Done criteria

ALL must hold:

- [ ] `python3 -m py_compile app.py` exits 0
- [ ] `grep -c "@requires_auth" app.py` returns `6`
- [ ] `grep -c "compare_digest" app.py` returns `1`
- [ ] `grep -c "ADMIN_PASSWORD" Dockerfile` returns `0`
- [ ] `compose.yml` no longer defines a competing healthcheck
- [ ] `.venv/bin/python -m pytest tests/ -q` exits 0, including `test_public_routes_never_require_auth`
- [ ] Live: `/source.json`, `/qr`, `/ipas/*`, `/icons/*` all return 200 with **no** credentials
- [ ] Live: all six mutating routes return 401 with no session, and succeed with one
- [ ] The login modal appears on page load when signed out, and the app list loads after signing in
- [ ] A real device successfully refreshes the source and starts an install
- [ ] No password value appears in any source file, commit message, log line, or test file (tests use an obvious placeholder)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- Any of `/source.json`, `/ipas/*`, `/icons/*`, `/qr` returns 401 at any point. **Revert immediately** — this breaks every subscribed device.
- A real device fails to refresh the source after the change.
- `hmac.compare_digest` raises `TypeError`. One argument is `bytes` and the other `str`; normalise both to `str`. Do **not** fall back to `==` — that reintroduces a timing side channel.
- Flask raises a duplicate-endpoint error at import. `@wraps` is missing from the decorator.
- The `window.fetch` wrapper causes recursion or breaks existing calls. Fall back to explicit per-function 401 handling and report which approach you used.
- Sessions do not persist across requests (every request looks logged out). Check that `SECRET_KEY` is set in `.env` — with Plan 001's random fallback, sessions silently die on every restart.
- You are tempted to gate `/api/apps` or `/api/app/<id>` "for consistency". They are read-only and the page needs them; leave them open unless the operator asks otherwise.

## Maintenance notes

- **`SECRET_KEY` becomes load-bearing here.** Plan 001 falls back to `os.urandom(32)` with a warning, which means every container restart logs everyone out. That was harmless before this plan and is user-visible now. Confirm `SECRET_KEY` is set in `.env` with a real random value, and treat the startup warning as a defect once this lands.
- **`SESSION_COOKIE_SECURE` must flip to `True`** the moment TLS terminates in front of this app. Until then the session cookie crosses the network in cleartext — acceptable on a private network, not otherwise. The moving-to-HTTPS change also requires updating `PUBLIC_BASE_URL` (see Plan 008).
- **CSRF protection rests entirely on `SameSite=Lax`.** That is sufficient for current browsers. If a future change needs `SameSite=None` (cross-site embedding, a separate admin origin), real CSRF tokens become mandatory on all six routes.
- The password is compared against a plaintext environment variable. Fine for a single-admin tool; if this ever grows real accounts, it needs hashed storage.
- **Rotate `ADMIN_PASSWORD` before relying on this.** The service ran with no authentication at all, and the password sat in a world-readable file — treat the existing value as burned. Plan 002 flagged this.
- **Reviewer should scrutinise**: the exact list of decorated routes. Six, and only six. A seventh `@requires_auth` on a public route is the one mistake in this plan that reaches end users' devices, and no unit test will catch it if the test list is edited to match.
