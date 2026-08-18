"""Tests for the auto-import backend (Plan 048): the app-managed job store,
CRUD + config + run routes, the shared per-job runner, and the run-lock.

No test makes a real network call: `release_ingest.select_candidate` and
`release_ingest.stream_download` are monkeypatched on the reloaded app
module's `release_ingest` alias, and the "download" writes a real tiny IPA
zip to disk so the runner's own `extract_ipa_metadata` / publish path runs
for real -- same approach as tests/test_import_release.py.

The create-if-missing tests use provider "gitlab" so the runner's icon
fallback (GitHub owner avatar) is never reached, keeping every path here
free of network calls without needing to monkeypatch
`source_manager.download_icon_from_url`.

No test starts the real scheduler thread; test 10 asserts that importing
the app module never spawns one (the scheduler is started only from
`app.py`'s `__main__` block).
"""

import hashlib
import importlib
import io
import json
import os
import plistlib
import threading
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


def build_ipa_bytes(bundle_id="com.newapp.example", version="1.0.0", name="New App"):
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
        provider="gitlab",
        project="group/project",
        release_id="1",
        release_tag="v1.0.0",
        release_time="2026-01-01T00:00:00Z",
        asset_id="1",
        asset_name="App.ipa",
        declared_size=None,
        download_url="https://gitlab.example.com/group/project/releases/1/App.ipa",
        auth_host="gitlab.example.com",
    )
    defaults.update(overrides)
    return release_ingest.ReleaseCandidate(**defaults)


def make_fake_select_candidate(candidate=None, exc=None):
    def fake(job, session, tokens, timeout=30):
        if exc is not None:
            raise exc
        return candidate

    return fake


