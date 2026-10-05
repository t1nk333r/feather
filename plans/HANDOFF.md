# Handoff

Refreshed **2026-09-30** at the merge of `claude/practical-wozniak-tn54z5` into `main` (agent upload API + MCP server). Coding conventions and invariants live in [`/AGENTS.md`](../AGENTS.md); this file is **state, open work, and traps**. Read **State** and **Not finished** first.

## What this is

A self-hosted iOS AltStore/Feather source plus a signed third-party F-Droid repository, with an authenticated admin UI, a public `/store` page, release importers (cron and in-app), Telegram ingest, and — new — a token-authenticated publish API and MCP server for agents. Layout and commands: `AGENTS.md`.

- `app.py` about 6,170 lines; `templates/index.html` about 3,570.
- **Tests: 449 passed, 1 skipped** (`ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider`).

## State

- `main` = the branch head after this merge. CI is GitHub Actions; a green `main` publishes `ghcr.io/t1nk333r/feather`, `feather-bot`, `feather-fdroid` (`:latest`, `:main`, `:sha-<short>`). Publishing uses `GHCR_PAT` if set, else `GITHUB_TOKEN` — each package already grants this repo write access.
- The repo is **public**. History was rewritten on 2026-09-30 to drop private infrastructure details; keep it that way (no LAN IPs, hostnames, proxy addresses).
- **Whether the latest `main` is deployed is unknown to the repo.** Check with `curl https://<host>/api/version` — it reports the commit the image was built from (CI bakes it in; from the commit after `b5252ec` on). Route probes ("401 = new, 404 = old") stop working as soon as both images have the route; one wrongly certified a deploy on 2026-10-01.
- The branch `claude/practical-wozniak-tn54z5` is the working branch for agent sessions. The proxy used by those sessions cannot delete remote branches; delete it in the GitHub UI if wanted.

## What changed on 2026-09-30

All on `main`, all with tests:

- **CI moved from Jenkins to GitHub Actions**, and the fdroidserver base was re-pinned after another GitLab digest purge (`Dockerfile.fdroid` records the pin and the real-APK spike run against it).
- **F-Droid stopped publishing new APKs** — fixed: unsigned APKs are rejected at upload (fdroidserver silently skipped them), the sidecar's update has a timeout (`FDROID_UPDATE_TIMEOUT`), and APKs fdroidserver drops are listed as `rejected` in `last-update.json` and surfaced in the UI.
- **Public `/store`** page (read-only, built from the same public documents a phone sees).
- **QA / security passes:** IPA validation on every upload path, zip-bomb caps in both inspectors, a sandbox CSP on `/fdroid/repo/` (fdroidserver writes the repo name into HTML unescaped), `X-Frame-Options`/nosniff/Referrer-Policy, login throttling (5 per 60 s), constant-time password compare, validation of source-info and news fields, cleanup of featured/news references on app delete, axe-core-clean admin UI in light and dark.
- **Release import made to work end to end** (driven against an HTTPS fake GitHub): identity optional in cron manifests, unchanged Android assets skipped without download, `--apply` logs in once up front with clear 401/429/unreachable errors.
- **Icons extracted from binaries** when none is supplied: IPA (Info.plist icon files, CgBI decoded) and APK (launcher raster or adaptive composite). **Fill missing icons** (Apps tab) backfills older apps; Android **Rebuild Index** does too.
- **APKs served from Garage** when `APK_STORAGE_BACKEND=garage`: uploaded to `apks/<file>`, `/fdroid/repo/<apk>` 302s to `GARAGE_PUBLIC_BASE_URL`. The bucket must be publicly readable. **Rebuild Index** mirrors any local-only APKs.
- **Agent upload** (`279449b`): API tokens (Source tab; SHA-256 stored in `data/api-tokens.json`, mode 600; publish + read scope only), `POST /api/publish` (one endpoint, IPA or APK, upload or URL, auto-detected, idempotent), and `scripts/feather_mcp.py` (stdlib MCP stdio server). Contract: `UPLOADING.md`.
- The **"more than one asset matches"** importer error now names the matching assets.

## Not finished

Ordered by value. None is started.

1. **Import one real APK through the release importer against a real GitHub release.** The importer is tested only against a fake GitHub. (`/api/publish` + MCP *has* now been used for real: an agent published DAVKeep and nasplayer on 2026-10-01; index rebuilt with nothing rejected.)
2. **Re-run the agent smoke after deploying this round**: scoped token, `publish_app` with `whats_new` + details, `update_app`, then check an F-Droid client shows summary, licence, links, release notes and the Feather repo icon.
3. **Localised F-Droid text.** Only `en-US` is written (changelogs, icon). Per-locale summaries/descriptions would need fdroidserver's `metadata/<pkg>/<locale>/` files.

