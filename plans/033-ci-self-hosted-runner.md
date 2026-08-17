# Plan 033: Run the CI build/publish workflow on a self-hosted runner

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 88e6a11..HEAD -- .github/workflows/docker-publish.yml
> grep -n "runs-on:" .github/workflows/docker-publish.yml
> ```
> At planning time the diff is empty and `grep` returns exactly three lines,
> all `runs-on: ubuntu-latest` (at the `test`, `build`, and `build-bot` jobs).
> If the file has drifted (different job structure or `runs-on` values), compare
> against "Current state" before proceeding; on a mismatch, STOP.

## Status

- **Priority**: P2
- **Effort**: XS
- **Risk**: LOW–MED (a wrong `runs-on` label leaves jobs queued forever; mitigated by using the universal `self-hosted` label)
- **Depends on**: none
- **Category**: dx / tooling
- **Planned at**: commit `88e6a11`, 2026-08-17

## Why this matters

The operator deployed a self-hosted GitHub Actions runner and wants CI to build
and publish the images on it instead of on GitHub-hosted `ubuntu-latest`. The
workflow `.github/workflows/docker-publish.yml` pins all three jobs to
`ubuntu-latest`; this plan moves them to the self-hosted runner. Publishing to
`ghcr.io` is unaffected — `secrets.GITHUB_TOKEN` is provided to self-hosted
runners too, and the existing `docker/login-action` + `build-push-action` steps
work unchanged as long as Docker and buildx are available on the runner host.

The label `self-hosted` is automatically applied by GitHub to **every**
self-hosted runner, regardless of any custom labels the operator assigned, so
`runs-on: self-hosted` matches the runner without needing to know its specific
label set. That is why this plan uses the bare `self-hosted` label rather than
guessing at `[self-hosted, linux, x64]`, which would leave jobs stuck "waiting
for a runner" if the runner lacks one of those labels.

## Current state

`.github/workflows/docker-publish.yml` has three jobs, each pinned to
GitHub-hosted runners. The three `runs-on` lines (at planning time):

```yaml
# job: test  (name: "Tests (Python ${{ matrix.python-version }})")
    runs-on: ubuntu-latest

# job: build  (name: "Build and push")
    runs-on: ubuntu-latest

# job: build-bot  (name: "Build and push (bot)")
    runs-on: ubuntu-latest
```

`grep -n "runs-on:" .github/workflows/docker-publish.yml` returns exactly three
lines, all identical. The `build` and `build-bot` jobs both `needs: test`, so
`test` must run on a working runner or the whole pipeline stalls — which is why
all three move together.

### Repo conventions to follow

- YAML indentation in this file is 2 spaces; `runs-on:` sits at 6 spaces of
  indentation (inside `jobs.<name>:`). Preserve exact indentation.
- Conventional-commit messages (see `git log`), e.g. `ci(...)`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Count self-hosted | `grep -c "runs-on: self-hosted" .github/workflows/docker-publish.yml` | `3` |
| No ubuntu-latest left | `grep -c "runs-on: ubuntu-latest" .github/workflows/docker-publish.yml` | `0` |
| YAML parses | `python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/docker-publish.yml')); print('YAML OK')"` | `YAML OK` (see note) |
| Diff hygiene | `git diff --check` | exit 0 |

> YAML note: if `python3 -c "import yaml"` fails with `ModuleNotFoundError`
> (PyYAML is not a project dependency), SKIP that check and rely on the two
> `grep` counts plus a manual read of the diff. Do NOT `pip install` PyYAML to
> satisfy it — that is out of scope. Report that the YAML-parse check was
> skipped for lack of the module.

## Scope

**In scope** (the only file you may modify):
- `.github/workflows/docker-publish.yml` — the three `runs-on:` lines only

**Out of scope** (do NOT touch):
- Every other line of the workflow — job names, `needs:`, `permissions:`,
  steps, `if:` guards, the smoke tests, tags/labels. Change **only** the
  `runs-on` values.
