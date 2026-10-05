"""Regression tests for the audit's data-correctness findings (#10-#21, #24, #25)."""

import io
import json
import os
import plistlib
import subprocess
import sys
import zipfile

import pytest
import requests
import yaml

from tests.test_routes import (  # noqa: F401
    client, authed_client, client_with_base_url, seed_source, TEST_ADMIN_PASSWORD, _minimal_ipa,
)
from tests.test_publish_api import _token, _anon, _bearer, _mcp, live_server, FAKE_APK  # noqa: F401
from tests.test_release_source_ingest import (
    FakeResponse, FakeSession, FakeFeatherClient, build_ipa_bytes, _valid_github_job,
)
from scripts import release_source_ingest as ingest
from scripts.ipa_inspection import inspect_ipa, InspectionError

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _publish_ipa(c, bundle_id, version, headers=None, **fields):
    return c.post("/api/publish", headers=headers or {}, content_type="multipart/form-data",
                  data={"file": (io.BytesIO(_minimal_ipa(bundle_id, version)), "a.ipa"), **fields})


def _fake_apk(monkeypatch, module, **overrides):
    monkeypatch.setattr(module, "_inspect_apk", lambda _p: {**FAKE_APK, **overrides})
    monkeypatch.setattr(module, "extract_apk_icon", lambda _p: None)


# --- #10 storage-key collisions --------------------------------------------------

def test_version_that_sanitises_onto_an_existing_one_is_refused(authed_client, tmp_path):
    assert _publish_ipa(authed_client, "com.example.app", "2.0").status_code == 200
    stored = (tmp_path / "ipas" / "com.example.app" / "2.0.ipa").read_bytes()
    resp = _publish_ipa(authed_client, "com.example.app", "2.0.")       # secure_filename -> "2.0"
    assert resp.status_code == 400 and "collides" in resp.get_json()["error"]
    assert (tmp_path / "ipas" / "com.example.app" / "2.0.ipa").read_bytes() == stored


def test_bundle_id_that_sanitises_onto_another_app_is_refused(authed_client, tmp_path):
    resp = _publish_ipa(authed_client, "com.example.app.", "9.0")       # -> com.example.app
    assert resp.status_code == 400 and "collides" in resp.get_json()["error"]
    assert not (tmp_path / "ipas" / "com.example.app" / "9.0.ipa").exists()


# --- #11 upload size: 413 JSON, waitress cap follows MAX_CONTENT_LENGTH ------------

def test_oversized_publish_is_413_json(authed_client):
    authed_client.application.config["MAX_CONTENT_LENGTH"] = 1024
    try:
        resp = _publish_ipa(authed_client, "com.big.app", "1.0")
    finally:
        authed_client.application.config["MAX_CONTENT_LENGTH"] = authed_client.app_module.MAX_CONTENT_LENGTH
    assert resp.status_code == 413 and resp.get_json()["success"] is False


def test_waitress_body_cap_is_set_from_max_content_length():
    src = open(os.path.join(ROOT, "app.py")).read()
    serve = src[src.index("serve(app,"):]
    assert "max_request_body_size=MAX_CONTENT_LENGTH" in serve[:600]


# --- #12 APK label over the Name limit ---------------------------------------------

def test_long_apk_label_is_truncated_not_half_published(authed_client, tmp_path, monkeypatch):
    _fake_apk(monkeypatch, authed_client.app_module, app_name="A" * 80)
    resp = authed_client.post("/api/android/add-apk", content_type="multipart/form-data",
                              data={"apkFile": (io.BytesIO(b"a"), "a.apk")})
    assert resp.status_code == 200, resp.get_json()
    meta = yaml.safe_load((tmp_path / "fdroid" / "metadata" / "org.example.agent.yml").read_text())
    assert len(meta["Name"]) == 50 and meta["Name"].endswith("…")
    assert (tmp_path / "fdroid" / ".update-requested").exists()


# --- #13 repo name/description in fdroidserver's YAML --------------------------------

def _render_config(tmp_path, cfg):
    script = open(os.path.join(ROOT, "scripts", "fdroid_index_loop.sh")).read()
    start = script.index("python3 - <<'PY'\n", script.index("render_config()")) + len("python3 - <<'PY'\n")
    body = script[start:script.index("\nPY\n", start)]
    (tmp_path / "repo-config.json").write_text(json.dumps(cfg))
    subprocess.run([sys.executable, "-c", body], cwd=tmp_path, check=True)
    return yaml.safe_load((tmp_path / "config.yml").read_text())


