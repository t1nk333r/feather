"""API tokens and POST /api/publish (the agent/CI upload path), plus a
round-trip through scripts/feather_mcp.py against a live local server."""

import io
import json
import os
import subprocess
import sys
import threading

import pytest

from tests.test_routes import (  # noqa: F401
    client, authed_client, TEST_ADMIN_PASSWORD, _minimal_ipa,
)


FAKE_APK = {"package": "org.example.agent", "version_code": 7, "version_name": "0.7",
            "min_sdk": 23, "target_sdk": 34, "app_name": "Agent Demo"}


def _token(authed_client, name="ci"):
    resp = authed_client.post("/api/tokens", json={"name": name})
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["token"].startswith("ftr_")
    return body


def _anon(authed_client):
    """A second client with no session, sharing the same app module."""
    c = authed_client.application.test_client()
    c.app_module = authed_client.app_module
    return c


def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def _fake_apk(monkeypatch, module, info=FAKE_APK):
    monkeypatch.setattr(module, "_inspect_apk", lambda _p: dict(info))
    monkeypatch.setattr(module, "extract_apk_icon", lambda _p: None)


# --- tokens -----------------------------------------------------------------

def test_token_management_requires_a_session(client):
    assert client.get("/api/tokens").status_code == 401
    assert client.post("/api/tokens", json={"name": "x"}).status_code == 401
    assert client.post("/api/tokens/revoke", json={"id": "x"}).status_code == 401


def test_token_is_stored_hashed_and_listed_without_secret(authed_client, tmp_path):
    body = _token(authed_client)
    stored = (tmp_path / "api-tokens.json").read_text()
    assert body["token"] not in stored
    assert oct(os.stat(tmp_path / "api-tokens.json").st_mode & 0o777) == "0o600"
    listed = authed_client.get("/api/tokens").get_json()
    assert [t["id"] for t in listed] == [body["id"]]
    assert "hash" not in listed[0] and "token" not in listed[0]
    assert listed[0]["prefix"] == body["token"][:10]


def test_token_name_required(authed_client):
    assert authed_client.post("/api/tokens", json={}).status_code == 400
    assert authed_client.post("/api/tokens", json={"name": "x" * 81}).status_code == 400


def test_token_cannot_manage_tokens_or_delete(authed_client):
    tok = _token(authed_client)["token"]
    anon = _anon(authed_client)
    assert anon.get("/api/tokens", headers=_bearer(tok)).status_code == 401
    assert anon.post("/api/tokens", json={"name": "y"}, headers=_bearer(tok)).status_code == 401
    assert anon.post("/api/delete-app", json={"bundleIdentifier": "com.example.app"},
                     headers=_bearer(tok)).status_code == 401
    assert anon.post("/api/android/delete-app", json={"package": "a.b"},
                     headers=_bearer(tok)).status_code == 401


def test_token_can_read_android_status(authed_client):
    tok = _token(authed_client)["token"]
    anon = _anon(authed_client)
    assert anon.get("/api/android/apps").status_code == 401
    assert anon.get("/api/android/apps", headers=_bearer(tok)).status_code == 200
    assert anon.get("/api/android/status", headers=_bearer(tok)).status_code == 200


def test_bad_and_revoked_tokens_are_refused(authed_client):
    body = _token(authed_client)
    anon = _anon(authed_client)
    for header in ({"Authorization": "Bearer ftr_wrong"}, {"Authorization": body["token"]},
                   {"Authorization": "Basic " + body["token"]}):
        ipa = {"file": (io.BytesIO(_minimal_ipa("com.agent.one", "1.0")), "a.ipa")}
        assert anon.post("/api/publish", data=ipa, headers=header,
                         content_type="multipart/form-data").status_code == 401
    assert authed_client.post("/api/tokens/revoke", json={"id": body["id"]}).status_code == 200
    assert authed_client.post("/api/tokens/revoke", json={"id": body["id"]}).status_code == 404
    resp = anon.post("/api/publish", headers=_bearer(body["token"]), content_type="multipart/form-data",
                     data={"file": (io.BytesIO(_minimal_ipa("com.agent.one", "1.0")), "a.ipa")})
    assert resp.status_code == 401


def test_token_use_is_recorded(authed_client):
    body = _token(authed_client)
    _anon(authed_client).get("/api/android/apps", headers=_bearer(body["token"]))
    assert authed_client.get("/api/tokens").get_json()[0]["lastUsed"]


