"""Plan 091: per-app version retention. An app with a policy keeps its newest
version plus `keepPreviousVersions` before it; an update deletes the rest,
and POST /api/retention/apply does the same on demand."""

import io
import json

import pytest

from tests.test_routes import client, authed_client, _minimal_ipa  # noqa: F401
from tests.test_publish_api import _anon, _bearer, _token


BUNDLE = "com.example.app"   # seeded by tests/test_routes.py at 1.0.0
PACKAGE = "org.example.nas"


def _set(client, platform, app_id, keep):
    return client.post("/api/retention", json={"platform": platform, "id": app_id,
                                                "keepPreviousVersions": keep})


def _publish_ipa(client, version, bundle=BUNDLE):
    resp = client.post("/api/publish", content_type="multipart/form-data", data={
        "file": (io.BytesIO(_minimal_ipa(bundle, version)), "x.ipa")})
    body = resp.get_json()
    assert resp.status_code == 200, body
    return body


def _ios_versions(tmp_path, bundle=BUNDLE):
    source = json.loads((tmp_path / "source.json").read_text())
    app = next(a for a in source["apps"] if a["bundleIdentifier"] == bundle)
    return [v["version"] for v in app["versions"]]


def _ipa(tmp_path, version, bundle=BUNDLE):
    return tmp_path / "ipas" / bundle / f"{version}.ipa"


# --- iOS ---------------------------------------------------------------------

def test_ios_update_keeps_newest_plus_previous_and_deletes_the_rest(authed_client, tmp_path):
    seeded = _ipa(tmp_path, "1.0.0")
    seeded.parent.mkdir(parents=True, exist_ok=True)
    seeded.write_bytes(b"old ipa")
    assert _set(authed_client, "ios", BUNDLE, 2).get_json()["success"] is True

    for version in ("1.1.0", "1.2.0"):
        assert _publish_ipa(authed_client, version)["pruned"] == []
    body = _publish_ipa(authed_client, "1.3.0")

    assert body["pruned"] == [{"version": "1.0.0"}]
    assert "Retention removed 1 older version: 1.0.0" in body["message"]
    assert _ios_versions(tmp_path) == ["1.3.0", "1.2.0", "1.1.0"]
    assert not seeded.exists()
    assert _ipa(tmp_path, "1.1.0").exists()


def test_ios_without_a_policy_keeps_every_version(authed_client, tmp_path):
    for version in ("1.1.0", "1.2.0", "1.3.0"):
        assert _publish_ipa(authed_client, version)["pruned"] == []
    assert _ios_versions(tmp_path) == ["1.3.0", "1.2.0", "1.1.0", "1.0.0"]


def test_ios_backport_below_the_newest_prunes_nothing(authed_client, tmp_path):
    _set(authed_client, "ios", BUNDLE, 0)
    assert _publish_ipa(authed_client, "2.0.0")["pruned"] == [{"version": "1.0.0"}]
    # An older build is not an update: it lands below 2.0.0 and deletes nothing.
    assert _publish_ipa(authed_client, "1.5.0")["pruned"] == []
    assert _ios_versions(tmp_path) == ["2.0.0", "1.5.0"]


def test_ios_republish_of_an_existing_version_prunes_nothing(authed_client, tmp_path):
    for version in ("1.1.0", "1.2.0"):
        _publish_ipa(authed_client, version)
    _set(authed_client, "ios", BUNDLE, 0)
    again = _publish_ipa(authed_client, "1.2.0")
    assert again["added"] is False
    assert _ios_versions(tmp_path) == ["1.2.0", "1.1.0", "1.0.0"]


def test_ios_policy_change_waits_for_an_update_or_apply(authed_client, tmp_path):
    for version in ("1.1.0", "1.2.0", "1.3.0"):
        _publish_ipa(authed_client, version)
    assert _set(authed_client, "ios", BUNDLE, 1).status_code == 200
    assert _ios_versions(tmp_path) == ["1.3.0", "1.2.0", "1.1.0", "1.0.0"]

    resp = authed_client.post("/api/retention/apply", json={"platform": "ios", "id": BUNDLE})
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert body["pruned"] == [{"version": "1.1.0"}, {"version": "1.0.0"}]
    assert _ios_versions(tmp_path) == ["1.3.0", "1.2.0"]
    assert not _ipa(tmp_path, "1.1.0").exists()

    again = authed_client.post("/api/retention/apply", json={"platform": "ios", "id": BUNDLE}).get_json()
    assert again["pruned"] == [] and again["message"] == "Nothing to remove"


