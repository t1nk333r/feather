# Plan 001: Make data paths and configuration environment-driven

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: this repo has no git history yet, so verify by checksum:
> ```
> cd /home/t1nk33r/Documents/feather && md5sum app.py compose.yml
> ```
> Expected: `2d17cee45698fa4f062cd9b4114e20d0  app.py` and
> `e7add507ae8dc7d276dbbebd4fd84a20  compose.yml`.
> `wc -l app.py` must print `2531`.
> On a mismatch, compare the "Current state" excerpts against the live code
> before proceeding; if they differ, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx
- **Planned at**: no VCS — `app.py` md5 `2d17cee45698fa4f062cd9b4114e20d0`, 2026-08-10

## Why this matters

`app.py` hardcodes `/app/data` — the *container's* path — as four module-level constants, and constructs `SourceManager` at module scope, which immediately calls `os.makedirs("/app/data")`. The consequence: **merely running `import app` on a developer machine tries to create a directory at the filesystem root and raises `PermissionError`.** That means no pytest, no `flask run`, no REPL, no debugger. The only feedback loop available today is edit → rebuild the Docker image → deploy → tail `data/app.log`.

Separately, `.env` declares five configuration keys and **not one of them is read** — `grep -c "os.environ\|os.getenv" app.py` returns `0` — and `compose.yml` has no `env_file:`, so they never reach the container either.

After this plan: the app is importable and testable locally, and the `.env` contract is real. This unblocks Plan 005 (tests), which in turn gates Plans 006–010.

## Current state

Files in play:

- `app.py` — the entire application, 2531 lines. The only file `Dockerfile:16` copies.
- `compose.yml` — 30 lines, one service `altstore-manager`.
- `.env` — declares `ADMIN_PASSWORD`, `PORT`, `SECRET_KEY`, `MAX_CONTENT_LENGTH`, `RATE_LIMIT`. **Do not read or print its values.** You only need the key names, which are listed here.

`app.py:1-26` as it exists today:

```python
from flask import Flask, render_template_string, request, jsonify, send_file
import json
import os
import logging
import qrcode
import io
import requests
import tempfile
import hashlib
import shutil
from datetime import datetime
from werkzeug.utils import secure_filename
from altparse import AltSourceManager, Parser, AltSource

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

app = Flask(__name__)

# Configuration
SOURCE_FILE = "/app/data/source.json"
UPLOAD_FOLDER = "/app/data/uploads"
IPA_FOLDER = "/app/data/ipas"
ICON_FOLDER = "/app/data/icons"
ALLOWED_EXTENSIONS = {'ipa'}
ALLOWED_ICON_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
```

`app.py:49-55` — note the literal `/app/data` repeated instead of using the constant:

```python
    def ensure_data_directory(self):
        """Ensure data and upload directories exist"""
        os.makedirs("/app/data", exist_ok=True)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(IPA_FOLDER, exist_ok=True)
        os.makedirs(ICON_FOLDER, exist_ok=True)
        logging.info("Data directories verified")
```

`app.py:718-719` — this runs at import time, which is the whole problem:

```python
# Initialize source manager
source_manager = SourceManager(SOURCE_FILE)
```

`app.py:2529-2531`:

```python
if __name__ == '__main__':
    logging.info("Starting AltStore Source Manager...")
    app.run(host='0.0.0.0', port=5000, debug=False)
```

`compose.yml:1-14` — note there is **no** `env_file:` key:

```yaml
services:
  altstore-manager:
    build: .
    container_name: altstore-source-manager
    ports:
      - 7000:5000
    volumes:
      # Mount only the data directory, not the entire /app
      - ./data:/app/data
      - ./data/ipas:/app/data/ipas
      - ./data/icons:/app/data/icons
    environment:
      - FLASK_ENV=production
      - PYTHONUNBUFFERED=1
```

