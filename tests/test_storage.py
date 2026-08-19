"""Tests for the IPA storage abstraction (Plan 011): LocalIpaStorage,
GarageIpaStorage, and their wiring through serve_ipa and update_version.

No test in this file makes a real network call. Garage-backend tests
inject a hand-rolled fake S3 client (FakeS3Client below) into
GarageIpaStorage instead of exercising boto3/Garage for real -- the
existing suite runs in ~1s with no network and that property is worth
protecting (see plans/011-garage-s3-ipa-storage.md's test plan).

Reuses the `client` fixture pattern from tests/test_routes.py:54-80.
"""

import http.server
import importlib
import io
import json
import os
import os as _os
import threading
from unittest import mock

import pytest
from botocore.exceptions import ClientError


# Obviously-fake credentials -- never a real password. Plan 010: app.py now
# refuses to import at all without ADMIN_PASSWORD set, and a stable
# SECRET_KEY is needed for a login session to survive across requests.
TEST_ADMIN_PASSWORD = "test-password-not-a-real-secret"
TEST_SECRET_KEY = "test-secret-key"


# ---------------------------------------------------------------------------
# Fake S3 client -- records calls, keeps objects in memory, never touches
# the network. Good enough to exercise GarageIpaStorage's put / exists /
# delete / post-upload-verify logic exactly the way real boto3 calls would.
# ---------------------------------------------------------------------------


