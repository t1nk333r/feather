# Plan 081: Make manual app creation idempotent for an existing version

> Executor: run every gate. Based on commit `5e8f447` (2026-08-28); stop on drift.

## Status

- Status: DONE — implemented and verified; full suite `262 passed, 1 skipped`.
- Priority: P2
- Effort: S
- Risk: MED — the existing manual route may already be used for updates; preserve new-version behavior and avoid deleting the previous binary.
- Depends on: none
- Category: correctness
- Planned at: commit `5e8f447`, 2026-08-28

## Why this matters

`SourceManager.add_version` already returns success without mutation when a version exists. `add_app_manual` does not use that guard: it writes the incoming IPA to the version path and inserts another catalog version. Repeated form submissions or retries can create duplicate versions and overwrite a hosted binary before catalog persistence succeeds. Align manual add with the established idempotent version path.

## Current state

- `app.py:1266-1355` implements `add_app_manual`; it saves/uploads the IPA before checking whether the app exists and inserts at `1335-1345` without a version duplicate check.
- `app.py:1512-1544` checks for an existing version before any file operation and returns `Version <version> already exists; nothing to add`.
- Existing route tests cover `add_version` idempotency around `tests/test_routes.py:437-460` and `:683`; use that style for manual add.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Focused tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -p no:cacheprovider` | all pass plus manual-add regressions |
| Full tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | all pass |

## Scope

In scope: `app.py` `SourceManager.add_app_manual`, its route-level behavior if required, and `tests/test_routes.py`.

Out of scope: changing `add_version`, storage abstractions, update-version semantics, catalog migration, or deleting pre-existing duplicate data.

## Steps

### Step 1: Guard duplicate versions before file mutation

Within the existing-app branch, inspect the current versions before saving an IPA or replacing a path. If the requested version exists, return the same successful no-op contract used by `add_version`, and do not alter the catalog, IPA, or icon. Preserve the path that creates a genuinely new version and the path that creates a new app.

Verify: focused tests pass; a test that pre-seeds the same version and submits a different IPA confirms bytes, catalog count, and icon remain unchanged.

### Step 2: Protect the old binary during new-version failure

Confirm the new-version path does not remove or overwrite an existing hosted binary before the new file is fully staged and catalog save succeeds. Reuse existing storage behavior rather than introducing a second abstraction. Add a failure test following the existing failed-download/save tests.

Verify: the failure test leaves the prior file and catalog unchanged; full pytest passes.

## Test plan

Cover existing app + existing version no-op, existing app + new version, new app creation, failed upload/download preserving old state, and notification behavior if the route currently notifies.

## Done criteria

- [ ] Repeating manual add for the same app/version performs no file or catalog mutation.
- [ ] New versions still publish exactly once.
- [ ] A failed replacement cannot destroy the old hosted binary.
- [ ] Focused and full pytest commands pass.
- [ ] Only the scoped files are modified and the index row is updated.

## STOP conditions

- The route’s response contract differs from the cited implementation and changing it would affect a documented client.
- Correctness requires changing shared storage code outside scope; stop and report instead.

## Maintenance notes

Keep one idempotency rule for all version-ingest surfaces. If manual add is later removed in favor of a dedicated update route, retire this guard only with a migration and route test review.
