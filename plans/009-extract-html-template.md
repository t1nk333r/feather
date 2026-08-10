# Plan 009: Extract the embedded HTML template to `templates/index.html`

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> sed -n '721p;2215p' app.py
> grep -c "{{\|{%" app.py
> ```
> Line 721 must be `HTML_TEMPLATE = '''`, line 2215 must be `'''`, and the
> Jinja-syntax count must be `0`. **If that count is not 0, STOP** — the whole
> premise of this plan is that the template contains no Jinja.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW–MED (mechanical move; the risk is the Dockerfile step, not the code)
- **Depends on**: `plans/005-smoke-test-suite.md` (its `GET /` test is the safety net)
- **Blocks**: `plans/010-login-session-auth.md`, which edits this markup
- **Category**: tech-debt
- **Planned at**: no VCS at authoring time — `app.py` md5 `2d17cee45698fa4f062cd9b4114e20d0`, 2026-08-10

## Why this matters

1,493 of `app.py`'s 2,531 lines — **59% of the file** — are a single Python string literal holding the entire frontend: HTML, ~490 lines of CSS, and ~770 lines of JavaScript.

The concrete cost: no syntax highlighting, no HTML/CSS/JS linting, no formatter, no language server, no browser-devtools "edit and save back to source". The editor sees one enormous string. Frontend CSS and `SourceManager` business logic share the same merge surface. Quotes inside the template are permanently constrained by the `'''` delimiter.

**This extraction is unusually cheap, and that is the reason to do it now**: `grep -c "{{\|{%" app.py` returns `0`. The template contains **zero Jinja interpolation**. It is 100% static markup; all dynamic content arrives client-side via `fetch()` calls to the `/api/*` routes, using JavaScript template literals (`${...}`), which Jinja ignores. So this is a byte-for-byte move, not a rewrite — no escaping to audit, no `{{ }}` to reconcile.

Plan 010 adds a login modal to this markup. Doing that in a real `.html` file rather than inside a Python string is materially easier and less error-prone, which is why this is sequenced first.

## Current state

**The template boundaries**, verified:

- Line 721: `HTML_TEMPLATE = '''`
- Line 722: `<!DOCTYPE html>` — first line of content
- Line 2214: `</html>` — last line of content
- Line 2215: `'''` — closing delimiter

**Content to move: lines 722–2214 inclusive (1,493 lines).**

Internal structure:

| Region | Lines | Size |
|---|---|---|
| `<style>` … `</style>` | 727–1220 | ~494 |
| `<body>` | 1222– | |
| `<script>` … `</script>` | 1441–2212 | ~772 |
| `</body>` `</html>` | 2213–2214 | 2 |

`app.py:719-726` as it exists:

```python
source_manager = SourceManager(SOURCE_FILE)

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>AltStore Source Manager</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
```

`app.py:2213-2220` — the closing delimiter and the sole consumer:

```python
</body>
</html>
'''

