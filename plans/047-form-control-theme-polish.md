# Plan 047: Theme polish — style native `<select>` and fix faint placeholders (iOS token system)

> **Executor instructions**: Follow this plan step by step. This is a CSS-only
> change to `templates/index.html`; do NOT touch HTML structure, any
> `class`/`id`, or any JavaScript. Run the verification gates. If a STOP
> condition occurs, stop and report. Update this plan's status row in
> `plans/README.md` when done unless a reviewer maintains the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat c052cb0..HEAD -- templates/index.html
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 205 passed
> ```

## Status

- **Priority**: P3 (polish on the new iOS theme from plan 043)
- **Effort**: XS–S
- **Risk**: LOW (CSS only; no DOM/JS change)
- **Depends on**: plan 043 (the `:root` token system it builds on — merged)
- **Category**: UI polish
- **Planned at**: commit `c052cb0`, 2026-08-18

## Why this matters

After the iOS re-skin (plan 043), two form-control rough edges remain:
1. **Native `<select>`** (e.g. the Import-from-Repo "Provider" dropdown) only gets
   the generic input styling — no custom disclosure chevron — so the closed
   control looks unstyled next to the iOS inputs.
2. **Placeholder text** (e.g. "owner/repo or a full repo URL", "leave blank to
   auto-detect") renders too faint in dark mode — the browser default placeholder
   color has poor contrast on the dark surface.

Both are small CSS fixes on the existing token system. (The *open* dropdown popup
is drawn by the OS and can't be restyled — that's expected and out of scope.)

## Current state — `templates/index.html` (`<style>` block, 043 tokens in `:root`)

- `input, textarea, select` share one rule: `background: var(--surface); color: var(--label); border: 1px solid var(--border); border-radius: var(--radius-md);` (plus padding). No `appearance` override on `select`, so it shows the OS default arrow.
- There is **no `::placeholder` rule**, so placeholders fall back to the UA default color.
- Tokens available (from 043): `--surface`, `--surface-2`, `--label`, `--label-secondary`, `--label-tertiary`, `--border`, `--accent`, `--radius-md`, etc. `--label-secondary` is the Apple secondaryLabel (readable but muted); `--label-tertiary` is fainter.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `205 passed` before AND after (no DOM change) |
| Style block intact | `grep -c "<style>\|</style>" templates/index.html` | `2` |
| New rules present | `grep -nE "::placeholder|appearance: *none" templates/index.html` | matches present |

## Scope

**In scope:** `templates/index.html` `<style>` block only — a `::placeholder`
rule and a `select` appearance/chevron rule.

**Out of scope:** any HTML/JS/`class`/`id` change; the OS-drawn open dropdown
popup; replacing the native `<select>` with a custom JS widget (do NOT build a
custom dropdown — keep the native element, just skin the closed control).

## Steps

### Step 1: Baseline → `205 passed`. If not, STOP.

### Step 2: Placeholder contrast
Add a rule (near the `input, textarea, select` rule):
```css
input::placeholder, textarea::placeholder {
    color: var(--label-secondary);
    opacity: 1;               /* Firefox lowers placeholder opacity by default */
}
```

### Step 3: Style the closed `<select>`
Add a `select`-specific rule that removes the native arrow and draws an iOS-style
chevron. Use a single mid-gray chevron (Apple systemGray `#8e8e93`) so it reads on
both light and dark surfaces — a data-URI SVG background can't reference a CSS
`var()`, and systemGray is legible on both:
```css
select {
    appearance: none;
    -webkit-appearance: none;
    -moz-appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='none' stroke='%238e8e93' stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round' d='M1 1.5 6 6.5 11 1.5'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 14px center;
    padding-right: 38px;      /* room for the chevron */
}
```
(Keep the shared `background: var(--surface)` from the base rule — a `select` with
both a `background-color` token and a `background-image` chevron works because
`background-image` and `background-color` are different longhands; but if the base
rule uses the `background` shorthand and this one also needs the color, set
`background-color: var(--surface)` explicitly here to be safe.)

**Verify**: `grep -c "<style>\|</style>" templates/index.html` → `2` (block still balanced); `grep -nE "::placeholder|appearance: *none" templates/index.html` shows the new rules.

### Step 4: Suite unchanged
```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # still 205 passed
```
(No DOM changed, so the count must be identical. If a test fails, you touched
markup — revert.)

## Done criteria
- [ ] Placeholders use `var(--label-secondary)` with `opacity:1` (readable in dark mode).
- [ ] `<select>` shows a custom chevron and no native arrow; the closed control matches the iOS inputs; `background-color` still tokenized.
- [ ] `grep -c "<style>\|</style>"` → `2`; suite still `205 passed`; `git status --short` shows only `templates/index.html`.

## STOP conditions
- Removing the native arrow leaves the control with no affordance in some engine — keep the chevron background; if `appearance:none` breaks the control's box in an unexpected way, report rather than restructuring.
- A reviewer wants the OPEN popup styled too — that's an OS limitation; note it, don't attempt a custom-widget rewrite here.

## Maintenance notes
- If a later plan adds a manual light/dark toggle, the systemGray chevron still
  works in both; no change needed.
- Reviewer: eyeball the Provider dropdown and the import/bundle-id placeholders in
  both light and dark; confirm no markup/JS changed (suite count identical).
