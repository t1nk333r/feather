# Plan 058: Re-theme to the Omarchy look — flat terminal dark (black + orange), thin lines, sharp corners, mono chrome

> **Executor instructions**: UI-only change to `templates/index.html` (CSS
> tokens + font stacks + flatten). Do NOT change `app.py`, server code, or JS
> logic. Run the verification gates. If a STOP condition occurs, stop and report.
> Update this plan's status row in `plans/README.md` when done unless a reviewer
> maintains it.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat e05600f..HEAD -- templates/index.html tests/
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 236 passed
> ```
> If the tokens/surfaces changed since `e05600f`, compare the "Current state"
> excerpts against the live file first; on a mismatch, STOP.

## Status

- **Priority**: P2 (the maintainer rejected the glass theme; wants this look)
- **Effort**: M
- **Risk**: MED (full palette + font + flatten pass; keep legibility & hooks)
- **Depends on**: plans 043/055 (token system, bottom-bar structure) — merged
- **Category**: UI / theme
- **Planned at**: commit `e05600f`, 2026-08-19
- **Decided with the maintainer**: replace the current theme with the
  **omarchyplugins.com** look — a flat, dark, terminal/tiling-WM aesthetic
  (pure black, thin grey lines, one orange accent, square corners, mono chrome).
  **This supersedes the Liquid Glass theme (plan 057)** and re-skins the
  043/055 palette.

## Why this matters

The maintainer wants the **Omarchy** visual style (from omarchyplugins.com): a
flat, high-contrast, developer/terminal aesthetic — pure black background,
near-black panels divided by thin grey lines (no shadows), a single hot orange-red
accent, **square corners**, and monospace for the chrome. The current theme is the
opposite (translucent Liquid Glass, rounded, blurred). This plan swaps the entire
palette + fonts to Omarchy's exact tokens and flattens everything.

## Current state — `templates/index.html`

- Token system in `:root` (light) + `@media (prefers-color-scheme: dark)` (dark),
  currently the iOS/glass palette from plans 043/055/057: `--canvas`, `--surface`,
  `--surface-2`, `--label`, `--label-secondary`, `--label-tertiary`, `--separator`,
  `--border`, `--fill`/`--fill-hover`, `--accent`(#007aff)/`--accent-tint`,
  `--danger`, `--success`, `--warning`, `--radius-lg/md/sm(/xl)`, `--shadow-*`, and
  the plan-057 **glass** tokens `--glass-bg`, `--glass-bg-strong`, `--glass-blur`,
  `--glass-highlight`, `--glass-border`.
- Plan 057 applied `backdrop-filter: var(--glass-blur)` (+ `-webkit-`) to ~9
  surfaces, a dual `radial-gradient` **body background**, a **floating rounded**
  `.tabbar` pill, and `inset ... var(--glass-highlight)` specular edges. **All of
  that must be removed** (flat, no blur, no gradient, no float, no glow).
- `body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', ... }` — no
  web fonts, no CDN (keep it self-contained).
- Structure (plan 055, keep it): a `.topbar` (brand + Sign out), a `.workspace`
  with the six `.tab-content` panels, and a fixed bottom `.tabbar` of `.tab`
  buttons. `switchTab` + all hooks unchanged.
- The `<select>` chevron fix (`calc(100% - 14px) center`) from 057 — **keep it.**

## The Omarchy palette (exact — from their `style.css`)

