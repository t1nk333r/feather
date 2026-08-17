# Plan 029: Poll GitHub and GitLab releases from cron and publish new IPAs

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If
> anything in the "STOP conditions" section occurs, stop and report — do not
> improvise. When done, update this plan's status row in `plans/README.md`
> unless a reviewer dispatched you and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 0983cb3..HEAD -- \
>   Dockerfile .dockerignore compose.yml .env.example README.md \
>   .github/workflows/docker-publish.yml release-sources.example.json \
>   scripts/release_source_ingest.py tests/test_release_source_ingest.py
> git diff --stat 0983cb3..HEAD -- app.py scripts/telegram_bot_ingest.py
> wc -l Dockerfile .dockerignore compose.yml .env.example README.md \
>   .github/workflows/docker-publish.yml app.py scripts/telegram_bot_ingest.py
> md5sum Dockerfile .dockerignore compose.yml .env.example README.md \
>   .github/workflows/docker-publish.yml app.py scripts/telegram_bot_ingest.py
> ```
> At planning time the first diff is empty. The second diff is also empty and
> is a dependency check: `app.py` and the Telegram worker are deliberately out
> of scope, but their HTTP/validation contracts shape this plan. Planned-at
> line counts are 66 / 26 / 83 / 59 / 137 / 192 / 1,816 / 747. Planned-at MD5
> values are:
>
> ```text
> aab1edaf978f3260be21d244254f35da  Dockerfile
> 4510a03da2a02c93b17d7f83ab3abe55  .dockerignore
> fdfbd475b9a446bb17ff78cbb6233140  compose.yml
> 4c62ac86848d16abc6018070e9f3f9fa  .env.example
> 0230a90bb8077bd94e39d7ca94c23111  README.md
> 5ee67904687f59ea5e00da690017f36f  .github/workflows/docker-publish.yml
> 51b126bae7c3024e5a661023c43f622e  app.py
> e82e7d3e346cca32d0011d37ee5b0e36  scripts/telegram_bot_ingest.py
> ```
>
> Line-number drift alone is harmless. If the application no longer exposes
> the authenticated multipart endpoints described below, catalog writes are
> no longer protected by one in-process lock, or the image/Compose conventions
> have changed semantically, that is a STOP condition.

## Status

- **Priority**: P2 — removes repetitive release checking and turns configured upstream releases into durable Feather-hosted versions
- **Effort**: L
- **Risk**: MED — this automates publication of installable code, mitigated by opt-in configuration, dry-run default, strict IPA/bundle validation, serialized runs, and no direct catalog/storage writes
- **Depends on**: Plans 006, 007, 010, 011, 019, 021, and 023 (all DONE)
- **Category**: direction / automation
- **Planned at**: commit `0983cb3`, 2026-08-17 (reconciled 2026-08-17 to `88e6a11`)

> **Reconciliation note (2026-08-17)**: `main` advanced by one commit since
> this plan was written — `88e6a11` ("fix: migrate legacy source artwork"),
> which edited `app.py` (module constants + `initialize_source` only) and added
> 2 tests. None of this plan's **in-scope** files changed, and the four route
> contracts (`/api/login`, `/api/app/<id>`, `/api/add-version`, `/api/add-app`)
> and the in-process `SourceManager` lock are all intact — verified. The only
> consequence for this plan is the test baseline: it is now **136**, not 134,
> so the full-suite total after this work is **136 + 21 = 157**, not 155. The
> counts below have been updated accordingly. The `app.py` MD5/line-count in the
> drift block above will not match `88e6a11`; that is expected line-content
> drift in an out-of-scope dependency file and is not a STOP condition.

## Why this matters

Feather can ingest an IPA manually or through Telegram, but it cannot poll a
release repository. A dormant GitHub-only method exists inside `SourceManager`,
yet no route or UI calls it, the pinned `altparse` parser has no GitLab adapter,
and importing `app.py` from cron would create a second process with a different
`threading.Lock` around the same `source.json`. That is unsafe.

This plan adds a one-shot importer designed to be launched by host crontab. It
discovers a single configured IPA asset from the latest eligible GitHub or
GitLab release, downloads and validates it to a temporary file, reads the real
bundle identifier and version from the app's top-level `Info.plist`, and uploads
it through Feather's authenticated API. Because the app remains the only writer,
catalog backups, atomic replacement, notifications, and local/Garage storage
routing all continue to work without a second implementation.

## Current state

### The existing GitHub path is dormant and is the wrong cron boundary

`app.py:996-1046` contains this in-process method:

```python
def add_app_from_github(self, data):
    with self._lock:
        source_data = self.load_source()
        ...
        sources_data = [{
            "parser": Parser.GITHUB,
            "kwargs": {
                "repo_author": data['repo_author'],
                "repo_name": data['repo_name']
            },
            "ids": [data.get('app_id', '')]
        }]
        srcmgr = AltSourceManager(src, sources_data)
        srcmgr.update()
        srcmgr.update_hashes()
        ...
        return self.save_source(updated_source), "Apps imported from GitHub successfully"
