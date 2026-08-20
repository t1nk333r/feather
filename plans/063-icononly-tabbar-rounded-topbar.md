# Plan 063: Icons-only floating bar (centered compact pill + rounded highlight) + rounded top bar

> UI-only, `templates/index.html` (tab-bar markup + `.tab`/`.tabbar`/`.topbar`/
> `.brand` img/`.signout` CSS). No app/JS change. Keeps the Omarchy palette + glass.
> Drift check: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 236 passed.

## Status
- Priority P2 (user-requested polish). Effort S. Risk LOW. Planned at commit `431747e`.

## Why
The maintainer wants, on top of plan 062's floating glass pill:
1. **Icons only in the bottom bar** — drop the text labels (Add/Apps/QR/Source/
   Import/Health), keep just the SF-style symbols, each with a rounded highlight
   when active (like the reference X/Twitter bar).
2. **A centered, compact floating bar** — they inset it ~500px each side on desktop
   and loved it; do the responsive equivalent: center the capsule with a max-width
   so it's a compact centered pill on desktop and fills (with margins) on mobile.
3. **Rounded top-bar elements** — the `.topbar` (brand + Sign out) is a flat
   square full-width bar; round it into a floating card, round the brand logo, and
   make "Sign out" a rounded pill.

Keep the Omarchy palette, glass, SF icons, and all hooks. Accessibility: since the
labels go away, add `aria-label` + `title` to each icon button.

## Current state — `templates/index.html`

### Bottom bar markup (lines ~910-934) — six buttons, each `svg` + `<span class="tab-label">`
```html
<nav class="tabbar">
    <button class="tab active" onclick="switchTab('add-app', this)">
        <svg ...>...</svg>
        <span class="tab-label">Add</span>
    </button>
    ... (manage-apps "Apps", qr-code "QR", source-info "Source", import-repo "Import", health "Health")
</nav>
```

### `.tab` / `.tab-label` / `.tab.active` (lines ~157-173, post-062)
```css
.tab {
    flex: 1 1 0; min-width: 0;
    display: flex; flex-direction: column; align-items: center; gap: 3px;
    border: none; background: none; cursor: pointer;
    padding: 8px 4px 6px;
    border-radius: 999px;
    color: var(--label-secondary);
    font-family: var(--sans);
    transition: color .15s, background .15s;
}
.tab svg { width: 24px; height: 24px; display: block; }
.tab .tab-label { font-size: 10px; line-height: 1; letter-spacing: 0.01em; }
.tab.active { color: var(--accent); background: var(--fill-hover); }
.tab:hover { color: var(--label); }
```

### `.tabbar` (lines ~147-156, post-062 floating pill)
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

### `.topbar` / `.brand` / `.signout` (lines ~121-140) and markup (lines ~640-646)
```css
.topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 20px;
    border-bottom: 1px solid var(--separator);
    position: sticky; top: 0; z-index: 5;
    background: var(--surface);
}
.brand { display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 1.0em; color: var(--label); letter-spacing: -0.01em; font-family: var(--mono); }
.btn-link { border: none; background: none; cursor: pointer; font-size: 15px; color: var(--accent); padding: 6px 8px; }
.btn-link.signout { color: var(--danger); }
```
```html
<header class="topbar">
    <div class="brand">
        <img src="/static/icon.svg" alt="" width="24" height="24" style="border-radius:2px;">
        <span>Source Manager</span>
    </div>
    <button class="btn-link signout" onclick="logout()">Sign out</button>
</header>
```

## The change

### 1. Bottom bar → icons only (markup): drop each `<span class="tab-label">`, add `aria-label`+`title`
For every one of the six buttons: remove the `<span class="tab-label">TEXT</span>`
line and add `aria-label="TEXT" title="TEXT"` to the `<button>` (keep `class`,
`onclick`, and the `<svg>` exactly). Example:
```html
<button class="tab active" onclick="switchTab('add-app', this)" aria-label="Add" title="Add">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5.5v13M5.5 12h13"/></svg>
</button>
```
Labels to use, in order: Add, Apps, QR, Source, Import, Health.