```
DARK (default :root)                 LIGHT (:root[data-theme="light"] / your light block)
--bg           #000                  #f8f8f6
--panel        #0b0b0d               #f1f1ef
--panel-2      #101012               #ffffff
--line         #28282c               #cdcdca
--line-strong  #3a3a3f               #b9b9b6
--line-soft    #1c1c20               #dededb
--text         #d7d7d9               #19191b
--muted        #aaaab0               #65656a
--faint        #7d7d84               #6d6d74
--accent       #ff5a36               #c6371c
--accent-contrast #111                #ffffff
--stable(success) #b4c96f            #65751e
--updated(warning) #ffb000           #9a6700
```
Fonts: `--mono` = JetBrains Mono / Nerd Fonts …; `--sans` = Inter …. (We will use
**system fallback stacks** — no web-font download — see Step 3.)

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `236 passed` before AND after |
| Style block intact | `grep -c "<style>\|</style>" templates/index.html` | `2` |
| Glass removed | `grep -c "backdrop-filter\|--glass\|radial-gradient" templates/index.html` | `0` |
| Accent switched | `grep -c "#ff5a36\|#c6371c" templates/index.html` | ≥ 2 (dark + light accent) |
| Dropdown fix kept | `grep -c "calc(100% - 14px) center" templates/index.html` | `1` |

## Scope

**In scope:** `templates/index.html` — replace the palette tokens (light + dark)
with Omarchy's, set mono/sans font stacks, apply mono to the chrome, and flatten
(remove all 057 glass/blur/gradient/float/glow; square the corners; thin lines).

**Out of scope:** `app.py`/server; JS logic; panel inner content; renaming any
`class`/`id`/hook; downloading web fonts / any CDN (use system fallback stacks);
re-laying-out to Omarchy's 3-pane sidebar (keep the plan-055 bottom bar — the
maintainer chose mobile-first; a sidebar layout is a separate decision).

## Steps

### Step 1: Baseline → `236 passed`. If not, STOP.

### Step 2: Replace the token values (map Omarchy → existing token names)
Keep the existing token NAMES (the rest of the CSS references them) but set new
values, in BOTH the light `:root` and the dark block:
```
--canvas:        #000     / #f8f8f6
--surface:       #0b0b0d  / #ffffff
--surface-2:     #101012  / #f1f1ef
--label:         #d7d7d9  / #19191b
--label-secondary:#aaaab0 / #65656a
--label-tertiary:#7d7d84  / #6d6d74
--separator:     #28282c  / #cdcdca
--border:        #1c1c20  / #dededb
--fill:          rgba(215,215,217,0.06) / rgba(25,25,27,0.05)
--fill-hover:    rgba(215,215,217,0.10) / rgba(25,25,27,0.09)
--accent:        #ff5a36  / #c6371c
--accent-contrast:#111111 / #ffffff
--accent-tint:   rgba(255,90,54,0.14) / rgba(198,55,28,0.10)
--accent-press:  #e64a2b  / #a82c15
--danger:        #e5484d  / #c0392b      (a distinct red for delete, NOT the orange accent)
--danger-tint:   rgba(229,72,77,0.14) / rgba(192,57,43,0.10)
--success:       #b4c96f  / #65751e
--warning:       #ffb000  / #9a6700
```
**Corners → square:** `--radius-lg: 3px; --radius-md: 3px; --radius-sm: 2px;` (drop
`--radius-xl` usage). **Shadows → lines:** `--shadow-card: 0 0 0 1px var(--border);`
`--shadow-modal: 0 0 0 1px var(--separator);` (flat; no soft drop shadows).

### Step 3: Fonts — mono chrome, sans body (system stacks, no download)
Add tokens:
```css
--mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, "Liberation Mono", monospace;
--sans: -apple-system, BlinkMacSystemFont, Inter, "Segoe UI", Roboto, sans-serif;
```
- `body { font-family: var(--sans); background: var(--canvas); }`
- Apply `font-family: var(--mono)` to the **chrome**: `h1, h2, h3`, `.brand`,
  `.tab` (nav labels), and the `.source-url`/any code-ish element. Optionally
  uppercase the small nav labels for a terminal feel (`.tab { text-transform: none }`
  is fine too — keep it readable). Keep inputs/textarea/body prose in `--sans`.

### Step 4: Flatten (remove all 057 glass)
- Delete the `--glass-*` tokens and every `backdrop-filter` / `-webkit-backdrop-filter`
  declaration.
