"""Smoke-test suite for the AltStore Source Manager Flask app.

Covers all 13 routes in app.py with a fresh, isolated data directory per
test (via `tmp_path`). No test reads or writes the real `data/` directory
and no test makes real network requests.

See plans/005-smoke-test-suite.md for the design rationale.
"""

import gzip
import http.server
import json
import os
import importlib
import threading

import pytest


def seed_source():
    """A minimal valid source.json document, matching plans/005's fixture."""
    return {
        "name": "Test Source",
        "subtitle": "",
        "description": "",
        "iconURL": "",
        "headerURL": "",
        "website": "",
        "tintColor": "#4185A9",
        "featuredApps": [],
        "apps": [
            {
                "name": "Example App",
                "bundleIdentifier": "com.example.app",
                "developerName": "Example Dev",
                "localizedDescription": "",
                "iconURL": "",
                "addedDate": "2026-01-01",
                "versions": [
                    {
                        "version": "1.0.0",
                        "date": "2026-01-01T00:00:00Z",
                        "downloadURL": "http://example.test/ipas/com.example.app/1.0.0.ipa",
                        "minOSVersion": "14.0",
                        "size": 1234,
                    }
                ],
            }
        ],
        "news": [],
    }


@pytest.fixture(scope="function")
def client(tmp_path):
    """Fresh app instance rooted at an isolated temp data dir.

    DATA_DIR must be set before `app` is imported: app.py instantiates
    SourceManager at module scope (app.py:733-734), which creates
    directories and writes a default source.json on construction. A normal
    pytest fixture runs too late for a plain `import app`, so we set the
    env var first and use importlib.reload to re-execute the module body
    (including the module-scope SourceManager construction) against the
    new DATA_DIR.
    """
    os.environ["DATA_DIR"] = str(tmp_path)

    import app as app_module

    importlib.reload(app_module)

    # Seed a known catalog. Must happen after the reload, since
    # SourceManager.initialize_source() writes a default source.json
    # during construction and would otherwise overwrite this seed.
    (tmp_path / "source.json").write_text(json.dumps(seed_source()))

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        c.app_module = app_module  # stash for tests that need module access
        yield c


# ---------------------------------------------------------------------------
# Local-loopback HTTP stubs for the download tests below. These bind to
# 127.0.0.1 on an ephemeral port and only ever talk to the test process
# itself -- no real network call leaves the machine.
# ---------------------------------------------------------------------------


class _GzipIpaHandler(http.server.BaseHTTPRequestHandler):
    payload = b"PK\x03\x04 fake but plausible ipa bytes " * 50

    def do_GET(self):
        compressed = gzip.compress(self.payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(compressed)))
        self.end_headers()
        self.wfile.write(compressed)

    def log_message(self, format, *args):
        pass  # keep test output quiet


class _LargeIpaHandler(http.server.BaseHTTPRequestHandler):
    payload = b"X" * (256 * 1024)  # 256 KiB -- bigger than the test's cap

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format, *args):
        pass


def _local_stub_server(handler_cls):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/app.ipa"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def gzip_ipa_server():
    yield from _local_stub_server(_GzipIpaHandler)


@pytest.fixture
def large_ipa_server():
    yield from _local_stub_server(_LargeIpaHandler)


# ---------------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------------


def test_source_json_ok(client):
    """Direct regression test for the 'cache_timeout' 500 in data/app.log."""
    resp = client.get("/source.json")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert "apps" in body
    bundle_ids = [a["bundleIdentifier"] for a in body["apps"]]
    assert "com.example.app" in bundle_ids


def test_ipas_serves_existing_file(client, tmp_path):
    ipa_dir = tmp_path / "ipas" / "com.example.app"
    ipa_dir.mkdir(parents=True)
    payload = b"fake ipa bytes"
    (ipa_dir / "1.0.0.ipa").write_bytes(payload)

    resp = client.get("/ipas/com.example.app/1.0.0.ipa")
    assert resp.status_code == 200
    assert resp.data == payload


def test_ipas_missing_file_404(client):
    resp = client.get("/ipas/com.example.app/does-not-exist.ipa")
    assert resp.status_code == 404


