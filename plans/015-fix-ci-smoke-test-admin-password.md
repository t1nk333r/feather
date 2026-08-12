# Plan 015: Fix the CI smoke test broken by Plan 010

> **Executor instructions**: Follow this plan step by step. If anything in
> the "STOP conditions" section occurs, stop and report — do not improvise.
> When done, update the status row in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> git rev-parse --short HEAD          # plan written against 6bf3aa4
> grep -n "ADMIN_PASSWORD" .github/workflows/docker-publish.yml   # expect NO output
> ```

## Status

- **Priority**: P1 — `main` is red. Every push since `da7ee42` has failed.
- **Effort**: XS (three lines)
- **Risk**: LOW
- **Depends on**: nothing
- **Category**: bug (CI)
- **Planned at**: 2026-08-12, `6bf3aa4`

## Why this matters

Plan 010 made the app **refuse to boot** without `ADMIN_PASSWORD`:

```python
if not ADMIN_PASSWORD:
    raise RuntimeError(
        "ADMIN_PASSWORD is not set. Refusing to start with unauthenticated "
        "admin routes. Set it in .env (compose.yml loads it via env_file)."
    )
```

That is correct and deliberate. But `.github/workflows/docker-publish.yml` starts the built image with only `-e DATA_DIR=/tmp/feather-data`, so the container raises at import and exits. `curl` then fails to connect (**exit code 7**), and `set -euo pipefail` fails the step.

Confirmed from the failing run: `Tests (Python 3.11)` and `Tests (Python 3.14)` both **pass** — the pytest fixtures set their own password. Only `Build and push` → `Smoke-test the built image` fails. So this is purely a workflow gap, not an application bug.

Three consecutive runs are red (`da7ee42`, `62c8642`, `6bf3aa4`), and the image is still being published before the smoke test runs — so a broken image could ship without the smoke test ever gating it.

## Current state

`.github/workflows/docker-publish.yml`, the smoke-test step:

```yaml
      - name: Smoke-test the built image
        if: github.event_name != 'pull_request'
        run: |
          set -euo pipefail
          IMAGE="$(echo '${{ steps.meta.outputs.tags }}' | head -n1)"
          docker run -d --rm --name feather-smoke -p 5000:5000 \
            -e DATA_DIR=/tmp/feather-data "$IMAGE"
```

`grep -n "ADMIN_PASSWORD" .github/workflows/docker-publish.yml` returns nothing.

## Scope

**In scope**: `.github/workflows/docker-publish.yml` — the smoke-test step only.

**Out of scope — do NOT touch**:
- `app.py`. The refuse-to-boot behaviour is correct; **do not weaken it** to make CI pass. That inverts the fix.
- The `test` job, the build/push steps, the tags, the matrix, the permissions.
- `requirements.txt`, `compose.yml`, `Dockerfile`, `tests/`, `.env.example`.
- Adding a repository secret. The value here is a throwaway for a container that is destroyed seconds later; a real secret would be worse, not better, because it would put a live credential into CI for no benefit.

## Steps

### Step 1: Pass a throwaway password to the smoke-test container

Add `-e ADMIN_PASSWORD=...` to the `docker run` line. Use an obviously-fake value — `ci-smoke-test-not-a-real-password` — so nobody mistakes it for a credential:

```yaml
          docker run -d --rm --name feather-smoke -p 5000:5000 \
            -e DATA_DIR=/tmp/feather-data \
            -e ADMIN_PASSWORD=ci-smoke-test-not-a-real-password "$IMAGE"
