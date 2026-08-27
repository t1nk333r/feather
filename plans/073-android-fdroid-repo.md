# Plan 073: Serve an Android F-Droid repository beside the iOS source

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Projects/feather
> git rev-parse --short HEAD                      # plan written at 764bd16; re-stamped 4bb2db5 (022 merged; no in-scope drift)
> git diff --stat 764bd16..HEAD -- app.py templates/index.html compose.yml Dockerfile Dockerfile.bot Jenkinsfile requirements.txt .dockerignore .env.example README.md tests/test_routes.py
> ```
> If any of those files changed, compare every "Current state" excerpt below
> against the live code before proceeding; on a mismatch, STOP.

## Status

- **Priority**: P2
- **Effort**: L (two to three focused sessions; the steps are independently verifiable)
- **Risk**: MED — additive (new routes, new tab, new opt-in Compose service, one new pip dependency); nothing on the iOS path changes. The risk is in the sidecar contract, which Step 0 proves before any code is written.
- **Depends on**: none (Plans 010 auth, 011 storage, 016 profiles are already DONE and are reused as-is)
- **Category**: direction
- **Planned at**: commit `764bd16`, 2026-08-26 (baseline refreshed at `4bb2db5` the same day: 244 tests)

## Why this matters

Feather today is a self-hosted iOS app source: an admin UI, a `source.json` catalog, `.ipa` hosting, Telegram and GitHub/GitLab ingest. The operator wants the same thing for Android — a third-party **F-Droid repository** that Android devices can subscribe to, with the same admin login, the same data directory, and the same operational shape (Docker Compose, opt-in profiles, sidecars that talk to feather over HTTP or a shared volume).

The decision already taken (by the operator, 2026-08-26) is: **extend this repo, do not fork**, and **let the reference `fdroidserver` build and sign the index in a sidecar** rather than hand-rolling JAR signing in Python. F-Droid clients require a JAR-signed `index-v1.jar` / `entry.jar`; `fdroidserver` produces exactly what the client expects, extracts icons, computes hashes and signer fingerprints, and is maintained by the F-Droid project. Feather's job is therefore: accept APKs, write `metadata/<package>.yml`, keep the `repo/` directory in shape, ask the sidecar to rebuild, and serve the result under `/fdroid/repo/`.

After this plan lands, an operator can: enable the `android` profile, upload an APK (or give a URL) in a new **Android** tab, and scan a QR code on an Android phone that adds the repo — with its fingerprint — to the F-Droid client. Telegram ingest, cron release import, and Garage storage for APKs are explicitly **follow-up plans**, not this one.

## Current state

### Verified facts about `fdroidserver` (spiked on 2026-08-26 — do not re-derive)

All of the following was confirmed by running the official image against a real APK (`https://f-droid.org/F-Droid.apk`, 12.4 MB) in a scratch directory:

- Image: `registry.gitlab.com/fdroid/docker-executable-fdroidserver:master` (857 MB). Entrypoint is `sh -c '. /etc/profile.d/bsenv.sh && … /home/vagrant/fdroidserver/fdroid "$@"'`, `WORKDIR /repo`. `fdroid --version` → `6af4c42`. `apksigner`, `keytool`, `jarsigner`, `java`, `python3`, androguard 4.1.3 are all in the image.
- **It must run as root.** `/home/vagrant` is mode 0700 owned by uid 1000, so any other uid gets `fdroid: Permission denied`; uid 1000 itself dies in `setup_status_output` on a GitPython `git diff --cached` error. Root works on a clean directory every time. `fdroidserver` is not pip-installed in the image (`python3 -m fdroidserver` does not exist) — the only way to run it is the entrypoint or `/home/vagrant/fdroidserver/fdroid`.
- `fdroid update --create-metadata --pretty` run in a directory containing `config.yml`, `keystore.p12`, `repo/<apk>` and `metadata/` writes into `repo/`: `index-v1.jar`, `index-v1.json`, `index-v2.json`, `entry.jar`, `entry.json`, `index.jar`, `index.xml`, `index.html`, `index.css`, `index.png`, `icons/`, `icons-120/` … `icons-640/`, `status/`, `diff/`. It logs `Creating signed index with this key (SHA256): 47 8B FF …` and `INFO: Finished`. With no `repo/icons/icon.png` it warns and generates a placeholder repo icon — harmless.
- `index-v1.json` shape (what feather will read):
  ```json
  {"repo": {"timestamp": 1787756718595, "version": 30000, "name": "…", "icon": "icon.png", "address": "https://…/fdroid/repo", "description": "…"},
   "requests": {"install": [], "uninstall": []},
   "apps": [{"packageName": "org.fdroid.fdroid", "name": "F-Droid", "suggestedVersionCode": "2147483647", "license": "Unknown", "icon": "org.fdroid.fdroid.1023052.png", "added": 1787756718595, "lastUpdated": 1787756718595, "categories": ["repo"]}],
   "packages": {"org.fdroid.fdroid": [{"apkName": "org.fdroid.fdroid_1023052.apk", "versionCode": 1023052, "versionName": "1.23.2", "hash": "985f…", "hashType": "sha256", "size": 12426276, "minSdkVersion": 23, "targetSdkVersion": 30, "signer": "43238d51…", "sig": "…", "nativecode": ["arm64-v8a", …], "uses-permission": [["android.permission.INTERNET", null], …], "added": 1787756718595}]}}
  ```
- `metadata/<package>.yml` is plain YAML. Keys feather will write: `Name` (≤50 chars), `Summary` (≤80), `Description` (≤4000, newlines preserved), `AuthorName`, `WebSite`, `SourceCode`, `License` (SPDX id), `Categories` (must be a YAML list). A skeleton generated by `--create-metadata` looks like:
  ```yaml
  AuthorName: ''
  Categories:
  - repo
  CurrentVersionCode: 2147483647
  IssueTracker: ''
  Name: F-Droid
  SourceCode: ''
  Summary: ''
  WebSite: ''
  ```
- `config.yml` keys that matter: `repo_url`, `repo_name`, `repo_description`, `repo_icon`, `archive_older`, `keystore`, `repo_keyalias`, `keystorepass`, `keypass`, `keydname`. Secrets may be written as `keystorepass: {env: FDROID_KEYSTORE_PASSWORD}` and are then read from the environment.
- Repository fingerprint = SHA-256 of the signing certificate. Reproducible with:
  `keytool -exportcert -keystore keystore.p12 -alias feather -storepass "$FDROID_KEYSTORE_PASSWORD" | sha256sum` (64 lowercase hex chars; same value `fdroid update` prints with spaces).
