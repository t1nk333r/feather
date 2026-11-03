# Handoff

Refreshed **2026-09-30**, when `claude/practical-wozniak-tn54z5` (the agent upload API and MCP server) merged into `main`. Coding conventions and invariants are in [`/AGENTS.md`](../AGENTS.md). This file records state, open work and traps. Read "State" and "Not finished" first.

## What this is

Feather is a self-hosted iOS AltStore/Feather source and a signed third-party F-Droid repository. It has an authenticated admin UI, a public `/store` page, release importers (cron and in-app) and Telegram ingest. New in this round are a token-authenticated publish API and an MCP server for agents. `AGENTS.md` describes the layout and the commands.

- `app.py` is about 7,600 lines and `templates/index.html` about 3,750.
- Tests: 696 collected on 2026-11-03. Run them with `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider`.

## State

- `main` is the branch head after this merge. CI runs on GitHub Actions. A green `main` publishes `ghcr.io/t1nk333r/feather`, `feather-bot` and `feather-fdroid`, tagged `:latest`, `:main` and `:sha-<short>`. Publishing uses `GHCR_PAT` if it is set and `GITHUB_TOKEN` otherwise. Each package already grants this repo write access.
- The repo is **public**. Its history was rewritten on 2026-09-30 to drop private infrastructure details. Keep LAN IPs, hostnames and proxy addresses out of it.
- **The repo cannot tell you whether the latest `main` is deployed.** Check with `curl https://<host>/api/version`, which reports the commit the image was built from. CI bakes that commit into the image, starting with the commit after `b5252ec`. Route probes ("401 means new, 404 means old") stop working once both images have the route, and one such probe wrongly certified a deploy on 2026-10-01.
- Agent sessions work on the branch `claude/practical-wozniak-tn54z5`. Their proxy cannot delete remote branches, so delete it in the GitHub UI if you want it gone.

## What changed on 2026-09-30

All of this is on `main`, and all of it has tests.

- CI moved from Jenkins to GitHub Actions. The fdroidserver base was re-pinned after another GitLab digest purge, and `Dockerfile.fdroid` records the pin and the real-APK spike run against it.
- F-Droid had stopped publishing new APKs. Upload now rejects unsigned APKs, which fdroidserver used to skip without saying so. The sidecar's update has a timeout (`FDROID_UPDATE_TIMEOUT`). APKs that fdroidserver drops appear as `rejected` in `last-update.json` and in the UI.
- There is a public, read-only `/store` page. It renders from the same public documents a phone sees.
- The QA and security passes added:
  - IPA validation on every upload path
  - zip-bomb caps in both inspectors
  - a sandbox CSP on `/fdroid/repo/`, because fdroidserver writes the repo name into HTML unescaped
  - the `X-Frame-Options`, nosniff and Referrer-Policy headers
  - login throttling (5 per 60 s)
  - a constant-time password compare
  - validation of the source-info and news fields
  - cleanup of featured and news references when an app is deleted
  - an admin UI with no axe-core findings in light or dark mode
- Release import now works end to end, tested against a fake GitHub over HTTPS. Identity is optional in cron manifests. The importer skips unchanged Android assets without downloading them. `--apply` logs in once up front and gives clear errors for 401, 429 and an unreachable server.
- When the uploader supplies no icon, the app extracts one from the binary. For an IPA it uses the Info.plist icon files and decodes CgBI. For an APK it uses the launcher raster or the adaptive-icon composite. **Fill missing icons** (Apps tab) backfills older apps, and so does Android **Rebuild index**.
- With `APK_STORAGE_BACKEND=garage`, Garage serves the APKs. The app uploads each one to `apks/<file>`, and `/fdroid/repo/<apk>` answers with a 302 to `GARAGE_PUBLIC_BASE_URL`. The bucket must be publicly readable. **Rebuild index** mirrors any APKs that exist only locally.
- Agent upload (`279449b`) added three things. The operator manages API tokens in the Source tab. The app stores their SHA-256 in `data/api-tokens.json` with mode 600, and a token can only publish and read. `POST /api/publish` is one endpoint for an IPA or an APK, by upload or by URL. It detects the platform and is idempotent. `scripts/feather_mcp.py` is an MCP stdio server that uses only the standard library. `UPLOADING.md` has the contract.
- The importer's "more than one asset matches" error now names the matching assets.

## Not finished

The list runs from most to least valuable. None has started.

