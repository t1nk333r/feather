"""Device backups (plan 088): an app on the operator's own phone keeps copies of
its settings under data/device-backups/, reached with a session or a
backup-kind token -- never a publish token, and never anonymously."""

import hashlib
import io
import os
import zipfile

import pytest

from tests.test_routes import (  # noqa: F401
    client, authed_client, _minimal_ipa,
)
from tests.test_publish_api import _anon, _bearer, _token


APP = "dev.example.launcher"
DEVICE = "a1b2c3d4-phone"


@pytest.fixture
def backups(authed_client, monkeypatch):
    monkeypatch.setattr(authed_client.app_module, "DEVICE_BACKUPS", True)
    return authed_client


def _backup_token(authed_client, apps=(APP,), name="phone"):
    resp = authed_client.post("/api/tokens", json={"name": name, "kind": "backup", "apps": list(apps)})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _zip(files=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in (files or {"manifest.json": b'{"format": 1}', "prefs/launcher.xml": b"<map/>"}).items():
            z.writestr(name, data)
    return buf.getvalue()


def _upload(c, data, headers=None, app=APP, device=DEVICE, label="Pixel"):
    return c.post(f"/api/device-backups/{app}/{device}", headers=headers or {},
                  content_type="multipart/form-data",
                  data={"file": (io.BytesIO(data), "backup.zip"), "label": label})


# --- off by default -----------------------------------------------------------

def test_routes_are_off_by_default(authed_client):
    assert authed_client.app_module.DEVICE_BACKUPS is False
    assert _upload(authed_client, _zip()).status_code == 404
    assert authed_client.get(f"/api/device-backups/{APP}").status_code == 404
    assert authed_client.get(f"/api/device-backups/{APP}/{DEVICE}").status_code == 404
    assert authed_client.get("/api/device-backups").get_json() == {"success": True, "enabled": False, "apps": []}


# --- token kinds --------------------------------------------------------------

def test_backup_token_must_name_its_apps_and_lists_its_kind(authed_client):
    assert authed_client.post("/api/tokens", json={"name": "p", "kind": "backup"}).status_code == 400
    assert authed_client.post("/api/tokens", json={"name": "p", "kind": "admin"}).status_code == 400
    _backup_token(authed_client)
    _token(authed_client, name="ci")
    kinds = {t["name"]: t["kind"] for t in authed_client.get("/api/tokens").get_json()}
    assert kinds == {"phone": "backup", "ci": "publish"}


def test_backup_token_cannot_publish_or_read_the_catalogue(backups):
    tok = _backup_token(backups, apps=("com.agent.one",))["token"]
    anon = _anon(backups)
    resp = anon.post("/api/publish", headers=_bearer(tok), content_type="multipart/form-data",
                     data={"file": (io.BytesIO(_minimal_ipa("com.agent.one", "1.0")), "a.ipa")})
    assert resp.status_code == 403
    assert "device backups" in resp.get_json()["error"]
    assert anon.get("/api/android/apps", headers=_bearer(tok)).status_code == 403
    assert anon.post("/api/app-details", json={"id": "com.agent.one", "subtitle": "x"},
                     headers=_bearer(tok)).status_code == 403


def test_publish_token_and_anonymous_are_refused(backups):
    tok = _token(backups)["token"]
    anon = _anon(backups)
    assert _upload(anon, _zip()).status_code == 401
    assert anon.get(f"/api/device-backups/{APP}").status_code == 401
    assert _upload(anon, _zip(), headers=_bearer(tok)).status_code == 403
    assert anon.get(f"/api/device-backups/{APP}", headers=_bearer(tok)).status_code == 403
    assert anon.get("/api/device-backups", headers=_bearer(tok)).status_code == 401
    # Even one limited to the same app: publishing it doesn't make it a backup token.
    scoped = backups.post("/api/tokens", json={"name": "ci-launcher", "apps": [APP]}).get_json()["token"]
    assert _upload(anon, _zip(), headers=_bearer(scoped)).status_code == 403
    assert anon.get(f"/api/device-backups/{APP}/{DEVICE}", headers=_bearer(scoped)).status_code == 403


def test_backup_token_is_limited_to_its_apps(backups):
    tok = _backup_token(backups)["token"]
    anon = _anon(backups)
    assert _upload(anon, _zip(), headers=_bearer(tok), app="org.other.app").status_code == 403
    assert anon.get("/api/device-backups/org.other.app", headers=_bearer(tok)).status_code == 403


# --- round trip ---------------------------------------------------------------

def test_upload_list_and_download(backups, tmp_path):
    tok = _backup_token(backups)["token"]
    anon = _anon(backups)
    data = _zip()
    resp = _upload(anon, data, headers=_bearer(tok))
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["success"] and body["unchanged"] is False
    snap = body["snapshot"]
    assert snap["sha256"] == hashlib.sha256(data).hexdigest() and snap["size"] == len(data)

    devices = anon.get(f"/api/device-backups/{APP}", headers=_bearer(tok)).get_json()["devices"]
    assert [(d["id"], d["label"], d["snapshots"]) for d in devices] == [(DEVICE, "Pixel", 1)]
    assert devices[0]["lastBackup"] == snap["created"]

    listed = anon.get(f"/api/device-backups/{APP}/{DEVICE}", headers=_bearer(tok)).get_json()
    assert listed["label"] == "Pixel" and listed["snapshots"] == [snap]

    got = anon.get(f"/api/device-backups/{APP}/{DEVICE}/{snap['name']}", headers=_bearer(tok))
    assert got.status_code == 200 and got.data == data
    assert got.headers["X-Content-SHA256"] == snap["sha256"]
    assert got.mimetype == "application/zip"
    stored = tmp_path / "device-backups" / APP / DEVICE / snap["name"]
    assert stored.read_bytes() == data


def test_an_unchanged_backup_is_not_stored_again(backups, tmp_path):
    tok = _backup_token(backups)["token"]
    anon = _anon(backups)
    first = _upload(anon, _zip(), headers=_bearer(tok)).get_json()["snapshot"]
    again = _upload(anon, _zip(), headers=_bearer(tok), label="Pixel 9").get_json()
    assert again["unchanged"] is True and again["snapshot"] == first
    listed = anon.get(f"/api/device-backups/{APP}/{DEVICE}", headers=_bearer(tok)).get_json()
    assert len(listed["snapshots"]) == 1 and listed["label"] == "Pixel 9"


def test_newest_first_and_pruned_to_keep(backups, monkeypatch, tmp_path):
    monkeypatch.setattr(backups.app_module, "DEVICE_BACKUP_KEEP", 3)
    names = []
    for i in range(5):
        resp = _upload(backups, _zip({"n.txt": str(i).encode()}))
        assert resp.status_code == 200
        names.append(resp.get_json()["snapshot"]["name"])
    listed = backups.get(f"/api/device-backups/{APP}/{DEVICE}").get_json()["snapshots"]
    # Five uploads within one second still get distinct, ordered names.
    assert [s["name"] for s in listed] == names[::-1][:3]
    left = sorted(os.listdir(tmp_path / "device-backups" / APP / DEVICE))
    assert left == sorted([n for n in names[2:]] + [n + ".sha256" for n in names[2:]] + ["device.json"])


def test_device_count_is_capped(backups, monkeypatch):
    monkeypatch.setattr(backups.app_module, "DEVICE_BACKUP_MAX_DEVICES", 2)
    assert _upload(backups, _zip(), device="one").status_code == 200
    assert _upload(backups, _zip(), device="two").status_code == 200
    assert _upload(backups, _zip(), device="three").status_code == 400
    assert _upload(backups, _zip({"x": b"new"}), device="two").status_code == 200


# --- validation ---------------------------------------------------------------

@pytest.mark.parametrize("device", ["..", ".hidden", "a" * 65, "bad id", "x%2Fy"])
def test_bad_device_ids_are_refused(backups, device, tmp_path):
    # A decoded "/" lands on the GET/DELETE snapshot route: 405, nothing stored either.
    assert _upload(backups, _zip(), device=device).status_code in (400, 404, 405)
    assert not (tmp_path / "device-backups" / APP).exists()


@pytest.mark.parametrize("app", ["..", ".x", "a b"])
def test_bad_app_ids_are_refused(backups, app):
    assert _upload(backups, _zip(), app=app).status_code in (400, 404)


@pytest.mark.parametrize("payload", [
    b"",
    b"not a zip at all",
    "../escape.txt",
    "/etc/passwd",
    "dir\\file",
])
def test_bad_archives_are_refused(backups, tmp_path, payload):
    data = payload if isinstance(payload, bytes) else _zip({payload: b"x"})
    resp = _upload(backups, data)
    assert resp.status_code == 400, resp.get_json()
    root = tmp_path / "device-backups"
    leftovers = [p for p in root.rglob("*") if p.is_file()] if root.exists() else []
    assert leftovers == []


def test_oversized_upload_is_refused_without_leftovers(backups, monkeypatch, tmp_path):
    monkeypatch.setattr(backups.app_module, "DEVICE_BACKUP_MAX_BYTES", 64)
    resp = _upload(backups, _zip({"big.bin": os.urandom(500)}))
    assert resp.status_code == 413
    root = tmp_path / "device-backups"
    assert [p for p in root.rglob("*") if p.is_file()] == []


def test_snapshot_names_cannot_escape(backups, tmp_path):
    _upload(backups, _zip())
    for name in ("device.json", "..%2F..%2Fapi-tokens.json", "x.zip", "20261021T101500Z.zip.sha256"):
        assert backups.get(f"/api/device-backups/{APP}/{DEVICE}/{name}").status_code == 404


def test_label_is_cleaned_and_bounded(backups):
    _upload(backups, _zip(), label="  S24\x00 Ultra\n" + "x" * 200)
    label = backups.get(f"/api/device-backups/{APP}/{DEVICE}").get_json()["label"]
    assert label.startswith("S24 Ultra") and len(label) == 80 and "\x00" not in label


# --- deleting is session-only --------------------------------------------------

def test_only_a_session_deletes(backups, tmp_path):
    tok = _backup_token(backups)["token"]
    anon = _anon(backups)
    name = _upload(anon, _zip(), headers=_bearer(tok)).get_json()["snapshot"]["name"]
    assert anon.delete(f"/api/device-backups/{APP}/{DEVICE}/{name}", headers=_bearer(tok)).status_code == 401
    assert anon.delete(f"/api/device-backups/{APP}/{DEVICE}", headers=_bearer(tok)).status_code == 401
    assert backups.delete(f"/api/device-backups/{APP}/{DEVICE}/{name}").get_json()["success"]
    assert backups.get(f"/api/device-backups/{APP}/{DEVICE}").get_json()["snapshots"] == []
    assert backups.delete(f"/api/device-backups/{APP}/{DEVICE}").get_json()["success"]
    assert not (tmp_path / "device-backups" / APP / DEVICE).exists()


def test_overview_for_the_admin_ui(backups):
    _upload(backups, _zip())
    body = backups.get("/api/device-backups").get_json()
    assert body["enabled"] is True
    assert [(a["id"], [d["id"] for d in a["devices"]]) for a in body["apps"]] == [(APP, [DEVICE])]
    device = body["apps"][0]["devices"][0]
    assert set(device) == {"id", "label", "snapshots", "lastBackup", "size"}  # no server paths