def make_fake_select_candidate_by_job(mapping):
    """Dispatches on `job.id` -- mapping is {job_id: candidate_or_Exception}."""

    def fake(job, session, tokens, timeout=30):
        outcome = mapping[job.id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return fake


def make_fake_stream_download(ipa_bytes):
    def fake(session, candidate, job, dest_path, tokens, timeout, max_bytes, progress_cb=None):
        with open(dest_path, "wb") as fh:
            fh.write(ipa_bytes)
        return len(ipa_bytes), hashlib.sha256(ipa_bytes).hexdigest()

    return fake


VALID_GITHUB_JOB = {
    "id": "anymex",
    "provider": "github",
    "project": "RyanYuuki/AnymeX",
    "bundleIdentifier": "com.ryan.anymex",
    "assetGlob": "*.ipa",
    "includePrereleases": False,
    "createIfMissing": False,
    "name": None,
    "developerName": None,
    "allowedDownloadHosts": [],
}


def valid_gitlab_create_job(job_id="glapp"):
    return {
        "id": job_id,
        "provider": "gitlab",
        "project": "group/project",
        "bundleIdentifier": "com.newapp.example",
        "assetGlob": "*.ipa",
        "includePrereleases": False,
        "createIfMissing": True,
        "name": "New App",
        "developerName": "New Dev",
        "allowedDownloadHosts": ["gitlab.example.com"],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_auto_import_empty_by_default(authed_client):
    resp = authed_client.get("/api/auto-import")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is False
    assert data["jobs"] == []


def test_auto_import_add_and_list_job(authed_client):
    resp = authed_client.post("/api/auto-import/job", json=VALID_GITHUB_JOB)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["success"] is True

    resp = authed_client.get("/api/auto-import")
    jobs = resp.get_json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["id"] == "anymex"
    assert jobs[0]["project"] == "RyanYuuki/AnymeX"
    assert jobs[0]["enabled"] is True


def test_auto_import_rejects_bad_job(authed_client):
    bad_provider = dict(VALID_GITHUB_JOB, id="bad1", provider="svn")
    resp = authed_client.post("/api/auto-import/job", json=bad_provider)
    assert resp.status_code == 400
    assert "error" in resp.get_json()

    bad_project = dict(VALID_GITHUB_JOB, id="bad2", project="not-a-valid-project-ref")
    resp = authed_client.post("/api/auto-import/job", json=bad_project)
    assert resp.status_code == 400
    assert "error" in resp.get_json()

    bad_gitlab = dict(
        VALID_GITHUB_JOB, id="bad3", provider="gitlab", project="group/project",
        allowedDownloadHosts=[],
    )
    resp = authed_client.post("/api/auto-import/job", json=bad_gitlab)
    assert resp.status_code == 400
    assert "error" in resp.get_json()

    resp = authed_client.get("/api/auto-import")
    assert resp.get_json()["jobs"] == []


def test_auto_import_update_and_delete_job(authed_client):
    resp = authed_client.post("/api/auto-import/job", json=VALID_GITHUB_JOB)
    assert resp.status_code == 200

    updated = dict(VALID_GITHUB_JOB, bundleIdentifier="com.ryan.anymex2")
    resp = authed_client.post("/api/auto-import/job", json=updated)
    assert resp.status_code == 200

    resp = authed_client.get("/api/auto-import")
    jobs = resp.get_json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["bundleIdentifier"] == "com.ryan.anymex2"

    resp = authed_client.post("/api/auto-import/job/delete", json={"id": "anymex"})
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    resp = authed_client.get("/api/auto-import")
    assert resp.get_json()["jobs"] == []

    resp = authed_client.post("/api/auto-import/job/delete", json={"id": "anymex"})
    assert resp.status_code == 404


def test_auto_import_config_bounds(authed_client):
    resp = authed_client.post("/api/auto-import/config", json={"intervalHours": 0})
    assert resp.status_code == 400

    resp = authed_client.post("/api/auto-import/config", json={"intervalHours": 999})
    assert resp.status_code == 400

    resp = authed_client.post("/api/auto-import/config", json={"intervalHours": 12, "enabled": True})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["intervalHours"] == 12
    assert data["enabled"] is True

    resp = authed_client.get("/api/auto-import")
    data = resp.get_json()
    assert data["intervalHours"] == 12
    assert data["enabled"] is True


def test_auto_import_run_publishes(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    job = valid_gitlab_create_job("glapp")
    resp = authed_client.post("/api/auto-import/job", json=job)
    assert resp.status_code == 200

    ipa_bytes = build_ipa_bytes(bundle_id="com.newapp.example", version="1.0.0")
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    mp = pytest.MonkeyPatch()
    mp.setattr(release_ingest, "select_candidate", make_fake_select_candidate(candidate))
    mp.setattr(release_ingest, "stream_download", make_fake_stream_download(ipa_bytes))
    try:
        resp = authed_client.post("/api/auto-import/run", json={"id": "glapp"})
    finally:
        mp.undo()

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "done"
    assert body["results"]["glapp"]["status"] == "published"
    assert body["results"]["glapp"]["version"] == "1.0.0"

    app_info = app_module.source_manager.get_app("com.newapp.example")
    assert app_info is not None
    versions = [v["version"] for v in app_info["versions"]]
    assert versions == ["1.0.0"]

    resp = authed_client.get("/api/auto-import")
    stored_job = resp.get_json()["jobs"][0]
    assert stored_job["lastResult"]["status"] == "published"
    assert stored_job["lastRunAt"] is not None


def test_auto_import_run_skips_existing_version(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    # Reuse the already-seeded app/version: com.example.app @ 1.0.0.
    job = {
        "id": "existing",
        "provider": "github",
        "project": "owner/repo",
        "bundleIdentifier": "com.example.app",
        "assetGlob": "*.ipa",
        "includePrereleases": False,
        "createIfMissing": False,
        "name": None,
        "developerName": None,
        "allowedDownloadHosts": [],
    }
    resp = authed_client.post("/api/auto-import/job", json=job)
    assert resp.status_code == 200

    ipa_bytes = build_ipa_bytes(bundle_id="com.example.app", version="1.0.0")
    candidate = make_candidate(release_ingest, provider="github", project="owner/repo",
                                declared_size=len(ipa_bytes))

    mp = pytest.MonkeyPatch()
    mp.setattr(release_ingest, "select_candidate", make_fake_select_candidate(candidate))
    mp.setattr(release_ingest, "stream_download", make_fake_stream_download(ipa_bytes))
    try:
        resp = authed_client.post("/api/auto-import/run", json={"id": "existing"})
    finally:
        mp.undo()

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["results"]["existing"]["status"] == "skipped"

    app_info = app_module.source_manager.get_app("com.example.app")
    versions = [v["version"] for v in app_info["versions"]]
    assert versions == ["1.0.0"]  # not duplicated


def test_auto_import_run_one_bad_job_isolated(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    good_job = valid_gitlab_create_job("good")
    bad_job = valid_gitlab_create_job("bad")
    bad_job["bundleIdentifier"] = "com.bad.example"

    for job in (good_job, bad_job):
        resp = authed_client.post("/api/auto-import/job", json=job)
        assert resp.status_code == 200

    ipa_bytes = build_ipa_bytes(bundle_id="com.newapp.example", version="1.0.0")
    good_candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    mapping = {
        "good": good_candidate,
        "bad": RuntimeError("provider exploded"),
    }

    mp = pytest.MonkeyPatch()
    mp.setattr(release_ingest, "select_candidate", make_fake_select_candidate_by_job(mapping))
    mp.setattr(release_ingest, "stream_download", make_fake_stream_download(ipa_bytes))
    try:
        resp = authed_client.post("/api/auto-import/run", json={})
    finally:
        mp.undo()

    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert results["good"]["status"] == "published"
    assert results["bad"]["status"] == "error"
    assert "provider exploded" in results["bad"]["message"]

    app_info = app_module.source_manager.get_app("com.newapp.example")
    assert app_info is not None


def test_auto_import_routes_require_auth(client):
    resp = client.get("/api/auto-import")
    assert resp.status_code == 401

    resp = client.post("/api/auto-import/config", json={"intervalHours": 6})
    assert resp.status_code == 401

    resp = client.post("/api/auto-import/job", json=VALID_GITHUB_JOB)
    assert resp.status_code == 401

    resp = client.post("/api/auto-import/job/delete", json={"id": "anymex"})
    assert resp.status_code == 401

    resp = client.post("/api/auto-import/run", json={})
    assert resp.status_code == 401


def test_importing_app_module_starts_no_thread(client):
    # `client` already imports/reloads the app module (as the whole suite
    # does); the scheduler must only ever start from `__main__`.
    names = [t.name for t in threading.enumerate()]
    assert not any("auto-import" in n.lower() for n in names), names
