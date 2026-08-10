# Plan 006: Make catalog writes atomic and serialized

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> ```
> cd /home/t1nk33r/Documents/feather
> sed -n '238,246p' app.py
> grep -c "def save_source" app.py
> ```
> The excerpt must match "Current state" below and the count must be `1`.
> Also confirm Plan 005 landed: `test -d tests && .venv/bin/python -m pytest tests/ -q`
> must pass before you change anything.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: `plans/005-smoke-test-suite.md` (mandatory — you need a regression net)
- **Category**: bug
- **Planned at**: no VCS at authoring time — `app.py` md5 `2d17cee45698fa4f062cd9b4114e20d0`, 2026-08-10

## Why this matters

`data/source.json` **is the product**: 8 hand-curated apps that every subscribed iOS device polls. It is not in version control (`.gitignore` excludes `data/`), and `data/backups/` exists on disk but is **empty because no code has ever written to it** — `grep -c "backups" app.py` returns `0`.

`save_source` opens that file with mode `'w'`, which **truncates it to zero bytes before writing anything**. Any exception, container kill, OOM, or full disk during `json.dump` leaves a truncated file. `load_source` then returns `None`, and every route degrades to `"Failed to load source data"` — while `/source.json` keeps serving the corrupt file to clients.

The danger window is real and wide: `/api/add-app` and `/api/add-version` call `save_source` *after* a `requests.get(..., timeout=300)` download, and `compose.yml` caps container memory at 4096m.

Separately, **eight** methods do read → mutate → write with no lock, under Flask's default `threaded=True`. Two concurrent mutations silently lose one of the two writes — the classic read-modify-write race. On `/api/add-version` that means a published version quietly disappears.

`requirements.txt:5` declares `atomicwrites==1.4.1` and `grep -c atomicwrites app.py` returns `0`. The correct fix was chosen, added to the manifest, and never implemented.

## Current state

**`app.py:229-246`** — the load/save pair:

```python
    def load_source(self):
        """Load source data from JSON file"""
        try:
            with open(self.source_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error loading source: {str(e)}")
            return None

    def save_source(self, source_data):
        """Save source data to JSON file"""
        try:
            with open(self.source_file, 'w') as f:
                json.dump(source_data, f, indent=2)
            return True
        except Exception as e:
            logging.error(f"Error saving source: {str(e)}")
            return False
```

**`app.py:44-47`** — the constructor you will add the lock to:

```python
    def __init__(self, source_file):
        self.source_file = source_file
        self.ensure_data_directory()
        self.initialize_source()
```

**The eight mutating methods.** Each does `load_source()` → mutate in memory → `save_source()`. Verified line numbers:

| Method | Defined at | `save_source` call at |
|---|---|---|
| `add_app_manual` | 256 | 326 |
| `add_app_from_github` | 328 | 370 |
| `add_app_from_altsource` | 376 | 468 |
| `delete_app` | 484 | 509 |
| `update_app` | 525 | 572 |
| `add_version` | 575 | 632 |
| `update_version` | 635 | 702 |
| `update_source_info` | 705 | 715 |

Representative shape — `app.py:705-716`, the smallest of the eight:

```python
    def update_source_info(self, data):
        """Update source metadata"""
        source_data = self.load_source()
        if not source_data:
            return False, "Failed to load source data"

        for key in ['name', 'subtitle', 'description', 'website', 'tintColor']:
            if key in data and data[key]:
                source_data[key] = data[key]

        success = self.save_source(source_data)
        return success, "Source information updated successfully" if success else "Failed to update source information"
```

**Already-available imports** — `app.py:1-13` already imports `os`, `json`, `tempfile`, `shutil`, `logging`, and `datetime`. `tempfile` is imported at line 8 and currently **unused**, so you need it for nothing new. You will need to add `threading`.

**`data/backups/`** exists and is empty. It was clearly intended for exactly this.

**Repo conventions**: methods return `(bool, message)` tuples. Errors are logged with `logging.error(f"...: {str(e)}")` and swallowed into a falsy return. Keep both conventions — this plan does not change the error-reporting contract (Plan 007 does).

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `.venv/bin/python -m pytest tests/ -q` | all pass |
| Syntax | `python3 -m py_compile app.py` | exit 0 |
| Catalog validity | `python3 -c "import json; print(len(json.load(open('data/source.json'))['apps']))"` | `8` |
| Container | `docker compose up --build -d` | exit 0 |

(If Plan 005 recorded that tests run inside the container rather than a host venv, use that command instead — check `plans/README.md`.)

## Scope

**In scope** (the only file you may modify, plus tests):
- `app.py` — `SourceManager.__init__` (44–47), `save_source` (238–246), and the eight mutating methods listed above
- `tests/test_routes.py` — add the cases in "Test plan"
- `requirements.txt` — remove the unused `atomicwrites` pin (Step 5)

