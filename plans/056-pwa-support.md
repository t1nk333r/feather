# Plan 056: PWA support — installable on iOS/Android, standalone mobile chrome

> **Executor instructions**: Follow this plan step by step. Run the verification
> gates. If a STOP condition occurs, stop and report. Update this plan's status
> row in `plans/README.md` when done unless a reviewer maintains the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 2223576..HEAD -- app.py templates/index.html static/ tests/test_routes.py
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 232 passed
> ```

## Status

- **Priority**: P2 (the UI is used on phones; make it installable to the home screen)
- **Effort**: M
- **Risk**: LOW (additive: a manifest, head tags, a public service-worker route; no existing behavior changes)
- **Depends on**: none (pairs with plan 055's bottom bar)
- **Category**: feature / mobile
- **Planned at**: commit `2223576`, 2026-08-19

## Why this matters

The admin UI is used on a **phone** to manage the source, but it isn't a PWA — you
can't "Add to Home Screen" and get a real app-like, standalone experience. This
plan adds a **web app manifest**, the **Apple/`theme-color` meta tags**, and a
minimal **service worker** so the UI is installable on iOS Safari and Android
Chrome and runs in standalone mode (no browser chrome), with a home-screen icon.

## Current state

- `<head>` (top of `templates/index.html`) has a `<title>`, favicon links
  (`/static/icon.svg` light, `/static/icon-dark.svg` dark, `/static/favicon.png`),
  and `<meta name="viewport" content="width=device-width, initial-scale=1.0">`.
  It has **no** `manifest` link, **no** `theme-color`, and **no** `apple-mobile-web-app-*` tags.
- `static/` already contains PNG icons: `icon-256.png` (256×256), `icon-512.png`
  (512×512), `icon-1024.png` (1024×1024), plus `icon.png`, `favicon.png`. **Reuse
  these — do not generate new images.**
- Flask serves `/static/` publicly (no auth). Public GET routes include `/`,
  `/source.json`, `/qr`, `/icons/...`, `/ipas/...`; mutation routes are
  `@requires_auth`. A manifest under `/static/` and a `/sw.js` route are fetched
  **before/around login**, so they MUST be public (no `@requires_auth`).
- The page's JS lives in a `<script>` near the end of the body (where `switchTab`,
  `showToast`, etc. are defined) — register the service worker there.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `232 passed` before |
| Syntax | `.venv/bin/python -m py_compile app.py` | exit 0 |
| Manifest valid JSON | `python -c "import json;json.load(open('static/manifest.webmanifest'))"` | no error |
| New tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k "pwa or manifest or sw"` | pass |
| Full | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `232 + N passed` |

## Scope

**In scope:**
- `static/manifest.webmanifest` (new) — the web app manifest.
- `templates/index.html` — `<head>` PWA/apple/theme-color tags + a small
  service-worker registration script.
- `app.py` — a **public** `GET /sw.js` route serving the service worker at root
  scope (so it can control the whole app).
- `tests/test_routes.py` — tests.

**Out of scope:**
- Generating new icon images (reuse existing PNGs).
- Offline caching of the live admin UI or API responses (staleness risk) — the
  service worker stays minimal (network-first for navigations; it does NOT cache
  authenticated pages or API calls).
