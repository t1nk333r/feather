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
import time
import zipfile
from datetime import datetime, timezone

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
    """`select_candidate` returns a list -- at most one candidate per
    platform (plan 083). `candidate` may be a single ReleaseCandidate (
    wrapped into a one-item list) or an already-built list/tuple (for tests
    driving more than one platform from a single job)."""

    def fake(job, session, tokens, timeout=30):
        if exc is not None:
            raise exc
        if candidate is None:
            return []
        if isinstance(candidate, (list, tuple)):
            return list(candidate)
        return [candidate]

    return fake


def make_fake_select_candidate_by_job(mapping):
    """Dispatches on `job.id` -- mapping is {job_id: candidate_or_Exception}.
    A bare candidate is wrapped into a one-item list to match
    `select_candidate`'s list contract; a list/tuple value is passed through."""

    def fake(job, session, tokens, timeout=30):
        outcome = mapping[job.id]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, (list, tuple)):
            return list(outcome)
        return [outcome]

    return fake


def make_fake_stream_download(ipa_bytes):
    def fake(session, candidate, job, dest_path, tokens, timeout, max_bytes, progress_cb=None):
        with open(dest_path, "wb") as fh:
            fh.write(ipa_bytes)
        return len(ipa_bytes), hashlib.sha256(ipa_bytes).hexdigest()

    return fake


def make_fake_stream_download_by_platform(content_by_platform):
    """Like make_fake_stream_download, but writes different bytes depending
    on which platform's candidate is being downloaded -- needed once a job
    matches both an .ipa and an .apk in the same run."""

    def fake(session, candidate, job, dest_path, tokens, timeout, max_bytes, progress_cb=None):
        content = content_by_platform[candidate.platform]
        with open(dest_path, "wb") as fh:
            fh.write(content)
        return len(content), hashlib.sha256(content).hexdigest()

    return fake


class _FakeApk:
    """Stand-in for pyaxmlparser.APK, mirroring tests/test_apk_inspection.py
    and tests/test_android.py's fake_inspect -- a real binary
    AndroidManifest.xml cannot be authored by hand in a test."""

    def __init__(self, package="com.cross.app.android", version_code="7",
                 version_name="1.7", min_sdk=21, target_sdk=34,
                 app_name="Cross App", valid=True):
        self.package = package
        self.version_code = version_code
        self.version_name = version_name
        self._min_sdk = min_sdk
        self._target_sdk = target_sdk
        self._app_name = app_name
        self._valid = valid

    def is_valid_APK(self):
        return self._valid

    def is_signed(self):
        return True

    def get_min_sdk_version(self):
        return self._min_sdk

    def get_target_sdk_version(self):
        return self._target_sdk

    def get_app_name(self):
        return self._app_name


def patch_pyaxmlparser(monkeypatch, **fake_kwargs):
    """Patch the one point both release_ingest.inspect_apk_metadata (via
    scripts/apk_inspection.inspect_apk) and android_repo.add_apk (via
    app.py's _inspect_apk, the same shared module) read APK metadata from,
    so a single patch covers the whole watcher pipeline."""
    import pyaxmlparser

    def fake_ctor(path):
        return _FakeApk(**fake_kwargs)

    monkeypatch.setattr(pyaxmlparser, "APK", fake_ctor)


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


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"enabled": False}, False),
        ({"enabled": True}, True),
        ({"enabled": True, "intervalHours": 6, "lastRunAt": "garbage"}, True),
        ({"enabled": True, "intervalHours": 6, "lastRunAt": "2026-01-01T09:00:00+00:00"}, False),
        ({"enabled": True, "intervalHours": 6, "lastRunAt": "2026-01-01T06:00:00+00:00"}, True),
        ({"enabled": True, "intervalHours": 6, "lastRunAt": "2026-01-01T06:00:00"}, True),
        ({"enabled": True, "lastRunAt": "2026-01-01T05:59:59+00:00"}, True),
    ],
)
def test_auto_import_due_table(client, data, expected):
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    assert client.app_module._auto_import_due(data, now) is expected


def test_scheduler_cycle_handles_missing_and_configured_base_url(client, monkeypatch, caplog):
    app_module = client.app_module
    app_module.save_auto_import({
        "enabled": True, "intervalHours": 6, "lastRunAt": None, "jobs": []
    })
    calls = []
    monkeypatch.setattr(app_module, "_run_all_enabled_jobs", calls.append)

    monkeypatch.setattr(app_module, "_safe_base_url", lambda: None)
    with caplog.at_level("WARNING"):
        assert app_module._auto_import_scheduler_cycle() == 6
    assert calls == []
    assert "PUBLIC_BASE_URL" in caplog.text

    monkeypatch.setattr(app_module, "_safe_base_url", lambda: "https://feather.example")
    app_module._auto_import_scheduler_cycle()
    assert calls == ["https://feather.example"]


