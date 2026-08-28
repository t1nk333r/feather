# Plan 080: Return stable error messages instead of raw exception text

> Executor: run every verification gate. Based on commit `5e8f447` (2026-08-28); stop on drift.

## Status

- Status: DONE — implemented and verified; full suite `262 passed, 1 skipped`.
- Priority: P1
- Effort: S
- Risk: LOW–MED — clients may currently depend on text, so retain status codes and stable generic messages.
- Depends on: none
- Category: security
- Planned at: commit `5e8f447`, 2026-08-28

## Why this matters

Several handlers log an exception and return `str(e)`. Public IPA/icon/source routes can expose filesystem paths or backend details, while authenticated Android APIs expose storage and parser internals to the admin client unnecessarily. Existing health and storage endpoints already demonstrate fixed-message responses. Keep detailed diagnostics in server logs and return stable, non-sensitive messages to callers.

## Current state

- Public reflection: `app.py:2103-2105`, `2132-2134`, and `2161-2163` return raw exception text.
- Android reflection: `app.py:3805-3807`, `3815-3817`, and `3865-3955` return raw exception text in error JSON.
- Fixed-message examples exist at `app.py:3659-3670`; match that convention and do not change successful response shapes.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Focused tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py tests/test_android.py -q -p no:cacheprovider` | all pass plus new assertions |
| Full tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -p no:cacheprovider` | all pass |

## Scope

In scope: the listed exception responses in `app.py`, plus regression tests in `tests/test_routes.py` and `tests/test_android.py` as needed.

Out of scope: changing logging, diagnostics retention, HTTP status codes, successful payloads, or unrelated internal exception handling.

## Steps

### Step 1: Replace reflected public error payloads

For source, IPA, and icon routes, retain server-side logging but return fixed messages such as `Source unavailable`, `IPA unavailable`, and `Icon unavailable`, with the existing status code appropriate to each route. Do not include exception objects, paths, URLs, headers, or backend details.

Verify: `rg -n 'return jsonify.*str\(e\)' app.py` no longer matches the three public route handlers.

### Step 2: Replace reflected Android error payloads

Apply the same pattern to Android status/list/add/update/delete/config/request handlers. Preserve `ValueError` validation messages where they are deliberate client-facing contract errors; sanitize only broad unexpected-exception branches.

Verify: `rg -n 'return jsonify.*str\(e\)' app.py` has no unexpected-exception matches in the Android route block.

### Step 3: Add discrimination tests

Induce sentinel exceptions containing a fake filesystem path and assert the response omits that sentinel while retaining the expected status and generic error key. Test at least one public route and one Android route; follow the existing fixtures and monkeypatch style.

Verify: focused and full pytest commands pass. The tests must fail if one selected handler is temporarily reverted.

## Test plan

Cover public source/IPA/icon failures, Android status or list failure, and validation errors remaining readable. Do not assert exact logging output beyond existing tests.

## Done criteria

- [ ] No broad unexpected-error response in the scoped handlers contains `str(e)`.
- [ ] Validation errors and status codes remain compatible.
- [ ] Focused and full pytest commands pass.
- [ ] Only scoped source/test files are modified.
- [ ] `plans/README.md` status row is updated.

## STOP conditions

- A client contract or test requires raw exception text; stop and report the exact endpoint.
- The endpoint is no longer covered by the cited handler or has moved to a different error boundary.

## Maintenance notes

New routes should use a fixed public message by default and put diagnostics in the authenticated diagnostics log. Avoid returning exception text from storage, network, or parser boundaries.