```

At `0983cb3`, this command finds only the definition and messages; there is no
route, form, or test:

```bash
rg -n "add_app_from_github|add-github" app.py templates/index.html tests
```

The pinned `altparse==0.3.0` exposes only `ALTSOURCE`, `GITHUB`, and `UNC0VER`
parsers. Its GitHub parser performs unauthenticated `requests.get()` calls with
no timeout and does not support GitLab. Do not expose or schedule this method,
and do not add a GitLab parser inside site-packages.

Most importantly, `SourceManager`'s lock is process-local. `app.py:1260-1327`
shows that `/api/add-version` holds it across the full read-modify-write, while
`save_source()` performs backup plus same-directory atomic replacement. A cron
process that imports `app.py` gets its own lock and can race the web process.

### The safe ingest boundary already exists

The app exposes:

- public `GET /api/app/<bundle_identifier>` to inspect current versions;
- authenticated multipart `POST /api/add-version` for an existing app;
- authenticated multipart `POST /api/add-app` for first publication.

`scripts/telegram_bot_ingest.py:395-453` is the exemplar. Its `FeatherClient`
logs in with a `requests.Session`, streams an open file object as `ipaFile`, and
never writes `data/ipas/`, Garage, or `source.json` directly. Its module
docstring states the invariant explicitly:

```python
publishes it to feather through the existing HTTP API
(`/api/login`, `/api/add-version`) -- it never writes to
`data/ipas/` or S3 directly.
```

Match that architecture. Do not import the Telegram worker or refactor it in
this plan; copy only the small behavioral patterns needed by a standalone
release importer. Plan 021's exact metadata rule must also be preserved:
only `^Payload/[^/]+\.app/Info\.plist$` identifies the main app. Filenames and
release tags are hints, not authoritative versions.

### Deployment and scheduling conventions

The deployment is Docker Compose under Dockge. Normal `docker compose up -d`
starts only `altstore-manager`; optional services carry a profile. The main
image is published as `ghcr.io/d7eeem/feather:latest`, and `.dockerignore` is
deny-by-default, so a new script must be explicitly re-admitted and copied.

Scheduling belongs to the TrueNAS host's crontab, not inside Flask and not in a
forever-sleep loop. The image should expose one opt-in Compose service that runs
once and exits. Docker documents `docker compose run` as the one-off command and
states that explicitly targeting a profiled service activates that profile.

### Provider API facts pinned by primary documentation

- GitHub's [List releases API](https://docs.github.com/en/rest/releases/releases?apiVersion=2026-03-10)
  returns release IDs, `draft`, `prerelease`, timestamps, and assets. Public
  repositories need no token; private repositories need read-only Contents
  permission. Use `X-GitHub-Api-Version: 2026-03-10`.
- GitHub's [Get a release asset API](https://docs.github.com/en/rest/releases/assets?apiVersion=2026-03-10)
  returns either `200` or `302` when requested with
  `Accept: application/octet-stream`; the client must handle both.
- GitLab's [Project Releases API](https://docs.gitlab.com/api/releases/)
  lists releases sorted by `released_at` and exposes `assets.links`; projects
  are addressed by numeric ID or URL-encoded path. Private access uses the
  `PRIVATE-TOKEN` header.
- GitLab release links may point at an external host. Provider credentials must
  never be forwarded to a different redirect host, and an operator-configured
  hostname allowlist must gate those downloads.

## Target architecture

```text
host crontab
    |
    | docker compose run --rm -T release-import --apply
    v
one-shot release-import container
    |-- read sources.json + atomic state/lock (dedicated mounted subdirectory)
    |-- GET GitHub/GitLab release metadata
    |-- stream one selected IPA into /tmp, validate ZIP/Payload/Info.plist
    `-- POST /api/login, then multipart /api/add-version or /api/add-app
                                      |
                                      v
                          Feather owns source.json + local/Garage storage
```

The importer has no route, no daemon loop, and no direct mount of Feather's
catalog, IPA, or icon directories. The only writable mount is its dedicated
`data/release-import/` configuration/state directory.

## Behavioral contract

### CLI

```text
python scripts/release_source_ingest.py [--config PATH] [--state PATH]
                                        [--job ID] [--apply]
```

