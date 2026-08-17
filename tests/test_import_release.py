"""Tests for POST /api/import-release (Plan 034).

No test makes a real network call: `release_ingest.select_candidate` and
`release_ingest.stream_download` are monkeypatched on the reloaded app
module's `release_ingest` alias, and the "download" writes a real tiny IPA
zip to disk so the route's own `extract_ipa_metadata` / publish path runs
for real. Fixture pattern copied from tests/test_routes.py:73-107 (no
conftest.py in this repo, see tests/test_storage.py:88-91 for the same
note).

IMPORTANT: the route streams its NDJSON body lazily -- the Flask test
client's response body isn't actually consumed until `resp.get_data()` (or
similar) is called. Monkeypatches must therefore stay active until the body
has been read, not just until `.post()` returns. `post_and_collect` below
reads the body while the patch is still applied.
"""

import hashlib
import importlib
import io
import json
import os
import plistlib
import zipfile

import pytest


TEST_ADMIN_PASSWORD = "test-password-not-a-real-secret"
TEST_SECRET_KEY = "test-secret-key"


def seed_source():
    """Matches tests/test_routes.py's seed_source(): one app, one version."""
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
    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["ADMIN_PASSWORD"] = TEST_ADMIN_PASSWORD
    os.environ["SECRET_KEY"] = TEST_SECRET_KEY

    import app as app_module
    importlib.reload(app_module)

    (tmp_path / "source.json").write_text(json.dumps(seed_source()))

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        c.app_module = app_module
        yield c


@pytest.fixture(scope="function")
def authed_client(client):
    resp = client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD})
    assert resp.status_code == 200
    return client


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def build_ipa_bytes(bundle_id="com.example.app", version="1.0.0", name="Test App"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        plist = {
            "CFBundleIdentifier": bundle_id,
            "CFBundleShortVersionString": version,
            "CFBundleDisplayName": name,
        }
        zf.writestr(
            "Payload/App.app/Info.plist", plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)
        )
    return buf.getvalue()


def make_candidate(release_ingest, **overrides):
    defaults = dict(
        provider="github",
        project="owner/repo",
        release_id="1",
        release_tag="v1.0.0",
        release_time="2026-01-01T00:00:00Z",
        asset_id="1",
        asset_name="App.ipa",
        declared_size=None,
        download_url="https://api.github.com/repos/owner/repo/releases/assets/1",
        auth_host="api.github.com",
    )
    defaults.update(overrides)
    return release_ingest.ReleaseCandidate(**defaults)


def make_fake_select_candidate(candidate=None, exc=None):
    def fake(job, session, tokens, timeout=30):
        if exc is not None:
            raise exc
        return candidate

    return fake


def make_fake_stream_download(ipa_bytes):
    def fake(session, candidate, job, dest_path, tokens, timeout, max_bytes, progress_cb=None):
        with open(dest_path, "wb") as fh:
            fh.write(ipa_bytes)
        if progress_cb is not None:
            progress_cb(len(ipa_bytes), candidate.declared_size)
        return len(ipa_bytes), hashlib.sha256(ipa_bytes).hexdigest()

    return fake


def post_and_collect(client, payload, patches):
    """POST to /api/import-release with `patches` (a dict of attr name ->
    replacement) applied to `client.app_module.release_ingest`, and return
    the parsed list of NDJSON events.

    Crucial: the route's body is a lazy generator, so the patches must stay
    applied until the body is fully read (`resp.get_data()`), not just
    until `.post()` returns -- otherwise the real network functions run.
    """
    release_ingest = client.app_module.release_ingest
    mp = pytest.MonkeyPatch()
    for attr, replacement in patches.items():
        mp.setattr(release_ingest, attr, replacement)
    try:
        resp = client.post("/api/import-release", json=payload)
        text = resp.get_data(as_text=True)
        status = resp.status_code
    finally:
        mp.undo()

    events = [json.loads(line) for line in text.strip().split("\n") if line.strip()]
    return status, events


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_import_existing_app_adds_version(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.example.app", version="2.0.0")
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "owner/repo",
            "bundleIdentifier": "",
            "createIfMissing": False,
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    assert events, "expected at least one NDJSON event"
    last = events[-1]
    assert last["stage"] == "done", events
    assert last["bundleIdentifier"] == "com.example.app"
    assert last["version"] == "2.0.0"

    app_info = app_module.source_manager.get_app("com.example.app")
    versions = [v["version"] for v in app_info["versions"]]
    assert "2.0.0" in versions
    assert "1.0.0" in versions


def test_import_new_app_requires_create_flag(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.new.app", version="1.0.0")
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "owner/repo",
            "bundleIdentifier": "",
            "createIfMissing": False,
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "error", events

    assert app_module.source_manager.get_app("com.new.app") is None


def test_import_new_app_with_create_flag_and_metadata(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.new.app", version="1.0.0")
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "owner/repo",
            "bundleIdentifier": "",
            "createIfMissing": True,
            "name": "New App",
            "developerName": "New Dev",
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events
    assert last["bundleIdentifier"] == "com.new.app"

    app_info = app_module.source_manager.get_app("com.new.app")
    assert app_info is not None
    assert app_info["name"] == "New App"
    assert app_info["developerName"] == "New Dev"


def test_import_bundle_mismatch_is_rejected(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.other.app", version="1.0.0")
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "owner/repo",
            "bundleIdentifier": "com.example.app",
            "createIfMissing": False,
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "error", events

    # Neither the mismatched bundle nor the entered one gained anything.
    assert app_module.source_manager.get_app("com.other.app") is None
    app_info = app_module.source_manager.get_app("com.example.app")
    assert len(app_info["versions"]) == 1


def test_import_gitlab_requires_allowed_hosts(authed_client):
    def boom(*args, **kwargs):
        raise AssertionError("select_candidate must not be called without allowed hosts")

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "gitlab",
            "project": "owner/repo",
            "allowedDownloadHosts": [],
        },
        {"select_candidate": boom},
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "error", events
    assert "allowed download host" in last["error"].lower()


def test_import_provider_error_is_reported(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "owner/repo",
        },
        {
            "select_candidate": make_fake_select_candidate(
                exc=release_ingest.ProviderError("no matching release")
            )
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "error", events
    assert "no matching release" in last["error"]
