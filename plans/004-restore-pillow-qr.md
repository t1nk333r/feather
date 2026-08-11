# Plan 004: Restore Pillow so the QR code endpoint works

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather && md5sum requirements.txt
> ```
> Expected: `529118e6cac74383205ed8be97c2fec0  requirements.txt`
> On a mismatch, compare the "Current state" excerpt against the live file
> before proceeding.

## Status

- **Priority**: P1
- **Effort**: S (one line of change; most of this plan is the verification)
- **Risk**: LOW
- **Depends on**: none. **Run this before Plan 003**, which deletes the file the pins are harvested from.
- **Confidence upgraded to HIGH (2026-08-10)**: during Plan 001's review, `pip install -r requirements.txt` completed cleanly into a fresh venv and `import PIL` then raised `ModuleNotFoundError`. Pillow is confirmed absent and is not pulled in transitively. Step 1's container checks are still worth running to observe the actual `/qr` status code, but the missing dependency is no longer in question.
- **Pin revised to `pillow==11.3.0` (2026-08-11)**, after a first execution attempt hit a STOP condition. See "Why not `pillow==10.1.0`" below. The harvested pin is no longer the pin to apply.
- **Category**: bug
- **Planned at**: no VCS — `requirements.txt` md5 `529118e6cac74383205ed8be97c2fec0`, 2026-08-10

## Why this matters

`GET /qr` is very likely returning HTTP 500 in production right now, and the failure is invisible because the exception is swallowed into a generic message.

The QR code is the **primary onboarding path** for this product: it encodes a `feather://` source URL that a user scans to add the catalog to AltStore/Feather on their device. The frontend renders `<img id="qrImage" src="/qr">` unconditionally on every page load, so every visit fires a request that returns 500 and shows a broken image.

The cause is a dependency that was present in the older copy of the app and dropped in the rewrite. `backup.old/requirements.txt` pins `qrcode[pil]==7.4.2` **and** `pillow==10.1.0`; the live `requirements.txt` pins bare `qrcode==7.4.2` and no Pillow at all. The base image `python:3.11-slim` ships no Pillow, and nothing else in `requirements.txt` pulls it transitively.

Without PIL, `qrcode.make_image()` falls back to the pure-Python `PyPNGImage` factory, whose method signature is `save(self, stream, kind=None)`. The call site passes `format='PNG'` → `TypeError` → caught → HTTP 500.

**This is the one audit finding rated MED confidence** because it was not reproduced inside a container. Steps 1–2 confirm it before you change anything.

## Why not `pillow==10.1.0` — read this before Step 3

The pin harvested from `backup.old/requirements.txt` is `pillow==10.1.0`. **Do not use it.** A first execution attempt of this plan stopped here, correctly.

`10.1.0` installs fine in the container (`python:3.11-slim` — a `cp311` manylinux wheel exists) but **cannot install on this host**, which runs Python **3.14.6**. No `cp314` wheel was ever published for it, so pip falls back to building from sdist and the build fails against modern setuptools:

```
File "<string>", line 27, in get_version
KeyError: '__version__'
ERROR: Failed to build 'pillow' when getting requirements to build wheel
```

That matters beyond this plan: **Plan 005 runs its test suite in a host virtualenv** built from `requirements.txt`. A container-only pin would make Plan 005 unrunnable on the recorded known-good path, and Plans 006–010 all depend on Plan 005.

Wheel availability was checked directly (`pip download --no-deps --only-binary=:all: --platform manylinux_2_28_x86_64` for each `--python-version`):

| Pillow | cp311 (container) | cp314 (host) |
|---|---|---|
| 10.1.0 | yes | **no** |
| 11.0.0 | yes | **no** |
| 11.1.0 | yes | **no** |
| 11.2.1 | yes | **no** |
| **11.3.0** | **yes** | **yes** |
| 12.0.0 | yes | yes |
| 12.3.0 | yes | yes |

`11.3.0` is the **earliest** version with wheels for both interpreters, so it is the smallest bump the constraint allows. Both `11.3.0` and `12.3.0` were functionally exercised against the exact code path at `app.py:2301-2307` — `qrcode[pil]==7.4.2`, `QRCode(...).make_image(...).save(buf, format='PNG')` — and both produced a byte-identical 777-byte PNG via the `PilImage` factory. `11.3.0` is chosen over `12.3.0` deliberately: the app never decodes untrusted images (`grep` confirms the only PIL consumer is `qrcode`; `app.py` has no `from PIL` import and stores uploaded icons as opaque bytes), so there is no CVE-exposure argument for chasing latest, and a general dependency refresh is a separate deferred change.