**Out of scope** (do NOT touch):
- `load_source` — reads are fine as they are. Do not add locking to reads; it is unnecessary and risks deadlock with the write lock.
- Any `@app.route` handler. This plan works entirely inside `SourceManager`.
- The `(bool, message)` return contract and the "silent failure" behaviour — **Plan 007 owns that.** If you notice that `add_app_manual` reports success when a download failed, leave it. Fixing it here creates a merge conflict with Plan 007 and muddies both diffs.
- `HTML_TEMPLATE` and anything frontend.
- `data/` contents — do not hand-edit the catalog.

## Git workflow

- Branch: `advisor/006-atomic-writes`
- Commit per step is fine; one commit for the whole plan is also fine.

## Steps

### Step 1: Add the lock

Add `import threading` to the imports at the top of `app.py`, then add a lock to the constructor:

```python
    def __init__(self, source_file):
        self.source_file = source_file
        self._lock = threading.Lock()
        self.ensure_data_directory()
        self.initialize_source()
```

**Verify**: `python3 -m py_compile app.py` → exit 0, and `grep -c "import threading" app.py` → `1`

### Step 2: Rewrite `save_source` to write atomically

Replace the body of `save_source` (`app.py:238-246`). The pattern:

1. Write the JSON to a temporary file **in the same directory as `self.source_file`**. This is not optional — `os.replace` is only atomic within a single filesystem, and `data/` is a bind mount, so a temp file in `/tmp` would be on a different device.
2. `flush()` and `os.fsync()` the temp file before closing, so the bytes are actually on disk before the rename.
3. `os.replace(tmp_path, self.source_file)` — atomic on POSIX. A reader either sees the whole old file or the whole new one, never a truncated one.
4. On any exception, remove the temp file so failures don't litter `data/`.

Target shape:

```python
    def save_source(self, source_data):
        """Save source data to JSON file atomically.

        Writes to a temp file in the same directory then os.replace()s it
        into position, so a crash mid-write can never truncate the live
        catalog. A timestamped copy of the previous catalog is kept in
        data/backups/.
        """
        tmp_path = None
        try:
            self._backup_source()
            directory = os.path.dirname(self.source_file) or "."
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".source-", suffix=".json.tmp")
            with os.fdopen(fd, 'w') as f:
                json.dump(source_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.source_file)
            return True
        except Exception as e:
            logging.error(f"Error saving source: {str(e)}")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False
```

Keep the `return True` / `return False` contract exactly — eight call sites depend on it.

**Verify**: `python3 -m py_compile app.py` → exit 0

### Step 3: Add the backup helper

Add a `_backup_source` method to `SourceManager`. It copies the *current* `source.json` to `data/backups/source-<UTC timestamp>.json` before it gets replaced, and prunes to the most recent 20.

- Use `datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")` for the timestamp (`datetime` is already imported).
- If `source.json` does not exist yet (first run), do nothing and return quietly.
- Backup failure must **never** block the save — wrap it so an error is logged and swallowed. A failed backup is a nuisance; a failed save is data loss.
- Create `data/backups/` if missing. Add it alongside the other `os.makedirs` calls in `ensure_data_directory` (`app.py:49-55`) so it exists from startup.

**Verify**: `grep -c "backups" app.py` → at least `2` (it was `0` before)

### Step 4: Serialize the eight mutating methods

For each of the eight methods in the table above, wrap the load → mutate → save body in `with self._lock:`.

The mechanical transformation, using `update_source_info` as the example:

```python
    def update_source_info(self, data):
        """Update source metadata"""
        with self._lock:
            source_data = self.load_source()
            if not source_data:
                return False, "Failed to load source data"

            for key in ['name', 'subtitle', 'description', 'website', 'tintColor']:
                if key in data and data[key]:
                    source_data[key] = data[key]

            success = self.save_source(source_data)
            return success, "Source information updated successfully" if success else "Failed to update source information"
```

Rules:
- The lock must be acquired **before** `load_source()` and released **after** `save_source()`. Locking only the save is useless — the race is in the read-modify-write cycle, not the write.
- `threading.Lock` is **not reentrant**. If any of these methods calls another one that also takes the lock, you get a deadlock. Check before you wrap: `grep -n "self\.\(add_app\|delete_app\|update_app\|add_version\|update_version\|update_source_info\|add_app_from\)" app.py`. If any method calls another locked method, **STOP and report** — do not silently switch to `RLock` without saying so.
- Long-running network I/O inside the lock is acceptable here. `add_app_manual` and `add_version` download IPAs (up to a 300 s timeout) while holding it, which serialises concurrent uploads. That is a deliberate trade: correctness over throughput, on a single-user admin tool. Note it in the commit message.

**Verify**: `grep -c "with self._lock:" app.py` → `8`

### Step 5: Remove the unused `atomicwrites` dependency

The implementation above uses `tempfile` + `os.replace`, which needs no third-party package. `atomicwrites==1.4.1` at `requirements.txt:5` has never been imported and the package is unmaintained. Remove that line.