- Dry-run is the default. It may query provider release metadata and Feather's
  public app endpoint, but it must not download an IPA, log in, publish, take a
  write lock, or create/modify the state file.
- `--apply` enables download and publication.
- `--job ID` selects one configured job; an unknown ID is configuration error.
- Process jobs independently. One failure does not prevent later jobs from
  being checked, but any failed job makes the final process exit `1`.
- Invalid configuration exits `2`. A concurrent apply already holding the lock
  prints one concise `already running; skipped` message and exits `0`.
- The final summary reports `checked`, `would-publish`, `downloaded`,
  `published`, `created`, `skipped`, and `failed` counts. Never print an auth
  header, token, password, or token-bearing URL.

### Manifest schema

Create `release-sources.example.json` with this schema and placeholder values:

```json
{
  "schemaVersion": 1,
  "jobs": [
    {
      "id": "example-github-app",
      "provider": "github",
      "project": "owner/repository",
      "bundleIdentifier": "com.example.app",
      "assetGlob": "*.ipa",
      "includePrereleases": false,
      "createIfMissing": false
    },
    {
      "id": "example-gitlab-app",
      "provider": "gitlab",
      "project": "group/project",
      "bundleIdentifier": "com.example.other",
      "assetGlob": "*.ipa",
      "allowedDownloadHosts": ["gitlab.com"],
      "createIfMissing": true,
      "name": "Example Other",
      "developerName": "Example Developer"
    }
  ]
}
```

Contracts:

- `schemaVersion` must equal integer `1`; `jobs` must be a non-empty list.
- `id`, `provider`, `project`, `bundleIdentifier`, and `assetGlob` are required,
  non-empty strings. Job IDs are unique.
- Providers are exactly `github` and `gitlab`. Unknown providers fail config;
  do not silently treat them as generic HTTP.
- GitHub `project` is exactly `owner/repository`, with neither URL nor `.git`.
  GitLab `project` is a numeric project ID or namespace path and is URL-encoded
  with `urllib.parse.quote(..., safe="")` before entering the API path.
- `assetGlob` uses `fnmatch`, case-insensitively. It must match exactly one IPA
  link in the selected release. Zero matches means continue to an older eligible
  release; more than one means fail that job rather than guessing.
- GitHub drafts are always excluded. `includePrereleases` defaults `false` and
  applies only to GitHub. GitLab releases with `released_at` in the future are
  excluded; GitLab has no equivalent boolean prerelease field.
- `allowedDownloadHosts` is required and non-empty for GitLab. Each entry is an
  exact lower-case hostname, not a URL, wildcard, IP literal, localhost, or
  private/reserved IP. The selected URL and every redirect must be HTTPS and
  land on one of these hosts. GitHub uses a built-in allowlist for
  `api.github.com`, `github.com`, and GitHub's release-asset host suffixes.
- `createIfMissing` defaults `false`. When true, non-empty `name` and
  `developerName` are required. This is the only condition that permits a new
  catalog app; no arbitrary publish error may fall through to creation.
- The manifest never contains tokens or the Feather password. Secrets live only
  in environment variables.

### Release and asset selection

Provider adapters return one normalized candidate containing provider, project,
release ID/tag/time, asset ID/name/declared size, API download URL, and the
credential scope host. Do not put token-bearing headers into this object or the
state file.

Sort eligible releases newest-first by their provider timestamp. Walk them in
that order:

1. no asset matches `assetGlob` → try the next release;
2. exactly one asset matches → select it and stop;
3. multiple assets match → fail with the job/release/asset names, requiring the
   operator to narrow `assetGlob`.

Do not compare semantic versions or trust tag formatting. The provider timestamp
chooses a candidate; the downloaded IPA supplies the published version.

### Download and trust boundary

- Use `requests.Session` and streamed chunks; never load an IPA into memory.
- Default timeout is 900 seconds and default maximum is 2 GiB. Environment
  variables `RELEASE_IMPORT_TIMEOUT` and `RELEASE_IMPORT_MAX_BYTES` accept
  positive integers and override them.
- If a provider declares size, reject it before download when it exceeds the
  maximum and reject after download when actual bytes differ. Always enforce
  the streaming maximum even when no size was declared.
- Implement redirects explicitly with a maximum of five hops. Validate HTTPS
  and the allowed hostname at every hop. Send `Authorization` or
  `PRIVATE-TOKEN` only to its original provider API hostname; strip both before
  following any cross-host redirect. Never put tokens in a query string.
- Stage under `tempfile` and delete the file in `finally` on success, skip, or
  failure.