def test_admin_edit_survives_concurrent_store_mutation(authed_client):
    app_module = authed_client.app_module
    assert authed_client.post("/api/auto-import/job", json=VALID_GITHUB_JOB).status_code == 200
    callback_started = threading.Event()

    def slow_stamp(data):
        callback_started.set()
        time.sleep(0.1)
        data["lastRunAt"] = datetime.now(timezone.utc).isoformat()

    worker = threading.Thread(target=app_module.mutate_auto_import, args=(slow_stamp,))
    worker.start()
    assert callback_started.wait(timeout=1)
    second = dict(VALID_GITHUB_JOB, id="second", bundleIdentifier="com.example.second")
    response = authed_client.post("/api/auto-import/job", json=second)
    worker.join(timeout=2)

    assert response.status_code == 200
    stored = authed_client.get("/api/auto-import").get_json()
    assert {job["id"] for job in stored["jobs"]} == {"anymex", "second"}
    assert stored["lastRunAt"] is not None


# ---------------------------------------------------------------------------
# Plan 084: watcher support for Android (.apk) candidates
# ---------------------------------------------------------------------------


def android_only_job(job_id="android-only", package="com.cross.app.android"):
    return {
        "id": job_id,
        "provider": "gitlab",
        "project": "group/project",
        "bundleIdentifier": None,
        "package": package,
        "assetGlob": "*.apk",
        "includePrereleases": False,
        "createIfMissing": False,
        "name": None,
        "developerName": None,
        "allowedDownloadHosts": ["gitlab.example.com"],
    }


def cross_platform_job(job_id="crossplat"):
    return {
        "id": job_id,
        "provider": "gitlab",
        "project": "group/project",
        "bundleIdentifier": "com.cross.app",
        "package": "com.cross.app.android",
        "assetGlob": "App*",
        "includePrereleases": False,
        "createIfMissing": True,
        "name": "Cross App",
        "developerName": "Cross Dev",
        "allowedDownloadHosts": ["gitlab.example.com"],
    }


def test_auto_import_job_with_only_package_validates_and_runs(authed_client, monkeypatch, tmp_path):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    resp = authed_client.post("/api/auto-import/job", json=android_only_job())
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["job"]["bundleIdentifier"] is None
    assert resp.get_json()["job"]["package"] == "com.cross.app.android"

    apk_bytes = b"fake-apk-bytes-android-only"
    candidate = release_ingest.ReleaseCandidate(
        provider="gitlab", project="group/project", release_id="1", release_tag="v1.0.0",
        release_time="2026-01-01T00:00:00Z", asset_id="1", asset_name="App.apk",
        declared_size=len(apk_bytes), download_url="https://gitlab.example.com/dl/App.apk",
        auth_host="gitlab.example.com", platform="android",
    )
    patch_pyaxmlparser(monkeypatch, package="com.cross.app.android", version_code="7", version_name="1.7")

    mp = pytest.MonkeyPatch()
    mp.setattr(release_ingest, "select_candidate", make_fake_select_candidate(candidate))
    mp.setattr(release_ingest, "stream_download", make_fake_stream_download(apk_bytes))
    try:
        resp = authed_client.post("/api/auto-import/run", json={"id": "android-only"})
    finally:
        mp.undo()

    assert resp.status_code == 200
    result = resp.get_json()["results"]["android-only"]
    assert result["status"] == "published"
    assert result["package"] == "com.cross.app.android"
    assert result["versionCode"] == 7
    assert result["platforms"]["android"]["status"] == "published"
    assert "ios" not in result["platforms"]

    # The artifact actually reached android_repo, not just a success flag.
    apk_path = tmp_path / "fdroid" / "repo" / "com.cross.app.android_7.apk"
    assert apk_path.exists()


def test_auto_import_bundle_identifier_only_job_unchanged(authed_client):
    """Regression: a job configured exactly like every job stored before
    plan 084 (bundleIdentifier only, no package) validates and stores with
    package == None, and the store round-trips the pre-084 fields
    unchanged."""
    resp = authed_client.post("/api/auto-import/job", json=VALID_GITHUB_JOB)
    assert resp.status_code == 200, resp.get_json()
    stored = resp.get_json()["job"]
    assert stored["bundleIdentifier"] == "com.ryan.anymex"
    assert stored["package"] is None

    resp = authed_client.get("/api/auto-import")
    jobs = resp.get_json()["jobs"]
    assert jobs[0]["bundleIdentifier"] == "com.ryan.anymex"
    assert jobs[0]["package"] is None


