# Implementation plans

The `improve` skill generated this index on 2026-08-10 from an audit of `app.py` (2531 lines), `Dockerfile`, `compose.yml`, `requirements.txt`, and the on-disk state of `data/`. On 2026-08-16, a focused review of source metadata, Telegram thumbnail ingestion, and local/Garage icon storage added Plan 028. **Last reconciled 2026-09-02 at `936e1d4`.** See "Reconcile log" below.

Execute the plans in the order below unless the dependency notes say otherwise. If you execute a plan, **read it fully before starting**, honour its STOP conditions, and update its row when you finish.

## Context for anyone picking this up cold

This is a self-hosted AltStore/Feather iOS app-source manager. It serves a `source.json` catalog and about 1.3 GB of `.ipa` binaries to iOS devices, and runs under Docker Compose on a **private network** (VPN/LAN). `Dockerfile:16` is `COPY app.py .`, so `app.py` is the only live application file.

Three facts shape every plan here:

1. **`data/source.json` is the product.** It is hand-curated and not in version control (`data/` is gitignored), and every subscribed device polls it. Corrupting it is the worst possible outcome.
2. **Four routes must never require authentication.** They are `/source.json`, `/ipas/<bundle_id>/<filename>`, `/icons/<bundle_id>/icon.<ext>` and `/qr`. iOS clients fetch them with no credentials, so gating any one breaks every user's device.
3. **The repo's stated contract diverged from its behaviour everywhere.** `.env` declared five keys that nothing read. `requirements.txt` declared `atomicwrites` and `Flask-Limiter`, which nothing imported. `Dockerfile` health-checked with a credential for auth that did not exist. No code referenced `data/backups/`. Plans 001, 006, and 010 close those gaps.

> **New here?** Read [HANDOFF.md](HANDOFF.md) first. It has the current state, the outstanding operator tasks, and the traps that cost real debugging time.

## Execution order & status

