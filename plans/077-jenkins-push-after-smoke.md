# Plan 077: Push images only after they pass smoke, and make the boot-refusal assertion specific

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> ```
> cd <repo root>
> git rev-parse --short HEAD                        # plan written at 5e8f447
> git diff --stat 5e8f447..HEAD -- Jenkinsfile plans/HANDOFF.md
> ```
> On any change, compare the excerpts below against the live file; mismatch → STOP.

## Status

- **Priority**: P1 — the deployment pulls `:latest`; a broken build reaches it before the pipeline goes red
- **Effort**: S
- **Risk**: LOW — reorders stages and tightens one grep; no application code
- **Depends on**: none. Land before any dependency bump (audit finding #5, 2026-08-28 — not yet planned) so a broken dependency cannot reach `:latest`.
- **Category**: dx
- **Planned at**: commit `5e8f447`, 2026-08-28

## Why this matters

`plans/HANDOFF.md` has recorded since 2026-08-12: "CI pushes the image *before* the smoke test runs. A broken `latest` can reach ghcr even when the run ends red. Reordering push-after-smoke is an unwritten follow-up and matters more now the deployment actually pulls `latest`." It is still true, and now applies to three images. Separately, the main image's "refuses to boot without `ADMIN_PASSWORD`" check treats *any* non-zero exit of `python -c "import app"` as a pass and discards stderr — a missing dependency (two were added two days ago for plan 073) makes that stage green, and the failure surfaces only in the later container-start loop, after `:latest` was already pushed.

## Current state

`Jenkinsfile` stage order (`command grep -n "stage('" Jenkinsfile`):
```
21  Tests (Python 3.11)
32  Tests (Python 3.14)
43  requests pin drift check
55  Build & push image            ← pushes :main :latest :sha-<short>
73  Smoke-test image              ← runs against $IMAGE:main *after* the push
110 Build & push bot image
124 Smoke-test bot image
142 Build & push fdroid image
156 Smoke-test fdroid image
```

`Jenkinsfile:55-71`:
```groovy
    stage('Build & push image') {
      when { branch 'main' }
      steps {
        withCredentials([usernamePassword(credentialsId: 'ghcr-pat',
            usernameVariable: 'REG_USER', passwordVariable: 'REG_TOKEN')]) {
          sh 'echo "$REG_TOKEN" | docker login "$REGISTRY" -u "$REG_USER" --password-stdin'
        }
        sh '''
          set -eu
          SHORT=$(git rev-parse --short HEAD)
          docker build -t "$IMAGE:main" -t "$IMAGE:latest" -t "$IMAGE:sha-$SHORT" .
          docker push "$IMAGE:main"
          docker push "$IMAGE:latest"
          docker push "$IMAGE:sha-$SHORT"
        '''
      }
    }
```

`Jenkinsfile:86-89` — the weak assertion:
```groovy
          if docker run --rm -e DATA_DIR=/tmp/feather-data "$IMAGE:main" python -c "import app" 2>/dev/null; then
            echo "FAIL: image booted without ADMIN_PASSWORD"; exit 1
          fi
          echo "ok: refuses to boot without ADMIN_PASSWORD"
```
The bot (`Jenkinsfile:124-139`) and fdroid (`156-168`) smoke stages do it right: capture output, `grep -q` for the specific variable name.

`Jenkinsfile:46` uses `set -euo pipefail` in a `sh` step; Jenkins runs `/bin/sh`, which on Debian agents is `dash` and does not support `pipefail`. The other steps use `set -eu`.

The bot and fdroid images are built *and pushed* in their own stages, then smoked — same defect.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Syntax sanity | `docker run --rm -v "$PWD/Jenkinsfile:/j:ro" alpine sh -c 'grep -c "stage(" /j'` | `9` stages before, `10`–`12` after (see Step 1) |
| Local dry-run of a smoke block | copy the `sh '''…'''` body into a script and run it against a locally built `feather:dev` image | behaves as the stage describes |

There is no Jenkins linter available locally; the Jenkins server validates the file on the next push. Keep Groovy changes minimal and mirror existing stages exactly.

## Scope

**In scope**: `Jenkinsfile`; `plans/HANDOFF.md` (delete the now-fixed "CI pushes the image before the smoke test" trap — one paragraph).

**Out of scope — do NOT touch**: the test stages; the drift-check stage's logic (only its `set` line); `Dockerfile*`; `compose.yml`; the smoke assertions' *content* for bot/fdroid (only their ordering).

## Git workflow

- Branch: `advisor/077-jenkins-push-after-smoke`; commits `ci: build, smoke, then push (plan 077)`, `ci: assert the boot refusal names ADMIN_PASSWORD (plan 077)`, `docs(handoff): drop the push-before-smoke trap (plan 077)`.
- Do not push.

## Steps

### Step 1: Split build/push and move every push after its smoke test

For each of the three images, restructure into: **Build <x> image** (build with all three tags, no push) → **Smoke-test <x> image** (unchanged assertions, running against `:main`) → **Push <x> image** (login once, push the three tags). Keep the `docker login` in the first push stage; it persists for the agent session. Resulting order:

```
Tests 3.11 → Tests 3.14 → requests pin drift check →
Build image → Smoke-test image → Build bot image → Smoke-test bot image →
Build fdroid image → Smoke-test fdroid image → Push images
```
A single final **Push images** stage pushing all nine tags is simplest and means *no* image reaches the registry unless *all three* pass — prefer that unless you have a reason to push per-image. All push/smoke/build stages keep `when { branch 'main' }`.

**Verify**: reading the file top to bottom, no `docker push` appears before every `Smoke-test` stage; `command grep -n "docker push" Jenkinsfile` lines are all greater than the last `Smoke-test` stage's line.

### Step 2: Make the boot-refusal assertion specific

Replace `Jenkinsfile:86-89` with the pattern the bot stage uses:

```sh
          OUT="$(docker run --rm -e DATA_DIR=/tmp/feather-data "$IMAGE:main" python -c "import app" 2>&1 || true)"
          echo "$OUT" | grep -q "ADMIN_PASSWORD" \
            || { echo "FAIL: import without ADMIN_PASSWORD did not name ADMIN_PASSWORD (got: $OUT)"; exit 1; }
          if docker run --rm -e DATA_DIR=/tmp/feather-data "$IMAGE:main" python -c "import app" >/dev/null 2>&1; then
            echo "FAIL: image booted without ADMIN_PASSWORD"; exit 1
          fi
          echo "ok: refuses to boot without ADMIN_PASSWORD, and names it"
```
Add one more line to the same stage that proves the image's dependencies import: `docker run --rm -e ADMIN_PASSWORD=ci-smoke-test-not-a-real-password -e DATA_DIR=/tmp/feather-data "$IMAGE:main" python -c "import app, pyaxmlparser, yaml, boto3, waitress; print('imports ok')" | grep -q "imports ok"`.

**Verify**: run the block locally against a `feather:dev` build → prints both `ok` lines; then against a deliberately broken image (e.g. `docker build --build-arg` is not available — instead run the block with `$IMAGE` pointing at `python:3.11-slim`, which has no `app`) → the first check fails with a message quoting the real error, proving the grep discriminates.

### Step 3: `pipefail` under `/bin/sh`

Change `Jenkinsfile:46` from `set -euo pipefail` to `set -eu`. The commands in that step are `grep -oP … | …`-free assignments in `$(...)`, so nothing relied on `pipefail`; if you find a pipeline whose failure would now be masked, add an explicit status check instead.

**Verify**: `command grep -n "pipefail" Jenkinsfile` → none.

### Step 4: HANDOFF

In `plans/HANDOFF.md`, delete the paragraph beginning `**CI pushes the image *before* the smoke test runs.**` (the "Traps" section). Do not edit anything else in that file — plan 078 rewrites it.

**Verify**: `command grep -n "before\* the smoke" plans/HANDOFF.md` → none.

## Test plan

No pytest. Verification is: local execution of the modified smoke block against a good image and a wrong image (Step 2), and the first `main` run on Jenkins after merge — the operator should watch it and confirm the stage order in the Blue Ocean view. Record in NOTES that the Jenkins run itself was not observed by you.

## Done criteria

- [ ] Every `docker push` line number in `Jenkinsfile` is greater than every `Smoke-test` stage's line number
- [ ] The main smoke stage captures output and greps for `ADMIN_PASSWORD`; it also imports `pyaxmlparser, yaml, boto3, waitress` successfully
- [ ] `command grep -n "pipefail" Jenkinsfile` → none
- [ ] The push-before-smoke trap is gone from `plans/HANDOFF.md`
- [ ] The local run of the smoke block passes on a good image and fails with a quoted error on `python:3.11-slim`
- [ ] `git status --short` shows only `Jenkinsfile` and `plans/HANDOFF.md`
- [ ] `plans/README.md` status row updated

## STOP conditions

- The Jenkinsfile's stage structure no longer matches the listing above (someone already reordered it).
- A smoke stage depends on a tag existing in the registry (e.g. pulls `:main` instead of using the locally built tag) — report; the local tag must be used.
- You want to change what a smoke stage asserts for bot/fdroid — out of scope.

## Maintenance notes

- New images must follow the same three-stage shape (build → smoke → push in the final stage). Reviewers: reject a `docker push` in a build stage.
- The final Push stage is all-or-nothing by design; if one image's smoke is flaky, fix the smoke, don't split the push.
- The import check list (`pyaxmlparser, yaml, boto3, waitress`) should grow with `requirements.txt`; a plan that adds a runtime dependency should add it here.