1. **Import one real APK through the release importer against a real GitHub release.** The importer has only been tested against a fake GitHub. `/api/publish` and the MCP server *have* now had real use. On 2026-10-01 an agent published DAVKeep and nasplayer through them, and the index rebuilt with nothing rejected.
2. **Re-run the agent smoke test after deploying this round.** Use a scoped token, call `publish_app` with `whats_new` and app details, then call `update_app`. Then check that an F-Droid client shows the summary, licence, links, release notes and the Feather repo icon.
3. **Localise the F-Droid text.** The app writes only `en-US` (changelogs and icon). Per-locale summaries and descriptions would need fdroidserver's `metadata/<pkg>/<locale>/` files.

### Agent feedback round (2026-10-01)

An agent published DAVKeep and nasplayer through the MCP server and reported back. Each item below is fixed, with a test that fails when the fix is reverted.

- The agent reported "description dropped". It was not dropped. The app stored it as the F-Droid `Description`, but F-Droid lists show `Summary`, which was empty. The app now derives a missing `summary` from the first line of the description, and clients can send `summary` directly. On iOS, `summary` sets the subtitle.
- A debuggable APK was published without any warning. `inspect_apk` now reads `android:debuggable`, the publish response carries a warning, and `APK_REJECT_DEBUGGABLE=true` refuses such APKs. The default stays "warn", because the operator's own debug builds are a legitimate use and fdroidserver itself only warns.
- Publishing accepts more fields: `summary`, `license`, `website`, `sourceCode` and `categories`. Release notes go in `whatsNew`, which becomes `changelogs/<versionCode>.txt` on Android and the version's `localizedDescription` on iOS.
- `POST /api/app-details` and the MCP tool `update_app` update an existing app's details.
- A token can carry an optional app allowlist, which answers 403 for other apps, and an expiry. The agent's claim that "a leaked token could replace any app" overstated the risk, because the app never overwrites an existing version. A leaked unscoped token *can* add a new, higher version to any app, and scoping closes that.
- The repo icon was a QR-code placeholder, because fdroidserver resolves `repo_icon: icon.png` relative to `data/fdroid/` and nothing wrote that file. The app now installs `static/icon-512.png` there once and never overwrites an operator's own file.
- The repo description is empty. That is an operator setting (Android tab, repo name and description), and by design only an admin session can change it. Set it.

**Second round, the same day.** After the agent verified the deploy, `update_app` and the signed index carried all the DAVKeep fields. Two of my claims were wrong, and both are fixed. The repo icon stayed a QR code (see the repo-icon trap). The "401 vs 404" deploy probe could not tell the two images apart, so `GET /api/version` replaced it. This round also added release notes for versions already published (`update_app` with `whats_new` and `version`), and `repo_status` now reports `server_version`.

### Audit (2026-10-02, at `59f8fe4`)

A workflow audit covered 9 dimensions, and a code-reading refuter and a reproducer checked each finding. It confirmed 60 findings, 33 after deduplication. A usage limit cut off three planned gap areas (Garage maintenance routes, the UI-vs-API contract and the health monitor) and the synthesis step, so those **were not audited**.

