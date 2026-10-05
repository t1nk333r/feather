"""Regression tests for the 2026-10 audit's security findings (#1-#6).
Each test was checked to fail with its fix reverted."""

import http.server
import io
import json
import os
import plistlib
import re
import struct
import tempfile
import threading
import time
import zipfile
import zlib

import pytest

from tests.test_routes import client, authed_client, _minimal_ipa  # noqa: F401
from tests.test_publish_api import _token, _anon, _bearer, live_server, _mcp  # noqa: F401
from tests.test_auto_import import (  # noqa: F401
    build_ipa_bytes, make_candidate, make_fake_select_candidate, make_fake_stream_download,
)
from tests.test_release_source_ingest import FakeSession, FakeResponse
from scripts import release_source_ingest as ingest
from scripts.ipa_inspection import inspect_ipa, InspectionError
from scripts.apk_inspection import inspect_apk, ApkInspectionError, extract_apk_icon

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")


def _ipa(bundle_id, version, build=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Payload/A.app/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version,
            "CFBundleVersion": build or version}))
        zf.writestr("Payload/A.app/A", b"\0" * 64)
    return buf.getvalue()


# --- #1 stored XSS via IPA identity in inline handlers -------------------------

def test_admin_ui_never_interpolates_into_inline_handlers():
    html = open(TEMPLATE, encoding="utf-8").read()
    # A value inside on*="...'${...}'..." is decoded from HTML entities before
    # the JS is compiled, so escapeHtml cannot protect it. Values go in data-*.
    offenders = re.findall(r'''on[a-z]+="[^"]*'\$\{''', html)
    assert offenders == []


@pytest.mark.parametrize("bundle_id,version", [
    ("com.x');alert(1);//", "1.0"),
    ("com.good.app", "1.0');alert(1);//"),
    ("com.good.app", '1.0"><img src=x>'),
    ("com.good.app", "1.0\n2"),
    ("../../etc", "1.0"),
])
def test_ipa_with_hostile_identity_is_refused(tmp_path, bundle_id, version):
    path = tmp_path / "a.ipa"
    path.write_bytes(_ipa(bundle_id, version))
    with pytest.raises(InspectionError, match="invalid"):
        inspect_ipa(str(path))


@pytest.mark.parametrize("bundle_id,version,build", [
    ("com.michael-128.qBitControl", "408.1.0_TH", "408.1.0_TH"),
    ("MikeMichael225.qBitControl", "1.0 (2)", "45"),
    ("com.fouadraheb.watusi", "B_25.36.10_WC", "25.36.10"),
    ("com.example.app", "2.0.0-beta+3", "2.0.0~b3"),
])
def test_real_world_identities_still_accepted(tmp_path, bundle_id, version, build):
    path = tmp_path / "a.ipa"
    path.write_bytes(_ipa(bundle_id, version, build))
    assert inspect_ipa(str(path)).bundle_identifier == bundle_id


def test_scoped_token_cannot_publish_hostile_version(authed_client, tmp_path):
    tok = authed_client.post("/api/tokens", json={"name": "s", "apps": "com.good.app"}).get_json()["token"]
    resp = _anon(authed_client).post("/api/publish", headers=_bearer(tok), content_type="multipart/form-data",
                                     data={"file": (io.BytesIO(_ipa("com.good.app", "3.0');x=1;//")), "a.ipa")})
    assert resp.status_code == 400
    assert not (tmp_path / "ipas" / "com.good.app").exists()


# --- #2 token-triggered server-side fetches ------------------------------------

def test_token_cannot_make_server_fetch_url(authed_client, monkeypatch):
    calls = []
    monkeypatch.setattr(authed_client.app_module.requests, "get", lambda *a, **k: calls.append(a) or None)
    tok = _token(authed_client)["token"]
    resp = _anon(authed_client).post("/api/publish", headers=_bearer(tok),
                                     json={"url": "http://127.0.0.1:9/internal"})
    assert resp.status_code == 400 and "upload" in resp.get_json()["error"]
    assert calls == []


class _IpaHandler(http.server.BaseHTTPRequestHandler):
    payload = _minimal_ipa("com.agent.fromurl", "4.2")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *a):
        pass


def test_mcp_downloads_url_itself_and_uploads(authed_client, live_server, tmp_path):
    server = http.server.HTTPServer(("127.0.0.1", 0), _IpaHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        tok = _token(authed_client, "agent")["token"]
        env = {**os.environ, "FEATHER_URL": live_server, "FEATHER_TOKEN": tok}
        out, err = _mcp(env, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "publish_app",
            "arguments": {"url": f"http://127.0.0.1:{server.server_port}/build/app.ipa",
                          "create_if_missing": "true"}}}])
    finally:
        server.shutdown()
    result = out[0]["result"]
    assert result["isError"] is False, (result, err)
    assert json.loads(result["content"][0]["text"])["id"] == "com.agent.fromurl"
    assert (tmp_path / "ipas" / "com.agent.fromurl" / "4.2.ipa").exists()


def test_mcp_create_if_missing_string_false_is_false(authed_client, live_server, tmp_path):
    ipa = tmp_path / "x.ipa"
    ipa.write_bytes(_minimal_ipa("com.agent.notnew", "1.0"))
    env = {**os.environ, "FEATHER_URL": live_server, "FEATHER_TOKEN": _token(authed_client)["token"]}
    out, _ = _mcp(env, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "publish_app", "arguments": {"path": str(ipa), "create_if_missing": "false"}}}])
    assert out[0]["result"]["isError"] is True and "404" in out[0]["result"]["content"][0]["text"]


# --- #3 APK zip bomb hidden behind a small declared size -------------------------