- Hard validation before Feather login:
  1. actual filename ends in `.ipa`, case-insensitive;
  2. `zipfile.is_zipfile(path)`;
  3. archive has a `Payload/` entry;
  4. exactly one `^Payload/[^/]+\.app/Info\.plist$` exists and parses;
  5. extracted bundle identifier and version are non-empty;
  6. extracted bundle identifier exactly matches manifest `bundleIdentifier`.
- Use `CFBundleShortVersionString`, falling back to `CFBundleVersion`; use
  `CFBundleDisplayName`, then `CFBundleName`, only for diagnostic output.
- Compute and log the full SHA-256. It is provenance evidence, not a secret.

The configured upstream repository is a trusted publisher: anyone able to
replace its release asset can control what this automation offers to devices.
Validation proves the bytes are an IPA for the configured bundle, not that the
code is benign or signed by an expected certificate. Certificate verification
and code-signing policy are explicitly out of scope.

### Feather publication and idempotency

Use a dedicated `FeatherClient`, patterned after the Telegram worker:

- `get_app(bundle_id)` calls the public app route with a 30-second timeout and
  returns `None` only for `404`; other failures raise.
- `login()` posts the password through JSON and retains the session cookie.
- `add_version()` and `add_app()` stream multipart `ipaFile`; neither reads the
  file into memory. Give publication a configurable timeout at least as large
  as `RELEASE_IMPORT_TIMEOUT`.
- If the app exists and already has the exact extracted version, do not upload.
  Record the candidate as reconciled and report `skipped-existing`.
- If the app exists without that version, log in lazily and call
  `/api/add-version`.
- If the app is absent and `createIfMissing=false`, fail before login/upload.
  If true, call `/api/add-app` with manifest name/developer and the extracted
  bundle/version.
- If an app disappears between the GET and `/api/add-version`, creation is
  allowed only when `createIfMissing=true` and the API error is exactly
  `App not found`, matching Plan 023. Any other error is reported unchanged.
- A successful existing-version reconciliation or API publish advances state.
  Failed validation/download/auth/publication never advances it.

State lives at `RELEASE_IMPORT_STATE` and has this non-secret shape:

```json
{
  "schemaVersion": 1,
  "jobs": {
    "example-github-app": {
      "provider": "github",
      "project": "owner/repository",
      "releaseId": "123",
      "assetId": "456",
      "bundleIdentifier": "com.example.app",
      "version": "1.2.3",
      "sha256": "...",
      "publishedAt": "2026-08-17T00:00:00Z"
    }
  }
}
```

On apply, take a non-blocking exclusive `fcntl.flock()` on a sibling lock file.
Write state atomically in the same directory with `mkstemp`, `flush`, `fsync`,
`chmod 0600`, then `os.replace`. Keep state for jobs removed from the manifest;
do not silently prune operator history.

