# Plan 025 — Embed the Feather Tinker source icon, and remove every emoji from the UI

**Written against commit `f520dad`.** Before editing, confirm drift: `templates/index.html` is
1580 lines, `app.py` is 1650 lines, `tests/test_routes.py` is 1466 lines with 58 `def test_`
functions. If those do not match, re-read the files before trusting any line number below.

**Test command** (the `ADMIN_PASSWORD` prefix is mandatory — the app refuses to import without it):

```bash
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q      # 108 passed today
```

---

## Why

Two unrelated-looking changes that touch the same file, so they ship together.

**1. The catalog has no icon of its own.** `data/source.json` still carries
`iconURL = https://f000.backblazeb2.com/file/rileytestut/ExampleSource/OctoSource.png` —
Riley Testut's *example source* placeholder, shipped with AltStore's documentation. Every
device subscribed to this source displays someone else's demo artwork, fetched from a
third-party host that this deployment does not control and that can disappear at any time.
A real icon now exists (below) and needs somewhere to be served from.

**2. The admin UI is decorated with 21 emoji characters across 17 lines.** They render
inconsistently across platforms, they are the only pictographic elements in an otherwise
plain interface, and one of them sits in a JS lookup table with a `||` fallback that will
misbehave if edited carelessly (see the trap in Step 4). The owner has asked for all of
them gone.

---

## Scope

**In scope — these files only:**

| File | Change |
|---|---|
| `static/icon.svg` | new — the source icon, vector |
| `static/icon.png` | new — 1024px, flattened, no alpha |
| `static/icon-512.png`, `static/icon-256.png` | new — smaller renders |
| `static/favicon.png` | new — 64px |
| `Dockerfile` | one added `COPY` line |
| `.dockerignore` | one added re-admit line |
| `templates/index.html` | favicon link, header logo, 17 emoji removals |
| `tests/test_routes.py` | new tests |

**Explicitly out of scope — do not touch:**

- **`class SourceManager` (`app.py` lines ~390–1265).** This plan needs no change inside it.
  Those methods hold a `threading.Lock` across a 300-second IPA download; nothing here
  belongs in there.
- **`data/source.json`.** Pointing the catalog at the new icon is a one-field operator edit
  through the web UI, recorded at the bottom of this plan. Do not script it, do not
  hand-edit `data/`, and do not add a migration. `data/` holds 1.3 GB of irreplaceable
  binaries and a hand-curated catalog.
- **`normalize_source()` (`app.py:108`).** It is tempting to default `iconURL` there. Do not.
  Its contract is *"never overwriting a value that is already present"*, and the value that
  needs replacing here **is** present — so a `setdefault` would do nothing, and anything
  stronger would break that contract on the `/source.json` request path that every
  subscribed device polls.
- The four routes that must stay unauthenticated: `/source.json`, `/ipas/*`, `/icons/*`,
  `/qr`. This plan adds no decorator to anything.

---

## Step 1 — Create `static/icon.svg`

Write this file **verbatim**. It is the traced, production artwork; do not redraw, reformat,
re-indent the path data, or "optimise" the coordinates.

```svg
<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <title>Feather Tinker</title>
  <rect width="1024" height="1024" fill="#FFFFFF"/>
  <path fill="#d02828" d="M770.4 192.0L773.9 193.7L773.9 205.9L758.3 297.8L628.2 388.0L636.9 388.0L747.9 342.9L747.9 360.2L730.5 424.4L581.4 493.8L590.0 495.5L714.9 460.8L716.7 466.0L702.8 499.0L682.0 537.1L597.0 566.6L532.8 594.4L543.2 596.1L567.5 592.7L652.5 571.8L657.7 573.6L616.1 623.9L532.8 710.6L375.0 745.3L373.2 741.8L555.4 493.8L612.6 410.5L612.6 403.6L541.5 481.6L366.3 693.2L307.3 800.8L253.6 832.0L250.1 826.8L342.0 698.4L343.8 580.5L515.5 353.3L518.9 355.0L522.4 466.0L524.1 478.2L527.6 478.2L539.8 420.9L543.2 315.1L768.7 193.7Z"/>
</svg>
```

The `#d02828` fill matches the catalog's existing `tintColor`, which Feather renders directly
beside this icon. **Do not change the colour.**

The opaque white `<rect>` is deliberate and load-bearing: AltStore-family clients composite
source icons onto varying backgrounds, and an alpha-channel icon designed against white
inverts into an unreadable smear in dark mode. Do not remove it or make it transparent.

