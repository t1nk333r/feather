"""Smoke-test suite for the AltStore Source Manager Flask app.

Covers all 13 routes in app.py with a fresh, isolated data directory per
test (via `tmp_path`). No test reads or writes the real `data/` directory
and no test makes real network requests.

See plans/005-smoke-test-suite.md for the design rationale.
"""

import gzip
import http.server
import json
import logging
import os
import importlib
import threading

import pytest


# Obviously-fake credentials -- never a real password. Plan 010.
TEST_ADMIN_PASSWORD = "test-password-not-a-real-secret"
TEST_SECRET_KEY = "test-secret-key"


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

    ADMIN_PASSWORD and SECRET_KEY must also be set before the reload
    (Plan 010): app.py now refuses to import at all without ADMIN_PASSWORD,
    and sessions need a stable SECRET_KEY to survive across requests within
    a test.
    """
    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["ADMIN_PASSWORD"] = TEST_ADMIN_PASSWORD
    os.environ["SECRET_KEY"] = TEST_SECRET_KEY

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


@pytest.fixture(scope="function")
def authed_client(client):
    """Same as `client`, but already logged in -- the session cookie is
    set on the underlying test client, so subsequent requests made through
    it carry an authenticated session (Plan 010).
    """
    resp = client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD})
    assert resp.status_code == 200
    return client


@pytest.fixture(scope="function")
def client_with_base_url(tmp_path, monkeypatch):
    """Same as `client`, but with PUBLIC_BASE_URL set before the module
    reloads (plan 008). resolve_base_url() reads PUBLIC_BASE_URL at import
    time via a module-level constant, so it must be set before the reload,
    same as DATA_DIR in the `client` fixture above. monkeypatch.setenv
    restores the previous (unset) value automatically at teardown, so it
    does not leak into other tests -- the next test's `client`/
    `client_with_base_url` fixture reload picks up the restored env.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://feather.example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("SECRET_KEY", TEST_SECRET_KEY)

    import app as app_module

    importlib.reload(app_module)

    (tmp_path / "source.json").write_text(json.dumps(seed_source()))

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        c.app_module = app_module
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


# ---------------------------------------------------------------------------
# Plan 024: AltStore-required fields, filled in at serve time
# ---------------------------------------------------------------------------


def test_source_json_has_required_top_level_fields(client):
    """AltStore requires 'nsfw' at the top level; normalize_source must add
    it when the stored catalog lacks it."""
    resp = client.get("/source.json")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    for key in ("name", "apps", "news", "nsfw"):
        assert key in body
    assert body["nsfw"] is False


def test_source_json_apps_have_app_permissions(client):
    """Every app in the served document must carry 'appPermissions' with
    the shape AltStore expects, even though the seeded catalog has none."""
    resp = client.get("/source.json")
    body = json.loads(resp.data)
    assert body["apps"], "seed_source() must produce at least one app"
    for app_entry in body["apps"]:
        assert "appPermissions" in app_entry
        perms = app_entry["appPermissions"]
        assert isinstance(perms["entitlements"], list)
        assert isinstance(perms["privacy"], dict)


def test_source_json_versions_have_build_version(client):
    """Every version in the served document must carry a string
    'buildVersion', defaulting to the version string when absent."""
    resp = client.get("/source.json")
    body = json.loads(resp.data)
    for app_entry in body["apps"]:
        assert app_entry["versions"], "seed_source() must produce at least one version"
        for version_entry in app_entry["versions"]:
            assert isinstance(version_entry.get("buildVersion"), str)


def test_normalize_does_not_overwrite_existing_values(client):
    """A catalog that already has real appPermissions / buildVersion must
    keep them unchanged -- normalize_source only fills absences."""
    app_module = client.app_module
    source = {
        "name": "x",
        "apps": [
            {
                "bundleIdentifier": "a.b",
                "appPermissions": {
                    "entitlements": ["com.apple.developer.something"],
                    "privacy": {"NSCameraUsageDescription": "reason"},
                },
                "versions": [
                    {"version": "1.0.0", "buildVersion": "25"},
                ],
            }
        ],
        "news": [],
        "nsfw": True,
    }
    out = app_module.normalize_source(source)
    assert out["nsfw"] is True
    app_entry = out["apps"][0]
    assert app_entry["appPermissions"] == {
        "entitlements": ["com.apple.developer.something"],
        "privacy": {"NSCameraUsageDescription": "reason"},
    }
    assert app_entry["versions"][0]["buildVersion"] == "25"


