# Plan 082: Pin the Telegram ingest worker to uid 101 in the repo's `compose.yml`

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 3e0cd8a..HEAD -- compose.yml`
> If `compose.yml` changed since this plan was written, compare the
> "Current state" excerpt against the live file before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: XS
- **Risk**: LOW
- **Depends on**: none (plans 013 and 016 introduced the service; both are DONE)
- **Category**: bug
- **Planned at**: commit `3e0cd8a`, 2026-08-31

## Why this matters

The repo's `compose.yml` and the operator's deployed copy disagree, and the
deployed copy is the correct one. `telegram-bot-api` chowns its work directory
to uid 101; the `ipa-ingest-bot` image runs as a different system uid
(`Dockerfile.bot:14-16` creates its own `ipabot` user). With the wrong uid the
worker cannot traverse into the token directory — and because
`os.path.exists()` returns `False` on permission-denied, the failure surfaces
as the misleading `MountMismatchError: shared volume mounts disagree` rather
than as a permission error. That misdirection has already cost real debugging
time once; it is recorded as a trap in `plans/HANDOFF.md`. The operator fixed
it by hand in the Dockge/Dockhand copy of `compose.yml`, so anybody deploying
the Telegram profile fresh from this repository reproduces the original bug.
Two consecutive reconciles (2026-08-26 and 2026-08-31) recorded the divergence
as still outstanding. One line closes it.

## Current state

- `compose.yml` — the only file that needs to change. The `ipa-ingest-bot`
  service, lines 61–81 at commit `3e0cd8a`, has **no** `user:` key:

  ```yaml
    ipa-ingest-bot:
      profiles: ["telegram"]
      # image + build kept together -- `docker compose pull` fetches the
      # CI-built image (the one that was actually smoke-tested), while
      # `docker compose build` still works locally and tags the result as
      # this name.
      image: ghcr.io/t1nk333r/feather-bot:latest
      build:
        context: .
        dockerfile: Dockerfile.bot
      container_name: ipa-ingest-bot
      depends_on:
        - telegram-bot-api
        - altstore-manager
      env_file:
        - .env
      volumes:
        # Read-only: the worker reads files the API server wrote. It has no
        # business modifying that directory.
        - telegram-bot-api-data:/var/lib/telegram-bot-api:ro
      restart: unless-stopped
  ```

- `Dockerfile.bot:13-16` is why the default uid is wrong:

  ```dockerfile
  # Non-root user, matching the main Dockerfile's convention.
  RUN groupadd -r ipabot && useradd -r -g ipabot ipabot \
  ...
  USER ipabot
  ```

- The peer service it must match, `compose.yml:39-52`, is
  `aiogram/telegram-bot-api:latest`, which chowns
  `/var/lib/telegram-bot-api` to uid 101 at startup.

- **Repo convention for this file**: every non-obvious key carries a comment
  naming the plan that introduced it and the reason. Follow that — see the
  `profiles:` comment at `compose.yml:63-66` and the `volumes:` comment at
  `compose.yml:78-79` for the exact voice (lowercase prose, `--` for dashes,
  explains *why* not *what*).

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Assert the key | `python3 -c "import yaml;print(yaml.safe_load(open('compose.yml'))['services']['ipa-ingest-bot']['user'])"` | prints `101:101` |
| YAML still parses | `python3 -c "import yaml;yaml.safe_load(open('compose.yml'));print('ok')"` | prints `ok` |
| Scope check | `git status --porcelain` | only `compose.yml` (and `plans/`) modified |
| Full test suite | `ADMIN_PASSWORD=x python -m pytest tests/ -q -p no:cacheprovider` | `307 passed, 1 skipped` |

`PyYAML` is already a dependency (`requirements.txt`). If no virtualenv exists,
create one and `pip install -r requirements.txt -r requirements-dev.txt` first.

Note: `docker compose config` is **not** a usable verification here — every
service declares `env_file: - .env`, and `.env` is gitignored and absent from a
fresh clone, so the command fails before it validates anything. Use the YAML
assertions above.

## Scope

**In scope** (the only files you should modify):
- `compose.yml`
- `plans/README.md` (status row only)

**Out of scope** (do NOT touch, even though they look related):
- `Dockerfile.bot` — do not change the image's `USER`. The uid override belongs
  in compose so the image stays a plain non-root image, and changing the
  Dockerfile would force a rebuild and republish of `feather-bot` for a
  deployment-only concern.
- `compose.yml`'s `telegram-bot-api`, `altstore-manager`, `release-import`, or
  `fdroid-index` services — none of them has this problem.
- The `:ro` flag on the shared volume — the worker must stay read-only.
- `plans/HANDOFF.md` — the trap text stays; it explains the history.

## Git workflow

- Branch: `advisor/082-compose-bot-uid`
- One commit. Message style is conventional commits — recent examples from
  `git log --oneline`: `fix(android): encode QR as F-Droid deep link`,
  `feat(bot): ingest Android APKs from Telegram`. Use
  `fix(compose): run ingest worker as uid 101 (plan 082)`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add the `user:` key with its explanatory comment

In `compose.yml`, inside the `ipa-ingest-bot` service, add the key immediately
after `container_name: ipa-ingest-bot` (before `depends_on:`), matching the
file's comment convention:

```yaml
    container_name: ipa-ingest-bot
    # Must match the uid telegram-bot-api chowns its work dir to (101).
    # Dockerfile.bot's own user has a different uid, and it cannot traverse
    # into the token directory -- which surfaces misleadingly as
    # MountMismatchError, because os.path.exists() returns False on
    # permission-denied rather than raising.
    user: "101:101"
    depends_on:
