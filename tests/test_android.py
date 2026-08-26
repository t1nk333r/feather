"""Tests for the Android / F-Droid repository (Plan 073): AndroidRepoManager,
_inspect_apk, and the /fdroid/* and /api/android/* routes.

Reuses the `client` / `authed_client` fixtures from tests/test_routes.py
(same isolated-tmp_path-DATA_DIR pattern). A real binary AndroidManifest.xml
cannot be authored in a test, so `_inspect_apk` is monkeypatched to a fixed
result for a fixed input (b"fake-apk-bytes") -- see `fake_inspect` below.
Only test_inspect_apk_real_file_if_available exercises the real parser, and
only when a developer has set FEATHER_TEST_APK; it is skipped in CI.
"""

import http.server
import io
import json
import os
import threading

import pytest
import yaml

from tests.test_routes import client, authed_client, TEST_ADMIN_PASSWORD  # noqa: F401


FAKE = {
    "package": "org.example.demo",
    "version_code": 42,
    "version_name": "4.2",
    "min_sdk": 23,
    "target_sdk": 34,
    "app_name": "Demo",
}


def fake_inspect(path):
    assert open(path, "rb").read() == b"fake-apk-bytes"
    return dict(FAKE)


# ---------------------------------------------------------------------------
# Local-loopback HTTP stub, used only to prove a route made zero requests.
# Binds to 127.0.0.1 on an ephemeral port; no real network call leaves the
# machine. Mirrors the pattern in tests/test_routes.py:220-264.
# ---------------------------------------------------------------------------


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        _CountingHandler.hits += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # keep test output quiet


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------


def test_fdroid_repo_route_is_public_and_404s_when_missing(client):
    resp = client.get("/fdroid/repo/index-v1.jar")
    assert resp.status_code == 404
    body = json.loads(resp.data)
    assert "error" in body


def test_fdroid_repo_serves_file_with_apk_mimetype(client, tmp_path):
    repo_dir = tmp_path / "fdroid" / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "org.example.demo_42.apk").write_bytes(b"fake-apk-bytes")

    resp = client.get("/fdroid/repo/org.example.demo_42.apk")
    assert resp.status_code == 200
    assert resp.content_type.startswith("application/vnd.android.package-archive")


def test_fdroid_repo_rejects_traversal(client):
    resp = client.get("/fdroid/repo/../repo-config.json")
    assert resp.status_code == 404

    resp2 = client.get("/fdroid/repo/..%2Frepo-config.json")
    assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_android_api_requires_auth(client):
    checks = [
        ("get", "/api/android/status"),
        ("get", "/api/android/apps"),
        ("post", "/api/android/add-apk"),
        ("post", "/api/android/update-app"),
        ("post", "/api/android/delete-version"),
        ("post", "/api/android/delete-app"),
        ("post", "/api/android/repo-config"),
        ("post", "/api/android/request-update"),
    ]
    for method, path in checks:
        resp = getattr(client, method)(path)
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"


# ---------------------------------------------------------------------------
# add-apk
# ---------------------------------------------------------------------------


