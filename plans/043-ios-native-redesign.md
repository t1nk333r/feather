# Plan 043: Re-skin the admin UI as iOS-native (Apple HIG), light + dark — kill the AI-generated look

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report — do not improvise. When done, update
> this plan's status row in `plans/README.md` unless a reviewer dispatched you
> and told you they maintain the index.
>
> **Drift check (run first)**:
> ```bash
> git diff --stat 80b9a50..HEAD -- templates/index.html
> ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # expect: 202 passed
> ```
> If `templates/index.html` changed since `80b9a50`, compare the "Current state"
> line references below against the live file and re-locate by selector name (not
> line number) before editing. Line numbers WILL drift as you edit — always
> match on the CSS selector / string, never a bare line number.

## Status

- **Priority**: P3 (polish / product)
- **Effort**: M–L (one file, but ~490 lines of CSS to rework + a token system + dark mode)
- **Risk**: LOW (pure presentation — no HTML structure, JS, routes, or Python change). The only way to break functionality is to rename/remove a class or `id` the JS depends on, which this plan forbids.
- **Depends on**: none (styles the current UI including the 038 Health tab and 039/040/041 controls already merged)
- **Category**: direction / UX
- **Planned at**: commit `80b9a50`, 2026-08-18
- **Chosen direction** (decided with the maintainer): **iOS-native, Apple Human Interface Guidelines**, **light + dark, system-aware** (`prefers-color-scheme`). This is a deliberate move away from the current "AI-generated" aesthetic.

## Why this matters

The admin UI currently screams "generated": a full-page `linear-gradient(135deg,
#667eea 0%, #764ba2 100%)` purple background, that same indigo used as the accent
**22 times**, gradient-filled buttons, `translateY(-2px)` hover-lifts with glowing
colored shadows, 20px corner radii, and `0 20px 60px rgba(0,0,0,0.3)` drop
shadows. Every color is a raw hex literal repeated inline — there is no token
system, so the palette can't be changed in one place.

This plan replaces all of that with an **iOS-native** look grounded in Apple's
actual system semantic colors (systemBackground, label/secondaryLabel, system
blue `#007AFF`, system red `#FF3B30`, etc.), introduces a **CSS custom-property
token system** so the palette lives in one `:root` block, and adds a **dark
theme** via `@media (prefers-color-scheme: dark)`. The result is cohesive with
the Feather/AltStore iOS world this tool serves, and unmistakably hand-designed.

**No behavior changes.** HTML structure, every `class` and `id`, all JavaScript,
all routes, and all Python stay exactly as they are. This is a re-skin.

## Current state — `templates/index.html`

- Single inline stylesheet, `<style>` … `</style>` at **lines 9–502**. No CSS
  variables anywhere. ~40 unique hardcoded colors; the worst offenders:
  | color | uses | role |
  |---|---|---|
  | `#667eea` | 22 | the indigo accent (headings, tabs, focus, borders, left-borders, spinner) |
  | `#764ba2` | 3 | gradient partner (body/button/secondary gradients) |
  | `#f093fb` / `#f5576c` | 1 / 7 | pink→red danger gradient + inline error text |
  | `#666` / `#333` / `#999` / `#aaa` | many | gray text ramp |
  | `#e0e0e0` / `#eee` / `#f5f5f5` / `#fafafa` / `#f8f9ff` / `#f8f9fa` | many | borders / fills |
  | `#d4edda`+`#155724`, `#f8d7da`+`#721c24` | alerts | success / error tints |
- The **AI tells** to remove outright (not just recolor):
  - `body` full-page purple **gradient** background (line 13)
  - `.container` `border-radius: 20px` + `box-shadow: 0 20px 60px rgba(0,0,0,0.3)` (21, 23)
  - `h1 { color: #667eea; font-size: 2.5em }` — a big **colored** heading (25–29)
  - `.btn` **gradient** fill + `transform: translateY(-2px)` hover **lift** + glow shadow (98, 106, 109–110)
  - `.btn-danger` pink→red **gradient** (116)
  - `.btn-secondary` reuses the **same accent gradient** as primary (215) — secondary must read as secondary
  - `.app-card:hover` lift + colored glow (133–137)
  - `.qr-container img` `border: 5px solid #667eea` (164)
  - purple **left-borders** on `.info-box` (211), `.version-item` (279), `.toast` (361); **dashed purple** `.add-version-section` (301)
  - focus glow `box-shadow: 0 0 0 3px rgba(102,126,234,0.1)` (90)
  - tab tops `border-radius: 8px 8px 0 0` + `#f8f9ff` tinted hover (51, 57, 61)
