# Plan 066: Independent icon storage backend — icons on local disk, IPAs on Garage

> Backend + config. `app.py` (backend selection + one reconcile gate), `.env`/README
> docs. Adds tests. Drift check: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q`
> → 241 passed at commit `4f6e8ef`.

## Status
- Priority P2 (operator request: stop putting icons on Garage). Effort S. Risk MED. Planned at `4f6e8ef`.

## Why
The maintainer wants **icons stored on local disk at `/app/data/icons`** while **IPAs
stay on Garage** — sidestepping the Garage icon-permission problems entirely. Today
the backend is a single switch: `STORAGE_BACKEND=garage` sends **both** icons and IPAs
to Garage; there is no way to split them (app.py ~1663):
```python
if STORAGE_BACKEND == "garage":
    ipa_storage = GarageIpaStorage()
    icon_storage = GarageIconStorage()
else:
    ipa_storage = LocalIpaStorage()
    icon_storage = LocalIconStorage()
```
The icon path is already fully polymorphic through `icon_storage`, so only the
**selection** needs to change:
- `get_hosted_icon_url` (app.py ~1108) always returns the app-owned `/icons/<bundle>/icon.<ext>` path (backend-independent; that's what's stored in `source.json`).
- `serve_icon` (app.py ~1745) calls `icon_storage.exists()`, then `icon_storage.public_url()`: a truthy URL (Garage) → 302 redirect; a falsy one (Local) → `send_file` from `ICON_FOLDER` (`/app/data/icons`).
So pointing `icon_storage` at `LocalIconStorage` makes icons read/write local disk with no other route changes.

## Design — a separate `ICON_STORAGE_BACKEND`, defaulting to `STORAGE_BACKEND`
Add an independent env var that defaults to the existing one (so current deployments
are unaffected until they set it). To get the requested layout the operator sets:
```
STORAGE_BACKEND=garage          # IPAs -> Garage (unchanged)
ICON_STORAGE_BACKEND=local      # icons -> /app/data/icons
```
The per-class Garage config validator (`_require_garage_config`, called from each
Garage class's `__init__`) still fires correctly for whichever backend(s) are Garage,
because it runs at instantiation — no separate top-level check needed.

## Current state — the facts that matter
- `ICON_FOLDER = os.path.join(DATA_DIR, "icons")` (app.py ~74) → `/app/data/icons`.
- `compose.yml` **already mounts** `./data:/app/data` and `./data/icons:/app/data/icons`,
  so the icon volume exists — no new mount is strictly required (the operator only needs
  the host dir to persist).
- `STORAGE_BACKEND` is referenced at: line ~120 (def), ~1663 (init), ~3002
  (`is_local_backend` — **IPA-specific**: gates on-disk IPA-path + zip-validation checks;
  the icon health check uses the polymorphic `icon_storage.exists()`, so this stays keyed
  on the IPA backend), and ~3129 (`_reconcile_icons` gate — **icon-specific**, must move
  to the icon backend).

## Scope
- **In scope:** add `ICON_STORAGE_BACKEND`; route `icon_storage` on it; move the
  `_reconcile_icons` backend gate to it; docs; tests.
- **Out of scope:** `ipa_storage` selection (stays on `STORAGE_BACKEND`); the storage
  classes themselves; `serve_icon`/`get_hosted_icon_url` (already backend-agnostic);
  `is_local_backend` in the health scan (IPA-specific — leave it); any Garage→local data
  migration of existing objects (see Migration note).

## Step 1 — declare the var (app.py, right after line ~120 `STORAGE_BACKEND = ...`)
```python
# Icons can use a different backend than IPAs (e.g. IPAs on Garage, icons on
# local disk at /app/data/icons). Defaults to STORAGE_BACKEND for backward
# compatibility, so existing single-backend deployments are unaffected.
ICON_STORAGE_BACKEND = os.environ.get("ICON_STORAGE_BACKEND", STORAGE_BACKEND)
```

## Step 2 — route the icon backend independently (app.py ~1663)
Replace the coupled init block with:
```python
# IPA storage backend (STORAGE_BACKEND).
if STORAGE_BACKEND == "garage":
    ipa_storage = GarageIpaStorage()
else:
    ipa_storage = LocalIpaStorage()

# Icon storage backend (ICON_STORAGE_BACKEND, defaults to STORAGE_BACKEND).
# Independent so icons can live on local disk while IPAs stay on Garage.
if ICON_STORAGE_BACKEND == "garage":
    icon_storage = GarageIconStorage()