**Repo conventions to match**: this is a plain single-file Flask app with no linter or formatter config. 4-space indent, double-quoted strings for config values, `logging.info(...)` / `logging.error(...)` with f-strings for messages. There is no config object or settings module — configuration lives as module-level constants right after `app = Flask(__name__)`. Keep it that way; do not introduce a config class or a settings package.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Import check | `cd /home/t1nk33r/Documents/feather && DATA_DIR=/tmp/feather-test python3 -c "import app"` | exit 0, no traceback |
| Syntax check | `python3 -m py_compile app.py` | exit 0, no output |
| Compose config | `docker compose config` | exit 0, prints resolved YAML |
| Container build | `docker compose up --build -d` | exit 0, container starts |

Note: the host runs Python 3.14 and the container runs 3.11. For *this* plan you only need `import app` to reach the point where it reads env vars — if a third-party import (`altparse`, `qrcode`) is missing on the host, see STOP conditions.

## Scope

**In scope** (the only files you may modify):
- `app.py` — lines 20–26 (the constants block), line 51 (`ensure_data_directory`), line 2531 (`app.run`)
- `compose.yml` — add `env_file:`

**Out of scope** (do NOT touch, even though they look related):
- Any `@app.route` handler — routes are untouched by this plan.
- `SourceManager` methods other than `ensure_data_directory`.
- `.env` itself — do not edit it, do not print its contents, do not copy values out of it. Plan 002 handles `.env` hygiene.
- `Dockerfile` — Plan 009 and Plan 010 modify it; leave it alone here.
- `deepseek-app.py` and `backup.old/` — dead code, Plan 003 deletes them. **Do not apply this change to them.**

## Git workflow

There is no git repository yet (Plan 002 creates it). If Plan 002 has already run, use branch `advisor/001-configurable-paths` and one commit per step. Otherwise just edit in place — but make a manual copy of `app.py` before you start:
```
cp app.py /tmp/app.py.pre-001
```

## Steps

### Step 1: Introduce `DATA_DIR` and derive the path constants

Replace the four hardcoded constants at `app.py:21-24` so they derive from a single environment-driven root. The default **must remain `/app/data`** so container behaviour is byte-identical.

Target shape:

```python
# Configuration
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
SOURCE_FILE = os.path.join(DATA_DIR, "source.json")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
IPA_FOLDER = os.path.join(DATA_DIR, "ipas")
ICON_FOLDER = os.path.join(DATA_DIR, "icons")
```

**Verify**: `python3 -m py_compile app.py` → exit 0, no output.

### Step 2: Use the constant in `ensure_data_directory`

At `app.py:51`, replace the hardcoded `os.makedirs("/app/data", exist_ok=True)` with `os.makedirs(DATA_DIR, exist_ok=True)`.

**Verify**: `grep -c '"/app/data"' app.py` → `0`

### Step 3: Read the remaining `.env` keys

Immediately after the path constants, add configuration reads. Use the exact key names below — they must match what `.env` already declares.

```python
# Environment-driven configuration
SECRET_KEY = os.environ.get("SECRET_KEY")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL")
PORT = int(os.environ.get("PORT", "5000"))
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 2 * 1024 * 1024 * 1024))
```

Then apply the two that Flask consumes directly:

```python
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
if SECRET_KEY:
    app.secret_key = SECRET_KEY
else:
    app.secret_key = os.urandom(32)
    logging.warning("SECRET_KEY not set — using a random key; sessions will not survive restart")
```

Notes on intent:
- `MAX_CONTENT_LENGTH` is the disk-fill guard. Flask's default is **unbounded**, and `data/ipas/` is already 1.3 GB. 2 GB default is deliberately generous because `.ipa` files are large.
- `ADMIN_PASSWORD` and `PUBLIC_BASE_URL` are read here but **intentionally unused** in this plan. Plan 010 consumes the first, Plan 008 the second. Do not add auth or URL logic here.
- Do **not** log the value of `ADMIN_PASSWORD` or `SECRET_KEY`, ever.

**Verify**: `grep -c "os.environ.get" app.py` → `6`

### Step 4: Honour `PORT` at startup

At `app.py:2531`, change `port=5000` to `port=PORT`.

**Verify**: `grep -n "app.run" app.py` → shows `app.run(host='0.0.0.0', port=PORT, debug=False)`