# --- publish: iOS -------------------------------------------------------------

def test_publish_new_ipa_creates_app_with_token(authed_client, tmp_path):
    tok = _token(authed_client)["token"]
    anon = _anon(authed_client)
    resp = anon.post("/api/publish", headers=_bearer(tok), content_type="multipart/form-data", data={
        "file": (io.BytesIO(_minimal_ipa("com.agent.new", "2.1")), "whatever.bin"),
        "name": "Agent App", "developerName": "Bot", "description": "Built by CI",
    })
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert body["platform"] == "ios" and body["id"] == "com.agent.new"
    assert body["added"] is True and body["created"] is True and body["version"] == "2.1"
    assert body["downloadURL"].endswith("/ipas/com.agent.new/2.1.ipa")
    app = json.loads((tmp_path / "source.json").read_text())
    entry = next(a for a in app["apps"] if a["bundleIdentifier"] == "com.agent.new")
    assert (entry["name"], entry["developerName"], entry["localizedDescription"]) == ("Agent App", "Bot", "Built by CI")
    assert (tmp_path / "ipas" / "com.agent.new" / "2.1.ipa").exists()


def test_publish_ipa_adds_version_to_existing_and_is_idempotent(authed_client, tmp_path):
    def send(version):
        return authed_client.post("/api/publish", content_type="multipart/form-data", data={
            "ipaFile": (io.BytesIO(_minimal_ipa("com.example.app", version)), "x.ipa"),
            "name": "Ignored For Existing"})
    body = send("1.1.0").get_json()
    assert body["added"] is True and body["created"] is False
    again = send("1.1.0").get_json()
    assert again["success"] is True and again["added"] is False
    app = json.loads((tmp_path / "source.json").read_text())["apps"][0]
    assert app["name"] == "Example App"
    assert [v["version"] for v in app["versions"]] == ["1.1.0", "1.0.0"]


def test_publish_create_if_missing_false_refuses_unknown(authed_client, tmp_path):
    resp = authed_client.post("/api/publish", content_type="multipart/form-data", data={
        "file": (io.BytesIO(_minimal_ipa("com.agent.nope", "1.0")), "a.ipa"), "createIfMissing": "false"})
    assert resp.status_code == 404
    assert not (tmp_path / "ipas" / "com.agent.nope").exists()


def test_publish_rejects_junk_and_empty(authed_client, tmp_path):
    assert authed_client.post("/api/publish", json={}).status_code == 400
    r = authed_client.post("/api/publish", content_type="multipart/form-data",
                           data={"file": (io.BytesIO(b"not a zip"), "notes.txt")})
    assert r.status_code == 400 and "Not an .ipa or .apk" in r.get_json()["error"]
    r = authed_client.post("/api/publish", content_type="multipart/form-data",
                           data={"file": (io.BytesIO(b"not a zip"), "fake.ipa")})
    assert r.status_code == 400
    assert authed_client.post("/api/publish", json={"url": "file:///etc/passwd"}).status_code == 400
    assert authed_client.post("/api/publish", json={"url": "http://x", "name": "n" * 51}).status_code == 400
    assert os.listdir(tmp_path / "uploads") == []


def test_publish_from_url(authed_client, tmp_path, monkeypatch):
    payload = _minimal_ipa("com.agent.url", "3.0")

    class Resp:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield payload

    calls = []
    monkeypatch.setattr(authed_client.app_module.requests, "get",
                        lambda url, **kw: calls.append(url) or Resp())
    resp = authed_client.post("/api/publish", json={"url": "https://example.test/dl?id=1"})
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert calls == ["https://example.test/dl?id=1"]
    assert body["id"] == "com.agent.url" and body["added"] is True


# --- publish: Android ---------------------------------------------------------

def test_publish_apk_with_token(authed_client, tmp_path, monkeypatch):
    _fake_apk(monkeypatch, authed_client.app_module)
    tok = _token(authed_client)["token"]
    resp = _anon(authed_client).post("/api/publish", headers=_bearer(tok), content_type="multipart/form-data",
                                     data={"file": (io.BytesIO(b"apk-bytes"), "demo.apk"),
                                           "name": "Agent Droid", "developerName": "Bot"})
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert body["platform"] == "android" and body["id"] == "org.example.agent"
    assert body["build"] == 7 and body["created"] is True and body["pending"] is True
    assert body["downloadURL"].endswith("/fdroid/repo/org.example.agent_7.apk")
    assert (tmp_path / "fdroid" / "repo" / "org.example.agent_7.apk").exists()
    meta = (tmp_path / "fdroid" / "metadata" / "org.example.agent.yml").read_text()
    assert "Agent Droid" in meta and "AuthorName: Bot" in meta

    again = _anon(authed_client).post("/api/publish", headers=_bearer(tok), content_type="multipart/form-data",
                                      data={"file": (io.BytesIO(b"apk-bytes"), "demo.apk")}).get_json()
    assert again["added"] is False and again["success"] is True