- **Inline colors outside the `<style>` block** (must also be swept — grep will catch them):
  - line **648**: `<input ... readonly style="background: #f5f5f5;">`
  - lines **1139, 1146, 1151, 1177, 1186, 1190, 1217, 1227, 1244** (Health/Reconcile result messages in JS template strings): `style="color: #666;"` and `style="color: #f5576c;"`
  - line **633**: `<input id="editTintColor" ... placeholder="#667eea">` — the example hex literally shows the AI indigo
- Already present (leverage, don't fight): the favicon `<link>`s (lines 5–7) already switch between `/static/icon.svg` (light) and `/static/icon-dark.svg` (dark) by `prefers-color-scheme` — so a dark theme is already half-anticipated. `body`/inputs already use the `-apple-system` / `SF Mono` font stacks.
- Tabs (lines 512–517): `Manage Apps`, `QR Code`, `Source Info`, `Import from Repo`, `Health`, and `Sign out` (pushed right with inline `margin-left:auto` — keep that). Plain-text labels, **no emoji** — keep it that way (plan 025).

## Commands you will need

Presentation isn't unit-tested, so the gates are: the suite still passes (proves
the template still renders and no JS-referenced `class`/`id` was lost), and greps
prove the AI palette is gone and every color is a token.

| Purpose | Command | Expected |
|---|---|---|
| Baseline | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` | `202 passed` before AND after |
| No AI palette remains | `grep -nE "667eea\|764ba2\|f093fb\|f5576c\|linear-gradient\(135deg" templates/index.html` | **no matches** |
| No raw hex outside the token block | see Step 6 | only inside `:root` / dark `:root` |
| Page renders (light) | `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/test_routes.py -q -k "login or index or root"` | pass (or run the app and load `/`) |

## Scope

**In scope:**
- `templates/index.html` — the `<style>` block (rewrite with a token system + iOS
  palette + dark mode) and the handful of inline/JS color literals listed above.

**Out of scope — do NOT touch:**
- Any HTML **structure**, or any `class`/`id`/`name`/`onclick` attribute value — the
  JS selects on these. You may edit inline `style="…"` **color/background** values,
  and you may add class names, but must not rename or remove existing ones.
- Any `<script>` logic, event handler, or fetch call (you may only change color
  literals inside JS template strings, e.g. `color: #666` → a class or token).
- `app.py`, routes, Python, `scripts/`, tests, `/static/*` assets.
- Adding a CSS framework, web font, or any external/CDN resource (keep it a single
  self-contained inline `<style>`; no network requests).
- Emoji or icon fonts — stay plain-text (plan 025). The existing text glyphs used
  as a search icon / toast icon may keep their characters; just recolor.
- A theme **toggle** button — the theme follows the OS (`prefers-color-scheme`).
  A manual switch is a separate future plan.

## The design system to introduce (paste verbatim)

Put this at the very top of the `<style>` block, immediately after
`* { margin:0; padding:0; box-sizing:border-box; }`. These are authentic Apple
HIG system colors (light + the dark-mode variants Apple actually uses — e.g.
system blue shifts `#007AFF`→`#0A84FF`, red `#FF3B30`→`#FF453A` in dark).