- Subscription URL the F-Droid client accepts: `https://<host>/fdroid/repo?fingerprint=<64 hex>`. A QR of that URL is the standard way third-party repos (e.g. IzzyOnDroid) are added.
- Pure-Python APK inspection: `pyaxmlparser==0.3.31` (universal wheel; pulls `lxml` which has cp311 and cp314 wheels, `asn1crypto`, `click`). Verified output on the real APK:
  ```
  a = APK(path); a.package -> 'org.fdroid.fdroid'; a.version_code -> '1023052' (a str!)
  a.version_name -> '1.23.2'; a.get_min_sdk_version() -> 23; a.get_target_sdk_version() -> 30
  a.get_app_name() -> 'F-Droid'; a.icon_data -> PNG bytes; a.is_valid_APK() -> True
  ```
- `PyYAML==6.0.3` has cp311 and cp314 wheels (both interpreters CI runs — see Jenkinsfile).

### Files in this repo and their roles

- `app.py` (3,289 lines) — the whole Flask app. Config constants at lines 66–136, `requires_auth` at 1680, public routes 1714–1804, the six mutating routes from 1841, `_scan_catalog_health` at 2973, error handlers + `__main__` at 3273–3289.
- `templates/index.html` (2,464 lines) — the single-page admin UI. Tab panels are `<div id="…" class="tab-content">` at lines 653–915; the floating tab bar is `<nav class="tabbar">` at 915–933; `switchTab()` at 1125; `showToast()` at 1076; the add-app submit handler at ~1240–1290; `loadApps()` at 1614.
- `compose.yml` — services `altstore-manager`, `telegram-bot-api` + `ipa-ingest-bot` (profile `telegram`), `release-import` (profile `release-import`).
- `Dockerfile` (main image, `python:3.11-slim`, runs as user `altstore`, uid **999**), `Dockerfile.bot` (sidecar exemplar — see below), `.dockerignore` (deny-all, re-admit per file), `Jenkinsfile` (tests on 3.11 + 3.14, image builds + smoke tests on `main`).
- `tests/test_routes.py` — route tests; fixtures `client`, `authed_client` (lines 80–115).
- `.env.example` — every variable, grouped by plan, with a comment naming the plan.

### Excerpts the executor must match

`app.py:66-83` — configuration constants (add the Android ones directly after `BACKUP_FOLDER`):
```python
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
SOURCE_FILE = os.path.join(DATA_DIR, "source.json")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
IPA_FOLDER = os.path.join(DATA_DIR, "ipas")
ICON_FOLDER = os.path.join(DATA_DIR, "icons")
BACKUP_FOLDER = os.path.join(DATA_DIR, "backups")
…
ALLOWED_EXTENSIONS = {'ipa'}
```

`app.py:1680-1687` — the auth decorator every mutating route uses:
```python
def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('authed'):
            return jsonify({"success": False, "error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return wrapper
```

`app.py:1841-1891` — the response convention (`{"success": true, "message"}` / `{"success": false, "error"}`, HTTP 400 on validation failure, `logging.error` then 400 on exception):
```python
@app.route('/api/add-app', methods=['POST'])
@requires_auth
def add_app():
    try:
        base_url = resolve_base_url()
        …
        if not data.get('bundleIdentifier'):
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400
        …
        if success:
            notify("add_app", f"New app published: …")
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
    except Exception as e:
        logging.error(f"Error adding app: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400
```

`app.py:895-908` — `SourceManager` uses one in-process `threading.Lock` for writes; the Android manager must do the same (single-process Waitress, see the comment at `app.py:3279-3284`).

`app.py:1783-1801` — the QR route to mirror for `/fdroid/qr`:
```python
@app.route('/qr')
def generate_qr():
    try:
        source_url = resolve_base_url() + '/source.json'
        feather_url = source_url.replace('https://', 'feather://').replace('http://', 'feather://')
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(feather_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png')
    except Exception as e:
        logging.error(f"QR generation error: {str(e)}")
        return jsonify({"error": "QR generation failed"}), 500
```

`Dockerfile.bot` — the sidecar-image exemplar (short, single COPY, non-root where possible):
```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir requests==2.31.0
COPY scripts/telegram_bot_ingest.py .
RUN groupadd -r ipabot && useradd -r -g ipabot ipabot \
    && chown -R ipabot:ipabot /app
USER ipabot
CMD ["python", "telegram_bot_ingest.py"]
```

`compose.yml` — the opt-in service exemplar (`release-import`):
```yaml
  release-import:
    profiles: ["release-import"]
    image: ghcr.io/d7eeem/feather:latest
    build: .
    …
    env_file:
      - .env
    volumes:
      - ./data/release-import:/var/lib/feather-release-import
    restart: "no"
```

`.dockerignore` — deny-all with explicit re-admits; every new file an image COPYs must be re-admitted:
```
*
!requirements.txt
!app.py
!templates/
!static/
!scripts/telegram_bot_ingest.py
!scripts/release_source_ingest.py
```

`tests/test_routes.py:80-115` — fixtures. `client` sets `DATA_DIR`, `ADMIN_PASSWORD`, `SECRET_KEY` **before** `importlib.reload(app_module)`, seeds `source.json`, yields a Flask test client with `c.app_module` stashed; `authed_client` logs in via `POST /api/login`. Model every new test on `test_add_app_then_delete_app_round_trip` (line 636) and `test_add_version_skips_existing_version` (line 683).

Conventions to keep: constants `UPPER_SNAKE` from `os.environ.get`; public routes never require auth; every mutating route is `@requires_auth`; `logging.error(f"… {str(e)}")` then a JSON error; filenames via a strict regex (do **not** use `secure_filename` on Android package names — it would keep dots, but the versionCode is an int and package names must be validated by regex anyway); commit messages `feat(scope): … (plan 073)`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Create venv + install | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt` | exit 0 |
| Tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `244 passed` before you start |
| Focused tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_android.py -q` | all pass |
| Wheel check (HANDOFF trap) | `.venv/bin/pip download --no-deps --only-binary=:all: --python-version 311 -d /tmp/w pyaxmlparser PyYAML lxml && .venv/bin/pip download --no-deps --only-binary=:all: --python-version 314 -d /tmp/w pyaxmlparser PyYAML lxml` | both exit 0 |
| Pull sidecar image | `docker pull registry.gitlab.com/fdroid/docker-executable-fdroidserver:master` | exit 0 |
| Build sidecar image | `docker build -f Dockerfile.fdroid -t feather-fdroid:dev .` | exit 0 |
| Real APK for manual checks | `curl -sSL -o /tmp/F-Droid.apk https://f-droid.org/F-Droid.apk` | ~12 MB, `file` says `Android package (APK)` |