def test_repo_description_with_document_markers_round_trips(tmp_path):
    description = "Our apps:\n--- and more\n... soon 'quoted' \"double\" é 🚀"
    cfg = _render_config(tmp_path, {"name": "Feather's repo", "description": description})
    assert cfg["repo_description"] == description and cfg["repo_name"] == "Feather's repo"


def test_repo_config_rejects_control_characters(authed_client):
    bad = authed_client.post("/api/android/repo-config", json={"name": "x", "description": "a\x07b"})
    assert bad.status_code == 400
    ok = authed_client.post("/api/android/repo-config", json={"name": "x", "description": "line\n---\nline"})
    assert ok.status_code == 200


# --- #14 APK_REJECT_DEBUGGABLE on every path -----------------------------------------

def test_debuggable_refused_on_admin_upload_too(authed_client, tmp_path, monkeypatch):
    module = authed_client.app_module
    _fake_apk(monkeypatch, module, debuggable=True)
    body = authed_client.post("/api/android/add-apk", content_type="multipart/form-data",
                              data={"apkFile": (io.BytesIO(b"a"), "a.apk")}).get_json()
    assert body["added"] is True and any("debuggable" in w for w in body["warnings"])
    _fake_apk(monkeypatch, module, debuggable=True, version_code=8)
    monkeypatch.setattr(module, "APK_REJECT_DEBUGGABLE", True)
    resp = authed_client.post("/api/android/add-apk", content_type="multipart/form-data",
                              data={"apkFile": (io.BytesIO(b"b"), "b.apk")})
    assert resp.status_code == 400 and "debuggable" in resp.get_json()["error"]
    assert not (tmp_path / "fdroid" / "repo" / "org.example.agent_8.apk").exists()


# --- #15 same id on iOS and Android ----------------------------------------------------

def test_app_details_needs_platform_when_id_is_shared(authed_client, tmp_path, monkeypatch):
    _fake_apk(monkeypatch, authed_client.app_module, package="com.example.app")
    authed_client.post("/api/publish", content_type="multipart/form-data",
                       data={"file": (io.BytesIO(b"a"), "a.apk"), "name": "Droid"})
    resp = authed_client.post("/api/app-details", json={"id": "com.example.app", "license": "MIT"})
    assert resp.status_code == 409
    resp = authed_client.post("/api/app-details", json={"id": "com.example.app", "platform": "android",
                                                         "license": "MIT", "summary": "Sync"})
    assert resp.status_code == 200 and resp.get_json()["metadata"]["License"] == "MIT"
    resp = authed_client.post("/api/app-details", json={"id": "com.example.app", "platform": "ios", "summary": "Sub"})
    assert resp.status_code == 200 and resp.get_json()["app"]["subtitle"] == "Sub"


def test_mcp_get_app_returns_both_platforms(authed_client, live_server, monkeypatch):
    _fake_apk(monkeypatch, authed_client.app_module, package="com.example.app")
    authed_client.post("/api/publish", content_type="multipart/form-data", data={"file": (io.BytesIO(b"a"), "a.apk")})
    env = {**os.environ, "FEATHER_URL": live_server, "FEATHER_TOKEN": _token(authed_client)["token"]}
    out, _ = _mcp(env, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "get_app", "arguments": {"id": "com.example.app"}}}])
    record = json.loads(out[0]["result"]["content"][0]["text"])
    assert record["platform"] == "both" and "ios" in record and "android" in record


# --- #16 restore not blocked by informational notes ------------------------------------

def _snapshot(module, download_url):
    os.makedirs(module.BACKUP_FOLDER, exist_ok=True)
    cand = seed_source()
    cand["name"] = "Recovered Source"
    cand["apps"][0]["versions"][0]["downloadURL"] = download_url
    fn = "source-20260828T120000Z.json"
    with open(os.path.join(module.BACKUP_FOLDER, fn), "w") as h:
        json.dump(cand, h)
    return fn


