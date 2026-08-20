# Plan 059: Actually fix the `<select>` chevron (stuck top-left)

> UI-only, one CSS rule in `templates/index.html`. No app/JS change.
> Drift check: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 236 passed.

## Status
- Priority P2 (visible bug). Effort XS. Risk LOW. Planned at commit `39d96c1`.

## Why
The filter dropdowns' chevron renders at the **top-left** instead of the right.
The `select` rule (below) sets `background-position: calc(100% - 14px) center;`, but
it's being overridden/reset to `0 0` in the cascade (an earlier `background:`
shorthand's position wins when the value loses). Two prior attempts
(`right 14px center` — invalid 3-value — and the `calc` 2-value) both landed
top-left. Fix it definitively with the **axis longhands** + an **edge keyword**
(no `calc`) + `!important` so nothing can override or reject it.

## Current state — `templates/index.html`, the `select` rule (~line 212)
```css
select {
    appearance: none; -webkit-appearance: none; -moz-appearance: none;
    background-color: var(--surface-2);
    background-image: url("data:image/svg+xml,...chevron...");
    background-repeat: no-repeat;
    background-position: calc(100% - 14px) center;   /* <-- being reset to 0 0 */
    padding-right: 38px;
}
```
(There is also a base rule `input, textarea, select { background: var(--surface-2); }`
whose `background` shorthand sets position to `0 0`; when the select's position
value loses, the chevron falls there.)

## The fix
Replace the single `background-position` line with the two axis longhands, edge
keyword, `!important`:
```css
    background-position-x: right 14px !important;
    background-position-y: center !important;
```
So the rule becomes:
```css
select {
    appearance: none; -webkit-appearance: none; -moz-appearance: none;
    background-color: var(--surface-2);
    background-image: url("data:image/svg+xml,...chevron...");   /* keep as-is */
    background-repeat: no-repeat;
    background-position-x: right 14px !important;
    background-position-y: center !important;
    padding-right: 38px;
}
```
(Keep the existing chevron `background-image` untouched. Do NOT reintroduce
`calc(100% - 14px) center` or `right 14px center`.)

## Verify
```bash
grep -c "background-position-x: right 14px !important" templates/index.html   # 1
grep -c "calc(100% - 14px) center\|right 14px center" templates/index.html    # 0
grep -c "<style>\|</style>" templates/index.html                              # 2
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q                         # 236 passed
```

## Done criteria
- [ ] The `select` rule uses `background-position-x: right 14px !important;` + `background-position-y: center !important;`; the old single `background-position` line is gone.
- [ ] `git status --short` shows only `templates/index.html`; suite `236 passed`.

## Maintenance note
- If the chevron STILL renders top-left after this, the override is an inline
  style or a higher-specificity rule on `.filter-select`/`.search-filter-bar select`
  — inspect the element's *computed* `background-position` in devtools to find the
  winning rule. But axis-longhand + `!important` should end it.
