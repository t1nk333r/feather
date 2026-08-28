# Plan 079: Strip GitLab credentials on every cross-host download redirect

> Executor: run each gate before the next step. This plan is based on commit `5e8f447` (2026-08-28). If the cited code differs, stop and report.

## Status

- Status: REJECTED — already fixed in the live code at `scripts/release_source_ingest.py:594-599`.
- Priority: P1
- Effort: S
- Risk: MED — changes authenticated release downloads; preserve same-host auth and the existing host allowlist.
- Depends on: none
- Category: security
- Planned at: commit `5e8f447`, 2026-08-28

## Why this matters

The release importer sends provider credentials while following asset redirects. `requests` removes `Authorization` on a cross-host redirect, but it does not provide the same guarantee for the GitLab `PRIVATE-TOKEN` header. A malicious or compromised redirect target could therefore receive a GitLab credential. The fix must preserve authentication for approved same-host redirects and remove all provider credentials before any cross-host hop.

## Current state

- `scripts/release_source_ingest.py:434-443` follows redirects manually and copies the prior headers; the GitLab branch sets `PRIVATE-TOKEN`.
- `scripts/release_source_ingest.py:546-561` builds provider headers, and `tests/test_release_source_ingest.py:633-667` already proves cross-host `Authorization`/`PRIVATE-TOKEN` behavior for the current contract; extend that contract rather than changing provider APIs.
- Download hosts are constrained by each job’s `allowed_download_hosts`; do not weaken that allowlist.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Focused tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_release_source_ingest.py -q -p no:cacheprovider` | all pass, including the new redirect test |
| Full tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | all pass; no regressions |

## Scope

In scope: `scripts/release_source_ingest.py`, `tests/test_release_source_ingest.py`.

Out of scope: provider API selection, URL allowlist policy, Telegram ingest, application routes, and dependency upgrades.

## Steps

### Step 1: Define the redirect credential rule

Inspect the manual redirect loop. Make the header transition explicit: retain provider authentication only when the next URL’s hostname is the authenticated provider host; remove both `Authorization` and `PRIVATE-TOKEN` for every cross-host request. Keep HTTPS and the existing allowlist checks unchanged.

Verify: `rg -n "PRIVATE-TOKEN|Authorization|MAX_REDIRECTS" scripts/release_source_ingest.py` shows both headers handled in the redirect logic.

### Step 2: Add regression coverage

Add a test using the existing fake-session/response pattern that starts with a GitLab request carrying `PRIVATE-TOKEN`, redirects to an allowed asset host, and asserts the second request has neither provider credential. Add the same-host case if the current tests do not already assert that `PRIVATE-TOKEN` is retained.

Verify: focused pytest passes; temporarily removing the cross-host `PRIVATE-TOKEN` removal must make the new test fail, then restore the implementation.

## Test plan

Cover cross-host stripping, same-host retention, allowlist rejection, and the existing GitHub behavior. No real network calls.

## Done criteria

- [ ] Cross-host requests never carry `Authorization` or `PRIVATE-TOKEN`.
- [ ] Same-host provider redirects retain the expected credential.
- [ ] Focused and full pytest commands pass.
- [ ] Only the two in-scope files are modified.
- [ ] `plans/README.md` status row is updated.

## STOP conditions

- The redirect implementation is no longer at the cited location or uses a different HTTP client.
- The change would require allowing a host not already accepted by the job configuration.
- Any test requires real provider credentials or network access.

## Maintenance notes

If another provider-specific credential is added, include it in the cross-host stripping rule and its regression test. Review redirects whenever the importer’s HTTP client changes.