```

Change nothing else in the service.

**Verify**:
`python3 -c "import yaml;print(yaml.safe_load(open('compose.yml'))['services']['ipa-ingest-bot']['user'])"`
→ prints `101:101`

**Verify** the rest of the service is untouched:
`git diff --stat compose.yml` → `1 file changed, 6 insertions(+)` (the key plus
its five comment lines and nothing removed). If any line shows as deleted, you
changed something you should not have — revert and redo the step.

### Step 2: Confirm nothing else regressed

The application does not read `compose.yml`, so no behaviour changes; run the
suite to prove it.

**Verify**: `ADMIN_PASSWORD=x python -m pytest tests/ -q -p no:cacheprovider`
→ `307 passed, 1 skipped`

### Step 3: Update the index

Set the plan 082 row in `plans/README.md` to `**DONE**` with the commit SHA.
Skip this step if a reviewer told you they maintain the index.

## Test plan

No new tests. There is no test harness for `compose.yml` in this repo
(`tests/` covers `app.py` and the scripts only), and adding a compose-parsing
test for a single literal key would be more machinery than the change. The
YAML assertion in step 1 is the verification. The existing suite is run in
step 2 purely as a no-regression gate.

## Done criteria

ALL must hold:

- [ ] `python3 -c "import yaml;print(yaml.safe_load(open('compose.yml'))['services']['ipa-ingest-bot']['user'])"` prints `101:101`
- [ ] `git diff compose.yml` shows insertions only, zero deletions
- [ ] `ADMIN_PASSWORD=x python -m pytest tests/ -q -p no:cacheprovider` → `307 passed, 1 skipped`
- [ ] `git status --porcelain` lists no file outside the in-scope list
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `compose.yml`'s `ipa-ingest-bot` service already has a `user:` key, or its
  content differs from the "Current state" excerpt — somebody fixed this
  already, or the service was restructured.
- The `telegram-bot-api` service no longer uses the `aiogram/telegram-bot-api`
  image, or the shared `telegram-bot-api-data` volume is gone. The uid 101
  value is specific to that image; a different image invalidates this plan.
- The test suite does not report `307 passed, 1 skipped` **before** you make
  any edit. That means the baseline has moved and the expected output in this
  plan is stale; report the actual baseline instead of adjusting the plan.

## Maintenance notes

- The uid `101` is not ours — it is whatever `aiogram/telegram-bot-api` creates
  for its `telegram-bot-api` user. If that image is upgraded or replaced,
  re-derive the uid (`docker run --rm --entrypoint id <image> telegram-bot-api`)
  rather than assuming 101 survived.
- A reviewer should check exactly two things: that the diff is insertions only,
  and that `:ro` on the shared volume is still present.
- Deliberately deferred: the deployment's `compose.yml` is still hand-synced
  from this repo. Wiring Dockhand to deploy from git would prevent this whole
  class of divergence, but that is an operator/infrastructure decision, not a
  code change, and it is out of scope here.
