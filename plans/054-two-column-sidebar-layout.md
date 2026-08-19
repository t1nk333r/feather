# Plan 054: Two-column layout — left nav sidebar + right workspace (replace the top tab bar)

> **Executor instructions**: Follow this plan step by step. This is a UI-only
> change to `templates/index.html` (HTML restructure + CSS). Do NOT change
> `app.py`, any server code, or the JavaScript logic. Run the verification gates.
> If a STOP condition occurs, stop and report. Update this plan's status row in
> `plans/README.md` when done unless a reviewer maintains the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 7fdffd6..HEAD -- templates/index.html tests/
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 232 passed
> ```
> If the tabs/container markup or `switchTab` changed since `7fdffd6`, compare the
> "Current state" excerpts against the live file first; on a mismatch, STOP.

## Status

- **Priority**: P3 (UX / layout)
- **Effort**: M
- **Risk**: LOW–MED (layout restructure; all JS hooks preserved, no logic change)
- **Depends on**: plan 043 (the iOS token system it styles with) — merged
- **Category**: UI / layout
- **Planned at**: commit `7fdffd6`, 2026-08-19

## Why this matters

The admin UI is a single centered card with a **horizontal tab bar** across the
top; with 6 sections + Sign out it's cramped and doesn't scale. The maintainer
wants a **two-column dashboard layout**: a **left nav sidebar** and a **right
workspace** that shows the active section —

```
+--------+---------------------------+
|  nav   |        workspace          |
+--------+---------------------------+
```

This is a pure presentation change: the same nav buttons move into a vertical
sidebar, the same `.tab-content` panels render in the workspace column, and the
existing `switchTab()` keeps working untouched.

## Current state — `templates/index.html`

- Body structure (from line ~593):
  ```html
  <div class="container">
      <h1><img src="/static/icon.svg" ...>AltStore Source Manager</h1>
      <p class="subtitle">Manage your custom iOS app repository</p>
      <div class="tabs">
          <button class="tab active" onclick="switchTab('add-app', this)">Add App</button>
          <button class="tab" onclick="switchTab('manage-apps', this)">Manage Apps</button>
          <button class="tab" onclick="switchTab('qr-code', this)">QR Code</button>
          <button class="tab" onclick="switchTab('source-info', this)">Source Info</button>
          <button class="tab" onclick="switchTab('import-repo', this)">Import from Repo</button>
          <button class="tab" onclick="switchTab('health', this)">Health</button>
          <button class="tab" onclick="logout()" style="margin-left: auto;">Sign out</button>
      </div>
      <!-- the six panels -->
      <div id="add-app" class="tab-content active"> ... </div>
      <div id="manage-apps" class="tab-content"> ... </div>
      <div id="qr-code" class="tab-content"> ... </div>
      <div id="source-info" class="tab-content"> ... </div>
      <div id="import-repo" class="tab-content"> ... </div>
      <div id="health" class="tab-content"> ... </div>
      <!-- modals + toast container follow (position:fixed) -->
      <div id="loginModal" class="modal"> ... </div>
      <div id="editAppModal" class="modal"> ... </div>
      ...
  </div>
  ```
- CSS (from ~line 85), iOS tokens (plan 043): `.container` is a centered card
  (`max-width:1200px; margin:0 auto; background:var(--surface); border-radius:var(--radius-lg); padding:40px; box-shadow:var(--shadow-card)`).
  `.tabs` is `display:flex; gap:10px; border-bottom:1px solid var(--separator); flex-wrap:wrap`.
  `.tab` is a horizontal item with a `border-bottom:2px solid transparent`; `.tab.active` uses `border-bottom-color:var(--accent)` (an underline).
  `.tab-content { display:none }` / `.tab-content.active { display:block }`.
  A mobile block exists: `@media (max-width:768px) { .tabs { flex-direction:column } ... }`.
- `switchTab(tabName, clickedTab)` (JS ~1050): removes `.active` from all `.tab`
  and `.tab-content`, adds it to the clicked button + the panel with id `tabName`,
  and lazy-loads for `manage-apps`/`source-info`/`import-repo`. **It selects on
  `.tab` and panel ids — keep both, and it needs NO change.**
- Tokens available: `--surface`, `--canvas`, `--label`, `--label-secondary`,
  `--accent`, `--accent-tint`, `--fill`, `--separator`, `--radius-lg`, `--radius-md`.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `232 passed` before AND after |
| Style block intact | `grep -c "<style>\|</style>" templates/index.html` | `2` |
| Hooks preserved | `grep -c "class=\"tab\b\|class=\"tab \|switchTab(" templates/index.html` | unchanged nav buttons still present |
| New layout present | `grep -nE "\.sidebar\b\|\.workspace\b" templates/index.html` | matches present |

## Scope

**In scope:** `templates/index.html` — restructure the header/tabs/panels region
into a two-column grid and restyle the nav as a vertical sidebar. CSS + the small
HTML wrapper change only.

**Out of scope:**
- `app.py` / any server code; the `switchTab` JS logic (leave it as-is — it already
  works with the sidebar because it targets `.tab` + panel ids); any panel's inner
  content; the modals' and toast's own markup/JS.
- Renaming/removing any `class="tab"`, panel `id`, `onclick`, or other JS-referenced
  hook. You may ADD wrapper elements/classes; you must not break existing selectors.
- A collapsible/hamburger sidebar with JS state — keep it CSS-only responsive
  (a JS toggle is a separate future plan).

## Steps

### Step 1: Baseline → `232 passed`. If not, STOP.

### Step 2: Restructure the markup
Wrap the nav in a `<aside class="sidebar">` and the six panels in a
`<main class="workspace">`, both inside `.container`:
```html
<div class="container">
    <aside class="sidebar">
        <div class="brand">
            <img src="/static/icon.svg" alt="" width="28" height="28" style="border-radius:6px;">
            <span>AltStore Source Manager</span>
        </div>
        <nav class="tabs">
            <button class="tab active" onclick="switchTab('add-app', this)">Add App</button>
            <button class="tab" onclick="switchTab('manage-apps', this)">Manage Apps</button>
            <button class="tab" onclick="switchTab('qr-code', this)">QR Code</button>
            <button class="tab" onclick="switchTab('source-info', this)">Source Info</button>
            <button class="tab" onclick="switchTab('import-repo', this)">Import from Repo</button>
            <button class="tab" onclick="switchTab('health', this)">Health</button>
        </nav>
        <button class="tab signout" onclick="logout()">Sign out</button>
    </aside>
    <main class="workspace">
        <!-- MOVE the six <div ... class="tab-content"> panels here, UNCHANGED -->
    </main>
    <!-- MODALS + TOAST CONTAINER: leave them here (direct children of .container),
         after </main>. They are position:fixed so the grid takes them out of flow. -->
