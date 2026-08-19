# Plan 055: Mobile-first iOS bottom tab bar (replace the sidebar) + fix color contrast

> **Executor instructions**: UI-only change to `templates/index.html` (layout +
> CSS tokens). Do NOT change `app.py`, server code, or the `switchTab` JS logic.
> Run the verification gates. If a STOP condition occurs, stop and report. Update
> this plan's status row in `plans/README.md` when done unless a reviewer
> maintains the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 2223576..HEAD -- templates/index.html tests/
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 232 passed
> ```
> If the layout/tokens changed since `2223576`, compare the "Current state"
> excerpts against the live file first; on a mismatch, STOP.

## Status

- **Priority**: P2 (the current sidebar reads as a desktop pattern on a phone; the UI is used on mobile)
- **Effort**: M
- **Risk**: LOW–MED (layout restructure; all JS hooks preserved, no logic change)
- **Depends on**: plan 043 (tokens), plan 054 (the sidebar this replaces) — merged
- **Category**: UI / mobile UX / accessibility
- **Planned at**: commit `2223576`, 2026-08-19
- **Decided with the maintainer**: this UI is used on **mobile**, so replace the
  left **sidebar** (plan 054) with a **native-iOS-style bottom tab bar**
  (thumb-reachable, pairs with the PWA install in plan 056). Also **fix the color
  contrast** — several tokens are too faint.

## Why this matters

The two-column sidebar (plan 054) is a desktop pattern; on a phone it's cramped
and wrong. A **bottom tab bar** is the native iOS navigation pattern — thumb
reachable, familiar, and ideal for an installed PWA. Separately, several palette
tokens fail readable-contrast: `--label-tertiary` (~0.30 alpha) is nearly
invisible on the surface, and `--label-secondary` is borderline for body text.
This plan reshapes the nav to a bottom bar and bumps the faint tokens to meet
WCAG AA (4.5:1 for text, 3:1 for large/UI), while staying close to the Apple look.

## Current state — `templates/index.html`

- Layout (from plan 054): `<div class="container">` is a CSS **grid**
  (`grid-template-columns: 240px minmax(0,1fr)`) with `<aside class="sidebar">`
  (a `.brand`, a vertical `<nav class="tabs">` of `.tab` buttons, and a
  `.tab.signout` pinned bottom) and `<main class="workspace">` holding the six
  `.tab-content` panels; the modals + `#toastContainer` follow. Key CSS: `.container`
  (grid), `.sidebar`, `.brand`, `.tabs` (`flex-direction:column`), `.tab` (vertical
  pill: `width:100%; text-align:left; border-radius:var(--radius-md); .tab.active {background:var(--accent-tint); color:var(--accent)}`), `.tab.signout {margin-top:auto; color:var(--danger)}`, `.workspace {padding:32px 36px}`, and a `@media (max-width:768px)` block that collapses the grid to one column.
- `switchTab(tabName, clickedTab)` (JS) toggles `.active` on `.tab` and the panel
  with id `tabName`, and lazy-loads a few tabs. **It selects on `.tab` + panel ids
  — keep both; it needs NO change.**
- Nav buttons: `Add App` (`add-app`), `Manage Apps` (`manage-apps`), `QR Code`
  (`qr-code`), `Source Info` (`source-info`), `Import from Repo` (`import-repo`),
  `Health` (`health`), plus `Sign out` (`logout()`).
- **Tokens** (`:root` at the top of `<style>`, plan 043) — the contrast offenders:
  ```css
  --label-secondary: rgba(60,60,67,0.60);   /* borderline as body text on --surface */
  --label-tertiary:  rgba(60,60,67,0.30);   /* ~1.9:1 on white — fails; used on .close, .search-icon, .toast-close */
  --accent: #007aff;                         /* ~3.9:1 on white — ok for UI/large, borderline for small text */
  ```
  and the dark-mode overrides (`@media (prefers-color-scheme: dark)` / `:root`):
  `--label-secondary: rgba(235,235,245,0.60)`, `--label-tertiary: rgba(235,235,245,0.30)`.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `232 passed` before AND after |
