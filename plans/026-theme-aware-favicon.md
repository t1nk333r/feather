# Plan 026 — Theme-aware favicon

**Written against commit `1e07265`.** Drift check before editing: `templates/index.html` is
1574 lines, `tests/test_routes.py` has 62 `def test_` functions, and lines 4–6 of the
template read exactly:

```html
    <title>AltStore Source Manager</title>
    <link rel="icon" type="image/svg+xml" href="/static/icon.svg">
    <link rel="alternate icon" href="/static/favicon.png">
```

If any of that does not match, re-read the file and report rather than guessing.

**Test command** (the `ADMIN_PASSWORD` prefix is mandatory — the app refuses to import without it):

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q      # 117 passed today
```

---

## Why

Plan 025 added `static/icon.svg` — a red mark on an **opaque white** square. That opacity is
correct and must not change: AltStore-family clients composite source icons onto varying
backgrounds, and a transparent icon designed against white inverts into an unreadable smear
in dark mode.

But the same file is also the browser favicon, and there the white square is a bright block
against dark browser chrome. Both facts are true at once, so the fix is not to change the
icon — it is to ship a second one and let the browser choose.

**Why not a single compromise icon:** the obvious middle ground is the red mark on a dark
slate background. It fails a contrast check — `#d02828` on `#1F2937` is **2.1:1**, far below
what a 16 px mark needs. Do not add that variant.

---

## Scope

**In scope:**

| File | Change |
|---|---|
| `static/icon-dark.svg` | new — white mark on `#1F2937` |
| `static/favicon-dark.png` | new — 64 px render of the above |
| `templates/index.html` | lines 5–6 only |
| `tests/test_routes.py` | two new tests |

**Explicitly out of scope — do not touch:**

- **`static/icon.svg`, `static/icon.png`, `static/icon-1024.png`, `static/icon-512.png`,
  `static/icon-256.png`, `static/favicon.png`.** These are the published source icon. The
  catalog's `iconURL` points at `icon.png`; changing any of them changes what every
  subscribed device displays.
- **`app.py`** — no change of any kind. In particular `class SourceManager` (~lines 390–1265)
  and `normalize_source()`.
- **`data/`**, `compose.yml`, `Dockerfile`, `.dockerignore`. The Dockerfile already does
  `COPY static/ ./static/` and `.dockerignore` already re-admits `!static/`, so new files in
  that directory are covered with no build change. Verify this rather than assuming — see
  Done criteria.
- The `<h1>` header logo at ~line 505. It sits on a white card, where the white-backed icon
  is already correct. Leave it.

---

## Step 1 — Create `static/icon-dark.svg`

Write verbatim. The path data is identical to `static/icon.svg`; only the two `fill` values
differ. Do not reformat, re-indent, or "optimise" the coordinates.

```svg
<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <title>Feather Tinker</title>
  <rect width="1024" height="1024" fill="#1F2937"/>
  <path fill="#FFFFFF" d="M770.4 192.0L773.9 193.7L773.9 205.9L758.3 297.8L628.2 388.0L636.9 388.0L747.9 342.9L747.9 360.2L730.5 424.4L581.4 493.8L590.0 495.5L714.9 460.8L716.7 466.0L702.8 499.0L682.0 537.1L597.0 566.6L532.8 594.4L543.2 596.1L567.5 592.7L652.5 571.8L657.7 573.6L616.1 623.9L532.8 710.6L375.0 745.3L373.2 741.8L555.4 493.8L612.6 410.5L612.6 403.6L541.5 481.6L366.3 693.2L307.3 800.8L253.6 832.0L250.1 826.8L342.0 698.4L343.8 580.5L515.5 353.3L518.9 355.0L522.4 466.0L524.1 478.2L527.6 478.2L539.8 420.9L543.2 315.1L768.7 193.7Z"/>
</svg>
```

**Verify the path is genuinely identical to the light variant** — a divergence here means the
two favicons are subtly different shapes, which is exactly the kind of thing nobody notices
for a year:

```bash
diff <(grep -o 'd="M[^"]*"' static/icon.svg) <(grep -o 'd="M[^"]*"' static/icon-dark.svg) \
  && echo "PATHS IDENTICAL"
```

## Step 2 — Render its PNG fallback

```bash
rsvg-convert -w 64 -h 64 static/icon-dark.svg -o static/favicon-dark.png
magick static/favicon-dark.png -alpha off PNG24:static/favicon-dark.png
identify -format "%f alpha=%A\n" static/favicon-dark.png     # must read alpha=Undefined
```

## Step 3 — Wire the theme-aware links

Replace lines 5–6 of `templates/index.html` with exactly:

```html
    <link rel="icon" type="image/svg+xml" href="/static/icon.svg" media="(prefers-color-scheme: light)">
    <link rel="icon" type="image/svg+xml" href="/static/icon-dark.svg" media="(prefers-color-scheme: dark)">
    <link rel="alternate icon" href="/static/favicon.png">
```

Order matters. The unqualified `alternate icon` stays **last** so that a browser ignoring the
`media` attribute falls back to the light PNG — the current behaviour — rather than to
whichever media-qualified link it happened to parse last.

## Step 4 — Tests

Add to `tests/test_routes.py`, matching the existing `client` fixture and plain-assertion
style of the neighbouring Plan 025 tests (read `test_static_icon_is_served` first and follow
it).

1. `test_static_dark_icon_is_served` — `GET /static/icon-dark.svg` → 200, body contains
   `#1F2937`.
2. `test_index_offers_both_favicon_themes` — `GET /` → 200, and the body contains **both**
   `prefers-color-scheme: light` and `prefers-color-scheme: dark`.

**Prove test 2 discriminates.** Delete the dark `<link>` line from the template, run
`pytest -k favicon`, and confirm it FAILS. Restore, and confirm it passes again. Quote the
failure. A test that passes both with and without the change under test is worthless, and
this has caught real defects on this repo before.

---

## Done criteria — all must hold

```bash
# 1. tests green, 62 -> 64 test functions
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
grep -c "def test_" tests/test_routes.py                    # 64

# 2. both favicon variants exist and are opaque
identify -format "%f alpha=%A\n" static/favicon.png static/favicon-dark.png
# both lines must read alpha=Undefined

# 3. the two SVGs share one path
diff <(grep -o 'd="M[^"]*"' static/icon.svg) <(grep -o 'd="M[^"]*"' static/icon-dark.svg)
# no output

# 4. the published source icon is untouched
git diff --stat -- static/icon.svg static/icon.png static/icon-1024.png \
  static/icon-512.png static/icon-256.png static/favicon.png app.py data/
# no output

# 5. build plumbing already covers the new files -- no change needed, but confirm
docker build -q -t feather-favicon-check . >/dev/null && \
  docker run --rm feather-favicon-check ls static/ | grep -c "icon-dark.svg"   # 1
```

Criterion 5 is the one that proves `.dockerignore`'s `!static/` really does cover new files,
rather than assuming it. Remove the test image afterwards with `docker rmi`.

---

## STOP and report instead of improvising if

- `templates/index.html` is not 1574 lines, or lines 4–6 do not match the block quoted at the
  top. Report the actual content.
- Criterion 3 fails — the paths differ. Do **not** hand-edit coordinates to force them to
  match; report it, because it means Step 1 was not written verbatim.
- Criterion 4 shows any change to the published icon files. That is a scope violation; revert
  it and report.
- `grep -c "def test_"` will not reach 64. Report the actual number rather than adding filler
  tests to hit it.

---

## Maintenance note

There are now two icon variants that must stay in visual sync. Criterion 3 enforces that
mechanically, so a future change to the mark must update both files or the check fails —
which is the intended failure mode.

`media` on `<link rel="icon">` is honoured by current Chrome, Safari and Firefox. Browsers
that ignore it fall back to the trailing `alternate icon` PNG, so the worst case is exactly
today's behaviour. No JavaScript is involved and no request is made for the unused variant.