</div>
```
- Keep every nav button's `class="tab"`, `onclick`, and label exactly as-is (drop
  only the inline `style="margin-left:auto"` on Sign out — it moves out of `.tabs`).
- The `<h1>`/`.subtitle` are replaced by the sidebar `.brand`. If any test asserts
  the old subtitle text, restore a minimal brand text instead (see STOP conditions).
- Move the six panels verbatim into `.workspace`. Do NOT edit their contents.
- Leave `#loginModal`, `#editAppModal`, and the toast container where they are
  (after `</main>`, inside `.container`) — they're `position:fixed`, out of flow.

### Step 3: CSS — the grid + sidebar
Update `.container` and add the new rules (replace the `.tabs`/`.tab` horizontal
styles with the vertical sidebar styles; delete the old `border-bottom` underline
bits on `.tab`/`.tab.active`):
```css
.container {
    max-width: 1100px;
    margin: 24px auto;
    background: var(--surface);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-card);
    display: grid;
    grid-template-columns: 240px minmax(0, 1fr);
    overflow: hidden;              /* keep the rounded corners over the grid */
    min-height: 600px;
    padding: 0;                    /* padding moves into sidebar/workspace */
}
.sidebar {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 20px 12px;
    border-right: 1px solid var(--separator);
    background: var(--canvas);     /* subtle contrast vs the workspace surface */
}
.brand {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 12px 16px;
    font-weight: 700; font-size: 1.0em; color: var(--label);
    letter-spacing: -0.01em;
}
.tabs {                            /* now the vertical nav list */
    display: flex; flex-direction: column; gap: 4px;
    border-bottom: none;           /* remove the old underline rule */
}
.tab {
    width: 100%;
    text-align: left;
    padding: 10px 12px;
    border: none; background: none;
    font-size: 15px; color: var(--label-secondary);
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: background .15s, color .15s;
}
.tab:hover { background: var(--fill); color: var(--label); }
.tab.active {
    background: var(--accent-tint);
    color: var(--accent);
    font-weight: 600;
}
.tab.signout { margin-top: auto; color: var(--danger); }  /* pinned to the bottom */
.tab.signout:hover { background: var(--danger-tint); color: var(--danger); }
.workspace {
    padding: 32px 36px;
    min-width: 0;                  /* allow inner content (tables/cards) to shrink */
}
```
(Remove the now-obsolete `.tab { border-bottom: 2px solid transparent }` and
`.tab.active { border-bottom-color: ... }` declarations if they exist separately.)

