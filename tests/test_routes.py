"""Smoke-test suite for the AltStore Source Manager Flask app.

Covers all 13 routes in app.py with a fresh, isolated data directory per
test (via `tmp_path`). No test reads or writes the real `data/` directory
and no test makes real network requests.

See plans/005-smoke-test-suite.md for the design rationale.
"""

import json
import os
import importlib

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
