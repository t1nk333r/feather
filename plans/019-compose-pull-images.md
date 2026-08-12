# Plan 019: Make `compose.yml` pull published images instead of building

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result. If anything in
> "STOP conditions" occurs, stop and report — do not improvise. When done,
> update the status row in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD
> grep -nE "build:|image:|dockerfile:" compose.yml
> docker manifest inspect ghcr.io/d7eeem/feather:latest      >/dev/null && echo "feather image: EXISTS"
> docker manifest inspect ghcr.io/d7eeem/feather-bot:latest  >/dev/null && echo "bot image: EXISTS"
> ```

## Status

- **Priority**: P2
- **Effort**: XS
- **Risk**: LOW–MED — this changes how the live stack obtains its code. The hybrid form in "Design" is what keeps it low.
- **Depends on**: **017 (mandatory)** — `ghcr.io/d7eeem/feather-bot` does not exist until 017 publishes it. Referencing it before then makes `docker compose pull` fail with a 404 on a stack that currently works. 018 (DONE) is also a prerequisite: pulling published images is only safe once the build context cannot leak `.env` or `data/`.
- **Category**: DX / deployment
- **Planned at**: 2026-08-12

## Why this matters

`compose.yml` builds both services from source:

```yaml
  altstore-manager:
    build: .
  ipa-ingest-bot:
    build:
      context: .
      dockerfile: Dockerfile.bot
```

Three consequences:

1. **The published images are unused.** `ghcr.io/d7eeem/feather` is built, smoke-tested and published on every push — and the live host ignores it and builds its own. The thing CI verified is not the thing that runs.
2. **`docker compose pull` does nothing** for these services, so the natural update command is misleading. Updating actually requires `docker compose up -d --build`.
3. **The host does the work.** Every deploy re-runs `pip install` on the TrueNAS box, needs the full source tree checked out there, and can drift from what CI tested.

`ghcr.io/d7eeem/feather` is now **public**, so pulling needs no `docker login` and no PAT on the host. That was the one operational blocker; it is gone.

## Design: keep `build:` alongside `image:`

Do **not** delete the `build:` keys. Compose accepts both, and together they give:

| Command | Behaviour |
|---|---|
| `docker compose pull` | fetches the published image |
| `docker compose up -d` | uses the pulled image |
| `docker compose build` | builds locally and **tags it as that image name** |
| `docker compose up -d --build` | builds locally, ignoring the registry |

So the default path becomes pull-based and reproducible, while local building still works unchanged for development or when the registry is unreachable. Deleting `build:` would buy nothing and remove the fallback.

**Use `:latest`, not a pinned SHA.** This is a single-admin self-hosted service where the update ritual is "pull and restart". SHA pinning would mean editing `compose.yml` on every deploy, which nobody sustains. The `sha-…` tags remain available for pinning a specific build when diagnosing something.

## Target state

```yaml
  altstore-manager:
    image: ghcr.io/d7eeem/feather:latest
    build: .
    container_name: altstore-source-manager
    ...

  ipa-ingest-bot:
    image: ghcr.io/d7eeem/feather-bot:latest
    build:
      context: .
      dockerfile: Dockerfile.bot
    ...
```

Everything else in both service blocks — ports, volumes, env_file, restart, profiles, depends_on, deploy limits — stays exactly as it is.

`telegram-bot-api` already uses `image: aiogram/telegram-bot-api:latest` and needs no change.

## Scope

**In scope**: `compose.yml` — adding one `image:` line to each of two services.

**Out of scope — do NOT touch**:
- The `build:` keys. They stay. Removing them is a STOP condition.
- `telegram-bot-api`'s service block.
- The `profiles: ["telegram"]` keys added by Plan 016. The Telegram services must remain opt-in.
- `Dockerfile`, `Dockerfile.bot`, `.dockerignore`, `.github/`, `app.py`, `scripts/`, `tests/`.
- Package visibility, registry auth, or anything in the GitHub UI.
- Pinning to a specific SHA.

## Steps

### Step 1: Confirm both images exist and are pullable anonymously

**Do this before editing anything.** The whole plan is invalid if the bot image is not published yet.

```
docker manifest inspect ghcr.io/d7eeem/feather:latest > /dev/null && echo "feather: ok"
docker manifest inspect ghcr.io/d7eeem/feather-bot:latest > /dev/null && echo "bot: ok"
```

Both must print `ok`. If the bot image 404s, **STOP** — Plan 017 has not landed, and this plan must not proceed.

Then confirm anonymous pull works (i.e. the packages really are public — no credentials in play):

```
docker logout ghcr.io 2>/dev/null
docker pull ghcr.io/d7eeem/feather:latest
```
→ succeeds. If it asks for credentials, the package is still private; **STOP and report** — the operator must flip visibility first, or this change breaks their deploy.

### Step 2: Add the two `image:` lines

Add `image:` above the existing `build:` in each of the two services, as shown in "Target state". Add a short comment in the style of the file's existing comments explaining the hybrid:

```yaml
    # image: + build: together -- `docker compose pull` fetches the CI-built
    # image (the one that was actually smoke-tested), while `docker compose
    # build` still works locally and tags the result as this name.