def test_auto_import_job_accepts_neither_identity_field(authed_client):
    """Both identity fields are optional -- each is auto-detected from the
    artifact (bundleIdentifier from the IPA's Info.plist, package from the
    APK's manifest). A job that supplies neither must save and be stored
    with both as None, not rejected."""
    job = dict(VALID_GITHUB_JOB, id="neither", bundleIdentifier="", package="")
    resp = authed_client.post("/api/auto-import/job", json=job)
    assert resp.status_code == 200, resp.get_json()

    stored = authed_client.get("/api/auto-import").get_json()["jobs"]
    assert len(stored) == 1
    assert stored[0]["id"] == "neither"
    assert stored[0]["bundleIdentifier"] is None
    assert stored[0]["package"] is None


def test_auto_import_job_still_rejects_a_malformed_package(authed_client):
    """Dropping the requirement must not drop the format check: a package
    that is set but not a valid Android package name is still refused."""
    bad = dict(VALID_GITHUB_JOB, id="badpkg", bundleIdentifier="", package="not a package!")
    resp = authed_client.post("/api/auto-import/job", json=bad)
    assert resp.status_code == 400
    assert "package" in resp.get_json()["error"].lower()
    assert authed_client.get("/api/auto-import").get_json()["jobs"] == []


def test_auto_import_run_publishes_both_platforms(authed_client, monkeypatch, tmp_path):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    resp = authed_client.post("/api/auto-import/job", json=cross_platform_job())
    assert resp.status_code == 200, resp.get_json()

    ipa_bytes = build_ipa_bytes(bundle_id="com.cross.app", version="2.0.0", name="Cross App")
    apk_bytes = b"fake-apk-bytes-crossplat"

    ios_candidate = make_candidate(
        release_ingest, asset_id="1", asset_name="App.ipa",
        declared_size=len(ipa_bytes), platform="ios",
    )
    android_candidate = make_candidate(
        release_ingest, asset_id="2", asset_name="App.apk",
        declared_size=len(apk_bytes), platform="android",
    )
    patch_pyaxmlparser(monkeypatch, package="com.cross.app.android", version_code="7", version_name="1.7")

    mp = pytest.MonkeyPatch()
    mp.setattr(release_ingest, "select_candidate",
               make_fake_select_candidate([ios_candidate, android_candidate]))
    mp.setattr(release_ingest, "stream_download",
               make_fake_stream_download_by_platform({"ios": ipa_bytes, "android": apk_bytes}))
    try:
        resp = authed_client.post("/api/auto-import/run", json={"id": "crossplat"})
    finally:
        mp.undo()

    assert resp.status_code == 200
    result = resp.get_json()["results"]["crossplat"]
    assert result["status"] == "published"
    assert result["platforms"]["ios"]["status"] == "published"
    assert result["platforms"]["ios"]["version"] == "2.0.0"
    assert result["platforms"]["android"]["status"] == "published"
    assert result["platforms"]["android"]["versionName"] == "1.7"

    # Both artifacts actually reached their respective managers -- not just
    # a top-level "ok".
    app_info = app_module.source_manager.get_app("com.cross.app")
    assert app_info is not None
    assert "2.0.0" in [v["version"] for v in app_info["versions"]]

    apk_path = tmp_path / "fdroid" / "repo" / "com.cross.app.android_7.apk"
    assert apk_path.exists()

    stored_job = authed_client.get("/api/auto-import").get_json()["jobs"][0]
    assert stored_job["lastResult"]["platforms"]["ios"]["status"] == "published"
    assert stored_job["lastResult"]["platforms"]["android"]["status"] == "published"


def test_auto_import_android_rerun_already_present_is_skip_not_failure(authed_client, monkeypatch, tmp_path):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    resp = authed_client.post("/api/auto-import/job", json=android_only_job(job_id="rerun-android"))
    assert resp.status_code == 200

    apk_bytes = b"fake-apk-bytes-rerun"
    candidate = release_ingest.ReleaseCandidate(
        provider="gitlab", project="group/project", release_id="1", release_tag="v1.0.0",
        release_time="2026-01-01T00:00:00Z", asset_id="1", asset_name="App.apk",
        declared_size=len(apk_bytes), download_url="https://gitlab.example.com/dl/App.apk",
        auth_host="gitlab.example.com", platform="android",
    )
    patch_pyaxmlparser(monkeypatch, package="com.cross.app.android", version_code="9", version_name="1.9")

    def run_once():
        mp = pytest.MonkeyPatch()
        mp.setattr(release_ingest, "select_candidate", make_fake_select_candidate(candidate))
        mp.setattr(release_ingest, "stream_download", make_fake_stream_download(apk_bytes))
        try:
            return authed_client.post("/api/auto-import/run", json={"id": "rerun-android"})
        finally:
            mp.undo()

    first = run_once()
    assert first.status_code == 200
    first_result = first.get_json()["results"]["rerun-android"]
    assert first_result["status"] == "published"

    second = run_once()
    assert second.status_code == 200
    second_result = second.get_json()["results"]["rerun-android"]
    assert second_result["status"] == "skipped"
    assert second_result["platforms"]["android"]["status"] == "skipped"
    assert "error" not in second_result

    apk_path = tmp_path / "fdroid" / "repo" / "com.cross.app.android_9.apk"
    assert apk_path.exists()