```css
:root {
    color-scheme: light dark;               /* native form controls, scrollbars adapt */

    /* backgrounds */
    --canvas: #f2f2f7;                       /* systemGroupedBackground (light) */
    --surface: #ffffff;                      /* secondarySystemGroupedBackground — cards */
    --surface-2: #f2f2f7;                    /* inset fills */
    --fill: rgba(120,120,128,0.12);          /* systemFill — subtle control fill */
    --fill-hover: rgba(120,120,128,0.20);

    /* text (Apple label ramp) */
    --label: #1c1c1e;
    --label-secondary: rgba(60,60,67,0.60);
    --label-tertiary: rgba(60,60,67,0.30);

    /* hairlines */
    --separator: rgba(60,60,67,0.29);        /* opaque-ish separator */
    --border: rgba(60,60,67,0.18);           /* input/card hairline */

    /* accents (system colors, light) */
    --accent: #007aff;                       /* systemBlue */
    --accent-press: #0062cc;
    --accent-tint: rgba(0,122,255,0.12);     /* tinted-button fill */
    --danger: #ff3b30;                       /* systemRed */
    --danger-press: #d70015;
    --danger-tint: rgba(255,59,48,0.12);
    --success: #34c759;                      /* systemGreen */
    --warning: #ff9500;                      /* systemOrange */

    /* shape & depth (iOS is flat with tight radii + faint shadow) */
    --radius-lg: 12px;                       /* cards, modals, tab panels */
    --radius-md: 10px;                       /* buttons, inputs */
    --radius-sm: 8px;
    --shadow-card: 0 0 0 0.5px var(--border), 0 1px 2px rgba(0,0,0,0.05);
    --shadow-modal: 0 12px 40px rgba(0,0,0,0.18);
    --focus-ring: 0 0 0 3px rgba(0,122,255,0.25);
    --mono: 'SF Mono','Monaco','Inconsolata','Roboto Mono',monospace;
}

@media (prefers-color-scheme: dark) {
    :root {
        --canvas: #000000;                   /* systemGroupedBackground (dark) */
        --surface: #1c1c1e;                  /* secondarySystemGroupedBackground */
        --surface-2: #2c2c2e;
        --fill: rgba(120,120,128,0.24);
        --fill-hover: rgba(120,120,128,0.32);

        --label: #ffffff;
        --label-secondary: rgba(235,235,245,0.60);
        --label-tertiary: rgba(235,235,245,0.30);

        --separator: rgba(84,84,88,0.65);
        --border: rgba(84,84,88,0.50);

        --accent: #0a84ff;                   /* systemBlue (dark) */
        --accent-press: #409cff;
        --accent-tint: rgba(10,132,255,0.24);
        --danger: #ff453a;                   /* systemRed (dark) */
        --danger-press: #ff6961;
        --danger-tint: rgba(255,69,58,0.24);
        --success: #30d158;
        --warning: #ff9f0a;

        --shadow-card: 0 0 0 0.5px var(--border), 0 1px 2px rgba(0,0,0,0.30);
        --shadow-modal: 0 12px 40px rgba(0,0,0,0.60);
        --focus-ring: 0 0 0 3px rgba(10,132,255,0.35);
    }
}
```

**Rule for the rest of the stylesheet:** after adding the block above, replace
**every** remaining raw color in the file with the nearest token via `var(--…)`.
When you finish, the ONLY raw hex/`rgba()` in the file must be inside these two
`:root` blocks. Everything else references a token.

## Component mapping (apply these; they encode the iOS look)

Rewrite each rule to match. Where a value isn't listed, pick the nearest token by
role from the table above.

