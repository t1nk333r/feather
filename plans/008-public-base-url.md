# Plan 008: Derive published URLs from configuration, not the `Host` header

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD      # plan refreshed against 0584fa7
> md5sum app.py                   # expect a5756489489e116821a9c3a1f775f567
> grep -n "request.url_root" app.py
> grep -c "PUBLIC_BASE_URL" app.py       # expect 1 — plan 001 landed
> .venv/bin/python -m pytest tests/ -q   # expect 34 passed
> ```
> Expected: exactly 5 `url_root` hits — lines **2469, 2500, 2570, 2614, 2656**.
> **All line numbers re-verified 2026-08-11** after plans 001/004/005/006/007
> landed; `app.py` is 2722 lines. On a mismatch, match the code excerpts —
> they are authoritative, the line numbers are a convenience.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: `plans/001-configurable-paths-and-config.md`, `plans/005-smoke-test-suite.md`
- **Category**: bug (with a security dimension)
- **Planned at**: 2026-08-10. **Refreshed 2026-08-11** against `0584fa7`; `app.py` md5 `a5756489489e116821a9c3a1f775f567`. Plans 004/005/006/007 landed in between and shifted the route block by roughly +190 lines. Two done criteria were also arithmetically wrong and have been corrected — see "Done criteria".

## Why this matters

Every download URL this app publishes is built from `request.url_root` — which Werkzeug derives from the **client-supplied `Host` header** — and is then written permanently into `data/source.json`.

On this deployment (a private network with a public hostname, `feather.example.com`) that is primarily a **correctness bug** and it is easy to trigger by accident: an admin who adds an app while browsing at `http://<proxy-ip>:7000` causes the catalog to permanently advertise `http://<proxy-ip>:7000/ipas/...`. Every iOS device that reaches the source by hostname then fails to download that app. Nothing warns anyone; the value is baked into the JSON and served to clients from then on.

The security dimension is the same mechanism with intent: a request carrying a forged `Host` header permanently repoints a binary at an attacker-controlled host, and AltStore clients install whatever `downloadURL` says. This persists after the request ends.

`/qr` has the same dependency, so the QR code users scan to onboard encodes whatever `Host` the requester sent.

## Current state

**The five `request.url_root` sites.**

`app.py:2465-2470` — the QR endpoint:

```python
@app.route('/qr')
def generate_qr():
    try:
        # Use feather:// URL scheme for QR code
        source_url = request.url_root + 'source.json'
        feather_url = source_url.replace('https://', 'feather://').replace('http://', 'feather://')
```

`app.py:2500`, `2570`, `2614`, `2656` — identical in all four mutating routes:

```python
        base_url = request.url_root.rstrip('/')
```

That `base_url` is threaded down into `SourceManager` and consumed by two methods.

`app.py:181-187`:

```python
    def get_local_ipa_url(self, bundle_id, version, base_url=None):
        """Get the URL path for serving a local IPA file"""
        filename = f"{secure_filename(version)}.ipa"
        path = f"/ipas/{secure_filename(bundle_id)}/{filename}"
        if base_url:
            return f"{base_url.rstrip('/')}{path}"
        return path
```

`app.py:277-289`:

```python
    def get_local_icon_url(self, bundle_id, base_url=None):
        """Get the URL path for serving a local icon file"""
        bundle_folder = os.path.join(ICON_FOLDER, secure_filename(bundle_id))
        if os.path.exists(bundle_folder):
            for ext in ['png', 'jpg', 'jpeg', 'webp', 'gif']:
                icon_path = os.path.join(bundle_folder, f"icon.{ext}")
                if os.path.exists(icon_path):
                    path = f"/icons/{secure_filename(bundle_id)}/icon.{ext}"
                    if base_url:
                        return f"{base_url.rstrip('/')}{path}"
                    return path
        return None
```

Note both already handle `base_url=None` by returning a **relative** path. That fallback is useful and you should preserve it.

**What Plan 001 already provides** — near the top of `app.py`:

```python
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL")
```

Read, but not yet used anywhere. This plan is its consumer.

**Live evidence of the intended value** — `data/source.json` entries currently use:

```
"downloadURL": "http://feather.example.com/ipas/com.google.ios.youtube/20.49.5.ipa"
```

So `http://feather.example.com` is the canonical base in use today. Confirm with the operator before writing it into `.env`.

**Repo conventions**: module-level constants for config; `logging.warning(f"...")` for degraded-but-working conditions.

## Canonical base URL — already decided, do not re-litigate

The operator's canonical base is **`http://feather.example.com`** (no trailing slash, plain HTTP). Verified against the live catalog on 2026-08-11:

```
 5  http://feather.example.com      <- this app, canonical
 5  https://filebin.example.com     <- external file host, leave alone
 1  https://github.com               <- external, leave alone
 1  https://michael-128.github.io    <- external, leave alone
 1  http://<nas-ip>:7000           <- THE BUG: com.zhiliaoapp.musically 43.4.0_AC
```

So yes, two distinct *local* hosts exist — that is the bug this plan fixes, not a reason to stop. `filebin.example.com` shares a domain but is a different service; it is external hosting and must not be rewritten.

## Worktree caveat — Steps 3 and 4 are NOT the executor's work

The executor runs in a git worktree containing only committed files. `.env` and `data/` are gitignored and therefore **absent**. That means:

- **Step 3 (set `PUBLIC_BASE_URL` in `.env`)** — operator task, in the main checkout. `.env` must never be committed. The executor only confirms the valueless key already exists in `.env.example`.
- **Step 4 (repair catalog entries)** — operator task. There is exactly one entry to fix: `com.zhiliaoapp.musically` version `43.4.0_AC`, whose `downloadURL` is `http://<nas-ip>:7000/ipas/...` and should become `http://feather.example.com/ipas/...`. One hand edit. **Never script it** — a regex would clobber the four legitimate external URLs.
- **Step 5's `docker compose` / live-`curl` checks** — operator task, same reason.

Executor: do Steps 1, 2 and the test plan. Run the pytest half of Step 5. Say plainly in your report which steps you skipped and why. Do not fabricate a `.env` or copy `data/` in.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `.venv/bin/python -m pytest tests/ -q` | all pass |
| Syntax | `python3 -m py_compile app.py` | exit 0 |
| Find remaining sites | `grep -c "request.url_root" app.py` | `2` after Step 2 |
| Inspect catalog URLs | `python3 -c "import json;[print(v['downloadURL']) for a in json.load(open('data/source.json'))['apps'] for v in a['versions']]"` | see Step 4 |

## Scope

**In scope**:
- `app.py` — `generate_qr` (2465–2484), and the `base_url = request.url_root.rstrip('/')` line in the four mutating routes (2500, 2570, 2614, 2656)
- `.env` — add `PUBLIC_BASE_URL` (Step 3)
- `.env.example` — add the key if Plan 002 has not already
- `tests/test_routes.py`
- `data/source.json` — **hand corrections only**, Step 4

**Out of scope** (do NOT touch):
- `get_local_ipa_url` / `get_local_icon_url` themselves. They correctly use whatever `base_url` they are given; the bug is in what the callers pass. Changing them would be fixing the wrong layer.
- The `serve_ipa` / `serve_icon` routes — they read from disk and never build URLs.
- Adding a `SERVER_NAME` config or a trusted-host allowlist. Tempting and adjacent, but a separate change with its own failure modes (`SERVER_NAME` in Flask affects routing and can break `/` unexpectedly). Report it as a follow-up instead.
- Any bulk/scripted rewrite of `data/source.json`. See Step 4.

## Git workflow

- Branch: `advisor/008-public-base-url`
- One commit for code, a separate one if you correct catalog data.

## Steps

### Step 1: Add a helper that resolves the base URL

Add a module-level function near the other helpers (just after `get_file_size`, which is now at `app.py:49-59`, is a good spot):

```python
def resolve_base_url():
    """The externally-reachable base URL for links written into source.json.

    Prefers PUBLIC_BASE_URL. Falls back to the request's Host header, which
    is client-controlled — so an admin browsing by LAN IP would otherwise
    bake that IP permanently into the catalog.
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip('/')
    logging.warning(
        "PUBLIC_BASE_URL is not set — falling back to the request Host header. "
        "URLs written to source.json will reflect however this request reached the server."
    )
    return request.url_root.rstrip('/')
```

