# Plan 061: Native-iOS bottom tab bar — SF-Symbols-style icons + SF Pro labels

> UI-only, `templates/index.html` (the `.tabbar` markup + `.tab` CSS). No app/JS
> change. Depends on plan 060 (glass bar) being merged first — same region.
> Drift check: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 236 passed.

## Status
- Priority P3. Effort S. Risk LOW. Planned against the post-060 `main`.

## Why
Make the bottom bar look like a native iPhone tab bar: an **SF-Symbols-style icon
above each label**, labels in **SF Pro** (the `--sans`/`-apple-system` stack, not
the terminal mono), inactive muted, active in the orange accent. SF Symbols/SF Pro
can't be embedded on the web, so use **inline SVG** icons in that visual style
(monoline, rounded) — self-contained, no CDN.

## Current state — `templates/index.html`
- The bottom bar is `<nav class="tabbar">` with six text-only buttons:
  ```html
  <button class="tab active" onclick="switchTab('add-app', this)">Add</button>
  <button class="tab" onclick="switchTab('manage-apps', this)">Apps</button>
  <button class="tab" onclick="switchTab('qr-code', this)">QR</button>
  <button class="tab" onclick="switchTab('source-info', this)">Source</button>
  <button class="tab" onclick="switchTab('import-repo', this)">Import</button>
  <button class="tab" onclick="switchTab('health', this)">Health</button>
  ```
- `.tab` is currently a flat text button using `var(--mono)`, `color: var(--label-secondary)`, `.tab.active { color: var(--accent); }`. `.tabbar` is the glass bar from plan 060 (keep it).
- KEEP every button's `class="tab"` + `onclick="switchTab(...)"`. `switchTab` unchanged.

## The change

### 1. Put an icon + label span inside each `.tab` (keep class/onclick/label text)
Wrap the label in a `<span class="tab-label">` and prepend an SVG. Use these exact
SF-style icons (24×24 viewBox, `fill="none" stroke="currentColor" stroke-width="1.8"
stroke-linecap="round" stroke-linejoin="round"`). Example for the first button;
apply the pattern to all six:
```html
<button class="tab active" onclick="switchTab('add-app', this)">
  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5.5v13M5.5 12h13"/></svg>
  <span class="tab-label">Add</span>
</button>
```
Icons per tab (the `<path>`/shapes to put inside each `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">`):
- **Add** (`plus`): `<path d="M12 5.5v13M5.5 12h13"/>`
- **Apps** (`square.grid.2x2`): `<rect x="4" y="4" width="7" height="7" rx="1.6"/><rect x="13" y="4" width="7" height="7" rx="1.6"/><rect x="4" y="13" width="7" height="7" rx="1.6"/><rect x="13" y="13" width="7" height="7" rx="1.6"/>`
- **QR** (`qrcode`): `<rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><path d="M14 14h2v2M20 14v2M14 20h2M18 18h2v2"/>`
- **Source** (`doc.text`): `<path d="M7 3.5h7l3.5 3.5V20.5H7z"/><path d="M14 3.5V7h3.5"/><path d="M9.5 12h5M9.5 15.5h5"/>`
- **Import** (`square.and.arrow.down`): `<path d="M12 4v10"/><path d="M8.5 10.5 12 14l3.5-3.5"/><path d="M5 15v3.5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V15"/>`
- **Health** (`waveform.path.ecg`): `<path d="M3 12h4l2-5 3.5 11L15 12h6"/>`

### 2. `.tab` CSS — stack icon over label, SF Pro label, native sizing
```css
.tab {
    flex: 1 1 0; min-width: 0;
    display: flex; flex-direction: column; align-items: center; gap: 3px;
    border: none; background: none; cursor: pointer;
    padding: 8px 4px 6px;
    color: var(--label-secondary);
    font-family: var(--sans);      /* SF Pro on iOS, not the terminal mono */
    transition: color .15s;
}
.tab svg { width: 24px; height: 24px; display: block; }
.tab .tab-label { font-size: 10px; line-height: 1; letter-spacing: 0.01em; }
.tab.active { color: var(--accent); }          /* icon (currentColor) + label both go orange */
.tab:hover { color: var(--label); }
```
(Remove the old `font-size:12px`/`text-align:center` bits that conflict; if the
active tab had an `inset 0 2px 0 var(--accent)` top bar from 058, you may drop it —
the colored icon+label is the iOS indicator — or keep it, your call, but the native
look is just the color.)

## Verify
```bash
grep -c 'class="tab-label"' templates/index.html     # 6
grep -c "switchTab(" templates/index.html            # unchanged (6 nav + any others)
grep -c "<style>\|</style>" templates/index.html      # 2
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q # 236 passed
```

## Done criteria
- [ ] Each of the six tabs has an SF-style inline SVG icon above its label; labels use `var(--sans)`; active tab (icon + label) is the orange accent, inactive muted.
- [ ] `class="tab"` + `onclick`/`switchTab` unchanged; the glass `.tabbar` (060) intact.
- [ ] `git status --short` shows only `templates/index.html`; suite `236 passed`.

## Maintenance note
- Icons are `currentColor` stroke, so they inherit the tab's color automatically —
  no per-state icon swaps needed.
- Real SF Symbols can't ship on the web; these SVGs approximate them. If the
  maintainer wants filled (not outline) icons, add `fill="currentColor"` variants.