```

**Verify**: `grep -c "ADMIN_PASSWORD=ci-smoke-test-not-a-real-password" .github/workflows/docker-publish.yml` → `1`

### Step 2: Assert the refuse-to-boot behaviour, so CI covers what broke it

The gap existed because nothing in CI exercised the new requirement. Add a check *before* the existing container start, asserting the image **fails** without the variable:

```yaml
          # Plan 010: the app must refuse to boot without ADMIN_PASSWORD.
          # Assert that, so a regression that silently re-allows an
          # unauthenticated boot fails the build.
          if docker run --rm -e DATA_DIR=/tmp/feather-data "$IMAGE" python -c "import app" 2>/dev/null; then
            echo "FAIL: image booted without ADMIN_PASSWORD"; exit 1
          fi
          echo "ok: refuses to boot without ADMIN_PASSWORD"
```

Place it immediately after the `IMAGE=` line and before `docker run -d`.

**Verify**: the block is present and the step still parses — see Step 3.

### Step 3: Verify the workflow is valid YAML

```
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/docker-publish.yml')); print('YAML ok')"
```
→ `YAML ok`

You **cannot** run the workflow itself from the worktree. Do not try to install `act` or simulate GitHub Actions. Reproduce the underlying behaviour locally instead:

```
docker build -q -t feather-015 .
# must FAIL (this is the bug's root cause):
docker run --rm -e DATA_DIR=/tmp/fd feather-015 python -c "import app" ; echo "exit=$?"
# must SUCCEED:
docker run --rm -e DATA_DIR=/tmp/fd -e ADMIN_PASSWORD=ci-smoke-test-not-a-real-password feather-015 python -c "import app" ; echo "exit=$?"
```
→ first is non-zero and prints the `RuntimeError`; second is `exit=0`.

Then the full smoke sequence, which is what CI does:
```
CID=$(docker run -d --rm -p 7093:5000 -e DATA_DIR=/tmp/fd \
      -e ADMIN_PASSWORD=ci-smoke-test-not-a-real-password feather-015)
sleep 8
curl -fsS -o /dev/null -w 'source.json %{http_code}\n' http://localhost:7093/source.json
curl -fsS -o /dev/null -w 'qr          %{http_code}\n' http://localhost:7093/qr
curl -fsS -o /dev/null -w 'index       %{http_code}\n' http://localhost:7093/
docker stop $CID; docker rmi -f feather-015
```
→ all three `200`.

## Done criteria

ALL must hold:

- [x] `grep -c "ADMIN_PASSWORD" .github/workflows/docker-publish.yml` returns **`4`** — the `-e` flag, the comment, and both `echo` lines contain the word. An earlier version of this criterion said `2`; that was an arithmetic slip, caught by the executor. The substantive check is `grep -c "ADMIN_PASSWORD=ci-smoke-test-not-a-real-password"` → `1`.
- [ ] `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/docker-publish.yml'))"` exits 0
- [ ] Locally: the image fails to import `app` without the variable, succeeds with it
- [ ] Locally: the three smoke URLs all return `200`
- [ ] `git diff --name-only` shows **only** `.github/workflows/docker-publish.yml`
- [ ] `git diff app.py` is empty — the refuse-to-boot behaviour is untouched
- [ ] `plans/README.md` status row updated

## STOP conditions

- You are tempted to relax or remove the `raise` in `app.py` to make CI green. That is backwards — the application behaviour is correct and CI is what is wrong.
- The image still fails to start with `ADMIN_PASSWORD` set. Something other than this is broken; capture `docker logs` and report.
- You find yourself adding a GitHub repository secret, or editing any job other than the smoke-test step.

## Maintenance notes

- **This class of break will recur.** Any future plan that adds a required environment variable makes the CI smoke test fail the same way, because the workflow starts the container with an explicit minimal env. When adding a required variable, grep `.github/workflows/` in the same change. Plan 011's `STORAGE_BACKEND` avoided it only because it defaults to `local`.
- The Step 2 assertion is the cheap guard: it means the workflow now *tests* the requirement rather than merely tripping over it.
- **The image is pushed before the smoke test runs**, so a broken image can reach `ghcr.io` tagged `latest` even when the smoke test then fails. Reordering push-after-smoke is a real improvement and a separate change — worth doing, but not here.