def test_icons_serves_existing_png(client, tmp_path):
    icon_dir = tmp_path / "icons" / "com.example.app"
    icon_dir.mkdir(parents=True)
    payload = b"\x89PNG\r\n\x1a\nfakeicondata"
    (icon_dir / "icon.png").write_bytes(payload)

    resp = client.get("/icons/com.example.app/icon.png")
    assert resp.status_code == 200
    assert resp.data == payload
    assert resp.mimetype == "image/png"


def test_icons_disallowed_extension_400(client, tmp_path):
    # Extension allowlist check at app.py:2271 rejects before checking
    # whether the file even exists.
    resp = client.get("/icons/com.example.app/icon.exe")
    assert resp.status_code == 400


def test_qr_ok_png(client):
    """Permanent guard for Plan 004 (Pillow QR generation), which has
    landed. This is a normal passing test, not xfail: /qr genuinely works
    now that pillow==11.3.0 is installed.

    The magic-byte assertion matters more than the status code: it is
    what would catch a silent fallback to the pure-Python PyPNGImage
    backend if Pillow ever went missing again.
    """
    resp = client.get("/qr")
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data.startswith(b"\x89PNG\r\n\x1a\n")


def test_api_apps_lists_seeded_app(client):
    resp = client.get("/api/apps")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["bundleIdentifier"] == "com.example.app"


def test_api_app_found(client):
    resp = client.get("/api/app/com.example.app")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["bundleIdentifier"] == "com.example.app"


def test_api_app_not_found_404(client):
    resp = client.get("/api/app/com.nonexistent")
    assert resp.status_code == 404


def test_index_page_ok(client):
    resp = client.get("/")
    assert resp.status_code == 200
    # Stable marker: the id of the <img> tag the QR code is loaded into.
    # This becomes the guard for Plan 009's template extraction.
    assert b"qrImage" in resp.data


# ---------------------------------------------------------------------------
# Mutating routes
# ---------------------------------------------------------------------------


def test_add_app_then_delete_app_round_trip(client):
    new_app = {
        "name": "New App",
        "bundleIdentifier": "com.example.newapp",
        "developerName": "New Dev",
        "version": "1.0.0",
    }

    resp = client.post("/api/add-app", json=new_app)
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = client.get("/source.json")
    body = json.loads(resp.data)
    bundle_ids = [a["bundleIdentifier"] for a in body["apps"]]
    assert "com.example.newapp" in bundle_ids

    resp = client.post("/api/delete-app", json={"bundleIdentifier": "com.example.newapp"})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = client.get("/source.json")
    body = json.loads(resp.data)
    bundle_ids = [a["bundleIdentifier"] for a in body["apps"]]
    assert "com.example.newapp" not in bundle_ids