### Step 5: Pass `.env` into the container

In `compose.yml`, add an `env_file:` key to the `altstore-manager` service, as a sibling of the existing `environment:` block:

```yaml
    env_file:
      - .env
```

Leave the existing `environment:` entries (`FLASK_ENV`, `PYTHONUNBUFFERED`) in place.

**Verify**: `docker compose config | grep -A2 "FLASK_ENV"` → exit 0 and the resolved config includes the `.env` keys. If `docker compose config` errors, the YAML indentation is wrong — `env_file` must be indented to the same level as `environment` and `volumes`.

### Step 6: Confirm the app is importable outside Docker

**Verify**:
```
cd /home/t1nk33r/Documents/feather
DATA_DIR=/tmp/feather-test python3 -c "import app; print(app.SOURCE_FILE)"
```
Expected output: `/tmp/feather-test/source.json`, exit 0.

Then confirm no stray root directory was created:
```
ls -d /app 2>&1
```
Expected: `ls: cannot access '/app': No such file or directory`

Then clean up: `rm -rf /tmp/feather-test`

## Test plan

No automated tests exist yet — Plan 005 creates them, and it depends on this plan. For now the verification commands in Steps 1–6 *are* the test.

One manual end-to-end check before you call this done:

```
docker compose up --build -d
sleep 10
curl -f http://localhost:7000/source.json | head -c 200
docker compose logs --tail 20 altstore-manager
```
Expected: `/source.json` returns the catalog JSON, and the logs show `Data directories verified` with no traceback. The container must behave **exactly** as before — this plan is a pure refactor from the container's point of view.

Then confirm the catalog was not damaged:
```
python3 -c "import json; d=json.load(open('data/source.json')); print(len(d['apps']))"
```
Expected: `8`

## Done criteria

ALL must hold:

- [ ] `python3 -m py_compile app.py` exits 0
- [ ] `grep -c '"/app/data"' app.py` returns `0`
- [ ] `DATA_DIR=/tmp/feather-test python3 -c "import app"` exits 0 and creates `/tmp/feather-test/`
- [ ] `/app` does not exist on the host
- [ ] `docker compose config` exits 0 and resolves `.env`
- [ ] `curl -f http://localhost:7000/source.json` returns valid JSON with 8 apps
- [ ] `git status` (if Plan 002 has run) shows only `app.py` and `compose.yml` modified
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The checksums in the drift check don't match and the "Current state" excerpts differ from the live code.
- `import app` fails on the host with `ModuleNotFoundError` for `altparse`, `qrcode`, `flask`, or `requests`. That is a missing host dependency, **not** a bug in your change. Report it — Plan 005 sets up a virtualenv and may need to run tests inside the container instead. Do not try to `pip install` system-wide.
- `docker compose up --build` fails, or `/source.json` stops returning the catalog. Restore from `/tmp/app.py.pre-001` and report.
- `data/source.json` shows anything other than 8 apps after the container restarts.
- You find yourself needing to modify a route handler to make this work. You should not — report instead.

## Maintenance notes

- **The `/app/data` default is load-bearing.** `compose.yml` bind-mounts `./data:/app/data`, so changing the default silently orphans 1.3 GB of IPAs. Any future change to `DATA_DIR`'s default must be paired with a compose volume change.
- `ADMIN_PASSWORD` and `PUBLIC_BASE_URL` are read but unused after this plan. That is intentional and expected — a reviewer should not "clean them up". Plans 008 and 010 wire them.
- The `SECRET_KEY` fallback to `os.urandom(32)` means sessions drop on every restart. That is acceptable now (nothing uses sessions yet) but becomes user-visible once Plan 010 lands — at that point `SECRET_KEY` must be set in `.env` for real, and the warning is the signal.
- **Reviewer should scrutinise**: that the four path constants still resolve to the same absolute paths inside the container, and that `int()` on `PORT`/`MAX_CONTENT_LENGTH` can't crash startup on a malformed `.env` value (it will raise `ValueError` — acceptable, it's fail-fast, but know that it's the behaviour).