### Step 4: Responsive — stack on mobile
Replace the mobile `.tabs` rule so the layout collapses to one column with a
horizontal, scrollable nav on top:
```css
@media (max-width: 768px) {
    .container { grid-template-columns: 1fr; margin: 10px; min-height: 0; }
    .sidebar { border-right: none; border-bottom: 1px solid var(--separator); }
    .tabs { flex-direction: row; overflow-x: auto; }
    .tab { width: auto; white-space: nowrap; }
    .tab.signout { margin-top: 0; }
    .workspace { padding: 20px; }
}
```
(Keep any other existing rules inside the mobile block, e.g. `.app-header`.)

**Verify**: `grep -c "<style>\|</style>" templates/index.html` → `2`.

### Step 5: Full suite unchanged → `232 passed`
No DOM hook changed, so the count and pass/fail must be identical. If a test
fails, you renamed/removed a hook or removed asserted text — fix that specific
thing (see STOP conditions), don't work around it.

## Done criteria
- [ ] The UI is a two-column grid: a left `.sidebar` (brand + vertical `.tabs` nav + Sign out pinned bottom) and a right `.workspace` holding the six `.tab-content` panels.
- [ ] Every nav button keeps `class="tab"` + `onclick="switchTab(...)"`; every panel keeps its `id`; `switchTab` is unchanged and still switches sections; the active nav item is highlighted (accent-tint pill).
- [ ] Modals/toast still work (they're `position:fixed`, unaffected by the grid).
- [ ] Responsive: under 768px it collapses to one column with a horizontal nav.
- [ ] `grep -c "<style>\|</style>"` → `2`; full suite `232 passed`; `git status --short` shows only `templates/index.html`.

## STOP conditions
- A test asserts specific old-layout text/markup (e.g. the `.subtitle` string, or the horizontal `.tabs` structure) — restore just enough (keep the brand/app-name text) to satisfy it; if the assertion is fundamentally about horizontal tabs, report it rather than gutting the test.
- Moving the panels into `.workspace` would require editing a panel's inner markup/JS to keep it working — STOP; the panels must move verbatim.
- The modals break when left inside the grid `.container` — move them to be direct children of `<body>` after `</div>` (still `position:fixed`), and report that you did.

## Manual verification (do this, report what you saw)
Run the app (`DATA_DIR=/tmp/feather-data ADMIN_PASSWORD=x python app.py`), log in,
and confirm: the sidebar shows all six sections + Sign out; clicking each swaps the
workspace content; the active item is highlighted; it reads correctly in **light and
dark**; and at a narrow width it collapses to a single column. If you can't run a
browser, say so — a human reviewer will eyeball it.

## Maintenance notes
- New sections are added the same way: a `<button class="tab" onclick="switchTab('x', this)">` in the sidebar `<nav class="tabs">` + a `<div id="x" class="tab-content">` in `.workspace`. `switchTab` needs no change unless the new tab lazy-loads.
- If a collapsible sidebar (hamburger) is wanted later, that's a JS-state follow-up on top of this CSS structure.
- Reviewer: confirm no JS/hook/panel-content changed (suite count identical), the grid holds in both themes, and the modals still overlay correctly.