def test_add_version_grows_versions_list(client):
    resp = client.post(
        "/api/add-version",
        json={
            "bundleIdentifier": "com.example.app",
            "version": "2.0.0",
            "downloadURL": "http://example.test/ipas/com.example.app/2.0.0.ipa",
        },
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = client.get("/api/app/com.example.app")
    body = json.loads(resp.data)
    assert len(body["versions"]) == 2


def test_update_version_persists_min_os(client):
    resp = client.post(
        "/api/update-version",
        json={
            "bundleIdentifier": "com.example.app",
            "version": "1.0.0",
            "downloadURL": "http://example.test/ipas/com.example.app/1.0.0.ipa",
            "minOSVersion": "17.0",
        },
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = client.get("/api/app/com.example.app")
    body = json.loads(resp.data)
    version = next(v for v in body["versions"] if v["version"] == "1.0.0")
    assert version["minOSVersion"] == "17.0"


def test_update_app_persists_name(client):
    resp = client.post(
        "/api/update-app",
        json={"bundleIdentifier": "com.example.app", "name": "Renamed App"},
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = client.get("/api/app/com.example.app")
    body = json.loads(resp.data)
    assert body["name"] == "Renamed App"


def test_update_source_persists_name(client):
    resp = client.post("/api/update-source", json={"name": "Renamed Source"})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = client.get("/source.json")
    body = json.loads(resp.data)
    assert body["name"] == "Renamed Source"


# ---------------------------------------------------------------------------
# Validation: missing bundleIdentifier -> 400 with a JSON error
# ---------------------------------------------------------------------------


def test_delete_app_missing_bundle_id_400(client):
    resp = client.post("/api/delete-app", json={})
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False


def test_update_app_missing_bundle_id_400(client):
    resp = client.post("/api/update-app", json={"name": "X"})
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False


def test_add_version_missing_bundle_id_400(client):
    resp = client.post(
        "/api/add-version",
        json={"version": "3.0.0", "downloadURL": "http://example.test/x.ipa"},
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False


def test_update_version_missing_bundle_id_400(client):
    resp = client.post(
        "/api/update-version",
        json={"version": "1.0.0", "downloadURL": "http://example.test/x.ipa"},
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False


# ---------------------------------------------------------------------------
# Known-bug documentation — deliberately asserting current behaviour, not
# the "correct" behaviour, for the cases that remain intentional by design.
# These are NOT failures of this test suite; they are regression pins on
# the documented behaviour itself.
# ---------------------------------------------------------------------------


def test_delete_app_nonexistent_bundle_id_400(client):
    """Unlike some mutating routes, delete_app correctly reports failure
    when the bundle id does not exist (app.py:518-527: it compares list
    length before/after filtering). This is NOT one of the "reports
    success on failure" bugs Plan 007 targets — recorded here so a
    future change to this behaviour shows up as an intentional diff.
    """
    resp = client.post("/api/delete-app", json={"bundleIdentifier": "com.does.not.exist"})
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False


def test_add_version_downloadurl_never_fetched_without_download_flag(client, tmp_path):
    """Intended behaviour: add_version's success path never validates that
    downloadURL is reachable, and the route only requires ONE of
    ipaFile/downloadURL/downloadFromUrl (app.py:2463-2464). Passing a
    bogus, unreachable downloadURL without downloadFromUrl=True is
    accepted verbatim and stored as-is -- no HTTP request is made because
    add_version() in SourceManager only calls download_ipa_from_url when
    download_from_url is truthy. Validating that a caller-supplied
    downloadURL resolves would require an outbound request to a
    caller-controlled address, which is a deliberately deferred SSRF
    finding, kept out of scope here. This test is also the
    guard that our own suite makes no real network calls: the URL below
    is deliberately unroutable.
    """
    bogus_url = "http://example.invalid/never-fetched.ipa"
    resp = client.post(
        "/api/add-version",
        json={
            "bundleIdentifier": "com.example.app",
            "version": "9.9.9",
            "downloadURL": bogus_url,
        },
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = client.get("/api/app/com.example.app")
    body = json.loads(resp.data)
    version = next(v for v in body["versions"] if v["version"] == "9.9.9")
    # Stored verbatim -- no fetch, no validation that it resolves.
    assert version["downloadURL"] == bogus_url


# ---------------------------------------------------------------------------
# Fail loudly instead of silently reporting success
# ---------------------------------------------------------------------------


def test_failed_download_reports_error(client):
    """Defect 1: a failed IPA download must surface as a specific error,
    not fall through to 'App added successfully'. Points at an unreachable
    loopback port (the same pattern the plan's own curl repro uses)
    rather than a real network host, so no outbound connection is ever
    actually reachable and no real network call is attempted.
    """
    resp = client.post(
        "/api/add-app",
        data={
            "name": "Broken",
            "bundleIdentifier": "com.test.broken",
            "developerName": "T",
            "version": "1.0",
            "downloadURL": "http://127.0.0.1:9/nope.ipa",
            "downloadFromUrl": "true",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False
    assert body["error"] != "App added successfully"

    resp = client.get("/source.json")
    body = json.loads(resp.data)
    bundle_ids = [a["bundleIdentifier"] for a in body["apps"]]
    assert "com.test.broken" not in bundle_ids


def test_failed_save_does_not_return_success_message(client, monkeypatch):
    """Defect 1: when save_source itself fails, add_app_manual must not
    report 'App added successfully' as its error message (app.py:341,
    pre-fix)."""
    monkeypatch.setattr(client.app_module.source_manager, "save_source", lambda data: False)

    resp = client.post(
        "/api/add-app",
        json={
            "name": "X",
            "bundleIdentifier": "com.example.savefail",
            "developerName": "Y",
            "version": "1.0",
        },
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False
    assert body["error"] != "App added successfully"


def test_add_app_missing_bundle_id_400(client):
    """Defect 5: /api/add-app lacked the bundleIdentifier guard its sibling
    mutating routes all have, so a missing bundleIdentifier fell through
    to a KeyError whose raw repr ("'bundleIdentifier'") was returned as
    the error message."""
    resp = client.post(
        "/api/add-app",
        json={"name": "X", "developerName": "Y", "version": "1.0"},
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False
    assert body["error"] == "Bundle identifier is required"


def test_update_version_preserves_original_on_failed_fetch(client, tmp_path):
    """Defect 2, the critical one: update_version must fetch the
    replacement IPA before touching the original file. Before the fix,
    the old file was deleted first ("delete old one first"), so a
    transient network error permanently destroyed a hosted binary while
    /source.json kept advertising it as available and the route reported
    {"success": true}. This test fails before the fix and passes after.

    Uses an unreachable loopback port rather than a real network host --
    same pattern as the plan's own curl repro -- so no real network call
    is attempted.
    """
    ipa_dir = tmp_path / "ipas" / "com.example.app"
    ipa_dir.mkdir(parents=True)
    original_bytes = b"original ipa bytes -- must survive a failed re-fetch"
    (ipa_dir / "1.0.0.ipa").write_bytes(original_bytes)

    resp = client.post(
        "/api/update-version",
        data={
            "bundleIdentifier": "com.example.app",
            "version": "1.0.0",
            "downloadURL": "http://127.0.0.1:9/nope.ipa",
            "downloadFromUrl": "true",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False

    assert (ipa_dir / "1.0.0.ipa").exists()
    assert (ipa_dir / "1.0.0.ipa").read_bytes() == original_bytes


def test_gzip_encoded_download_is_decoded(client, tmp_path, gzip_ipa_server):
    """Defect 3: response.raw (used with shutil.copyfileobj) is the
    undecoded urllib3 stream, so a gzip-encoded origin wrote a gzip
    stream to disk instead of the real .ipa. iter_content() applies
    content decoding, so the bytes landing on disk must be the original,
    decompressed payload.
    """
    resp = client.post(
        "/api/add-app",
        data={
            "name": "Gzipped",
            "bundleIdentifier": "com.test.gzipped",
            "developerName": "T",
            "version": "1.0",
            "downloadURL": gzip_ipa_server,
            "downloadFromUrl": "true",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    saved = tmp_path / "ipas" / "com.test.gzipped" / "1.0.ipa"
    assert saved.exists()
    assert saved.read_bytes() == _GzipIpaHandler.payload


def test_download_over_size_limit_is_rejected(client, tmp_path, large_ipa_server):
    """Defect 3: outbound downloads had no size ceiling at all (Flask's
    MAX_CONTENT_LENGTH only bounds inbound uploads). A download that
    exceeds the configured limit must be rejected and leave no partial
    file behind.
    """
    client.app_module.app.config["MAX_CONTENT_LENGTH"] = 1024  # 1 KiB cap

    resp = client.post(
        "/api/add-app",
        data={
            "name": "TooBig",
            "bundleIdentifier": "com.test.toobig",
            "developerName": "T",
            "version": "1.0",
            "downloadURL": large_ipa_server,
            "downloadFromUrl": "true",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False

    # No partial file left behind.
    bundle_dir = tmp_path / "ipas" / "com.test.toobig"
    assert not bundle_dir.exists() or list(bundle_dir.iterdir()) == []


def test_delete_app_removes_icon(client, tmp_path):
    """delete_icon_file was defined but never called from delete_app, so
    every deleted app left its icon directory behind forever. delete_app
    must now also remove the icon.
    """
    icon_dir = tmp_path / "icons" / "com.example.app"
    icon_dir.mkdir(parents=True)
    (icon_dir / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\nfakeicon")

    resp = client.post("/api/delete-app", json={"bundleIdentifier": "com.example.app"})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    assert not icon_dir.exists()


def test_get_ipa_path_does_not_create_directories(client, tmp_path):
    """get_ipa_path is a read-only path computation and must not create
    the bundle subdirectory as a side effect -- that left empty
    directories under data/ipas/ for bundle ids that were only ever
    looked up, never written.
    """
    source_manager = client.app_module.source_manager
    bundle_folder = tmp_path / "ipas" / "com.example.novel"

    path = source_manager.get_ipa_path("com.example.novel", "1.0.0")

    assert not bundle_folder.exists()
    assert not os.path.exists(os.path.dirname(path))


# ---------------------------------------------------------------------------
# Plan 006: atomic, serialized catalog writes
# ---------------------------------------------------------------------------


def test_save_source_is_atomic_on_failure(client, tmp_path, monkeypatch):
    """Core regression test. Before this plan, save_source opened
    source.json with mode 'w', which truncates the file to zero bytes
    before writing anything -- any exception during json.dump left a
    truncated, unparseable file on disk. Now save_source writes to a temp
    file in the same directory and only os.replace()s it into position on
    success, so a failure mid-write must leave the live catalog completely
    untouched.
    """
    original_bytes = (tmp_path / "source.json").read_bytes()

    def boom(*args, **kwargs):
        raise RuntimeError("simulated failure mid-write")

    # json.dump is only called from SourceManager.save_source (verified via
    # grep), so this patch only affects the write path under test -- it
    # does not touch json.dumps/loads used by Flask's jsonify or by this
    # test file itself.
    monkeypatch.setattr(client.app_module.json, "dump", boom)

    resp = client.post("/api/update-source", json={"name": "Should Not Persist"})
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False

    # The file on disk must be byte-for-byte identical to before the
    # failed write -- not truncated, not partially written.
    assert (tmp_path / "source.json").read_bytes() == original_bytes
    reloaded = json.loads((tmp_path / "source.json").read_text())
    assert reloaded["name"] == "Test Source"
    assert reloaded["apps"][0]["bundleIdentifier"] == "com.example.app"


def test_backup_written_before_save(client, tmp_path):
    """update-source must back up the previous catalog to data/backups/
    before replacing it, so the pre-change state is recoverable."""
    backups_dir = tmp_path / "backups"

    resp = client.post("/api/update-source", json={"name": "Renamed Source"})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    backup_files = sorted(backups_dir.glob("source-*.json"))
    assert len(backup_files) == 1

    backup_content = json.loads(backup_files[0].read_text())
    # The backup holds the catalog as it was BEFORE this save, not after.
    assert backup_content["name"] == "Test Source"

    current_content = json.loads((tmp_path / "source.json").read_text())
    assert current_content["name"] == "Renamed Source"


def test_no_temp_files_left_behind(client, tmp_path):
    """After a successful mutation, the temp file save_source writes to
    (prefix '.source-', suffix '.json.tmp') must have been renamed away,
    not left sitting in the data directory."""
    resp = client.post("/api/update-source", json={"name": "Cleanup Check"})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    leftover = list(tmp_path.glob(".source-*.json.tmp"))
    assert leftover == []


def test_concurrent_add_version_all_land(client, tmp_path):
    """Direct proof that SourceManager._lock serializes the read-modify-
    write cycle in add_version. 20 threads each add a distinct version to
    the same app concurrently; without the lock, two threads can both
    load_source() before either save_source()s, and the second save
    silently clobbers the first thread's write. Calls source_manager
    directly (not the test client, which is not thread-safe).
    """
    import threading as _threading

    source_manager = client.app_module.source_manager
    n = 20
    results = [None] * n

    def worker(i):
        results[i] = source_manager.add_version(
            "com.example.app",
            {
                "version": f"9.{i}.0",
                "downloadURL": f"http://example.test/ipas/com.example.app/9.{i}.0.ipa",
            },
        )

    threads = [_threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(success for success, _ in results)

    final = source_manager.load_source()
    versions = {v["version"] for v in final["apps"][0]["versions"]}
    expected = {f"9.{i}.0" for i in range(n)} | {"1.0.0"}
    assert versions == expected


def test_backups_pruned_to_20(client, tmp_path):
    """data/backups/ must be pruned to the most recent 20 entries so it
    does not grow without bound."""
    backups_dir = tmp_path / "backups"

    for i in range(25):
        resp = client.post("/api/update-source", json={"name": f"Name {i}"})
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["success"] is True

    backup_files = list(backups_dir.glob("source-*.json"))
    assert len(backup_files) == 20
