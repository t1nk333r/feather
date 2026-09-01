# Handoff

Written 2026-08-12 against `1b75e2c`; refreshed 2026-08-31 against `3e0cd8a` and the live deployment. Read **Immediate blocker** and **Traps** first.

## What this is

A self-hosted iOS AltStore source and third-party Android F-Droid repository. It serves iOS and Android artifacts, provides an authenticated admin UI, and supports Telegram and repository-release ingestion.

- `app.py` — the Flask app (about 4,930 lines)
- `templates/index.html` — the frontend, extracted in Plan 009
- `scripts/telegram_bot_ingest.py` — the Telegram ingest worker
- `scripts/release_source_ingest.py` — GitHub/GitLab release importer
- `scripts/fdroid_index_loop.sh` — F-Droid signing/index loop
- `Dockerfile.bot`, `Dockerfile.fdroid` — optional service images
- `Jenkinsfile` — test, build, smoke, then publish pipeline
- `scripts/migrate_ipas_to_garage.py` — one-shot local-disk → Garage S3 migration
- `tests/` — latest CI result is 307 passed and 1 skipped on both supported Python versions; no real provider calls
- `plans/` — 81 numbered plans; `README.md` is the status index

**Test command** (the `ADMIN_PASSWORD` prefix is mandatory — the app refuses to import without it):

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

## State

All currently selected plans through 081 are implemented or explicitly rejected; see `plans/README.md` for historical statuses and deferred findings. The working tree was clean before this handoff-only edit. `main` is at `3e0cd8a` (`fix(android): encode QR as F-Droid deep link`).

Jenkins tests Python 3.11 and 3.14, checks the `requests` pin, builds and smokes all three images, then publishes `ghcr.io/t1nk333r/feather`, `feather-bot`, and `feather-fdroid` only after every smoke stage passes.

The latest observed successful Jenkins run for `3e0cd8a` passed 307 tests with 1 skip on each Python version and deployed the refreshed core image. The Android QR now encodes a standard `fdroidrepos://` deep link while the manual repository address remains HTTPS.

## Immediate blocker — restore the GHCR F-Droid image

The live Android repository is healthy, but a cold pull of its sidecar image is not:

```text
docker pull ghcr.io/t1nk333r/feather-fdroid:latest
Error response from daemon: Head "https://ghcr.io/v2/t1nk333r/feather-fdroid/manifests/latest": unauthorized
```

Verified on 2026-08-31:

- `feather` and `feather-bot` issue anonymous GHCR pull tokens (`HTTP 200`).
- `feather-fdroid` does not (`HTTP 401`), its public package page returns 404, and it is absent from the public package list.
- The operator reports that `feather-fdroid` is also absent while signed into the owning GitHub account. Treat the package as deleted, not merely private, unless the authenticated GitHub package view proves otherwise.
- Jenkins successfully pushed `feather-fdroid` tags in an earlier run. The live `feather-fdroid-index` container is therefore running a cached local image; this does **not** prove that the registry package still exists.
- The local GitHub CLI credential has repository/workflow scopes but no package scope, so it cannot inspect or repair this package. Do not print, copy, or replace any credential value.

Recovery sequence:

1. Run a Jenkins build of current `main`. The `Push images` stage at `Jenkinsfile:170-190` must push `feather-fdroid:main`, `:latest`, and `:sha-3e0cd8a` using the existing `ghcr-pat` Jenkins credential.
2. Confirm the Jenkins log contains a successful digest line for all three `feather-fdroid` tags and ends in `Finished: SUCCESS`. If the push reports `denied`, stop and repair the Jenkins credential's GHCR package-write permission; do not work around it with an untracked local image.
3. While signed into GitHub as `t1nk333r`, confirm the recreated package appears under the account's Packages tab. Make it public if anonymous deployment pulls are desired. GitHub treats public visibility as irreversible, so this remains an operator decision.
4. Verify anonymously from a Docker host that has no cached copy:

   ```bash
   docker pull ghcr.io/t1nk333r/feather-fdroid:latest
   ```

   Expected: pull succeeds without `docker login`. A cached-host `docker image inspect` is not sufficient verification.
5. In Dockhand, pull the stack images and redeploy the Feather stack without changing or regenerating `data/fdroid/keystore.p12` or `FDROID_KEYSTORE_PASSWORD`.
6. Run every Android check in **Verifying a deployment** below. The repository fingerprint before and after redeploy must be identical.

STOP and report instead of improvising if Jenkins succeeds but no authenticated package appears, the package owner is not `t1nk333r`, the signing fingerprint changes, or recovery appears to require deleting/recreating `data/fdroid`.

## Deployment

TrueNAS, managed through **Dockhand** at the Tailscale address `http://<tailnet-host>` (environment `truenas`, ID 1). The stack data is at `/path/to/feather`; the historical path name does not mean Dockge is still the control plane. Optional profiles add services:

| Container | Image | Notes |
|---|---|---|
| `altstore-manager` | `ghcr.io/t1nk333r/feather:latest` | the product; port 7000 → 5000 |
| `telegram-bot-api` | `aiogram/telegram-bot-api:latest` | self-hosted Bot API, `--local`; **no host port** |
| `ipa-ingest-bot` | `ghcr.io/t1nk333r/feather-bot:latest` | forward-to-bot worker |
| `feather-fdroid-index` | `ghcr.io/t1nk333r/feather-fdroid:latest` | profile `android`; root by necessity; mounts only `./data/fdroid` |

Public at `https://feather.example.com` (openresty on `<proxy-ip>`). Garage S3 at `https://s3.example.com`, bucket `feather-repo` served at `https://feather-repo.web.example.com`.

The custom Dockhand MCP bridge is installed locally under `~/.local/share/mcp-dockhand`, configured in `~/.codex/config.toml`, and run by `~/.config/systemd/user/mcp-dockhand.service`. Its environment file is `~/.config/mcp-dockhand/env` (mode 0600). The MCP bridge is bound to `127.0.0.1:8080` and separately bearer-protected. Never copy its bearer credential into this repository or a handoff.

**Update ritual:**
```bash
docker compose pull && docker compose up -d      # NOT --build
```
`--build` gives you a locally built image instead of the CI-verified one — same source, different artifact, silently bypassing what CI checked.

## Outstanding — operator tasks

Ordered by urgency. None of these are code.