If state identifies the same release+asset and the catalog still contains the
recorded bundle+version, skip without downloading. If state is missing/stale,
download once, extract metadata, and reconcile against the catalog before any
upload. This makes recovery from a lost state file safe. A release that reuses
an already-catalogued version is treated as existing and never replaces bytes;
same-version replacement remains an explicit operator action through the UI.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `136 passed` before changes |
| Focused tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q` | `21 passed`, no network |
| Full tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `157 passed` |
| Syntax | `.venv/bin/python -m py_compile scripts/release_source_ingest.py` | exit 0 |
| CLI | `.venv/bin/python scripts/release_source_ingest.py --help` | exit 0; shows `--config`, `--state`, `--job`, `--apply` |
| Compose syntax | `docker compose config --quiet` | exit 0 without printing resolved secrets |
| Default services | `docker compose config --services` | only `altstore-manager` unless another profile is explicitly enabled in the operator environment |
| Import profile | `docker compose --profile release-import config --services` | includes `altstore-manager` and `release-import` |
| Image | `docker build -q -t feather-plan029 .` | prints image ID |
| Image script | `docker run --rm feather-plan029 python scripts/release_source_ingest.py --help` | exit 0 |
| Diff hygiene | `git diff --check` | exit 0 |

The six existing loopback tests can fail with `PermissionError` in a restricted
sandbox. Do not weaken or skip them; run the full suite where loopback binds are
permitted, as established by prior plan reviews.

## Scope

**In scope — the only product files the executor may create or modify:**

- `scripts/release_source_ingest.py` — new one-shot provider poller, validator,
  Feather client, state/locking, and CLI
- `tests/test_release_source_ingest.py` — new no-network unit tests
- `release-sources.example.json` — non-secret manifest example
- `Dockerfile` — copy the one new script into the existing main image
- `.dockerignore` — re-admit exactly the one new script
- `compose.yml` — add the opt-in, one-shot `release-import` service
- `.env.example` — key names/comments for provider tokens and importer limits
- `README.md` — configuration, dry-run/apply, and crontab instructions
- `.github/workflows/docker-publish.yml` — assert the published main image
  contains a runnable importer CLI
- `plans/README.md` — status row only

**Out of scope — do not touch:**

- `app.py`, `templates/index.html`, or any existing route. The feature uses the
  existing HTTP boundary and adds no UI.
- `scripts/telegram_bot_ingest.py`, its tests, or `Dockerfile.bot`. Match their
  patterns without refactoring working ingestion code during this feature.
- `data/source.json`, `data/ipas/`, `data/icons/`, Garage credentials, bucket
  policy, or any live object. The worker never mounts or writes them.
- The dead `SourceManager.add_app_from_github()` method. Removing it is a small
  separate cleanup; scheduling it is forbidden.
- `altparse`, site-packages, `requirements.txt`, or `requirements-dev.txt`.
  `requests` and all IPA parsing primitives are already available.
- Git tags without releases, GitHub Actions artifacts, GitLab pipeline job
  artifacts, package registries not linked from a Release, branch/nightly
  builds, web scraping, Gitea, Forgejo, Bitbucket, or self-hosted GitHub/GitLab.
- Icon discovery or replacement. Provider releases do not have Telegram's
  normalized thumbnail, and Plan 023 rejected IPA icon extraction.
- Signature/notarization verification, certificate pinning, or malware scanning.
- Editing the live host's crontab. The executor documents the exact operator
  command; the operator enables it only after a real dry run and one manual apply.

## Git workflow

- Branch: `advisor/029-cron-release-imports`
- Use conventional commits matching the repository, for example:
  `feat(ingest): poll repository releases`,
  `test(ingest): cover scheduled release imports`, and
  `docs(ingest): document cron release polling`.
- Keep worker/tests, container wiring, and documentation as separate logical
  commits. Do not push or open a PR unless instructed.

## Steps

### Step 1: Add the manifest, config validation, and dry-run CLI shell

Create `scripts/release_source_ingest.py` with no network at import time. Define
small data structures for a validated job and normalized release candidate,
specific exceptions (`ConfigError`, `ProviderError`, `ValidationError`,
`FeatherAuthError`), and `load_config` / `load_manifest` functions.

Read paths from `--config` / `--state`, falling back to
`RELEASE_IMPORT_CONFIG` / `RELEASE_IMPORT_STATE`. Read provider tokens only
from `GITHUB_TOKEN` and `GITLAB_TOKEN`; public jobs must work with both unset.
Read existing `FEATHER_BASE_URL` and `FEATHER_ADMIN_PASSWORD` only for apply.
Dry-run must be able to start without the Feather password because it performs
no mutation.

Implement the CLI flags, summary accumulator, and per-job error isolation now,
with provider/publish functions injected or separable enough for fake sessions.
Create the example manifest exactly to the schema above.

**Verify**:

```bash
.venv/bin/python -m py_compile scripts/release_source_ingest.py
.venv/bin/python scripts/release_source_ingest.py --help
```

Expected: both exit 0; importing the module creates no files and makes no
network calls.

### Step 2: Implement strict GitHub and GitLab provider adapters

Use fixed API origins `https://api.github.com` and
`https://gitlab.com/api/v4`. No configurable base URL in v1.

GitHub:

- `GET /repos/{owner}/{repo}/releases?per_page=100` with recommended JSON
  Accept header and `X-GitHub-Api-Version: 2026-03-10`;
- optional `Authorization: Bearer ...` only when `GITHUB_TOKEN` is set;
- exclude drafts and prereleases unless the job opts in;
- download through the chosen asset's API `url` with
  `Accept: application/octet-stream`.

GitLab:

- `GET /projects/{quote(project, safe='')}/releases?per_page=100&order_by=released_at&sort=desc`;
- optional `PRIVATE-TOKEN` only when `GITLAB_TOKEN` is set;
- exclude releases whose `released_at` is in the future;
- match only `assets.links`, never generated `assets.sources` archives;
- use the selected link's URL, guarded by `allowedDownloadHosts`.

For both, call `raise_for_status()`, apply the exact selection algorithm, and
surface job/provider/status/rate-limit reset information without response body
or credentials. A 403/429 is a failed job; do not busy-retry inside one cron run.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q \
  -k 'github or gitlab or provider'