def test_ios_failed_save_deletes_no_binary(authed_client, tmp_path, monkeypatch):
    seeded = _ipa(tmp_path, "1.0.0")
    seeded.parent.mkdir(parents=True, exist_ok=True)
    seeded.write_bytes(b"old ipa")
    _publish_ipa(authed_client, "1.1.0")
    _set(authed_client, "ios", BUNDLE, 0)
    monkeypatch.setattr(authed_client.app_module.source_manager, "save_source", lambda _data: False)
    resp = authed_client.post("/api/retention/apply", json={"platform": "ios", "id": BUNDLE})
    assert resp.status_code == 500
    assert seeded.exists()
    assert _ios_versions(tmp_path) == ["1.1.0", "1.0.0"]


def test_deleting_an_ios_app_forgets_its_policy(authed_client, tmp_path):
    _set(authed_client, "ios", BUNDLE, 1)
    assert authed_client.post("/api/delete-app", json={"bundleIdentifier": BUNDLE}).get_json()["success"]
    assert authed_client.get("/api/retention").get_json()["retention"]["ios"] == {}


# --- Android -----------------------------------------------------------------
# A real binary manifest cannot be authored in a test, so _inspect_apk is
# replaced by a parser of the fake APK body "apk:<package>:<code>:<name>".

def _fake_apk_bytes(code, name, package=PACKAGE):
    return f"apk:{package}:{code}:{name}".encode()


def _fake_inspect(path):
    raw = open(path, "rb").read().decode(errors="replace")
    parts = raw.split(":")
    if len(parts) != 4 or parts[0] != "apk":
        raise ValueError("not an APK")
    return {"package": parts[1], "version_code": int(parts[2]), "version_name": parts[3],
            "min_sdk": 23, "target_sdk": 34, "app_name": "NAS"}


@pytest.fixture
def droid(authed_client, monkeypatch):
    monkeypatch.setattr(authed_client.app_module, "_inspect_apk", _fake_inspect)
    monkeypatch.setattr(authed_client.app_module, "extract_apk_icon", lambda _p: None)
    return authed_client


def _repo(tmp_path):
    path = tmp_path / "fdroid" / "repo"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _seed_apks(tmp_path, releases, indexed=True):
    """releases: [(versionName, [codes])]. Writes the APKs and, when
    `indexed`, an index-v1.json that lists them as the sidecar would."""
    repo = _repo(tmp_path)
    packages = []
    for name, codes in releases:
        for code in codes:
            (repo / f"{PACKAGE}_{code}.apk").write_bytes(_fake_apk_bytes(code, name))
            packages.append({"versionCode": code, "versionName": name})
    if indexed:
        (repo / "index-v1.json").write_text(json.dumps({"apps": [], "packages": {PACKAGE: packages}}))


def _codes(tmp_path):
    return sorted(int(p.stem.rsplit("_", 1)[1]) for p in _repo(tmp_path).glob(f"{PACKAGE}_*.apk"))


def _publish_apk(client, code, name):
    resp = client.post("/api/publish", content_type="multipart/form-data", data={
        "file": (io.BytesIO(_fake_apk_bytes(code, name)), "x.apk")})
    body = resp.get_json()
    assert resp.status_code == 200, body
    return body


# NAS Player ships one APK per ABI, with versionCode abi*100000 + build.
NAS_HISTORY = [("0.1.0", [10]), ("0.2.0", [20]), ("0.3.0", [30, 100030, 200030])]


def test_android_abi_splits_of_one_version_count_as_one_release(droid, tmp_path):
    _seed_apks(tmp_path, NAS_HISTORY)
    assert _set(droid, "android", PACKAGE, 1).get_json()["success"] is True

    # The first ABI of 0.4.0 ranks below 0.3.0's arm64 build: not an update yet.
    assert _publish_apk(droid, 40, "0.4.0")["pruned"] == []
    assert _publish_apk(droid, 100040, "0.4.0")["pruned"] == []
    assert _codes(tmp_path) == [10, 20, 30, 40, 100030, 100040, 200030]

    body = _publish_apk(droid, 200040, "0.4.0")
    assert sorted(body["pruned"], key=lambda p: p["versionCode"]) == [
        {"versionCode": 10, "versionName": "0.1.0"}, {"versionCode": 20, "versionName": "0.2.0"}]
    # 0.4.0 (newest) and all three ABIs of 0.3.0 (one before it) stay.
    assert _codes(tmp_path) == [30, 40, 100030, 100040, 200030, 200040]
    assert (tmp_path / "fdroid" / ".update-requested").exists()


def test_android_reads_unindexed_version_names_from_the_apk(droid, tmp_path):
    _seed_apks(tmp_path, NAS_HISTORY, indexed=False)
    _set(droid, "android", PACKAGE, 0)
    body = _publish_apk(droid, 200040, "0.4.0")
    assert {p["versionName"] for p in body["pruned"]} == {"0.1.0", "0.2.0", "0.3.0"}
    assert _codes(tmp_path) == [200040]