1. **Rotate the Telegram bot token.** It has been exposed at least twice — in container logs before Plan 020's redaction landed, and in a directory listing (the Bot API names its work folder after the token). @BotFather → `/revoke`, then update `.env`. The folder name changes with it; harmless.
2. **Rotate `ADMIN_PASSWORD`.** It sat in a mode-0755 `.env` while the app had no auth at all, so it protected nothing. It is now load-bearing (Plan 010) and the app refuses to boot without it.
3. **Three catalog entries ship gzip-compressed HTML, not IPAs** — `com.instagram.theta 408.1.0_TH`, `com.instagram.ifgram 408.1.0_IF`, `com.fouadraheb.watusi B_25.36.10_WC`. 1,719–1,724 bytes each; decompressed they are a filebin landing page. Plan 007 stopped new ones; nothing repaired these. Either source real binaries or delete those versions from `data/source.json`.
4. **Three catalog bundle IDs disagree with their binaries** — `com.instagram.theta` and `com.instagram.ifgram` both ship `com.burbn.instagram`; `com.michael-128.qBitControl` ships `MikeMichael225.qBitControl`. May be deliberate (a renamed patched build installs beside the real app), but AltStore keys update-tracking on the bundle identifier, so it should be a decision rather than an accident.
5. **Garage is configured but not in use.** `STORAGE_BACKEND` defaults to `local`. To switch: fill the Garage keys, run the migration **dry-run first** (expect 8 uploadable / 3 corrupt-skipped / 2 orphans), then `--apply`, *then* set `STORAGE_BACKEND=garage`. Doing it in the other order makes every existing app un-installable on restart.
6. **`developerName` is `"Unknown"`** on anything the bot created (Plan 023's default), and `localizedDescription` is empty. Cosmetic; fix in the web UI.
7. **Enable and protect Android signing state.** Set `COMPOSE_PROFILES=android`, run `chown -R 999:999 data/fdroid`, and back up both `data/fdroid/keystore.p12` and `FDROID_KEYSTORE_PASSWORD`. Losing either changes the repository fingerprint for every subscriber.
8. **Restore the missing `feather-fdroid` GHCR package.** Follow **Immediate blocker** above. `feather` and `feather-bot` are already anonymously pullable; `feather-fdroid` is the only failing image.

## Traps

Each of these cost real debugging time. They are the reason this file exists.

**`command:` in compose does nothing for `aiogram/telegram-bot-api`.** Its entrypoint ends in `exec $COMMAND` and never forwards `"$@"`. Every option must come from a `TELEGRAM_*` env var — local mode is `TELEGRAM_LOCAL=true`, not `--local`. The `--http-port=8081` you see in the process list comes from the entrypoint's own default, which is exactly why the override *looks* like it works. **Verify against the command line the container logs at startup**, not against `compose.yml`.

**The worker must run as uid 101.** `telegram-bot-api` chowns its work dir to `telegram-bot-api` (uid 101); the worker image defaults to uid 999. Different uid means it cannot traverse into the token directory — and `os.path.exists()` returns `False` on permission-denied, so this surfaces misleadingly as `MountMismatchError: shared volume mounts disagree`. `user: "101:101"` on `ipa-ingest-bot` fixes it. Reproduced directly: same file, same mount, `False` as 999 and `True` as 101.

**Dockge cannot pass `--profile`.** Both Telegram services are opt-in (Plan 016). Use `COMPOSE_PROFILES=telegram` in `.env` instead — Compose reads it from the project `.env` and it does the same thing. Do **not** set it before the eight worker variables exist; the worker correctly refuses to start unconfigured and `restart: unless-stopped` turns that into a crash loop.

**The Dockge `compose.yml` and the repo's have diverged.** Nothing I change in the repo reaches the deployment automatically. Dockge's "From Git" option would fix that permanently; until then, hand-sync. `user: "101:101"` is currently in the Dockge copy but **not** in the repo's — worth reconciling.

**Every pin in `requirements.txt` needs wheels for both cp311 (the container) and cp314 (the test host).** This is not hypothetical — `pillow==10.1.0` publishes no cp314 wheel and stalled Plan 004. Check with `pip download --no-deps --only-binary=:all: --python-version 311` and again with `314`. The host's system `python3` has no `pip`; use a venv's.

**`getFile` is synchronous over the download in `--local` mode.** It blocks for the whole transfer, so the timeout must be sized to the file, not to an API call. It is configurable (`BOT_API_GETFILE_TIMEOUT`, default 900 s).

**Filenames lie about versions.** `YT_20.49.5_KP.ipa` declares `CFBundleShortVersionString = 20.47.3`. Never parse a version from a filename; read `Info.plist`.

**An IPA has an `Info.plist` per bundled framework** — 235 of them in the TikTok file. Matching `endswith("Info.plist")` returns a framework's identifier, plausible and wrong. The regex must be exactly `^Payload/[^/]+\.app/Info\.plist$`.

**Do not extract app icons from the IPA.** The declared icon is often only inside the compiled `Assets.car`, and what is loose is usually CgBI (byte-swapped BGRA, premultiplied alpha, raw-deflate IDAT) which neither browsers nor Pillow decode. Coverage would be ~1 in 3 for ~70 lines of PNG un-filtering. The Telegram thumbnail is used instead.

**`.dockerignore` is deny-by-default.** A new file a Dockerfile needs must be re-admitted with a `!` line, and you will find out via a failed build — the right failure mode. It exists so a careless `COPY . .` cannot leak `.env` or the 1.3 GB `data/`.

**A fresh `data/` bind mount is created as root**, and the container's non-root user cannot write to it — the app fails with `PermissionError` and serves nothing. `chown -R 999:999 data/` before the first start on a new host.

**The F-Droid sidecar must run as root.** Adding `user:` to that service breaks `fdroidserver` because its upstream image requires root-owned paths and setup.

**`./data/fdroid` is a separate bind mount.** Moving an uploaded APK into it may raise `EXDEV`; `AndroidRepoManager.add_apk` handles that with a hidden staged copy plus atomic replace.

**A running container does not prove its registry image still exists.** Docker can continue running a cached `feather-fdroid` image after the GHCR package is deleted or made inaccessible. Test recovery with an anonymous pull on a host without that cached image before redeploying. Never delete the running container merely to test registry availability.

## Verifying a deployment

```bash
# the app
curl -sI https://feather.example.com/source.json | head -1        # 200
curl -s https://feather.example.com/source.json | python3 -m json.tool | head -5

# local mode really enabled (the single most common regression)
docker logs telegram-bot-api 2>&1 | head -2                        # must end with --local

# storage backend
curl -sI https://feather.example.com/ipas/<bundle>/<ver>.ipa | grep -i '^HTTP\|^location'
# 302 + Location -> Garage;  200 + content-length -> local disk

# the worker
docker logs --tail 30 ipa-ingest-bot                               # names missing vars if misconfigured

# the Android repo
curl -sI https://feather.example.com/fdroid/repo/index-v1.jar | head -1
curl -s https://feather.example.com/fdroid/repo/index-v1.json | python3 -c 'import json,sys; print(list(json.load(sys.stdin)["packages"]))'
curl -s https://feather.example.com/fdroid/repo/index-v2.json | python3 -m json.tool >/dev/null
docker logs --tail 5 feather-fdroid-index                           # "INFO: Finished"
cat data/fdroid/last-update.json                                    # "ok": true

# QR: decode with a QR reader; expected scheme and shape
# fdroidrepos://feather.example.com/fdroid/repo?fingerprint=<64 lowercase hex>
```

## Conventions worth preserving

- **Plans are the unit of work.** Each is self-contained for an executor with no context. `plans/README.md` records what was rejected and why, so findings are not re-audited.
- **Prove a test discriminates** by breaking the behaviour it guards and watching it fail. Several plans here caught real bugs precisely because that step was mandatory rather than optional — and it caught three arithmetic errors in my own done-criteria.
- **Optional features default off.** New `compose.yml` services get a `profiles:` key; new app behaviour gets a default-off env flag. Plan 011 followed this; Plan 013 did not, and broke a working deployment on the next pull.
- **`data/` is the product.** 1.3 GB of irreplaceable binaries plus a hand-curated catalog, gitignored. Never `git add` it, never bulk-edit it, and prefer read-only fixes on the serve path over migrations.
- **Redact credentials, never configuration.** Scrubbing a path prefix destroys the readability of every message containing a path under it — see Plan 022.