### 2. `.tab` → centered icon + rounded highlight; delete the `.tab-label` rule
```css
.tab {
    flex: 1 1 0; min-width: 0;
    display: flex; align-items: center; justify-content: center;
    border: none; background: none; cursor: pointer;
    padding: 10px 0;
    border-radius: 999px;
    color: var(--label-secondary);
    transition: color .15s, background .15s;
}
.tab svg { width: 24px; height: 24px; display: block; }
.tab.active { color: var(--accent); background: var(--fill-hover); }
.tab:hover { color: var(--label); }
```
- Remove `flex-direction: column; gap; font-family; text align` remnants and the
  entire `.tab .tab-label { ... }` line (no labels anymore). The `flex:1` tabs share
  the capsule so each active highlight is a rounded pill behind its icon.

### 3. `.tabbar` → centered compact pill (the "500px inset", responsive)
Add two properties to the existing `.tabbar` rule (keep everything else):
```css
    margin: 0 auto;
    max-width: 460px;
```
So a compact centered capsule on desktop (six icons ≈ comfortable at 460px), and on
narrow screens `max-width` yields to the 12px `left/right` margins (fills the width).

### 4. Round the top-bar elements
`.topbar` → a rounded floating card:
```css
.topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 20px;
    margin: 10px 10px 0;
    border: 1px solid var(--separator);
    border-radius: var(--radius-lg);
    position: sticky; top: 10px; z-index: 5;
    background: var(--surface);
}
```
(Replace `border-bottom` with the full `border`, add `border-radius`, add the
`margin`, and shift sticky `top: 0`→`10px` so it floats with the rounding visible.)

Brand logo — round it more (inline style on the img, line ~642):
`style="border-radius:2px;"` → `style="border-radius:6px;"`.

Sign out — a rounded pill button. Extend `.btn-link.signout`:
```css
.btn-link.signout {
    color: var(--danger);
    border: 1px solid var(--danger);
    border-radius: 999px;
    padding: 6px 14px;
}
```

## Verify
```bash
grep -c 'class="tab-label"' templates/index.html                 # 0 (labels removed)
grep -c 'aria-label="Add"\|aria-label="Health"' templates/index.html   # 2
grep -c 'max-width: 460px' templates/index.html                  # 1
grep -c 'border-radius: 999px' templates/index.html              # 3 (.tabbar, .tab, .signout)
grep -c 'border-radius:6px' templates/index.html                 # 1 (brand logo)
grep -c 'backdrop-filter' templates/index.html                   # 2 (glass kept)
grep -c '<style>\|</style>' templates/index.html                 # 2
grep -c 'switchTab(' templates/index.html                        # 7 (unchanged)
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q            # 236 passed
```

## Done criteria
- [ ] Bottom bar shows only the six SF icons (no text); each button has `aria-label`+`title`; active icon has an orange color + rounded `--fill-hover` highlight.
- [ ] `.tabbar` is a centered compact capsule (`margin:0 auto; max-width:460px`), glass kept, floating.
- [ ] `.topbar` is a rounded floating card; brand logo `border-radius:6px`; "Sign out" is a rounded pill.
- [ ] `switchTab`/hooks/SF-icon SVGs unchanged; `git status --short` shows only `templates/index.html`; suite `236 passed`.

## STOP conditions
- If any test asserts the tab-label text or the flat topbar markup, reconcile or report.

## Maintenance note
- `max-width:460px` and the topbar `margin/top:10px` are the tunable knobs. If six
  icons feel cramped/loose, nudge `max-width`. If content peeks above the floating
  header, the black canvas behind it is intentional (floating look).
- Icons-only drops visible labels — the `aria-label`/`title` keep it accessible and
  give hover tooltips on desktop; keep them if icons are ever reordered/added.