def test_android_apk_with_unreadable_version_name_is_never_pruned(droid, tmp_path):
    _seed_apks(tmp_path, [("0.1.0", [10]), ("0.2.0", [20])], indexed=False)
    (_repo(tmp_path) / f"{PACKAGE}_5.apk").write_bytes(b"not an apk")
    _set(droid, "android", PACKAGE, 0)
    _publish_apk(droid, 30, "0.3.0")
    assert _codes(tmp_path) == [5, 30]


def test_android_without_a_policy_keeps_every_apk(droid, tmp_path):
    _seed_apks(tmp_path, NAS_HISTORY)
    assert _publish_apk(droid, 200040, "0.4.0")["pruned"] == []
    assert len(_codes(tmp_path)) == 6


def test_android_apply_now(droid, tmp_path):
    _seed_apks(tmp_path, NAS_HISTORY)
    _set(droid, "android", PACKAGE, 1)
    assert _codes(tmp_path) == [10, 20, 30, 100030, 200030]
    body = droid.post("/api/retention/apply", json={"platform": "android", "id": PACKAGE}).get_json()
    assert body["success"] is True
    assert [p["versionCode"] for p in body["pruned"]] == [10]
    assert body["message"] == "Retention removed 1 older version: 0.1.0"
    assert _codes(tmp_path) == [20, 30, 100030, 200030]


def test_deleting_an_android_app_forgets_its_policy(droid, tmp_path):
    _seed_apks(tmp_path, NAS_HISTORY)
    _set(droid, "android", PACKAGE, 1)
    assert droid.post("/api/android/delete-app", json={"package": PACKAGE}).get_json()["success"]
    assert droid.get("/api/retention").get_json()["retention"]["android"] == {}


# --- routes ------------------------------------------------------------------

def test_policy_round_trips_and_clears(authed_client, tmp_path):
    assert _set(authed_client, "ios", BUNDLE, 3).get_json()["keepPreviousVersions"] == 3
    body = authed_client.get("/api/retention").get_json()
    assert body["retention"] == {"ios": {BUNDLE: 3}, "android": {}}
    assert json.loads((tmp_path / "retention.json").read_text())["ios"] == {BUNDLE: 3}
    for cleared in (None, ""):
        _set(authed_client, "ios", BUNDLE, 3)
        assert _set(authed_client, "ios", BUNDLE, cleared).get_json()["keepPreviousVersions"] is None
        assert authed_client.get("/api/retention").get_json()["retention"]["ios"] == {}


@pytest.mark.parametrize("value", [-1, 101, 1.5, "2", True, [2]])
def test_malformed_keep_previous_is_refused(authed_client, value):
    resp = _set(authed_client, "ios", BUNDLE, value)
    assert resp.status_code == 400
    assert authed_client.get("/api/retention").get_json()["retention"]["ios"] == {}


@pytest.mark.parametrize("payload, status", [
    ({"platform": "windows", "id": BUNDLE}, 400),
    ({"platform": "ios", "id": ""}, 400),
    ({"platform": "ios", "id": "com.missing.app"}, 404),
    ({"platform": "android", "id": "not a package"}, 400),
    ({"platform": "android", "id": "org.missing.app"}, 404),
])
def test_retention_targets_must_be_existing_apps(authed_client, payload, status):
    assert authed_client.post("/api/retention", json={**payload, "keepPreviousVersions": 1}).status_code == status
    assert authed_client.post("/api/retention/apply", json=payload).status_code == status


def test_apply_without_a_policy_is_refused(authed_client, tmp_path):
    _publish_ipa(authed_client, "1.1.0")
    resp = authed_client.post("/api/retention/apply", json={"platform": "ios", "id": BUNDLE})
    assert resp.status_code == 400
    assert _ios_versions(tmp_path) == ["1.1.0", "1.0.0"]


def test_retention_routes_need_an_admin_session_not_a_token(authed_client):
    tok = _token(authed_client)["token"]
    anon = _anon(authed_client)
    for method, path, payload in (("get", "/api/retention", None),
                                  ("post", "/api/retention", {"platform": "ios", "id": BUNDLE,
                                                              "keepPreviousVersions": 0}),
                                  ("post", "/api/retention/apply", {"platform": "ios", "id": BUNDLE})):
        for headers in ({}, _bearer(tok)):
            resp = getattr(anon, method)(path, json=payload, headers=headers)
            assert resp.status_code == 401, (method, path, headers, resp.status_code)


def test_a_corrupt_store_prunes_nothing(authed_client, tmp_path):
    (tmp_path / "retention.json").write_text("{not json")
    assert _publish_ipa(authed_client, "1.1.0")["pruned"] == []
    assert _ios_versions(tmp_path) == ["1.1.0", "1.0.0"]
