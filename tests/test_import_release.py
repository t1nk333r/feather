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
import struct
import zipfile
import zlib

import pytest
from PIL import Image


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


def build_cgbi_png_bytes(img):
    """Encode a PIL RGBA image as a minimal Apple CgBI PNG: BGRA pixels,
    premultiplied alpha, raw-deflate (no zlib header) IDAT, with a CgBI
    chunk -- the inverse of `_decode_cgbi_png`'s reversal. Alpha is kept
    >=150 so the round trip through floor-based un-premultiply in
    `_decode_cgbi_png` stays within +/-1 per channel (low alpha values are
    inherently lossy to premultiply/un-premultiply -- not a decoder bug).
    """
    w, h = img.size
    img = img.convert("RGBA")
    px = img.tobytes()
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter type: None
        row = px[y * stride:(y + 1) * stride]
        for x in range(0, stride, 4):
            r, g, b, a = row[x], row[x + 1], row[x + 2], row[x + 3]
            pr = (r * a + 127) // 255
            pg = (g * a + 127) // 255
            pb = (b * a + 127) // 255
            raw.extend([pb, pg, pr, a])

    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    idat = compressor.compress(bytes(raw)) + compressor.flush()

    def chunk(ctype, data):
        return (
            struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"CgBI", b"\x00\x00\x00\x02")
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def build_ipa_bytes_with_icon(
    bundle_id="com.example.app",
    version="1.0.0",
    name="Test App",
    itunes_artwork=None,
    appicon_png=None,
    appicon_name="AppIcon60x60@2x.png",
):
    """Like `build_ipa_bytes`, optionally adding a root `iTunesArtwork` and/or
    a `Payload/App.app/<appicon_name>` referenced from Info.plist's
    CFBundleIconFiles."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        plist = {
            "CFBundleIdentifier": bundle_id,
            "CFBundleShortVersionString": version,
            "CFBundleDisplayName": name,
        }
        if appicon_png is not None:
            plist["CFBundleIcons"] = {
                "CFBundlePrimaryIcon": {"CFBundleIconFiles": ["AppIcon60x60"]}
            }
        zf.writestr(
            "Payload/App.app/Info.plist", plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)
        )
        if itunes_artwork is not None:
            zf.writestr("iTunesArtwork", itunes_artwork)
        if appicon_png is not None:
            zf.writestr(f"Payload/App.app/{appicon_name}", appicon_png)
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


def test_import_existing_app_sets_icon_from_url(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest
    ipa_bytes = build_ipa_bytes(bundle_id="com.example.app", version="2.0.0")
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    mp = pytest.MonkeyPatch()
    mp.setattr(app_module.source_manager, "download_icon_from_url", lambda url, bundle_id: "png")
    try:
        status, events = post_and_collect(
            authed_client,
            {"provider": "github", "project": "owner/repo", "bundleIdentifier": "",
             "createIfMissing": False, "iconURL": "https://example.test/logo.png"},
            {"select_candidate": make_fake_select_candidate(candidate),
             "stream_download": make_fake_stream_download(ipa_bytes)},
        )
    finally:
        mp.undo()

    assert status == 200
    assert events[-1]["stage"] == "done", events
    app_info = app_module.source_manager.get_app("com.example.app")
    assert "2.0.0" in [v["version"] for v in app_info["versions"]]
    assert app_info["iconURL"].endswith("/icons/com.example.app/icon.png")


def test_import_existing_app_auto_extracts_icon_when_missing(authed_client, tmp_path):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest
    ipa_bytes = build_ipa_bytes(bundle_id="com.example.app", version="2.0.0")
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    # a real 1x1 PNG on disk for _extract_ipa_icon to return
    png = tmp_path / "extracted.png"
    png.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000154a24f9f0000000049454e44ae426082"))
    mp = pytest.MonkeyPatch()
    mp.setattr(app_module, "_extract_ipa_icon", lambda p: str(png))
    try:
        status, events = post_and_collect(
            authed_client,
            {"provider": "github", "project": "owner/repo", "bundleIdentifier": "",
             "createIfMissing": False},
            {"select_candidate": make_fake_select_candidate(candidate),
             "stream_download": make_fake_stream_download(ipa_bytes)},
        )
    finally:
        mp.undo()

    assert status == 200
    assert events[-1]["stage"] == "done", events
    app_info = app_module.source_manager.get_app("com.example.app")
    assert app_info["iconURL"].endswith("/icons/com.example.app/icon.png")


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


def test_import_normalizes_pasted_github_url(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.example.app", version="2.0.0")
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    recorded = {}

    def fake_select_candidate(job, session, tokens, timeout=30):
        recorded["project"] = job.project
        return candidate

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "https://github.com/Owner/Repo",
            "bundleIdentifier": "",
            "createIfMissing": False,
        },
        {
            "select_candidate": fake_select_candidate,
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events
    assert recorded["project"] == "Owner/Repo"


def test_import_rejects_unparseable_github_project(authed_client):
    def boom(*args, **kwargs):
        raise AssertionError("select_candidate must not be called for an unparseable project")

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "not a repo",
            "createIfMissing": False,
        },
        {"select_candidate": boom},
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "error", events
    assert "owner/repo" in last["error"].lower()


def test_import_new_app_auto_names_from_ipa(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.new.app", version="1.0.0", name="AnymeX")
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "owner/repo",
            "bundleIdentifier": "",
            "createIfMissing": True,
            "name": "",
            "developerName": "",
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events

    app_info = app_module.source_manager.get_app("com.new.app")
    assert app_info is not None
    assert app_info["name"] == "AnymeX"
    # developerName falls back to the repo owner (from "owner/repo"), not "Unknown" --
    # see test_import_new_app_developer_from_repo_owner for the dedicated coverage.
    assert app_info["developerName"] == "owner"


def test_import_new_app_sets_icon_url(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.icon.app", version="1.0.0", name="IconApp")
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    mp = pytest.MonkeyPatch()
    mp.setattr(app_module.source_manager, "download_icon_from_url", lambda url, bundle_id: "png")
    try:
        status, events = post_and_collect(
            authed_client,
            {
                "provider": "github",
                "project": "owner/repo",
                "bundleIdentifier": "",
                "createIfMissing": True,
                "name": "Icon App",
                "developerName": "Icon Dev",
                "iconURL": "https://example.test/icon.png",
            },
            {
                "select_candidate": make_fake_select_candidate(candidate),
                "stream_download": make_fake_stream_download(ipa_bytes),
            },
        )
    finally:
        mp.undo()

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events

    app_info = app_module.source_manager.get_app("com.icon.app")
    assert app_info is not None
    assert app_info["iconURL"].endswith("/icons/com.icon.app/icon.png")


def test_import_new_app_uses_release_body_as_description(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.new.app", version="1.0.0", name="AnymeX")
    candidate = make_candidate(
        release_ingest,
        declared_size=len(ipa_bytes),
        release_body="AnymeX is an anime streaming app.",
    )

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "owner/repo",
            "bundleIdentifier": "",
            "createIfMissing": True,
            "name": "",
            "developerName": "",
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events

    app_info = app_module.source_manager.get_app("com.new.app")
    assert app_info is not None
    assert app_info["localizedDescription"] == "AnymeX is an anime streaming app."


def test_import_new_app_developer_from_repo_owner(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.new.app", version="1.0.0", name="AnymeX")
    candidate = make_candidate(
        release_ingest, project="RyanYuuki/AnymeX", declared_size=len(ipa_bytes)
    )

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "RyanYuuki/AnymeX",
            "bundleIdentifier": "",
            "createIfMissing": True,
            "name": "",
            "developerName": "",
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events

    app_info = app_module.source_manager.get_app("com.new.app")
    assert app_info is not None
    assert app_info["developerName"] == "RyanYuuki"


def test_import_user_developer_overrides_repo_owner(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.new.app", version="1.0.0", name="AnymeX")
    candidate = make_candidate(
        release_ingest, project="RyanYuuki/AnymeX", declared_size=len(ipa_bytes)
    )

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "RyanYuuki/AnymeX",
            "bundleIdentifier": "",
            "createIfMissing": True,
            "name": "",
            "developerName": "Custom Dev",
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events

    app_info = app_module.source_manager.get_app("com.new.app")
    assert app_info is not None
    assert app_info["developerName"] == "Custom Dev"


def test_import_long_release_body_is_truncated(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.new.app", version="1.0.0", name="AnymeX")
    long_body = "x" * 2000
    candidate = make_candidate(
        release_ingest, declared_size=len(ipa_bytes), release_body=long_body
    )

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "owner/repo",
            "bundleIdentifier": "",
            "createIfMissing": True,
            "name": "",
            "developerName": "",
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events

    app_info = app_module.source_manager.get_app("com.new.app")
    assert app_info is not None
    description = app_info["localizedDescription"]
    assert len(description) <= 801
    assert description.endswith("…")


def test_import_empty_release_body_leaves_description_blank(authed_client):
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.new.app", version="1.0.0", name="AnymeX")
    candidate = make_candidate(
        release_ingest, declared_size=len(ipa_bytes), release_body=""
    )

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "owner/repo",
            "bundleIdentifier": "",
            "createIfMissing": True,
            "name": "",
            "developerName": "",
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events

    app_info = app_module.source_manager.get_app("com.new.app")
    assert app_info is not None
    assert not app_info.get("localizedDescription")


# ---------------------------------------------------------------------------
# Plan 046: IPA icon extraction (CgBI-aware) with GitHub-avatar fallback
# ---------------------------------------------------------------------------


def test_import_extracts_itunesartwork_icon(authed_client):
    """A root `iTunesArtwork` (a standard PNG) is preferred and hosted."""
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    artwork = Image.new("RGBA", (512, 512), (255, 0, 0, 255))
    artwork_buf = io.BytesIO()
    artwork.save(artwork_buf, format="PNG")

    ipa_bytes = build_ipa_bytes_with_icon(
        bundle_id="com.icon.itunesartwork",
        version="1.0.0",
        name="ArtworkApp",
        itunes_artwork=artwork_buf.getvalue(),
    )
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "owner/repo",
            "bundleIdentifier": "",
            "createIfMissing": True,
            "name": "",
            "developerName": "",
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events

    app_info = app_module.source_manager.get_app("com.icon.itunesartwork")
    assert app_info is not None
    assert app_info["iconURL"].endswith("/icons/com.icon.itunesartwork/icon.png")
    assert app_module.icon_storage.exists("com.icon.itunesartwork", "png")


def test_import_extracts_appicon_png(authed_client):
    """A `Payload/X.app/AppIcon60x60@2x.png` named via Info.plist is
    extracted and hosted (exercised here as a standard, Pillow-openable PNG;
    the CgBI reversal itself is covered by the dedicated unit test)."""
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    icon = Image.new("RGBA", (120, 120), (0, 255, 0, 255))
    icon_buf = io.BytesIO()
    icon.save(icon_buf, format="PNG")

    ipa_bytes = build_ipa_bytes_with_icon(
        bundle_id="com.icon.appicon",
        version="1.0.0",
        name="AppIconApp",
        appicon_png=icon_buf.getvalue(),
        appicon_name="AppIcon60x60@2x.png",
    )
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "github",
            "project": "owner/repo",
            "bundleIdentifier": "",
            "createIfMissing": True,
            "name": "",
            "developerName": "",
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events

    app_info = app_module.source_manager.get_app("com.icon.appicon")
    assert app_info is not None
    assert app_info["iconURL"].endswith("/icons/com.icon.appicon/icon.png")
    assert app_module.icon_storage.exists("com.icon.appicon", "png")


def test_extract_ipa_icon_cgbi_roundtrips(authed_client):
    """Unit test of `_decode_cgbi_png`: encode a known RGBA image into a
    minimal CgBI PNG (BGRA + premultiplied alpha + raw-deflate IDAT + a
    `CgBI` chunk) and confirm the decoded pixels round-trip within +/-1 per
    channel (alpha kept >=150 so premultiply/un-premultiply rounding stays
    tightly bounded -- see `build_cgbi_png_bytes`)."""
    app_module = authed_client.app_module

    width, height = 8, 6
    original = Image.new("RGBA", (width, height))
    pixels = original.load()
    for y in range(height):
        for x in range(width):
            r = (x * 30) % 256
            g = (y * 40) % 256
            b = (x + y) * 10 % 256
            a = 150 + (x * y * 3) % 106
            pixels[x, y] = (r, g, b, a)

    cgbi_bytes = build_cgbi_png_bytes(original)
    decoded = app_module._decode_cgbi_png(cgbi_bytes)

    assert decoded.size == (width, height)
    assert decoded.mode == "RGBA"

    decoded_pixels = decoded.load()
    for y in range(height):
        for x in range(width):
            orig = pixels[x, y]
            got = decoded_pixels[x, y]
            assert got[3] == orig[3], f"alpha mismatch at ({x},{y}): {orig} vs {got}"
            for channel in range(3):
                assert abs(got[channel] - orig[channel]) <= 1, (
                    f"channel {channel} mismatch at ({x},{y}): {orig} vs {got}"
                )


def test_import_no_icon_falls_back_to_avatar(authed_client):
    """A github import with no user icon URL and no extractable IPA icon
    falls back to the GitHub owner avatar."""
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.noicon.github", version="1.0.0", name="NoIconApp")
    candidate = make_candidate(
        release_ingest, project="AvatarOwner/repo", declared_size=len(ipa_bytes)
    )

    captured_urls = []

    def fake_download_icon(url, bundle_id):
        captured_urls.append(url)
        return "png"

    mp = pytest.MonkeyPatch()
    mp.setattr(app_module.source_manager, "download_icon_from_url", fake_download_icon)
    try:
        status, events = post_and_collect(
            authed_client,
            {
                "provider": "github",
                "project": "AvatarOwner/repo",
                "bundleIdentifier": "",
                "createIfMissing": True,
                "name": "",
                "developerName": "",
            },
            {
                "select_candidate": make_fake_select_candidate(candidate),
                "stream_download": make_fake_stream_download(ipa_bytes),
            },
        )
    finally:
        mp.undo()

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events

    assert captured_urls == ["https://github.com/AvatarOwner.png"]

    app_info = app_module.source_manager.get_app("com.noicon.github")
    assert app_info is not None
    assert app_info["iconURL"].endswith("/icons/com.noicon.github/icon.png")


def test_import_gitlab_no_icon_no_avatar(authed_client):
    """A gitlab import with no extractable icon gets no icon at all (no
    GitLab avatar fallback) and does not crash."""
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    ipa_bytes = build_ipa_bytes(bundle_id="com.noicon.gitlab", version="1.0.0", name="NoIconApp")
    candidate = make_candidate(
        release_ingest,
        provider="gitlab",
        project="group/project",
        declared_size=len(ipa_bytes),
        auth_host="gitlab.example.test",
    )

    status, events = post_and_collect(
        authed_client,
        {
            "provider": "gitlab",
            "project": "group/project",
            "bundleIdentifier": "",
            "createIfMissing": True,
            "name": "",
            "developerName": "",
            "allowedDownloadHosts": ["gitlab.example.test"],
        },
        {
            "select_candidate": make_fake_select_candidate(candidate),
            "stream_download": make_fake_stream_download(ipa_bytes),
        },
    )

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events

    app_info = app_module.source_manager.get_app("com.noicon.gitlab")
    assert app_info is not None
    assert not app_info.get("iconURL")
    assert not app_module.icon_storage.exists("com.noicon.gitlab", "png")


def test_import_explicit_icon_url_still_wins(authed_client):
    """A form Icon URL beats IPA extraction, even when the IPA ships an
    extractable icon."""
    app_module = authed_client.app_module
    release_ingest = app_module.release_ingest

    artwork = Image.new("RGBA", (100, 100), (10, 20, 30, 255))
    artwork_buf = io.BytesIO()
    artwork.save(artwork_buf, format="PNG")

    ipa_bytes = build_ipa_bytes_with_icon(
        bundle_id="com.icon.explicit",
        version="1.0.0",
        name="ExplicitIconApp",
        itunes_artwork=artwork_buf.getvalue(),
    )
    candidate = make_candidate(release_ingest, declared_size=len(ipa_bytes))

    called_extract = []
    mp = pytest.MonkeyPatch()
    real_extract = app_module._extract_ipa_icon

    def spying_extract(*args, **kwargs):
        called_extract.append(True)
        return real_extract(*args, **kwargs)

    mp.setattr(app_module, "_extract_ipa_icon", spying_extract)
    mp.setattr(app_module.source_manager, "download_icon_from_url", lambda url, bundle_id: "png")
    try:
        status, events = post_and_collect(
            authed_client,
            {
                "provider": "github",
                "project": "owner/repo",
                "bundleIdentifier": "",
                "createIfMissing": True,
                "name": "",
                "developerName": "",
                "iconURL": "https://example.test/explicit-icon.png",
            },
            {
                "select_candidate": make_fake_select_candidate(candidate),
                "stream_download": make_fake_stream_download(ipa_bytes),
            },
        )
    finally:
        mp.undo()

    assert status == 200
    last = events[-1]
    assert last["stage"] == "done", events

    # The IPA icon extractor must never even run when a form icon URL is set.
    assert called_extract == []

    app_info = app_module.source_manager.get_app("com.icon.explicit")
    assert app_info is not None
    assert app_info["iconURL"].endswith("/icons/com.icon.explicit/icon.png")