## Current state

`requirements.txt` in full, as it exists today (8 lines, note line 3):

```
Flask==2.3.3
Flask-Limiter==3.5.0
qrcode==7.4.2
requests==2.31.0
atomicwrites==1.4.1
Werkzeug==2.3.7
altparse==0.3.0
```

`backup.old/requirements.txt` — the source of the pins to restore (**Plan 003 deletes this file**, so harvest it here first):

```
flask==3.0.0
altparse==0.3.0
qrcode[pil]==7.4.2
pillow==10.1.0
requests==2.31.0
```

The call site, `app.py:2279-2298`:

```python
@app.route('/qr')
def generate_qr():
    try:
        # Use feather:// URL scheme for QR code
        source_url = request.url_root + 'source.json'
        feather_url = source_url.replace('https://', 'feather://').replace('http://', 'feather://')

        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(feather_url)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')          # <-- line 2292: the failing call
        buf.seek(0)

        return send_file(buf, mimetype='image/png')
    except Exception as e:
        logging.error(f"QR generation error: {str(e)}")
        return jsonify({"error": "QR generation failed"}), 500
```

The frontend consumer, `app.py:1395` (inside the embedded `HTML_TEMPLATE` string):

```html
<img id="qrImage" src="/qr">
```

Historical evidence that this route breaks silently and repeatedly — `data/app.log`:

```
2025-12-31 14:53:18,527 - ERROR - QR generation error: name 'io' is not defined
2025-12-31 14:53:18,527 - INFO - <proxy-ip> - - "GET /qr HTTP/1.1" 500 -
```

That was a *different* bug (a missing import, since fixed — `io` is imported at `app.py:6`), but it demonstrates that `/qr` has failed in production before and was only noticed by tailing a log file.

**Repo conventions**: `requirements.txt` uses exact `==` pins, one per line, no comments, mixed capitalisation of package names (`Flask`, `Werkzeug` capitalised; `qrcode`, `requests`, `altparse` lowercase). Match the existing style; do not reformat the file.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Is PIL in the container | `docker compose exec altstore-manager python -c "import PIL; print(PIL.__version__)"` | see Step 1 |
| Hit the endpoint | `curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://localhost:7000/qr` | see Step 1 |
| Container logs | `docker compose logs --tail 30 altstore-manager` | see Step 1 |
| Rebuild | `docker compose up --build -d` | exit 0 |

## Scope

**In scope** (the only file you may modify):
- `requirements.txt` — line 3, plus one added line

**Out of scope** (do NOT touch):
- `app.py` — in particular, **do not "fix" this by removing the `format='PNG'` keyword at line 2292.** That would technically make the pure-Python fallback work, but it silently changes which image backend renders the QR and leaves the build non-reproducible. Restore the declared dependency instead.
- `Dockerfile` — it already runs `pip install --no-cache-dir -r requirements.txt` at line 14; no change needed.
- `backup.old/` — read it in Step 3, do not modify it. Plan 003 deletes it.
- Any other pin in `requirements.txt`. In particular **do not** bump `Flask==2.3.3` to `3.0.0` just because `backup.old` had it — that is a separate, deferred change that needs Plan 005's tests first.

## Git workflow

- Branch: `advisor/004-restore-pillow` (or the default branch if the repo has only one).
- Single commit.

## Steps

### Step 1: Confirm the bug before changing anything

Make sure the container is running:
```
cd /home/t1nk33r/Documents/feather
docker compose up -d
sleep 10
```

Check whether Pillow is present **inside the container** (not on the host — the host has Pillow installed, which is exactly why this bug is invisible to local experimentation):

```
docker compose exec altstore-manager python -c "import PIL; print(PIL.__version__)"
```

**Expected today**: `ModuleNotFoundError: No module named 'PIL'`

Now hit the endpoint:
```
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:7000/qr
```

**Expected today**: `500`

And confirm the error text:
```
docker compose logs --tail 30 altstore-manager | grep "QR generation error"
```
**Expected today**: a line mentioning an unexpected keyword argument `format`.