class FakeS3Client:
    def __init__(self):
        self.objects = {}  # (bucket, key) -> size in bytes
        self.calls = []
        self.fail_uploads = False
        self.head_size_override = None
        # Plan 050: simulate the exact bug the storage self-test pins --
        # a 403 on head_object (a read), independent of whether the
        # object was actually written.
        self.fail_head_forbidden = False

    def _upload_error(self):
        return ClientError(
            {
                "Error": {"Code": "InternalError", "Message": "simulated upload failure"},
                "ResponseMetadata": {"HTTPStatusCode": 500},
            },
            "PutObject",
        )

    def _forbidden_error(self, operation_name):
        return ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "simulated permission denial"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            operation_name,
        )

    def put_object(self, Bucket, Key, Body=None, **kwargs):
        self.calls.append(("put_object", Bucket, Key))
        if self.fail_uploads:
            raise self._upload_error()
        size = len(Body) if Body is not None else 0
        self.objects[(Bucket, Key)] = size

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.calls.append(("upload_file", path, bucket, key, ExtraArgs))
        if self.fail_uploads:
            raise self._upload_error()
        self.objects[(bucket, key)] = os.path.getsize(path)

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None):
        self.calls.append(("upload_fileobj", bucket, key, ExtraArgs))
        if self.fail_uploads:
            raise self._upload_error()
        data = fileobj.read()
        self.objects[(bucket, key)] = len(data)

    def head_object(self, Bucket, Key):
        if self.fail_head_forbidden:
            raise self._forbidden_error("HeadObject")
        size = self.objects.get((Bucket, Key))
        if size is None:
            raise ClientError(
                {
                    "Error": {"Code": "404", "Message": "Not Found"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        if self.head_size_override is not None:
            size = self.head_size_override
        return {"ContentLength": size}

    def delete_object(self, Bucket, Key):
        self.calls.append(("delete_object", Bucket, Key))
        self.objects.pop((Bucket, Key), None)
        return {}


@pytest.fixture(scope="function")
def client(tmp_path):
    """Local-backend app instance rooted at an isolated temp data dir.

    Same pattern as tests/test_routes.py:54-80 (not imported from there
    since pytest fixtures aren't shared across test files without a
    conftest.py, and this suite intentionally doesn't add one).
    """
    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["ADMIN_PASSWORD"] = TEST_ADMIN_PASSWORD
    os.environ["SECRET_KEY"] = TEST_SECRET_KEY

    import app as app_module
    importlib.reload(app_module)

    (tmp_path / "source.json").write_text(json.dumps(seed_source_with_app()))

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        c.app_module = app_module
        yield c


GARAGE_ENV = {
    "STORAGE_BACKEND": "garage",
    "GARAGE_S3_ENDPOINT": "https://garage.example.invalid",
    "GARAGE_S3_REGION": "garage",
    "GARAGE_S3_ACCESS_KEY_ID": "test-key-id",
    "GARAGE_S3_SECRET_ACCESS_KEY": "test-secret",
    "GARAGE_BUCKET": "test-bucket",
    "GARAGE_PUBLIC_BASE_URL": "https://garage-web.example.invalid",
}


@pytest.fixture
def garage_client(tmp_path, monkeypatch):
    """Like test_routes.py's `client` fixture, but reloads the app with
    STORAGE_BACKEND=garage and a fake S3 client injected into ipa_storage.
    Uses monkeypatch.setenv so every env var set here is automatically
    reverted at teardown -- a later test's plain `client` fixture (from
    test_routes.py) must see STORAGE_BACKEND back to unset/local.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("SECRET_KEY", TEST_SECRET_KEY)
    for key, value in GARAGE_ENV.items():
        monkeypatch.setenv(key, value)

    import app as app_module
    importlib.reload(app_module)

    fake_client = FakeS3Client()
    app_module.ipa_storage._client = fake_client
    app_module.icon_storage._client = fake_client

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        c.app_module = app_module
        c.fake_client = fake_client
        yield c


def seed_source_with_app():
    """Minimal catalog with one app/version, matching test_routes.py's
    seed_source() fixture shape closely enough for update-version tests.
    """
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
                        "size": 999,
                    }
                ],
            }
        ],
        "news": [],
    }


class _PlainIpaHandler(http.server.BaseHTTPRequestHandler):
    """Serves a fixed, plausible-looking IPA payload -- used to give
    update_version a replacement download that succeeds all the way
    through the local-capture step, so a subsequent storage-backend
    failure can be isolated and tested on its own.
    """

    payload = b"PK\x03\x04 replacement ipa bytes " * 10

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def plain_ipa_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _PlainIpaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/replacement.ipa"
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# LocalIpaStorage
# ---------------------------------------------------------------------------


def test_local_storage_roundtrip(client, tmp_path):
    storage = client.app_module.ipa_storage
    assert isinstance(storage, client.app_module.LocalIpaStorage)

    payload = b"local roundtrip bytes"
    src = tmp_path / "src.ipa"
    src.write_bytes(payload)

    assert storage.exists("com.example.round", "1.0.0") is False

    size = storage.put(str(src), "com.example.round", "1.0.0")
    assert size == len(payload)
    assert storage.exists("com.example.round", "1.0.0") is True

    final_path = os.path.join(str(tmp_path), "ipas", "com.example.round", "1.0.0.ipa")
    assert os.path.exists(final_path)
    assert not src.exists()  # put() commits via an atomic rename

    assert storage.delete("com.example.round", "1.0.0") is True
    assert storage.exists("com.example.round", "1.0.0") is False
    # Deleting again is a clean False, not an exception.
    assert storage.delete("com.example.round", "1.0.0") is False


def test_local_storage_public_url_is_none(client):
    storage = client.app_module.ipa_storage
    assert storage.public_url("com.example.app", "1.0.0") is None


# ---------------------------------------------------------------------------
# GarageIpaStorage
# ---------------------------------------------------------------------------


def test_garage_key_layout(garage_client, tmp_path):
    """put() for bundle com.example.app version 1.0.0 must land at key
    ipas/com.example.app/1.0.0.ipa. Pinned: scripts/migrate_ipas_to_garage.py
    depends on this exact contract to write to the keys serve_ipa expects.
    """
    storage = garage_client.app_module.ipa_storage
    payload = b"garage key layout bytes"
    src = tmp_path / "src.ipa"
    src.write_bytes(payload)

    size = storage.put(str(src), "com.example.app", "1.0.0")
    assert size == len(payload)
    assert ("test-bucket", "ipas/com.example.app/1.0.0.ipa") in garage_client.fake_client.objects


def test_garage_public_url(garage_client, monkeypatch):
    storage = garage_client.app_module.ipa_storage
    url = storage.public_url("com.example.app", "1.0.0")
    assert url == "https://garage-web.example.invalid/ipas/com.example.app/1.0.0.ipa"

    # No double slash when GARAGE_PUBLIC_BASE_URL has a trailing slash.
    monkeypatch.setattr(
        garage_client.app_module, "GARAGE_PUBLIC_BASE_URL",
        "https://garage-web.example.invalid/",
    )
    url2 = storage.public_url("com.example.app", "1.0.0")
    assert url2 == "https://garage-web.example.invalid/ipas/com.example.app/1.0.0.ipa"


def test_garage_backend_refuses_to_start_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("SECRET_KEY", TEST_SECRET_KEY)
    monkeypatch.setenv("STORAGE_BACKEND", "garage")
    # Some vars set (and must never be echoed back)...
    monkeypatch.setenv("GARAGE_S3_ACCESS_KEY_ID", "AKIA_TEST_KEY_ID_DO_NOT_LEAK")
    monkeypatch.setenv("GARAGE_S3_SECRET_ACCESS_KEY", "shhh-do-not-print-me")
    # ...others deliberately left unset.
    for key in ("GARAGE_S3_ENDPOINT", "GARAGE_BUCKET", "GARAGE_PUBLIC_BASE_URL"):
        monkeypatch.delenv(key, raising=False)

    import app as app_module

    with pytest.raises(RuntimeError) as exc_info:
        importlib.reload(app_module)

    message = str(exc_info.value)
    # Names the variables that are actually missing...
    assert "GARAGE_S3_ENDPOINT" in message
    assert "GARAGE_BUCKET" in message
    assert "GARAGE_PUBLIC_BASE_URL" in message
    # ...but never leaks the secret material of variables that ARE set.
    assert "AKIA_TEST_KEY_ID_DO_NOT_LEAK" not in message
    assert "shhh-do-not-print-me" not in message


# ---------------------------------------------------------------------------
# serve_ipa
# ---------------------------------------------------------------------------


def test_serve_ipa_redirects_when_backend_is_garage(garage_client):
    garage_client.fake_client.objects[("test-bucket", "ipas/com.example.app/1.0.0.ipa")] = 1234

    resp = garage_client.get("/ipas/com.example.app/1.0.0.ipa")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://garage-web.example.invalid/ipas/com.example.app/1.0.0.ipa"


def test_serve_ipa_404_when_object_missing(garage_client):
    resp = garage_client.get("/ipas/com.example.app/does-not-exist.ipa")
    assert resp.status_code == 404


def test_serve_ipa_uses_send_file_when_backend_is_local(client, tmp_path):
    ipa_dir = tmp_path / "ipas" / "com.example.app"
    ipa_dir.mkdir(parents=True)
    payload = b"local send_file body"
    (ipa_dir / "1.0.0.ipa").write_bytes(payload)

    resp = client.get("/ipas/com.example.app/1.0.0.ipa")
    assert resp.status_code == 200
    assert resp.data == payload
    assert "Location" not in resp.headers


def test_local_icon_storage_roundtrip(client, tmp_path):
    storage = client.app_module.icon_storage
    src = tmp_path / "icon.webp"
    src.write_bytes(b"icon bytes")
    assert storage.put(str(src), "com.example.icon", "webp") is True
    assert storage.exists("com.example.icon", "webp") is True
    assert (tmp_path / "icons" / "com.example.icon" / "icon.webp").read_bytes() == b"icon bytes"
    assert storage.delete("com.example.icon") is True
    assert storage.exists("com.example.icon", "webp") is False


def test_garage_icon_key_and_content_type(garage_client, tmp_path):
    storage = garage_client.app_module.icon_storage
    src = tmp_path / "icon.png"
    src.write_bytes(b"png bytes")
    assert storage.put(str(src), "com.example.icon", "png") is True
    assert ("test-bucket", "icons/com.example.icon/icon.png") in garage_client.fake_client.objects
    upload = [call for call in garage_client.fake_client.calls if call[0] == "upload_file"][-1]
    assert upload[3] == "icons/com.example.icon/icon.png"
    assert upload[4] == {"ContentType": "image/png"}
    garage_client.fake_client.head_size_override = 999
    replacement = tmp_path / "replacement.png"
    replacement.write_bytes(b"replacement")
    assert storage.put(str(replacement), "com.example.icon", "png") is False
    garage_client.fake_client.head_size_override = None


def test_garage_icon_public_url(garage_client):
    assert garage_client.app_module.icon_storage.public_url("com.example.icon", "webp") == (
        "https://garage-web.example.invalid/icons/com.example.icon/icon.webp"
    )


def test_serve_icon_redirects_for_garage(garage_client):
    garage_client.fake_client.objects[("test-bucket", "icons/com.example.app/icon.png")] = 4
    response = garage_client.get("/icons/com.example.app/icon.png")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/icons/com.example.app/icon.png")


def test_serve_icon_404_when_garage_object_missing(garage_client):
    assert garage_client.get("/icons/com.example.app/icon.png").status_code == 404


def _seed_garage_catalog(app_module):
    path = os.path.join(app_module.DATA_DIR, "source.json")
    with open(path, "w") as f:
        json.dump(seed_source_with_app(), f)
    return path


def test_update_app_icon_upload_writes_garage_and_stable_catalog_url(garage_client):
    app_module = garage_client.app_module
    _seed_garage_catalog(app_module)
    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    response = garage_client.post(
        "/api/update-app",
        data={
            "bundleIdentifier": "com.example.app",
            "name": "Example App",
            "developerName": "Example Dev",
            "iconFile": (io.BytesIO(b"webp bytes"), "icon.webp"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert ("test-bucket", "icons/com.example.app/icon.webp") in garage_client.fake_client.objects
    app_entry = app_module.source_manager.get_app("com.example.app")
    assert app_entry["iconURL"].startswith("http://localhost/icons/")


def test_update_app_icon_replacement_removes_old_extension_after_success(garage_client):
    app_module = garage_client.app_module
    _seed_garage_catalog(app_module)
    garage_client.fake_client.objects[("test-bucket", "icons/com.example.app/icon.png")] = 3
    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    response = garage_client.post(
        "/api/update-app",
        data={"bundleIdentifier": "com.example.app", "iconFile": (io.BytesIO(b"webp"), "icon.webp")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert ("test-bucket", "icons/com.example.app/icon.webp") in garage_client.fake_client.objects
    assert ("test-bucket", "icons/com.example.app/icon.png") not in garage_client.fake_client.objects
    assert app_module.source_manager.get_app("com.example.app")["iconURL"].endswith("icon.webp")


def test_failed_icon_upload_preserves_previous_object_and_catalog_url(garage_client):
    app_module = garage_client.app_module
    source_path = _seed_garage_catalog(app_module)
    source = json.load(open(source_path))
    source["apps"][0]["iconURL"] = "http://localhost/icons/com.example.app/icon.png"
    json.dump(source, open(source_path, "w"))
    garage_client.fake_client.objects[("test-bucket", "icons/com.example.app/icon.png")] = 3
    garage_client.fake_client.fail_uploads = True
    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    response = garage_client.post(
        "/api/update-app",
        data={"bundleIdentifier": "com.example.app", "iconFile": (io.BytesIO(b"new"), "icon.webp")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert garage_client.fake_client.objects[("test-bucket", "icons/com.example.app/icon.png")] == 3
    assert app_module.source_manager.get_app("com.example.app")["iconURL"].endswith("icon.png")


def test_failed_catalog_save_preserves_old_icon_and_catalog_url(garage_client, monkeypatch):
    app_module = garage_client.app_module
    source_path = _seed_garage_catalog(app_module)
    source = json.load(open(source_path))
    source["apps"][0]["iconURL"] = "http://localhost/icons/com.example.app/icon.png"
    json.dump(source, open(source_path, "w"))
    garage_client.fake_client.objects[("test-bucket", "icons/com.example.app/icon.png")] = 3
    monkeypatch.setattr(app_module.source_manager, "save_source", lambda _: False)
    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    response = garage_client.post(
        "/api/update-app",
        data={"bundleIdentifier": "com.example.app", "iconFile": (io.BytesIO(b"new"), "icon.webp")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert garage_client.fake_client.objects[("test-bucket", "icons/com.example.app/icon.png")] == 3
    assert json.load(open(source_path))["apps"][0]["iconURL"].endswith("icon.png")


def test_delete_app_removes_garage_icon(garage_client):
    app_module = garage_client.app_module
    _seed_garage_catalog(app_module)
    garage_client.fake_client.objects[("test-bucket", "icons/com.example.app/icon.png")] = 3
    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    assert garage_client.post("/api/delete-app", json={"bundleIdentifier": "com.example.app"}).status_code == 200
    assert ("test-bucket", "icons/com.example.app/icon.png") not in garage_client.fake_client.objects


# ---------------------------------------------------------------------------
# update_version + Garage: the "never destroy the original" guarantee
# ---------------------------------------------------------------------------


def test_update_version_preserves_original_on_failed_upload(garage_client, plain_ipa_server):
    """Garage analogue of Plan 007's most important test. update_version
    fully captures the replacement to a local temp file first (a real,
    reachable local HTTP stub -- plain_ipa_server -- so that step
    succeeds), and only then calls ipa_storage.put(), which for Garage
    uploads straight to the final key. If that upload itself fails, the
    previously-hosted object must be left exactly as it was and the route
    must report failure, not silently succeed.
    """
    app_module = garage_client.app_module
    key = "ipas/com.example.app/1.0.0.ipa"

    # Seed a catalog with this version already present.
    source_path = os.path.join(app_module.DATA_DIR, "source.json")
    with open(source_path, "w") as f:
        json.dump(seed_source_with_app(), f)

    # Seed the "existing hosted object" directly in the fake backend.
    original_size = 999
    garage_client.fake_client.objects[("test-bucket", key)] = original_size

    garage_client.fake_client.fail_uploads = True

    login_resp = garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD})
    assert login_resp.status_code == 200

    resp = garage_client.post(
        "/api/update-version",
        data={
            "bundleIdentifier": "com.example.app",
            "version": "1.0.0",
            "downloadURL": plain_ipa_server,
            "downloadFromUrl": "true",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False

    # The original object must be untouched -- same size, still there.
    assert garage_client.fake_client.objects[("test-bucket", key)] == original_size

    # No leftover local staging file under data/ipas/.
    ipa_dir = os.path.join(app_module.IPA_FOLDER, "com.example.app")
    leftover = [f for f in os.listdir(ipa_dir) if f != "1.0.0.ipa"] if os.path.isdir(ipa_dir) else []
    assert leftover == []


# ---------------------------------------------------------------------------
# POST /api/reconcile-icons (Plan 040): upload on-disk icons missing from
# the active backend. Dry-run by default; never touches local icon files.
# ---------------------------------------------------------------------------


def _write_local_icon(app_module, bundle_id, ext, contents=b"icon bytes"):
    bundle_dir = os.path.join(app_module.ICON_FOLDER, bundle_id)
    os.makedirs(bundle_dir, exist_ok=True)
    path = os.path.join(bundle_dir, f"icon.{ext}")
    with open(path, "wb") as f:
        f.write(contents)
    return path


def test_reconcile_dry_run_reports_missing_without_uploading(garage_client):
    app_module = garage_client.app_module
    _write_local_icon(app_module, "com.example.app", "png")

    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    resp = garage_client.post("/api/reconcile-icons", json={"apply": False})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["backend"] == "garage"
    assert body["would_upload"] >= 1
    assert body["uploaded"] == 0
    assert ("test-bucket", "icons/com.example.app/icon.png") not in garage_client.fake_client.objects


def test_reconcile_apply_uploads_missing_icons(garage_client):
    app_module = garage_client.app_module
    _write_local_icon(app_module, "com.example.app", "png")

    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    resp = garage_client.post("/api/reconcile-icons", json={"apply": True})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["backend"] == "garage"
    assert body["uploaded"] >= 1
    assert ("test-bucket", "icons/com.example.app/icon.png") in garage_client.fake_client.objects


def test_reconcile_skips_already_present(garage_client):
    app_module = garage_client.app_module
    _write_local_icon(app_module, "com.example.app", "png")
    garage_client.fake_client.objects[("test-bucket", "icons/com.example.app/icon.png")] = 10

    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    resp = garage_client.post("/api/reconcile-icons", json={"apply": True})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["skipped_existing"] >= 1
    assert body["uploaded"] == 0
    upload_calls = [call for call in garage_client.fake_client.calls if call[0] in ("upload_file", "upload_fileobj")]
    assert upload_calls == []


def test_reconcile_local_backend_is_noop(client):
    app_module = client.app_module
    _write_local_icon(app_module, "com.example.app", "png")

    assert client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    resp = client.post("/api/reconcile-icons", json={"apply": True})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["backend"] == "local"
    assert body["uploaded"] == 0
    # Local icon file must remain untouched.
    icon_path = os.path.join(app_module.ICON_FOLDER, "com.example.app", "icon.png")
    assert os.path.exists(icon_path)


def test_reconcile_requires_auth(garage_client):
    resp = garage_client.post("/api/reconcile-icons", json={"apply": False})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Plan 045: a filesystem error in the icon walk must not 500.
# ---------------------------------------------------------------------------


def test_reconcile_icon_folder_unreadable_does_not_500(garage_client):
    app_module = garage_client.app_module
    real_listdir = _os.listdir

    def fake_listdir(p):
        if str(p) == app_module.ICON_FOLDER:
            raise OSError("simulated unreadable icon folder")
        return real_listdir(p)

    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    with mock.patch.object(app_module.os, "listdir", side_effect=fake_listdir):
        resp = garage_client.post("/api/reconcile-icons", json={"apply": False})
    assert resp.status_code == 200          # <-- the bug: was 500
    body = json.loads(resp.data)
    assert body["backend"] == "garage"
    assert body["checked"] == 0
    assert "note" in body


def test_reconcile_one_unreadable_bundle_is_isolated(garage_client):
    app_module = garage_client.app_module
    _write_local_icon(app_module, "com.good.app", "png")
    _write_local_icon(app_module, "com.bad.app", "png")
    bad_dir = os.path.join(app_module.ICON_FOLDER, "com.bad.app")
    real_listdir = _os.listdir

    def fake_listdir(p):
        if str(p) == bad_dir:
            raise OSError("simulated")
        return real_listdir(p)

    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    with mock.patch.object(app_module.os, "listdir", side_effect=fake_listdir):
        resp = garage_client.post("/api/reconcile-icons", json={"apply": False})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    # the good bundle is still processed; the bad one is reported, not fatal
    assert body["would_upload"] >= 1
    assert any(i.get("status") == "scan_failed" and i["bundleIdentifier"] == "com.bad.app"
               for i in body["items"])


def test_reconcile_empty_folder_returns_note(garage_client):
    # garage backend, no local icons written at all
    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    resp = garage_client.post("/api/reconcile-icons", json={"apply": False})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["checked"] == 0
    assert "note" in body


# ---------------------------------------------------------------------------
# Plan 050: storage self-test -- a write/read/delete round-trip that must
# distinguish a 403 (permission) from a real 404/missing object, which
# exists()'s bool contract cannot do. This is the tool that would have
# diagnosed the 2026-08-19 "403 read-denied" incident in one click.
# ---------------------------------------------------------------------------


def test_selftest_garage_all_ok(garage_client):
    """A fully-cooperative fake client: write/read/delete all succeed for
    both icon_storage and ipa_storage, and the probe key is not left
    behind in the fake store afterward.
    """
    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    resp = garage_client.post("/api/storage-selftest")
    assert resp.status_code == 200
    body = json.loads(resp.data)

    for name in ("icon", "ipa"):
        report = body[name]
        assert report["backend"] == "garage"
        assert report["write"] == "ok"
        assert report["read"] == "ok"
        assert report["delete"] == "ok"

    # The probe object(s) must not be left behind for either storage class.
    leftover = [key for key in garage_client.fake_client.objects if "__selftest__" in key[1]]
    assert leftover == []


def test_selftest_reports_403_read_as_forbidden(garage_client):
    """The exact scenario that confused everyone: head_object (a read)
    returns 403. write must still show "ok" (the put succeeded), while
    read is reported "forbidden" -- not confused with a plain "error" or
    with "ok".
    """
    garage_client.fake_client.fail_head_forbidden = True

    assert garage_client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    resp = garage_client.post("/api/storage-selftest")
    assert resp.status_code == 200
    body = json.loads(resp.data)

    for name in ("icon", "ipa"):
        report = body[name]
        assert report["write"] == "ok"
        assert report["read"] == "forbidden"
        assert report["detail"]  # the S3 error code is surfaced


def test_selftest_local_backend_ok(client):
    """Local backend: all capabilities "ok", and no probe file left
    behind under either ICON_FOLDER or IPA_FOLDER.
    """
    app_module = client.app_module
    assert client.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    resp = client.post("/api/storage-selftest")
    assert resp.status_code == 200
    body = json.loads(resp.data)

    for name in ("icon", "ipa"):
        report = body[name]
        assert report["backend"] == "local"
        assert report["write"] == "ok"
        assert report["read"] == "ok"
        assert report["delete"] == "ok"

    assert not os.path.exists(os.path.join(app_module.ICON_FOLDER, "__selftest__"))
    assert not os.path.exists(os.path.join(app_module.IPA_FOLDER, "__selftest__"))


def test_selftest_requires_auth(garage_client):
    resp = garage_client.post("/api/storage-selftest")
    assert resp.status_code == 401