**Verify:**
```bash
test -f static/icon.svg && grep -c 'fill="#d02828"' static/icon.svg      # 1
```

## Step 2 — Render the PNGs

`rsvg-convert` and ImageMagick (`magick`) are both present on this machine.

```bash
mkdir -p static
for s in 1024 512 256; do
  rsvg-convert -w $s -h $s -b white static/icon.svg -o static/icon-$s.png
  magick static/icon-$s.png -alpha off PNG24:static/icon-$s.png
done
cp static/icon-1024.png static/icon.png
rsvg-convert -w 64 -h 64 -b white static/icon.svg -o static/favicon.png
magick static/favicon.png -alpha off PNG24:static/favicon.png
```

**Verify — every PNG must be opaque.** This is the whole point of Step 1's white rect, so
check it rather than assuming:
```bash
identify -format "%f %wx%h alpha=%A\n" static/*.png
# every line must read alpha=Undefined. If any says alpha=Blend, the -alpha off
# step did not apply and the icon will render wrong in dark mode.
```

## Step 3 — Make the files reachable inside the container

`app.py:23` is `app = Flask(__name__)`, so Flask already serves `static/` at `/static/`
with no route needed. Two things stop the files reaching the image:

**3a. `Dockerfile`** — add after line 17 (`COPY templates/ ./templates/`):

```dockerfile
COPY static/ ./static/
```

**3b. `.dockerignore`** — this file is **deny-by-default**: it starts with `*` and re-admits
named paths. A new directory the Dockerfile copies is invisible to the build until it is
re-admitted, and you will find out through a failing build. Add under the existing
`!templates/` line:

```
!static/
```

**Verify both:**
```bash
grep -c "^COPY static/ ./static/$" Dockerfile      # 1
grep -c "^!static/$" .dockerignore                 # 1
```

## Step 4 — Remove all 21 emoji from `templates/index.html`

17 lines are affected: **503, 515, 571, 574, 598, 635, 639, 675, 690, 698, 708, 766, 767,
768, 769, 997, 1189**.

For lines **503, 515, 571, 574, 598, 635, 639, 675, 690, 698, 708, 997, 1189** the edit is
mechanical: delete the emoji character and any variation selector (U+FE0F) immediately
following it, then collapse the resulting leading/double space. Example, line 515:

```html
<!-- before -->
<h2 style="margin-bottom: 20px;">➕ Add New App</h2>
<!-- after -->
<h2 style="margin-bottom: 20px;">Add New App</h2>
```

Line 574 is `<span class="search-icon">🔍</span>` — delete the whole `<span>` element, not
just its contents, or an empty span keeps its CSS margin and the search field shifts.

**Lines 766–769 are the trap.** They are a JS lookup consumed at line ~779:

```javascript
const icons = {
    success: '✅',
    error: '❌',
    warning: '⚠️',
    info: 'ℹ️'
};
...
<span class="toast-icon">${icons[type] || icons.info}</span>
```

Emptying the strings is **wrong**: `'' || icons.info` is falsy-chained, so every toast would
fall through to `icons.info` — which is also now `''`, so it happens to render nothing, but
the code is left lying about its own intent and the `<span>` still occupies layout width.

**Correct edit:** delete the entire `const icons = {...};` declaration *and* the
`<span class="toast-icon">...</span>` line from the template literal. Leave the adjacent
`const titles = {...}` object alone — those are real English words, not emoji, and are used
separately.

**Do not** touch line 503's surrounding `<h1>` yet; Step 5 rewrites it.

## Step 5 — Put the icon in the page

**5a.** In `<head>` (after the `<title>` on line 4), add:

```html
<link rel="icon" type="image/svg+xml" href="/static/icon.svg">
<link rel="alternate icon" href="/static/favicon.png">
```

**5b.** Line 503, after Step 4 has removed the rocket, reads
`<h1>AltStore Source Manager</h1>`. Replace it with:

```html
<h1><img src="/static/icon.svg" alt="" width="40" height="40"
         style="vertical-align: -6px; margin-right: 10px; border-radius: 8px;">AltStore Source Manager</h1>
```

`alt=""` is correct and deliberate — the adjacent text already names the thing, so a screen
reader announcing the image too would be redundant noise.

## Step 6 — Tests

Add to `tests/test_routes.py`, following the existing fixture and naming style in that file
(read a neighbouring test first and match it — do not invent a new fixture).

