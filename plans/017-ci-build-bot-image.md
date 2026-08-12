# Plan 017: Build and publish the ingest-bot image in CI

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If
> anything in "STOP conditions" occurs, stop and report — do not improvise.
> When done, update the status row in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD                                   # plan written against 1b84e19
> grep -c "Dockerfile.bot" .github/workflows/docker-publish.yml  # expect 0
> test -f Dockerfile.bot && echo "Dockerfile.bot present"
> ```

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW — additive CI only; no application code, no compose change.
- **Depends on**: 013 (DONE), 015 (DONE — the smoke-test pattern this reuses)
- **Category**: DX / CI
- **Planned at**: 2026-08-12, `1b84e19`

## Why this matters

Plan 013 added `Dockerfile.bot`, and **CI never builds it.** The workflow's build job hardcodes the default `Dockerfile`:

```yaml
      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
```

`grep -c "Dockerfile.bot" .github/workflows/docker-publish.yml` returns `0`.

So a syntax error, a bad base image, or a broken `pip install` in `Dockerfile.bot` is invisible until someone runs `docker compose --profile telegram up -d` on the live host — and discovers it at the worst moment. Every other artifact in this repo is validated on push; this one isn't.

There is a second, quieter problem. `Dockerfile.bot` pins its only dependency **independently** of `requirements.txt`:

```dockerfile
RUN pip install --no-cache-dir requests==2.31.0
```

`requirements.txt` also pins `requests==2.31.0`. Nothing keeps those two in sync. Bump one and the worker silently runs a different version from the app it talks to — the kind of drift that is invisible until it isn't.

## What this plan does NOT change

**It does not switch the deployment to pull images.** `compose.yml` currently builds both from source:

```yaml
  altstore-manager:
    build: .
  ipa-ingest-bot:
    build:
      context: .
      dockerfile: Dockerfile.bot
```

That means the published `ghcr.io/d7eeem/feather` image is **currently unused by the live deployment** — the host builds its own. Switching to `image:` would make deploys faster and reproducible, but it also requires the TrueNAS host to authenticate to ghcr.io (the package is private), which is a real operational change with its own failure mode. **That is a separate decision, deliberately out of scope here** — see "Maintenance notes".

This plan's value is validation: catch a broken bot image on push, not on deploy.

## Scope

**In scope**: `.github/workflows/docker-publish.yml` — one new job, plus one pin-drift check.

**Out of scope — do NOT touch**:
- The existing `test` and `build` jobs. The `build` job was carefully brought back to green in Plan 015 and its smoke test is verified working; **leave it byte-identical**. Add a *new* job rather than parameterising the existing one into a matrix.
- `compose.yml`. No `build:` → `image:` switch here.
- `Dockerfile`, `Dockerfile.bot`, `app.py`, `scripts/`, `tests/`, `requirements.txt`.
- Registry authentication settings, permissions blocks, or the tag scheme of the existing image.

## Design

A second job, `build-bot`, mirroring the existing one but for the bot image:

- Image name: `ghcr.io/<repo>-bot` (i.e. `ghcr.io/d7eeem/feather-bot`)
- Same trigger rules: build on PRs to validate, **push only on non-PR**, so a fork PR cannot publish
- Same `docker/metadata-action` tag scheme so the two images stay legible together
- `needs: test` — do not publish a bot image if the suite is red

**Its smoke test is different from feather's**, because the worker is not a server: it long-polls and exits on missing config. So the meaningful check is that it **fails fast and legibly** when unconfigured — the same trick Plan 015 used for `ADMIN_PASSWORD`:

```
run the image with no TELEGRAM_* env  ->  must exit non-zero
                                          and name the missing variables
```

That single assertion catches a broken base image, a failed `pip install`, a syntax error in the script, and a regression that lets the worker start unconfigured — which would be a security regression, since the allowlist lives behind that same config check.

## Steps

### Step 1: Add the pin-drift check to the existing `test` job

The two `requests` pins must agree. Add a step to the **`test` job** (this is the one exception to "don't touch existing jobs" — it is additive and touches no existing step):

```yaml
      - name: requests pin must match between requirements.txt and Dockerfile.bot
        run: |
          set -euo pipefail
          REQ="$(grep -oP '^requests==\K[0-9.]+' requirements.txt)"
          BOT="$(grep -oP 'requests==\K[0-9.]+' Dockerfile.bot)"
          echo "requirements.txt: $REQ   Dockerfile.bot: $BOT"
          [ "$REQ" = "$BOT" ] || { echo "FAIL: requests pins have drifted"; exit 1; }