No lint or typecheck step exists in this repo.

## Scope

**In scope** (the only files you may create or modify):
- `app.py` — Android constants, `AndroidRepoManager`, `_inspect_apk`, eight new routes
- `templates/index.html` — new `android` tab panel, tab-bar button, JS
- `requirements.txt` — add `pyaxmlparser==0.3.31` and `PyYAML==6.0.3`
- `scripts/fdroid_index_loop.sh` (create) — the sidecar's loop
- `Dockerfile.fdroid` (create) — sidecar image
- `compose.yml` — new `fdroid-index` service, profile `android`; one new volume line on `altstore-manager`
- `.dockerignore` — re-admit `scripts/fdroid_index_loop.sh`
- `Jenkinsfile` — build + smoke-test the third image
- `.env.example`, `README.md` — document the new variables and the operator steps
- `tests/test_android.py` (create)
- `plans/README.md` — status row only

**Out of scope** (do NOT touch, even though they look related):
- `SourceManager`, `source.json`, `/source.json`, `/ipas/…`, `/icons/…`, `/qr` — the iOS path is the product; nothing here may alter its behaviour or response shape.
- `scripts/telegram_bot_ingest.py`, `scripts/release_source_ingest.py`, `Dockerfile.bot` — APK ingest via Telegram / releases is a follow-up plan.
- Garage/S3 for APKs. Phase 1 serves the F-Droid repo from local disk only. Do not add `STORAGE_BACKEND` awareness to any Android code path.
- `_scan_catalog_health`, `/api/health` — do not extend the iOS health scan; Android has its own status endpoint in this plan.
- Existing tests — none may be edited.

## Git workflow

- Branch: `advisor/073-android-fdroid-repo`
- One commit per step, messages like `feat(android): APK inspection + AndroidRepoManager (plan 073)`, `feat(compose): fdroid-index sidecar (plan 073)`, `docs: Android repo operator guide (plan 073)`.
- Do not push or open a PR unless the operator instructed it.

## Steps

### Step 0: Prove the sidecar contract in a scratch directory (no repo changes)

Recreate the spike so you have seen the tool behave before you code against it.

```bash
mkdir -p /tmp/fd-spike/repo /tmp/fd-spike/metadata && cd /tmp/fd-spike
curl -sSL -o repo/org.fdroid.fdroid_1023052.apk https://f-droid.org/F-Droid.apk
export FDROID_KEYSTORE_PASSWORD=spike-not-a-real-password
docker run --rm -v "$PWD":/repo --entrypoint sh registry.gitlab.com/fdroid/docker-executable-fdroidserver:master -c \
  'keytool -genkeypair -keystore /repo/keystore.p12 -storetype PKCS12 -alias feather -keyalg RSA -keysize 4096 -validity 10000 -storepass "'"$FDROID_KEYSTORE_PASSWORD"'" -dname "CN=feather"'
cat > config.yml <<'EOF'
repo_url: https://example.test/fdroid/repo
repo_name: Spike
repo_description: spike
repo_icon: icon.png
archive_older: 0
keystore: keystore.p12
repo_keyalias: feather
keystorepass: {env: FDROID_KEYSTORE_PASSWORD}
keypass: {env: FDROID_KEYSTORE_PASSWORD}
keydname: CN=feather
EOF
docker run --rm -e FDROID_KEYSTORE_PASSWORD -v "$PWD":/repo registry.gitlab.com/fdroid/docker-executable-fdroidserver:master update --create-metadata --pretty
ls repo/ | grep -E 'index-v1.jar|index-v2.json|entry.jar'; cat metadata/org.fdroid.fdroid.yml
```

**Verify**: last command prints all three filenames and a skeleton YAML with `Name: F-Droid`. The update log contains `INFO: Finished`. If the container errors on the keystore, `{env: …}` syntax, or the run does not produce `entry.jar` → STOP (the image changed since the plan was written).

### Step 1: Dependencies

Add to `requirements.txt` (keep alphabetical-ish grouping; put them after `waitress`):
```
pyaxmlparser==0.3.31
PyYAML==6.0.3
```
Run the wheel check from "Commands you will need" for **both** 311 and 314. Install into your venv.

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -c "import pyaxmlparser, yaml; print('ok')"` → `ok`; full suite still `244 passed`.

### Step 2: Android constants + APK inspection in `app.py`

Directly after `BACKUP_FOLDER = …` (line 75) add:

```python
# Plan 073: Android / F-Droid repository. Everything lives under
# DATA_DIR/fdroid, which is ALSO the working directory of the fdroid-index
# sidecar (compose service, profile "android"). Layout inside it:
#   repo/          APKs + the signed index the sidecar writes (served at /fdroid/repo/)
#   metadata/      one <package>.yml per app, written by feather
#   repo-config.json     repo name/description, written by feather, read by the sidecar
#   .update-requested    marker: feather touches it, the sidecar consumes it
#   last-update.json     sidecar's last result {ok, finished_at, log_tail}
#   fingerprint.txt      64-hex SHA-256 of the signing cert, written by the sidecar
#   config.yml / keystore.p12   owned by the sidecar (root); feather never reads them
FDROID_DIR = os.path.join(DATA_DIR, "fdroid")
FDROID_REPO_DIR = os.path.join(FDROID_DIR, "repo")
FDROID_METADATA_DIR = os.path.join(FDROID_DIR, "metadata")
FDROID_REPO_CONFIG = os.path.join(FDROID_DIR, "repo-config.json")
FDROID_UPDATE_MARKER = os.path.join(FDROID_DIR, ".update-requested")
FDROID_LAST_UPDATE = os.path.join(FDROID_DIR, "last-update.json")
FDROID_FINGERPRINT = os.path.join(FDROID_DIR, "fingerprint.txt")
ALLOWED_APK_EXTENSIONS = {'apk'}
# Android package names: Java identifiers separated by dots, at least two segments.
ANDROID_PACKAGE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$')
```
`app.py` does **not** import `re` today and its Flask import line (`app.py:1`) is `from flask import Flask, render_template, request, jsonify, send_file, redirect, session` — add `import re` with the stdlib imports and append `send_from_directory` to that Flask import.

Add, near `_extract_ipa_icon` (line 2165) so the two inspectors sit together:

```python
def _inspect_apk(path):
    """Read identity and version out of an APK's binary AndroidManifest.

    Returns a dict {package, version_code (int), version_name, min_sdk,
    target_sdk, app_name} or raises ValueError with an operator-readable
    message. pyaxmlparser returns version_code as a *string*; it is
    converted here so callers never compare "10" < "9".
    """
    from pyaxmlparser import APK  # imported lazily: keeps app import fast for tests
    try:
        apk = APK(path)
    except Exception as e:
        raise ValueError(f"Not a readable APK: {e}")
    if not apk.is_valid_APK():
        raise ValueError("Not a valid APK (no AndroidManifest.xml)")
    package = apk.package or ""
    if not ANDROID_PACKAGE_RE.match(package):
        raise ValueError(f"APK declares an invalid package name: {package!r}")
    try:
        version_code = int(apk.version_code)
    except (TypeError, ValueError):
        raise ValueError(f"APK declares a non-integer versionCode: {apk.version_code!r}")
    if version_code <= 0:
        raise ValueError(f"APK declares versionCode {version_code}; must be > 0")
    return {
        "package": package,
        "version_code": version_code,
        "version_name": apk.version_name or str(version_code),
        "min_sdk": apk.get_min_sdk_version(),
        "target_sdk": apk.get_target_sdk_version(),
        "app_name": apk.get_app_name() or package,
    }