**Record what you actually observed.** Three outcomes:

- **PIL missing and `/qr` returns 500** → the finding is confirmed. Proceed to Step 2.
- **PIL missing but `/qr` returns 200** → the fallback works differently than expected. Still proceed (pinning explicitly makes the build reproducible), but say so in your report and in the commit message.
- **PIL is present and `/qr` returns 200** → Pillow is arriving transitively from some other package. Still proceed — an undeclared transitive dependency is exactly what breaks on the next rebuild — but note it clearly in your report.

### Step 2: Harvest the pins

```
cat backup.old/requirements.txt
```

Confirm it shows `qrcode[pil]==7.4.2` and `pillow==10.1.0`. This step exists to capture the `qrcode[pil]` extra, which is what you carry forward.

**The Pillow version is *not* carried forward.** Use `pillow==11.3.0`, for the reasons in "Why not `pillow==10.1.0`" above. If `backup.old/` no longer exists (Plan 003 ran first), nothing is lost — both pins to apply are stated verbatim in Step 3.

**Verify**: you have the two exact pin strings recorded: `qrcode[pil]==7.4.2` and `pillow==11.3.0`.

### Step 3: Apply the change

Edit `requirements.txt`: change line 3 from `qrcode==7.4.2` to `qrcode[pil]==7.4.2`, and add an explicit `pillow` pin.

Result should be exactly:

```
Flask==2.3.3
Flask-Limiter==3.5.0
qrcode[pil]==7.4.2
requests==2.31.0
atomicwrites==1.4.1
Werkzeug==2.3.7
altparse==0.3.0
pillow==11.3.0
```

Both the extra *and* the explicit pin are deliberate: the extra expresses the real requirement, the pin makes the build reproducible.

**Verify**: `grep -c "qrcode\[pil\]==7.4.2\|pillow==11.3.0" requirements.txt` → `2`

### Step 3b: Confirm it installs on the host before touching Docker

This is the check the first attempt failed on. Do it before the rebuild, because it is fast and it is the one Plan 005 depends on.

```
python3 -m venv /tmp/p004-host
/tmp/p004-host/bin/pip install -q -r requirements.txt
/tmp/p004-host/bin/python -c "import PIL; print(PIL.__version__)"
```

→ prints `11.3.0`, exit 0. (Note: `python3 -m venv -q` is **not** a valid flag — omit `-q` on the venv line.)

Then exercise the real code path, not just the import:

```
/tmp/p004-host/bin/python -c "
import io, qrcode
qr = qrcode.QRCode(version=1, box_size=10, border=5)
qr.add_data('feather://example/source.json')
qr.make(fit=True)
buf = io.BytesIO()
qr.make_image(fill_color='black', back_color='white').save(buf, format='PNG')
assert buf.getvalue()[:8] == b'\x89PNG\r\n\x1a\n', 'not a PNG'
print('OK', len(buf.getvalue()), 'bytes')
"
```

→ prints `OK` and a byte count, exit 0. A failure here means the `format='PNG'` call still is not going through the PIL backend — **STOP and report**.

Clean up: `rm -rf /tmp/p004-host`

### Step 4: Rebuild and confirm the fix

```
docker compose up --build -d
sleep 15
```

**Verify PIL is now present**:
```
docker compose exec altstore-manager python -c "import PIL; print(PIL.__version__)"
```
→ prints `11.3.0`, exit 0

**Verify the endpoint**:
```
curl -s -o /tmp/qr.png -w "%{http_code} %{content_type}\n" http://localhost:7000/qr
```
→ `200 image/png`

**Verify the file is a real PNG**:
```
file /tmp/qr.png
```
→ output contains `PNG image data`

**Verify no error was logged**:
```
docker compose logs --tail 30 altstore-manager | grep -c "QR generation error"
```
→ `0`

Clean up: `rm /tmp/qr.png`

### Step 5: Confirm the QR encodes the right thing

Open `http://localhost:7000/` in a browser and confirm the QR image renders rather than showing a broken-image icon.

If you can decode it (optional), the payload should be a `feather://` URL ending in `/source.json`. Note: the host portion is derived from the request's `Host` header — that is a separate known bug, fixed by Plan 008. Do not fix it here.

### Step 6: Commit