def _restore(c, fn):
    sha = c.post("/api/catalog-backups/preview", json={"filename": fn}).get_json()["sha256"]
    return c.post("/api/catalog-backups/restore", json={"filename": fn, "confirm": fn, "expectedSha256": sha,
                                                        "allowMissingArtifacts": False})


def test_restore_with_external_download_url(client_with_base_url):
    c = client_with_base_url
    assert c.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    resp = _restore(c, _snapshot(c.app_module, "https://github.com/o/r/releases/download/v1/App.ipa"))
    assert resp.status_code == 200, resp.get_json()


def test_restore_on_garage_backend(authed_client, monkeypatch):
    m = authed_client.app_module
    path = m.source_manager.get_ipa_path("com.example.app", "1.0.0")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/X.app/Info.plist", "x")
    monkeypatch.setattr(m, "STORAGE_BACKEND", "garage")
    resp = _restore(authed_client, _snapshot(m, "http://localhost/ipas/com.example.app/1.0.0.ipa"))
    assert resp.status_code == 200, resp.get_json()


def test_restore_still_blocks_a_missing_ipa(client_with_base_url):
    c = client_with_base_url
    assert c.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
    resp = _restore(c, _snapshot(c.app_module, "http://feather.example.com/ipas/com.example.app/1.0.0.ipa"))
    assert resp.status_code == 400


# --- #17 Add App on an existing ID with a new icon -------------------------------------

def test_add_app_existing_id_applies_the_new_icon(authed_client, tmp_path):
    from PIL import Image
    old = tmp_path / "icons" / "com.example.app"
    old.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(old / "icon.jpg")
    source = json.loads((tmp_path / "source.json").read_text())
    source["apps"][0]["iconURL"] = "http://localhost/icons/com.example.app/icon.jpg"
    (tmp_path / "source.json").write_text(json.dumps(source))
    png = io.BytesIO()
    Image.new("RGBA", (8, 8)).save(png, format="PNG")
    resp = authed_client.post("/api/add-app", content_type="multipart/form-data", data={
        "name": "Example App", "bundleIdentifier": "com.example.app", "developerName": "Dev", "version": "2.0",
        "ipaFile": (io.BytesIO(_minimal_ipa("com.example.app", "2.0")), "a.ipa"),
        "iconFile": (png, "icon.png")})
    assert resp.status_code == 200, resp.get_json()
    icon_url = json.loads((tmp_path / "source.json").read_text())["apps"][0]["iconURL"]
    served = icon_url.rsplit("/icons/", 1)[1]
    assert (tmp_path / "icons" / served).exists(), icon_url


# --- #18 no version without a usable download URL --------------------------------------

def test_add_app_and_version_require_an_ipa_or_url(authed_client, tmp_path):
    resp = authed_client.post("/api/add-app", json={"name": "N", "bundleIdentifier": "com.no.ipa",
                                                    "developerName": "D", "version": "1.0"})
    assert resp.status_code == 400
    resp = authed_client.post("/api/add-version", content_type="multipart/form-data", data={
        "bundleIdentifier": "com.example.app", "version": "2.0", "downloadFromUrl": "true", "downloadURL": ""})
    assert resp.status_code == 400
    urls = [v["downloadURL"] for a in json.loads((tmp_path / "source.json").read_text())["apps"] for v in a["versions"]]
    assert all(urls)


# --- #19 an older backport never becomes versions[0] --------------------------------------

def test_older_version_does_not_become_latest(authed_client, tmp_path):
    assert _publish_ipa(authed_client, "com.example.app", "0.9.1").status_code == 200
    assert _publish_ipa(authed_client, "com.example.app", "1.1").status_code == 200
    versions = [v["version"] for v in json.loads((tmp_path / "source.json").read_text())["apps"][0]["versions"]]
    assert versions == ["1.1", "1.0.0", "0.9.1"]


def test_insert_version_ordering(client):
    insert = client.app_module._insert_version
    versions = [{"version": "2.0.0"}, {"version": "1.0"}]
    insert(versions, {"version": "1.9.1"})
    insert(versions, {"version": "2.1"})
    insert(versions, {"version": "nightly"})          # no number: newest by arrival
    assert [v["version"] for v in versions] == ["nightly", "2.1", "2.0.0", "1.9.1", "1.0"]