```

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -c "import app; print(app._inspect_apk('/tmp/F-Droid.apk'))"` → dict with `'package': 'org.fdroid.fdroid', 'version_code': 1023052, 'version_name': '1.23.2'`.

### Step 3: `AndroidRepoManager`

Add the class after `SourceManager` (i.e. before `def requires_auth`, line 1680). Instantiate it at module scope right after `source_manager = SourceManager(SOURCE_FILE)` (`app.py:1662`), as `android_repo = AndroidRepoManager()`.

Required behaviour (write it; the shape below is the contract, not every line):

```python
class AndroidRepoManager:
    """Feather's side of the F-Droid repo. It owns repo/*.apk, metadata/*.yml
    and repo-config.json; the fdroid-index sidecar owns everything else
    under FDROID_DIR. Every mutation ends with request_update(), which is
    the only signal the sidecar listens for. Single-process lock, same
    caveat as SourceManager."""

    METADATA_KEYS = ("Name", "Summary", "Description", "AuthorName", "WebSite", "SourceCode", "License", "Categories")

    def __init__(self):
        self._lock = threading.Lock()
        os.makedirs(FDROID_REPO_DIR, exist_ok=True)
        os.makedirs(FDROID_METADATA_DIR, exist_ok=True)

    # --- paths -------------------------------------------------------------
    @staticmethod
    def apk_filename(package, version_code):
        return f"{package}_{int(version_code)}.apk"     # matches fdroid's own --rename-apks convention

    def apk_path(self, package, version_code): ...
    def metadata_path(self, package): return os.path.join(FDROID_METADATA_DIR, f"{package}.yml")

    # --- reads -------------------------------------------------------------
    def load_index(self):
        """repo/index-v1.json as written by the sidecar, or None if it has never run."""
    def list_apps(self):
        """Merge of metadata/*.yml (what feather intends) and index-v1.json
        (what is published). Returns a list of
        {package, name, summary, versions: [{versionCode, versionName, apkName, size, minSdkVersion, signer, published: bool}],
         pending: bool, mixed_signers: bool}
        `published` is True when index-v1.json lists that apkName.
        `pending` is True when the marker file exists or an APK on disk is not yet in the index.
        `mixed_signers` is True when index-v1.json shows more than one distinct
        `signer` for the package (the F-Droid client refuses such updates)."""
    def status(self):
        """{'configured': bool (fingerprint.txt exists), 'fingerprint': str|None,
            'repo_url': str, 'subscribe_url': str|None, 'pending': bool,
            'last_update': dict|None (contents of last-update.json), 'index_timestamp': int|None}"""
    def repo_config(self):
        """repo-config.json or defaults {'name': 'Feather Android', 'description': ''}"""

    # --- writes (all under self._lock, all end with request_update) --------
    def write_repo_config(self, name, description): ...
    def write_metadata(self, package, fields):
        """fields: subset of METADATA_KEYS. Merges into the existing yml.
        Enforce: Name ≤ 50, Summary ≤ 80, Description ≤ 4000, Categories is a list
        (default ['Feather']). Write with yaml.safe_dump(data, sort_keys=True,
        allow_unicode=True, default_flow_style=False) to a temp file in the same
        directory, then os.replace()."""
    def add_apk(self, src_path, expected_package=None):
        """Inspect src_path with _inspect_apk. If expected_package is given and
        differs -> ValueError. If the destination apk already exists -> return
        (False, 'already present') WITHOUT overwriting (idempotent, mirrors
        the iOS add-version behaviour). Else os.replace(src, dest); if that raises errno.EXDEV
        (compose.yml bind-mounts ./data/fdroid separately from ./data, so the
        staging dir and the repo dir can be different devices) fall back to
        shutil.copy2 + os.remove. Create metadata yml if
        missing, using app_name as Name. Return (True, info_dict)."""
    def delete_version(self, package, version_code): ...   # remove apk, request_update
    def delete_app(self, package): ...                       # remove all apks + yml, request_update
    def request_update(self):
        """touch FDROID_UPDATE_MARKER (open(..., 'a').close())"""
```

Path safety: every `package` reaching a path must pass `ANDROID_PACKAGE_RE.match`; every `version_code` must be `int(...)`. Refuse (ValueError) otherwise. Never build a path from a client-supplied filename.

**Verify**: `ADMIN_PASSWORD=x DATA_DIR=/tmp/fd-mgr .venv/bin/python -c "import app, shutil; shutil.copy('/tmp/F-Droid.apk','/tmp/x.apk'); print(app.android_repo.add_apk('/tmp/x.apk')); print(app.android_repo.list_apps()); import os; print(os.path.exists(app.FDROID_UPDATE_MARKER))"` → `(True, {...'package': 'org.fdroid.fdroid'...})`, one app with one unpublished version, `True`.
Then `cat /tmp/fd-mgr/fdroid/metadata/org.fdroid.fdroid.yml` → contains `Name: F-Droid` and `Categories:\n- Feather`.

### Step 4: Routes

Add after the `/api/diagnostics` route (line 3265) and before the error handlers. Public (no auth):