| Plan | Title | Priority | Effort | Risk | Depends on | Status |
|------|-------|----------|--------|------|------------|--------|
| [001](001-configurable-paths-and-config.md) | Make data paths and configuration environment-driven | P1 | S | LOW | none | **DONE.** `bdcf5da`, merged to `main` as `26451c4` and pushed. |
| [002](002-version-control.md) | Put the repository under version control | P1 | S | LOW | none | **DONE.** `2fe1d2c` on `main`, pushed to `d7eeem/feather` (private). |
| [003](003-delete-dead-copies.md) | Delete the two dead copies of the application | P1 | S | LOW | 002, 004 | **DONE.** `04d3893`, merged. It deletes 3,792 lines and adds none; the 48 tests are unchanged. |
| [004](004-restore-pillow-qr.md) | Restore Pillow so the QR endpoint works | P1 | S | LOW | none | **DONE.** `cec6ae4`, merged as `facf9c1`. Pins `pillow==11.3.0` rather than the harvested `10.1.0`, which has no cp314 wheel (see the plan). Verified on both interpreters; see "Plan 004 verification" below. |
| [005](005-smoke-test-suite.md) | Establish a one-command smoke-test suite | P1 | S | LOW | 001 | **DONE.** `901699e`, merged as `41c417a`. The suite runs in a host venv, and its 21 tests cover all 13 routes. |
| [006](006-atomic-catalog-writes.md) | Make catalog writes atomic and serialized | P1 | S | LOW | 005 | **DONE.** `65dccfb`, merged as `a65c6bc`. 29→34 tests. Breaking each of the two guarantees proved that a test catches it; see "Plan 006 verification" below. |
| [007](007-fail-loudly.md) | Report failures instead of silently reporting success | P1 | S-M | LOW-MED | 005 (006 recommended) | **DONE.** `7b36d0a`..`d6d32bc` (5 defects, one commit each), merged as `4de7fed`. 21→29 tests. The container checks ran during review; see "Plan 007 verification" below. |
| [008](008-public-base-url.md) | Derive published URLs from configuration, not the `Host` header | P2 | S | LOW | 001, 005 | **DONE.** `f575e71`, merged as `309f882`. 34→39 tests. Two operator tasks remain: set `PUBLIC_BASE_URL` in `.env`, and hand-fix the one `<nas-ip>:7000` catalog entry. |
| [009](009-extract-html-template.md) | Extract the embedded HTML template to `templates/index.html` | P2 | S | LOW-MED | 005 | **DONE.** `367c036`, merged as `1f815b0`. Moves 1,493 lines byte for byte, which takes `app.py` from 2986 lines to 1491. Review checked the Dockerfile `COPY` in both directions. |
| [010](010-login-session-auth.md) | Gate the mutating routes behind a login form | P2 | M | MED | 001, 005, 009 | **DONE.** `1f95005`, merged as `632f40f`. 48→70 tests. Six routes are gated, and tests prove the four public routes are not. **The app now refuses to boot without `ADMIN_PASSWORD`.** |
| [012](012-telegram-ipa-ingest.md) | Ingest IPAs from a Telegram channel (Telethon / user account) | P3 | M | MED | 005, 010 | **REJECTED** (reconcile 2026-08-26). It was the fallback if 013's Step 0 failed. Step 0 passed on 2026-08-12 (see "Plan 013 Step 0: RESOLVED"), and the bot path has run in production since. Do not implement it; the plan file stays as the record of the trade-off. |
| [013](013-telegram-bot-ingest.md) | Self-hosted Bot API server and forward-to-bot IPA ingest | P3 | M | MED | 005, 010 | **DONE (code)**, merged as `15ae84e`. 70→78 tests. Step 0 resolved on 2026-08-12 (forwarding works, so 012 is retired). After Plan 023, the full pipeline was confirmed working end to end. |
| [014](014-telegram-notifications.md) | Telegram notifications on catalog changes | P3 | S | LOW | 005, 010 | **DONE.** `a4535b6`, merged. Off by default; set `TELEGRAM_NOTIFY_CHAT_ID` to turn it on. 108→113 tests. |
| [015](015-fix-ci-smoke-test-admin-password.md) | Fix the CI smoke test broken by Plan 010 | P1 | XS | LOW | none | **DONE.** `2d405aa`, merged. The smoke test also asserts that the image refuses to boot without `ADMIN_PASSWORD`. |
| [016](016-telegram-services-opt-in.md) | Make Plan 013's Telegram services opt-in | P1 | XS | LOW | 013 | **DONE.** `b20a0a9` and `05f84c6`, merged. `docker compose up -d` now starts only `altstore-manager`, and `--profile telegram` starts all three services. Compose no longer prints any warnings. |
| [017](017-ci-build-bot-image.md) | Build and publish the ingest-bot image in CI | P2 | S | LOW | 013, 015 | **DONE.** `757c57c`, merged. A new `build-bot` job publishes `ghcr.io/d7eeem/feather-bot`, and its smoke test asserts that the image refuses to run unconfigured. The plan also adds a `requests` pin-drift check. |
| [018](018-dockerignore.md) | Add a deny-by-default `.dockerignore` | P1 | XS | LOW | none | **DONE.** `70ed756`, merged. A `COPY . .` image now contains only the four allowlisted paths and no `.env`, `data/` or `.git`, so public packages are now safe. |
| [019](019-compose-pull-images.md) | Make `compose.yml` pull published images instead of building | P2 | XS | LOW-MED | 017, 018 | **DONE.** `50bad62`, merged. To update, run `docker compose pull && docker compose up -d`, **without `--build`**. `build:` stays as the local fallback. |
| [020](020-fix-getfile-timeout-and-silence.md) | Enable `--local`, redact tokens, surface failures | P1 | S | LOW | 013 | **DONE.** `aee7a19`, merged. The root cause was that `--local` never reached the server, because that image's entrypoint ignores compose's `command:`. 78→83 tests. **Rotate the bot token**, because the log recorded it in full. |
| [021](021-extract-ipa-metadata.md) | Read the bundle identifier and version out of the IPA | P2 | S | LOW | 013, 020 | **DONE.** `d8a40da`, merged. Bare `/add` uses the detected values, and `/add <id> <ver>` still overrides them. 83→91 tests. Checked against all 11 real IPAs. |
| [022](022-narrow-redaction.md) | Narrow the redaction to actual secrets | P2 | XS | LOW | 020 | **DONE**, merged to `main` (`4bb2db5`, 2026-08-26; a Sonnet executor ran it and the advisor reviewed it). Three `_redact` call sites drop `bot_api_file_root` and gain `telegram_api_hash` (already in config), and the docstring is fixed. The reviewer independently confirmed that the new `test_mount_mismatch_reply_keeps_root_and_path_readable` fails when the root is re-added. 243→244 tests. No existing test changed. |
| [023](023-create-app-on-first-publish.md) | Create the app when it isn't in the catalog yet, and set its icon | P1 | S-M | LOW-MED | 013, 020, 021 | **DONE.** `c46593e`, merged. `/add` creates the app only when the error is exactly `App not found`, then sets its icon from the Telegram thumbnail through `/api/update-app`. 91→102 tests. |
| [024](024-altstore-schema-required-fields.md) | Emit the AltStore-required fields so the source imports | P1 | S | LOW | 005 | **DONE.** `1f4c8a5`, merged. Normalisation at serve time adds `nsfw`, `appPermissions` and `buildVersion`. 102→108 tests. **Nobody has retested the import on a device yet.** |
| [011](011-garage-s3-ipa-storage.md) | Move IPA storage to the self-hosted Garage S3 object store | P2 | M-L | MED | 005, 008 | **DONE (code)**, merged as `5a636b5`. 39→48 tests. `STORAGE_BACKEND` defaults to `local`, so nothing changed at runtime. Running the migration and the cutover are operator tasks; see below. |
| [025](025-embed-source-icon-and-remove-emoji.md) | Embed the source icon and remove every UI emoji | P3 | S | LOW | 005, 009 | **DONE.** `eb98dd8`, merged as `51bcf8e`. Flask serves a new `static/` directory, and 21 emoji are gone from 17 lines. 113→117 tests. On 2026-08-16 the source `iconURL` target was corrected to the operator-supplied Backblaze PNG; Plan 028 makes it editable. |
| [026](026-theme-aware-favicon.md) | Theme-aware favicon | P3 | XS | LOW | 025 | **DONE.** `af9cad1`, merged as `de473a8`. Adds `static/icon-dark.svg` for dark browser chrome; the published source icon is unchanged. 117→119 tests. Red on slate was rejected because its contrast is only 2.1:1. |
| [027](027-write-readme.md) | Write the repository README | P2 | S | LOW | none | **DONE.** `4e2f2d6`, merged. The README is 134 lines, uses `example.com` placeholders throughout, and touches no code. It retires the "No README" deferred finding. The plan's criterion 3 could not be met as written and was corrected in place; see below. |
| [028](028-source-and-garage-app-icons.md) | Publish the supplied source icon and store app icons in Garage | P2 | M | MED | 011, 023, 025 | **DONE (code).** `42f20c8`..`262e7ca` (4 commits), merged as `0bcc9c0`. 119→134 tests. Step 6 is still an operator task: migrate the Garage icons and cut over to them, then update the live source icon through Source Information. |
| [029](029-cron-release-imports.md) | Poll GitHub/GitLab releases from cron and publish new IPAs | P2 | L | MED | 006, 007, 010, 011, 019, 021, 023 | **DONE**, merged to `main` (2026-08-17). Four commits on `advisor/029-cron-release-imports` (HEAD `26868ac`) add 2465 lines across 9 in-scope files and change nothing in `app.py`, the Telegram worker or `requirements`. The reviewer re-ran 21 focused tests and the full suite of 157; all pass. The reviewer also audited the security-critical code and its tests. A cross-host redirect strips `Authorization` and `PRIVATE-TOKEN`. Every hop must be HTTPS to an allowlisted host. A dry run makes no network calls, no login and no state writes. State advances only after a verified publish, and an app is created only on exact `App not found`. The default compose startup is unchanged. The full `docker build` image smoke was not run; CI runs it. The executor hit a session limit after committing, with its work intact. A 2026-08-31 check confirmed the commit is an ancestor of `3e0cd8a`. |
| [030](030-remove-dead-import-methods.md) | Delete the two dead catalog-import methods and drop `altparse` | P2 | S | LOW | none | **DONE**, merged to `main` (2026-08-17). Commit `32a8116` on branch `advisor/030-remove-dead-import-methods` makes 163 deletions and no insertions, in `app.py` and `requirements.txt` only. The reviewer re-ran every done criterion: 136 tests pass unchanged, the greps come back empty, and the change only deletes code. A 2026-08-31 check confirmed the commit is an ancestor of `3e0cd8a`. |
| [031](031-utc-correct-dates.md) | Stamp catalog dates in real UTC and drop the deprecated `datetime.utcnow()` | P2 | S | LOW | none | **DONE**, merged to `main` (2026-08-17). Commit `4f686c9` on branch `advisor/031-utc-correct-dates` makes three one-line changes to `app.py` and adds one test; 136→137 pass. The reviewer independently confirmed that the timezone test discriminates. The buggy code gives a 14400s delta and fails, and the fixed code gives 0.7s and passes. A 2026-08-31 check confirmed the commit is an ancestor of `3e0cd8a`. |
| [038](038-catalog-health-view.md) | Read-only "Catalog Health" admin view (flags corrupt/missing/duplicate/mismatched entries) | P2 | M | LOW | none | **DONE**, merged to `main` (`aec1d6e`, 2026-08-18). Adds `GET /api/health` (`@requires_auth`), `_scan_catalog_health` and a Health tab. The view is read-only and never returns a 500; on a scan error it returns `200` with an empty result. It flags duplicate-version, zero-size, empty/non-public downloadURL, missing-icon and missing-ipa entries, plus not-a-zip on the local backend. 178→183 tests. The reviewer re-ran the suite in the worktree (183) and confirmed that only the 3 in-scope files changed. |
| [039](039-delete-version.md) | Add delete-version to finish the version CRUD | P2 | S-M | MED | none | **DONE**, merged to `main` (`71a5c53`, 2026-08-18). Adds `POST /api/delete-version`, `SourceManager.delete_version` and a per-version Delete button with `confirm`. It deletes the IPA only after a successful atomic save, and it refuses to delete an app's only version. 183→188 tests. The reviewer confirmed the ordering, the only-version refusal and `notify` safety, and got 188 independently. |
| [040](040-reconcile-icons-action.md) | In-app "reconcile icons" action (upload on-disk icons to the active backend) | P3 | M | LOW-MED | none | **DONE**, merged to `main` (`0d7cfe6`, 2026-08-18). `POST /api/reconcile-icons` uploads on-disk icons that are missing from Garage. It defaults to a dry run. Apply uploads only the missing set and never deletes or modifies local files. On the local backend it reports a no-op. The control is on the Health tab. 188→193 tests. The reviewer confirmed that a dry run writes nothing and that no local file changes. |
| [041](041-richer-app-metadata.md) | UI editing for subtitle, screenshots and tint color | P3 | M | LOW | none | **DONE**, merged to `main` (`442c226`, 2026-08-18). Edit App round-trips `subtitle`, `screenshotURLs` (a list) and `tintColor`. All three are optional, and a field missing from the request is preserved, not wiped. 193→197 tests. The reviewer confirmed that a field is wiped only when the request includes it, and that screenshots are stored as a list. |
| [043](043-ios-native-redesign.md) | Re-skin the admin UI as iOS-native (Apple HIG) in light and dark, and remove the AI-generated look | P3 | M-L | LOW | none | **DONE**, merged to `main` (`669cd5a`, 2026-08-18, pushed). Adds an Apple HIG `:root` token system and a `prefers-color-scheme` dark theme, and removes every purple and gradient AI tell. The reviewer confirmed that only `templates/index.html` changed, that every `class`, `id` and `getElementById` hook is intact (a pure recolor), and that the 202 tests are unchanged. Deploy the new image to see it live. |
| [045](045-harden-reconcile-icons-fs-walk.md) | Harden `_reconcile_icons` so a filesystem error in the icon walk cannot cause a 500 | P2 | S | LOW | 040 | **DONE**, merged to `main` (`5d44b3c`, 2026-08-18, pushed). Wraps the `os.listdir` walk so an `OSError` returns a 200 with a `note`/`scan_failed` item instead of a 500. An empty local-icons folder returns a note that explains why. This fixes the live production 500. The reviewer confirmed that the change only adds to the response shape, and that the test discriminates: with the guard removed, the 500 comes back. 202→205. Deploy the new image. |
| [046](046-import-auto-icon.md) | On repo import, extract the app icon from the IPA (CgBI-aware), with an avatar fallback | P2 | M-L | MED | 034,035,042 | **DONE**, merged to `main` (`615a5b0`, 2026-08-18). `_extract_ipa_icon` takes iTunesArtwork, or else the largest AppIcon PNG, and converts Apple CgBI to standard PNG with `_decode_cgbi_png`. The GitHub avatar is the fallback. It never blocks an import, and it cleans up its temp file. The reviewer confirmed that the CgBI reversal round-trips under all 5 PNG filters, and confirmed the fallback chain. 205→211. _Prior TODO description:_ A user reported that imported apps (for example AnymeX) arrive with no icon. Extract the real icon from the IPA: iTunesArtwork, or else the largest AppIcon PNG, converting Apple CgBI to standard PNG with Pillow. Fall back to the GitHub owner's avatar, or else to no icon. Every failure falls back, so an import is never blocked. The bot uses a Telegram thumbnail, not the IPA, so this IPA-icon code is new. Touches `app.py` and tests. 205→211. |
| [047](047-form-control-theme-polish.md) | Theme polish: style the native `<select>` and fix faint placeholders | P3 | XS-S | LOW | 043 | **DONE**, merged to `main` (`3ae347c`, 2026-08-18). A CSS-only change adds an iOS chevron to `<select>` and makes `::placeholder` readable. 211 tests unchanged. _Prior TODO description:_ A user reported that the Provider dropdown looks unstyled and that placeholders are too faint in dark mode. A CSS-only change on the 043 token system sets the `::placeholder` color and a custom `<select>` chevron. No DOM or JS change; 205 tests unchanged. |
| [048](048-auto-import-backend.md) | In-app auto-import backend: job store, CRUD API, in-process scheduler, Run-Now | P2 | L | MED-HIGH | 029,034,035 | **DONE**, merged to `main` (`c9dbaf5`, 2026-08-18). Adds `data/auto-import.json`, the `/api/auto-import` CRUD, config and run routes, and a scheduler thread that only `__main__` starts. A check confirmed that importing the app spawns no thread. Runs are idempotent, isolated per job, and guarded by a run-lock. **Scheduled runs need `PUBLIC_BASE_URL` set**, because the thread has no request context. 211→221. _Prior TODO description:_ The user wants cron "in the apps page, not `.env`". The maintainer decided to make it fully in-app: an app-managed `data/auto-import.json`, CRUD, config and run routes, a background scheduler thread **started only from `__main__`** (tests never spawn it), and Run-Now reusing the engine in-process. Backend and API only. 205→215. |
| [049](049-auto-import-ui.md) | Auto-Import admin UI to manage repos, the schedule and Run-Now from the app | P2 | M-L | LOW-MED | **048** | **DONE**, merged to `main` (`4a9a039`, 2026-08-18). An "Auto-Import" tab on 048's API adds, edits and deletes jobs, sets enable and interval, and runs Run-Now. The reviewer confirmed that the server is unchanged, that inputs pass through `escapeHtml`, and that `/run` reads 048's JSON summary rather than a stream. The reviewer also verified a full CRUD round-trip against the live app. 221→222. _Prior TODO description:_ The UI half of "cron in the apps page". A new "Auto-Import" tab (job list, add, edit and delete, enable and interval, Run-Now) on plan 048's API. `templates/index.html` only, iOS-styled (043), with no server change. Execute AFTER 048. |
| [054](054-two-column-sidebar-layout.md) | Two-column layout with a left nav sidebar and a right workspace (replace the top tab bar) | P3 | M | LOW-MED | 043 | **SUPERSEDED by 055**, which reverted the sidebar to a mobile-first bottom tab bar. It was DONE, merged as `2223576`. _Prior TODO description, user-requested:_ Restructure `templates/index.html` into a two-column grid: a vertical nav sidebar, and a workspace that holds the existing `.tab-content` panels. Layout and CSS only; `switchTab` and all JS hooks stay unchanged. UI only; the 232 tests must stay green. |
| [058](058-omarchy-theme.md) | Re-theme to the Omarchy look: flat terminal dark (black and orange), thin lines, sharp corners, mono chrome | P2 | M | MED | 043,055 | **DONE**, merged to `main` (2026-08-19). The reviewer approved it: the glass is fully gone, the exact Omarchy palette is in place for light and dark, corners are square, the chrome is mono, and the dropdown fix stays. Both themes were checked in a headless browser. Prior TODO description: the user rejected the glass and wants omarchyplugins.com's theme. Swap the palette to Omarchy's exact tokens (bg `#000`/`#f8f8f6`, accent `#ff5a36`/`#c6371c`, thin `--line` borders). Flatten the UI by removing all of 057's glass, blur, gradients, floating and shadows. Use square corners and mono chrome. Keep the 055 bottom bar and the 057 dropdown fix. **Supersedes 057.** UI only; the 236 tests stay green. |
| [066](066-split-icon-storage-backend.md) | Independent `ICON_STORAGE_BACKEND`, so icons can sit on local disk and IPAs on Garage | P2 | S | MED | 011 | **DONE**, merged to `main` (`254a4fd`, 2026-08-20). Adds `ICON_STORAGE_BACKEND`, which defaults to `STORAGE_BACKEND` and selects the icon backend independently. The init block is split so that `ipa_storage` follows `STORAGE_BACKEND` and `icon_storage` follows `ICON_STORAGE_BACKEND`, and the `_reconcile_icons` gate now checks the icon backend. With `STORAGE_BACKEND=garage` and `ICON_STORAGE_BACKEND=local`, IPAs go to Garage and icons to `/app/data/icons`, which is already a mounted volume. The change is backward-compatible; `serve_icon` and `get_hosted_icon_url` are unchanged because they were already polymorphic. Updates the docs and `.env.example`. 241→243 tests. **Icons that exist only on Garage return 404 until they are re-hosted locally**; a re-import re-extracts them. |
| [067](067-release-asset-discovery.md) | Release asset discovery: preview, explain, and disambiguate matched IPA assets | P1 | M | MED | none | **DONE.** Adds a shared include/exclude selector, sanitized provider previews, an authenticated inspect route, and a discovery flow in the UI. |
| [068](068-shared-ipa-preflight.md) | Shared IPA preflight that verifies identity, version, platform, and minimum OS before publish | P2 | L | MED | none | **DONE.** One shared, immutable inspection model drives repo, auto, standalone, and Telegram imports. It rejects tvOS builds and publishes the verified build, minimum-OS and privacy metadata. |
| [069](069-actionable-health-watchtower.md) | Actionable health watchtower: deep links, snapshots, transitions, and optional checks | P2 | M-L | MED | none | **DONE.** Adds health actions, bounded atomic snapshots, a default-off monitor, and notifications that fire only on transitions. |
| [070](070-guarded-catalog-recovery.md) | Guarded catalog recovery: inspect, diff, download, and restore catalog backups | P2 | M-L | HIGH | none | **DONE.** Adds strict snapshot resolution and validation, a structural preview, a storage preflight, an atomic restore guarded by a typed confirmation and the expected hash, and a Recovery UI. |
| [071](071-import-provenance-history.md) | Import provenance: keep sanitized, bounded run history outside the public catalog | P2 | M | LOW-MED | 067, 068 | **DONE.** Adds a private, bounded ledger with mode 0600, recording from both in-app and standalone imports, authenticated filters and provenance views, and a job-history UI. |
| [072](072-news-and-featured-app-authoring.md) | News and featured-app authoring: guarded editorial controls for standard AltStore metadata | P3 | M | MED | none | **DONE.** Adds validated featured and news mutations that run under the catalog lock, authenticated APIs, and controls in Source Information. |
| [073](073-android-fdroid-repo.md) | Serve an Android F-Droid repository beside the iOS source (`fdroidserver` sidecar, Android tab, `/fdroid/repo/`) | P2 | L | MED | none | **DONE**, merged to `main`. A Sonnet executor ran it on 2026-08-26 and the advisor reviewed it. A 2026-08-31 check confirmed the merge: `f885e30`..`89de5ba` are ancestors of `3e0cd8a`, and the follow-up commits `f591adf` and `3e0cd8a` build on them. The executor worktree is gone. The local branch `worktree-agent-ab89652b51670fac4` is a leftover pointer and can be deleted. The plan adds 1,606 lines across the 11 in-scope files, leaves the iOS path alone, and modifies no existing test. 244→260 tests (1 gated on `FEATHER_TEST_APK`). The reviewer re-ran the full suite, the sidecar image build and its refusal to run unconfigured, and the compose profile gating (absent by default, present with `android`). The reviewer also re-checked the route inventory: 2 public and 8 gated routes, which brings the `@requires_auth` count to 25. The executor found and fixed a real bug the plan missed. `./data/fdroid` is a separate bind mount, so `os.replace` from `uploads/` raised `EXDEV` on the first real upload; the code now falls back to copy and remove. An end-to-end compose run with the real F-Droid APK passed: index-v1.jar returned 200, the packages were listed, and the QR returned 200. **No actual Android device has scanned the QR yet.** Before use, the operator must put `FDROID_KEYSTORE_PASSWORD` in `.env`, run `mkdir -p data/fdroid && chown -R 999:999 data/fdroid`, set `COMPOSE_PROFILES=android`, and back up `keystore.p12`. |
| [074](074-auto-import-scheduler-robustness.md) | Auto-import scheduler runs off-request; `auto-import.json` edits can't be lost | P1 | S | LOW | none | **DONE.** Adds an atomic mutation helper and a scheduler cycle that needs no request, with tests for races, due times and the base URL. |
| [075](075-android-post-landing-fixes.md) | Fix six Android/F-Droid defects found after plan 073 | P1 | S-M | LOW | 073 | **DONE.** Package deletion matches exactly, staging across an EXDEV boundary is atomic, metadata is validated before publish, YAML values are type-checked, the publish notification fires, and an authenticated status request refreshes the repo URL. |
| [076](076-fdroid-sidecar-hardening.md) | Harden the fdroid-index sidecar | P1 | S | LOW | 073 | **DONE (code/static verification).** Adds password-safe keytool calls, a minimal environment, a digest pin, a sturdier status loop, and a rotation runbook. The live container smoke could not run, because this host denies access to the Docker daemon. |
| [077](077-jenkins-push-after-smoke.md) | Jenkins: build, then smoke, then push; the boot-refusal assertion names `ADMIN_PASSWORD` | P1 | S | LOW | none | **DONE.** All three images build and pass the smoke test before one final publish stage. The plan also corrects the POSIX shell and the import and boot assertions. |
| [078](078-docs-refresh.md) | Bring README and HANDOFF back in line with the code | P2 | S | LOW | 074-077 recommended first | **DONE.** Documents Waitress, the full live route inventory, Jenkins and the three images, Android operations, the recovery and editorial features, and `WAITRESS_THREADS`. |
| [079](079-strip-gitlab-auth-on-redirect.md) | Strip GitLab credentials on every cross-host download redirect | P1 | S | MED | none | **REJECTED**, already fixed; no implementation change is needed. The citation was refreshed on 2026-08-31. The stripping now lives at `scripts/release_source_ingest.py:617-630`, which rebuilds the headers on each hop and gates them by host, and at `:672`, which drops `Authorization` and `PRIVATE-TOKEN` from the carried headers. The old `:594-599` citation had drifted onto `_host_allowed`. |
| [080](080-sanitize-error-responses.md) | Return stable error messages instead of raw exception text | P1 | S | LOW-MED | none | **DONE.** Fixes the unexpected-error payloads on the public and Android routes, and regression tests check that exception text is not reflected back. Full suite `262 passed, 1 skipped`. |
| [081](081-idempotent-manual-app-add.md) | Make manual app creation idempotent for an existing version | P2 | S | MED | none | **DONE.** The duplicate-version guard runs before storage, and a regression test proves that a retry is a no-op. Full suite `262 passed, 1 skipped`. |
| [082](082-compose-bot-uid.md) | Pin the Telegram ingest worker to uid 101 in the repo's `compose.yml` | P2 | XS | LOW | none | **DONE**, merged to `main`. A Sonnet executor ran it on 2026-08-31 and the advisor reviewed it. Commit `7aafb8b` on `advisor/082-compose-bot-uid`, worktree `.claude/worktrees/agent-a03e56d43ce480469`. It adds `user: "101:101"` to `ipa-ingest-bot` in 6 insertions and no deletions, and `compose.yml` is the only file it touches. The reviewer re-ran the suite in the worktree (`307 passed, 1 skipped`) and diffed the parsed YAML against `3e0cd8a`. The service set is unchanged, and the only difference in the whole file is the new `user` key; the shared volume keeps its `:ro`. The executor caught an arithmetic error in the plan's own verify line, which said 7 insertions where its parenthetical described 6. The plan file now has the corrected count. |
| [083](083-engine-apk-support.md) | Teach the release-import engine to select, validate, and publish APKs | P2 | L | MED | none | **DONE**, merged to `main`. A Sonnet executor ran it on 2026-09-01 and the advisor reviewed it. Seven commits, `0588473`..`0a5f41d`, fast-forwarded onto `main`. The plan adds `scripts/apk_inspection.py`, which mirrors 068's `ipa_inspection.py`, along with `Job.package`, `inspect_apk_metadata` and `FeatherClient.add_apk`. Selection now returns at most one candidate per platform. `process_job` is split into `_process_ios_candidate` and `_process_android_candidate`. State is nested per platform and still accepts the old flat shape. 307→338 passed, 1 skipped. The reviewer verified independently that the scope is clean (6 in-scope files) and that `app.py` has only the two sanctioned edits. The only line that touches auth, host or redirect logic re-indents the GitLab host-allowlist check, which now runs once per candidate and so is stricter than before. The reviewer sabotaged three behaviours, and each time exactly the right test caught it. Forcing platform inference to `ios` caused 11 failures, and the tests also caught a neutered package-mismatch guard and already-present APKs counted as published. The executor also found one of its own tests that did not discriminate, strengthened it, and reported this without being asked. One piece of known debt remains until plan 084 removes it. `select_candidate_single` carries an `isinstance(result, list)` branch that only the old-shape monkeypatches in the out-of-scope `test_import_release.py` and `test_auto_import.py` can reach. One cosmetic change went unreported. APK error messages gained an `APK: ` prefix, but the dict contract and the `ValueError` type are preserved, and no test asserts the text. |
| [084](084-watcher-ui-apk-support.md) | Let the watcher and the "Import from Repo" UI publish APKs | P2 | M-L | MED | **083** | **DONE**, merged to `main`. A Sonnet executor ran it on 2026-09-01 and the advisor reviewed it. Five commits, cherry-picked as `a338782`..`967d1b8`. The branch predated the citation-refresh commit, so it could not fast-forward; a check confirmed `main` is byte-identical to the reviewed branch. `_validate_auto_import_job` and `_release_job_from_payload` accept `package`, and at least one identity is required. The watcher and `/api/import-release` both split into per-platform helpers, with a `platforms` breakdown in `lastResult`. `/inspect` tags each asset's platform, and `importRepoForm` gains an Android Package field. 338→344 passed, 1 skipped. **083's `select_candidate_single` shim is deleted**, and `grep` finds it nowhere in the code. The reviewer verified that the engine diff is a 19-line shim deletion and nothing else, and that `AndroidRepoManager` and every `/api/android/*` route are untouched. Security logic is untouched too, apart from one de-indent of `allowed_download_hosts` with the same expression. The SSE fields the frontend reads are still emitted. All five new DOM interpolations use `textContent` or are numeric, with no new `innerHTML`. The reviewer independently sabotaged three behaviours the executor had not tried, and the tests caught all three: dropping `package` from the returned dict (the silent-drop trap), and skipping Android candidates in each of the two dispatch paths. The executor caught two plan defects rather than gaming them. The done criterion `name="package" → exactly one match` was wrong, because plan 073's Add APK form already had one and 084 correctly added exactly one more. The plan also contradicted itself by listing the engine as out of scope while directing the shim deletion from it. The reviewer endorsed one judgment call. The asset-preview "unsafe" heuristic widened from `matchCount !== 1` to more than one match per platform, so a legitimate release with one IPA and one APK no longer trips the "import anyway?" dialog. |
| [085](085-regex-asset-matching.md) | Let asset selection use a regular expression, so one APK can be pinned out of many | P2 | M | MED | 083, 084 | **DONE**, merged to `main`. A Sonnet executor ran it on 2026-09-02 and the advisor reviewed it. Four commits, cherry-picked as `…`..`ac3a734` because the branch predated `936e1d4` and touched the same file. Adds an opt-in `assetMatchMode: glob\|regex`. `re.fullmatch` with `re.IGNORECASE` mirrors fnmatch's whole-name semantics. Every entry point compiles each pattern and caps its length at 200 when it is saved. 346→365 passed, 1 skipped. The reviewer verified that the glob branch behaves as before, and that the one-per-platform guard and the security and redirect code are untouched. `assetMatchMode` is present in the dict that `_validate_auto_import_job` returns. Most important, the cherry-pick preserved `936e1d4`, so no identity filter came back, and it did not resurrect `test_github_selection_ignores_apk_when_job_has_no_package`. The reviewer independently sabotaged three behaviours and the tests caught each one: flipping the default mode to regex (22 failures), swapping `fullmatch` for `search`, and removing the invalid-pattern guard. A check against the real case confirmed the fix. `*.apk` on a two-ABI AnymeX release still raises the ambiguity error, while `.*arm64-v8a\.apk` in regex mode selects exactly one. |
| [086](086-research-signing-ipas-on-upload.md) | Research: what signing IPAs on upload would actually take | P3 | M | none | none | **RESEARCH**, no code. A note built on primary sources: Apple technotes and licence agreements, the `zsign`, `ldid`, AltStore, SideStore and `claration/Feather` source, and this repo. Its verdict is not to sign server-side. In Apple's own words, `codesign` is macOS-only. AltStore and SideStore both strip the signature and re-sign with the *subscriber's* identity, so a signed upload behaves exactly like an unsigned one. A Development or Ad-Hoc identity is bound to device UDIDs (at most 100 devices), and `ProvisionsAllDevices` is Enterprise-only and licensed to employees. §8 covers the `.p12` and `.mobileprovision` upload workflow the operator asked for, and settles on validating and displaying them only. That became 087. |
| [087](087-certificate-inspection-panel.md) | Inspect an uploaded `.p12` and `.mobileprovision` pair from the admin UI (sign nothing) | P2 | S-M | LOW | 086, 010 | **DONE** (2026-09-18). Adds `scripts/certificate_inspection.py`, which reads the profile with the stdlib (its CMS payload is cleartext) and the p12 with `cryptography`. Adds a session-gated `POST /api/certificate/inspect` that reads both files in memory and persists nothing. Adds a Certificate panel whose headline is the pair match, which asks whether the p12 is in the profile's `DeveloperCertificates`. This is the one check the iOS Feather app never performs. The panel states both expiry dates and all three device-binding states in words. Adds the first cryptographic pin, `cryptography==50.0.1`. 365→381 passed, 1 skipped. A live browser smoke test covered a matching pair, a mismatch, a wrong passphrase and an expired pair. |
| [088](088-device-backups.md) | Keep device backups (settings zips) for the operator's own phones, with a backups-only token kind | P2 | M | LOW | 010 | **DONE** (2026-10-21). Adds `DEVICE_BACKUPS`, off by default. `kind: backup` tokens reach only `/api/device-backups/<app>/…`, and `requires_publish` refuses them. 27 tests. |
| [089](089-declare-ipa-entitlements.md) | Declare each iOS app's entitlement and privacy keys from its binary, so AltStore and SideStore install it | P1 | S-M | MED | 068, 086 | **DONE** (2026-10-31). It declares keys only, never values. Reads go forward only, under a 2 GiB per-inspection budget, and per-inspection and per-app caps limit what reaches `source.json`. |
| [090](090-catalog-bundle-override.md) | Let a repo import publish an IPA under a catalog ID of the operator's choosing | P2 | S | LOW | 048, 084 | **DONE** (2026-11-03). Adds an optional `catalogBundleIdentifier` to auto-import jobs and the one-off import (admin field *Publish Under Catalog ID*), while `bundleIdentifier` still asserts the IPA's own ID. Feather records provenance under the catalog ID, with the IPA's ID as `artifactBundleIdentifier`. iOS only; the cron manifest and `/api/publish` are unchanged. 17 tests; sabotaging each behaviour made a test fail. |
| [091](091-version-retention.md) | Keep only an app's newest version and N before it, pruning older ones when an update lands | P2 | S | MEDIUM. It deletes binaries, but only for apps an admin opts in | 073 | **DONE** (2026-11-03). Adds a per-app `keepPreviousVersions` in `data/retention.json` (admin session only), set from the iOS Edit dialog or the Android card. When an add becomes the newest version, it prunes the rest after the catalogue is saved. Android groups APKs by versionName, so ABI splits count once. *Prune Now*, or `POST /api/retention/apply`, applies the setting on demand. `/api/publish` reports `pruned`. 28 tests; each of 13 sabotages made a test fail. |
| [065](065-garage-put-tolerate-verify-403.md) | Garage `put()` must not fail an upload when the post-upload verify-read is 403 | P1 | S | MED | 028,050 | **DONE**, merged to `main` (`ddc7164`, 2026-08-20). This was the root cause of the live "Failed to save uploaded icon". The write succeeded, but the app's write-only Garage key gets a 403 on the verify `head_object`, and the code treated "can't verify" as "write failed". Now both `GarageIconStorage.put` and `GarageIpaStorage.put` classify a forbidden verify with `_classify_storage_error` and return success with a warning instead of failing: `True` for an icon, and the precomputed size for an IPA. A size mismatch or any other error still fails. `_remaining_size` is duplicated onto the IPA class. 236→239 tests. The operator's real fix is still `garage bucket allow --read`. |
| [064](064-import-icon-for-existing-apps.md) | Apply the icon on repo-import of an *existing* app (URL and auto-detect) | P2 | S-M | MED | 034,046 | **DONE**, merged to `main` (`cf2e511`, 2026-08-20). Both import paths set the icon only in the create-if-missing branch, so an existing app (for example AnymeX) got neither a supplied `iconURL` nor auto-detection. A new best-effort helper, `_apply_icon_to_existing`, runs after `add_version` in `import_release()` and in the auto-import scheduler. An explicit `iconURL` always wins. Otherwise the helper auto-detects an icon (the IPA icon, then the owner avatar), but only when the current icon is missing or a placeholder. It never raises. The form label is updated. 239→241 tests. |
| [063](063-icononly-tabbar-rounded-topbar.md) | Icons-only floating bar (centered compact pill with a rounded highlight) and a rounded top bar | P2 | S | LOW | 062 | **DONE**, merged to `main` (`e88bd28`, 2026-08-20). Drops the tab text labels, so the bar shows icons only and each button gets an `aria-label` and a `title`. `.tab` is centered, and the active icon keeps its orange and a rounded `--fill-hover` highlight. `.tabbar` becomes a centered compact capsule (`margin:0 auto; max-width:460px`, the responsive form of the 500px inset). `.topbar` becomes a rounded floating card (`border-radius:var(--radius-lg)`, `margin:10px`, sticky `top:10px`), the brand logo gets `border-radius:6px`, and "Sign out" becomes a rounded danger pill. The glass, the SVGs and `switchTab` are untouched. UI only; 236 tests unchanged. |
| [062](062-rounded-corners-floating-tabbar.md) | Restore rounded corners and float the bottom bar as a glass pill (with an active-tab highlight) | P2 | S | LOW | 058,060,061 | **DONE**, merged to `main` (`82ddc0f`, 2026-08-20). The radius tokens go from `3/3/2` to `14/10/8`, which rounds every card, input, modal and button. `.tabbar` becomes a floating glass capsule with 12px side margins, `999px`, a full border and a drop shadow, and `color-mix` raised from 62% to 72%; the glass stays. The active tab gets a rounded `--fill-hover` highlight. The `.workspace` bottom padding grows to 92px, in both the base rule and the mobile media query. `switchTab` and the SF icons are untouched. UI only; 236 tests unchanged. |
| [061](061-ios-tabbar-icons.md) | Native iOS bottom tab bar with SF-Symbols-style inline SVG icons and SF Pro labels | P3 | S | LOW | 058,060 | **DONE**, merged to `main` (`d357e78`, 2026-08-20). Each of the six tabs (Add, Apps, QR, Source, Import, Health) gets an SF-style monoline SVG icon above its label. Labels switch from `--mono` to `--sans` (SF Pro on iOS). The active tab's icon and label take the orange accent through `currentColor`. The glass `.tabbar` (060), `switchTab` and the hooks are untouched. Real SF Symbols and SF Pro can't be embedded as webfonts, so hand-drawn SVGs approximate them. UI only; 236 tests unchanged. |
| [060](060-glass-bottom-bar.md) | Liquid-glass the bottom tab bar only (keep the flat Omarchy theme everywhere else) | P3 | XS | LOW | 058 | **DONE**, merged to `main` (`36f7244`, 2026-08-20). Changes `.tabbar` only. `background: var(--surface)` becomes `color-mix(in srgb, var(--surface) 62%, transparent)`, and the bar gains `backdrop-filter: saturate(180%) blur(20px)` with both prefixes. No other surface is glassy. UI only; 236 tests unchanged. |
| [059](059-fix-select-chevron.md) | Fix the `<select>` chevron properly (it was stuck top-left) | P2 | XS | LOW | 058 | **DONE**, merged to `main` (2026-08-19). The single `background-position` lost in the cascade. The plan replaces it with axis longhands, an edge keyword and `!important` (`background-position-x: right 14px !important; background-position-y: center !important;`), so the cascade can't reset the chevron to `0 0`. UI only; 236 tests unchanged. |
| [057](057-liquid-glass-theme.md) | Liquid Glass re-skin (translucent blurred surfaces, rounder corners, floating glass tab bar) and fix the dropdowns | P2 | M-L | MED | 043,055 | **SUPERSEDED by 058**, after the user rejected the glass in favour of the flat Omarchy theme. It was DONE, merged on 2026-08-19, with glass on 9 surfaces using the -webkit- prefix, a floating tab-bar pill, a fixed dropdown chevron, and legibility preserved. Prior TODO description: the user asked to 'make it liquid glass all around, fix the drop-downs.' Add glass tokens and `backdrop-filter` to every surface, a soft refraction background, a floating glass tab-bar pill and rounder corners. Fix the `<select>` chevron by replacing the invalid `right 14px center` with `calc(100% - 14px) center`. UI only; keep the 055 contrast legible; the 236 tests stay green. |
| [055](055-mobile-bottom-tabbar-and-contrast.md) | Mobile-first iOS bottom tab bar (replace the sidebar) and fix color contrast | P2 | M | LOW-MED | 043,054 | **DONE**, merged to `main` (2026-08-19). The reviewer approved it: the bottom bar is in place, the hooks are intact and the tokens are lifted, and a headless browser check passed. Prior TODO description: user feedback said the sidebar looks wrong on mobile and the contrast is too faint. Replace the 054 sidebar with a native-iOS bottom tab bar (thumb-reachable, safe-area-aware) and lift `--label-secondary` and `--label-tertiary` to WCAG AA. **Supersedes 054's sidebar.** UI only; the 232 tests stay green. |
| [056](056-pwa-support.md) | PWA support: installable on iOS and Android, with standalone mobile chrome | P2 | M | LOW | none | **DONE**, merged to `main` (2026-08-19). The reviewer approved it: a public, root-scoped `/sw.js`, a static-only cache, and an installable app. Prior TODO description: the user said it 'should be PWA compatible, it's for mobile.' Add `manifest.webmanifest` (reusing the existing PNG icons), apple and theme-color head tags, and a public root-scope `/sw.js` that caches only `/static/` and never the live UI or API. Touches `app.py`, the head, `static/` and tests. 232→236. |
| [053](053-consolidate-import-tabs.md) | Consolidate "Import from Repo" and "Auto-Import" into one tab (one form, two actions) | P2 | M | MED | 034,035,048,049 | **DONE**, merged to `main` (`61ab07c`, 2026-08-19). Merges the two import tabs into one. A shared repo form has **Import now** (a streaming one-off) and **Save & auto-import** (a scheduled job), with the job list and schedule below. UI only (`templates/index.html` and a smoke-test update); both APIs already existed. |
| [050](050-storage-selftest.md) | Storage self-test: a write, read and delete round-trip that labels a 403 (permission) apart from a 404 (missing) | P2 | M | LOW | 011,028 | **DONE**, merged to `main` (`455ee25`, 2026-08-19). The reviewer approved it: the probe is always deleted, a 403 gets its own label, and the `exists()` contract is untouched. `POST /api/storage-selftest` exercises the active backend and reports each capability, naming a 403 separately. That was the exact blind spot behind the 2026-08-19 Garage debugging. The control is on the Health tab. Touches `app.py`, the template and tests. 222→226. |
| [051](051-in-app-diagnostics-log.md) | In-app diagnostics: a recent-errors ring buffer in the admin UI | P2 | M | LOW | none | **DONE**, merged to `main` (`5f65305`, 2026-08-19). The reviewer approved it: WARNING and above only, secrets redacted, a handler that never raises, and an auth gate. A bounded in-memory buffer of WARNING+ log records and `GET /api/diagnostics` (authenticated, secrets redacted) show on the Health tab, so incidents are visible in the browser instead of over SSH and `docker logs`. Touches `app.py`, the template and tests. 222→226. |
| [052](052-waitress-wsgi-server.md) | Serve behind Waitress (single-process WSGI) instead of the Flask dev server | P2 | S-M | LOW-MED | 048 | **DONE**, merged to `main` (`c19d0fc`, 2026-08-19). The reviewer approved it: `__main__` serves through Waitress after starting the scheduler, in a single process, and the Dockerfile is unchanged. Replaces the `app.run()` dev server with `waitress.serve` in `__main__`, which still starts the scheduler. **Single-process only.** The in-process lock and the 048 scheduler assume one process, so multiple workers are out of scope. Touches `requirements`, `app.py`'s `__main__` and a small test. |
| ~~044~~ | Research a non-Python backend replacement (Go vs Rust vs TypeScript) as a decision doc, not a migration | P4 | M | none | none | **DROPPED** by user request (2026-08-20). It was never executed, and the plan file is removed. It was a research spike that scored Go, Rust and TypeScript for a hypothetical backend rewrite. If the question comes back, reopen it by re-running `/improve next`. |
| [042](042-importer-auto-metadata.md) | On import, auto-fill the description from the release notes and the developer from the repo owner | P3 | S-M | LOW-MED | 034, 035 | **DONE**, merged to `main` (`d06d3dc`, 2026-08-18). Adds `release_body` to `ReleaseCandidate` (GitHub `body` or GitLab `description`) as the last field, with a default, so the engine's 21 tests stay intact. When an import creates an app, it fills `localizedDescription` (trimmed and capped) and `developerName` from the repo owner, only when the user left them blank and only on create. 197→202 tests. The reviewer confirmed engine backward-compatibility and the create-only, blank-only guard. |
| [037](037-source-always-parseable.md) | Never publish an unparseable source: coerce an empty `iconURL` and dedupe versions | P1 | S-M | LOW-MED | none | **DONE**, merged to `main` (`3eeb0cc`→`165bade`, pushed). 170→174 tests. Coercion and dedupe at serve time fix the live break with no migration, and an `add_version` idempotency guard stops new duplicates. Deploy the image to fix the live source. Prior TODO description: at serve time, `normalize_source` coerces an empty `iconURL` to a valid URL and dedupes versions, which fixes the current "isn't in the correct format" break with no migration. An `add_version` guard stops duplicates being stored. Touches `app.py` and tests. |
| [036](036-telegram-add-name-argument.md) | Telegram `/add <id> <ver> <name>` to set a custom app name for patched or same-name builds | P2 | S | LOW | none | **DONE**, merged to `main` (`165bade`→`b0b6c30`, pushed). An optional third argument overrides the IPA-detected name when the app is created, and in the chat confirmation. Bot and tests only; no server change. 174→178 tests. |
| [035](035-import-repo-usability.md) | Repo-import UX: normalize pasted URLs, auto-name new apps, optional icon | P2 | M | LOW-MED | 034 | **DONE**, merged to `main` (fast-forward `5a2d10e`→`3eeb0cc`, pushed). Changes the route, the form and tests, and leaves the engine untouched. 166→170 tests. With DNS blocked, the new tests were shown to make no network calls. |
| [034](034-ui-import-from-repo.md) | Admin-UI "Import from Repo" button (GitHub and GitLab) with a live progress bar | P2 | L | MED-HIGH | 029 | **DONE**, merged to `main` (fast-forward `0da4a1b`→`5a2d10e`, pushed). Four commits add 614 lines across the 4 in-scope files. The reviewer re-ran the engine's 21 tests (backward compatibility) and the full suite of 166; all pass. The 6 new route tests pass with **all real DNS blocked**, which proves they make no network calls. The route reuses the 029 engine in-process, publishes under the lock, cleans up temp files on every path, and enforces the GitLab allowlist. A 2026-08-31 check confirmed the commit is an ancestor of `3e0cd8a`. |
| [033](033-ci-self-hosted-runner.md) | Run the CI build/publish workflow on a self-hosted runner | P2 | XS | LOW-MED | none | **DONE**, merged to `main` (fast-forward `88e6a11`→`095231f`, 2026-08-17). All three jobs (`test`,`build`,`build-bot`) now use `runs-on: self-hosted`, and nothing else changed. |
| [032](032-add-app-icon-upload.md) | Make `/api/add-app` honor the icon the Add-App form already sends | P3 | S | LOW-MED | none | **DONE**, merged to `main` (2026-08-17). Commit `688e4bf` on branch `advisor/032-add-app-icon-upload`. The route now reads `iconFile` and `downloadIconFromUrl` and passes them through; the JSON branch is unchanged. 136→138 pass. Two new tests assert a self-hosted `/icons/...` URL and fail without the fix. A 2026-08-31 check confirmed the commit is an ancestor of `3e0cd8a`. |