The warning matters: it is what tells an operator why their catalog has a LAN IP in it.

**Verify**: `python3 -m py_compile app.py` → exit 0

### Step 2: Use it at all five sites

Replace `base_url = request.url_root.rstrip('/')` with `base_url = resolve_base_url()` at lines **2500, 2570, 2614 and 2656**.

For `/qr` (`app.py:2469`), build the source URL from the same helper:

```python
        source_url = resolve_base_url() + '/source.json'
        feather_url = source_url.replace('https://', 'feather://').replace('http://', 'feather://')
```

Note the original concatenated `request.url_root + 'source.json'` — `url_root` ends with `/`, whereas `resolve_base_url()` strips it. **Adding the `/` back is required**, or the QR encodes `feather://hostsource.json`. Get this right and assert it in a test.

**Verify**: `grep -c "request.url_root" app.py` → **`1`** — the single remaining occurrence is the fallback inside `resolve_base_url`. (An earlier version of this plan said `2`; that was wrong. Anything higher means a site was missed.)

### Step 3: Configure the value

Add to `.env`:
```
PUBLIC_BASE_URL=http://feather.example.com
```

**Confirm the exact value with the operator first.** The catalog currently uses `http://feather.example.com` (plain HTTP, no port) — derive it from the live data rather than guessing:
```
python3 -c "import json;[print(v['downloadURL']) for a in json.load(open('data/source.json'))['apps'] for v in a['versions']]" | grep "^http" | sed 's|\(https\?://[^/]*\)/.*|\1|' | sort -u
```

No trailing slash. Plan 001 already made `compose.yml` load `.env` via `env_file:`, so nothing else is needed to deliver it to the container.

Also add the key to `.env.example` (with no value) if it is not already there.

**Verify**: `docker compose config | grep -c PUBLIC_BASE_URL` → at least `1`

### Step 4: Inspect and repair existing catalog entries — by hand

Some entries may already carry a wrong host. List them:

```
python3 -c "
import json
d = json.load(open('data/source.json'))
for a in d['apps']:
    for v in a['versions']:
        print(a['bundleIdentifier'], v['version'], v.get('downloadURL'))
    print(' icon:', a.get('iconURL'))
"
```

Review the output. Expected shapes:
- `http://feather.example.com/ipas/...` — correct, locally hosted
- `https://github.com/...`, `https://michael-128.github.io/...` — **correct and intentional**: externally-hosted apps. **Do not rewrite these.**
- `http://10.x.x.x:7000/...` or any LAN IP — wrong, fix by hand

**Do not script a bulk rewrite.** There are 8 apps and 13 versions; read them. A regex that rewrites every URL will also clobber the legitimate external ones, and there is no undo — `data/` is gitignored.

Back up first:
```
cp data/source.json /tmp/source.json.pre-008
```

**Verify** after any edit: `python3 -c "import json; d=json.load(open('data/source.json')); print(len(d['apps']))"` → `8`

### Step 5: Confirm end to end

```
.venv/bin/python -m pytest tests/ -q
docker compose up --build -d
sleep 15
```

Confirm the QR encodes the configured host regardless of how you reach the server:
```
curl -s -o /tmp/qr.png -w "%{http_code}\n" -H "Host: evil.example.com" http://localhost:7000/qr
```
→ `200`. Decode `/tmp/qr.png` if you have a decoder available; it must contain `feather://feather.example.com/source.json` and **not** `evil.example.com`. If you cannot decode it, the unit test in the test plan covers this — rely on that.

Confirm a mutation writes the configured base:
```
curl -s -X POST http://localhost:7000/api/add-version \
  -H 'Content-Type: application/json' -H 'Host: evil.example.com' \
  -d '{"bundleIdentifier":"com.fouadraheb.watusi","version":"test-008","downloadURL":"https://example.com/x.ipa"}'
python3 -c "
import json
d=json.load(open('data/source.json'))
for a in d['apps']:
    for v in a['versions']:
        if v['version']=='test-008': print(v['downloadURL'])
"
```
The stored URL must not contain `evil.example.com`. Then remove the test version by hand and restore from `/tmp/source.json.pre-008` if anything looks wrong.