def test_normalize_does_not_mutate_input(client):
    """normalize_source must work on a deep copy and leave its argument
    untouched -- a future cache would turn mutation into silent corruption."""
    app_module = client.app_module
    source = {
        "name": "x",
        "apps": [{"bundleIdentifier": "a.b", "versions": [{"version": "1.0"}]}],
        "news": [],
    }
    before = json.loads(json.dumps(source))
    app_module.normalize_source(source)
    assert source == before
    assert "nsfw" not in source
    assert "appPermissions" not in source["apps"][0]
    assert "buildVersion" not in source["apps"][0]["versions"][0]


def test_normalize_tolerates_malformed_catalog(client):
    """A malformed catalog -- an app missing 'versions', a version that is
    a string rather than a dict, a missing 'apps' key -- must not raise,
    and /source.json must still return 200. Every subscribed device polls
    this route; it must never 500."""
    app_module = client.app_module

    # A version that is not a dict, and an app missing 'versions' entirely.
    malformed = {
        "name": "x",
        "apps": [
            {"bundleIdentifier": "a.b"},
            {"bundleIdentifier": "c.d", "versions": ["not-a-dict"]},
        ],
        "news": [],
    }
    out = app_module.normalize_source(malformed)
    assert out["nsfw"] is False
    assert out["apps"][0]["appPermissions"] == {"entitlements": [], "privacy": {}}
    assert out["apps"][1]["versions"] == ["not-a-dict"]

    # A missing 'apps' key entirely.
    no_apps = {"name": "x", "news": []}
    out2 = app_module.normalize_source(no_apps)
    assert out2["nsfw"] is False

    # And end to end: seed the on-disk catalog with the malformed shape
    # and confirm the route survives it.
    data_dir = app_module.DATA_DIR
    with open(os.path.join(data_dir, "source.json"), "w") as f:
        f.write(json.dumps(malformed))
    resp = client.get("/source.json")
    assert resp.status_code == 200


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


def test_add_app_then_delete_app_round_trip(authed_client):
    new_app = {
        "name": "New App",
        "bundleIdentifier": "com.example.newapp",
        "developerName": "New Dev",
        "version": "1.0.0",
    }

    resp = authed_client.post("/api/add-app", json=new_app)
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = authed_client.get("/source.json")
    body = json.loads(resp.data)
    bundle_ids = [a["bundleIdentifier"] for a in body["apps"]]
    assert "com.example.newapp" in bundle_ids

    resp = authed_client.post("/api/delete-app", json={"bundleIdentifier": "com.example.newapp"})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = authed_client.get("/source.json")
    body = json.loads(resp.data)
    bundle_ids = [a["bundleIdentifier"] for a in body["apps"]]
    assert "com.example.newapp" not in bundle_ids


