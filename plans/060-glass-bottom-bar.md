# Plan 060: Liquid-glass the bottom tab bar only (keep the flat Omarchy theme everywhere else)

> UI-only, one CSS rule (`.tabbar`) in `templates/index.html`. No app/JS change.
> Drift check: `ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q` → 236 passed.

## Status
- Priority P3. Effort XS. Risk LOW. Planned at commit `bd91c17`.

## Why
The maintainer wants the flat Omarchy theme kept, but the **bottom tab bar** made
"liquid glass" — translucent with a background blur, so content shows through it
faintly. Only the `.tabbar` changes; every other surface stays flat/opaque.

## Current state — `templates/index.html`, `.tabbar` rule
```css
.tabbar {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    display: flex;
    background: var(--surface);
    border-top: 1px solid var(--separator);
    padding-bottom: env(safe-area-inset-bottom, 0px);
}
```
(The workspace already scrolls under this fixed bar, so a blur has content to
refract. `color-mix(...)` is already used elsewhere in this file, so it is safe.)

## The change — make ONLY `.tabbar` glass
```css
.tabbar {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 20;
    display: flex;
    background: color-mix(in srgb, var(--surface) 62%, transparent);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    backdrop-filter: saturate(180%) blur(20px);
    border-top: 1px solid var(--separator);
    padding-bottom: env(safe-area-inset-bottom, 0px);
}
```
- Translucent surface (`color-mix` at ~62%) + `backdrop-filter` blur, with the
  `-webkit-` prefix (Safari/iOS). Keep the top hairline, fixed position, and
  safe-area padding. Do NOT add blur/translucency to any other selector.

## Verify
```bash
# .tabbar now has backdrop-filter (both prefixes) and color-mix bg:
grep -c "backdrop-filter" templates/index.html          # >=2 (std + -webkit-, both inside .tabbar)
grep -c "<style>\|</style>" templates/index.html          # 2
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q     # 236 passed
```
Also confirm NO other surface gained `backdrop-filter` (only `.tabbar`).

## Done criteria
- [ ] `.tabbar` is translucent + blurred (both prefixes); all other surfaces unchanged/flat.
- [ ] `git status --short` shows only `templates/index.html`; suite `236 passed`.

## Maintenance note
- If the bar looks too see-through / illegible over busy content, raise the
  `color-mix` percentage toward opaque (62% → 75–85%). That's the knob.
