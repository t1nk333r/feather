# Plan 057: Liquid Glass re-skin — translucent blurred surfaces, rounder corners, floating glass tab bar; fix the dropdowns

> **Executor instructions**: UI-only change to `templates/index.html` (CSS +
> minimal wrapper markup). Do NOT change `app.py`, server code, or JS logic. Run
> the verification gates. If a STOP condition occurs, stop and report. Update this
> plan's status row in `plans/README.md` when done unless a reviewer maintains it.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat ca3e87d..HEAD -- templates/index.html tests/
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 236 passed
> ```
> If the tokens/surfaces changed since `ca3e87d`, compare the "Current state"
> excerpts against the live file first; on a mismatch, STOP.

## Status

- **Priority**: P2 (the requested aesthetic; also fixes visibly-broken dropdowns)
- **Effort**: M–L
- **Risk**: MED (broad visual restyle; legibility must be preserved over glass)
- **Depends on**: plans 043/055 (tokens + bottom-bar structure) — merged
- **Category**: UI / design
- **Planned at**: commit `ca3e87d`, 2026-08-19
- **Decided with the maintainer**: make it **Liquid Glass all around** —
  translucent, blurred, glassy surfaces with **rounded corners** — and **fix the
  dropdowns** (chevron renders in the wrong place).

## Why this matters

The current theme is **flat, opaque** iOS (solid `--surface` panels, no blur).
The maintainer wants **Liquid Glass**: translucent "frosted" surfaces that blur
what's behind them, generous rounded corners, and floating glass chrome (top bar +
bottom tab bar). Separately, the filter **dropdowns are broken** — the `<select>`
chevron lands at the top-left instead of the right.

## Current state — `templates/index.html`

### Tokens (`:root`, top of `<style>`; plan 043/055)
Light: `--canvas:#f2f2f7; --surface:#fff; --surface-2:#f2f2f7; --border:rgba(60,60,67,0.18); --separator:rgba(60,60,67,0.29); --radius-lg:12px; --radius-md:10px; --shadow-card:0 0 0 0.5px var(--border),0 1px 2px rgba(0,0,0,0.05);` etc.
Dark (`@media (prefers-color-scheme: dark)` and the `[data-theme]` blocks if present): `--canvas:#000; --surface:#1c1c1e; --surface-2:#2c2c2e; --border:rgba(84,84,88,0.50);` etc.

### Surfaces (all currently opaque, no blur)
- `body { background: var(--canvas); padding:20px; }`
- `.container { max-width:900px; background:var(--surface); display:flex; flex-direction:column; box-shadow:var(--shadow-card); }`
- `.topbar { position:sticky; top:0; background:var(--surface); border-bottom:1px solid var(--separator); }`
- `.tabbar { position:fixed; bottom:0; left:0; right:0; background:var(--surface); border-top:1px solid var(--separator); padding-bottom:env(safe-area-inset-bottom,0); }`
- `.app-card { background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-lg); box-shadow:var(--shadow-card); }`
- `.modal-content { background:var(--surface); border-radius:var(--radius-lg); box-shadow:var(--shadow-modal); }`
- Inputs (line ~176): `input, textarea, select { background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-md); padding:12px; }`

### The dropdown bug (line ~200)
```css
select {
    appearance: none; -webkit-appearance: none; -moz-appearance: none;
    background-color: var(--surface);
    background-image: url("data:image/svg+xml,...chevron...");
    background-repeat: no-repeat;
    background-position: right 14px center;   /* <-- INVALID 3-value syntax; some browsers reject it → chevron falls back to top-left */
    padding-right: 38px;
}
```
`right 14px center` mixes an edge-offset with a keyword — a 3-value form that
modern CSS dropped; strict browsers ignore the whole declaration and the chevron
falls to `0 0` (top-left). Fix: `background-position: calc(100% - 14px) center;`.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `236 passed` before AND after |
| Style block intact | `grep -c "<style>\|</style>" templates/index.html` | `2` |
| Glass present | `grep -c "backdrop-filter" templates/index.html` | ≥ 5 (surfaces made glassy) |
| Dropdown fixed | `grep -c "right 14px center" templates/index.html` | `0` (replaced with calc) |