def _bomb_apk(path, member="AndroidManifest.xml", real=64 << 20, declared=1000):
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = b"".join(comp.compress(bytes(1 << 20)) for _ in range(real >> 20)) + comp.flush()
    name = member.encode()
    lfh = struct.pack("<4sHHHHHIIIHH", b"PK\x03\x04", 20, 0, 8, 0, 0, 0, len(body), declared, len(name), 0) + name
    cd = struct.pack("<4sHHHHHHIIIHHHHHII", b"PK\x01\x02", 20, 20, 0, 8, 0, 0, 0,
                     len(body), declared, len(name), 0, 0, 0, 0, 0, 0) + name
    data = lfh + body
    eocd = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 1, 1, len(cd), len(data), 0)
    path.write_bytes(data + cd + eocd)


def test_apk_bomb_with_lying_declared_size_is_refused_before_parsing(tmp_path, monkeypatch):
    import pyaxmlparser
    monkeypatch.setattr(pyaxmlparser, "APK", lambda *a, **k: pytest.fail("pyaxmlparser reached"))
    path = tmp_path / "bomb.apk"
    _bomb_apk(path)
    with pytest.raises(ApkInspectionError, match="implausibly large"):
        inspect_apk(str(path))
    assert extract_apk_icon(str(path)) is None


# --- #4 CgBI icon decoder bounds -------------------------------------------------

def _chunk(ctype, data):
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)


def _cgbi(width, height):
    rows = (b"\x00" + bytes(width * 4)) * height
    idat = zlib.compressobj(9, zlib.DEFLATED, -15)
    data = idat.compress(rows) + idat.flush()
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"CgBI", b"\x00\x00\x00\x02")
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", data) + _chunk(b"IEND", b""))


def test_cgbi_decoder_refuses_huge_dimensions_quickly(client):
    module = client.app_module
    start = time.monotonic()
    with pytest.raises(ValueError, match="exceeds"):
        module._decode_cgbi_png(_cgbi(8192, 8192))
    assert time.monotonic() - start < 5
    assert module._decode_cgbi_png(_cgbi(60, 60)).size == (60, 60)


def test_icon_pixel_cap_and_downscale(client):
    module = client.app_module
    from PIL import Image
    big = io.BytesIO()
    Image.new("1", (5000, 5000)).save(big, format="PNG")
    with pytest.raises(ValueError, match="implausibly large"):
        module._open_icon(big.getvalue())
    ok = io.BytesIO()
    Image.new("RGBA", (1024, 1024)).save(ok, format="PNG")
    path = module._save_temp_png(module._open_icon(ok.getvalue()))
    try:
        assert Image.open(path).size == (512, 512)
    finally:
        os.remove(path)


# --- #5 GitLab token only to GitLab ------------------------------------------------

def _gitlab_job_and_candidate(link_url):
    job = ingest.parse_manifest_dict({"schemaVersion": 1, "jobs": [{
        "id": "gl", "provider": "gitlab", "project": "group/proj", "assetGlob": "*.ipa",
        "allowedDownloadHosts": ["gitlab.com", "downloads.example.org"]}]})[0]
    releases = [{"id": 1, "tag_name": "v1", "released_at": "2020-01-01T00:00:00Z",
                 "assets": {"links": [{"id": 9, "name": "a.ipa", "url": link_url}]}}]
    return job, ingest.gitlab_select_candidate(job, releases)[0]


@pytest.mark.parametrize("link_url,expect_token", [
    ("https://downloads.example.org/a.ipa", False),
    ("https://gitlab.com/group/proj/-/releases/v1/downloads/a.ipa", True),
])
def test_gitlab_token_only_sent_to_gitlab(tmp_path, link_url, expect_token):
    job, cand = _gitlab_job_and_candidate(link_url)
    session = FakeSession()
    session.add_response(link_url, FakeResponse(200, content_chunks=[b"x" * 10]))
    try:
        ingest.stream_download(session, cand, job, str(tmp_path / "a.ipa"),
                               {"github": None, "gitlab": "glpat-SECRET"}, timeout=30, max_bytes=10_000_000)
    except Exception:
        pass   # later validation of the fake bytes is irrelevant here
    headers = session.calls[0][1]["headers"]
    assert ("PRIVATE-TOKEN" in headers) is expect_token


# --- #6 auto-import honours the configured bundleIdentifier ------------------------

@pytest.mark.parametrize("create_if_missing,ipa_bundle", [(False, "com.example.app"), (True, "com.other.app")])
def test_auto_import_refuses_ipa_with_other_bundle_id(authed_client, create_if_missing, ipa_bundle):
    module = authed_client.app_module
    ri = module.release_ingest
    job = {"id": "mism", "provider": "gitlab", "project": "group/project",
           "bundleIdentifier": "com.expected.app", "assetGlob": "*.ipa", "includePrereleases": False,
           "createIfMissing": create_if_missing, "name": "Expected", "developerName": "Dev",
           "allowedDownloadHosts": ["gitlab.example.com"]}
    assert authed_client.post("/api/auto-import/job", json=job).status_code == 200
    ipa = build_ipa_bytes(bundle_id=ipa_bundle, version="9.9.9")
    cand = make_candidate(ri, provider="gitlab", project="group/project", declared_size=len(ipa))
    mp = pytest.MonkeyPatch()
    mp.setattr(ri, "select_candidate", make_fake_select_candidate(cand))
    mp.setattr(ri, "stream_download", make_fake_stream_download(ipa))
    try:
        body = authed_client.post("/api/auto-import/run", json={"id": "mism"}).get_json()
    finally:
        mp.undo()
    assert body["results"]["mism"]["status"] != "published"
    assert [v["version"] for v in module.source_manager.get_app("com.example.app")["versions"]] == ["1.0.0"]
    assert module.source_manager.get_app("com.other.app") is None