def test_publish_apk_existing_keeps_metadata(authed_client, tmp_path, monkeypatch):
    _fake_apk(monkeypatch, authed_client.app_module)
    authed_client.post("/api/publish", content_type="multipart/form-data",
                       data={"file": (io.BytesIO(b"a"), "a.apk"), "name": "First"})
    _fake_apk(monkeypatch, authed_client.app_module, {**FAKE_APK, "version_code": 8})
    body = authed_client.post("/api/publish", content_type="multipart/form-data",
                              data={"file": (io.BytesIO(b"b"), "b.apk"), "name": "Second"}).get_json()
    assert body["added"] is True and body["created"] is False
    assert "First" in (tmp_path / "fdroid" / "metadata" / "org.example.agent.yml").read_text()


def test_publish_apk_create_if_missing_false(authed_client, tmp_path, monkeypatch):
    _fake_apk(monkeypatch, authed_client.app_module)
    resp = authed_client.post("/api/publish", content_type="multipart/form-data",
                              data={"file": (io.BytesIO(b"a"), "a.apk"), "createIfMissing": "0"})
    assert resp.status_code == 404
    assert not (tmp_path / "fdroid" / "repo" / "org.example.agent_7.apk").exists()


# --- MCP server ---------------------------------------------------------------

@pytest.fixture
def live_server(authed_client):
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", 0, authed_client.application, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _mcp(env, messages):
    script = os.path.join(os.path.dirname(__file__), "..", "scripts", "feather_mcp.py")
    proc = subprocess.run([sys.executable, script], input="".join(json.dumps(m) + "\n" for m in messages),
                          capture_output=True, text=True, env=env, timeout=60)
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()], proc.stderr


def test_mcp_server_publishes_and_lists(authed_client, live_server, tmp_path):
    tok = _token(authed_client, "agent")["token"]
    ipa = tmp_path / "agent.ipa"
    ipa.write_bytes(_minimal_ipa("com.agent.mcp", "5.0"))
    env = {**os.environ, "FEATHER_URL": live_server, "FEATHER_TOKEN": tok}
    out, err = _mcp(env, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "publish_app", "arguments": {"path": str(ipa), "name": "MCP App"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "list_apps", "arguments": {"platform": "ios"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "publish_app", "arguments": {"path": str(tmp_path / "missing.ipa")}}},
        {"jsonrpc": "2.0", "id": 6, "method": "nope"},
    ])
    by_id = {m["id"]: m for m in out}
    assert set(by_id) == {1, 2, 3, 4, 5, 6}, err
    assert by_id[1]["result"]["serverInfo"]["name"] == "feather"
    assert by_id[1]["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in by_id[2]["result"]["tools"]}
    assert {"publish_app", "list_apps", "get_app", "repo_status"} <= names
    published = json.loads(by_id[3]["result"]["content"][0]["text"])
    assert by_id[3]["result"]["isError"] is False
    assert published["id"] == "com.agent.mcp" and published["added"] is True
    listed = by_id[4]["result"]["content"][0]["text"]
    assert "com.agent.mcp" in listed
    assert by_id[5]["result"]["isError"] is True
    assert by_id[6]["error"]["code"] == -32601
    assert (tmp_path / "ipas" / "com.agent.mcp" / "5.0.ipa").exists()


def test_mcp_server_reports_bad_token(authed_client, live_server, tmp_path):
    ipa = tmp_path / "agent.ipa"
    ipa.write_bytes(_minimal_ipa("com.agent.mcp", "5.0"))
    env = {**os.environ, "FEATHER_URL": live_server, "FEATHER_TOKEN": "ftr_bogus"}
    out, _ = _mcp(env, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "publish_app", "arguments": {"path": str(ipa)}}}])
    assert out[0]["result"]["isError"] is True
    assert "401" in out[0]["result"]["content"][0]["text"]