```

**Verify locally**:
```
grep -oP '^requests==\K[0-9.]+' requirements.txt
grep -oP 'requests==\K[0-9.]+' Dockerfile.bot
```
→ both print the same version (`2.31.0` at time of writing).

### Step 2: Add the `build-bot` job

Append a new job after the existing `build` job. Model it on that job — same `permissions`, same login step guarded by `if: github.event_name != 'pull_request'`, same buildx setup — but with:

```yaml
      - name: Derive tags and labels
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}-bot
          tags: |
            type=ref,event=branch
            type=ref,event=pr
            type=semver,pattern={{version}}
            type=sha,format=long
            type=raw,value=latest,enable={{is_default_branch}}

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          file: Dockerfile.bot
          push: ${{ github.event_name != 'pull_request' }}
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

The `file: Dockerfile.bot` line is the whole point — omit it and you build and publish a second copy of the feather image under the bot's name.

Add `needs: test` so a red suite blocks publication.

### Step 3: Add the fail-fast smoke test

In `build-bot`, after the push step, guarded by `if: github.event_name != 'pull_request'`:

```yaml
      - name: Smoke-test the bot image
        if: github.event_name != 'pull_request'
        run: |
          set -euo pipefail
          IMAGE="$(echo '${{ steps.meta.outputs.tags }}' | head -n1)"
          # The worker must refuse to run unconfigured, naming what is
          # missing. That single assertion also proves the base image,
          # the pip install and the script all work.
          OUT="$(docker run --rm "$IMAGE" 2>&1 || true)"
          echo "$OUT"
          echo "$OUT" | grep -q "TELEGRAM_BOT_TOKEN" \
            || { echo "FAIL: did not name the missing variables"; exit 1; }
          if docker run --rm "$IMAGE" >/dev/null 2>&1; then
            echo "FAIL: bot started with no configuration"; exit 1
          fi
          echo "ok: refuses to run unconfigured, names the missing variables"
```

**Verify locally** before pushing:
```
docker build -q -f Dockerfile.bot -t feather-bot-017 .
docker run --rm feather-bot-017 ; echo "exit=$?"
```
→ non-zero exit, and the output names the missing `TELEGRAM_*` variables. Then `docker rmi -f feather-bot-017`.

### Step 4: Validate the workflow

```
python3 -c "import yaml; d=yaml.safe_load(open('.github/workflows/docker-publish.yml')); print(sorted(d['jobs']))"
```
→ `['build', 'build-bot', 'test']`

You **cannot** run GitHub Actions locally. Do not install `act`. The local Docker checks in Steps 1–3 are the verification.

## Done criteria

ALL must hold:

- [ ] `grep -c "file: Dockerfile.bot" .github/workflows/docker-publish.yml` returns `1`
- [ ] `grep -c "IMAGE_NAME }}-bot" .github/workflows/docker-publish.yml` returns `1`
- [ ] The workflow parses and defines exactly three jobs: `build`, `build-bot`, `test`
- [ ] The existing `build` job is **byte-identical** — `git diff` shows no change within it
- [ ] Locally: `docker build -f Dockerfile.bot` succeeds, and running the image with no env exits non-zero while naming the missing variables
- [ ] Locally: both `requests` pins print the same version
- [ ] `git diff --name-only` shows only `.github/workflows/docker-publish.yml`
- [ ] `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 78, unchanged
- [ ] `plans/README.md` status row updated

## STOP conditions

- The bot image **starts successfully** with no configuration. That is a security regression — the allowlist check lives behind the same config validation — not a CI problem. Report; do not adjust the test to accept it.
- You find yourself converting the existing `build` job into a matrix. It is green and verified; add a job instead.
- You find yourself editing `compose.yml` to pull images. Out of scope — that changes the deployment model and needs registry auth on the host.
- The two `requests` pins already disagree. Report the two versions rather than editing either file; reconciling them is a dependency decision, not a CI one.

## Maintenance notes

- **The deployment still builds from source.** `compose.yml` uses `build:` for both services, so `ghcr.io/d7eeem/feather` is published and unused, and `docker compose pull` does nothing for it. Switching to `image:` would make deploys faster and byte-identical to what CI tested, but the packages are **private**, so the TrueNAS host would need `docker login ghcr.io` with a PAT that has `read:packages`. That is a worthwhile follow-up and a deliberate operational change — plan it separately rather than sliding it in here.
- **Two images now share one tag scheme.** `feather` and `feather-bot` get the same `latest` / `main` / `sha-…` tags from the same commit, which is what you want: a matched pair is always identifiable by SHA.
- **The pin-drift check is a stopgap.** The real fix is for `Dockerfile.bot` to not restate a dependency that `requirements.txt` already owns — e.g. by installing from a small `requirements-bot.txt`. Worth doing if the worker ever needs a second dependency; overkill for one line today.
- **The bot image is still not smoke-tested for actual behaviour** — only that it refuses to run unconfigured. Testing the polling loop needs a live token and a Telegram round-trip, which does not belong in CI.