1. `test_static_icon_is_served` — `GET /static/icon.svg` → 200, and the body contains
   `#d02828`.
2. `test_static_favicon_is_served` — `GET /static/favicon.png` → 200.
3. `test_index_references_the_icon` — `GET /` → 200 and the body contains
   `/static/icon.svg`.
4. `test_index_contains_no_emoji` — `GET /` → 200, and the rendered body matches **zero**
   characters of this class:

   ```python
   EMOJI = re.compile("[\U0001F300-\U0001FAFF←-⇿☀-➿"
                      "⬀-⯿️✅❌❤ℹ]")
   ```

   This is the regression guard. Without it the next person adding a button puts an emoji
   straight back.

**Prove test 4 actually discriminates** — do not skip this, and do not take a passing test
as proof on its own:

```bash
# temporarily put one emoji back
sed -i '0,/AltStore Source Manager/s//🚀 AltStore Source Manager/' templates/index.html
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q -k emoji   # MUST FAIL
git checkout templates/index.html   # then re-apply Step 4/5 edits, or stash first
```

If that run passes, the character class is wrong and the test is worthless. Fix it and
repeat until it fails, then restore.

---

## Done criteria — all must hold

```bash
# 1. tests green, and 4 more than the 58 that exist today
ADMIN_PASSWORD=x .venv/bin/python -m pytest tests/ -q
grep -c "def test_" tests/test_routes.py                        # 62

# 2. zero emoji anywhere in the template
.venv/bin/python - <<'PY'
import re
pat = re.compile("[\U0001F300-\U0001FAFF←-⇿☀-➿"
                 "⬀-⯿️✅❌❤ℹ]")
s = open("templates/index.html", encoding="utf-8").read()
print("emoji chars remaining:", len(pat.findall(s)))   # must print 0
PY

# 3. assets exist and are opaque
identify -format "%f alpha=%A\n" static/*.png          # every line alpha=Undefined

# 4. build plumbing
grep -c "^COPY static/ ./static/$" Dockerfile          # 1
grep -c "^!static/$" .dockerignore                     # 1

# 5. the build actually carries the files
docker build -q -t feather-icon-check . >/dev/null && \
  docker run --rm feather-icon-check ls static/        # lists icon.svg + the PNGs
```

Criterion 5 is the one that catches a forgotten `.dockerignore` line, which is otherwise
invisible until deployment.

---

## STOP and report instead of improvising if

- `templates/index.html` is not 1580 lines, or the emoji are not on the 17 listed lines.
  The file has drifted; re-read it and report the actual state rather than guessing.
- The emoji count in done-criterion 2 will not reach 0 because a character sits somewhere
  this plan did not list. **Report the line — do not widen the regex to make the number
  come out right.** The count in this plan may simply be wrong; that has happened before on
  this repo and the correct response was to fix the plan, not the code.
- Any test outside the four new ones changes status. Nothing here should affect them.
- `docker build` fails. Report the error; do not loosen `.dockerignore` beyond the single
  `!static/` line.

---

## Operator task — NOT part of this plan, do not do it

Serving the embedded icon does not make Feather display it as the published source icon.
The catalog's source-level `iconURL` still points at Riley Testut's example artwork and
must be repointed once to the operator-supplied public PNG:

```
iconURL:  https://f002.backblazeb2.com/file/S30000PUBLIC/MEDIA-PUBLIC/feather-tinker-1024.png
```

Use this exact `.png` URL, not the embedded `.svg` or `/static/icon.png`. AltStore-family
client support for SVG source icons is not guaranteed, and this public PNG is the requested
canonical source artwork. Do not change `headerURL`, and do not apply this source-level URL
to the `apps[*].iconURL` fields.

**Correction recorded 2026-08-16:** the current Source Information form does not actually
expose `iconURL`, and `SourceManager.update_source_info()` does not accept it. Plan 028 adds
that missing field and the regression tests. Perform this operator update after Plan 028
lands; do not hand-edit or script a rewrite of `data/source.json`.

---

## Maintenance note

`static/` is now a third thing the Dockerfile copies, and `.dockerignore` is deny-by-default
— so **any future file added under `static/` is already covered** by the `!static/` line, but
a future *new top-level directory* will not be. That is the intended failure mode: a build
that fails loudly beats an image that silently leaks `.env` or the 1.3 GB `data/`.

The emoji regression test guards the template only. If a future change moves UI strings into
`app.py` or a new template, extend `test_index_contains_no_emoji` to cover them.