| Style block intact | `grep -c "<style>\|</style>" templates/index.html` | `2` |
| Bar present / sidebar gone | `grep -c "class=\"tabbar\"\|class=\"sidebar\"" templates/index.html` | tabbar present, sidebar 0 |
| Hooks preserved | `grep -c "class=\"tab\b\|class=\"tab \|switchTab(" templates/index.html` | nav buttons still present |

## Scope

**In scope:** `templates/index.html` — replace the sidebar/grid layout with a
bottom-tab-bar layout, and adjust the faint contrast tokens in `:root` (light +
dark).

**Out of scope:** `app.py`/server; the `switchTab` JS logic (unchanged — it works
with the bar); any panel's inner content; renaming/removing any `class="tab"`,
panel `id`, or `onclick`. PWA/manifest (that's plan 056). A JS-driven tab overflow
menu (keep all six visible; CSS only).

## Steps

### Step 1: Baseline → `232 passed`. If not, STOP.

### Step 2: Restructure to a top header + scrolling workspace + fixed bottom bar
Target structure inside `.container`:
```html
<div class="container">
    <header class="topbar">
        <div class="brand"><img src="/static/icon.svg" width="24" height="24" style="border-radius:6px;"> <span>Source Manager</span></div>
        <button class="btn-link signout" onclick="logout()">Sign out</button>
    </header>
    <main class="workspace">
        <!-- the six .tab-content panels, VERBATIM -->
    </main>
    <nav class="tabbar">
        <button class="tab active" onclick="switchTab('add-app', this)">Add</button>
        <button class="tab" onclick="switchTab('manage-apps', this)">Apps</button>
        <button class="tab" onclick="switchTab('qr-code', this)">QR</button>
        <button class="tab" onclick="switchTab('source-info', this)">Source</button>
        <button class="tab" onclick="switchTab('import-repo', this)">Import</button>
        <button class="tab" onclick="switchTab('health', this)">Health</button>
    </nav>
    <!-- modals + #toastContainer stay after, position:fixed -->
</div>
```
- Keep every nav button's `class="tab"` + `onclick="switchTab(...)"`. **Shorten the
  visible labels** to fit a phone bottom bar (Add / Apps / QR / Source / Import /
  Health) — the label text is display-only, not a JS hook, so shortening is safe.
- Move `Sign out` into the `.topbar` (a text button), out of the tab list.
- Move the six panels into `.workspace` VERBATIM (no inner edits).
- Modals/toast stay after `</nav>` (fixed).

### Step 3: CSS — bottom bar layout
Replace the `.container` grid + `.sidebar`/`.workspace`/`.tab` sidebar styles with:
```css
.container {
    max-width: 900px;
    margin: 0 auto;
    min-height: 100vh;
    background: var(--surface);
    display: flex;
    flex-direction: column;
    box-shadow: var(--shadow-card);
}
.topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 20px;
    border-bottom: 1px solid var(--separator);
    position: sticky; top: 0; background: var(--surface); z-index: 5;
}
.brand { display:flex; align-items:center; gap:8px; font-weight:700; color:var(--label); }
.btn-link { border:none; background:none; cursor:pointer; font-size:15px; color:var(--accent); padding:6px 8px; }
.btn-link.signout { color: var(--danger); }
.workspace {
    flex: 1 1 auto;
    padding: 24px 20px calc(72px + env(safe-area-inset-bottom, 0px));  /* clear the bar */
    overflow-x: hidden;
}
.tabbar {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    display: flex;
    background: var(--surface);
    border-top: 1px solid var(--separator);
    padding-bottom: env(safe-area-inset-bottom, 0px);   /* iPhone home indicator */
}
.tab {
    flex: 1 1 0; min-width: 0;
    border: none; background: none; cursor: pointer;
    padding: 10px 4px; font-size: 12px;
    color: var(--label-secondary);
    text-align: center;
    transition: color .15s;
}
.tab.active { color: var(--accent); font-weight: 600; }
.tab:hover { color: var(--label); }
```
Delete the obsolete sidebar rules (`.sidebar`, `.tab.signout` pill, the vertical
`.tabs` column, the grid `grid-template-columns`, and the old `.tab` pill styles).
Keep `.tab-content { display:none } / .tab-content.active { display:block }`.
The `#toastContainer` should sit above the bar — if toasts overlap the tabbar, give
the toast container `bottom: calc(80px + env(safe-area-inset-bottom,0px))` on
mobile (only if it currently anchors to the bottom).

### Step 4: Responsive — center on wide screens
On desktop the bar can stay bottom (iPad-style) but the container is centered
(`max-width:900px`). Update/replace the old `@media (max-width:768px)` block as
needed (e.g. full-width container, tighter workspace padding). The bar is fixed
full-width on all sizes; that's fine.

### Step 5: Contrast fixes (the `:root` tokens)
Bump the faint tokens to meet WCAG AA. Suggested values (verify they read well in
both themes; keep them Apple-ish):
- Light: `--label-secondary: rgba(60,60,67,0.72);` (≈4.5:1 on `--surface`),
  `--label-tertiary: rgba(60,60,67,0.52);` (readable, still muted).
- Dark: `--label-secondary: rgba(235,235,245,0.70);`, `--label-tertiary: rgba(235,235,245,0.50);`.
- Leave `--accent` as-is for UI/active use (it's ≥3:1). If any **small body text**
  uses `--accent`, that's for a later pass — don't restyle content here.
Do NOT change the background/surface tokens or the accent hue — only lift the two
label tokens' alpha in both `:root` blocks.

**Verify**: `grep -c "<style>\|</style>"` → `2`.

### Step 6: Full suite unchanged → `232 passed`. A changed count/failure means a hook or asserted text broke — fix that specific thing.

## Done criteria
- [ ] Navigation is a **fixed bottom tab bar** (six tabs), not a sidebar; `.sidebar`/grid removed; a slim top header holds the brand + Sign out.
- [ ] Every nav button keeps `class="tab"` + `onclick="switchTab(...)"`; panels keep their ids; `switchTab` unchanged; the workspace scrolls and clears the bar (safe-area-inset respected).
- [ ] `--label-secondary` / `--label-tertiary` lifted to readable contrast in light AND dark; accent hue/backgrounds unchanged.
- [ ] `grep -c "<style>\|</style>"` → `2`; suite `232 passed`; `git status --short` shows only `templates/index.html`.

## STOP conditions
- A test asserts the sidebar/old-layout markup — reconcile (keep the brand/app-name text); report if it's fundamentally about the sidebar.
- Moving panels into `.workspace` would require editing their inner markup — STOP; move them verbatim.
- The bottom bar overlaps modal action buttons (modals are full-screen `position:fixed`, so they should sit above `z-index:20` — give `.modal` a higher z-index if needed) — adjust z-index rather than restructuring.

## Manual verification (do this, report what you saw)
Run the app, log in, and confirm: the bottom bar switches sections; the active tab
is accent-colored; content scrolls without hiding behind the bar; it reads well in
**light and dark**; on a narrow (phone) width it looks native; Sign out works from
the header. If no browser, exercise `GET /` via the test client and say so.

## Maintenance notes
- This reverts plan 054's sidebar in favor of a mobile-first bar; both build on the
  043 tokens. New sections = a `.tab` in `.tabbar` + a `#id` panel in `.workspace`.
- `env(safe-area-inset-bottom)` handles the iPhone home indicator — keep it when the PWA (plan 056) runs in standalone.
- Reviewer: confirm no JS/hook/panel-content changed (suite count identical), the
  bar is thumb-reachable and doesn't cover content, and the lifted tokens are legible in both themes.