The security batch (#1-#6) made these fixes:

- An IPA's identity could inject stored XSS into inline admin handlers. Data-* attributes and identity validation in `inspect_ipa` fix it.
- A token could trigger server-side fetches. The server now refuses `url` from tokens, and the MCP server downloads locally.
- The APK zip-bomb caps now measure the real inflated size.
- CgBI and icon decoding are bounded.
- The app sends the GitLab token only to GitLab.
- Auto-import enforces the configured bundleIdentifier.
- The batch also fixed two bugs along the way. App names that start with an emoji broke the Apps tab, and MCP mishandled `create_if_missing: "false"`.

The deployment batch made these fixes:

- `app.py` now treats a blank env value as unset everywhere (`_env`). After `cp .env.example .env`, the app no longer crashes on `int('')` or replaces defaults without saying so.
- `compose.yml` no longer requires `FDROID_KEYSTORE_PASSWORD` outside the android profile. Compose interpolates every service before it filters by profile.
- Moving an IPA or icon out of `data/uploads` falls back to copy, fsync and replace on EXDEV (`_move_file`).
- The README creates `data/ipas data/icons data/fdroid` before the `chown`.
- CI now renders compose from `.env.example` and boots the image with its blank keys.

The correctness batch made these fixes:

- The app refuses storage-key collisions (`_storage_conflict`).
- The Waitress body cap follows `MAX_CONTENT_LENGTH`, and an oversize request gets a 413 JSON answer.
- The app truncates an auto-derived APK Name, so no APK ends up half-published.
- The app writes the fdroid `config.yml` values as JSON strings and validates the repo name and description.
- `add_apk` enforces `APK_REJECT_DEBUGGABLE`, so it applies on every path.
- `/api/app-details` takes `platform` and answers 409 when the id exists on both platforms. MCP `get_app` returns both.
- Restore blocks only on severity "error".
- Add App on an existing ID applies the new icon.
- A version needs an IPA or an http(s) URL.
- The app inserts versions in version order (`_insert_version`).
- The cron importer isolates each candidate, and the inspectors raise only their own error types.
- Sessions carry an ADMIN_PASSWORD fingerprint, and `SESSION_COOKIE_SECURE` is now an env var.
- The MCP server treats 3xx and non-JSON responses as errors, catches every error per call and per message, and reads stdin as UTF-8. Its `repo_status` tolerates old servers.
- URL downloads have a wall-clock deadline.
- The iOS publish retry race answers `added:false`, and storage failures answer 503.
- `delete_app` saves the catalogue before it deletes IPAs.

The low findings got these fixes:

- Diagnostics redact the Telegram bot token, the Garage key ID and the Telegram API hash.
- A one-off import of a version that is already present reports "skipped". It sends no notification and does not overwrite provenance.
- The admin glob hint no longer suggests `*.{ipa,apk}`, because fnmatch has no braces.
- Cron Android provenance uses the field names the server keeps.
- A new Telegram forward always clears the previous pending file.
- The Garage migration scripts ship in the app image (`docker compose run --rm altstore-manager python scripts/migrate_...`).
- README and UPLOADING now state the login throttle and the Telegram notification defaults correctly.

Still open are the three unaudited areas: Garage maintenance routes, the UI-vs-API contract and the health monitor.

## Outstanding operator tasks

This is the last known state. The repo cannot show any of it, so verify before acting.

0. **Decide nasplayer's release path.** It blocks the app's next publish. The release build is signed with a different key (`CN=NAS Player F-Droid`) from the debug builds on the store. It also ships two flavours, `fdroid` and `standard`, with separate keys. The recommendation is to publish the **`fdroid` flavour with the release key**, starting at a new versionCode (0.1.3 / 50; the debug build already has 40). Then delete the debug versions in the Android tab so the app has one signer. Anyone with the debug build installed must uninstall it before the release installs over it. That is a one-time cost, and staying on a debug key forever is worse.
1. **Set the F-Droid repo name and description** in the Android tab. Subscribers currently see a repo with no description. Then, if DAVKeep was created before the summary fix, fix its details with `update_app` or the UI.
2. ~~Fix the NeoFreeBird job glob.~~ Done on 2026-11-03. The `neofreebird` job watches `NeoFreeBird/app` with the regex `NeoFreeBird-sideloaded-X_.*\.ipa`, excludes the legacy `_6.` line, and files the IPA under `com.atebits.Tweetie2.nfb` through `catalogBundleIdentifier` (plan 090). The archived-repo job is gone.
3. **Use the corrected `compose.yml`** delivered for the deployment on 2026-09-30. The old deployed file had lost the indentation under the top-level `volumes:`, which fails with "volumes must be a mapping". The corrected file also adds the `release-import` service, 2048m of memory for `fdroid-index`, and `FDROID_UPDATE_TIMEOUT`. The repo's `compose.yml` sets no memory limit on `fdroid-index`.
4. **Rotate the Telegram bot token and `ADMIN_PASSWORD`** if nobody has done so since they were exposed. Older handoffs have the history.
5. **Back up `data/fdroid/keystore.p12` and `FDROID_KEYSTORE_PASSWORD`.** Losing either changes the repo fingerprint for every subscriber.
6. **Serve APKs from Garage.** Set `APK_STORAGE_BACKEND=garage`, make sure the bucket is publicly readable, then press **Rebuild index** once to mirror the existing APKs.
7. **Clean up the catalogue.** Two problems carry over from earlier handoffs. Three versions are gzip-compressed HTML rather than IPAs (`com.instagram.theta 408.1.0_TH`, `com.instagram.ifgram 408.1.0_IF`, `com.fouadraheb.watusi B_25.36.10_WC`), and three catalogue IDs differ from their binaries' IDs. Decide what to do with them and fix them in the UI.

## Deployment

Feather runs on a self-hosted Docker host, managed through a container UI. The stack is at `/path/to/feather`. Optional profiles add services:

| Container | Image | Notes |
|---|---|---|
| `altstore-source-manager` | `ghcr.io/t1nk333r/feather:latest` | the product; host port 7000 to container port 5000 |
| `telegram-bot-api` | `aiogram/telegram-bot-api:latest` | self-hosted Bot API, `--local`; no host port |
| `ipa-ingest-bot` | `ghcr.io/t1nk333r/feather-bot:latest` | forward-to-bot worker |
| `feather-fdroid-index` | `ghcr.io/t1nk333r/feather-fdroid:latest` | profile `android`; runs as root by necessity; mounts only `./data/fdroid` |

The public URL is `https://feather.example.com` (openresty on `<proxy-ip>`). Garage S3 is at `https://s3.example.com`, and it serves the bucket `feather-repo` at `https://feather-repo.web.example.com`.

To update:
```bash
docker compose pull && docker compose up -d      # NOT --build
```
`--build` gives you a locally built image instead of the one CI verified. The source is the same, but the artifact is different, and it bypasses what CI checked without telling you.

**Deploy the app container alone, not the whole stack.** Re-pulling and recreating only `altstore-source-manager` (`docker compose pull altstore-manager && docker compose up -d altstore-manager`) touches only the app. A whole-stack recreate would also try to re-pull `feather-fdroid`, and while that package was unavailable, that would have taken the Android sidecar down. Update only the app unless the sidecar image itself changed.

## Local development

A fresh clone has no `.env` or `data/`, and the app has no dotenv support, so the caller must export the environment. `run-local.sh` (untracked, created 2026-09-02) sources `.env` and starts waitress on port 5000:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
mkdir -p data/{ipas,icons,uploads,backups,fdroid/metadata}
# .env needs at minimum: ADMIN_PASSWORD, SECRET_KEY, DATA_DIR=./data, PORT, PUBLIC_BASE_URL
./run-local.sh
```

`DATA_DIR` defaults to the container path `/app/data`, so a local run **must** override it.

GitHub Actions builds the images, not a local machine.

## Traps

Each of these cost real debugging time, and recording them is the reason this file exists.

**A session upload route blames the artefact when you misname the field.** On the legacy session routes, `-F "file=@x.apk"` answers `400 "Either an APK file or downloadFromUrl is required"`. The message names the concept, not the field. The fields there are `apkFile` and `ipaFile`. `POST /api/publish` (2026-09-30) accepts `file` as well as both of those, and new clients should use it.

**`command:` in compose does nothing for `aiogram/telegram-bot-api`.** Its entrypoint ends in `exec $COMMAND` and never forwards `"$@"`. Every option must come from a `TELEGRAM_*` env var, so local mode is `TELEGRAM_LOCAL=true`, not `--local`. The `--http-port=8081` in the process list comes from the entrypoint's own default, which is why the override *looks* like it works. **Check the command line the container logs at startup**, not `compose.yml`.

**The worker must run as uid 101.** `telegram-bot-api` chowns its work dir to the `telegram-bot-api` user (uid 101), but the worker image defaults to uid 999. With a different uid, the worker cannot traverse into the token directory. `os.path.exists()` returns `False` on permission-denied, so the error you see is the misleading `MountMismatchError: shared volume mounts disagree`. Setting `user: "101:101"` on `ipa-ingest-bot` fixes it. A direct reproduction checked the same file on the same mount and got `False` as uid 999 and `True` as uid 101.

**Dockge cannot pass `--profile`.** Both Telegram services are opt-in (Plan 016). Put `COMPOSE_PROFILES=telegram` in `.env` instead. Compose reads it from the project `.env`, and it has the same effect. Do **not** set it before the eight worker variables exist. The worker correctly refuses to start without them, and `restart: unless-stopped` turns that refusal into a crash loop.

**The Dockge `compose.yml` and the repo's have diverged.** Nothing I change in the repo reaches the deployment on its own. Dockge's "From Git" option would fix that for good; until then, sync by hand. `user: "101:101"` used to be only in the Dockge copy. The repo's `compose.yml` now sets it on `ipa-ingest-bot` as well.

**Every pin in `requirements.txt` needs wheels for both cp311 (the container) and cp314 (the test host).** This already happened once. `pillow==10.1.0` publishes no cp314 wheel, and that stalled Plan 004. Check with `pip download --no-deps --only-binary=:all: --python-version 311` and again with `314`. The host's system `python3` has no `pip`, so use a venv's.

**A digest pin can vanish when upstream rebuilds the tag it was pinned from.** `Dockerfile.fdroid` pinned `registry.gitlab.com/fdroid/docker-executable-fdroidserver@sha256:e5853810…` (verified 2026-08-28). Upstream rebuilt `master` on 2026-09-06 and GitLab garbage-collected the old digest, so every build failed with `failed to resolve source metadata … not found`. A pin that protects against a moving tag can itself disappear. The symptoms look like a registry outage. Check the current tag digest with the registry API (`https://gitlab.com/jwt/auth?service=container_registry&scope=repository:<path>:pull`, then `GET /v2/<path>/manifests/master`), and re-pin with the fdroidserver revision from the `org.opencontainers.image.revision` label in the image config. `latest` on that repo is years stale (built 2024-10-21), so pin `master`. **This was resolved structurally on 2026-10-08.** The third purge came when the 2026-10-04 scheduled rebuild deleted e13f3682 (upstream rebuilds `master` every Sunday). Since then, `Dockerfile.fdroid` builds FROM `ghcr.io/t1nk333r/feather-fdroid-base@<digest>`. That image is a byte-identical copy, which CI's "Mirror fdroid base image" step makes with `skopeo copy --preserve-digests` while upstream still has the digest. To move to a newer fdroidserver, change the digest in `Dockerfile.fdroid` within a week of upstream publishing it.

**`getFile` is synchronous over the download in `--local` mode.** It blocks for the whole transfer, so size the timeout for the file, not for an API call. `BOT_API_GETFILE_TIMEOUT` sets it (default 900 s).

**Filenames lie about versions.** `YT_20.49.5_KP.ipa` declares `CFBundleShortVersionString = 20.47.3`. Never parse a version from a filename; read `Info.plist`.

**An IPA has an `Info.plist` for every bundled framework.** The TikTok file has 235 of them. Matching `endswith("Info.plist")` returns a framework's identifier, which looks plausible and is wrong. The regex must be exactly `^Payload/[^/]+\.app/Info\.plist$`.

**Loose IPA icon files are often CgBI, and some icons exist only in `Assets.car`.** Since 2026-09-30 the app decodes CgBI (byte-swapped BGRA, premultiplied alpha, raw-deflate IDAT) with `_decode_cgbi_png`, and it extracts icons on upload when the uploader gives none. It still cannot extract icons that exist only in `Assets.car`, so those apps keep the default icon. The same is true of Android launcher icons that are vector-only.

**`.dockerignore` is deny-by-default.** A new file that a Dockerfile needs must be re-admitted with a `!` line. If you forget, the build fails, which is the right way for this to fail. The rule exists so that a careless `COPY . .` cannot leak `.env` or the 1.3 GB `data/`.

**Docker creates a fresh `data/` bind mount as root**, and the container's non-root user cannot write to it. The app fails with `PermissionError` and serves nothing. Run `chown -R 999:999 data/` before the first start on a new host.

**The F-Droid sidecar must run as root.** Adding `user:` to that service breaks `fdroidserver`, because its upstream image requires root-owned paths and setup.

**`./data/fdroid` is a separate bind mount.** Moving an uploaded APK into it may raise `EXDEV`. `AndroidRepoManager.add_apk` handles that with a hidden staged copy and an atomic replace.

**A missing GHCR package may mean the build never succeeded.** `feather-fdroid` looked deleted for days, with a 401 on the anonymous token and a 404 on the package page. It was neither deleted nor private. `d7eeem/feather/main` had `lastSuccessfulBuild: None`, so the push stage had never run. Before theorising about the registry, check that the publishing CI run succeeded. That meant Jenkins `lastSuccessfulBuild` then; now it means the GitHub Actions `images` job on `main`.

**A new file a Dockerfile COPYs must also be re-admitted in `.dockerignore`.** This is the deny-by-default trap above. It first hit on 2026-09-02, when plan 083 added `COPY scripts/apk_inspection.py` without the matching `!` line and every build failed with `failed to compute cache key: ... "/scripts/apk_inspection.py": not found`. **No test can catch this**, because nothing in `tests/` builds an image. When a plan adds a file the image needs, the `.dockerignore` line is part of that change. It hit a second time on 2026-09-19. Plan 087 added `scripts/certificate_inspection.py`, which `app.py:39` imports at import time, with neither the `!` line nor the `COPY`. The app-image smoke failed with `ModuleNotFoundError: No module named 'certificate_inspection'` in build #4 while 381 tests stayed green. The failure shows up in the smoke stage's `python -c import app`, never in the test suites.

**Editing a plan file moves `main` under an in-flight executor.** Refreshing plan 084's line numbers while it ran meant its branch no longer fast-forwarded, which forced a cherry-pick. Dispatch from a stable base, or accept the cherry-pick and afterwards verify that `main` is byte-identical to the reviewed branch.

**Line numbers in a plan go stale as soon as another plan lands.** Plan 083 shifted `app.py` by 12 lines and invalidated every citation in plan 084, which had not started yet. A stale excerpt is a STOP condition for an executor, so re-verify a plan's citations right before dispatching it.

**Do not filter selection on a field that is auto-detected.** Plan 083 made `_group_matches_by_platform` skip a platform whose identity field was unset. Both fields are optional and auto-detected, so a blank field matched nothing and raised no error of its own. The failure surfaced as `no eligible release had ... asset matching '<glob>'`, which blamed the operator's glob. `936e1d4` fixed it. The identity is an assertion applied *after* inspection, never a selector.

**A running container does not prove its registry image still exists.** Docker can keep running a cached `feather-fdroid` image after the GHCR package is deleted or made inaccessible. Before redeploying, test recovery with an anonymous pull on a host that does not have the cached image. Never delete the running container just to test whether the registry is available.

**A cherry-picked branch can undo a fix made while it was running, and nothing warns you.** Plan 085's branch started before `936e1d4` and edited the same file. After the cherry-pick, two things needed an explicit re-check: that the identity filter had not come back, and that the test `936e1d4` deleted had not returned. Neither had, but `git` would not have complained if they had. After merging any long-running branch, re-verify the fixes that landed while it ran.

**The fdroidserver repo icon path is relative to its working directory, and a placeholder sticks.** `config.yml` says `repo_icon: icon.png`, which fdroidserver resolves against `data/fdroid/`. It does not resolve it against `repo/icons/`, despite the warning text plan 073 recorded. When that file is missing, fdroidserver writes a QR-code placeholder to `repo/icons/icon.png`. What happens next depends on the version. Version 2.4.5 copies `icon.png` on every run, but the **pinned master build copies it only when `repo/icons/icon.png` does not exist** (`copy_repo_icon`), so the placeholder survives installing a real icon. `b5252ec` assumed the 2.4.5 behaviour, and the live repo kept its QR code. An agent reported this, citing the pinned image's own `copy_repo_icon`. `ensure_repo_icon` now also deletes a published icon that differs from `data/fdroid/icon.png` before each rebuild request. Check fdroidserver behaviour against the pinned image, not a PyPI release.

**Release variants share every obvious word.** A job must match exactly one asset per platform. NeoFreeBird v7.0.0 ships `orionblur-NFB-BHTwitter-sideloaded-Twitter_7.0.0_12.28.1.ipa` and `…-sideloaded-X_7.0.0_12.28.1.ipa`, so `*.ipa`, `*Twitter*.ipa` and `*sideloaded*.ipa` all match both. Key the glob on the part that differs and leave the version out, as in `*-sideloaded-Twitter_*.ipa`. The error now lists the matching names.

## Verifying a deployment

```bash
# the app
curl -sI https://feather.example.com/source.json | head -1        # 200
curl -s https://feather.example.com/source.json | python3 -m json.tool | head -5

# which commit is running (compare with `git rev-parse --short origin/main`); "dev" = not a CI image
curl -s https://feather.example.com/api/version

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

- **Plans are the unit of work.** Each plan is self-contained, so an executor with no context can carry it out. `plans/README.md` records what was rejected and why, so nobody audits the same findings again.
- **Prove a test discriminates** by breaking the behaviour it guards and watching it fail. Several plans here caught real bugs because that step was mandatory, and it also caught three arithmetic errors in my own done-criteria.
- **Optional features default off.** New `compose.yml` services get a `profiles:` key, and new app behaviour gets a default-off env flag. Plan 011 followed this. Plan 013 did not, and it broke a working deployment on the next pull.
- **`data/` is the product.** It holds 1.3 GB of irreplaceable binaries and a hand-curated catalog, and git ignores it. Never `git add` it, never bulk-edit it, and prefer read-only fixes on the serve path over migrations.
- **Redact credentials, never configuration.** Scrubbing a path prefix makes every message that contains a path under it unreadable. See Plan 022.