def test_add_apk_upload_writes_apk_metadata_and_marker(authed_client, tmp_path, monkeypatch):
    monkeypatch.setattr(authed_client.app_module, "_inspect_apk", fake_inspect)

    resp = authed_client.post(
        "/api/android/add-apk",
        data={
            "apkFile": (io.BytesIO(b"fake-apk-bytes"), "demo.apk"),
            "name": "Demo",
            "summary": "Hi",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True
    assert body["package"] == "org.example.demo"
    assert body["versionCode"] == 42
    assert body["pending"] is True

    apk_path = tmp_path / "fdroid" / "repo" / "org.example.demo_42.apk"
    assert apk_path.exists()

    meta_path = tmp_path / "fdroid" / "metadata" / "org.example.demo.yml"
    meta = yaml.safe_load(meta_path.read_text())
    assert meta["Name"] == "Demo"
    assert meta["Summary"] == "Hi"
    assert meta["Categories"] == ["Feather"]

    marker = tmp_path / "fdroid" / ".update-requested"
    assert marker.exists()


def test_add_apk_rejects_non_apk_filename(authed_client, tmp_path, monkeypatch):
    monkeypatch.setattr(authed_client.app_module, "_inspect_apk", fake_inspect)

    resp = authed_client.post(
        "/api/android/add-apk",
        data={"apkFile": (io.BytesIO(b"fake-apk-bytes"), "demo.ipa")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False

    repo_dir = tmp_path / "fdroid" / "repo"
    assert not any(repo_dir.glob("*.apk")) if repo_dir.exists() else True


def test_add_apk_package_mismatch_400(authed_client, tmp_path, monkeypatch):
    monkeypatch.setattr(authed_client.app_module, "_inspect_apk", fake_inspect)

    resp = authed_client.post(
        "/api/android/add-apk",
        data={
            "apkFile": (io.BytesIO(b"fake-apk-bytes"), "demo.apk"),
            "package": "com.other.app",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False
    assert "org.example.demo" in body["error"]
    assert "com.other.app" in body["error"]

    repo_dir = tmp_path / "fdroid" / "repo"
    assert not any(repo_dir.glob("*.apk")) if repo_dir.exists() else True


def test_add_apk_idempotent_on_existing_version(authed_client, tmp_path, monkeypatch):
    monkeypatch.setattr(authed_client.app_module, "_inspect_apk", fake_inspect)

    resp1 = authed_client.post(
        "/api/android/add-apk",
        data={"apkFile": (io.BytesIO(b"fake-apk-bytes"), "demo.apk")},
        content_type="multipart/form-data",
    )
    assert resp1.status_code == 200

    apk_path = tmp_path / "fdroid" / "repo" / "org.example.demo_42.apk"
    assert apk_path.exists()
    content1 = apk_path.read_bytes()

    resp2 = authed_client.post(
        "/api/android/add-apk",
        data={"apkFile": (io.BytesIO(b"fake-apk-bytes"), "demo.apk")},
        content_type="multipart/form-data",
    )
    assert resp2.status_code == 200
    body2 = json.loads(resp2.data)
    assert "already present" in body2["message"]
    assert apk_path.read_bytes() == content1


def test_add_apk_download_from_url_never_fetched_without_flag(authed_client, monkeypatch):
    """Mirrors test_add_version_downloadurl_never_fetched_without_download_flag
    (tests/test_routes.py:946): downloadURL set but downloadFromUrl absent
    must be rejected without ever making the request -- proven here by an
    actual loopback stub server that must receive zero hits, since Android
    (unlike the iOS add-version route) has no "store the URL as-is"
    fallback: every APK must be fetched and validated locally."""
    monkeypatch.setattr(authed_client.app_module, "_inspect_apk", fake_inspect)
    _CountingHandler.hits = 0
    server = http.server.HTTPServer(("127.0.0.1", 0), _CountingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/demo.apk"
        resp = authed_client.post(
            "/api/android/add-apk",
            data={"downloadURL": url},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert body["success"] is False
        assert _CountingHandler.hits == 0
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_metadata_length_limits(authed_client, monkeypatch):
    monkeypatch.setattr(authed_client.app_module, "_inspect_apk", fake_inspect)

    resp = authed_client.post(
        "/api/android/update-app",
        json={"package": "org.example.demo", "summary": "x" * 81},
    )
    assert resp.status_code == 400

    resp2 = authed_client.post(
        "/api/android/update-app",
        json={"package": "org.example.demo", "name": "x" * 51},
    )
    assert resp2.status_code == 400


# ---------------------------------------------------------------------------
# list_apps / apps route
# ---------------------------------------------------------------------------


def test_list_apps_merges_index_and_disk(authed_client, tmp_path):
    repo_dir = tmp_path / "fdroid" / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "org.example.demo_43.apk").write_bytes(b"x")

    index = {
        "repo": {"timestamp": 1000},
        "apps": [{"packageName": "org.example.demo", "name": "Demo"}],
        "packages": {
            "org.example.demo": [
                {
                    "apkName": "org.example.demo_42.apk",
                    "versionCode": 42,
                    "versionName": "4.2",
                    "size": 100,
                    "minSdkVersion": 21,
                    "signer": "aaa",
                }
            ]
        },
    }
    (repo_dir / "index-v1.json").write_text(json.dumps(index))

    resp = authed_client.get("/api/android/apps")
    assert resp.status_code == 200
    apps = json.loads(resp.data)
    demo = next(a for a in apps if a["package"] == "org.example.demo")
    versions = {v["versionCode"]: v for v in demo["versions"]}
    assert versions[42]["published"] is True
    assert versions[43]["published"] is False
    assert demo["pending"] is True


def test_list_apps_flags_mixed_signers(authed_client, tmp_path):
    repo_dir = tmp_path / "fdroid" / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "apps": [{"packageName": "org.example.demo", "name": "Demo"}],
        "packages": {
            "org.example.demo": [
                {"apkName": "a1.apk", "versionCode": 1, "signer": "aaa"},
                {"apkName": "a2.apk", "versionCode": 2, "signer": "bbb"},
            ]
        },
    }
    (repo_dir / "index-v1.json").write_text(json.dumps(index))

    resp = authed_client.get("/api/android/apps")
    assert resp.status_code == 200
    apps = json.loads(resp.data)
    demo = next(a for a in apps if a["package"] == "org.example.demo")
    assert demo["mixed_signers"] is True


# ---------------------------------------------------------------------------
# delete-version / delete-app
# ---------------------------------------------------------------------------


def test_delete_version_and_app(authed_client, tmp_path, monkeypatch):
    monkeypatch.setattr(authed_client.app_module, "_inspect_apk", fake_inspect)

    resp = authed_client.post(
        "/api/android/add-apk",
        data={"apkFile": (io.BytesIO(b"fake-apk-bytes"), "demo.apk")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200

    meta_path = tmp_path / "fdroid" / "metadata" / "org.example.demo.yml"
    apk_path = tmp_path / "fdroid" / "repo" / "org.example.demo_42.apk"
    assert meta_path.exists()
    assert apk_path.exists()

    resp = authed_client.post(
        "/api/android/delete-version",
        json={"package": "org.example.demo", "versionCode": 42},
    )
    assert resp.status_code == 200
    assert not apk_path.exists()
    assert meta_path.exists()  # delete-version keeps the yml

    resp = authed_client.post(
        "/api/android/delete-version",
        json={"package": "org.example.demo", "versionCode": 42},
    )
    assert resp.status_code == 404

    resp = authed_client.post(
        "/api/android/delete-app",
        json={"package": "org.example.demo"},
    )
    assert resp.status_code == 200
    assert not meta_path.exists()  # delete-app removes the yml

    resp = authed_client.post(
        "/api/android/delete-app",
        json={"package": "org.example.demo"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# status / qr / repo-config
# ---------------------------------------------------------------------------


def test_status_unconfigured_then_configured(authed_client, tmp_path):
    resp = authed_client.get("/api/android/status")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["configured"] is False
    assert body["subscribe_url"] is None

    qr_resp = authed_client.get("/fdroid/qr")
    assert qr_resp.status_code == 503

    fingerprint = "a" * 64
    fdroid_dir = tmp_path / "fdroid"
    fdroid_dir.mkdir(parents=True, exist_ok=True)
    (fdroid_dir / "fingerprint.txt").write_text(fingerprint)

    resp2 = authed_client.get("/api/android/status")
    body2 = json.loads(resp2.data)
    assert body2["configured"] is True
    assert body2["subscribe_url"] == body2["repo_url"] + "?fingerprint=" + fingerprint
    assert body2["repo_url"].endswith("/fdroid/repo")

    qr_resp2 = authed_client.get("/fdroid/qr")
    assert qr_resp2.status_code == 200
    assert qr_resp2.content_type == "image/png"


def test_repo_config_roundtrip(authed_client, tmp_path):
    resp = authed_client.post(
        "/api/android/repo-config",
        json={"name": "My Repo", "description": "d"},
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True

    cfg_path = tmp_path / "fdroid" / "repo-config.json"
    cfg = json.loads(cfg_path.read_text())
    assert cfg["name"] == "My Repo"
    assert cfg["description"] == "d"
    assert cfg["repo_url"].endswith("/fdroid/repo")


# ---------------------------------------------------------------------------
# _inspect_apk against a real APK -- developer-machine only
# ---------------------------------------------------------------------------


def test_inspect_apk_real_file_if_available(client):
    path = os.environ.get("FEATHER_TEST_APK")
    if not path:
        pytest.skip("FEATHER_TEST_APK not set")
    info = client.app_module._inspect_apk(path)
    assert info["package"] == "org.fdroid.fdroid"
