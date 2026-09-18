"""Tests for scripts/certificate_inspection.py and /api/certificate/inspect.

Unlike an IPA or a binary AndroidManifest, both halves of a certificate pair
can be authored honestly in-process: `cryptography` mints a real self-signed
leaf and a real PKCS#12 container, and a `.mobileprovision` is a plist behind
a CMS wrapper whose payload is cleartext, so a byte prefix plus a plist is
structurally what the parser sees in a real profile.

What is deliberately exercised: the two failures an operator actually hits
(wrong passphrase vs not-a-p12, which pyca reports as the same exception type),
the pair match that decides whether the pair can sign at all, and the three
device-binding states -- which are the three different answers to "will this
install on my phone".
"""

import io
import json
import os
import plistlib
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from scripts.certificate_inspection import (
    BINDING_ALL,
    BINDING_APP_STORE,
    BINDING_UDID,
    CertificateInspectionError,
    inspect_pair,
)

PASSPHRASE = "correct-horse"
TEST_ADMIN_PASSWORD = "test-admin-password"
TEST_SECRET_KEY = "test-secret-key"

# CMS wrapping is irrelevant to the parser -- the profile plist travels in the
# clear as SignedData eContent -- so a DER-ish prefix and suffix stand in for it.
_CMS_PREFIX = b"\x30\x82\x0a\x0b\x06\x09*\x86H\x86\xf7\x0d\x01\x07\x02"
_CMS_SUFFIX = b"\x31\x82\x01\x00signerinfo-bytes"


def _leaf(common_name="Apple Development: Someone", team="ABCDE12345",
          not_after_days=365, code_signing=True):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, team),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test Team"),
    ])
    issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Apple Worldwide Developer Relations CA"),
    ])
    now = datetime.now(timezone.utc)
    not_after = now + timedelta(days=not_after_days)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # An already-expired fixture still needs notBefore < notAfter.
        .not_valid_before(min(now, not_after) - timedelta(days=1))
        .not_valid_after(not_after)
    )
    if code_signing:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False
        )
    return key, builder.sign(key, hashes.SHA256())


def _p12(key, certificate, passphrase=PASSPHRASE):
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    return pkcs12.serialize_key_and_certificates(
        name=b"identity", key=key, cert=certificate, cas=None, encryption_algorithm=encryption
    )


def _profile(certificates=(), devices=("00008030-001234567890ABCD",),
             provisions_all=False, expires_in_days=300, entitlements=None):
    payload = {
        "Name": "MangaSync Development",
        "AppIDName": "MangaSync",
        "TeamName": "Test Team",
        "TeamIdentifier": ["ABCDE12345"],
        "UUID": "1234abcd-0000-0000-0000-00000000cafe",
        "CreationDate": datetime.now(timezone.utc) - timedelta(days=1),
        "ExpirationDate": datetime.now(timezone.utc) + timedelta(days=expires_in_days),
        "Platform": ["iOS"],
        "IsXcodeManaged": False,
        "Entitlements": entitlements or {
            "application-identifier": "ABCDE12345.dev.mangasync.app",
            "com.apple.developer.team-identifier": "ABCDE12345",
            "get-task-allow": True,
        },
        "DeveloperCertificates": [
            c.public_bytes(serialization.Encoding.DER) for c in certificates
        ],
    }
    if devices:
        payload["ProvisionedDevices"] = list(devices)
    if provisions_all:
        payload["ProvisionsAllDevices"] = True
    return _CMS_PREFIX + plistlib.dumps(payload) + _CMS_SUFFIX


@pytest.fixture
def pair():
    key, certificate = _leaf()
    return _p12(key, certificate), _profile(certificates=[certificate]), certificate


# --------------------------------------------------------------------- module


def test_a_matching_pair_is_reported_as_a_match_with_both_expiries(pair):
    p12_bytes, profile_bytes, _ = pair

    result = inspect_pair(p12_bytes, profile_bytes, PASSPHRASE)

    assert result.match is True
    assert result.method == "der"
    assert result.certificate_count == 1
    assert result.certificate.common_name == "Apple Development: Someone"
    assert result.certificate.team_id == "ABCDE12345"
    assert result.certificate.code_signing_eku is True
    # Two independent expiries, both read, neither derived from the other.
    assert result.certificate.expired is False
    assert result.profile.expired is False
    assert result.certificate.not_after and result.profile.expiration_date
    assert result.profile.application_identifier == "ABCDE12345.dev.mangasync.app"
    assert "get-task-allow" in result.profile.entitlement_keys


def test_a_certificate_absent_from_the_profile_is_not_a_match(pair):
    p12_bytes, profile_bytes, _ = pair
    other_key, other_certificate = _leaf(common_name="Apple Development: Someone Else")
    stranger = _p12(other_key, other_certificate)

    result = inspect_pair(stranger, profile_bytes, PASSPHRASE)

    assert result.match is False
    assert result.method is None
    assert any("authorised signers" in w for w in result.warnings)


def test_the_wrong_passphrase_is_not_reported_as_a_broken_file(pair):
    p12_bytes, profile_bytes, _ = pair

    with pytest.raises(CertificateInspectionError) as excinfo:
        inspect_pair(p12_bytes, profile_bytes, "not-the-passphrase")

    assert excinfo.value.reason == "WRONG_PASSPHRASE"