```

Expected: provider tests pass using fake responses; no DNS/network access.

### Step 3: Stream, redirect safely, and validate the IPA

Implement explicit redirect handling, maximum size, declared-size check,
temporary-file cleanup, SHA-256, ZIP/Payload validation, and exact plist
extraction according to the behavioral contract. Never use the release tag or
asset filename as the published version. Reject a bundle mismatch before any
Feather authentication attempt.

Make error messages name job, release tag/ID, and failed invariant. Do not log
response bodies or request headers. Ensure a GitLab `PRIVATE-TOKEN` and GitHub
`Authorization` header disappear on a cross-host redirect; requests' default
handling of a custom `PRIVATE-TOKEN` header is not sufficient evidence.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q \
  -k 'download or redirect or validate or metadata or bundle'
```

Expected: all focused tests pass and every temporary file is absent after each
test.

### Step 4: Publish through Feather and make apply runs idempotent

Implement `FeatherClient`, exact-version reconciliation, optional first-app
creation, lazy login, atomic state writes, and non-blocking run locking.

The state check is an optimization, not the authority. Confirm its recorded
bundle/version against `GET /api/app/<bundle>` before skipping. Conversely, if
the state is gone, download/extract and inspect the catalog before uploading so
a restored deployment does not create duplicate versions.

Do not advance state until the API reports success or the exact extracted
version is already present. On API JSON parse errors or unexpected response
shapes, fail the job rather than assuming success.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q \
  -k 'feather or publish or state or idempotent or overlapping or failure'
```

Expected: all focused tests pass. Inspect fake-client call order: validation
precedes login; state replace follows publication; no duplicate upload occurs.

### Step 5: Wire the one-shot container without changing default startup

In `.dockerignore`, add only:

```text
!scripts/release_source_ingest.py
```

In `Dockerfile`, copy it to `/app/scripts/release_source_ingest.py`; do not use
`COPY scripts/` and do not change the main `CMD`.

Add a `release-import` Compose service:

- profile `release-import` so plain `docker compose up -d` remains unchanged;
- the same `image: ghcr.io/d7eeem/feather:latest` and `build: .` convention as
  `altstore-manager`;
- entrypoint `python /app/scripts/release_source_ingest.py`;
- `env_file: .env`;
- `depends_on: altstore-manager`;
- one bind mount only:
  `./data/release-import:/var/lib/feather-release-import`;
- environment paths pointing config/state beneath that mount;
- no port, no access to the app's data mount, and `restart: "no"`.

Keep dry-run as the service default. `docker compose run --rm -T release-import
--apply` must append `--apply` to the entrypoint.

**Verify** with a temporary fake `.env` containing no real secret if the
worktree has no `.env`:

```bash
docker compose config --quiet
docker compose config --services
docker compose --profile release-import config --services
docker build -q -t feather-plan029 .
docker run --rm feather-plan029 python scripts/release_source_ingest.py --help
```

Expected: syntax valid; the importer is absent from default services and
present under its profile; the image command exits 0. Remove any temporary
`.env` and confirm it is untracked/ignored before continuing.

### Step 6: Document configuration and the host crontab rollout

Update `.env.example` with names and comments only:

```text
RELEASE_IMPORT_CONFIG=
RELEASE_IMPORT_STATE=
RELEASE_IMPORT_TIMEOUT=
RELEASE_IMPORT_MAX_BYTES=
GITHUB_TOKEN=
GITLAB_TOKEN=
```

Clarify that provider tokens are optional for public repositories, must be
read-only for private repositories, and are never placed in the JSON manifest.
Reuse the existing `FEATHER_BASE_URL` / `FEATHER_ADMIN_PASSWORD` variables.

Update README optional features and repository layout. Include this operator
sequence, adapted to the deployment path already documented in
`plans/HANDOFF.md`:

```bash
mkdir -p data/release-import
cp release-sources.example.json data/release-import/sources.json
chown -R 999:999 data/release-import

# Edit sources.json, then prove discovery without downloading or publishing.
docker compose run --rm -T release-import

# Publish once by hand and immediately rerun to prove idempotency.
docker compose run --rm -T release-import --apply
docker compose run --rm -T release-import --apply
```

The second apply must report all unchanged jobs skipped and zero failures.
Only after that should the operator run `command -v docker`, use that absolute
path, and add a host crontab entry such as every six hours:

```cron
17 */6 * * * cd /path/to/feather && /usr/bin/docker compose run --rm -T release-import --apply >> /var/log/feather-release-import.log 2>&1
```

State that `/usr/bin/docker` and the stack path are examples that must match the
host, and that the executor must not edit live crontab. Document `--job ID` for
manual recovery and how to disable scheduling by removing/commenting the cron
line; the Compose service remains inert otherwise.

**Verify**:

```bash
rg -n "release-import|release_source_ingest|GITHUB_TOKEN|GITLAB_TOKEN|crontab" \
  README.md .env.example compose.yml Dockerfile .dockerignore