### Agent feedback round (2026-10-01)

An agent published DAVKeep and nasplayer through the MCP server and reported back. Addressed, with tests that fail when each fix is reverted:

- **"Description dropped"** — it wasn't: it was stored as F-Droid `Description`, but F-Droid lists show `Summary`, which was empty. A missing `summary` is now derived from the description's first line, and `summary` can be sent directly (iOS: subtitle).
- **Debuggable APK published silently** — `inspect_apk` now reads `android:debuggable`; the publish response carries a warning, and `APK_REJECT_DEBUGGABLE=true` refuses them. Default stays "warn": the operator's own debug builds are a legitimate use, and fdroidserver itself only warns.
- **More fields:** `summary`, `license`, `website`, `sourceCode`, `categories`; **release notes** as `whatsNew` (Android `changelogs/<versionCode>.txt`, iOS version `localizedDescription`).
- **Update details over the API:** `POST /api/app-details` + MCP `update_app`.
- **Tokens:** optional per-token app allowlist (403 otherwise) and expiry. Note the agent's "a leaked token could replace any app" overstated it: an existing version is never overwritten. What a leaked unscoped token *can* do is add a new, higher version to any app — scoping closes that.
- **Repo icon** was a QR-code placeholder: fdroidserver resolves `repo_icon: icon.png` relative to `data/fdroid/`, and nothing wrote it. The app now installs `static/icon-512.png` there once; an operator's own file is never overwritten.
- **Repo description is empty** — that is an operator setting (Android tab → repo name/description), deliberately session-only. Set it.

**Second round (same day), after the agent verified the deploy:** `update_app` and the signed index carried all DAVKeep fields. Two of my claims were wrong and are fixed: the repo icon stayed a QR code (see the repo-icon trap), and the "401 vs 404" deploy probe could not tell the two images apart (replaced by `GET /api/version`). Also added: release notes for already-published versions (`update_app` with `whats_new` + `version`), and `repo_status` reports `server_version`.

### Audit (2026-10-02, at `59f8fe4`)

A workflow audit (9 dimensions, each finding checked by a code-reading refuter and a reproducer) confirmed 60 findings, 33 after dedup. Three planned gap areas (Garage maintenance routes, UI-vs-API contract, health monitor) and the synthesis step were cut off by a usage limit and **were not audited**.