- Any other file. This is a one-file change.
- The `pull_request` triggers and the publish `if: github.event_name != 'pull_request'`
  guards — leave them exactly as they are (see Maintenance notes for the
  security implication, which is the operator's call, not this plan's).

## Git workflow

- Branch: `advisor/033-ci-self-hosted-runner`
- One commit. Example message: `ci: run build/publish jobs on the self-hosted runner`
- Do NOT push or open a PR.

## Steps

### Step 1: Confirm the current state

```bash
grep -n "runs-on:" .github/workflows/docker-publish.yml
```

**Verify**: exactly three lines, all `runs-on: ubuntu-latest`. If not, STOP.

### Step 2: Change all three `runs-on` values to `self-hosted`

Replace each `runs-on: ubuntu-latest` with `runs-on: self-hosted`, preserving
the exact 6-space indentation. There are exactly three occurrences (the `test`,
`build`, and `build-bot` jobs). Change nothing else.

**Verify**:

```bash
grep -c "runs-on: self-hosted" .github/workflows/docker-publish.yml   # 3
grep -c "runs-on: ubuntu-latest" .github/workflows/docker-publish.yml # 0
git diff --check                                                      # exit 0
```

### Step 3: Confirm the diff is exactly three lines

```bash
git diff 88e6a11..HEAD -- .github/workflows/docker-publish.yml
```

**Verify**: the diff shows exactly three changed lines, each
`-    runs-on: ubuntu-latest` / `+    runs-on: self-hosted`, and nothing else.
If any other line changed, STOP and revert the stray edit.

## Test plan

- No unit tests — this is CI infrastructure. The change is verified by the two
  `grep` counts and a read of the diff. The true end-to-end verification is the
  next push to `main`: the operator confirms the jobs pick up on the
  self-hosted runner (not stuck "waiting for a runner") and that `build` /
  `build-bot` still publish to `ghcr.io`.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `grep -c "runs-on: self-hosted" .github/workflows/docker-publish.yml` — `3`
- [ ] `grep -c "runs-on: ubuntu-latest" .github/workflows/docker-publish.yml` — `0`
- [ ] `git diff 88e6a11..HEAD -- .github/workflows/docker-publish.yml` shows only
      three `runs-on` line changes, nothing else
- [ ] `git diff --check` — exit 0
- [ ] `git status --short` shows only `.github/workflows/docker-publish.yml`
      modified
- [ ] `plans/README.md` status row for Plan 033 updated

## STOP conditions

Stop and report back (do not improvise) if:

- The drift check shows the workflow no longer has exactly three
  `runs-on: ubuntu-latest` lines (structure changed since planning).
- Making the change appears to require editing anything other than the three
  `runs-on` values.

## Maintenance notes

- **Runner prerequisites (operator, not the executor)**: the self-hosted runner
  host must have Docker + buildx installed and the runner service able to reach
  `ghcr.io`. `docker/setup-buildx-action` and `docker/build-push-action` assume
  a working Docker daemon on the runner.
- **`gha` build cache**: the workflow uses `cache-from/to: type=gha`. That
  cache backend works on self-hosted runners but is scoped per-repo in GitHub's
  cache service; nothing to change, but the first self-hosted run repopulates
  the cache from cold.
- **Security implication (operator decision, deliberately out of scope here)**:
  the workflow also triggers on `pull_request`. After this change, a pull
  request's `Dockerfile` build runs on the self-hosted runner. For a private
  repo with no untrusted contributors this is fine. If the repo ever accepts
  fork PRs, running their build on your own hardware is a real risk — at that
  point, gate the self-hosted jobs to `push`/`workflow_dispatch` only, or keep
  PR builds on `ubuntu-latest`. Not changed here because it is a policy call.
- If the operator would rather keep the light `test` job on GitHub-hosted
  runners (faster cold start, no need for Python 3.11/3.14 on the box) and only
  self-host the two image builds, that is a one-line revert of the `test` job's
  `runs-on` back to `ubuntu-latest`.
- A reviewer should confirm the diff touched only `runs-on` values — no step,
  guard, or trigger was altered in passing.
