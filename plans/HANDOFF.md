# Handoff

Written 2026-08-12 against `1b75e2c`; refreshed 2026-09-18 against the certificate-inspection work (plans 086–087). Read **State** and **Traps** first. The 2026-08-31 "Immediate blocker" (missing `feather-fdroid` GHCR package) is **resolved** — see State.

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
- `scripts/ipa_inspection.py`, `scripts/apk_inspection.py` — shared artifact inspectors, imported by both `app.py` (via `sys.path` at `app.py:35-36`) and the importers
- `scripts/certificate_inspection.py` — p12 + `.mobileprovision` inspector (plan 087); stdlib for the profile, `cryptography` for the p12
- `tests/` — **381 passed, 1 skipped**; no real provider calls
- `plans/` — 86 numbered plans; `README.md` is the status index

**Test command** (the `ADMIN_PASSWORD` prefix is mandatory — the app refuses to import without it):

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

## State

**All plans through 087 are implemented or explicitly rejected — the backlog is empty.** See `plans/README.md` for historical statuses and deferred findings. Working tree is clean apart from the untracked `run-local.sh` and `resume.txt`.

Jenkins tests Python 3.11 and 3.14, checks the `requests` pin, builds and smokes all three images, then publishes `ghcr.io/t1nk333r/feather`, `feather-bot`, and `feather-fdroid` only after every smoke stage passes.

The Android QR encodes a standard `fdroidrepos://` deep link while the manual repository address remains HTTPS.

## What changed on 2026-09-18

**Certificate inspection, and a deliberate refusal to sign.** Plan 086 researched
what it would take to hold a signing certificate and sign uploaded IPAs
server-side, and rejected it from primary sources: `codesign` is macOS-only by
Apple's own words, AltStore *and* SideStore both strip the incoming signature
and re-sign with the **subscriber's** identity (so a signed upload is
behaviourally identical to an unsigned one), a Development/Ad-Hoc identity is
UDID-bound to at most 100 devices, and the only non-UDID-bound iOS profile type
is Enterprise, licensed to employees only.

Plan 087 implements what was actually being asked underneath that request:
`POST /api/certificate/inspect` plus a Certificate panel that takes a `.p12` and
a `.mobileprovision` and reports whether the pair is usable, and on which
devices. Nothing is signed, nothing is stored, the passphrase is never
persisted or logged.

- The **headline is the pair match**: whether the p12's leaf appears in the
  profile's `DeveloperCertificates`. The iOS Feather app never performs this
  check, and it is what decides whether the pair can sign at all.
- **Two independent expiries** (the certificate's `notAfter`, the profile's
  `ExpirationDate`) and **three device-binding states** (UDID-bound with the
  count, all-devices, App Store with no device list) are each reported
  explicitly, because a boolean loses the useful answer.
- **Wrong passphrase is distinguished from an unusable file.** pyca raises
  `ValueError` for both; a handler that collapses them reports a typo as a
  corrupt certificate.
- Storing the pair was rejected on custody grounds: it would put a reusable
  code-signing identity behind the single shared admin password, with nothing
  here to encrypt it under (`SECRET_KEY` is regenerated per boot when unset).

## What changed on 2026-09-02

**Repo-import now handles Android.** Plans 083, 084 and 085 (all merged) taught the release-import path to select, validate and publish APKs alongside IPAs. All three consumers share one engine: the cron `release-import` container, the admin UI's "Import from Repo", and the auto-import watcher.

- Platform is inferred from the asset's file extension; one job can publish an IPA **and** an APK from the same release.
- Selection allows **at most one matching asset per platform** — the guard against an ambiguous release publishing the wrong binary.
- `bundleIdentifier` and `package` are both **optional**. Each is auto-detected from the artifact (Info.plist / AndroidManifest) and acts only as an assertion when set.
- Asset matching has an opt-in **`assetMatchMode: glob | regex`** (plan 085). Regex uses `re.fullmatch` + `re.IGNORECASE`, mirroring fnmatch's whole-name semantics. Patterns are compiled and length-capped (200 chars) **at save time**, never in the scheduler thread.

**Multi-ABI releases are why regex exists.** `RyanYuuki/AnymeX` ships one APK per ABI. Verified behaviour:

```
glob  '*.apk'             -> ProviderError: release has 2 android assets matching, expected at most one
regex '.*arm64-v8a\.apk'  -> 1 candidate: AnymeX-Android-arm64-v8a.apk
```

**The GHCR blocker is resolved.** All three packages issue anonymous pull tokens and resolve `:latest` (verified 2026-09-02):