| Route | Behaviour |
|---|---|
| `GET /fdroid/repo/<path:filename>` | `send_from_directory(FDROID_REPO_DIR, filename)`; 404 JSON if missing. `.apk` → `mimetype='application/vnd.android.package-archive'`, `.jar` → `application/java-archive`, `.json` → `application/json`, else let Flask guess. Must stay **public** — the F-Droid client sends no credentials. |
| `GET /fdroid/qr` | PNG QR of `status()['subscribe_url']`; if the repo is not configured yet (no fingerprint) return `503 {"error": "F-Droid repo not initialised — start the fdroid-index service"}`. |

Session-gated (`@requires_auth`):

| Route | Body | Behaviour |
|---|---|---|
| `GET /api/android/status` | — | `android_repo.status()` |
| `GET /api/android/apps` | — | `android_repo.list_apps()` |
| `POST /api/android/add-apk` | multipart: `apkFile` **or** (`downloadFromUrl=true` + `downloadURL`); optional `package`, `name`, `summary`, `description`, `authorName`, `website`, `sourceCode`, `license` | Stage the file under `UPLOAD_FOLDER` (`tempfile.mkstemp(suffix='.apk')`); for URLs reuse the streaming + `MAX_CONTENT_LENGTH` pattern from `SourceManager.download_ipa_from_url` (`app.py:967-1010`, `SourceManager.download_ipa_from_url`) — write it as a small module-level helper `_download_to_temp(url, suffix)` rather than calling the IPA method. Reject any filename not ending `.apk` (400). Call `add_apk`, then `write_metadata` with whichever metadata fields were supplied. On `ValueError` → 400 with the message. Always delete the temp file. Response `{"success": true, "message": "…", "package": …, "versionCode": …, "pending": true}` |
| `POST /api/android/update-app` | JSON `{package, name?, summary?, description?, authorName?, website?, sourceCode?, license?, categories?}` | `write_metadata` |
| `POST /api/android/delete-version` | JSON `{package, versionCode}` | 404 if absent |
| `POST /api/android/delete-app` | JSON `{package}` | 404 if absent |
| `POST /api/android/repo-config` | JSON `{name, description}` | `write_repo_config` |
| `POST /api/android/request-update` | — | `request_update()` — lets the operator force a rebuild |

`resolve_base_url()` supplies the origin for `repo_url = base + '/fdroid/repo'`; write it into `repo-config.json` on every `write_repo_config` and also on `add_apk` if the file is missing, so the sidecar always has a `repo_url` (the sidecar reads it from there — see Step 5).

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_android.py -q` (write the tests in Step 7 first if you prefer TDD; otherwise smoke by hand: run the app with `DATA_DIR=/tmp/fd-mgr`, `curl -o /dev/null -w '%{http_code}\n' localhost:5000/fdroid/repo/nope.apk` → `404`; `curl -w '%{http_code}\n' localhost:5000/api/android/status` → `401`).

### Step 5: The sidecar — `scripts/fdroid_index_loop.sh` + `Dockerfile.fdroid`

`scripts/fdroid_index_loop.sh` (POSIX `sh`, the image has no bash guarantee):

```sh
#!/bin/sh
# Plan 073: fdroid-index sidecar loop. Runs as root inside the official
# fdroidserver image (its /home/vagrant is 0700, so no other uid can run
# `fdroid`). Owns config.yml + keystore.p12; feather (uid FEATHER_UID,
# default 999) owns repo/ and metadata/ and only ever reads the index.
set -eu
: "${FDROID_KEYSTORE_PASSWORD:?FDROID_KEYSTORE_PASSWORD is required (the repo signing key password)}"
INTERVAL="${FDROID_UPDATE_INTERVAL:-15}"
FEATHER_UID="${FEATHER_UID:-999}"
cd /repo
. /etc/profile.d/bsenv.sh
FDROID="$fdroidserver/fdroid"

mkdir -p repo metadata
if [ ! -f keystore.p12 ]; then
  keytool -genkeypair -keystore keystore.p12 -storetype PKCS12 -alias feather \
    -keyalg RSA -keysize 4096 -validity 10000 -storepass "$FDROID_KEYSTORE_PASSWORD" \
    -dname "CN=feather" >/dev/null
  chmod 600 keystore.p12
fi
keytool -exportcert -keystore keystore.p12 -alias feather -storepass "$FDROID_KEYSTORE_PASSWORD" 2>/dev/null \
  | sha256sum | cut -d' ' -f1 > fingerprint.txt.tmp && mv fingerprint.txt.tmp fingerprint.txt

render_config() {
  # repo-config.json is written by feather: {"name","description","repo_url"}
  python3 - <<'PY'
import json, os
cfg = {}
try:
    cfg = json.load(open('repo-config.json'))
except Exception:
    pass
def q(s):  # single-quoted YAML scalar
    return "'" + str(s).replace("'", "''") + "'"
lines = [
  'repo_url: ' + q(cfg.get('repo_url') or os.environ.get('FDROID_REPO_URL') or 'http://localhost:7000/fdroid/repo'),
  'repo_name: ' + q(cfg.get('name') or 'Feather Android'),
  'repo_description: ' + q(cfg.get('description') or ''),
  'repo_icon: icon.png',
  'archive_older: 0',
  'keystore: keystore.p12',
  'repo_keyalias: feather',
  'keystorepass: {env: FDROID_KEYSTORE_PASSWORD}',
  'keypass: {env: FDROID_KEYSTORE_PASSWORD}',
  'keydname: CN=feather',
]
open('config.yml.tmp', 'w').write('\n'.join(lines) + '\n')
os.replace('config.yml.tmp', 'config.yml')
PY
  chmod 600 config.yml
}

run_update() {
  rm -f .update-requested            # consume BEFORE running so a request during the run is not lost
  render_config
  START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if "$FDROID" update --create-metadata --pretty > /tmp/fdroid-update.log 2>&1; then OK=true; else OK=false; fi
  # hand the outputs back to feather's uid; keystore/config stay root-only
  chown -R "$FEATHER_UID:$FEATHER_UID" repo metadata fingerprint.txt
  python3 - "$OK" "$START" <<'PY'
import json, sys, os, collections
ok = sys.argv[1] == 'true'
tail = collections.deque(open('/tmp/fdroid-update.log', errors='replace'), maxlen=40)
json.dump({'ok': ok, 'started_at': sys.argv[2], 'finished_at': __import__('datetime').datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
           'log_tail': ''.join(tail)}, open('last-update.json.tmp', 'w'))
os.replace('last-update.json.tmp', 'last-update.json')
PY
  chown "$FEATHER_UID:$FEATHER_UID" last-update.json
  tail -n 3 /tmp/fdroid-update.log
}