## Scope

**In scope:** `templates/index.html` — glass tokens + restyle the surfaces to
translucent/blurred/rounded, a soft body background so glass refracts, the
floating glass tab bar, and the `<select>` fix.

**Out of scope:** `app.py`/server; JS logic; panel inner content; renaming any
`class`/`id`/hook. No new images/fonts/CDN. Do not regress the plan-055 contrast —
keep glass opaque enough that text stays legible.

## Steps

### Step 1: Baseline → `236 passed`. If not, STOP.

### Step 2: Add glass tokens (both `:root` blocks — light and dark)
```css
/* light :root */
--glass-bg: rgba(255,255,255,0.65);
--glass-bg-strong: rgba(255,255,255,0.80);   /* bars/modals where legibility matters most */
--glass-blur: saturate(180%) blur(20px);
--glass-highlight: rgba(255,255,255,0.55);    /* top/edge specular border */
--glass-border: rgba(255,255,255,0.35);
--radius-lg: 18px;   /* rounder */
--radius-md: 12px;
--radius-xl: 26px;   /* floating tab bar / large cards */

/* dark :root (prefers-color-scheme: dark, and the [data-theme="dark"] block if present) */
--glass-bg: rgba(28,28,30,0.55);
--glass-bg-strong: rgba(28,28,30,0.72);
--glass-blur: saturate(180%) blur(22px);
--glass-highlight: rgba(255,255,255,0.14);
--glass-border: rgba(255,255,255,0.10);
```
(Keep the existing `--label*`, `--accent*`, `--danger*`, `--separator` tokens; only
ADD these and bump the radii.)

### Step 3: A soft background so glass has something to refract
```css
body {
    background:
      radial-gradient(1200px 800px at 15% -10%, rgba(0,122,255,0.10), transparent 60%),
      radial-gradient(1000px 700px at 110% 10%, rgba(175,82,222,0.10), transparent 55%),
      var(--canvas);
    background-attachment: fixed;
}
```
(Dark mode inherits the same gradients over `--canvas:#000` — the low-alpha washes
read as subtle depth. Keep them faint.)

### Step 4: Make the surfaces glass
Add a reusable pattern (translucent bg + blur + hairline highlight) to each surface.
Always include BOTH `-webkit-backdrop-filter` and `backdrop-filter` (Safari/iOS needs
the prefix), and a solid-ish fallback via the token alpha:
- `.container`: keep the flex column, but make it transparent (let its children be
  the glass) OR give it `background: transparent; box-shadow:none;` so the page
  background shows through to the panels. (Simplest: container transparent; the
  topbar/cards/tabbar are the glass.)
- `.topbar`: `background: var(--glass-bg-strong); -webkit-backdrop-filter: var(--glass-blur); backdrop-filter: var(--glass-blur); border-bottom: 1px solid var(--glass-border); box-shadow: inset 0 1px 0 var(--glass-highlight);`
- `.tabbar` (**floating glass pill** — the signature Liquid-Glass element): give it
  side/bottom margins and a big radius so it floats:
  ```css
  .tabbar {
      left: 12px; right: 12px; bottom: calc(10px + env(safe-area-inset-bottom,0px));
      border-radius: var(--radius-xl);
      background: var(--glass-bg-strong);
      -webkit-backdrop-filter: var(--glass-blur); backdrop-filter: var(--glass-blur);
      border: 1px solid var(--glass-border);
      box-shadow: 0 8px 30px rgba(0,0,0,0.18), inset 0 1px 0 var(--glass-highlight);
  }
  ```
  (Adjust `.workspace` bottom padding to clear the now-floating bar, e.g.
  `calc(96px + env(safe-area-inset-bottom,0px))`.)
