# Plan 076: Harden the fdroid-index sidecar — password off argv, minimal env, pinned base image, loud failures

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
> git diff --stat 5e8f447..HEAD -- scripts/fdroid_index_loop.sh Dockerfile.fdroid compose.yml .env.example README.md Jenkinsfile
> ```
> If any changed, compare the "Current state" excerpts against the live code
> before proceeding; on a mismatch, STOP.

## Status

- **Priority**: P1 — the signing key's password is visible in `docker top`, and the root sidecar receives every secret in `.env`
- **Effort**: S
- **Risk**: LOW — invocation and Compose changes; the sidecar's contract with feather (`.update-requested`, `last-update.json`, `fingerprint.txt`, `repo/`, `metadata/`) is unchanged
- **Depends on**: none. Independent of plan 075.
- **Category**: security
- **Planned at**: commit `5e8f447`, 2026-08-28

## Why this matters

The `fdroid-index` sidecar (plan 073) signs the F-Droid index every Android device trusts. It must run as root (the upstream image cannot run otherwise — decided, not in scope). What *is* in scope is what that root process can see and how it fails:

1. **Password on argv.** `keytool -genkeypair … -storepass "$FDROID_KEYSTORE_PASSWORD"` and, on *every* loop iteration, `keytool -exportcert … -storepass "$FDROID_KEYSTORE_PASSWORD"`. `argv` is readable by any process in the container and shows up in `docker top`, `ps` on the host, and crash dumps. The same script already passes the password to `fdroid` correctly via `keystorepass: {env: …}`.
2. **Whole `.env` in the sidecar.** `env_file: .env` gives the root container the admin password, Garage S3 keys, Telegram bot token/API hash, and GitHub/GitLab tokens — it needs exactly three variables. The `telegram-bot-api` service already shows the right pattern (explicit `environment:` list).
3. **Mutable base image.** `FROM …fdroidserver:master` — CI's `sha-<commit>` tag is not reproducible and upstream changes reach production with no commit here.
4. **Silent bad fingerprint.** `keytool -exportcert … 2>/dev/null | sha256sum | cut …` under POSIX `sh` (no `pipefail`): if keytool fails, `sha256sum` hashes empty input and the script publishes a plausible 64-hex fingerprint; feather then advertises a subscribe URL every client rejects.
5. **Crash loop on bad config.** `set -e` + `run_update` at start-up: a `repo-config.json` that parses to a non-dict makes the Python heredoc raise `AttributeError`, the script exits, Compose restarts it forever, and feather's UI shows "pending". A non-numeric `FDROID_UPDATE_INTERVAL` does the same at `sleep`.

## Current state

`scripts/fdroid_index_loop.sh` (76 lines) — the relevant parts, verbatim:

```sh
#!/bin/sh
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
  python3 - <<'PY'
import json, os
cfg = {}
try:
    cfg = json.load(open('repo-config.json'))
except Exception:
    pass
def q(s): ...
lines = [
  'repo_url: ' + q(cfg.get('repo_url') or os.environ.get('FDROID_REPO_URL') or 'http://localhost:7000/fdroid/repo'),
  ...
  'keystorepass: {env: FDROID_KEYSTORE_PASSWORD}',
  'keypass: {env: FDROID_KEYSTORE_PASSWORD}',
  ...
]
...
PY
  chmod 600 config.yml
}

run_update() {
  rm -f .update-requested
  render_config
  START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  if "$FDROID" update --create-metadata --pretty > /tmp/fdroid-update.log 2>&1; then OK=true; else OK=false; fi
  chown -R "$FEATHER_UID:$FEATHER_UID" repo metadata fingerprint.txt
  python3 - "$OK" "$START" <<'PY'
  ... writes last-update.json {ok, started_at, finished_at, log_tail} atomically ...
PY
  chown "$FEATHER_UID:$FEATHER_UID" last-update.json
  tail -n 3 /tmp/fdroid-update.log
}

run_update
while true; do
  if [ -f .update-requested ]; then run_update; fi
  sleep "$INTERVAL"