**Verify**: `grep -c "atomicwrites" requirements.txt app.py` → `0` for both

### Step 6: Verify nothing regressed

```
.venv/bin/python -m pytest tests/ -q
```
→ all pass, unchanged from before this plan

```
docker compose up --build -d
sleep 15
curl -f http://localhost:7000/source.json | python3 -c "import json,sys; print(len(json.load(sys.stdin)['apps']))"
```
→ `8`

Then exercise a real write and confirm a backup appeared:
```
curl -X POST http://localhost:7000/api/update-source \
     -H 'Content-Type: application/json' \
     -d '{"subtitle":"backup smoke test"}'
ls data/backups/
```
→ at least one `source-<timestamp>.json` file

Confirm the catalog is still valid and no temp files leaked:
```
python3 -c "import json; print(len(json.load(open('data/source.json'))['apps']))"
ls data/.source-*.tmp 2>&1
```
→ `8`, and "No such file or directory"

## Test plan

Add to `tests/test_routes.py`, following the existing `client` fixture pattern:

1. **`test_save_source_is_atomic_on_failure`** — patch `json.dump` to raise partway through, call a mutating route, then assert `source.json` still parses and still contains the original app. This is the core regression test: before this plan the file would be zero bytes.
2. **`test_backup_written_before_save`** — call `POST /api/update-source`, assert a file matching `source-*.json` now exists in `<tmp_path>/backups/`, and that its contents are the *previous* catalog (not the new one).
3. **`test_no_temp_files_left_behind`** — after a successful mutation, assert no `.source-*.tmp` files remain in the data dir.
4. **`test_concurrent_add_version_all_land`** — spawn 20 threads each calling `add_version` with a distinct version string against the same app; assert all 20 appear in the final catalog. **This test fails before the change and passes after** — it is the direct proof the lock works. Call `source_manager.add_version(...)` directly rather than going through the test client, which is not thread-safe.
5. **`test_backups_pruned_to_20`** — perform 25 mutations, assert `len(os.listdir(backups_dir)) == 20`.

Verification: `.venv/bin/python -m pytest tests/ -q` → all pass, including 5 new tests.

## Done criteria

ALL must hold:

- [ ] `python3 -m py_compile app.py` exits 0
- [ ] `grep -c "with self._lock:" app.py` returns `8`
- [ ] `grep -c "os.replace" app.py` returns `1`
- [ ] `grep -c "atomicwrites" requirements.txt app.py` returns `0` for both
- [ ] `grep -c "open(self.source_file, 'w')" app.py` returns `0`
- [ ] `.venv/bin/python -m pytest tests/ -q` exits 0 with 5 new tests passing
- [ ] The concurrency test fails when the `with self._lock:` wrappers are temporarily removed (prove the test is real)
- [ ] After a live mutation, `data/backups/` contains a timestamped file and `data/source.json` lists 8 apps
- [ ] No `.source-*.tmp` files remain in `data/`
- [ ] `git status --short` shows only `app.py`, `requirements.txt`, `tests/test_routes.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- Any of the eight methods calls another one of the eight. `threading.Lock` is non-reentrant and you would deadlock. Report the call graph; do not swap in `RLock` on your own.
- The concurrency test still loses writes after the lock is added. That means there is a second write path you have not found — report the investigation, do not add more locks speculatively.
- `os.replace` raises `OSError: [Errno 18] Invalid cross-device link`. The temp file landed on a different filesystem than `data/` — the `dir=` argument is wrong. Fix that specific bug; do not fall back to `shutil.move`, which is not atomic.
- The test suite had failures **before** you started. Fix nothing; report — you need a green baseline to attribute regressions.
- `data/source.json` reports anything other than 8 apps at any point.
- You find yourself changing a route handler or the `(bool, message)` contract. Out of scope — report.

## Maintenance notes

- **The in-process lock is only sufficient with a single process.** `app.py` currently runs under `app.run()` (one process, threaded), so it holds. If the app ever moves to gunicorn with `-w >1` — a deferred finding — **this lock silently stops working** and must become a file lock (`fcntl.flock` on a lockfile in `DATA_DIR`). Anyone touching the `CMD` in the `Dockerfile` must revisit this. Leave a comment in the code saying so.
- Downloads happen while holding the lock, so two simultaneous IPA uploads serialise. Acceptable for a single-admin tool; if this ever becomes multi-user, the fix is to download to a temp file *outside* the lock and only take the lock for the catalog mutation. Plan 007 restructures that download code and is the natural place to do it.
- `data/backups/` now grows to 20 files of a few KB each — negligible next to 1.3 GB of IPAs. It is **not** a substitute for real backups of `data/ipas/`, which remain entirely unprotected.
- **Reviewer should scrutinise**: that the lock spans the *entire* read-modify-write in all eight methods (a wrapper around only the `save_source` call would look correct in a diff and fix nothing), and that `tempfile.mkstemp` uses `dir=` pointing at the catalog's own directory.