Status values: `TODO` | `IN PROGRESS` | `DONE` | `BLOCKED` (with a one-line reason) | `REJECTED` (with a one-line rationale)

## 2026-08-28 audit addendum (Plans 074 to 078)

A full `standard`-depth audit ran at `5e8f447` with four parallel read-only agents. They covered `app.py` correctness and security; scripts, infrastructure and dependencies; tests, debt and DX; and performance, docs and direction. The advisor re-verified every finding in the table below. The operator selected findings #1 to #4 and #8, which became plans 074 to 078. The rest are **unselected but real**; re-run `/improve` or ask for a plan by number.

| # | Finding | Effort | Evidence |
|---|---|---|---|
| 5 | Flask 2.3.3, Werkzeug 2.3.7 and requests 2.31.0 predate the multipart-exhaustion and session/`.netrc` advisories; CI runs no `pip-audit` | M | `requirements.txt:1,4,5`, `Dockerfile.bot:8` |
| 6 | `/source.json` sends no ETag, Last-Modified or Cache-Control, and re-reads and deep-copies the catalog on every poll; the F-Droid index gets 304s for free | S | `app.py:2096-2102`, `:1179`, `:226` |
| 7 | Raw `str(e)` is returned on `/source.json`, `/ipas` and `/icons` (public) and on all of `/api/android/*`; a fixed-message convention already exists at `:3660,:3670` | S | `app.py:2105,2134,2163,3807-3955` |
| 9 | No `CLAUDE.md` and no lint baseline (`ruff --select E9,F` passes today except for a dead `import hashlib` at `app.py:13`) | S | none |
| 10 | Android test gaps: the `_download_to_temp` positive path is untested, and `_inspect_apk` is only stubbed in CI | S-M | `tests/test_android.py:34,210,413` |
| 11 | Extract the inline JS and CSS (2,275 of 2,776 lines, no Jinja) to `static/`; the service worker already caches `/static/` | S | `templates/index.html:17-641,1123-2774` |
| 12 | `add_app_manual` on an existing app inserts a duplicate version and `os.replace`s the old IPA; `add_version` has the guard | S | `app.py:1305-1345` vs `:1543` |
| 13 | Two in-app streaming downloaders whose cleanup behaviour has drifted apart | S | `app.py:1005-1051`, `:3727-3758` |
| 14 | The GitLab releases call forwards `PRIVATE-TOKEN` through redirects (`requests` strips only `Authorization`) | S | `scripts/release_source_ingest.py:434-443` |
| 15 | The iOS `bundleIdentifier` format is never validated, and `secure_filename` is many-to-one, so two ids can share a storage path (MED confidence) | S | `app.py:2252,2334,2377`, `:353` |

