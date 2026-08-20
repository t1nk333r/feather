# Plan 062: Restore rounded corners + make the bottom bar a floating glass pill

> UI-only, `templates/index.html` (radius tokens + `.tabbar`/`.tab`/`.workspace`).
> No app/JS change. Keeps the Omarchy palette (black + orange, glass) — only
> corners and the bottom-bar shape change.
> Drift check: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 236 passed.

## Status
- Priority P2 (user-reported look regression). Effort S. Risk LOW. Planned at commit `b8ac0dc`.

## Why
The maintainer wants two things back:
1. **Rounded corners** across the UI — the Omarchy theme (plan 058) squared
   everything to `--radius-lg/md/sm: 3px/3px/2px`; it now looks too sharp.
2. The **bottom tab bar should float as a rounded capsule pill** (like the
   reference: a detached, fully-rounded glass bar with side/bottom margins,
   content scrolling under it, and a rounded highlight behind the active tab) —
   not the current full-width flat bar.

Keep the Omarchy palette, the glass blur, the SF-style icons (061), and all hooks.

## Current state — `templates/index.html`

Radius tokens (light `:root` ~line 50; there is NO separate radius block in the
dark `@media` — tokens are shared):
```css
--radius-lg: 3px;
--radius-md: 3px;
--radius-sm: 2px;
```
`--fill` / `--fill-hover` exist in both themes (light ~26-27, dark ~66-67).

`.workspace` (line 141):
```css
.workspace {
    flex: 1 1 auto;
    padding: 24px 20px calc(64px + env(safe-area-inset-bottom, 0px));  /* clear the flat bottom bar */
    overflow-x: hidden;
    min-width: 0;
}
```
`.tabbar` / `.tab` / `.tab.active` (lines 147-170):
```css
.tabbar {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    display: flex;
    border-radius: 0;
    background: color-mix(in srgb, var(--surface) 62%, transparent);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    backdrop-filter: saturate(180%) blur(20px);
    border-top: 1px solid var(--separator);
    padding-bottom: env(safe-area-inset-bottom, 0);
}
.tab {
    flex: 1 1 0; min-width: 0;
    display: flex; flex-direction: column; align-items: center; gap: 3px;
    border: none; background: none; cursor: pointer;
    padding: 8px 4px 6px;
    color: var(--label-secondary);
    font-family: var(--sans);
    transition: color .15s;
}
.tab svg { width: 24px; height: 24px; display: block; }
.tab .tab-label { font-size: 10px; line-height: 1; letter-spacing: 0.01em; }
.tab.active { color: var(--accent); }
.tab:hover { color: var(--label); }
```
There is ALSO a mobile media-query override (~line 627):
```css
.workspace { padding: 20px 16px calc(72px + env(safe-area-inset-bottom, 0px)); }
```

## The change

### 1. Round the corners (radius tokens, ~line 50)
```css
--radius-lg: 14px;
--radius-md: 10px;
--radius-sm: 8px;
```
(One change; every `border-radius: var(--radius-*)` across the file follows. Do
NOT touch any other token or add a dark-mode radius block.)

### 2. Float the bottom bar as a glass pill (`.tabbar`, line 147)
```css
.tabbar {
    position: fixed;
    left: 12px; right: 12px;
    bottom: calc(10px + env(safe-area-inset-bottom, 0px));
    z-index: 20;
    display: flex;
    gap: 4px;
    padding: 6px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--surface) 72%, transparent);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    backdrop-filter: saturate(180%) blur(20px);
    border: 1px solid var(--separator);
    box-shadow: 0 8px 30px rgba(0,0,0,0.35);
}
```
- Floats with 12px side margins + a 10px+safe-area bottom gap; fully-rounded
  (`999px`) capsule; full `border` (not just `border-top`); a soft drop shadow so
  it reads as floating; `color-mix` bumped 62%→72% (more opaque, legible while
  floating). Remove the old `left:0;right:0;bottom:0`, `border-radius:0`,
  `border-top`, and the standalone `padding-bottom` (the safe-area is now folded
  into `bottom:`).

### 3. Rounded highlight behind the active tab (`.tab` + `.tab.active`)
```css
.tab {
    flex: 1 1 0; min-width: 0;
    display: flex; flex-direction: column; align-items: center; gap: 3px;
    border: none; background: none; cursor: pointer;
    padding: 8px 4px 6px;
    border-radius: 999px;                 /* so the active pill is capsule-shaped */
    color: var(--label-secondary);
    font-family: var(--sans);
    transition: color .15s, background .15s;
}
.tab svg { width: 24px; height: 24px; display: block; }
.tab .tab-label { font-size: 10px; line-height: 1; letter-spacing: 0.01em; }
.tab.active {
    color: var(--accent);
    background: var(--fill-hover);         /* subtle rounded highlight behind the active tab */
}
.tab:hover { color: var(--label); }
```
(The bar's `padding: 6px` insets the active pill from the capsule edge, matching
the reference. Active icon+label stay the orange accent via `currentColor`.)

### 4. Clear the now-floating bar (`.workspace` bottom padding, 2 places)
- Line 143: `calc(64px + ...)` → `calc(92px + env(safe-area-inset-bottom, 0px))`.
- Mobile media query (~line 627): `calc(72px + ...)` → `calc(92px + env(safe-area-inset-bottom, 0px))`.
(The floating bar sits ~10px off the bottom and is ~56px tall, so ~92px clears it.)

## Verify
```bash
grep -c "border-radius: 999px" templates/index.html          # 2 (.tabbar + .tab)
grep -c "\-\-radius-lg: 14px" templates/index.html            # 1
grep -c "border-radius: 0;" templates/index.html              # 0 (the old flat-bar line is gone)
grep -c "backdrop-filter" templates/index.html               # 2 (glass kept on .tabbar)
grep -c "<style>\|</style>" templates/index.html              # 2
grep -c "switchTab(" templates/index.html                    # 7 (unchanged)
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q         # 236 passed
```

## Done criteria
- [ ] Radius tokens are 14/10/8; cards/inputs/modals/buttons visibly rounded again.
- [ ] `.tabbar` floats (side+bottom margins, `border-radius:999px`, full border, drop shadow, glass kept); active tab has a rounded `--fill-hover` highlight with orange icon+label.
- [ ] `.workspace` bottom padding (both the base rule and the media query) clears the floating bar (~92px).
- [ ] `switchTab`/hooks/SF icons unchanged; `git status --short` shows only `templates/index.html`; suite `236 passed`.

## STOP conditions
- If a test asserts a specific `border-radius: 3px`/`0` or the flat full-width bar markup, reconcile or report.

## Maintenance note
- Floating-bar geometry (`left/right:12px`, `bottom:10px`, `padding:6px`, `999px`,
  `92px` clearance) are the tunable knobs; expect a nudge after the maintainer sees
  it on a phone. If the pill looks too see-through over busy content, raise the
  `color-mix` 72% toward opaque.