```

**Verify**: `grep -c "ghcr.io/d7eeem/feather" compose.yml` → `2`

### Step 3: Prove the file is valid and the services still resolve

```
printf 'ADMIN_PASSWORD=x\n' > .env
docker compose config --services                     # -> altstore-manager only
docker compose --profile telegram config --services  # -> all three
docker compose config | grep -E "image:|build:"      # both keys present for both services
rm -f .env
```

Then confirm no `.env` was committed: `git status --short` shows only `compose.yml`, and `git ls-files | grep -c "^\.env$"` → `0`.

### Step 4: Prove a pull-based start actually works

This is the check that matters — it proves the published image runs with this compose file:

```
printf 'ADMIN_PASSWORD=not-a-real-password\nDATA_DIR=/app/data\n' > .env
docker compose pull altstore-manager
docker compose up -d altstore-manager
sleep 10
curl -so /dev/null -w 'source.json %{http_code}\n' http://localhost:7000/source.json
curl -so /dev/null -w 'index       %{http_code}\n' http://localhost:7000/
docker compose down
rm -f .env
```
→ both `200`.

**If port 7000 is already in use on this machine**, skip this step rather than changing the port mapping, and say so in your report.

## Done criteria

ALL must hold:

- [ ] `grep -c "image: ghcr.io/d7eeem/feather:latest" compose.yml` returns `1`
- [ ] `grep -c "image: ghcr.io/d7eeem/feather-bot:latest" compose.yml` returns `1`
- [ ] `grep -c "build:" compose.yml` returns `2` — both `build:` keys survive
- [ ] `grep -c 'profiles: \["telegram"\]' compose.yml` returns `2` — the Telegram services are still opt-in
- [ ] `docker compose config --services` lists only `altstore-manager`
- [ ] `docker compose --profile telegram config --services` lists all three
- [ ] Step 4's pull-based start returns `200` for both URLs (or is documented as skipped, with the reason)
- [ ] `git diff --name-only` shows only `compose.yml`
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 78, unchanged
- [ ] `plans/README.md` status row updated

## STOP conditions

- `ghcr.io/d7eeem/feather-bot:latest` does not exist. Plan 017 has not landed; this plan cannot proceed.
- `docker pull` prompts for credentials after `docker logout`. The package is private and this change would break the operator's deploy.
- You are tempted to delete a `build:` key. Both stay — they are the local-development and registry-unreachable fallback.
- You are tempted to remove a `profiles:` key so the Telegram services start by default. Plan 016 made them opt-in deliberately.
- You are tempted to pin a SHA instead of `latest`. Out of scope; `latest` plus `docker compose pull` is the intended ritual.

## Maintenance notes

- **The update ritual becomes `docker compose pull && docker compose up -d`.** No `--build`. Anyone who keeps using `--build` gets a locally built image instead of the CI-verified one — same source, but not the same artifact, and it silently bypasses whatever CI checked.
- **`latest` is mutable.** A `docker compose pull` after a bad push deploys the bad build. The `sha-…` tags exist for rolling back: temporarily set `image: ghcr.io/d7eeem/feather:sha-<good>` and restart. Worth knowing before it is needed.
- **CI publishes the image before the smoke test runs**, so a broken `latest` can exist briefly even when the run ends red. That was noted in Plan 015's maintenance notes and matters more now that the deployment actually pulls `latest`. Reordering push-after-smoke is the fix and is still unplanned.
- **The two images are a matched pair by SHA**, since both jobs tag from the same commit. If feather and the bot ever need to be version-locked to each other, pin both to the same `sha-…` tag.