- `.app-card`, `.parser-options`, `.version-item`, `.info-box`: `background: var(--glass-bg); -webkit-backdrop-filter: var(--glass-blur); backdrop-filter: var(--glass-blur); border: 1px solid var(--glass-border); border-radius: var(--radius-lg); box-shadow: 0 4px 20px rgba(0,0,0,0.06), inset 0 1px 0 var(--glass-highlight);`
- `.modal-content`: `background: var(--glass-bg-strong); -webkit-backdrop-filter: var(--glass-blur); backdrop-filter: var(--glass-blur); border:1px solid var(--glass-border); border-radius: var(--radius-lg);`
- `.toast`: same glass treatment (keep the left accent border).
- Inputs/textarea: `background: var(--glass-bg); border:1px solid var(--glass-border); border-radius: var(--radius-md); -webkit-backdrop-filter: var(--glass-blur); backdrop-filter: var(--glass-blur);`

### Step 5: Fix the dropdowns (`select`)
In the `select` rule: change `background-position: right 14px center;` →
`background-position: calc(100% - 14px) center;`. Give the select the glass fill too
(`background-color: var(--glass-bg)` — but keep the `background-image` chevron; set
`background` via the two longhands `background-color` + `background-image`, never a
`background:` shorthand that would wipe the chevron). Ensure `appearance:none`
(+ `-webkit-`/`-moz-`) stays. Verify the chevron sits on the RIGHT.

### Step 6: Legibility guard
Because glass lowers contrast, keep `--glass-bg-strong` on text-dense chrome (bars,
modals) and keep the plan-055 `--label*` tokens. If any body text becomes hard to
read over glass, raise that surface's token alpha (toward opaque) rather than
darkening text. Do a quick check: the app list rows, form labels, and tab labels
must stay clearly readable in BOTH themes.

**Verify**: `grep -c "<style>\|</style>"` → `2`; `grep -c "right 14px center"` → `0`; `grep -c "backdrop-filter"` → ≥ 5.

### Step 7: Full suite unchanged → `236 passed`.

## Done criteria
- [ ] Surfaces (top bar, bottom tab bar, cards, modals, inputs, selects, toasts) are translucent + blurred (`backdrop-filter`, with `-webkit-` prefix), with a subtle highlight edge and rounder corners; the body has a faint gradient so the glass reads as glass.
- [ ] The bottom tab bar floats as a rounded glass pill; the workspace clears it.
- [ ] The `<select>` chevron renders on the RIGHT (`calc(100% - 14px) center`); dropdowns look correct.
- [ ] Text stays legible in light AND dark (contrast preserved).
- [ ] No JS/hook/panel-content change; `grep -c "<style>\|</style>"` → `2`; suite `236 passed`; `git status --short` shows only `templates/index.html`.

## STOP conditions
- Making a surface glassy drops its text below readable contrast and raising the
  token alpha doesn't fix it — report rather than shipping unreadable glass.
- A test asserts a specific opaque color/old-layout markup — reconcile; report if it fundamentally conflicts with the glass restyle.

## Manual verification (do this, report what you saw)
Run the app, log in, and confirm in a browser: surfaces are frosted/translucent
with blur, the tab bar floats as a glass pill, cards/inputs are rounded and glassy,
the **dropdown chevrons are on the right and the menus look correct**, and
everything is legible in **light and dark**. If no browser, exercise `GET /` via the
test client (confirm `backdrop-filter` present, no `right 14px center`) and say so —
a human will eyeball the glass.

## Maintenance notes
- `backdrop-filter` is supported on iOS Safari (`-webkit-` prefix) and Chromium;
  always ship both. On a browser without support, the token alpha makes surfaces a
  translucent solid — still fine.
- Glass is a contrast tax; if the maintainer later reports faint text, raise the
  `--glass-bg*` alpha (toward opaque) — that's the tuning knob, not text color.
- The floating tab-bar margins + radius are the most "tunable" bit; expect a couple
  of nudges after the maintainer sees it on a phone.
- Reviewer: confirm dropdowns are fixed, glass is applied broadly (≥5 surfaces),
  both prefixes are present, and text is legible in both themes.