def test_add_version_grows_versions_list(authed_client):
    resp = authed_client.post(
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

    resp = authed_client.get("/api/app/com.example.app")
    body = json.loads(resp.data)
    assert len(body["versions"]) == 2


def test_update_version_persists_min_os(authed_client):
    resp = authed_client.post(
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

    resp = authed_client.get("/api/app/com.example.app")
    body = json.loads(resp.data)
    version = next(v for v in body["versions"] if v["version"] == "1.0.0")
    assert version["minOSVersion"] == "17.0"


def test_update_app_persists_name(authed_client):
    resp = authed_client.post(
        "/api/update-app",
        json={"bundleIdentifier": "com.example.app", "name": "Renamed App"},
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = authed_client.get("/api/app/com.example.app")
    body = json.loads(resp.data)
    assert body["name"] == "Renamed App"


def test_update_source_persists_name(authed_client):
    resp = authed_client.post("/api/update-source", json={"name": "Renamed Source"})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = authed_client.get("/source.json")
    body = json.loads(resp.data)
    assert body["name"] == "Renamed Source"


# ---------------------------------------------------------------------------
# Validation: missing bundleIdentifier -> 400 with a JSON error
# ---------------------------------------------------------------------------


def test_delete_app_missing_bundle_id_400(authed_client):
    resp = authed_client.post("/api/delete-app", json={})
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False


def test_update_app_missing_bundle_id_400(authed_client):
    resp = authed_client.post("/api/update-app", json={"name": "X"})
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False


def test_add_version_missing_bundle_id_400(authed_client):
    resp = authed_client.post(
        "/api/add-version",
        json={"version": "3.0.0", "downloadURL": "http://example.test/x.ipa"},
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False


def test_update_version_missing_bundle_id_400(authed_client):
    resp = authed_client.post(
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


def test_delete_app_nonexistent_bundle_id_400(authed_client):
    """Unlike some mutating routes, delete_app correctly reports failure
    when the bundle id does not exist (app.py:518-527: it compares list
    length before/after filtering). This is NOT one of the "reports
    success on failure" bugs Plan 007 targets — recorded here so a
    future change to this behaviour shows up as an intentional diff.
    """
    resp = authed_client.post("/api/delete-app", json={"bundleIdentifier": "com.does.not.exist"})
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False


def test_add_version_downloadurl_never_fetched_without_download_flag(authed_client, tmp_path):
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
    resp = authed_client.post(
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

    resp = authed_client.get("/api/app/com.example.app")
    body = json.loads(resp.data)
    version = next(v for v in body["versions"] if v["version"] == "9.9.9")
    # Stored verbatim -- no fetch, no validation that it resolves.
    assert version["downloadURL"] == bogus_url


# ---------------------------------------------------------------------------
# Fail loudly instead of silently reporting success
# ---------------------------------------------------------------------------


def test_failed_download_reports_error(authed_client):
    """Defect 1: a failed IPA download must surface as a specific error,
    not fall through to 'App added successfully'. Points at an unreachable
    loopback port (the same pattern the plan's own curl repro uses)
    rather than a real network host, so no outbound connection is ever
    actually reachable and no real network call is attempted.
    """
    resp = authed_client.post(
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

    resp = authed_client.get("/source.json")
    body = json.loads(resp.data)
    bundle_ids = [a["bundleIdentifier"] for a in body["apps"]]
    assert "com.test.broken" not in bundle_ids


def test_failed_save_does_not_return_success_message(authed_client, monkeypatch):
    """Defect 1: when save_source itself fails, add_app_manual must not
    report 'App added successfully' as its error message (app.py:341,
    pre-fix)."""
    monkeypatch.setattr(authed_client.app_module.source_manager, "save_source", lambda data: False)

    resp = authed_client.post(
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


def test_add_app_missing_bundle_id_400(authed_client):
    """Defect 5: /api/add-app lacked the bundleIdentifier guard its sibling
    mutating routes all have, so a missing bundleIdentifier fell through
    to a KeyError whose raw repr ("'bundleIdentifier'") was returned as
    the error message."""
    resp = authed_client.post(
        "/api/add-app",
        json={"name": "X", "developerName": "Y", "version": "1.0"},
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False
    assert body["error"] == "Bundle identifier is required"


def test_update_version_preserves_original_on_failed_fetch(authed_client, tmp_path):
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

    resp = authed_client.post(
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


def test_gzip_encoded_download_is_decoded(authed_client, tmp_path, gzip_ipa_server):
    """Defect 3: response.raw (used with shutil.copyfileobj) is the
    undecoded urllib3 stream, so a gzip-encoded origin wrote a gzip
    stream to disk instead of the real .ipa. iter_content() applies
    content decoding, so the bytes landing on disk must be the original,
    decompressed payload.
    """
    resp = authed_client.post(
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


def test_download_over_size_limit_is_rejected(authed_client, tmp_path, large_ipa_server):
    """Defect 3: outbound downloads had no size ceiling at all (Flask's
    MAX_CONTENT_LENGTH only bounds inbound uploads). A download that
    exceeds the configured limit must be rejected and leave no partial
    file behind.
    """
    authed_client.app_module.app.config["MAX_CONTENT_LENGTH"] = 1024  # 1 KiB cap

    resp = authed_client.post(
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


def test_delete_app_removes_icon(authed_client, tmp_path):
    """delete_icon_file was defined but never called from delete_app, so
    every deleted app left its icon directory behind forever. delete_app
    must now also remove the icon.
    """
    icon_dir = tmp_path / "icons" / "com.example.app"
    icon_dir.mkdir(parents=True)
    (icon_dir / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\nfakeicon")

    resp = authed_client.post("/api/delete-app", json={"bundleIdentifier": "com.example.app"})
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


def test_save_source_is_atomic_on_failure(authed_client, tmp_path, monkeypatch):
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
    monkeypatch.setattr(authed_client.app_module.json, "dump", boom)

    resp = authed_client.post("/api/update-source", json={"name": "Should Not Persist"})
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False

    # The file on disk must be byte-for-byte identical to before the
    # failed write -- not truncated, not partially written.
    assert (tmp_path / "source.json").read_bytes() == original_bytes
    reloaded = json.loads((tmp_path / "source.json").read_text())
    assert reloaded["name"] == "Test Source"
    assert reloaded["apps"][0]["bundleIdentifier"] == "com.example.app"


def test_backup_written_before_save(authed_client, tmp_path):
    """update-source must back up the previous catalog to data/backups/
    before replacing it, so the pre-change state is recoverable."""
    backups_dir = tmp_path / "backups"

    resp = authed_client.post("/api/update-source", json={"name": "Renamed Source"})
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


def test_no_temp_files_left_behind(authed_client, tmp_path):
    """After a successful mutation, the temp file save_source writes to
    (prefix '.source-', suffix '.json.tmp') must have been renamed away,
    not left sitting in the data directory."""
    resp = authed_client.post("/api/update-source", json={"name": "Cleanup Check"})
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


def test_backups_pruned_to_20(authed_client, tmp_path):
    """data/backups/ must be pruned to the most recent 20 entries so it
    does not grow without bound."""
    backups_dir = tmp_path / "backups"

    for i in range(25):
        resp = authed_client.post("/api/update-source", json={"name": f"Name {i}"})
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["success"] is True

    backup_files = list(backups_dir.glob("source-*.json"))
    assert len(backup_files) == 20


# ---------------------------------------------------------------------------
# Plan 008: derive published URLs from PUBLIC_BASE_URL, not the client-
# controlled Host header.
# ---------------------------------------------------------------------------


def test_download_url_uses_public_base_url(client_with_base_url, gzip_ipa_server):
    """A request with a spoofed Host header must not leak that host into a
    downloadURL written to source.json -- resolve_base_url() must prefer
    the configured PUBLIC_BASE_URL instead.

    Logs in with the same spoofed Host header used below: Werkzeug's test
    client scopes cookies per-host like a real browser, so a session
    established against the default host would not be sent back on a
    request that spoofs a different one.
    """
    login_resp = client_with_base_url.post(
        "/api/login",
        json={"password": TEST_ADMIN_PASSWORD},
        headers={"Host": "evil.example.com"},
    )
    assert login_resp.status_code == 200

    resp = client_with_base_url.post(
        "/api/add-app",
        data={
            "name": "Spoofed",
            "bundleIdentifier": "com.test.spoofed",
            "developerName": "T",
            "version": "1.0",
            "downloadURL": gzip_ipa_server,
            "downloadFromUrl": "true",
        },
        content_type="multipart/form-data",
        headers={"Host": "evil.example.com"},
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = client_with_base_url.get("/api/app/com.test.spoofed")
    body = json.loads(resp.data)
    download_url = body["versions"][0]["downloadURL"]
    assert download_url.startswith("http://feather.example.com/ipas/")
    assert "evil.example.com" not in download_url


def test_icon_url_uses_public_base_url(client_with_base_url, gzip_ipa_server):
    """Same guarantee as above, for get_local_icon_url via /api/update-app.

    Logs in with the same spoofed Host header used below -- see the
    docstring on test_download_url_uses_public_base_url for why.
    """
    login_resp = client_with_base_url.post(
        "/api/login",
        json={"password": TEST_ADMIN_PASSWORD},
        headers={"Host": "evil.example.com"},
    )
    assert login_resp.status_code == 200

    resp = client_with_base_url.post(
        "/api/update-app",
        data={
            "bundleIdentifier": "com.example.app",
            "name": "Example App",
            "developerName": "Example Dev",
            "iconURL": gzip_ipa_server,
            "downloadIconFromUrl": "true",
        },
        content_type="multipart/form-data",
        headers={"Host": "evil.example.com"},
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = client_with_base_url.get("/api/app/com.example.app")
    body = json.loads(resp.data)
    icon_url = body["iconURL"]
    assert icon_url.startswith("http://feather.example.com/icons/")
    assert "evil.example.com" not in icon_url


def test_qr_uses_public_base_url(client_with_base_url):
    """/qr must not error with a spoofed Host header, and resolve_base_url()
    -- the function it builds its payload from -- must ignore that header
    in favor of the configured PUBLIC_BASE_URL.

    No QR decoder is available in this environment (only qrcode[pil] is
    installed, which encodes but does not decode), so the payload itself
    is verified indirectly: via a direct call to resolve_base_url() inside
    a request context carrying the spoofed header, and via
    test_qr_url_has_slash_before_source_json below, which spies on the
    exact string handed to the QR encoder.
    """
    resp = client_with_base_url.get("/qr", headers={"Host": "evil.example.com"})
    assert resp.status_code == 200
    assert resp.mimetype == "image/png"

    app_module = client_with_base_url.app_module
    with app_module.app.test_request_context(headers={"Host": "evil.example.com"}):
        assert app_module.resolve_base_url() == "http://feather.example.com"


def test_qr_url_has_slash_before_source_json(client_with_base_url, monkeypatch):
    """Guards the concatenation bug: resolve_base_url() strips the trailing
    slash that request.url_root used to provide, so the /qr route must add
    it back explicitly before appending 'source.json'. Verified (per plan
    008 step 2) to fail with AssertionError -- payload becomes
    'feather://feather.example.comsource.json' -- if the route instead
    concatenates resolve_base_url() + 'source.json' without the slash.
    """
    app_module = client_with_base_url.app_module
    captured = {}
    original_add_data = app_module.qrcode.QRCode.add_data

    def spy_add_data(self, data, *args, **kwargs):
        captured["data"] = data
        return original_add_data(self, data, *args, **kwargs)

    monkeypatch.setattr(app_module.qrcode.QRCode, "add_data", spy_add_data)

    resp = client_with_base_url.get("/qr")
    assert resp.status_code == 200
    assert captured["data"] == "feather://feather.example.com/source.json"


def test_falls_back_to_host_when_unset(authed_client, gzip_ipa_server, caplog):
    """With PUBLIC_BASE_URL unset, the old Host-header-derived behaviour
    must still work (no regression for deployments that haven't set it
    yet), and a warning must be logged so an operator can tell why.
    """
    with caplog.at_level(logging.WARNING):
        resp = authed_client.post(
            "/api/add-app",
            data={
                "name": "Fallback",
                "bundleIdentifier": "com.test.fallback",
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

    resp = authed_client.get("/api/app/com.test.fallback")
    body = json.loads(resp.data)
    download_url = body["versions"][0]["downloadURL"]
    assert download_url.startswith("http://localhost/ipas/")

    assert any(
        "PUBLIC_BASE_URL is not set" in record.message for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Plan 010: login/session auth
#
# test_public_routes_never_require_auth is the most important test in this
# suite: it is the regression test for the constraint that no subscribed
# iOS device may ever be broken by an accidental @requires_auth on a route
# AltStore/Feather fetch anonymously.
# ---------------------------------------------------------------------------


# Payloads that produce a genuine 200 once authenticated -- not just a 400
# that an unauthenticated caller would also receive, which would make the
# "works when authed" test pass for the wrong reason.
MUTATING_ROUTES_VALID_PAYLOADS = [
    (
        "/api/add-app",
        {
            "name": "Authed Add",
            "bundleIdentifier": "com.example.authed-add",
            "developerName": "Dev",
            "version": "1.0.0",
        },
    ),
    ("/api/delete-app", {"bundleIdentifier": "com.example.app"}),
    ("/api/update-app", {"bundleIdentifier": "com.example.app", "name": "Renamed"}),
    (
        "/api/add-version",
        {
            "bundleIdentifier": "com.example.app",
            "version": "5.0.0",
            "downloadURL": "http://example.test/ipas/com.example.app/5.0.0.ipa",
        },
    ),
    (
        "/api/update-version",
        {
            "bundleIdentifier": "com.example.app",
            "version": "1.0.0",
            "downloadURL": "http://example.test/ipas/com.example.app/1.0.0.ipa",
        },
    ),
    ("/api/update-source", {"name": "Renamed Source"}),
]

MUTATING_ROUTE_PATHS = [route for route, _ in MUTATING_ROUTES_VALID_PAYLOADS]


@pytest.mark.parametrize("route", MUTATING_ROUTE_PATHS)
def test_mutating_routes_require_auth(client, route):
    """None of the six mutating routes may be reachable with no session."""
    resp = client.post(route, json={"bundleIdentifier": "x"})
    assert resp.status_code == 401


@pytest.mark.parametrize("route,payload", MUTATING_ROUTES_VALID_PAYLOADS)
def test_mutating_routes_work_when_authed(authed_client, route, payload):
    """The same six routes, authenticated, succeed -- never a 401."""
    resp = authed_client.post(route, json=payload)
    assert resp.status_code != 401
    assert resp.status_code in (200, 400)


@pytest.mark.parametrize(
    "route",
    [
        "/source.json",
        "/ipas/com.example.app/1.0.0.ipa",
        "/icons/com.example.app/icon.png",
        "/qr",
        "/",
    ],
)
def test_public_routes_never_require_auth(client, tmp_path, route):
    """The four anonymous-fetch routes AltStore/Feather clients rely on,
    plus GET /, must return 200 with NO session. Seeds a real .ipa/icon
    file so the two file-serving routes don't 404 for an unrelated reason
    and get mistaken for "correctly" not-200 -- a 401 here is the only
    failure this test should ever be able to report on those two routes,
    and it can't produce one without a file to serve in the first place.
    """
    ipa_dir = tmp_path / "ipas" / "com.example.app"
    ipa_dir.mkdir(parents=True, exist_ok=True)
    (ipa_dir / "1.0.0.ipa").write_bytes(b"fake ipa bytes")

    icon_dir = tmp_path / "icons" / "com.example.app"
    icon_dir.mkdir(parents=True, exist_ok=True)
    (icon_dir / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\nfakeicondata")

    resp = client.get(route)
    assert resp.status_code == 200


def test_login_rejects_wrong_password(client):
    resp = client.post("/api/login", json={"password": "definitely-not-the-password"})
    assert resp.status_code == 401
    body = json.loads(resp.data)
    assert body["success"] is False
    # No session cookie is set on a failed login: session is never mutated,
    # so Flask never sends a Set-Cookie header for it.
    assert "Set-Cookie" not in resp.headers


def test_login_accepts_correct_password(client):
    resp = client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    # The session cookie set by login must be enough for a subsequent
    # mutating request to succeed on the same client.
    resp = client.post("/api/update-source", json={"name": "Post-login rename"})
    assert resp.status_code == 200


def test_logout_clears_session(authed_client):
    resp = authed_client.post("/api/update-source", json={"name": "Before logout"})
    assert resp.status_code == 200

    resp = authed_client.post("/api/logout")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    resp = authed_client.post("/api/update-source", json={"name": "After logout"})
    assert resp.status_code == 401


def test_session_endpoint_reports_state(client):
    resp = client.get("/api/session")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {"authed": False}

    resp = client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD})
    assert resp.status_code == 200

    resp = client.get("/api/session")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {"authed": True}


def test_version_string_is_escaped():
    """templates/index.html interpolated the latest version string into
    innerHTML without escaping, unlike every sibling field on the
    surrounding lines. Version strings are attacker-supplied and stored,
    so an unescaped one is a stored-XSS path back into an authenticated
    session. Rendering is client-side JS with no JS execution harness in
    this suite, so this is asserted directly against the template source:
    the version expression must be wrapped in escapeHtml(...).
    """
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "templates", "index.html"
    )
    with open(template_path, encoding="utf-8") as f:
        template_source = f.read()
    assert "escapeHtml(app.versions?.[0]?.version || 'N/A')" in template_source