```
git add requirements.txt
git commit -m "fix: restore Pillow so /qr stops returning 500

requirements.txt pinned bare qrcode==7.4.2 with no Pillow, while
backup.old/requirements.txt had qrcode[pil]==7.4.2 + pillow==10.1.0 —
the dependency was dropped in a rewrite. Without PIL, make_image()
returns PyPNGImage, whose save(stream, kind=None) rejects the
format='PNG' keyword used at app.py:2292.

Pinned 11.3.0 rather than the harvested 10.1.0: no cp314 wheel exists
for 10.1.0, so it cannot install on the host that runs the test suite
(Python 3.14). 11.3.0 is the earliest release with wheels for both
cp311 (the container) and cp314, and renders a byte-identical PNG.

/qr is the primary onboarding path and the frontend requests it on
every page load."
```

## Test plan

If Plan 005 has already run, add this case to `tests/test_routes.py` (it is already listed in that plan's coverage):

```python
def test_qr_returns_png(client):
    resp = client.get('/qr')
    assert resp.status_code == 200
    assert resp.content_type == 'image/png'
    assert resp.data[:8] == b'\x89PNG\r\n\x1a\n'   # PNG magic bytes
```

The magic-byte assertion matters more than the status code: it is what catches a regression to a different image backend.

If Plan 005 has not run yet, the Step 4 `curl` + `file` checks are the verification, and Plan 005 will add the permanent test.

## Done criteria

ALL must hold:

- [ ] `grep -c "qrcode\[pil\]" requirements.txt` returns `1`
- [ ] `grep -c "^pillow==11.3.0$" requirements.txt` returns `1`
- [ ] `grep -c "pillow==10.1.0" requirements.txt` returns `0`
- [ ] Step 3b passed: a fresh host venv installs `requirements.txt` and renders a real PNG through the PIL backend
- [ ] `docker compose exec altstore-manager python -c "import PIL"` exits 0
- [ ] `curl -s -o /dev/null -w "%{http_code}" http://localhost:7000/qr` returns `200`
- [ ] the downloaded response starts with PNG magic bytes
- [ ] `docker compose logs` shows no `QR generation error` after the rebuild
- [ ] `app.py` is unmodified (`git status --short` shows only `requirements.txt`)
- [ ] `plans/README.md` status row updated, including which of the three Step 1 outcomes you observed
- [ ] If Plan 005 has run: the new `/qr` test passes

## STOP conditions

Stop and report back (do not improvise) if:

- `pillow==11.3.0` fails to install anywhere — host or container. A `cp311` manylinux wheel and a `cp314` wheel were both confirmed to exist on PyPI, so a failure means pip is resolving from sdist for some other reason (no network, an index override, a `--no-binary` setting). **Do not add `gcc`/`libjpeg-dev` to the Dockerfile on your own initiative, and do not change the pin on your own initiative** — report what pip printed.
- `/qr` still returns 500 after the rebuild with PIL present. The cause is then something other than the missing dependency; capture the full traceback from `docker compose logs` and report.
- You find yourself editing `app.py:2292`. That is explicitly out of scope — report instead.
- The rebuild breaks any other endpoint (`/source.json` stops returning 8 apps).

## Maintenance notes

- **Why both `qrcode[pil]` and an explicit `pillow` pin**: the extra declares intent, the pin makes the build reproducible. A reviewer might flag this as redundant — it is deliberate, keep both.
- This bug existed because there is no test asserting `/qr` returns an image, and the exception handler at `app.py:2296-2298` converts any failure into a generic `"QR generation failed"` message. Plan 005's test is the permanent guard. The broader "errors are swallowed" pattern is Plan 007's subject.
- **The pin must stay installable on both interpreters.** The container is `python:3.11-slim`; the host that runs Plan 005's tests is Python 3.14. Any future bump has to keep wheels for both. `11.3.0` is the earliest version that does; `12.x` also works if a later refresh wants it. Verify with `pip download --no-deps --only-binary=:all: --python-version 311 --platform manylinux_2_28_x86_64 pillow==<v>` and again with `--python-version 314` before changing it.
- The commit message in Step 6 says `pillow==10.1.0` was what `backup.old` had — that is accurate history and should stay. Just make sure the message also notes the applied pin is `11.3.0` and why.
- **Reviewer should scrutinise**: that `app.py` is untouched. The tempting one-line "fix" (dropping `format='PNG'`) would mask the missing dependency and leave the build non-reproducible.