- `body` background: **solid** `var(--canvas)` — remove the dual `radial-gradient`.
- Surfaces (`.topbar`, `.app-card`, `.parser-options`, `.version-item`, `.info-box`,
  `.modal-content`, `.toast`, inputs): `background: var(--surface)` (or `--surface-2`
  for inset inputs), `border: 1px solid var(--border)`, square radius, **no** blur,
  **no** inset highlight, **no** soft shadow. Cards read via the thin line, not depth.
- `.tabbar`: **not** a floating pill anymore — full-width, flat: `left:0; right:0;
  bottom:0; border-radius:0; background: var(--surface); border-top: 1px solid
  var(--separator);` keep `padding-bottom: env(safe-area-inset-bottom,0)`. Reset
  `.workspace` bottom padding to clear a ~56px bar (`calc(64px + env(safe-area-inset-bottom,0px))`).
- `.tab.active { color: var(--accent); }` (orange). Optionally a 2px top accent bar
  on the active tab (`box-shadow: inset 0 2px 0 var(--accent)`), flat.
- `.btn` (primary): `background: var(--accent); color: var(--accent-contrast); border-radius: var(--radius-md);` flat, no gradient. `.btn-danger { background: var(--danger); color:#fff; }`.

### Step 5: Keep the dropdown fix + legibility
- Leave the `<select>` `background-position: calc(100% - 14px) center;` (from 057) —
  do NOT reintroduce `right 14px center`. Give the select `background-color: var(--surface-2)`.
- Verify text is legible: `--text`/`--muted`/`--faint` on `--bg`/`--panel` are
  Omarchy's own values (already balanced) — don't lower them.

**Verify**: `grep -c "backdrop-filter\|--glass\|radial-gradient"` → `0`;
`grep -c "calc(100% - 14px) center"` → `1`; `grep -c "<style>\|</style>"` → `2`.

### Step 6: Full suite unchanged → `236 passed`.

## Done criteria
- [ ] Palette is Omarchy's exact values in light + dark (`--bg #000`/`#f8f8f6`, accent `#ff5a36`/`#c6371c`, thin `--line` borders, olive/amber status).
- [ ] Flat: no `backdrop-filter`, no `--glass-*`, no `radial-gradient` body background, no soft shadows; corners are square (≤3px); the bottom bar is a flat full-width line-topped bar (not a floating pill).
- [ ] Chrome (headings, brand, nav labels) uses the mono stack; body uses sans; no web-font/CDN added.
- [ ] The `<select>` chevron stays on the right (`calc(100% - 14px)`); dropdowns correct.
- [ ] No JS/hook/panel-content change; `grep -c "<style>\|</style>"` → `2`; suite `236 passed`; `git status --short` shows only `templates/index.html`.

## STOP conditions
- A test asserts a specific old color (e.g. `#007aff`) or old-layout markup — reconcile; report if it fundamentally conflicts.
- Removing the glass leaves a surface with no visible boundary (e.g. a black card on black bg with no line) — ensure every surface has a `1px var(--border)` or `var(--separator)` line so it's distinguishable.

## Manual verification (do this, report what you saw)
Run the app, log in, and confirm in a browser: pure-black (dark) / paper (light)
background, near-black panels separated by thin grey lines, the orange accent on
active tab + primary buttons, square corners, mono headings/nav, flat (no blur), a
flat full-width bottom bar, and correct dropdowns — legible in both themes. If no
browser, verify structurally (grep: no glass, accent present, calc chevron) and say so.

## Maintenance notes
- This is a full theme swap living entirely in the `:root` blocks + a few chrome
  rules; future theme changes should stay token-driven.
- Optional faithful upgrade later: self-host **JetBrains Mono** + **Inter** in
  `static/` and point `--mono`/`--sans` at them (no CDN) for an exact match; the
  system stacks here get ~90% of the look on Apple devices.
- Omarchy's own site is a 3-pane sidebar dashboard; we kept the mobile bottom bar
  by decision. If the maintainer later wants the sidebar layout too, that's a
  separate plan on top of this theme.
- Reviewer: confirm all glass is gone, the palette matches the table, corners are
  square, chrome is mono, dropdowns are fixed, and text is legible in both themes.