run_update                            # always rebuild once at start-up
while true; do
  if [ -f .update-requested ]; then run_update; fi
  sleep "$INTERVAL"
done
```

Note: the log tail is stored verbatim; `fdroid update` never prints the keystore password. Do not add any echo of `FDROID_KEYSTORE_PASSWORD`.

`Dockerfile.fdroid`:
```dockerfile
# Plan 073: the fdroid-index sidecar. The official fdroidserver image plus
# one loop script. Runs as root by necessity -- see scripts/fdroid_index_loop.sh.
FROM registry.gitlab.com/fdroid/docker-executable-fdroidserver:master
COPY scripts/fdroid_index_loop.sh /usr/local/bin/fdroid-index-loop
RUN chmod 755 /usr/local/bin/fdroid-index-loop
WORKDIR /repo
ENTRYPOINT ["/bin/sh", "/usr/local/bin/fdroid-index-loop"]
```

`.dockerignore`: add `!scripts/fdroid_index_loop.sh` under the existing script re-admits, with a comment naming Plan 073.

**Verify**:
```bash
docker build -f Dockerfile.fdroid -t feather-fdroid:dev .
rm -rf /tmp/fd-side && mkdir -p /tmp/fd-side/repo /tmp/fd-side/metadata && cp /tmp/F-Droid.apk /tmp/fd-side/repo/org.fdroid.fdroid_1023052.apk
echo '{"name":"Spike","description":"d","repo_url":"https://example.test/fdroid/repo"}' > /tmp/fd-side/repo-config.json
docker run --rm -e FDROID_KEYSTORE_PASSWORD=spike-not-a-real-password -e FEATHER_UID=$(id -u) -e FDROID_UPDATE_INTERVAL=2 -v /tmp/fd-side:/repo --name fdspike -d feather-fdroid:dev
sleep 25; touch /tmp/fd-side/.update-requested; sleep 6
ls -l /tmp/fd-side/repo/index-v1.jar /tmp/fd-side/fingerprint.txt; cat /tmp/fd-side/last-update.json | head -c 200; echo; wc -c < /tmp/fd-side/fingerprint.txt
stat -c '%u' /tmp/fd-side/repo/index-v1.jar; docker stop fdspike
```
→ `index-v1.jar` exists, `last-update.json` has `"ok": true`, `fingerprint.txt` is 65 bytes (64 hex + newline), the index file's owner is your uid (i.e. the chown worked), and the marker file is gone.

### Step 6: Compose, env, Jenkins

`compose.yml` — add to `altstore-manager.volumes` a line `- ./data/fdroid:/app/data/fdroid` (it is already inside `./data:/app/data`; the explicit line matches how `ipas` and `icons` are declared and documents the shared directory). Add the service:

```yaml
  # Plan 073: builds and signs the F-Droid index for the Android repo.
  # Opt-in (profile "android"). Shares ./data/fdroid with feather: feather
  # writes APKs + metadata and touches .update-requested; this service runs
  # `fdroid update` and writes the signed index feather serves at
  # /fdroid/repo/. Runs as root INSIDE the container by necessity (the
  # upstream image is not usable by any other uid); it has no ports and
  # its only mount is this one directory.
  fdroid-index:
    profiles: ["android"]
    image: ghcr.io/d7eeem/feather-fdroid:latest
    build:
      context: .
      dockerfile: Dockerfile.fdroid
    container_name: feather-fdroid-index
    depends_on:
      - altstore-manager
    env_file:
      - .env
    environment:
      - FEATHER_UID=999
    volumes:
      - ./data/fdroid:/repo
    restart: unless-stopped
```

`.env.example` — append, in the repo's commented style:
```
# Added by plan 073 -- Android / F-Droid repository (compose profile
# "android", service fdroid-index). FDROID_KEYSTORE_PASSWORD protects the
# repo signing key (data/fdroid/keystore.p12, generated on first start).
# LOSING THE KEYSTORE OR ITS PASSWORD MEANS EVERY SUBSCRIBED DEVICE MUST
# RE-ADD THE REPO -- back up data/fdroid/keystore.p12. The sidecar refuses
# to start without the password. FDROID_UPDATE_INTERVAL (seconds, default
# 15) is how often it looks for feather's rebuild request.
FDROID_KEYSTORE_PASSWORD=
FDROID_UPDATE_INTERVAL=
```

`Jenkinsfile` — add `FDROID_IMAGE = 'ghcr.io/d7eeem/feather-fdroid'` to `environment`, and after the bot stages add `Build & push fdroid image` (same three tags, `-f Dockerfile.fdroid`) and `Smoke-test fdroid image`:
```groovy
    stage('Smoke-test fdroid image') {
      when { branch 'main' }
      steps {
        // Must refuse to run without the keystore password, naming it.
        sh '''
          set -eu
          OUT="$(docker run --rm "$FDROID_IMAGE:main" 2>&1 || true)"
          echo "$OUT" | grep -q "FDROID_KEYSTORE_PASSWORD" \
            || { echo "FAIL: did not name FDROID_KEYSTORE_PASSWORD"; exit 1; }
          echo "ok: refuses to run unconfigured"
        '''
      }
    }
```

`README.md` — add an "Android / F-Droid repository" subsection under "Optional features": enable with `COMPOSE_PROFILES=android` (Dockge cannot pass `--profile` — HANDOFF trap), set `FDROID_KEYSTORE_PASSWORD`, `mkdir -p data/fdroid && chown -R 999:999 data/fdroid` (same reason as the top-level `chown`), first start generates the keystore, back it up, subscribe via the Android tab's QR. Add the two public routes to the Routes table (`GET /fdroid/repo/<path>`, `GET /fdroid/qr`) and state that they must never require auth.

**Verify**: `docker compose config --profile android >/dev/null` → exit 0; `docker compose config | grep -c fdroid-index` → `0` (not started by default); `grep -c FDROID_KEYSTORE_PASSWORD .env.example Jenkinsfile scripts/fdroid_index_loop.sh` → each ≥ 1.

### Step 7: Tests — `tests/test_android.py`

Reuse the fixtures by importing them: `from tests.test_routes import client, authed_client, TEST_ADMIN_PASSWORD  # noqa: F401` (pytest discovers fixtures imported into the module namespace). Because a real APK cannot be synthesised in a test, **monkeypatch `app_module._inspect_apk`** to return a fixed dict for `b"fake-apk-bytes"` input, e.g.:

```python
FAKE = {"package": "org.example.demo", "version_code": 42, "version_name": "4.2", "min_sdk": 23, "target_sdk": 34, "app_name": "Demo"}

def fake_inspect(path):
    assert open(path, "rb").read() == b"fake-apk-bytes"
    return dict(FAKE)
```
and `monkeypatch.setattr(authed_client.app_module, "_inspect_apk", fake_inspect)`.

Cases (one test each, names indicative):
1. `test_fdroid_repo_route_is_public_and_404s_when_missing` — unauthenticated `GET /fdroid/repo/index-v1.jar` → 404 JSON (not 401).
2. `test_fdroid_repo_serves_file_with_apk_mimetype` — write `repo/org.example.demo_42.apk` into `tmp_path/fdroid/repo`, GET it → 200, `Content-Type` starts with `application/vnd.android.package-archive`.
3. `test_fdroid_repo_rejects_traversal` — `GET /fdroid/repo/../repo-config.json` → 404 (Flask normalises) and `GET /fdroid/repo/..%2Frepo-config.json` → 404.
4. `test_android_api_requires_auth` — every `/api/android/*` route → 401 unauthenticated.
5. `test_add_apk_upload_writes_apk_metadata_and_marker` — multipart upload of `b"fake-apk-bytes"` as `demo.apk` with `name=Demo`, `summary=Hi` → 200; `fdroid/repo/org.example.demo_42.apk` exists; `metadata/org.example.demo.yml` parses (`yaml.safe_load`) with `Name == 'Demo'`, `Summary == 'Hi'`, `Categories == ['Feather']`; `.update-requested` exists.
6. `test_add_apk_rejects_non_apk_filename` — upload as `demo.ipa` → 400, nothing written.
7. `test_add_apk_package_mismatch_400` — `package=com.other.app` in the form → 400 mentioning both names; no file written.
8. `test_add_apk_idempotent_on_existing_version` — upload twice → second is 200 with `"already present"` in `message`; file unchanged (compare mtime or content).
9. `test_add_apk_download_from_url_never_fetched_without_flag` — mirror `test_add_version_downloadurl_never_fetched_without_download_flag` (`tests/test_routes.py:946`): `downloadURL` set but flag absent → 400 and the loopback stub server received zero requests.
10. `test_metadata_length_limits` — `summary` of 81 chars → 400; `name` of 51 chars → 400.
11. `test_list_apps_merges_index_and_disk` — write a hand-made `repo/index-v1.json` listing `org.example.demo_42.apk` with `signer: "aaa"`, plus a second on-disk APK `org.example.demo_43.apk` not in the index → `GET /api/android/apps` shows two versions, `published` true/false respectively, `pending` true.
12. `test_list_apps_flags_mixed_signers` — index with two packages entries whose `signer` differ → `mixed_signers` true.
13. `test_delete_version_and_app` — round trip; deleting the last version keeps the yml (delete-version) vs removes it (delete-app); marker touched.
14. `test_status_unconfigured_then_configured` — no `fingerprint.txt` → `configured` false, `subscribe_url` null, `GET /fdroid/qr` → 503; write a 64-hex fingerprint → `subscribe_url == base + '/fdroid/repo?fingerprint=' + hex`, `GET /fdroid/qr` → 200 `image/png`.
15. `test_repo_config_roundtrip` — POST name/description → `repo-config.json` has them and `repo_url` ending in `/fdroid/repo`.
16. `test_inspect_apk_real_file_if_available` — `pytest.skip` unless `FEATHER_TEST_APK` env var points to a file; then assert `_inspect_apk` returns `package == 'org.fdroid.fdroid'`. (Runs only on a developer machine with `/tmp/F-Droid.apk`; CI skips it.)

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_android.py -q` → `16 passed` (15 + 1 skipped without the env var is also acceptable — say which). Full suite → `244 + 16 = 260` (or `259 passed, 1 skipped`).

### Step 8: UI — the Android tab

In `templates/index.html`:

1. Add a panel `<div id="android" class="tab-content">` after the `health` panel (line ~914), with three blocks:
   - **Repo** — status line (`configured`/`pending`/last update ok + time, from `/api/android/status`), `<img src="/fdroid/qr">` (hide when unconfigured, show the 503 hint instead), the subscribe URL in a `.source-url` box, a copy button, and a small form for repo name/description → `POST /api/android/repo-config`, plus a "Rebuild index" button → `POST /api/android/request-update`.
   - **Add APK** — form `id="addApkForm"`: file input `apkFile` (`accept=".apk"`) with the same "Use URL instead" checkbox pattern as the IPA form (copy `toggleDownloadMethod()` as `toggleApkDownloadMethod()`), optional `package`, `name`, `summary` (maxlength 80), `description` (maxlength 4000), `authorName`, `website`, `sourceCode`, `license`. Submit handler modelled on the add-app handler at ~1240: `FormData`, `fetch('/api/android/add-apk', {method:'POST', body})`, `showToast` on result, reload the list.
   - **Apps** — list from `/api/android/apps`: per app name/package/summary, per version `versionName (versionCode)`, size, a `published`/`pending` badge, a warning badge when `mixed_signers`, delete-version and delete-app buttons (confirm dialogs like the iOS list). Use `escapeHtml()` (already defined) on every string from the API.
2. Add a tab button in `<nav class="tabbar">` after Health: `<button class="tab" onclick="switchTab('android', this)" aria-label="Android" title="Android">` with an inline SVG in the same stroke style as its neighbours (a simple robot-head outline is fine — 24×24 viewBox, `stroke-width="1.8"`).
3. In `switchTab()` add `else if (tabName === 'android') { loadAndroid(); }` where `loadAndroid()` fetches status + apps.

Keep to the existing CSS classes (`form-group`, `btn`, `source-url`, `info-box`, badges used by the iOS list); no new colours, no emoji (regression guard `EMOJI` in `tests/test_routes.py`).

**Verify**: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → all pass (the emoji guard and template-render tests cover the HTML). Run the app locally (`DATA_DIR=/tmp/fd-mgr ADMIN_PASSWORD=x .venv/bin/python app.py`), log in, open the Android tab, upload `/tmp/F-Droid.apk` → toast success, app appears as *pending*.

### Step 9: End-to-end with Compose (manual, local)

```bash
cp .env.example /tmp/feather-e2e.env   # fill ADMIN_PASSWORD, FDROID_KEYSTORE_PASSWORD (throwaway values), PUBLIC_BASE_URL=http://localhost:7000
mkdir -p /tmp/feather-e2e-data/fdroid && chown -R 999:999 /tmp/feather-e2e-data
```
Point a scratch copy of `compose.yml` at that data dir and env file (or run in a scratch directory with `data/` symlinked), then `COMPOSE_PROFILES=android docker compose up -d --build`. Upload the real APK via the UI, wait ≤ `FDROID_UPDATE_INTERVAL`+ a few seconds, then:

```bash
curl -fsS -o /dev/null -w '%{http_code} %{content_type}\n' http://localhost:7000/fdroid/repo/index-v1.jar     # 200 application/java-archive
curl -fsS http://localhost:7000/fdroid/repo/index-v1.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(list(d['packages']))"   # ['org.fdroid.fdroid']
curl -fsS -o /dev/null -w '%{http_code} %{content_type}\n' http://localhost:7000/fdroid/qr                     # 200 image/png
```
If an Android device with the F-Droid client is available, scan the QR: the repo must be added with the fingerprint pre-filled and the app must appear after a refresh. Record the result (or "no device available") in your report.

**Verify**: the three curl lines above match; `docker compose ps` shows `feather-fdroid-index` `running`.

## Test plan

See Step 7: 16 tests in a new `tests/test_android.py`, modelled on `tests/test_routes.py` (`test_add_app_then_delete_app_round_trip`, `test_add_version_skips_existing_version`, `test_add_version_downloadurl_never_fetched_without_download_flag`). `_inspect_apk` is monkeypatched because a valid binary `AndroidManifest.xml` cannot be authored in a test; the sidecar contract is covered by the Step 5 container check and the Jenkins smoke stage, not by pytest.

## Done criteria

ALL must hold:

- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → `260 passed` (or `259 passed, 1 skipped`); zero existing tests modified (`git diff --stat -- tests/test_routes.py tests/test_storage.py tests/test_*ingest*.py tests/test_import_release.py tests/test_auto_import.py tests/test_migrate_icons.py` is empty)
- [ ] `grep -n "def _inspect_apk\|class AndroidRepoManager\|/fdroid/repo/<path:filename>\|/fdroid/qr\|/api/android/" app.py` shows the function, the class, and all eight routes
- [ ] `grep -c "@requires_auth" app.py` → `25` (17 today + the eight `/api/android/*` routes listed in Step 4; an earlier draft said 24 — the table is authoritative) (the seven `/api/android/*` routes); `/fdroid/repo/…` and `/fdroid/qr` are not decorated
- [ ] `grep -n "pyaxmlparser==0.3.31\|PyYAML==6.0.3" requirements.txt` → both present; the 311 + 314 wheel check passes
- [ ] `docker build -f Dockerfile.fdroid .` succeeds; running it with no env prints `FDROID_KEYSTORE_PASSWORD is required`
- [ ] Step 5's container check produced `index-v1.jar`, `fingerprint.txt` (65 bytes) and `last-update.json` with `"ok": true`, files owned by `FEATHER_UID`
- [ ] `docker compose config | grep -c fdroid-index` → `0`; `COMPOSE_PROFILES=android docker compose config | grep -c fdroid-index` → `≥1`
- [ ] `grep -n "fdroid_index_loop.sh" .dockerignore` → re-admitted
- [ ] `grep -n "FDROID_IMAGE\|Dockerfile.fdroid" Jenkinsfile` → build + smoke stages present
- [ ] `.env.example` and `README.md` document `FDROID_KEYSTORE_PASSWORD`, the keystore backup warning, `COMPOSE_PROFILES=android`, the `chown` step, and the two public routes
- [ ] `git status --short` shows only in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- Step 0 fails: the image no longer accepts `{env: …}` in `config.yml`, no longer produces `entry.jar`/`index-v1.jar`, or `keytool` is missing. The sidecar contract is the foundation; report the exact log.
- The image starts working as a non-root uid, or stops working as root. Either changes the ownership design in Step 5 — report rather than adapt.
- `pyaxmlparser` fails to parse the real F-Droid APK (`/tmp/F-Droid.apk`) or `lxml`/`PyYAML` lacks a cp311 **or** cp314 wheel.
- Any change seems to be needed to `SourceManager`, `/source.json`, `/ipas/…`, `/icons/…`, `/qr`, or an existing test.
- You are tempted to make `/fdroid/repo/…` or `/fdroid/qr` require a session. They are public by contract, like the four iOS routes.
- `fdroid update` rejects an APK you expect to be valid (e.g. `Bad signature`, v1-only signature warnings escalated to errors). Report the log tail; do not disable signature checks.
- The Step 9 end-to-end shows the index building but the F-Droid client rejecting the repo ("index signature invalid" / fingerprint mismatch). Report — do not touch the signing configuration.

## Maintenance notes

- **Everything Android is derived from `data/fdroid/`** — APKs + `metadata/*.yml` are the source of truth, `repo/index-*.{json,jar}` are build outputs. Backing up `data/fdroid/keystore.p12` (and knowing `FDROID_KEYSTORE_PASSWORD`) is the one thing that cannot be regenerated: a new key means a new fingerprint and every device re-adds the repo.
- The sidecar runs as **root** inside its container. Reviewers should confirm it still has no `ports:`, mounts only `./data/fdroid`, and that the loop never echoes the password. If upstream ever fixes non-root execution, switch to `user: "999:999"` and drop the `chown` lines.
- **Signer consistency**: the F-Droid client refuses an update signed by a different key than the installed app. `mixed_signers` in `/api/android/apps` surfaces this after the fact; a pre-publish check needs certificate parsing feather does not do yet — that belongs to the Android counterpart of Plan 068 (shared preflight).
- **Follow-ups deliberately deferred** (write them as separate plans when wanted): 074 Telegram APK ingest (extend `scripts/telegram_bot_ingest.py`: `.apk` → `POST /api/android/add-apk`); 075 release-import for APK assets (`scripts/release_source_ingest.py` + a `platform: android` job field); 076 Garage/S3 for the F-Droid repo (`fdroid deploy`, or a post-update `rclone`/`aws s3 sync` step in the loop with `repo_url` pointing at the bucket); 077 Android health checks + provenance (fold into 069/071 once those land); a Garage-hosted repo also changes `repo_url`, which is baked into the signed index — rebuild required.
- `fdroid update` regenerates icons from each APK; feather does not manage Android icons. If custom icons are ever wanted, that is `metadata/<pkg>/en-US/icon.png` in fdroidserver's "fastlane" layout — not `data/icons/`.
- `archive_older: 0` keeps every version in the main repo. If the APK count grows large, set it to N and also serve `archive/` (a second route) — out of scope now.
- The `.update-requested` marker is consumed *before* the build runs, so a request arriving mid-build triggers a second build; that is intended.