| package | `:latest` digest |
|---|---|
| `feather` | `sha256:84367cd2…` |
| `feather-bot` | `sha256:5b7da003…` |
| `feather-fdroid` | `sha256:81fcf533…` |

The cause was never a deleted package: **the pipeline had never produced a successful build**, so nothing was being pushed. `d7eeem/feather/main` had `lastSuccessfulBuild: None` until build #4 on 2026-09-02. Fixing the build (see the `.dockerignore` trap) fixed the registry.

**Jenkins is green and repeatable** — builds #4–#9 all SUCCESS, 90–122 s each.

## Not finished

- **`b8be7a2` is not deployed.** The running container was built from `936e1d4` (created 2026-09-02T12:44Z) and therefore has **no regex mode**. Trigger a Jenkins build of `main`, confirm SUCCESS, then recreate `altstore-source-manager` (see Deployment).
- **No APK has been imported end to end for real.** Every test is offline by design. A live `/inspect` against `RyanYuuki/AnymeX` correctly tagged both ABI APKs as `platform: android`, but nothing has been downloaded and published. **This is the highest-value next action** — use a single-APK release, or regex mode for a multi-ABI one.
- **`parse_manifest_dict` is still over-strict.** `scripts/release_source_ingest.py:330` requires "at least one of `bundleIdentifier` or `package`" for **CLI manifest** jobs — the same rule removed from the UI paths in `fd38864`, because both fields are auto-detected. An Android-only cron job is forced to declare a package it does not need. Pre-existing from plan 083, left deliberately rather than widen an unrelated merge.
- **The certificate panel is not deployed either.** Plan 087 adds the first cryptographic dependency (`cryptography==50.0.1`), so the image must actually rebuild before `/api/certificate/inspect` exists in the running container; a stale image will 404 the route rather than fail loudly.

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

**Deploying without shell access to the box.** Both a `dockhand` and a `jenkins` MCP server are configured for Claude Code globally in `~/.claude.json` (HTTP transport, bearer in `headers`). If a session does not load them as tools, they can still be driven directly over HTTP: `initialize` → `notifications/initialized` → `tools/call`, carrying the returned `Mcp-Session-Id`. Read the header out of `~/.claude.json` into a variable; **never echo it**.

Useful calls: `getJob`/`getBuild`/`getBuildLog`/`triggerBuild` (Jenkins, job `d7eeem/feather/main`); `list_containers`/`get_container`/`batch_update_containers` (Dockhand, `environmentId: 1`, name `truenas`). Note the argument names are `environmentId`, `containerId`, `jobFullName` — not `env`/`id`/`job` — and container IDs are the full 64 characters.

**Deploy the app container alone, not the whole stack.** `batch_update_containers` with just `altstore-source-manager` re-pulls and recreates only the app. A whole-stack recreate would also try to re-pull `feather-fdroid`; when that package was unavailable, that would have taken the Android sidecar down. Prefer the narrow operation unless the sidecar image itself changed.

## Outstanding — operator tasks

Ordered by urgency. None of these are code.