- `body`: `background: var(--canvas);` — **delete the gradient.** Keep padding.
- `.container`: `background: var(--surface); border-radius: var(--radius-lg); box-shadow: var(--shadow-card);` — no more 20px/heavy shadow.
- `h1`: `color: var(--label); font-size: 1.6em; font-weight: 700; letter-spacing:-0.02em;` — **not** accent-colored, **not** 2.5em.
- `.subtitle`: `color: var(--label-secondary);`
- `.tabs`: keep flex/wrap; `border-bottom: 1px solid var(--separator);`
- `.tab`: `color: var(--label-secondary); border-bottom: 2px solid transparent; border-radius: 0;` (drop the `8px 8px 0 0`). Hover: `color: var(--label); background: transparent;` (drop the `#f8f9ff`).
- `.tab.active`: `color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; background: transparent;`
- `label`: `color: var(--label);`
- `input, textarea, select`: `background: var(--surface); color: var(--label); border: 1px solid var(--border); border-radius: var(--radius-md);`
- `:focus`: `border-color: var(--accent); box-shadow: var(--focus-ring);`
- `.btn` (primary, filled): `background: var(--accent); color: #fff; border-radius: var(--radius-md); font-weight: 600; transition: background .15s, opacity .15s;` — **delete** the gradient, the `translateY` hover-lift, and the glow shadow. Hover: `background: var(--accent-press);`. Active: `opacity: .8;`. Keep `:disabled { opacity:.5 }`.
- `.btn-danger`: `background: var(--danger);` hover `background: var(--danger-press);` (flat, no gradient/glow).
- `.btn-secondary` (tinted, iOS style): `background: var(--accent-tint); color: var(--accent);` hover `background: var(--fill-hover);` — must NOT reuse the primary fill.
- `.app-card`: `background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); box-shadow: var(--shadow-card);`. Hover: keep it calm — `border-color: var(--separator);` only. **No lift, no colored glow.**
- `.app-info h3`: `color: var(--label);` · `.app-info p`, `.version-info`, `.version-item-details`, `.toast-message`: `color: var(--label-secondary);`
- `.app-icon`: `border: 1px solid var(--border);` keep `border-radius:12px`.
- `.qr-container img`: `border: 1px solid var(--border);` (drop the 5px accent border); keep white bg so the QR stays scannable in dark mode — set `background:#fff;` explicitly (a QR needs a light quiet-zone; do not tokenize this one).
- `.source-url`: `background: var(--surface-2); border: 1px solid var(--border); font-family: var(--mono); color: var(--label);`
- `.alert-success`: `background: var(--success-tint? )` → use `background: rgba(52,199,89,0.14); color: var(--label); border: 1px solid rgba(52,199,89,0.30);` (define inline; green tint reads in both themes). `.alert-error`: same pattern with the danger color: `background: var(--danger-tint); color: var(--label); border:1px solid var(--danger);`
- `.info-box`: `background: var(--accent-tint); border-left: 3px solid var(--accent);` (tint, not the old `#e8f4fd`).
- `.parser-options`: `background: var(--surface-2); border: 1px solid var(--border);`
- `.modal-content`: `background: var(--surface); border-radius: var(--radius-lg); box-shadow: var(--shadow-modal);`
- `.modal-header`: `border-bottom: 1px solid var(--separator);` · `.modal-header h2`: `color: var(--label);` (not accent). · `.close`: `color: var(--label-tertiary);` hover `color: var(--label);`
- `.version-list`: `border-top: 1px solid var(--separator);` · `.version-item`: `background: var(--surface-2); border-left: 3px solid var(--accent);`
- `.add-version-section`: `background: var(--surface-2); border: 1px dashed var(--border);` (drop dashed purple).
- `.search-icon`: `color: var(--label-tertiary);`
- `.toast`: `background: var(--surface); box-shadow: var(--shadow-card); border-left: 3px solid var(--accent);` · `.toast.success` → `border-left-color: var(--success);` · `.toast.error` → `var(--danger);` · `.toast.warning` → `var(--warning);` · `.toast-title` → `var(--label)` · `.toast-close` → `var(--label-tertiary)`.
- `.spinner`: `border: 3px solid var(--fill); border-top-color: var(--accent);` · `.btn.loading::after` keep white top-transparent (it's on a filled button).
- `.loading-overlay`: `background: color-mix(in srgb, var(--surface) 88%, transparent);` (so it works in dark too — if you'd rather avoid `color-mix`, use `background: var(--surface); opacity:.9;` on the overlay). · `.skeleton`: `background: linear-gradient(90deg, var(--fill) 25%, var(--fill-hover) 50%, var(--fill) 75%);` (this gradient is a shimmer, allowed — it's not the AI accent gradient).
- Keep all `@keyframes` and the `@media (max-width:768px)` block (retokenize any colors inside, but the shimmer keyframe is fine).

## Inline / JS color sweep (outside `<style>`)

- Line ~648: `style="background: #f5f5f5;"` → `style="background: var(--surface-2);"`.
- Health/Reconcile messages (lines ~1139–1244): replace `style="color: #666;"` → `style="color: var(--label-secondary);"` and `style="color: #f5576c;"` → `style="color: var(--danger);"`. (Simplest: keep them inline `var(--…)`; do not restructure the JS.)
- Line ~633: change the tint-color placeholder from `#667eea` to a neutral example, e.g. `placeholder="#007AFF"`.

## Steps

### Step 1: Baseline → `202 passed`. If not, STOP.

### Step 2: Insert the token blocks (light + dark) at the top of `<style>`.
Verify: `.venv/bin/python -m py_compile app.py` isn't relevant (no Python change) — instead confirm the file still has exactly one `<style>`/`</style>` pair and the block is valid CSS (no unclosed braces): `grep -c "<style>\|</style>" templates/index.html` → `2`.

### Step 3: Rewrite the `<style>` rules per the component mapping. Work top to bottom; after each screenful, keep going — do not stop to test (CSS has no unit test). Delete every gradient/lift/glow/heavy-shadow/big-radius per the AI-tells list.

### Step 4: Sweep the inline/JS colors (Step's "Inline / JS color sweep" list).

### Step 5: Full suite must still be green.
```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q      # expect 202 passed
```
If any route/template test now fails, you removed or renamed a `class`/`id` the
app depends on — revert that specific change. STOP if you can't reconcile it.

### Step 6: Prove the AI palette is gone and colors are tokenized.
```bash
# (a) none of the AI colors/gradient anywhere:
grep -nE "667eea|764ba2|f093fb|f5576c|f8f9ff|linear-gradient\(135deg" templates/index.html   # expect: no output
# (b) the ONLY raw hex/rgba left is inside the :root blocks. List offenders:
#     every hex/rgba line that is NOT a --token definition should be empty:
grep -nE "#[0-9a-fA-F]{3,6}|rgba?\(" templates/index.html | grep -vE "^\s*[0-9]+:\s*--|:root|prefers-color-scheme|var\(" | grep -vE "#fff;?\s*/\* *QR|background:#fff"
```
Allowed exceptions to (b): the token definitions themselves, and the single
explicit `#fff` on `.qr-container img` background (QR quiet-zone). Everything else
must be `var(--…)`. If other raw colors remain, tokenize them.

### Step 7: Eyeball both themes (manual — do this, then describe what you saw in your report).
Run the app and load `/` in a browser in both light and dark OS appearance, OR if
you can't run a browser, at minimum confirm the rendered HTML of `/` and the login
page is 200 and contains `--accent` in the inlined style. Report that you could
not visually verify if that's the case — the human reviewer will.

## Done criteria
- [ ] A `:root` token system (light) + `@media (prefers-color-scheme: dark)` overrides exist at the top of the stylesheet, using Apple HIG system colors.
- [ ] `grep -nE "667eea|764ba2|f093fb|f5576c|linear-gradient\(135deg" templates/index.html` → no matches. No full-page gradient, no gradient buttons, no `translateY` hover-lift, no `0 20px 60px` shadow, no 20px radius, no accent-colored `h1`.
- [ ] Every color in the file is `var(--…)` except the two `:root` blocks and the one QR `#fff`.
- [ ] Full suite still `202 passed`. No `class`/`id`/JS/route/Python changed.
- [ ] `git status --short` shows only `templates/index.html`.

## STOP conditions
- A test starts failing and the only way you can see to fix it is editing JS or a `class`/`id` — STOP; that means a needed hook was touched.
- The dark palette makes an existing text/background pair fall below readable contrast and you can't fix it within the token values given — report it rather than inventing a new structure.
- You find styling that depends on a color set dynamically from Python (a template `{{ }}` expression producing a color) — the tint-color field or similar — STOP and report; don't hardcode over a dynamic value.

## Maintenance notes
- After this lands, changing the whole palette = editing the two `:root` blocks
  only. Future components should use tokens, never raw hex.
- A manual light/dark **toggle** (overriding the OS) is a natural follow-up: add a
  `[data-theme]` attribute on `<html>` and duplicate the dark `:root` under
  `:root[data-theme="dark"]`, plus a small JS toggle — out of scope here.
- `color-scheme: light dark` makes native controls (checkboxes, the file picker,
  scrollbars) adapt automatically — don't override them.
- Reviewer: open `/` in both OS appearances; confirm no purple/gradient survives,
  buttons are flat iOS-filled, cards are grouped-list surfaces with hairlines, and
  the login modal + Health tab + Edit-App modal all read correctly in dark mode.
  Then confirm the 202-test suite still passes (structure/JS untouched).