done
```

`Dockerfile.fdroid`:
```dockerfile
FROM registry.gitlab.com/fdroid/docker-executable-fdroidserver:master
COPY scripts/fdroid_index_loop.sh /usr/local/bin/fdroid-index-loop
RUN chmod 755 /usr/local/bin/fdroid-index-loop
WORKDIR /repo
ENTRYPOINT ["/bin/sh", "/usr/local/bin/fdroid-index-loop"]
```
The digest of the image that plan 073 was verified against (pulled 2026-08-26): `sha256:e58538106529863d504f686e3a51189aec1d24e17d9fee87048f7ae5c9cee706` (`fdroid --version` → `6af4c42`).

`compose.yml:119-133`:
```yaml
  fdroid-index:
    profiles: ["android"]
    image: ghcr.io/t1nk333r/feather-fdroid:latest
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
Exemplar of the explicit-environment pattern, `compose.yml:46-49` (`telegram-bot-api`):
```yaml
    environment:
      - TELEGRAM_API_ID=${TELEGRAM_API_ID:-}
      - TELEGRAM_API_HASH=${TELEGRAM_API_HASH:-}
      - TELEGRAM_LOCAL=true
```

`app.py:1825-1834` — feather treats any non-empty `fingerprint.txt` as `configured: true`.

`Jenkinsfile:156-168` — the fdroid smoke stage asserts only that an unconfigured run names `FDROID_KEYSTORE_PASSWORD`.

Keytool facts (JDK 17+ in the image): `-storepass:env VARNAME` reads the password from an environment variable instead of argv; `-storepass:file PATH` reads it from a file. Both are supported by `-genkeypair` and `-exportcert`.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Build sidecar | `docker build -f Dockerfile.fdroid -t feather-fdroid:dev .` | exit 0 |
| Unconfigured refusal | `docker run --rm feather-fdroid:dev; echo $?` | prints the `FDROID_KEYSTORE_PASSWORD is required` line, non-zero exit |
| Real APK for the container check | `curl -sSL -o /tmp/F-Droid.apk https://f-droid.org/F-Droid.apk` (skip if present) | ~12 MB |
| Compose validity | `printf 'ADMIN_PASSWORD=x\nFDROID_KEYSTORE_PASSWORD=x\n' > /tmp/ci.env; docker compose --env-file /tmp/ci.env --profile android config >/dev/null` | exit 0 |
| App tests (unchanged) | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | `259 passed, 1 skipped` |

## Scope

