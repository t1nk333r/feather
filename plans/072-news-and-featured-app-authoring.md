# Plan 072: Add guarded news and featured-app authoring to Source Information

> **Executor instructions**: Read this plan fully before editing. Re-check the
> official AltStore source schema linked below because it is an external
> contract, then reconcile any repository drift. Implement the smallest complete
> vertical slice described here; do not turn this into a general JSON editor.
> Update the index status when complete unless review owns it.
>
> **Drift check (run first)**:
> `git diff --stat f8a8827..HEAD -- app.py templates/index.html tests/test_routes.py README.md`
> Stop if `featuredApps` or `news` has gained a different authoritative editor,
> or if the official schema has materially changed. Otherwise adapt line numbers
> and reuse the current catalog mutation/validation conventions.

## Status

- **Priority**: P3
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: direction
- **Planned at**: commit `f8a8827`, 2026-08-22

## Why this matters

Fresh catalogs already contain `featuredApps` and `news`, but operators can edit
neither without opening `source.json` by hand. That makes two standard AltStore
presentation features effectively dormant and invites malformed JSON or broken
references in the catalog's most sensitive file. Add a narrow authenticated
editor that validates every cross-reference and writes through `SourceManager`.

The authoritative schema is AltStore's official
[Make a Source](https://faq.altstore.io/developers/make-a-source) documentation.
At planning time it defines `featuredApps` as an ordered list of bundle
identifiers (only the first five are displayed), and each news item with required
`title`, unique `identifier`, `caption`, and ISO-8601 `date`; optional keys are
`tintColor`, `imageURL`, `notify`, `url`, and `appID`. Re-check that contract at
execution time rather than treating this plan's snapshot as permanent.

## Current state

`SourceManager.initialize_source()` creates both arrays
(`app.py:1119-1133`). `update_source_info()` only accepts six scalar source
fields (`app.py:1647-1659`), and its authenticated route simply forwards the
request (`app.py:2956-2970`). The Source Information form and loader likewise
handle only those scalar fields (`templates/index.html:751-779,2216-2253`).

The route suite already proves source updates, auth, atomic failure behavior,
and pre-change backups (`tests/test_routes.py:820-828,1190-1244,1480-1509`).
Extend those guarantees instead of creating a second persistence mechanism.

Applicable constraints:

- `source.json` remains valid AltStore JSON and the sole public catalog.
- Every mutation is authenticated and runs inside `SourceManager`'s lock.
- Use `save_source()` so backup, atomic replacement, and failure semantics stay
  identical to existing catalog mutations.
- Never accept arbitrary catalog keys or raw JSON from the browser.
- `featuredApps` and news `appID` values must reference existing app bundle IDs
  exactly; never silently repair casing or retain dangling references.
- The news `notify` flag is catalog metadata for clients. It must not trigger
  Telegram or another server-side notification.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Focused backend tests | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'featured or editorial or news'` | all selected pass |
| Full suite | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | all pass |
| Diff hygiene | `git diff --check` | no output |

## Scope

**In scope**:

- `app.py`
- `templates/index.html`
- `tests/test_routes.py`
- `README.md` for the authenticated route table/operator description

**Out of scope**:

- A generic `source.json` editor or arbitrary JSON patch endpoint.
- Automatic news publication after imports or releases.
- Server-side delivery of push, Telegram, email, or webhook notifications.
- Fetching/proxying/validating remote image or article contents.
- Scheduling, drafts, localization, rich text, analytics, or per-user targeting.
- Reordering apps in the main `apps` array.
- More than five featured apps; clients display only the first five.

## Git workflow

- Branch: `advisor/072-news-featured-authoring`
- Conventional commit example: `feat(source): add editorial metadata editor`.
- Commit backend/validation before UI if practical.
- Do not push/open a PR unless instructed.

## Target API contract

Add four authenticated endpoints:

| Method and path | Request | Success |
|---|---|---|
| `GET /api/editorial` | none | `{success, apps, featuredApps, news}` |
| `POST /api/featured-apps` | `{bundleIdentifiers: [...]}` | saved ordered list |
| `POST /api/news` | one normalized news object | created/replaced item |
| `POST /api/news/delete` | `{identifier: "..."}` | matching item removed |

`apps` is a minimal list of `{bundleIdentifier, name}` for authenticated editor
controls, not a copy of every version. Return news newest-first by parsed date.
`POST /api/news` is an upsert keyed by `identifier`: the identifier is immutable
while editing a selected item; a new identifier creates a new item. Return 400
for validation failures and 404 when deletion targets no item.

## Steps

### Step 1: Add explicit editorial validators

In `app.py`, add small pure helpers for featured-app and news normalization.
They must return normalized values or a user-safe validation error; do not
mutate their inputs.

Featured-app rules:

- payload must be a JSON object containing an array `bundleIdentifiers`;
- allow 0–5 non-empty strings, preserve order, and reject duplicates;
- require an exact match in the current catalog app set;
- reject unknown keys only if the route's existing API convention does so;
  always ignore no malformed array entries silently.

News rules:

- accept only the schema keys named in “Why this matters”;
- require non-empty string `title`, `identifier`, and `caption`, with documented
  bounds (suggested: 200, 128, and 1,000 characters respectively);
- constrain `identifier` to ASCII letters, digits, `.`, `_`, and `-`, and reject
  duplicate identifiers except the single record being replaced;
- require a timezone-aware ISO-8601 `date`; accept a terminal `Z`, normalize to
  UTC, and serialize one canonical form;
- if present, require `tintColor` as six-digit hex and canonicalize it with `#`;
- if present, require `imageURL` and `url` to be absolute `http` or `https` URLs
  with a host and no embedded credentials. Do not fetch either URL;
- if present, require `notify` to be a JSON boolean, not truthy strings/numbers;
- if present, require `appID` to exactly match an existing bundle identifier;
- trim allowed text fields, reject control characters, and never echo exception
  details or catalog contents in errors.

Add direct tests for canonicalization and each rejection class. If the current
official documentation requires a different field or format, follow it and
record the difference in the commit message and README.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'editorial_validation or news_validation'
```

Expected: required fields, date/timezone, color, URL, boolean, length, identifier,
duplicate, and app-reference boundaries are proven.

### Step 2: Add locked SourceManager mutations and authenticated routes

Add `SourceManager.update_featured_apps()`, `upsert_news_item()`, and
`delete_news_item()`. Each method must acquire `_lock`, load once, validate
against that same snapshot, modify only its owned array, and call
`save_source()`. Sort stored news descending by parsed date for deterministic
output, with identifier as the stable tie-breaker. A failed validation or save
must leave the live catalog unchanged.

Implement the four target endpoints with `@requires_auth`. Reject missing or
non-object JSON before calling manager methods. Keep responses consistent with
the existing `{success, message|error}` envelope. The GET endpoint must tolerate
missing legacy arrays as empty, but malformed stored shapes should return a
safe 500/error response and log the internal detail rather than overwriting the
catalog.

Add all three POST routes and payloads to the route suite's mutating-route auth
matrix. Prove that a successful update creates the normal pre-change backup and
that a forced `save_source()` failure preserves the original file byte-for-byte.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'featured or news_route or editorial_auth or editorial_atomic'
```

Expected: auth, round-trip order, upsert/delete, unknown targets, backup, and
atomic-failure tests pass.

### Step 3: Build the Source Information editorial controls

Extend the existing Source Information panel instead of adding another bottom
navigation item. Add two clearly separated sections:

1. **Featured Apps** — an add selector sourced from `/api/editorial`, a numbered
   list, remove controls, and up/down buttons. Disable Add at five entries and
   save the complete ordered list with one explicit button.
2. **News** — newest-first rows plus a form for all supported fields, a checkbox
   for `notify`, an app selector for optional `appID`, Edit and Delete controls,
   and a “New item” reset action. A small “Prefill from app” action may fill
   title/caption/app/date client-side, but must not save automatically.

Use existing CSS tokens/components. Set sensible input types (`datetime-local`,
`url`, `color`) while still enforcing all rules server-side. Every catalog value
inserted into HTML must pass `escapeHtml`; prefer `textContent` and DOM property
assignment. Delete requires the app's existing confirmation pattern. Refresh
the editorial view after any successful mutation and keep validation errors in
the current toast system.

Do not overload the existing `/api/update-source` payload or submit editorial
state whenever scalar source information is saved; these are independent
buttons so a stale browser cannot clobber another operator's list.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k 'editorial_ui or source_info_form'
```

Expected: stable DOM/JS hooks exist, values are escaped, and the prior scalar
form behavior remains intact.

### Step 4: Document and regression-test the complete contract

Update README's authenticated route table and briefly explain the five-item
featured limit, news schema validation, and that `notify` does not send a
server-side message. Add end-to-end route tests proving:

- ordered featured selection appears unchanged in `/source.json`;
- zero and five selections work; six, duplicate, and unknown IDs fail;
- news create, edit, and delete round-trip through `/source.json`;
- an edit cannot create a duplicate identifier;
- optional fields survive with correct JSON types;
- invalid date/color/URL/boolean/app reference never changes the catalog;
- all mutations require auth and use the backup/atomic save path;
- existing apps, versions, source metadata, and unrelated news items survive;
- `notify: true` causes no call to the server's `notify()` function;
- UI rendering treats a title/caption containing markup as text.

Run the focused and full suites. If a test fails only because it expected the
old mutating-route count, update the exact matrix and keep the assertion strict.

Prove discrimination: temporarily bypass the exact app-reference check. The
unknown featured-app and unknown news `appID` cases must fail. Restore the guard
before final verification.

**Verify**:

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
git diff --check
git status --short
```

Expected: all tests pass; diff check is clean; only in-scope files plus the
`plans/README.md` status row are modified.

## Done criteria

- [ ] Operators can order zero to five existing apps as `featuredApps` without
      hand-editing JSON.
- [ ] Operators can create, edit, and delete schema-valid news items from the
      authenticated Source Information panel.
- [ ] All references point to current exact bundle identifiers; malformed or
      dangling values are rejected before a write.
- [ ] News dates, colors, URLs, booleans, identifiers, and lengths are validated
      and serialized deterministically.
- [ ] The public `/source.json` reflects successful edits and no private/admin
      fields are introduced.
- [ ] Mutations preserve locking, backups, atomic replacement, and unchanged-on-
      failure guarantees.
- [ ] No editorial action triggers an external notification or remote fetch.
- [ ] Browser rendering escapes all catalog-provided content.
- [ ] Existing scalar source editing and all prior tests still pass.
- [ ] README documents the routes and operator-visible limits.

## Failure modes and mitigations

| Failure mode | Mitigation |
|---|---|
| Stale/dangling featured or news app reference | Validate against the same locked catalog snapshot used for the write |
| Two operators overwrite each other | Separate narrow mutations; lock every read-modify-write; future ETags are out of scope |
| Malformed date produces inconsistent ordering | Require timezone-aware ISO-8601 and store canonical UTC values |
| Remote URL becomes an SSRF vector | Validate syntax only; never fetch it server-side |
| `notify` unexpectedly sends Telegram | Treat it as catalog data and regression-test that `notify()` is not called |
| Script injection through catalog text | Escape before rendering and prefer `textContent` |
| Partial/corrupt catalog write | Reuse `save_source()` backup and atomic replacement path |
| Official AltStore contract changes | Re-check the primary documentation at execution and STOP on material drift |

## STOP conditions

- The official AltStore schema no longer matches the required/optional fields
  recorded here. Report the new primary-source contract before implementing.
- Editorial mutations cannot reuse `SourceManager._lock` and `save_source()`
  without bypassing current backup or atomic-write guarantees.
- Supporting a requested URL would require the server to fetch, proxy, or
  inspect remote content.
- Existing catalogs contain more than five featured entries and product intent
  is to preserve them through this editor. Report the compatibility decision;
  do not silently truncate stored data.
- A valid news item requires HTML or another rendering path that cannot be made
  safe with text-only escaping in the current UI.
- Tests require live AltStore, GitHub, image, or article access.

## Rollback

Revert the implementation commit. Existing `featuredApps` and `news` data stays
valid in `source.json`; only the admin editor/routes disappear. If implementation
wrote invalid data despite the guards, stop the app and restore the immediately
preceding catalog backup with the existing guarded recovery procedure—never
replace the catalog while the process is concurrently writing.

## Maintenance note

Keep the accepted field allowlist synchronized with the official AltStore source
documentation. If Plan 071 later provides import provenance, a future feature may
prefill a news draft from a successful import, but it must remain an explicit
operator-reviewed save and is not a dependency of this plan.