```

Expected: the feature, dry-run-first rollout, optional tokens, and cron command
are discoverable; no token value exists.

### Step 7: Add the image-level CI assertion

In the existing main-image smoke test, after deriving `IMAGE` and before booting
the server, run the image's importer with `--help` and assert the output contains
`--apply`. Do not add a third published image or a new workflow job.

This check catches both missing Dockerfile `COPY` and missing `.dockerignore`
admission. It does not call a provider or need credentials.

**Verify**:

```bash
rg -n "release_source_ingest.py --help|--apply" .github/workflows/docker-publish.yml
```

Expected: one importer smoke invocation and one assertion in the main build job;
the bot job is unchanged.

### Step 8: Add the full no-network regression suite

Create `tests/test_release_source_ingest.py`. Use fake response/session and fake
Feather client objects, real temporary ZIP/plist fixtures, and `tmp_path` state.
No test may call GitHub, GitLab, Feather, Docker, or DNS.

Add exactly these 21 tests:

1. `test_load_manifest_accepts_github_and_gitlab_jobs`
2. `test_load_manifest_rejects_unknown_duplicate_or_unsafe_jobs`
3. `test_config_errors_and_logs_never_expose_tokens`
4. `test_github_selects_latest_published_release_with_one_matching_asset`
5. `test_github_prerelease_requires_opt_in`
6. `test_gitlab_selects_latest_released_asset_link`
7. `test_provider_rejects_zero_or_ambiguous_matching_assets`
8. `test_download_strips_provider_auth_on_cross_host_redirect`
9. `test_dry_run_never_downloads_logs_in_or_writes_state`
10. `test_same_release_and_catalog_version_skips_without_download`
11. `test_download_streams_and_enforces_declared_and_configured_sizes`
12. `test_validate_rejects_non_ipa_and_missing_payload`
13. `test_extract_metadata_uses_only_top_level_app_plist`
14. `test_bundle_mismatch_fails_before_feather_login`
15. `test_existing_app_publishes_multipart_add_version`
16. `test_missing_app_creation_requires_explicit_config`
17. `test_create_fallback_triggers_only_on_exact_app_not_found`
18. `test_state_advances_only_after_verified_publish`
19. `test_second_run_is_idempotent_after_state_or_catalog_recovery`
20. `test_overlapping_run_exits_without_publishing`
21. `test_one_job_failure_does_not_block_remaining_jobs_and_sets_exit_one`

The multi-condition tests should use parameterization where useful but retain
the exact 21 top-level names. Follow `tests/test_telegram_bot_ingest.py` for fake
HTTP clients and IPA fixtures, without importing or modifying it.

Prove three load-bearing tests discriminate, one temporary break at a time:

- let dry-run call the download function → test 9 fails;
- change the plist match to loose `Info.plist$` → test 13 fails;
- advance state before publication → test 18 fails.

Restore each break and rerun the full suite.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
```

Expected: `21 passed` focused and `157 passed` total. Existing tests must not be
edited to accommodate the new feature.

## Test plan

- Manifest tests cover schema/version, duplicate IDs, provider-specific fields,
  creation metadata, host restrictions, and absence of secrets.
- Provider tests cover ordering, draft/prerelease/upcoming exclusion, URL
  encoding, exact-one asset matching, and status/rate-limit failures.
- Download tests use fake chunked responses and redirect chains; they prove size
  ceilings, auth stripping, temp cleanup, and hard IPA validation.
- Metadata tests build real ZIPs with top-level and nested plists, matching the
  Plan 021 regression shape.
- Publication tests assert exact endpoints/multipart fields, lazy login,
  existing-version reconciliation, guarded creation, and preserved errors.
- State tests assert atomic timing, recovery when state is lost, no duplicate
  upload, and non-overlapping applies.
- End-to-end orchestration tests remain fake-network and inspect summaries/exit
  codes. Real provider, real Feather, and crontab checks are operator tasks.

## Done criteria

All must hold:

- [ ] The importer supports exactly GitHub and GitLab release assets through a
      provider adapter boundary; unknown providers fail configuration.
- [ ] Default invocation is dry-run and makes no asset download, login, publish,
      lock, or state write.
- [ ] `--apply` streams to a temp file, enforces timeout/max/declared size,
      deletes temp files, and validates ZIP, Payload, exact plist, bundle, and
      extracted version before login.