# ---------------------------------------------------------------------------
# Regex asset match mode (plan 085)
# ---------------------------------------------------------------------------


def test_asset_match_mode_round_trips_through_save_and_read(authed_client):
    """assetMatchMode must be in _validate_auto_import_job's returned dict --
    a field absent there is silently dropped and the setting would not
    persist. Assert the round trip, not by eye."""
    job = dict(VALID_GITHUB_JOB, assetGlob=r".*arm64.*\.apk", assetMatchMode="regex")
    resp = authed_client.post("/api/auto-import/job", json=job)
    assert resp.status_code == 200, resp.get_json()

    resp = authed_client.get("/api/auto-import")
    stored = resp.get_json()["jobs"][0]
    assert stored["assetMatchMode"] == "regex"
    assert stored["assetGlob"] == r".*arm64.*\.apk"


def test_asset_match_mode_defaults_to_glob_when_saved_without_it(authed_client):
    job = dict(VALID_GITHUB_JOB)
    assert "assetMatchMode" not in job
    resp = authed_client.post("/api/auto-import/job", json=job)
    assert resp.status_code == 200, resp.get_json()

    stored = authed_client.get("/api/auto-import").get_json()["jobs"][0]
    assert stored["assetMatchMode"] == "glob"


def test_invalid_regex_asset_glob_is_refused_at_save_with_readable_message(authed_client):
    job = dict(VALID_GITHUB_JOB, assetGlob="(unterminated", assetMatchMode="regex")
    resp = authed_client.post("/api/auto-import/job", json=job)
    assert resp.status_code == 400
    error = resp.get_json()["error"]
    assert "assetGlob" in error
    assert "not a valid regular expression" in error

    # Never saved -- the bad job must not appear in the store.
    assert authed_client.get("/api/auto-import").get_json()["jobs"] == []


def test_asset_pattern_over_200_chars_refused_at_save(authed_client):
    job = dict(VALID_GITHUB_JOB, assetGlob="a" * 201, assetMatchMode="regex")
    resp = authed_client.post("/api/auto-import/job", json=job)
    assert resp.status_code == 400
    assert "too long" in resp.get_json()["error"]


def test_unknown_asset_match_mode_is_refused_at_save(authed_client):
    job = dict(VALID_GITHUB_JOB, assetMatchMode="wildcard")
    resp = authed_client.post("/api/auto-import/job", json=job)
    assert resp.status_code == 400
    assert "assetMatchMode" in resp.get_json()["error"]


def test_stored_job_with_no_asset_match_mode_behaves_as_glob(authed_client):
    """The test that protects data/auto-import.json: every job stored there
    today has no assetMatchMode key at all. Writing directly to the store
    (bypassing _validate_auto_import_job, which would insert a default)
    reproduces that exact on-disk shape."""
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    # Parentheses are literal in glob (fnmatch has no group syntax) but
    # meaningful in regex -- a genuine behavioral discriminator between
    # the two modes, not just a check that a field has some string value.
    job = dict(VALID_GITHUB_JOB, id="legacy", assetGlob="App(1).ipa")
    assert "assetMatchMode" not in job
    app_module.save_auto_import(
        {"enabled": False, "intervalHours": 6, "lastRunAt": None, "jobs": [job]}
    )

    captured = {}

    def capture_and_reject(job_obj, *a, **k):
        captured["job_obj"] = job_obj
        raise release_ingest.ProviderError("no releases (test stub)")

    mp = pytest.MonkeyPatch()
    mp.setattr(release_ingest, "select_candidate", capture_and_reject)
    try:
        resp = authed_client.post("/api/auto-import/run", json={"id": "legacy"})
    finally:
        mp.undo()

    assert resp.status_code == 200
    job_obj = captured.get("job_obj")
    assert job_obj is not None
    assert job_obj.asset_match_mode == "glob"
    state = release_ingest._asset_match_state(job_obj, "App(1).ipa")
    assert state["matched"] is True