- Push notifications, background sync, or app-shortcuts.
- The nav/layout (that's plan 055).

## Steps

### Step 1: Baseline → `232 passed`. If not, STOP.

### Step 2: The manifest — `static/manifest.webmanifest`
```json
{
  "name": "AltStore Source Manager",
  "short_name": "Source Mgr",
  "description": "Manage your self-hosted AltStore/Feather app source.",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "background_color": "#ffffff",
  "theme_color": "#f2f2f7",
  "icons": [
    { "src": "/static/icon-256.png",  "sizes": "256x256",  "type": "image/png" },
    { "src": "/static/icon-512.png",  "sizes": "512x512",  "type": "image/png" },
    { "src": "/static/icon-1024.png", "sizes": "1024x1024","type": "image/png" }
  ]
}
```
(Link it as `/static/manifest.webmanifest`. Flask's static handler serves
`.webmanifest` — confirm the response is 200; if the content-type matters for a
test, assert 200 + a `name` field rather than an exact MIME.)

### Step 3: `<head>` tags (add after the existing favicon links / viewport)
```html
<link rel="manifest" href="/static/manifest.webmanifest">
<meta name="theme-color" content="#f2f2f7" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Source Mgr">
<link rel="apple-touch-icon" href="/static/icon-256.png">
```

### Step 4: The service worker route — public `GET /sw.js`
Serve a minimal SW at **root scope** (a worker only controls paths under its own
URL, so it must be served from `/`, not `/static/`). Add a public route (NOT
`@requires_auth`):
```python
@app.route('/sw.js')
def service_worker():
    js = """
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
// Network-first for navigations; never cache API or authenticated pages.
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(caches.open('feather-static-v1').then(async (c) => {
      const hit = await c.match(e.request);
      if (hit) return hit;
      const res = await fetch(e.request);
      if (res.ok) c.put(e.request, res.clone());
      return res;
    }));
  }
  // everything else: default network handling (no caching)
});
""".strip()
    resp = app.response_class(js, mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp
```
(Only `/static/` assets are cached, cache-first; navigations and API calls are
never cached, so the live admin UI is always fresh. `Service-Worker-Allowed: /`
permits root scope even though the file is at `/sw.js`.)

### Step 5: Register the SW (in the page's `<script>`)
```html
<script>
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
    });
  }
</script>
```
(Guard with the feature check; swallow errors so a registration failure never
breaks the page.)

**Verify**: `.venv/bin/python -m py_compile app.py` → exit 0; manifest JSON loads.

### Step 6: Tests (`tests/test_routes.py`)
1. `test_sw_js_public_and_scoped` — `GET /sw.js` (unauthenticated) → 200, content-type contains `javascript`, body contains `addEventListener`, and header `Service-Worker-Allowed == '/'`.
2. `test_manifest_served` — `GET /static/manifest.webmanifest` → 200 and the JSON has `"name"` and `"display": "standalone"`.
3. `test_index_has_pwa_tags` — `GET /` contains `rel="manifest"`, `apple-mobile-web-app-capable`, and a `theme-color` meta.
4. `test_sw_and_manifest_need_no_auth` — both are reachable with no session (they must load before login).

**Prove discrimination**: temporarily drop the `Service-Worker-Allowed` header →
test 1 fails. Restore.

**Verify**: focused `-k "pwa or manifest or sw"`; then full suite.

## Done criteria
- [ ] `static/manifest.webmanifest` exists (valid JSON, standalone, reuses the existing PNG icons); `<head>` links it + has apple/theme-color tags.
- [ ] `GET /sw.js` is public, served at root scope (`Service-Worker-Allowed: /`), minimal (only `/static/` cached; navigations/API never cached); the page registers it defensively.
- [ ] The app is installable ("Add to Home Screen") and runs standalone; no existing route/behavior changed.
- [ ] `.venv/bin/python -m py_compile app.py` exit 0; full suite green (232 + 4).
- [ ] `git status --short` shows only `app.py`, `templates/index.html`, `static/manifest.webmanifest`, `tests/test_routes.py`.

## STOP conditions
- The SW caching would serve a stale authenticated page — it must not; only `/static/` is cached. If you can't guarantee that, ship the manifest + apple tags (which alone enable iOS "Add to Home Screen") and a no-op SW, and report.
- A test needs an exact `.webmanifest` MIME that Flask doesn't set — assert 200 + a JSON field instead of the MIME.

## Maintenance notes
- iOS Safari uses the `apple-mobile-web-app-*` tags + manifest for home-screen
  install; Android Chrome uses the manifest + a registered SW with a fetch handler.
  Both are covered.
- Pairs with plan 055: the bottom bar + `env(safe-area-inset-bottom)` make the
  standalone PWA feel native (respects the iPhone home indicator).
- If offline support of the shell is ever wanted, extend the SW carefully — never
  cache `@requires_auth` responses or `/api/*`.
- Reviewer: confirm `/sw.js` and the manifest are public, the SW caches only
  `/static/`, and registration is guarded so it can't break the page.