- [ ] GitHub draft/prerelease and GitLab future-release rules match this plan;
      multiple matching assets fail instead of being guessed.
- [ ] Provider tokens are optional for public repos, header-only for private
      repos, stripped on cross-host redirect, absent from manifest/state/logs.
- [ ] No script imports `app.py`, invokes `add_app_from_github`, writes
      `source.json`, or writes IPA/icon/Garage storage directly.
- [ ] Existing apps publish through `/api/add-version`; new apps require
      `createIfMissing=true`; the race fallback matches exactly `App not found`.
- [ ] Unchanged cron runs do not download or publish; lost state reconciles
      against the catalog without creating a duplicate version.
- [ ] Apply runs are mutually exclusive and state is written atomically only
      after success or verified existing-version reconciliation.
- [ ] `release-import` is inert under plain `docker compose up -d`, is one-shot
      when explicitly targeted, has no port, and mounts only its state directory.
- [ ] Main image contains the importer; main server `CMD` and bot image are
      unchanged; CI proves the importer `--help` path.
- [ ] `.env.example` adds names/comments only; sample JSON contains no secret.
- [ ] README documents manual dry-run, first apply, idempotency rerun, `--job`,
      host crontab, disable/rollback, and trusted-publisher limitations.
- [ ] `.venv/bin/python -m py_compile scripts/release_source_ingest.py` exits 0.
- [ ] Focused suite reports 21 passed; full suite reports 157 passed; all three
      deliberate breaks fail their named tests and pass again after restoration.
- [ ] `git diff requirements.txt requirements-dev.txt app.py templates/index.html
      scripts/telegram_bot_ingest.py tests/test_telegram_bot_ingest.py
      Dockerfile.bot` is empty.
- [ ] `git diff --check` exits 0 and `git status --short` contains only the
      in-scope files plus the permitted plan-index status update.

## STOP conditions

Stop and report instead of improvising if:

- A cron implementation appears to require importing `app.py`, direct
  `source.json` edits, or direct local/Garage writes. The HTTP API boundary is
  non-negotiable because the catalog lock is process-local.
- The existing `/api/login`, `/api/app/<id>`, `/api/add-version`, or `/api/add-app`
  request/response contract no longer matches this plan.
- Provider discovery requires scraping HTML, reading branch artifacts, or using
  a package registry that is not linked from a Release.
- A configured release produces zero candidate IPA assets across all eligible
  releases or more than one match on the selected release. Report the job and
  asset names; do not broaden/guess the glob.
- A provider returns an HTTP asset URL, disallowed host, private/reserved IP,
  redirect loop, or more than five redirects.
- The IPA bundle identifier differs from the manifest. Do not allow release tag,
  filename, or config to override extracted metadata.
- A new app would be created without `createIfMissing=true` and complete
  name/developer config, or on any error other than exact `App not found` in the
  race fallback.
- Tests need a real token, repository, provider API, Feather deployment, DNS, or
  live crontab.
- A new dependency seems necessary. The stdlib plus pinned `requests` is enough;
  report rather than editing requirements.
- Implementing self-hosted GitLab/GitHub requires a configurable API base. That
  materially expands the SSRF/auth-host model and needs a follow-up plan.
- Any existing test fails outside the documented loopback sandbox limitation.

## Maintenance notes

- The worker deliberately publishes actual IPA metadata, not release tags. A
  provider may publish multiple releases whose IPAs reuse one version; those are
  reconciled as existing and never replace bytes automatically.
- `allowedDownloadHosts` is a security boundary for GitLab's arbitrary release
  links. Future provider adapters must define their API origin, auth scope, and
  redirect allowlist before they can be registered.
- GitHub API version `2026-03-10` is current at planning time. GitHub guarantees
  at least 24 months of support after a newer version; a future upgrade should
  be a reviewed constant/test change, not an unversioned request.
- State is only an optimization/history ledger. The catalog remains authority,
  which is why every skip checks the recorded version still exists.
- The dedicated mount contains config, state, and lock only. If future code needs
  access to `source.json` or IPA files, that is architectural drift and should be
  rejected in review.
- Host cron captures stdout/stderr. Keep one-line per-job results and a final
  summary stable enough for log alerts; never convert failures into exit 0 except
  the intentional overlapping-run skip.
- Adding Gitea/Forgejo/Bitbucket or self-hosted forges should be a new adapter
  plan. Do not turn the manifest into a generic arbitrary-URL fetcher.
- Reviewer scrutiny should concentrate on redirect auth stripping, exact plist
  matching, state timing, dry-run purity, and the fact that normal Compose startup
  remains unchanged.