else:
    icon_storage = LocalIconStorage()
```

## Step 3 — the icon reconcile gate keys on the icon backend (app.py ~3129)
In `_reconcile_icons`, change:
```python
    if STORAGE_BACKEND != "garage":
```
to:
```python
    if ICON_STORAGE_BACKEND != "garage":
```
(With icons local, reconcile correctly reports the local no-op — "on-disk icons ARE
what's served". The docstring's mention of a "STORAGE_BACKEND switch" may be left or
reworded to "icon backend switch".)

## Step 4 — docs
- `.env` / `.env.example` (whichever the repo ships — check `release-sources.example.json`
  is NOT it; look for `.env.example`): document `ICON_STORAGE_BACKEND` with the split
  example above. If no `.env.example` exists, add the note to README's storage section.
- `README.md` storage section: one line — icons can be pinned to local disk with
  `ICON_STORAGE_BACKEND=local` while IPAs stay on Garage; icons live in `/app/data/icons`
  (already a mounted volume in `compose.yml`).

## Step 5 — tests (`tests/test_storage.py`)
Follow the file's existing reload-with-env pattern. Add:
- `test_icon_backend_defaults_to_storage_backend`: with `ICON_STORAGE_BACKEND` unset and
  `STORAGE_BACKEND=local`, reload and assert `icon_storage` is a `LocalIconStorage` and
  `ipa_storage` a `LocalIpaStorage`.
- `test_split_icon_local_ipa_garage`: set `STORAGE_BACKEND=garage` + full Garage env +
  `ICON_STORAGE_BACKEND=local`, reload, assert `isinstance(app.icon_storage, LocalIconStorage)`
  and `isinstance(app.ipa_storage, GarageIpaStorage)`. (Use the same Garage-env fixture the
  existing Garage tests use so `_require_garage_config` passes.)
- If reloading with `STORAGE_BACKEND=garage` is awkward in this suite, assert instead on a
  small pure helper — but prefer the reload test to prove the wiring.

## Verify
```bash
grep -c "ICON_STORAGE_BACKEND" app.py                                   # >= 3 (def + init + reconcile gate)
grep -n "icon_storage = GarageIconStorage\|icon_storage = LocalIconStorage" app.py  # both present, gated on ICON_STORAGE_BACKEND
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_storage.py -q
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q                   # 243 passed (241 + 2 new)
```

## Done criteria
- [ ] `ICON_STORAGE_BACKEND` (default `STORAGE_BACKEND`) selects the icon backend independently; `STORAGE_BACKEND=garage` + `ICON_STORAGE_BACKEND=local` yields Garage IPAs + local-disk icons.
- [ ] `_reconcile_icons` keys on the icon backend (local → no-op).
- [ ] Default/unset behavior is unchanged (backward compatible); `_require_garage_config` still fires for whichever backend is Garage.
- [ ] Docs updated; `git status --short` shows only `app.py`, the docs file(s), `tests/test_storage.py`; suite `243 passed`.

## STOP conditions
- If `LocalIconStorage` lacks a `public_url` returning falsy (so `serve_icon` would not
  fall through to `send_file`), STOP and report — the local path must serve from disk.
  (It should already, since local is the default backend.)
- If any test asserts icon+IPA backends are always the same object, that encodes the old
  coupling — update it and note it.

## Migration note (operator — important)
- `source.json` icon URLs are the app-owned `/icons/<bundle>/icon.<ext>` path regardless
  of backend, so **no source.json rewrite is needed.**
- BUT icons that currently exist **only on Garage** (uploaded under the old coupled
  backend) are NOT on local disk, so after the switch `serve_icon` will 404 for them
  until they're re-hosted locally. Options: (a) re-import those apps (the icon is
  re-extracted to disk — plan 064 helps here), or (b) copy the Garage `icons/` objects
  down into `/app/data/icons/<bundle>/icon.<ext>`. Newly imported/extracted icons land on
  local disk automatically. Consider a follow-up `migrate_icons_from_garage.py` (reverse
  of the existing `scripts/migrate_icons_to_garage.py`) if there are many.
- Deploy note: this is env-only at runtime — set `ICON_STORAGE_BACKEND=local` in `.env`
  and `docker compose up -d` (the icon volume is already mounted).

## Maintenance note
- Keeps the two backends orthogonal; the same pattern could later split any other
  asset class. `_require_garage_config` remains the single source of truth for Garage
  env validation and now guards either backend independently via class instantiation.