The audit also surfaced four direction options, none of them planned:

- D1. Generalise the sidecar's marker/`last-update.json` contract into a job runner for the four synchronous long operations (a spike).
- D2. Android repo branding. `config.yml` asks for `repo_icon: icon.png` and nothing writes it (S). Per-app graphics need fdroidserver's locale-dir layout (M-L).
- D3. A declared, documented notification event set that the UI exposes. Two events never fire by default, and no event covers an index-build failure.
- D4. Resolve the `RATE_LIMIT`/`Flask-Limiter` no-op. Either wire it up for `/api/login`, or delete both and record the decision as "proxy-level".

This round considered and rejected or deferred these:

- Admin URL-download SSRF. The shape is real, but the route is admin-gated on a private LAN, which matches the earlier "gated on the deployment assumption" verdicts. The error-reflection half is #7.
- `list_apps` parsing YAML on every request. It is admin-only and nothing polls it, so it is not worth changing.
- The health scan's O(versions) Garage HEADs. Garage is not in use.
- Hoisting the byte-identical Garage `selftest()`. The change would be cosmetic.
- Moving downloads off request threads. That is D1, not a bug.

Plans 079 to 081 came from unselected findings that were re-verified independently. They strip GitLab credentials on cross-host redirects (#14), sanitize raw exception responses (#7), and make manual app addition idempotent (#12). The dependency-upgrade finding (#5), caching (#6), the test gaps (#10), inline asset extraction (#11), duplicate downloader cleanup (#13), bundle-ID validation (#15), and the direction options remain unplanned. Write a focused plan for one only when it is selected.

The audit did not cover the Garage storage classes beyond their structure, or the `index.html` JS logic. It also skipped the zip and PNG parsing of untrusted IPAs in `_extract_ipa_icon` and `_decode_cgbi_png`, which is exposed to zip bombs and deserves its own pass. `pip-audit` could not run on this host, and `data/` was absent, so the performance claims rest on the shape of the code rather than on measurements.

## 2026-08-26 direction addendum (Plan 073, Android and F-Droid)

The operator asked for this with `/improve plan`: "the same but for Android and F-Droid (external 3rd-party repo)". The operator then answered two shaping decisions on 2026-08-26:

1. **Extend feather, don't fork.** It keeps one deployment, one login, a shared `data/` and shared sidecar patterns. The accepted cost is that `app.py` grows, and iOS-only assumptions (`bundle_id`, `.ipa`) stay iOS-only instead of being generalised. The Android path runs in parallel; it is not a refactor.
2. **An `fdroidserver` sidecar builds and signs the index.** F-Droid clients require a JAR-signed `index-v1.jar`/`entry.jar`. Hand-rolling that in Python was rejected as MED-HIGH risk for no benefit. Planning verified several facts about the official image (`registry.gitlab.com/fdroid/docker-executable-fdroidserver:master`, 857 MB). It runs only as **root**, because `/home/vagrant` is 0700 and uid 1000 dies on a GitPython check. It produces `index-v1.jar`, `index-v2.json` and `entry.jar` from `repo/*.apk` and `metadata/*.yml`, and it accepts `keystorepass: {env: …}`. `pyaxmlparser==0.3.31` and `PyYAML==6.0.3` have wheels for cp311 and cp314, as the HANDOFF pin trap requires.

Plan 073 is the foundation only: APK upload and URL ingest, metadata authoring, a rebuild trigger, the public `/fdroid/repo/` and `/fdroid/qr`, an Android tab, and a third CI image (`feather-fdroid`). Later plans, numbered when they are written, will cover Telegram APK ingest, release-import for APK assets, Garage/S3 hosting of the F-Droid repo (which changes the signed `repo_url`), and Android health and provenance folded into 069/071. Plan them after 073 lands. The 2026-08-28 audit took numbers 074 to 078.

## 2026-08-22 direction addendum (Plans 067 to 072)

A direction-focused audit at commit `f8a8827` produced six implementation-ready
options, and the user selected all six for planning. The verified baseline is
243 tests passing. These files are handoffs for other agents; this audit did
not modify application source.

- **067** makes multi-asset release failures inspectable and safely resolvable.
- **068** sets one shared IPA identity and platform preflight contract for
  every import path.
- **069** makes existing health findings actionable and keeps bounded state
  transitions for optional monitoring.
- **070** adds a recovery center that is catalog-only by design; it does not
  restore IPA or icon binaries.
- **071** keeps sanitized import provenance outside the public `source.json`,
  and is best-effort by design.
- **072** exposes the AltStore news and featured-app fields, which the source
  already supports, through guarded editorial controls.

Recommended execution order:

```text
067 ──> 068 ──> 071

069 ──> 070   (recommended ordering; no hard dependency)

072             (independent)
```

If you take only one plan next, start with **067**. It addresses the observed
`SceneBox-*.ipa` ambiguity and keeps publication fail-closed.

## 2026-08-17 audit addendum (Plans 029 to 032)

A fresh full audit on 2026-08-17 against commit `88e6a11` extended this file.
It covered `app.py` (1,838 lines), the container and CI config, and the
Telegram and Garage scripts. At that baseline, 136 tests pass.

Plan 029 (cron release imports) was written separately and is now indexed
above. Plans 030 to 032 come from the new audit. The audit found most
high-impact items already recorded in the "Deferred findings" and "considered
and rejected" sections below, and did not re-plan them. They stay gated on the
private-network deployment assumption, as they should. The three new plans
cover the findings that are both new and high-impact:

- **030** removes dead code and a dead dependency (`altparse`). Deleting the
  dormant `add_app_from_github` also retires the "Migrating off altparse"
  rejected entry below, because the dependency is being *removed*, not
  migrated. 030 interacts with 029. 029 deliberately reimplements GitHub/GitLab
  release polling through the HTTP-API boundary and forbids reusing
  `add_app_from_github`, and 030 deletes that inferior dormant importer. The
  two complement each other and need no ordering.
- **031** fixes a latent timezone bug. `get_current_dates()` uses
  `datetime.now()` (naive local time) but formats it with a literal `Z` (UTC).
- **032** fixes a real user-facing bug. The Add-App form's icon picker is wired
  in the frontend (`templates/index.html:541,945`), but `/api/add-app` never
  reads `iconFile`, so it silently discards the chosen icon.

```
001 (DATA_DIR) ──> 005 (tests) ──> 006, 007, 008 ──┐
                                                   ├──> 009 (extract template) ──> 010 (auth)
002 (git) ──> 003 (delete dead code)               │
004 (Pillow)  [independent, do anytime]  ──────────┘

005 ──> 011 (Garage S3 for IPA payload) ──> 028 (Garage app icons)
023 (Telegram thumbnail icon path) ────────> 028
025 (source artwork) ──────────────────────> 028
```

## Dependency notes

- **005 requires 001** because until then the app cannot be imported outside a container. `app.py:719` constructs `SourceManager` at module scope, which calls `os.makedirs("/app/data")` and raises `PermissionError` for any non-root user. This is the only hard blocker in the set.
- **006, 007, 008, 010 require 005** because they change runtime behaviour on a service whose entire product is one JSON file. Doing them without a regression net is what makes them risky.
- **003 requires 002.** It deletes 3,792 lines. Without git that is irreversible.
- **003 requires 004** (or 004's harvesting step) because `backup.old/requirements.txt` holds the only copy of the `qrcode[pil]` / `pillow` pins, and 003 deletes that file.
- **010 requires 009.** Plan 010 edits the frontend markup, which is much safer in a real `.html` file than inside a 1,493-line Python string literal.
- **007 after 006** is a recommendation, not a hard requirement. Both edit `SourceManager`, and doing 006 first avoids a merge conflict in `save_source`.
- **029's dependencies are all DONE.** It builds on the atomic, serialized catalog writes (006, 007), the login-gated mutating routes (010), the storage backend (011), the Compose pull convention (019), and the `Info.plist` metadata rule and first-publish creation from the Telegram path (021, 023). It can be executed on its own now; the dependency list records what its design assumes, not work still pending.
- **030, 031 and 032 are independent of each other** and depend on nothing else, so do them in any order. Each is a small self-contained change with its own regression test.

## If you only do three things

1. **004** is a one-line change. `/qr` is very likely returning 500 on every page load right now, and it is the primary onboarding path.
2. **007** fixes a silent failure. `data/source.json` currently publishes `"size": 0` for a 4.3 MB app, because a download failed silently and the caller reported success. The same code region deletes a hosted IPA *before* fetching its replacement.
3. **001 and 005** make the app testable, and everything else gets safer once it is.

## Notes on running the tests

Plan 005 establishes the suite. The host runs Python 3.14 and the container runs 3.11, and `Flask==2.3.3` may not install on 3.14. Plan 005 Step 1 tries a host virtualenv first and falls back to running inside the container. **Whoever executes 005 must record which path worked here**, because every later plan says "run the tests":

> Test command: `.venv/bin/python -m pytest tests/ -q` (host venv path, confirmed working with 21 tests passing in about 0.17s; no container fallback needed)

**Known-good environment setup**, established during Plan 001's review. Contrary to the concern above, `requirements.txt` installs cleanly on the host's Python 3.14:
>
> ```
> python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
> ```
> `flask`, `qrcode`, `requests` and `altparse` all import successfully, so Plan 005 should take the host-venv path, not the container fallback. `python3 -m venv -q` is **not** a valid flag, so leave it out.

**The host is Python 3.14.6; the container is Python 3.11.** Every pin in `requirements.txt` must therefore have a wheel for *both* `cp311` and `cp314`. Otherwise the host-venv path above breaks, and Plan 005 and everything downstream of it lose their test environment. This has already happened once. It stopped Plan 004's first execution attempt, because `pillow==10.1.0` publishes no `cp314` wheel. Check a candidate pin with:
>
> ```
> pip download --no-deps --only-binary=:all: --platform manylinux_2_28_x86_64 --python-version 311 -d /tmp/chk <pkg>==<v>
> pip download --no-deps --only-binary=:all: --platform manylinux_2_28_x86_64 --python-version 314 -d /tmp/chk <pkg>==<v>
> ```
>
> The host's system `python3` has no `pip` module, so use a venv's `pip`.

## Plan 004 verification (2026-08-11)

The executor could not run the plan's container steps. Its worktree holds only committed files, so the gitignored `.env` and `data/` are absent and `docker compose up` fails on the missing env file. The review ran those checks instead, by building images directly and using one-off `docker run` containers. That needs neither `.env` nor `data/`, and it does not disturb the deployment.

The **baseline image**, built from `main` before the fix, reproduced the audit's predicted failure exactly:

```
ModuleNotFoundError: No module named 'PIL'
backend: PyPNGImage
TypeError: PyPNGImage.save() got an unexpected keyword argument 'format'
```

This was the one finding rated MED confidence, because nobody had reproduced it in a container. The reproduction confirms that `/qr` was a guaranteed 500, not a probable one.

The **fixed image** (Python 3.11.15) has `PIL 11.3.0` and backend `PilImage`, and produces a valid 657-byte PNG.
The **host venv** (Python 3.14.6, same `requirements.txt`) has `PIL 11.3.0` and backend `PilImage`, and produces a valid 741-byte PNG.

The byte counts differ only because the wheels bundle different zlib builds. Both PNGs start with the PNG magic bytes, and both go through the PIL backend, which is what the fix is about.

Two things are **still unverified**, and only the live deployment can check them: that `GET /qr` returns `200 image/png` over HTTP, and that the page renders the QR rather than a broken image. Nothing was running at review time (`docker compose ps` was empty), so nobody observed this end to end. The in-process check above exercises the exact failing call at `app.py:2301-2307`, so the remaining risk is low. Still, on the next `docker compose up --build -d`, confirm that `/qr` returns a PNG.

**Pre-existing `SyntaxWarning`.** Plan 001's review noted it, and it is not a regression. Around line 1614, `app.py` emits `SyntaxWarning: "\/" is an invalid escape sequence` from a JavaScript regex inside `HTML_TEMPLATE`. The baseline commit has it too. Plan 009 moves this code into `templates/index.html`, where Python stops parsing it and the warning disappears on its own. Don't "fix" it in `app.py`.

## Plan 007 verification (2026-08-11)

These checks ran during review, because the executor's worktree has no `data/` or `.env` and so could not start a container.

**The regression tests discriminate.** Under the new 29-test suite, `main`'s pre-change `app.py` fails **7 of the 8 new tests**, including the critical one: `test_update_version_preserves_original_on_failed_fetch` gets 200 where it asserts 400. That is the silent-success bug reproducing on demand. After the change, all 29 pass in about 1.2 s.

An **end-to-end run in a container** built from the merged branch used a throwaway `DATA_DIR`, so it never touched the live deployment:

| Check | Result |
|---|---|
| `GET /source.json` | `200 application/json` |
| `GET /qr` | `200 image/png`, a real 390×390 PNG |
| `GET /` | `200` |
| `POST /api/add-app`, no `bundleIdentifier` | `400 {"error":"Bundle identifier is required"}`; it used to return the raw `KeyError` repr `'bundleIdentifier'` |
| `POST /api/add-app`, multipart, unreachable `downloadURL` | `400 {"error":"Failed to download IPA from http://127.0.0.1:9/nope.ipa"}`, and the app is **absent** from the catalog |

**Correction to the plan.** Its Step 6 repro used a JSON body and could never have passed. The JSON branch hardcodes `download_from_url = False`, so it never attempts a download; see the new deferred finding below on the JSON API. The multipart path is the one under test, and it behaves correctly. The plan text is fixed.

**Still unverified.** Nobody has clicked through add-app or update-version in the actual web UI. Some requests that used to return 200 now return 400, which is the point. But the frontend's error handling for those paths has only been read, not exercised.

## Live data issues found during Plan 011 recon (2026-08-11)

Three catalogued versions are **not IPAs**. They are gzip-compressed copies of a 5411-byte filebin landing page. The pre-Plan-007 `copyfileobj(response.raw, f)` bug wrote them to disk, because it skipped content-decoding:

```
1719 B  data/ipas/com.instagram.theta/408.1.0_TH.ipa
1719 B  data/ipas/com.instagram.ifgram/408.1.0_IF.ipa
1724 B  data/ipas/com.fouadraheb.watusi/B_25.36.10_WC.ipa
```

`data/source.json` advertises all three, so any device that tries to install them fails. Plan 007 stopped the code producing more of these; **nothing has repaired the existing three.** Fixing them means sourcing real binaries, or removing those three versions from the catalog by hand. `zipfile.is_zipfile()` detects them reliably, because an IPA is a ZIP.

The catalog also confirms the **Plan 008 `Host`-header bug in production**. Its `downloadURL` values are split between `http://feather.example.com/...` and `http://<nas-ip>:7000/...`, so `com.zhiliaoapp.musically` is reachable only from the LAN by IP.

Two orphans sit on disk with no catalog entry referencing them: `com.ryan.anymex/3.0.3.ipa` (42 MB) and `com.Michael-128.qbitControl/1.3.3.ipa` (4.3 MB, the case-collision twin).

**Deployment premise re-confirmed.** `example.com` resolves to `<proxy-ip>`, a private address, so the private-network assumption behind the deferred findings still holds.

## Garage S3 infrastructure (verified 2026-08-11)

| Endpoint | URL | Verified by |
|---|---|---|
| S3 API | `https://s3.example.com` | returns `<Region>garage</Region>`, `Forbidden: Garage does not support anonymous access yet` |
| Web (bucket `feather-repo`) | `https://feather-repo.web.example.com` | a missing key returns `NoSuchKey`, **not** `AccessDenied`, so website access is enabled and anonymous reads work |

The same openresty on `<proxy-ip>` that fronts the app terminates TLS for both, so devices can reach them. This is why Plan 011 uses a plain `302` to the web endpoint rather than presigned URLs or a proxy-stream fallback.

## Plan 006 verification (2026-08-11)

Breaking each of the two new guarantees in the worktree, one at a time, proved that its test discriminates.

**Lock.** Replacing `threading.Lock()` with `contextlib.nullcontext()`, with everything else identical, gives:
```
FAILED tests/test_routes.py::test_concurrent_add_version_all_land
1 failed, 4 passed
```
Restoring the lock gives `5 passed`. So the concurrency test is real, not decorative.

**Atomic write.** Reverting `save_source` to the old truncating `open(self.source_file, 'w')` gives:
```
assert (tmp_path / "source.json").read_bytes() == original_bytes
E       assert b'' == b'{"name": "T..., "news": []}'
```
The live catalog is **zero bytes** after a failed write, which is the exact failure this plan removes. Restoring the atomic write gives `34 passed`.

The lock sits correctly in all eight methods. It starts immediately before `load_source()` and spans through the `return`, so it covers the whole read-modify-write rather than only the save. A wrapper around just `save_source` would have looked correct in a diff and fixed nothing.

**Correction to the plan.** The `grep -c "os.replace"` criterion was wrong twice (`1`, then `3`). The baseline already returns `3`, from Plan 007's two real calls and a comment containing the string, so the correct post-change count is `4`. The executor caught this and did not improvise around it. The criterion is fixed, and now checks the substance rather than a count.

**Not verified.** The Docker and live-`curl` half of Step 6 did not run, because the worktree has no `data/` or `.env`. The backup and temp-file tests cover the equivalent behaviour against a `tmp_path` data dir. On the next `docker compose up --build -d`, confirm that `data/backups/` starts filling and that no `.source-*.tmp` files accumulate in `data/`.

## Operator tasks outstanding (2026-08-11)

The code for 008 and 011 is merged, but neither takes effect until a human does the tasks below. None of them can be done from an executor worktree, because `.env` and `data/` are gitignored and absent there.

**From Plan 008:**
1. Set `PUBLIC_BASE_URL=https://feather.example.com` in `.env` (never commit it). Use **HTTPS** and no trailing slash; this was corrected on 2026-08-11, because `http://` answers with a 301 to `https://`. Until then the app logs a warning and keeps deriving URLs from the `Host` header, so the code change has no effect.
2. Hand-fix one catalog entry, `com.zhiliaoapp.musically` version `43.4.0_AC`. Its `downloadURL` is `http://<nas-ip>:7000/ipas/...` and should be `http://feather.example.com/ipas/...`. **Make one edit, by hand.** A regex would clobber the four legitimate external URLs (github, michael-128.github.io, and two filebin).

**From Plan 011:**
3. Create the Garage bucket key and put the credentials in `.env` (`GARAGE_S3_ENDPOINT=https://s3.example.com`, `GARAGE_BUCKET=feather-repo`, `GARAGE_PUBLIC_BASE_URL=https://feather-repo.web.example.com`, plus the access key id and secret).
4. Run `.venv/bin/python scripts/migrate_ipas_to_garage.py` as a dry run first. Expect **8 uploadable, 3 skipped as corrupt, 2 orphans**. If the corrupt count is not 3, the data has drifted; stop and re-read the plan.
5. Run `... --apply`, then confirm one object byte for byte. This should print `200 124889851`:
   `curl -so /dev/null -w '%{http_code} %{size_download}\n' https://feather-repo.web.example.com/ipas/com.google.ios.youtube/20.49.5.ipa`
6. Only then set `STORAGE_BACKEND=garage` and restart. **Then start one real install on a device**, because nothing else proves that the iOS client follows the 302. If it fails, set `STORAGE_BACKEND=local` and restart. That rollback is why the flag exists.

**From Plans 025 and 028:**
7. After Plan 028 lands, set the source-level `iconURL` through **Source Information** to
   `https://f002.backblazeb2.com/file/S30000PUBLIC/MEDIA-PUBLIC/feather-tinker-1024.png`.
   Do not change `headerURL`, do not copy this value into app entries, and do not hand-edit
   `data/source.json`. Verify the exact value through the public `/source.json` response.

**Independent of both:** rotate the admin password, which is still unconfirmed and which Plan 010 turns into the admin login, and repair the three corrupt catalog entries.

## Plan 011 verification (2026-08-11)

Breaking three guarantees one at a time in the worktree proved that the tests catch each one:

| Break | Caught by |
|---|---|
| redirect branch disabled | `test_serve_ipa_redirects_when_backend_is_garage` |
| `exists()`-first 404 guard removed | `test_ipas_missing_file_404` (500 != 404), `test_serve_ipa_404_when_object_missing` |
| destructive delete injected before the staged upload | `test_update_version_preserves_original_on_failed_fetch` **and** `test_update_version_preserves_original_on_failed_upload` |

That last row is the important one. Plan 007's "never destroy the hosted binary before its replacement is complete" guarantee still holds on the Garage path, and both the old and the new test catch a regression.

A container built from the merged branch, on the default backend, returns `200 application/json` for `/source.json`, `200 image/png` for `/qr`, and `200 text/html` for `/`. With `STORAGE_BACKEND=garage` and nothing configured, it refuses to boot, naming all five missing variables and no values.

**Note on the merge.** The 011 executor's worktree was cut from a ref that predated the 008 merge, so its branch conflicted. The conflict was a harmless add/add, because both branches inserted a function right after `get_file_size` (`resolve_base_url()` from 008, `_require_garage_config()` from 011). Keeping both resolved it. A check afterwards confirmed that `resolve_base_url` still has 6 occurrences and `request.url_root` exactly 1.

**Not verified**, because they need credentials or the live host: the Garage `exists()` probe, the migration `--apply` run, and the device install.

## Plan 009 verification (2026-08-11)

The executor hit a session limit mid-report, so it produced no verification claims. It had committed cleanly first. Everything below was checked independently rather than taken on trust.

**The move is byte for byte.** Extracting lines 1161 to 2653 from the pre-merge `app.py` and diffing them against `templates/index.html` produces an empty diff. That is 1,493 lines with no reformatting or whitespace drift. `app.py` drops from 2,986 lines to 1,491.

**The Dockerfile `COPY` is the failure mode that matters**, because the host test suite passes either way and only the container breaks. Building the image and running a throwaway container checked it in both directions, without needing `data/` or `.env`:

```
with    COPY templates/ ./templates/  ->  GET / 200 text/html, qrImage present
without COPY templates/ ./templates/  ->  GET / 500 TemplateNotFound
```

`redirect` survived the import rewrite (Plan 011 needs it for `serve_ipa`), and `render_template_string` is gone. All 48 tests pass unmodified. As predicted, the pre-existing `SyntaxWarning` about the `\/` escape disappeared on its own, because Python no longer parses that JS regex.

This unblocks Plan 010, which can now edit the login modal in a real `.html` file.

## Plan 010 verification (2026-08-12)

The constraint that mattered was that four routes stay reachable with no credentials, or every subscribed iOS device breaks. Tests proved it in both directions rather than assuming it:

```
gated /source.json by hand      -> FAILED test_source_json_ok (401 == 200)
                                   FAILED test_public_routes_never_require_auth[/source.json]
removed @requires_auth from     -> FAILED test_mutating_routes_require_auth[/api/delete-app]
  /api/delete-app
```

A container check on the real image, with no session:

| | |
|---|---|
| `/source.json` `/qr` `/` | `200` |
| `/ipas/...` `/icons/...` | `404` for a missing file, **not** an auth wall |
| all six `/api/*` mutating | `401` |
| boot with no `ADMIN_PASSWORD` | refuses, with a clear message |
| healthcheck | `["CMD-SHELL","curl -f http://localhost:5000/source.json \|\| exit 1"]`; the credential is gone |

Review accepted one documented deviation. The executor edited `tests/test_storage.py`, although it is outside the plan's scope list. Plan 011's fixtures import `app.py` directly, and could not import it at all once `ADMIN_PASSWORD` became mandatory. The fix is mechanical, and of the same kind as the one the plan mandates for `test_routes.py`.

## Why Plan 012 does not use a Telegram bot

This is recorded so nobody proposes it again. Two facts from `core.telegram.org/bots/api` rule a bot out:

- **The cloud Bot API caps `getFile` downloads at 20 MB.** These IPAs are 83 to 353 MB. Only a self-hosted local Bot API server lifts the cap ("Download files without a size limit"), and that means an extra container.
- **Bots cannot read channel history.** There is no `getMessages`, and a bot sees only messages posted after it joins. The target is an existing message.

On top of that, the operator is **not an admin of that channel**, so the operator cannot add a bot to it at all. MTProto through Telethon is the only approach that works.

## Telegram ingest: what self-hosting the Bot API server does and does not fix

This is recorded because it is natural to assume that self-hosting removes *all* bot limits. It does not. The project README says:

> "The local server **does not change which messages a bot can access** — it only modifies operational capabilities, particularly regarding file handling."

| Limit | Cloud Bot API | Self-hosted `--local` |
|---|---|---|
| `getFile` download size | **20 MB** | no limit |
| `getFile` result | a URL | **an absolute local path** |
| Upload size | 50 MB | 2000 MB |
| Read channel history | never | **still never** |
| Access a channel you don't admin | never | **still never** |

The files here are 83 to 353 MB, so either bot design needs the local server. But the local server never lets a bot read a channel the operator only *belongs* to. That is why **Plan 013 is forward-to-bot** (the operator supplies the file) rather than fetch-by-link.

**Choosing Plan 013 over 012 is a security trade-off, not a preference.**

| | 013 (bot) | 012 (Telethon) |
|---|---|---|
| Credential | bot token, scoped and revocable in seconds through BotFather | `.session` file with **full account access** |
| If the host is compromised | attacker posts as an unfollowed bot | attacker reads every chat the account is in |
| Historical message | forward once by hand | fetched by link |
| Blocked by channel content protection | **yes, fatally** | no |

013 has the smaller blast radius and is the default. 012 exists only as the fallback in case forwarding is restricted, which many channels that distribute patched builds do.

## Plan 013 Step 0: RESOLVED (2026-08-12)

**Forwarding from the source channel works.** The operator forwarded a 31.1 MB IPA (`Immich_vv3.1.0-AppAssassin.ipa`, `app.alextran.immich` v3.1.0) to the bot successfully. The channel has no content protection.

That was the open question that decided whether Plan 013 was viable. It is viable, so **Plan 012 (Telethon / user account) stays shelved permanently** and should not be implemented. 013 has the smaller credential blast radius, and the fallback is not needed.

## Plan 020 root cause: `command:` does nothing for `aiogram/telegram-bot-api`

This is worth recording, because the override *looked* effective. That image's entrypoint ends:

```sh
append_arg_from_env "TELEGRAM_HTTP_PORT" "--http-port" "8081"
append_flag_from_env "TELEGRAM_LOCAL" "--local"
exec $COMMAND
```

`exec $COMMAND` **never forwards `"$@"`**, so the image ignored `command: ["--local", "--http-port=8081"]` entirely. The `--http-port=8081` visible in the running process came from the entrypoint's own default, which is why the override appeared to work.

As a result, the server ran in cloud-proxy mode, where `getFile` still enforces the 20 MB cap, so a 31 MB forward failed with `400 Bad Request`. Set every option for this image through `TELEGRAM_*` environment variables, and check it against the command line the container logs at startup.

**Credential note.** The Bot API puts the token in the URL *path*, and `requests` echoes the URL in every exception, so `logger.exception` wrote a live token into the container log. Plan 020 fixed this with central redaction. The exposed token still needs rotating through @BotFather.

## Catalog bundle IDs disagree with the IPAs they ship (2026-08-12)

Planning 021 read the `Info.plist` out of every IPA in `data/ipas/` and found this. **Three of eight catalogued apps declare a different bundle identifier than the binary they serve:**

| Catalog says | The IPA declares |
|---|---|
| `com.instagram.theta` | **`com.burbn.instagram`** |
| `com.instagram.ifgram` | **`com.burbn.instagram`** |
| `com.michael-128.qBitControl` | **`MikeMichael225.qBitControl`** |
| `com.zhiliaoapp.musically` | matches |
| `com.google.ios.youtube` | matches |
| `com.ryan.anymex` | matches |

Versions drift too. `YT_20.49.5_KP.ipa` and `YT_20.49.5_KP_Cracked.ipa` are catalogued as `20.49.5`, but both declare `CFBundleShortVersionString = 20.47.3`. **The filename lies; the plist does not.**

Some of this may be deliberate, because renaming a patched Instagram build lets it install beside the real one. But it should be a decision rather than an accident, because AltStore tracks updates by bundle identifier. **No plan fixes this. It needs a hand edit of `data/source.json`, and the operator must decide.**

## Telegram ingest end-to-end status (2026-08-12)

Every stage of Plan 013's pipeline now works in production except the final publish:

| Stage | Status |
|---|---|
| Forward from the source channel | works (Step 0 resolved) |
| `getFile` under `--local` | works for 31 MB, well past the cloud API's 20 MB cap |
| Shared-volume read | works, but needed `user: "101:101"` on the worker |
| Five validation checks | works |
| Metadata extraction (Plan 021) | works and reports `Detected: app.alextran.immich 3.1.0` |
| Login to feather | works; the log shows `POST /api/login 200` |
| **Publish** | **fails with `App not found`** (Plan 023) |

**The uid fix is not yet in the repo's `compose.yml`.** `telegram-bot-api` chowns its work dir to uid 101 and the worker image defaults to uid 999, so the worker cannot enter that directory. `os.path.exists()` returns `False` when permission is denied, and that shows up as the misleading `MountMismatchError: shared volume mounts disagree`. A direct reproduction:

```
same file, same mount --  as uid 999: False
                          as uid 101: True
```

The operator applied `user: "101:101"` to their Dockge copy. **The repo's `compose.yml` still lacks it** and should gain it, or the two will keep diverging.

## Reconcile log (2026-08-26, HEAD `764bd16`)

- **DONE plans spot-checked.** The full suite ran in a throwaway `python:3.12-slim` container with a read-only bind mount, because no local `.venv` exists any more. It gave **243 passed**, matching the count recorded for Plan 066. `ICON_STORAGE_BACKEND` is present at `app.py:124,1672,3138`, so 066's criteria hold.
- **012** is now REJECTED. The fallback was never needed, because 013's Step 0 passed on 2026-08-12. The **013** row is brought up to date to match.
- **022** is still valid; three `_redact` call sites still pass the mountpoint. Its drift block is refreshed, replacing the old `~/Documents/feather` path, the `91 passed` count and stale line numbers. No code change since the plan was written invalidates its approach.
- **067 to 072** were planned at `f8a8827`. Only `plans/` changed between that commit and HEAD, so they have not drifted. All are executable. The recommended order is 067, then 068, then 071, with 069, 070 and 072 independent.
- **Still outstanding, and not a plan.** The repo's `compose.yml` has no `user: "101:101"` on the Telegram worker (see "Telegram ingest end-to-end status"), while the operator's Dockge copy has it. The repo and the deployment still diverge.
- **Not verified:** the Docker image build, the live Garage endpoints and the production Telegram worker.

## Reconcile log (2026-08-31, HEAD `3e0cd8a`)

Sixteen commits landed since the 2026-08-26 reconcile at `764bd16`: plan 073's nine Android commits, the ghcr namespace move, plan 022, and the four post-073 commits `eaf7591`..`3e0cd8a`.

- **Test baseline re-verified.** In a throwaway venv on Python 3.14, `ADMIN_PASSWORD=x pytest tests/ -q` gave **307 passed, 1 skipped**, matching the figure `plans/HANDOFF.md` attributes to CI. No local `.venv` exists, so this check built one outside the repo and left the working tree untouched.
- **Merge status corrected.** Rows 029, 030, 031, 034 and 073 still carried "Not yet merged to `main`" or "awaiting merge" text from while the work sat on `advisor/*` branches. `git merge-base --is-ancestor` confirms that `26868ac`, `32a8116`, `4f686c9`, `0da4a1b`, and `f885e30`..`89de5ba` are all ancestors of `3e0cd8a`. All five rows are now up to date.
- **073's worktree reference retired.** `.claude/worktrees/agent-ab89652b51670fac4` no longer exists (`git worktree list` shows only the main checkout). Two orphan local branches, `worktree-agent-ab89652b51670fac4` and `worktree-agent-ade6674b6dbfeba85`, still point at already-merged work. They are safe to delete, but they stay in place because deleting branches is the operator's call.
- **Spot-checked DONE criteria at HEAD.** All of these hold:
  - 052: `waitress==3.0.2` is pinned, and `serve(...)` runs at `app.py:4941-4942` with `WAITRESS_THREADS`.
  - 073: `Dockerfile.fdroid` and `scripts/fdroid_index_loop.sh` are present, and `compose.yml` has ten `fdroid` references.
  - 074: `mutate_auto_import` is at `app.py:3140`, `_auto_import_scheduler_cycle` at `:3414`, and the run-lock at `:3079`.
  - 075: the `EXDEV` fallback is at `app.py:2180`.
  - 076: `-storepass:env` is at `scripts/fdroid_index_loop.sh:23,30`, so no password appears on a command line.
  - 077: the `Jenkinsfile` orders all three build and smoke stages before the single `Push images` stage at `:170`.
  - 081: the duplicate-version guard is at `app.py:1667-1668`, ahead of the storage block.
- **080 re-verified in depth**, because it is the one that touches security. Every public boundary returns a fixed message: "Source unavailable" at `app.py:2310`, "IPA unavailable" at `:2339`, "Icon unavailable" at `:2368`, "QR generation failed" at `:2389`/`:4755`, and "Not found" at `:4726`. So does every Android route (`:4765`, `:4775`, `:4833`, `:4857`, `:4877`, `:4896`, `:4910`, `:4921`). One residual is deliberately not a plan. The authenticated iOS admin routes (`app.py:2485`, `:2507`, `:2518`, `:2566`, `:2614`, `:3920`, `:3946`, `:3962`) still return `str(e)`. That was outside plan 080's stated scope of public and Android routes, and these routes sit behind `@requires_auth`, where the caller is the operator, who benefits from the detail. It is recorded here so nobody re-audits it as a new finding.
- **079's citation had drifted** and is refreshed in place. The redirect stripping is at `scripts/release_source_ingest.py:617-630` and `:672`, not at the `:594-599` recorded on 2026-08-26, which now lands on `_host_allowed`. The rejection itself still stands.
- **New plan opened: 082.** The `compose.yml` uid divergence, which the last two reconcile logs carried as a bare note, is now a written plan. It is the only executable item in the backlog.
- **`plans/HANDOFF.md` has uncommitted changes** in the working tree. They are a substantial refresh: a new "Immediate blocker" section on the missing `feather-fdroid` GHCR package, Dockhand replacing Dockge as the control plane, and a new trap about cached images. They stay uncommitted, because committing is the operator's call. This reconcile corrected the file's plan count from 80 to 81.
- **Not verified:** the Docker image builds, the live Garage endpoints, the production Telegram worker, the GHCR package state described in HANDOFF's "Immediate blocker", and any on-device behaviour.

## Reconcile log (2026-09-02, HEAD `936e1d4`)

This is a same-day follow-on to the 2026-08-31 reconcile. Plans 082 to 084 were executed and merged. 085 was written and executed but **not** merged.

- **The suite went from 307 to 346 passed, 1 skipped.** Plans 083 (engine) and 084 (watcher and UI) added APK support across all three import paths.
- **Three defects turned up after the fact.** All three trace back to plan 083's own text, and all are fixed:
  - `082b12a` adds a missing `.dockerignore` line. 083 added `COPY scripts/apk_inspection.py` to the `Dockerfile` but not the matching `!` line in the deny-by-default `.dockerignore`, so every Jenkins build failed. No test could catch it, because nothing in `tests/` builds an image.
  - `fd38864` removes the "at least one of bundleIdentifier or package" rule, which was unnecessary. Both fields are auto-detected from the artifact, and the watcher's iOS path already reads the id out of the IPA before touching the catalog.
  - `936e1d4` fixes `_group_matches_by_platform`, which dropped every asset for a platform whose identity field was blank. The error read `no eligible release had ... asset matching '<glob>'` and blamed the operator's glob. No such filter existed before 083 (`dc43400` has zero occurrences). Selection is by pattern alone, and identity is an assertion checked after inspection.
- **CI was never green before today.** `d7eeem/feather/main` had `lastSuccessfulBuild: None`. Build #4 was the first success, and builds #4 to #9 have all passed. This also explains the "missing" `feather-fdroid` GHCR package, because nothing had ever been pushed. All three packages now pull anonymously.
- **Deployed and verified live** at each step through the Dockhand MCP bridge. Each deploy recreated only `altstore-source-manager`, so the `feather-fdroid` sidecar was never at risk.
- **First real-world evidence.** A live `/inspect` against `RyanYuuki/AnymeX` tagged `AnymeX-Android-arm64-v8a.apk` and `AnymeX-Android-armeabi-v7a.apk` as `platform: android`. That confirms 084 works, and that multi-ABI releases need 085.
- **Not verified.** No APK has been imported end to end. Every test is offline by design.

## Findings considered and rejected

These are recorded so nobody re-audits them:

- **Path traversal on `/ipas/<bundle_id>/<filename>` and `/icons/<bundle_id>/icon.<ext>`.** An audit pass reported it, and checking proved it **false**. `app.py:2235-2238` applies `secure_filename` to *both* URL segments before `os.path.join`, and `app.py:2253-2259` does the same plus an allowlist check on the extension. The write paths are covered too, because `get_ipa_path` sanitises both `bundle_id` and `version`, and the icon writers sanitise `bundle_id` and limit the extension to a fixed set. It is not a finding.
- **`debug=True` in production.** It is not present; `app.py:2531` sets `debug=False`.
- **Splitting `SourceManager` into modules.** It has 25 cohesive methods over a single JSON document, with one responsibility. The file is large because 59% of it is a *string* (Plan 009), not because the class is oversized. Re-evaluate after the template extraction.
- ~~**Migrating off `altparse==0.3.0`**~~, **SUPERSEDED by Plan 030** (2026-08-17). The original verdict was "don't pre-emptively rewrite a working import path." The 2026-08-17 audit found that the path is not working but *dead*, because `add_app_from_github` (altparse's only user) has no route, caller, or test. So the answer is neither "migrate" nor "keep". *Delete* it and drop the pin, which Plan 030 does.
- **Adopting a frontend framework.** The UI is one page with about 770 lines of working vanilla JS. Extracting it to `static/app.js` (noted as a follow-up in Plan 009) gets nearly all the benefit at a fraction of the cost.

## Deferred findings: real, but out of scope

These are verified, and deliberately not planned, mostly because the deployment is on a private network. **If this is ever exposed publicly, revisit the first three immediately.**

- **SSRF with public read-back.** `app.py:82`, `149` and `389` fetch caller-supplied URLs, following redirects with no scheme or host validation. The fetched bytes land in `/ipas/`, where anyone can retrieve them anonymously. This is not blind SSRF, because the response body comes back. Exploiting it requires network access.
- **No rate limiting.** `requirements.txt:2` declares `Flask-Limiter==3.5.0`, but nothing imports it, and `RATE_LIMIT` in `.env` has no effect. Either wire it up or drop the dependency. (Plan 001 covers the disk-fill half by setting `MAX_CONTENT_LENGTH`.)
- **Werkzeug development server in production.** `Dockerfile:35` invokes `app.py:2531`, which runs `app.run()` over plain HTTP, and `compose.yml` has no reverse proxy. Moving to gunicorn interacts with Plan 006's in-process lock. **With `-w >1` that lock silently stops working**, and it would have to become a file lock.
- **`str(e)` returned to clients at 10 handlers.** They are at `app.py:2228, 2246, 2277, 2343, 2363, 2374, 2418, 2460, 2504, 2519`, and they leak absolute container paths and `requests` connection detail.
- **Flask 2.3.3 / Werkzeug 2.3.7.** `backup.old/requirements.txt:1` pinned `flask==3.0.0`, so the live pin is a *downgrade*, chosen arbitrarily rather than deliberately. Werkzeug 2.3.8 and later address a multipart resource-exhaustion advisory. Attempt the upgrade only after Plan 005 exists, because `data/app.log:33` records a Flask API removal (`send_file(cache_timeout=…)`) breaking this app once already.
- **Bundle-ID case normalisation.** `secure_filename` preserves case, so `data/ipas/` holds both `com.michael-128.qBitControl` (empty) and `com.Michael-128.qbitControl` (4.3 MB). Nothing 404s today, because that catalog entry uses an external URL, but it is a latent data-integrity hazard. This is a **migration**, not a patch, because it needs a rename step for existing directories. Do not ship a casual `.lower()`.
- **The JSON API silently ignores file and download parameters.** Plan 007's review found this. Every mutating route branches on `request.content_type`. The `else` branch, labelled "JSON request (backward compatibility)", hardcodes `ipa_file = None` and `download_from_url = False` (and likewise `icon_file` / `download_icon_from_url`). So the app accepts a JSON `POST /api/add-app` carrying `downloadFromUrl: true` with HTTP 200, and stores the caller's external `downloadURL` verbatim without fetching anything. The parameter is dropped without comment. The web UI uses multipart and is unaffected. Anyone driving the JSON API by hand, though, gets a catalog entry that silently means something different from what they asked for. The fix is either to honour the parameters in the JSON branch or to reject them explicitly. It is **not** to add URL validation (see the SSRF entry above).
- **Orphan reclamation.** `data/ipas/` has 10 directories for 8 catalogued apps, stranding about 45 MB (including `com.ryan.anymex`). It also has three empty directories, created by the `os.makedirs` side effect that Plan 007 removes. A reconciliation routine that *reports* orphans, without deleting them, would be a reasonable follow-up.
- ~~**No README / CLAUDE.md**~~, **RETIRED by Plan 027** (`4e2f2d6`). `README.md` now exists. The original finding was that nothing in the repo stated which of the three Python files was live. Plan 003 removed that ambiguity by deleting the dead copies, and Plan 027 documents the rest for anyone redeploying.

## Known live data issues (not code, fix by hand)

- `data/source.json` publishes `"size": 0` for `com.michael-128.qBitControl`. The real binary is 4,321,496 bytes and sits under the differently-cased `data/ipas/com.Michael-128.qbitControl/`. Plan 007 stops new instances of this; it does not repair the existing entry.
- **Rotate** the admin password in `.env`. The service ran with no authentication at all while that password sat in a mode-0755 file, so it protected nothing; treat it as burned. Do this before Plan 010 turns it into the admin login.

## Live-deployment note (2026-08-11)

The instance behind `https://feather.example.com` is **not** the checkout in this repo. It serves a 1-app catalog, while this repo's `data/` holds 8 apps and 1.3 GB. Whatever runs there has its own `DATA_DIR`.

On that instance, `com.faceboo.instagram.beegram 1.0.0` has `downloadURL = http://<nas-ip>:7000/ipas/...`, which is the Plan 008 bug, live. The binary itself is fine. It is a valid ZIP of 333,221,896 bytes, and the same file serves correctly at `https://feather.example.com/ipas/...`. Only the advertised URL is wrong, and no device off that LAN can reach it.

Fixing it needs three things **on that host**, not here:
1. Set `PUBLIC_BASE_URL=https://feather.example.com` in its `.env`.
2. Redeploy onto an image that contains Plan 008 (`ghcr.io/d7eeem/feather:latest` or newer).
3. Hand-correct the one existing `downloadURL`. The app rewrites URLs only when an entry is added or updated, so past entries do not fix themselves.