def test_a_file_that_is_not_a_pkcs12_is_refused_before_the_passphrase_is_blamed(pair):
    _, profile_bytes, _ = pair

    with pytest.raises(CertificateInspectionError) as excinfo:
        inspect_pair(b"this is not a certificate", profile_bytes, PASSPHRASE)

    assert excinfo.value.reason == "NOT_A_PKCS12"


def test_a_file_that_is_not_a_profile_is_refused(pair):
    p12_bytes, _, _ = pair

    with pytest.raises(CertificateInspectionError) as excinfo:
        inspect_pair(p12_bytes, b"\x30\x82 not a profile", PASSPHRASE)

    assert excinfo.value.reason == "NOT_A_PROFILE"


def test_a_passphraseless_p12_opens_with_an_empty_passphrase():
    key, certificate = _leaf()
    naked = _p12(key, certificate, passphrase=None)

    result = inspect_pair(naked, _profile(certificates=[certificate]), "")

    assert result.match is True


@pytest.mark.parametrize(
    "devices,provisions_all,expected_binding,expected_count",
    [
        (("00008030-001234567890ABCD", "00008030-00FEDCBA09876543"), False, BINDING_UDID, 2),
        ((), True, BINDING_ALL, 0),
        ((), False, BINDING_APP_STORE, 0),
    ],
)
def test_the_three_device_binding_states_are_distinguished(
    devices, provisions_all, expected_binding, expected_count
):
    key, certificate = _leaf()
    profile_bytes = _profile(
        certificates=[certificate], devices=devices, provisions_all=provisions_all
    )

    result = inspect_pair(_p12(key, certificate), profile_bytes, PASSPHRASE)

    assert result.profile.device_binding == expected_binding
    assert result.profile.provisioned_device_count == expected_count


def test_an_expired_certificate_and_an_expired_profile_are_reported_separately():
    key, certificate = _leaf(not_after_days=-1)
    profile_bytes = _profile(certificates=[certificate], expires_in_days=-2)

    result = inspect_pair(_p12(key, certificate), profile_bytes, PASSPHRASE)

    assert result.certificate.expired is True
    assert result.profile.expired is True
    assert any("certificate itself has expired" in w for w in result.warnings)
    assert any("profile has expired" in w for w in result.warnings)


def test_a_certificate_without_code_signing_usage_is_flagged():
    key, certificate = _leaf(code_signing=False)

    result = inspect_pair(
        _p12(key, certificate), _profile(certificates=[certificate]), PASSPHRASE
    )

    assert result.certificate.code_signing_eku is False
    assert any("Code Signing" in w for w in result.warnings)


def test_a_p12_larger_than_the_cap_is_refused(pair):
    _, profile_bytes, _ = pair

    with pytest.raises(CertificateInspectionError) as excinfo:
        inspect_pair(b"\x00" * (512 * 1024 + 1), profile_bytes, PASSPHRASE)

    assert excinfo.value.reason == "TOO_LARGE"


# ---------------------------------------------------------------------- route


@pytest.fixture
def authed_client(tmp_path):
    import importlib

    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["ADMIN_PASSWORD"] = TEST_ADMIN_PASSWORD
    os.environ["SECRET_KEY"] = TEST_SECRET_KEY

    import app as app_module

    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        assert c.post("/api/login", json={"password": TEST_ADMIN_PASSWORD}).status_code == 200
        yield c


def _upload(p12_bytes, profile_bytes, passphrase=PASSPHRASE):
    return {
        "p12File": (io.BytesIO(p12_bytes), "identity.p12"),
        "provisionFile": (io.BytesIO(profile_bytes), "profile.mobileprovision"),
        "p12Password": passphrase,
    }


def test_the_route_returns_the_pair_verdict_and_signs_nothing(authed_client, tmp_path, pair):
    p12_bytes, profile_bytes, _ = pair
    before = sorted(p.name for p in tmp_path.rglob("*"))

    response = authed_client.post(
        "/api/certificate/inspect",
        data=_upload(p12_bytes, profile_bytes),
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["ok"] is True
    assert body["pair"] == {"match": True, "method": "der", "certificateCount": 1}
    assert body["profile"]["deviceBinding"] == BINDING_UDID
    assert body["certificate"]["teamId"] == "ABCDE12345"
    # Validate-and-display only: no artifact of the upload is left behind.
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_the_route_reports_a_wrong_passphrase_as_such(authed_client, pair):
    p12_bytes, profile_bytes, _ = pair

    response = authed_client.post(
        "/api/certificate/inspect",
        data=_upload(p12_bytes, profile_bytes, passphrase="wrong"),
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    body = json.loads(response.data)
    assert body == {
        "ok": False,
        "reason": "WRONG_PASSPHRASE",
        "error": "Wrong passphrase for that certificate.",
    }


def test_the_route_requires_both_files(authed_client, pair):
    p12_bytes, _, _ = pair

    response = authed_client.post(
        "/api/certificate/inspect",
        data={"p12File": (io.BytesIO(p12_bytes), "identity.p12")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert json.loads(response.data)["reason"] == "MISSING_FILE"


def test_the_route_is_admin_only(tmp_path, pair):
    import importlib

    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["ADMIN_PASSWORD"] = TEST_ADMIN_PASSWORD
    os.environ["SECRET_KEY"] = TEST_SECRET_KEY
    import app as app_module

    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    p12_bytes, profile_bytes, _ = pair

    with app_module.app.test_client() as anonymous:
        response = anonymous.post(
            "/api/certificate/inspect",
            data=_upload(p12_bytes, profile_bytes),
            content_type="multipart/form-data",
        )

    assert response.status_code == 401