# --- #20 one failing job never ends the cron run ---------------------------------------------

class _RaisingSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.raise_for = {}

    def get(self, url, **kwargs):
        if url in self.raise_for:
            self.calls.append((url, kwargs))
            raise self.raise_for[url]
        return super().get(url, **kwargs)


def _release(project, rid, aid):
    return [{"id": rid, "tag_name": "v1", "draft": False, "prerelease": False,
             "published_at": "2026-01-01T00:00:00Z",
             "assets": [{"id": aid, "name": "App.ipa", "size": None,
                         "url": f"https://api.github.com/repos/{project}/releases/assets/{aid}"}]}]


@pytest.mark.parametrize("exc", [requests.exceptions.ReadTimeout("t"), requests.exceptions.ConnectionError("c")])
def test_cron_network_error_does_not_skip_later_jobs(tmp_path, monkeypatch, exc):
    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({"schemaVersion": 1, "jobs": [
        _valid_github_job(id="a", project="owner/a", bundleIdentifier="com.a.app"),
        _valid_github_job(id="b", project="owner/b", bundleIdentifier="com.b.app")]}))
    session = _RaisingSession()
    session.add_response("https://api.github.com/repos/owner/a/releases", FakeResponse(200, json_data=_release("owner/a", 1, 11)))
    session.add_response("https://api.github.com/repos/owner/b/releases", FakeResponse(200, json_data=_release("owner/b", 2, 22)))
    session.add_response("https://api.github.com/repos/owner/b/releases/assets/22",
                         FakeResponse(200, content_chunks=[build_ipa_bytes(bundle_id="com.b.app")]))
    session.raise_for["https://api.github.com/repos/owner/a/releases/assets/11"] = exc
    monkeypatch.setattr(ingest.requests, "Session", lambda: session)

    class Feather(FakeFeatherClient):
        def __init__(self, *a, **k):
            super().__init__(apps={b: {"bundleIdentifier": b, "versions": []} for b in ("com.a.app", "com.b.app")})

        def login(self):
            pass
    monkeypatch.setattr(ingest, "FeatherClient", Feather)
    monkeypatch.setenv("FEATHER_BASE_URL", "http://feather.example")
    monkeypatch.setenv("FEATHER_ADMIN_PASSWORD", "pw")
    rc = ingest.main(["--config", str(cfg), "--state", str(tmp_path / "state.json"), "--apply"])
    urls = [u for u, _ in session.calls]
    assert "https://api.github.com/repos/owner/b/releases/assets/22" in urls
    assert rc != 0