# Routes
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)
```

**`app.py:1`** — the import to change:

```python
from flask import Flask, render_template_string, request, jsonify, send_file
```

`render_template_string` has exactly one use, at line 2220.

**`Dockerfile:7-16`** — note it copies only `app.py`; there is no `templates/` directory today:

```dockerfile
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
```

Flask's default template folder is `templates/` relative to the application root, so no `template_folder` configuration is needed — just the directory and the `COPY`.

**Repo conventions**: none for frontend (there is no frontend tooling). Preserve the existing markup exactly — indentation, formatting, everything. This plan moves bytes; it does not improve them.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `.venv/bin/python -m pytest tests/ -q` | all pass |
| Syntax | `python3 -m py_compile app.py` | exit 0 |
| Line count check | `wc -l templates/index.html` | `1493` |
| Byte comparison | `diff <(...) templates/index.html` | no output — see Step 2 |
| Container | `docker compose up --build -d` | exit 0 |

## Scope

**In scope**:
- `app.py` — line 1 (import), lines 721–2215 (delete the string), line 2220 (the call)
- `templates/index.html` (create)
- `Dockerfile` — add one `COPY` line
- `tests/test_routes.py` — strengthen the `GET /` assertion

**Out of scope** (do NOT touch):
- **The template's contents.** Do not reformat, reindent, minify, "clean up", fix HTML validation warnings, or change a single character of CSS or JS. This is a move. Any content change makes the byte-comparison verification in Step 2 useless, which is the only real safety net you have for 1,493 lines.
- Splitting `<style>` and `<script>` into `static/`. A reasonable follow-up, explicitly deferred — see Maintenance notes.
- The escaping bug at `app.py:1727` (an unescaped `${...}` interpolation). **Plan 010 fixes it**, after this move. Do not fix it here; it would contaminate the byte comparison.
- Any `SourceManager` method or `/api/*` route.

## Git workflow

- Branch: `advisor/009-extract-template`
- **One commit.** Git will show it as a large deletion plus a large addition; the commit message should say it is a verbatim move so a reviewer knows not to read all 1,493 lines.

## Steps

### Step 1: Capture the original for comparison

Before touching anything, extract the exact bytes to a reference file:

```
cd /home/t1nk33r/Documents/feather
sed -n '722,2214p' app.py > /tmp/template-original.html
wc -l /tmp/template-original.html
```
**Verify**: `1493`

Confirm the boundaries are right:
```
head -1 /tmp/template-original.html   # <!DOCTYPE html>
tail -1 /tmp/template-original.html   # </html>
```

Also snapshot the rendered page from the running app, to compare against later:
```
docker compose up -d && sleep 10
curl -s http://localhost:7000/ > /tmp/page-before.html
wc -c /tmp/page-before.html
```

### Step 2: Create `templates/index.html`

```
mkdir -p templates
cp /tmp/template-original.html templates/index.html
```

**Verify — this is the important one**:
```
diff /tmp/template-original.html templates/index.html && echo IDENTICAL
```
→ `IDENTICAL`

```
wc -l templates/index.html
```
→ `1493`

### Step 3: Remove the string literal from `app.py`

Delete lines 721 through 2215 inclusive — the `HTML_TEMPLATE = '''` line, all 1,493 content lines, and the closing `'''`.

After deletion, `app.py:719-722` should read:

```python
source_manager = SourceManager(SOURCE_FILE)

# Routes
@app.route('/')
```

**Verify**:
```
grep -c "HTML_TEMPLATE" app.py
```
→ `1` (only the remaining use at what was line 2220 — fixed in Step 4)

```
wc -l app.py
```
→ `1036` (2531 − 1495)

```
python3 -m py_compile app.py
```
→ this will **fail** until Step 4, because `HTML_TEMPLATE` is now undefined. That is expected; it is a `NameError` at runtime rather than a syntax error, so `py_compile` may actually pass. Either way, proceed to Step 4 immediately — do not leave the tree in this state.

### Step 4: Switch to `render_template`

Change the import on `app.py:1`:

```python
from flask import Flask, render_template, request, jsonify, send_file
```

(`render_template_string` → `render_template`. Leave the other imports alone.)

Change the route:

```python
@app.route('/')
def index():
    return render_template('index.html')
```

**Verify**:
```
grep -c "render_template_string" app.py
```
→ `0`

```
grep -c "HTML_TEMPLATE" app.py
```
→ `0`

```
python3 -m py_compile app.py
```
→ exit 0

### Step 5: Add the `COPY` to the Dockerfile

**This is the step that is easy to forget and produces a failure only inside the container**: `jinja2.exceptions.TemplateNotFound: index.html`, at request time, with the host working perfectly.

In `Dockerfile`, after the existing `COPY app.py .` (line 16), add:

```dockerfile
COPY templates/ ./templates/
```

**Verify**: `grep -c "COPY templates/" Dockerfile` → `1`

### Step 6: Verify rendering is byte-identical

Host first:
```
.venv/bin/python -m pytest tests/ -q
```
→ all pass, including the Plan 005 `GET /` test

Then the container — the real check:
```
docker compose up --build -d
sleep 15
curl -s http://localhost:7000/ > /tmp/page-after.html
diff /tmp/page-before.html /tmp/page-after.html && echo IDENTICAL
```
→ `IDENTICAL`

If `diff` reports a difference, inspect it before proceeding. A trailing-newline difference is acceptable and explainable (`sed`/`cp` newline handling); **any difference in markup, CSS, or JS is not** — investigate.

### Step 7: Confirm the page actually works in a browser

Automated checks confirm bytes, not behaviour. Open `http://localhost:7000/` and confirm:
- the app list loads (this exercises `fetch('/api/apps')`)
- the QR image renders
- clicking "Edit" on an app opens the edit modal
- no errors in the browser console

`render_template` runs the file through Jinja2, which `render_template_string` also did — so behaviour should be unchanged. But this is the first time the file is parsed as a *file*, and it is worth 60 seconds to confirm.

### Step 8: Commit

```
git add app.py templates/ Dockerfile tests/
git commit -m "refactor: extract HTML_TEMPLATE to templates/index.html

Verbatim move of app.py lines 722-2214 (1493 lines, 59% of the file)
into templates/index.html. No content changes — the file is byte-identical
to the extracted string, verified by diff.

Safe because the template contains zero Jinja syntax (no {{ or {%);
it is fully static markup with a client-rendered frontend, so this is a
move rather than a port. app.py drops from 2531 to 1036 lines.

Adds COPY templates/ to the Dockerfile — without it the container
raises TemplateNotFound at request time."
```

## Test plan

Strengthen the existing `GET /` test in `tests/test_routes.py` rather than adding many new ones:

1. **`test_index_renders`** — already exists from Plan 005. Confirm it asserts on a stable marker string. Good choices: the `<title>AltStore Source Manager</title>` text, or the element id `qrImage`. Avoid asserting on anything Plan 010 will legitimately change.
2. **`test_index_contains_no_jinja_artifacts`** — assert the rendered body contains neither `{{` nor `{%`. If a future edit introduces Jinja syntax into the JavaScript, Jinja will try to interpret it; this catches that class of breakage early.
3. **`test_index_is_substantial`** — assert `len(response.data) > 40000`. Crude but effective: it catches a `TemplateNotFound` that somehow returns a short error page, and an accidentally-truncated template.

Verification: `.venv/bin/python -m pytest tests/ -q` → all pass.

## Done criteria

ALL must hold:

- [ ] `templates/index.html` exists, is 1,493 lines, and is byte-identical to `sed -n '722,2214p'` of the original `app.py`
- [ ] `wc -l app.py` returns `1036`
- [ ] `grep -c "HTML_TEMPLATE" app.py` returns `0`
- [ ] `grep -c "render_template_string" app.py` returns `0`
- [ ] `grep -c "COPY templates/" Dockerfile` returns `1`
- [ ] `python3 -m py_compile app.py` exits 0
- [ ] `.venv/bin/python -m pytest tests/ -q` exits 0
- [ ] `diff /tmp/page-before.html /tmp/page-after.html` shows no markup differences
- [ ] The page loads in a browser with a working app list, QR image, and edit modal
- [ ] `git status --short` shows only `app.py`, `Dockerfile`, `templates/index.html`, `tests/test_routes.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- `grep -c "{{\|{%" app.py` returns anything other than `0` in the drift check. Jinja would then interpret those sequences and the move is no longer byte-safe — it becomes a port, which is a different (larger) plan.
- The line boundaries do not match (721 is not `HTML_TEMPLATE = '''`, 2215 is not `'''`). Re-derive them with `grep -n "HTML_TEMPLATE\|^'''" app.py` and report before cutting.
- `diff /tmp/template-original.html templates/index.html` shows any difference at any point.
- `diff /tmp/page-before.html /tmp/page-after.html` shows differences beyond trailing whitespace.
- The container raises `TemplateNotFound`. That means Step 5 was missed or the `COPY` path is wrong. Fix the `COPY`; **do not** work around it by setting `template_folder` to an absolute path.
- You are tempted to reformat, reindent, or fix anything inside the template. Out of scope — report it as a follow-up.

## Maintenance notes

- **Jinja2 now parses this file.** A future edit that introduces `{{`, `{%`, or `{#` into the JavaScript — for instance a JS object literal written with unlucky spacing — will break rendering with a template syntax error. Test 2 in the test plan is the guard. Anyone adding JS to this file should know it passes through Jinja.
- **The `COPY templates/` line in the Dockerfile is load-bearing and easy to lose** in a future Dockerfile cleanup (the file already contains 30 lines of commented-out dead config, which someone will eventually prune). Its absence fails only at request time, only in the container.
- **Deferred follow-up**: extracting `<style>` (lines 727–1220 of the original) to `static/style.css` and `<script>` (1441–2212) to `static/app.js` would cut `templates/index.html` to roughly 220 lines and give CSS and JS real tooling. It is another S-sized change with the same byte-for-byte character, and it needs the same `COPY static/` addition. Worth doing after Plan 010 settles, so the login modal lands in one place rather than two.
- **Reviewer should scrutinise**: the Dockerfile `COPY` line and the `diff` output proving the move was verbatim. Do not read all 1,493 moved lines — verify the diff evidence instead.