**Fixed (security batch, #1-#6):** stored XSS via IPA identity in inline admin handlers (data-* attributes + identity validation in `inspect_ipa`); token-triggered server-side fetches (`url` refused for tokens, MCP downloads locally); APK zip-bomb caps now measure the real inflated size; CgBI/icon decoding bounded; GitLab token only sent to GitLab; auto-import enforces the configured bundleIdentifier. Also fixed on the way: emoji-first app names breaking the Apps tab; MCP `create_if_missing: "false"`.

**Fixed (deployment batch):** blank env values now count as unset everywhere in `app.py` (`_env`), so `cp .env.example .env` no longer crashes on `int('')` or silently replaces defaults; `compose.yml` no longer requires `FDROID_KEYSTORE_PASSWORD` outside the android profile (Compose interpolates every service before filtering by profile); IPA/icon moves out of `data/uploads` fall back to copy+fsync+replace on EXDEV (`_move_file`); the README creates `data/ipas data/icons data/fdroid` before the `chown`. CI now renders compose from `.env.example` and boots the image with its blank keys.

**Still open, correctness:** secure_filename collisions between distinct IDs/versions; Waitress 1 GiB body cap vs documented 2 GiB (oversize -> 500); APK label >50 chars half-publishes; repo description YAML escaping can break every rebuild (**avoid lines starting with `---`/`...`**); `APK_REJECT_DEBUGGABLE` only on `/api/publish`; app-details can't reach an Android app sharing an iOS ID; restore blocked on Garage/external URLs; Add App on an existing ID deletes the live icon; empty downloadURL publishable; backport releases become versions[0]; one network error aborts the cron run; sessions not revocable; MCP 3xx/non-JSON treated as success, non-FeatherError kills the server, Windows stdin encoding; URL downloads lack an overall deadline. Plus 8 low items (diagnostics shows the Telegram token, docs drift, etc.).

## Outstanding — operator tasks

Last known state; none of these is visible from the repo, so verify before acting.

0. **Decide nasplayer's release path** (blocks its next publish). The release build is signed with a different key (`CN=NAS Player F-Droid`) from the debug builds on the store, and ships two flavours (`fdroid`, `standard`) with separate keys. Recommended: publish the **`fdroid` flavour with the release key**, starting at a new versionCode (0.1.3 / 50 — 40 is taken by the debug build), then delete the debug versions in the Android tab so the app has one signer. Anyone with the debug build installs over it only after uninstalling — a one-time cost; staying on a debug key forever is worse.
1. **Set the F-Droid repo name and description** (Android tab) — subscribers currently see an undescribed repo. Then fix DAVKeep's details via `update_app` or the UI if it was created before the summary fix.
2. **Fix the NeoFreeBird job glob:** `*-sideloaded-Twitter_*.ipa` (or `*-sideloaded-X_*.ipa`). See the trap at the end.
3. **Use the corrected `compose.yml`** delivered on 2026-09-30 (indentation under top-level `volumes:` had been lost — "volumes must be a mapping"; adds the `release-import` service, `fdroid-index` memory 2048m, `FDROID_UPDATE_TIMEOUT`).
4. **Rotate the Telegram bot token and `ADMIN_PASSWORD`** if not done since they were exposed (older handoffs have the history).
5. **Back up `data/fdroid/keystore.p12` and `FDROID_KEYSTORE_PASSWORD`.** Losing either changes the repo fingerprint for every subscriber.
6. **Garage for APKs:** set `APK_STORAGE_BACKEND=garage`, make sure the bucket is publicly readable, then press **Rebuild Index** once to mirror existing APKs.
7. **Catalogue hygiene** carried from earlier handoffs: three versions that are gzip-compressed HTML rather than IPAs (`com.instagram.theta 408.1.0_TH`, `com.instagram.ifgram 408.1.0_IF`, `com.fouadraheb.watusi B_25.36.10_WC`), and three catalogue IDs that differ from their binaries' IDs. Decide and fix in the UI.

## Deployment

A self-hosted Docker host, managed through a container UI. The stack lives at `/path/to/feather`. Optional profiles add services:

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

**Deploy the app container alone, not the whole stack.** Re-pulling and recreating just `altstore-source-manager` (`docker compose pull altstore-manager && docker compose up -d altstore-manager`) touches only the app. A whole-stack recreate would also try to re-pull `feather-fdroid`; when that package was unavailable, that would have taken the Android sidecar down. Prefer the narrow operation unless the sidecar image itself changed.

## Local development

No `.env` or `data/` exists in a fresh clone, and the app has **no dotenv support** — the caller must export the environment. `run-local.sh` (untracked, created 2026-09-02) sources `.env` and starts waitress on port 5000:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
mkdir -p data/{ipas,icons,uploads,backups,fdroid/metadata}
# .env needs at minimum: ADMIN_PASSWORD, SECRET_KEY, DATA_DIR=./data, PORT, PUBLIC_BASE_URL
./run-local.sh
```

`DATA_DIR` defaults to the container path `/app/data`, so a local run **must** override it.

Images are built by GitHub Actions, not locally.

## Traps

Each of these cost real debugging time. They are the reason this file exists.

**A session upload route blames the artefact when you misname the field.** On the legacy session routes, `-F "file=@x.apk"` answers `400 "Either an APK file or downloadFromUrl is required"` — the message names the concept, not the field. The fields there are `apkFile` and `ipaFile`. `POST /api/publish` (2026-09-30) accepts `file` as well as both of those; new clients should use it.

**`command:` in compose does nothing for `aiogram/telegram-bot-api`.** Its entrypoint ends in `exec $COMMAND` and never forwards `"$@"`. Every option must come from a `TELEGRAM_*` env var — local mode is `TELEGRAM_LOCAL=true`, not `--local`. The `--http-port=8081` you see in the process list comes from the entrypoint's own default, which is exactly why the override *looks* like it works. **Verify against the command line the container logs at startup**, not against `compose.yml`.

**The worker must run as uid 101.** `telegram-bot-api` chowns its work dir to `telegram-bot-api` (uid 101); the worker image defaults to uid 999. Different uid means it cannot traverse into the token directory — and `os.path.exists()` returns `False` on permission-denied, so this surfaces misleadingly as `MountMismatchError: shared volume mounts disagree`. `user: "101:101"` on `ipa-ingest-bot` fixes it. Reproduced directly: same file, same mount, `False` as 999 and `True` as 101.

**Dockge cannot pass `--profile`.** Both Telegram services are opt-in (Plan 016). Use `COMPOSE_PROFILES=telegram` in `.env` instead — Compose reads it from the project `.env` and it does the same thing. Do **not** set it before the eight worker variables exist; the worker correctly refuses to start unconfigured and `restart: unless-stopped` turns that into a crash loop.

**The Dockge `compose.yml` and the repo's have diverged.** Nothing I change in the repo reaches the deployment automatically. Dockge's "From Git" option would fix that permanently; until then, hand-sync. `user: "101:101"` is currently in the Dockge copy but **not** in the repo's — worth reconciling.

**Every pin in `requirements.txt` needs wheels for both cp311 (the container) and cp314 (the test host).** This is not hypothetical — `pillow==10.1.0` publishes no cp314 wheel and stalled Plan 004. Check with `pip download --no-deps --only-binary=:all: --python-version 311` and again with `314`. The host's system `python3` has no `pip`; use a venv's.

**A digest pin can vanish when upstream rebuilds the tag it was pinned from.** `Dockerfile.fdroid` pinned `registry.gitlab.com/fdroid/docker-executable-fdroidserver@sha256:e5853810…` (verified 2026-08-28). Upstream rebuilt `master` on 2026-09-06 and GitLab garbage-collected the old digest, so every build failed `failed to resolve source metadata … not found` — a pin that protects against a moving tag is itself mortal. Symptoms look like a registry outage; check the current tag digest with the registry API (`https://gitlab.com/jwt/auth?service=container_registry&scope=repository:<path>:pull`, then `GET /v2/<path>/manifests/master`) and re-pin with the fdroidserver revision from the image config's `org.opencontainers.image.revision` label. `latest` on that repo is years stale (built 2024-10-21) — pin `master`.

**`getFile` is synchronous over the download in `--local` mode.** It blocks for the whole transfer, so the timeout must be sized to the file, not to an API call. It is configurable (`BOT_API_GETFILE_TIMEOUT`, default 900 s).

**Filenames lie about versions.** `YT_20.49.5_KP.ipa` declares `CFBundleShortVersionString = 20.47.3`. Never parse a version from a filename; read `Info.plist`.

**An IPA has an `Info.plist` per bundled framework** — 235 of them in the TikTok file. Matching `endswith("Info.plist")` returns a framework's identifier, plausible and wrong. The regex must be exactly `^Payload/[^/]+\.app/Info\.plist$`.

**IPA icons: loose files are often CgBI, and some exist only in `Assets.car`.** CgBI (byte-swapped BGRA, premultiplied alpha, raw-deflate IDAT) is now decoded (`_decode_cgbi_png`) and icons are extracted on upload when the uploader gives none (2026-09-30). `Assets.car`-only icons still cannot be extracted; those apps keep the default icon. Android vector-only launcher icons likewise.

**`.dockerignore` is deny-by-default.** A new file a Dockerfile needs must be re-admitted with a `!` line, and you will find out via a failed build — the right failure mode. It exists so a careless `COPY . .` cannot leak `.env` or the 1.3 GB `data/`.

**A fresh `data/` bind mount is created as root**, and the container's non-root user cannot write to it — the app fails with `PermissionError` and serves nothing. `chown -R 999:999 data/` before the first start on a new host.

**The F-Droid sidecar must run as root.** Adding `user:` to that service breaks `fdroidserver` because its upstream image requires root-owned paths and setup.

**`./data/fdroid` is a separate bind mount.** Moving an uploaded APK into it may raise `EXDEV`; `AndroidRepoManager.add_apk` handles that with a hidden staged copy plus atomic replace.

**A missing GHCR package may mean the build never succeeded.** `feather-fdroid` looked deleted for days — 401 on the anon token, 404 on the package page. It was neither deleted nor private: `d7eeem/feather/main` had `lastSuccessfulBuild: None`, so the push stage had never run. Check that the publishing CI run actually succeeded before theorising about the registry. (Then Jenkins `lastSuccessfulBuild`; now the GitHub Actions `images` job on `main`.)

**A new file a Dockerfile COPYs must also be re-admitted in `.dockerignore`.** This is the deny-by-default trap below, and it bit for real on 2026-09-02: plan 083 added `COPY scripts/apk_inspection.py` without the matching `!` line, and every build failed with `failed to compute cache key: ... "/scripts/apk_inspection.py": not found`. **No test can catch this** — nothing in `tests/` builds an image. When a plan adds a file the image needs, the `.dockerignore` line is part of that change. **It bit a second time on 2026-09-19**: plan 087 added `scripts/certificate_inspection.py`, imported by `app.py:39` at import time, with neither the `!` line nor the `COPY` — the app-image smoke failed `ModuleNotFoundError: No module named 'certificate_inspection'` in build #4 while 381 tests stayed green. The failure surfaces in the smoke stage's `python -c import app`, never in the test suites.

**Editing a plan file moves `main` under an in-flight executor.** Refreshing plan 084's line numbers mid-flight meant its branch no longer fast-forwarded, forcing a cherry-pick. Dispatch from a stable base, or accept the cherry-pick and verify `main` is byte-identical to the reviewed branch afterwards.

**Line numbers in a plan go stale the moment another plan lands.** Plan 083 shifted `app.py` by 12 lines, invalidating every citation in the then-unstarted plan 084. A stale excerpt is a STOP condition for an executor. Re-verify a plan's citations right before dispatching it.

**Do not filter selection on a field that is auto-detected.** Plan 083 made `_group_matches_by_platform` skip a platform whose identity field was unset. Since both fields are optional and auto-detected, a blank field silently matched nothing and surfaced as `no eligible release had ... asset matching '<glob>'` — blaming the operator's glob. Fixed in `936e1d4`; the identity is an assertion applied *after* inspection, never a selector.

**A running container does not prove its registry image still exists.** Docker can continue running a cached `feather-fdroid` image after the GHCR package is deleted or made inaccessible. Test recovery with an anonymous pull on a host without that cached image before redeploying. Never delete the running container merely to test registry availability.

**A cherry-picked branch can silently undo a fix made while it was running.** Plan 085's branch was based before `936e1d4` and edited the same file. After cherry-picking, two things had to be re-checked explicitly: that the identity filter had not come back, and that the test `936e1d4` deleted had not been resurrected. Neither had — but `git` would not have complained if they had. After merging any long-running branch, re-verify the fixes that landed while it ran.

**fdroidserver's repo icon: the source is relative to its working directory, and a placeholder sticks.** `config.yml` says `repo_icon: icon.png`, resolved against `data/fdroid/` (not `repo/icons/`, despite the warning text plan 073 recorded). When it is missing, fdroidserver writes a QR-code placeholder to `repo/icons/icon.png`. Behaviour then differs by version: 2.4.5 copies `icon.png` on every run, but the **pinned master build only copies when `repo/icons/icon.png` does not exist** (`copy_repo_icon`), so the placeholder survives installing a real icon. `b5252ec` assumed the 2.4.5 behaviour and the live repo kept its QR code; reported by an agent with the pinned image's own `copy_repo_icon`. `ensure_repo_icon` now also deletes a published icon that differs from `data/fdroid/icon.png` before each rebuild request. Lesson: check fdroidserver behaviour against the **pinned image**, not a PyPI release.

**Release variants share every obvious word.** A job must match exactly one asset per platform, and variants often share every obvious word. NeoFreeBird v7.0.0 ships `orionblur-NFB-BHTwitter-sideloaded-Twitter_7.0.0_12.28.1.ipa` and `…-sideloaded-X_7.0.0_12.28.1.ipa`, so `*.ipa`, `*Twitter*.ipa` and `*sideloaded*.ipa` all match both. Key the glob on the part that differs and leave the version out: `*-sideloaded-Twitter_*.ipa`. The error now lists the matching names.

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

- **Plans are the unit of work.** Each is self-contained for an executor with no context. `plans/README.md` records what was rejected and why, so findings are not re-audited.
- **Prove a test discriminates** by breaking the behaviour it guards and watching it fail. Several plans here caught real bugs precisely because that step was mandatory rather than optional — and it caught three arithmetic errors in my own done-criteria.
- **Optional features default off.** New `compose.yml` services get a `profiles:` key; new app behaviour gets a default-off env flag. Plan 011 followed this; Plan 013 did not, and broke a working deployment on the next pull.
- **`data/` is the product.** 1.3 GB of irreplaceable binaries plus a hand-curated catalog, gitignored. Never `git add` it, never bulk-edit it, and prefer read-only fixes on the serve path over migrations.
- **Redact credentials, never configuration.** Scrubbing a path prefix destroys the readability of every message containing a path under it — see Plan 022.