def test_unsupported_compression_raises_inspection_error(tmp_path):
    """Deflate64 (method 9) makes zipfile raise NotImplementedError, which
    used to escape inspect_ipa and crash the cron run."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("Payload/A.app/Info.plist", plistlib.dumps({"CFBundleIdentifier": "com.x.app",
                                                                 "CFBundleShortVersionString": "1.0"}))
    data = bytearray(buf.getvalue())
    data[8:10] = (9).to_bytes(2, "little")                      # local header method
    cd = data.rindex(b"PK\x01\x02")
    data[cd + 10:cd + 12] = (9).to_bytes(2, "little")           # central directory method
    path = tmp_path / "deflate64.ipa"
    path.write_bytes(bytes(data))
    with pytest.raises(InspectionError, match="NotImplementedError"):
        inspect_ipa(str(path))


# --- #21 sessions end when ADMIN_PASSWORD changes ------------------------------------------

def test_password_change_signs_existing_sessions_out(authed_client, monkeypatch):
    assert authed_client.get("/api/session").get_json() == {"authed": True}
    monkeypatch.setattr(authed_client.app_module, "ADMIN_PASSWORD", "a-new-password")
    assert authed_client.get("/api/session").get_json() == {"authed": False}
    assert authed_client.get("/api/tokens").status_code == 401


# --- #24 MCP server robustness ----------------------------------------------------------------

class _Fake:
    def __init__(self, status, body, headers=()):
        self.status, self.body, self.headers = status, body, dict(headers)


def _mcp_against(fake, tmp_path, messages, stdin_bytes=None):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def _reply(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(fake.status)
            for k, v in fake.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(fake.body)))
            self.end_headers()
            self.wfile.write(fake.body)
        do_GET = do_POST = _reply

        def log_message(self, *a):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = {**os.environ, "FEATHER_URL": f"http://127.0.0.1:{server.server_port}", "FEATHER_TOKEN": "ftr_x"}
        script = os.path.join(ROOT, "scripts", "feather_mcp.py")
        data = stdin_bytes if stdin_bytes is not None else "".join(json.dumps(m) + "\n" for m in messages).encode()
        proc = subprocess.run([sys.executable, script], input=data, capture_output=True, env=env, timeout=60)
    finally:
        server.shutdown()
    return [json.loads(line) for line in proc.stdout.decode().splitlines() if line.strip()]


def _call(name, arguments, mid=1):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_mcp_redirect_and_html_are_errors(tmp_path):
    ipa = tmp_path / "a.ipa"
    ipa.write_bytes(_minimal_ipa("com.x.app", "1.0"))
    out = _mcp_against(_Fake(301, b"", {"Location": "https://real.example/api/publish"}), tmp_path,
                       [_call("publish_app", {"path": str(ipa)})])
    assert out[0]["result"]["isError"] is True and "redirect" in out[0]["result"]["content"][0]["text"]
    out = _mcp_against(_Fake(200, b"<html>Sign in</html>"), tmp_path, [_call("publish_app", {"path": str(ipa)})])
    assert out[0]["result"]["isError"] is True and "not JSON" in out[0]["result"]["content"][0]["text"]


def test_mcp_survives_bad_input_and_reads_utf8(tmp_path):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "nope"},
        _call("update_app", "not-an-object", 2),
        _call("update_app", {"id": "com.x.app", "categories": [1, 2]}, 3),
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
    ]
    payload = "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs)
    payload += json.dumps(_call("update_app", {"id": "com.x.app", "name": "Café 🚀"}, 5), ensure_ascii=False) + "\n"
    out = _mcp_against(_Fake(200, json.dumps({"success": True}).encode()), tmp_path, None,
                       stdin_bytes=payload.encode("utf-8"))
    by_id = {m["id"]: m for m in out}
    assert by_id[1]["error"]["code"] == -32602
    assert by_id[2]["result"]["isError"] is True
    assert by_id[3]["result"]["isError"] is True
    assert by_id[4]["result"] == {}
    assert by_id[5]["result"]["isError"] is False


# --- #25 URL downloads have a wall-clock deadline ----------------------------------------------

def test_download_deadline(client, monkeypatch):
    module = client.app_module

    class Slow:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            while True:
                yield b"x"
    monkeypatch.setattr(module.requests, "get", lambda *a, **k: Slow())
    monkeypatch.setattr(module, "DOWNLOAD_DEADLINE_SECONDS", 0)
    with pytest.raises(ValueError, match="longer than"):
        module._download_to_temp("http://example.test/a.apk", ".apk")
    assert os.listdir(module.UPLOAD_FOLDER) == []


# --- publish retry race and storage failures ---------------------------------------------------

def test_publish_reports_concurrent_retry_as_not_added(authed_client, monkeypatch):
    module = authed_client.app_module
    monkeypatch.setattr(module.source_manager, "add_app_manual",
                        lambda *a, **k: (True, f"Version 1.0 {module.ALREADY_PRESENT}"))
    body = _publish_ipa(authed_client, "com.race.app", "1.0").get_json()
    assert body["added"] is False and body["created"] is False


def test_publish_storage_failure_is_503(authed_client, monkeypatch):
    monkeypatch.setattr(authed_client.app_module.source_manager, "add_app_manual",
                        lambda *a, **k: (False, "Failed to save source data"))
    assert _publish_ipa(authed_client, "com.disk.app", "1.0").status_code == 503


def test_delete_app_keeps_ipas_when_catalog_save_fails(authed_client, tmp_path, monkeypatch):
    assert _publish_ipa(authed_client, "com.example.app", "2.0").status_code == 200
    ipa = tmp_path / "ipas" / "com.example.app" / "2.0.ipa"
    monkeypatch.setattr(authed_client.app_module.source_manager, "save_source", lambda *_a: False)
    resp = authed_client.post("/api/delete-app", json={"bundleIdentifier": "com.example.app"})
    assert resp.status_code == 400 and ipa.exists()