**In scope**: `scripts/fdroid_index_loop.sh`, `Dockerfile.fdroid`, `compose.yml` (the `fdroid-index` service only), `.env.example` (comment lines for plan 073's block), `README.md` (the Android subsection: one paragraph on the pin-refresh policy and the rotation note), `Jenkinsfile` (the fdroid smoke stage only).

**Out of scope — do NOT touch**: `app.py` (plan 075 owns the feather side; the fingerprint-format check there is *its* follow-up — record it in NOTES if you think it matters); `release-import`'s `env_file` (a similar narrowing is worthwhile but is a separate change — mention it in NOTES); running the sidecar as non-root; `Dockerfile`, `Dockerfile.bot`.

## Git workflow

- Branch: `advisor/076-fdroid-sidecar-hardening`; one commit per step, e.g. `fix(fdroid): keytool reads the password from env, not argv (plan 076)`.
- Do not push.

## Steps

### Step 1: Password off the command line

Replace both `-storepass "$FDROID_KEYSTORE_PASSWORD"` occurrences with `-storepass:env FDROID_KEYSTORE_PASSWORD` (the variable is already exported into the process environment by Compose; add `export FDROID_KEYSTORE_PASSWORD` right after the `:?` check to be explicit).

**Verify**: `command grep -n 'storepass "' scripts/fdroid_index_loop.sh` → no hits; `command grep -c 'storepass:env FDROID_KEYSTORE_PASSWORD' scripts/fdroid_index_loop.sh` → `2`. Then the container check in Step 6 proves keytool accepts it.

### Step 2: Fingerprint step fails loudly

Replace the pipeline with discrete, checked commands:

```sh
compute_fingerprint() {
  rm -f cert.der.tmp
  if ! keytool -exportcert -keystore keystore.p12 -alias feather -storepass:env FDROID_KEYSTORE_PASSWORD -file cert.der.tmp; then
    echo "fdroid-index: keytool -exportcert failed (wrong FDROID_KEYSTORE_PASSWORD or damaged keystore.p12)" >&2
    return 1
  fi
  if [ ! -s cert.der.tmp ]; then
    echo "fdroid-index: exported certificate is empty" >&2
    return 1
  fi
  sha256sum cert.der.tmp | cut -d' ' -f1 > fingerprint.txt.tmp
  rm -f cert.der.tmp
  mv fingerprint.txt.tmp fingerprint.txt
}
```
Call `compute_fingerprint || exit 1` at start-up (a wrong password must stop the container with a readable reason — that *is* the loud failure; Compose's restart loop will surface it in `docker compose ps`/logs, unlike the silent hash of nothing). Do not redirect keytool's stderr to `/dev/null`.

**Verify**: run the built image with a keystore generated under password A and `FDROID_KEYSTORE_PASSWORD=B` → container exits non-zero, log contains `keytool -exportcert failed`, and `fingerprint.txt` is **not** written (or unchanged if it existed).

### Step 3: `run_update` and the loop survive bad input

- In `render_config`'s Python: after the `json.load`, add `if not isinstance(cfg, dict): cfg = {}`.
- Validate `INTERVAL` at start-up: `case "$INTERVAL" in ''|*[!0-9]*) echo "FDROID_UPDATE_INTERVAL must be a positive integer (got '$INTERVAL')" >&2; exit 1;; esac`.
- Make `run_update` never kill the loop: wrap its body so that a failure in `render_config` or the status-writing heredoc records `ok:false` with the error text in `last-update.json` (reuse the existing writer: pass `OK=false` and put the error in `/tmp/fdroid-update.log`) and returns 0. Concretely: `render_config || { echo "render_config failed" > /tmp/fdroid-update.log; OK=false; }` before the `fdroid update` call, skipping the update when `OK=false`.

**Verify**: with `repo-config.json` containing `[]`, the container starts, `last-update.json` is written (either `ok:true` using defaults — acceptable, since a non-dict is now treated as empty — or `ok:false` with a message), and the container is still running after 2× `FDROID_UPDATE_INTERVAL`. With `FDROID_UPDATE_INTERVAL=abc` the container exits immediately naming the variable.

### Step 4: Minimal environment in Compose

Replace `env_file: - .env` on `fdroid-index` with:

```yaml
    environment:
      - FDROID_KEYSTORE_PASSWORD=${FDROID_KEYSTORE_PASSWORD:?FDROID_KEYSTORE_PASSWORD must be set in .env}
      - FDROID_UPDATE_INTERVAL=${FDROID_UPDATE_INTERVAL:-15}
      - FDROID_REPO_URL=${PUBLIC_BASE_URL:-}/fdroid/repo
      - FEATHER_UID=999
```
(`FDROID_REPO_URL` is the sidecar's fallback when feather has not yet written `repo-config.json`; deriving it from `PUBLIC_BASE_URL` means a fresh deployment never signs `localhost` into the index. When `PUBLIC_BASE_URL` is unset this yields `/fdroid/repo`, which the script must treat as unset — add `[ "${FDROID_REPO_URL:-}" = "/fdroid/repo" ] && unset FDROID_REPO_URL` after the `:?` line.)

Note in the comment above the service that the `${VAR:?}` form makes `docker compose up` fail with a clear message only when the `android` profile is active.

**Verify**: `docker compose --env-file /tmp/ci.env --profile android config | command grep -A6 'fdroid-index:' | command grep -c environment` → `1`; the rendered config for `fdroid-index` shows exactly four environment keys and no `ADMIN_PASSWORD`/`GARAGE_*`/`TELEGRAM_*`; `docker compose --env-file /tmp/ci.env config` (no profile) still succeeds.

### Step 5: Pin the base image by digest

`Dockerfile.fdroid`:
```dockerfile
# Pinned by digest -- the upstream tag is a moving `master`. Refresh policy:
# bump deliberately, re-run the plan-073 Step 0 spike (init + update against a
# real APK), then update the digest + the `fdroid --version` note below.
# tag: master, fdroid --version 6af4c42, pulled 2026-08-26
FROM registry.gitlab.com/fdroid/docker-executable-fdroidserver@sha256:e58538106529863d504f686e3a51189aec1d24e17d9fee87048f7ae5c9cee706
```
Add the refresh policy sentence to README's Android subsection.

**Verify**: `docker build -f Dockerfile.fdroid -t feather-fdroid:dev .` → exit 0 (the digest resolves).

### Step 6: Container end-to-end + Jenkins smoke

Re-run plan 073's Step 5 container check against the new image (scratch dir, real APK, `FEATHER_UID=$(id -u)`, `FDROID_UPDATE_INTERVAL=2`): after start-up plus one marker touch, `repo/index-v1.jar` exists, `fingerprint.txt` is 65 bytes of lowercase hex + newline, `last-update.json` has `"ok": true`, and **`docker top <container>` / `ps -o args` inside the container shows no password**: `docker exec <c> sh -c 'cat /proc/*/cmdline | tr "\0" " "' | command grep -c "<the test password>"` → `0` (use an obviously fake password like `spike-not-a-real-password`).

Extend the Jenkins fdroid smoke stage with a second assertion that the image rejects a non-numeric interval: run with `-e FDROID_KEYSTORE_PASSWORD=ci-not-real -e FDROID_UPDATE_INTERVAL=abc` and grep the output for `FDROID_UPDATE_INTERVAL`.

**Verify**: all of the above; `command grep -c FDROID_UPDATE_INTERVAL Jenkinsfile` → ≥1.

### Step 7: Docs + rotation note

In `.env.example`'s plan-073 block add: "The sidecar receives only `FDROID_*` and `FEATHER_UID` — never the rest of `.env`." In README's Android subsection add a short **Rotating the keystore password** paragraph: `docker compose run --rm --entrypoint sh fdroid-index -c 'keytool -storepasswd -keystore /repo/keystore.p12 -storepass:env FDROID_KEYSTORE_PASSWORD -new:env NEW_PASSWORD'` style instructions (verify the exact keytool flags in the image before writing them: `docker run --rm --entrypoint keytool <image> -storepasswd -help`), then update `.env`, then `docker compose up -d fdroid-index`. State explicitly that rotating the *password* keeps the key and fingerprint — devices are unaffected — whereas regenerating `keystore.p12` changes the fingerprint and forces every device to re-add the repo.

**Verify**: the flags you documented are the ones `keytool -storepasswd -help` printed.

## Test plan

No pytest changes (the sidecar is shell + Compose). Verification is the Step 2/3/6 container checks and the Jenkins smoke stage. Record each container check's output in your report.

## Done criteria

- [ ] `command grep -n 'storepass "' scripts/fdroid_index_loop.sh` → none; `-storepass:env` used twice
- [ ] `command grep -n "2>/dev/null" scripts/fdroid_index_loop.sh` → none
- [ ] `command grep -n "isinstance(cfg, dict)" scripts/fdroid_index_loop.sh` → one hit; interval validated with a `case`
- [ ] `command grep -n "env_file" compose.yml` shows no `env_file` under `fdroid-index`
- [ ] `command grep -n "@sha256:" Dockerfile.fdroid` → one hit; `docker build -f Dockerfile.fdroid .` succeeds
- [ ] Container check: index built, fingerprint 65 bytes, `last-update.json` ok, no password in any `/proc/*/cmdline`
- [ ] Wrong-password run exits non-zero naming keytool; `FDROID_UPDATE_INTERVAL=abc` exits naming the variable; `[]` repo-config does not crash-loop
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → `259 passed, 1 skipped` (unchanged)
- [ ] README documents the digest-refresh policy and password rotation; `.env.example` notes the minimal env
- [ ] `git status --short` shows only in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

- `keytool` in the pinned image rejects `-storepass:env` (older JDK) — report; the fallback is `-storepass:file` with a `0600` file on `tmpfs`, but that is a design change to confirm, not improvise.
- The digest above no longer resolves (image garbage-collected upstream) — pull `:master`, record its digest and `fdroid --version`, re-run plan 073's Step 0 spike, and report the new digest before pinning it.
- You need to change `app.py` to satisfy a criterion — report instead.

## Maintenance notes

- Because the password sat on argv from 2026-08-26 until this lands, treat any keystore generated by a *deployed* sidecar in that window as password-exposed: rotate with `keytool -storepasswd` (key and fingerprint preserved). A keystore generated only in the plan-073 executor's throwaway spike directories needs nothing.
- Digest refresh is manual by design; note the date and `fdroid --version` in the Dockerfile comment each time.
- `release-import` still gets `env_file: .env` and uses only `FEATHER_*`, `GITHUB_TOKEN`, `GITLAB_TOKEN`, `RELEASE_IMPORT_*` — same narrowing, separate change.
- Feather side (plan 075 follow-up, not here): `status()` should require `fingerprint.txt` to match `^[0-9a-f]{64}$` before reporting `configured: true`.