1. **Rotate the Telegram bot token.** It has been exposed at least twice — in container logs before Plan 020's redaction landed, and in a directory listing (the Bot API names its work folder after the token). @BotFather → `/revoke`, then update `.env`. The folder name changes with it; harmless.
2. **Rotate `ADMIN_PASSWORD`.** It sat in a mode-0755 `.env` while the app had no auth at all, so it protected nothing. It is now load-bearing (Plan 010) and the app refuses to boot without it.
3. **Three catalog entries ship gzip-compressed HTML, not IPAs** — `com.instagram.theta 408.1.0_TH`, `com.instagram.ifgram 408.1.0_IF`, `com.fouadraheb.watusi B_25.36.10_WC`. 1,719–1,724 bytes each; decompressed they are a filebin landing page. Plan 007 stopped new ones; nothing repaired these. Either source real binaries or delete those versions from `data/source.json`.
4. **Three catalog bundle IDs disagree with their binaries** — `com.instagram.theta` and `com.instagram.ifgram` both ship `com.burbn.instagram`; `com.michael-128.qBitControl` ships `MikeMichael225.qBitControl`. May be deliberate (a renamed patched build installs beside the real app), but AltStore keys update-tracking on the bundle identifier, so it should be a decision rather than an accident.
5. **Garage is configured but not in use.** `STORAGE_BACKEND` defaults to `local`. To switch: fill the Garage keys, run the migration **dry-run first** (expect 8 uploadable / 3 corrupt-skipped / 2 orphans), then `--apply`, *then* set `STORAGE_BACKEND=garage`. Doing it in the other order makes every existing app un-installable on restart.
6. **`developerName` is `"Unknown"`** on anything the bot created (Plan 023's default), and `localizedDescription` is empty. Cosmetic; fix in the web UI.
7. **Enable and protect Android signing state.** Set `COMPOSE_PROFILES=android`, run `chown -R 999:999 data/fdroid`, and back up both `data/fdroid/keystore.p12` and `FDROID_KEYSTORE_PASSWORD`. Losing either changes the repository fingerprint for every subscriber.
8. ~~**Restore the missing `feather-fdroid` GHCR package.**~~ **Resolved 2026-09-02** — all three packages pull anonymously. The package was never deleted; the pipeline had simply never produced a successful build.
9. **Try one real APK import.** Nothing has gone through the Android path end to end. Use a single-APK release first (a multi-ABI project needs plan 085). This is the highest-value next action.
10. ~~**Review and merge plan 085.**~~ **Done 2026-09-02** (`b8be7a2`). Deploy it — see "Not finished".

## Local development

No `.env` or `data/` exists in a fresh clone, and the app has **no dotenv support** — the caller must export the environment. `run-local.sh` (untracked, created 2026-09-02) sources `.env` and starts waitress on port 5000:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
mkdir -p data/{ipas,icons,uploads,backups,fdroid/metadata}
# .env needs at minimum: ADMIN_PASSWORD, SECRET_KEY, DATA_DIR=./data, PORT, PUBLIC_BASE_URL
./run-local.sh
```

`DATA_DIR` defaults to the container path `/app/data`, so a local run **must** override it.

Docker is not usable from this workstation: the daemon runs, but the user is not in the `docker` group and `/var/run/docker.sock` is `root:docker`. Builds happen on Jenkins, not locally.

**Leftover executor branches.** `advisor/082` through `advisor/085` are all merged and their worktrees can be pruned (`git worktree remove`, then delete the branch and its `worktree-agent-*` pointer). No branch now holds unmerged work. `worktree-agent-ab89652b51670fac4` and `worktree-agent-ade6674b6dbfeba85` are older orphans.

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

**A missing GHCR package may mean the build never succeeded.** `feather-fdroid` looked deleted for days — 401 on the anon token, 404 on the package page. It was neither deleted nor private: `d7eeem/feather/main` had `lastSuccessfulBuild: None`, so the push stage had never run. Check `getJob`'s `lastSuccessfulBuild` before theorising about the registry.

**A new file a Dockerfile COPYs must also be re-admitted in `.dockerignore`.** This is the deny-by-default trap below, and it bit for real on 2026-09-02: plan 083 added `COPY scripts/apk_inspection.py` without the matching `!` line, and every build failed with `failed to compute cache key: ... "/scripts/apk_inspection.py": not found`. **No test can catch this** — nothing in `tests/` builds an image. When a plan adds a file the image needs, the `.dockerignore` line is part of that change.

**Editing a plan file moves `main` under an in-flight executor.** Refreshing plan 084's line numbers mid-flight meant its branch no longer fast-forwarded, forcing a cherry-pick. Dispatch from a stable base, or accept the cherry-pick and verify `main` is byte-identical to the reviewed branch afterwards.

**Line numbers in a plan go stale the moment another plan lands.** Plan 083 shifted `app.py` by 12 lines, invalidating every citation in the then-unstarted plan 084. A stale excerpt is a STOP condition for an executor. Re-verify a plan's citations right before dispatching it.

**Do not filter selection on a field that is auto-detected.** Plan 083 made `_group_matches_by_platform` skip a platform whose identity field was unset. Since both fields are optional and auto-detected, a blank field silently matched nothing and surfaced as `no eligible release had ... asset matching '<glob>'` — blaming the operator's glob. Fixed in `936e1d4`; the identity is an assertion applied *after* inspection, never a selector.

**A running container does not prove its registry image still exists.** Docker can continue running a cached `feather-fdroid` image after the GHCR package is deleted or made inaccessible. Test recovery with an anonymous pull on a host without that cached image before redeploying. Never delete the running container merely to test registry availability.

**A cherry-picked branch can silently undo a fix made while it was running.** Plan 085's branch was based before `936e1d4` and edited the same file. After cherry-picking, two things had to be re-checked explicitly: that the identity filter had not come back, and that the test `936e1d4` deleted had not been resurrected. Neither had — but `git` would not have complained if they had. After merging any long-running branch, re-verify the fixes that landed while it ran.

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
