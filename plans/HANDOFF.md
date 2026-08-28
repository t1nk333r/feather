# Handoff

Written 2026-08-12 against `1b75e2c`; refreshed 2026-08-28 against `5e8f447` plus the current working implementation. Read the Traps section first.

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
- `tests/` — 280 test functions (301 cases currently collected), no real provider calls
- `plans/` — 80 numbered plans; `README.md` is the status index

**Test command** (the `ADMIN_PASSWORD` prefix is mandatory — the app refuses to import without it):

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

## State

All currently selected plans through 081 are implemented or explicitly rejected; see `plans/README.md` for historical statuses and deferred findings.

Jenkins tests Python 3.11 and 3.14, checks the `requests` pin, builds and smokes all three images, then publishes `ghcr.io/t1nk333r/feather`, `feather-bot`, and `feather-fdroid` only after every smoke stage passes.

## Deployment

TrueNAS, managed by **Dockge**, stack at `/path/to/feather`. Optional profiles add services:

| Container | Image | Notes |
|---|---|---|
| `altstore-manager` | `ghcr.io/t1nk333r/feather:latest` | the product; port 7000 → 5000 |
| `telegram-bot-api` | `aiogram/telegram-bot-api:latest` | self-hosted Bot API, `--local`; **no host port** |
| `ipa-ingest-bot` | `ghcr.io/t1nk333r/feather-bot:latest` | forward-to-bot worker |
| `feather-fdroid-index` | `ghcr.io/t1nk333r/feather-fdroid:latest` | profile `android`; root by necessity; mounts only `./data/fdroid` |

Public at `https://feather.example.com` (openresty on `<proxy-ip>`). Garage S3 at `https://s3.example.com`, bucket `feather-repo` served at `https://feather-repo.web.example.com`.

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
8. **After the first Jenkins publish, make all three `t1nk333r` GHCR packages public** if deployment pulls without registry credentials.

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
docker logs --tail 5 feather-fdroid-index                           # "INFO: Finished"
cat data/fdroid/last-update.json                                    # "ok": true
```

## Conventions worth preserving

- **Plans are the unit of work.** Each is self-contained for an executor with no context. `plans/README.md` records what was rejected and why, so findings are not re-audited.
- **Prove a test discriminates** by breaking the behaviour it guards and watching it fail. Several plans here caught real bugs precisely because that step was mandatory rather than optional — and it caught three arithmetic errors in my own done-criteria.
- **Optional features default off.** New `compose.yml` services get a `profiles:` key; new app behaviour gets a default-off env flag. Plan 011 followed this; Plan 013 did not, and broke a working deployment on the next pull.
- **`data/` is the product.** 1.3 GB of irreplaceable binaries plus a hand-curated catalog, gitignored. Never `git add` it, never bulk-edit it, and prefer read-only fixes on the serve path over migrations.
- **Redact credentials, never configuration.** Scrubbing a path prefix destroys the readability of every message containing a path under it — see Plan 022.