## Test plan

Add to `tests/test_routes.py`:

1. **`test_download_url_uses_public_base_url`** — set `PUBLIC_BASE_URL` in the fixture environment, POST to `/api/add-app` with a spoofed `Host` header (`client.post(..., headers={'Host': 'evil.example.com'})`), assert the stored `downloadURL` starts with the configured base and does not contain `evil.example.com`.
2. **`test_qr_uses_public_base_url`** — assert the QR payload encodes the configured host. If no QR decoder is available, assert against `resolve_base_url()` directly plus a separate unit test that `resolve_base_url()` returns the configured value when the env var is set.
3. **`test_qr_url_has_slash_before_source_json`** — guards the concatenation bug from Step 2. Assert the built URL ends with `/source.json`, not `source.json` glued to the host.
4. **`test_falls_back_to_host_when_unset`** — with `PUBLIC_BASE_URL` unset, assert the old behaviour still works (relative-to-request URLs) and that a warning is logged. This keeps the fallback honest for anyone running without config.
5. **`test_icon_url_uses_public_base_url`** — same as (1) for `get_local_icon_url`.

Verification: `.venv/bin/python -m pytest tests/ -q` → all pass, 5 new tests.

## Done criteria

ALL must hold:

- [ ] `python3 -m py_compile app.py` exits 0
- [ ] `grep -c "request.url_root" app.py` returns **`1`** (the fallback inside `resolve_base_url`)
- [ ] `grep -c "resolve_base_url()" app.py` returns **`6`**, not `5` — the substring also matches the `def resolve_base_url():` line. The five *call sites* are what matter: `grep -n "= resolve_base_url()\|resolve_base_url() +" app.py` must show 5.
- [ ] `.venv/bin/python -m pytest tests/ -q` exits 0 with 5 new tests
- [ ] `.env.example` already contains a valueless `PUBLIC_BASE_URL=` at line 14 — confirm, do not duplicate it. Setting the real value in `.env` is an **operator task, out of scope for the executor** (see "Worktree caveat").
- [ ] A POST with a spoofed `Host` header does not put that host into the catalog — proven by the test suite, not by touching live data
- [ ] `git status --short` shows only `app.py` and `tests/test_routes.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `grep -n "request.url_root"` finds sites outside the five listed. The plan's assumptions are stale — report the full list.
- The operator cannot confirm the canonical base URL. Do **not** guess: an incorrect `PUBLIC_BASE_URL` breaks every subsequent download in a way that is invisible until a user tries to install something.
- ~~`data/source.json` contains download URLs on more than one distinct local host.~~ **Already resolved — do not stop for this.** It does, and the decision is made: see "Canonical base URL" below.
- Setting `PUBLIC_BASE_URL` breaks `/` or any route. It should not — this touches only URL *construction*. Report rather than working around it.
- You are about to run `sed`, `python -c`, or any script that rewrites URLs in `data/source.json` in bulk. Explicitly forbidden — 13 versions, edit by hand.

## Maintenance notes

- **`PUBLIC_BASE_URL` is now required for correct operation**, even though the code still runs without it. The fallback warning is the only signal — anyone deploying this fresh will get a working app that quietly writes wrong URLs until they notice. A stronger version of this plan would refuse to start without it; that was left out because it would break the existing deployment on restart. Consider tightening it once `.env` is reliably populated.
- If TLS is added later, `PUBLIC_BASE_URL` must change to `https://` — and the existing `http://` URLs already in `data/source.json` will **not** update themselves. That is a second manual pass over the catalog.
- The `/qr` scheme replacement (`.replace('https://', 'feather://')`) is order-dependent and would mangle a base URL containing those substrings elsewhere. It is fine for normal hostnames; worth knowing if the base URL ever gains a path component.
- Deliberately not done: a trusted-host allowlist or Flask's `SERVER_NAME`. Configuring the published base solves the actual bug; `SERVER_NAME` affects routing and has surprised people before.
- **Reviewer should scrutinise**: the `/qr` slash handling (Step 2), and that no legitimate external `downloadURL` in the catalog was rewritten in Step 4.
