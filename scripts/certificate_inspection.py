"""Inspection of an Apple code-signing pair: a PKCS#12 identity + a provisioning profile.

Validate-and-display only (see plans/086, section 8e): this module answers
"is this certificate pair usable, and on which devices" and signs nothing. It
never writes, never keeps the passphrase, and never touches the network.

Two halves, with deliberately different dependency weights:

* The profile is a property list wrapped in a CMS SignedData whose payload is
  carried in the clear (RFC 5652 section 5.2), so the plist is extracted with the
  standard library alone -- no key, no signature verification.
* The p12 is RFC 7292, and its interesting half is encrypted, so it needs
  `cryptography`. That is the one dependency this feature adds.

The pair check is the headline: a leaf certificate that does not appear in the
profile's `DeveloperCertificates` cannot legitimately sign against that profile
(TN3125, "The who").
"""

import hashlib
import plistlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# A p12 is a few kilobytes and a profile a few tens of kilobytes; anything far
# past that is not one of the two things we were handed.
MAX_P12_BYTES = 512 * 1024
MAX_PROFILE_BYTES = 1024 * 1024

# Device binding, kept as three states rather than a boolean, because
# "no device list" and "all devices" mean opposite things (TN3125, "The where").
BINDING_UDID = "udid-bound"
BINDING_ALL = "all-devices"
BINDING_APP_STORE = "app-store"

_PLIST_START = b"<?xml"
_PLIST_END = b"</plist>"


class CertificateInspectionError(RuntimeError):
    """A p12 or profile that cannot be inspected, with a machine-readable reason.

    `reason` is one of NOT_A_PKCS12, WRONG_PASSPHRASE, NO_PRIVATE_KEY,
    NOT_A_PROFILE, TOO_LARGE. A wrong passphrase is deliberately distinct from
    an unparseable file: reporting a typo as a corrupt certificate is the first
    thing an operator would hit otherwise.
    """

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class CertificateInfo:
    subject: str
    issuer: str
    common_name: str = None
    team_id: str = None
    not_before: str = None
    not_after: str = None
    expired: bool = False
    code_signing_eku: bool = False
    chain_length: int = 1


@dataclass(frozen=True)
class ProfileInfo:
    name: str = None
    app_id_name: str = None
    team_name: str = None
    team_identifier: tuple = ()
    application_identifier: str = None
    uuid: str = None
    creation_date: str = None
    expiration_date: str = None
    expired: bool = False
    device_binding: str = BINDING_APP_STORE
    provisioned_device_count: int = 0
    entitlement_keys: tuple = ()
    is_xcode_managed: bool = False
    platforms: tuple = ()


@dataclass(frozen=True)
class PairInspection:
    certificate: CertificateInfo
    profile: ProfileInfo
    match: bool
    method: str = None
    certificate_count: int = 0
    warnings: tuple = field(default=())


def _iso(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _text(value):
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _name_attribute(name, oid):
    values = name.get_attributes_for_oid(oid)
    if not values:
        return None
    return _text(str(values[0].value))


def _extract_profile_plist(data):
    """Pull the plist payload out of a `.mobileprovision`.

    The CMS wrapper carries the plist as cleartext `eContent`, so this is a
    slice, not a decryption. Bounded by the closing tag rather than taking the
    tail of the file, so a profile with trailing CMS structure still parses.
    """
    start = data.find(_PLIST_START)
    if start == -1:
        raise CertificateInspectionError(
            "NOT_A_PROFILE", "That file does not contain a provisioning profile."
        )
    end = data.rfind(_PLIST_END, start)
    if end == -1:
        raise CertificateInspectionError(
            "NOT_A_PROFILE", "That provisioning profile is truncated or malformed."
        )
    payload = data[start:end + len(_PLIST_END)]
    try:
        plist = plistlib.loads(payload)
    except Exception:
        raise CertificateInspectionError(
            "NOT_A_PROFILE", "That provisioning profile could not be read."
        )
    if not isinstance(plist, dict):
        raise CertificateInspectionError(
            "NOT_A_PROFILE", "That provisioning profile is not a property list."
        )
    return plist


def inspect_profile(data, now=None):
    """Inspect a `.mobileprovision`. Returns (ProfileInfo, developer_certificate_ders, der_profile)."""
    if len(data) > MAX_PROFILE_BYTES:
        raise CertificateInspectionError(
            "TOO_LARGE", "That provisioning profile is too large to be one."
        )
    plist = _extract_profile_plist(data)
    now = now or datetime.now(timezone.utc)

    devices = plist.get("ProvisionedDevices")
    device_count = len(devices) if isinstance(devices, list) else 0
    if device_count:
        binding = BINDING_UDID
    elif plist.get("ProvisionsAllDevices") is True:
        binding = BINDING_ALL
    else:
        binding = BINDING_APP_STORE

    entitlements = plist.get("Entitlements")
    entitlements = entitlements if isinstance(entitlements, dict) else {}
    teams = plist.get("TeamIdentifier")
    teams = tuple(t for t in teams if isinstance(t, str)) if isinstance(teams, list) else ()
    platforms = plist.get("Platform")
    platforms = tuple(p for p in platforms if isinstance(p, str)) if isinstance(platforms, list) else ()
    expiration = plist.get("ExpirationDate")

    info = ProfileInfo(
        name=_text(plist.get("Name")),
        app_id_name=_text(plist.get("AppIDName")),
        team_name=_text(plist.get("TeamName")),
        team_identifier=teams,
        application_identifier=_text(entitlements.get("application-identifier")),
        uuid=_text(plist.get("UUID")),
        creation_date=_iso(plist.get("CreationDate")),
        expiration_date=_iso(expiration),
        expired=isinstance(expiration, datetime) and _as_utc(expiration) < now,
        device_binding=binding,
        provisioned_device_count=device_count,
        entitlement_keys=tuple(sorted(k for k in entitlements if isinstance(k, str))),
        is_xcode_managed=plist.get("IsXcodeManaged") is True,
        platforms=platforms,
    )

    certificates = plist.get("DeveloperCertificates")
    ders = tuple(c for c in certificates if isinstance(c, bytes)) if isinstance(certificates, list) else ()
    der_profile = plist.get("DER-Encoded-Profile")
    der_profile = der_profile if isinstance(der_profile, bytes) else None
    return info, ders, der_profile


def _as_utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _load_identity(data, passphrase):
    """Open a p12, telling a wrong passphrase apart from an unusable file.

    Both failures are `ValueError` from pyca, distinguishable only by message:
    a DER-level failure says "Could not deserialize PKCS12 data", anything
    after that (wrong passphrase included) says "Invalid password or PKCS12 data".
    """
    attempts = [passphrase.encode() if passphrase else None]
    if not passphrase:
        # Some exports use an empty-string passphrase rather than none at all.
        attempts.append(b"")
    last = None
    for candidate in attempts:
        try:
            return pkcs12.load_key_and_certificates(data, candidate)
        except ValueError as e:
            last = e
        except Exception as e:
            last = e
    message = str(last or "")
    if "deserialize" in message.lower():
        raise CertificateInspectionError(
            "NOT_A_PKCS12", "That file is not a PKCS#12 (.p12) certificate."
        )
    raise CertificateInspectionError(
        "WRONG_PASSPHRASE", "Wrong passphrase for that certificate."
    )


def inspect_p12(data, passphrase=None, now=None):
    """Inspect a `.p12` identity. Returns (CertificateInfo, leaf_der, chain_length)."""
    if len(data) > MAX_P12_BYTES:
        raise CertificateInspectionError(
            "TOO_LARGE", "That certificate file is too large to be a .p12."
        )
    key, certificate, additional = _load_identity(data, passphrase)
    if key is None or certificate is None:
        raise CertificateInspectionError(
            "NO_PRIVATE_KEY",
            "That file holds no private key with a matching certificate.",
        )
    now = now or datetime.now(timezone.utc)

    try:
        eku = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        code_signing = ExtendedKeyUsageOID.CODE_SIGNING in eku
    except x509.ExtensionNotFound:
        code_signing = False

    not_after = certificate.not_valid_after_utc
    info = CertificateInfo(
        subject=certificate.subject.rfc4514_string(),
        issuer=certificate.issuer.rfc4514_string(),
        common_name=_name_attribute(certificate.subject, NameOID.COMMON_NAME),
        team_id=_name_attribute(certificate.subject, NameOID.ORGANIZATIONAL_UNIT_NAME),
        not_before=_iso(certificate.not_valid_before_utc),
        not_after=_iso(not_after),
        expired=_as_utc(not_after) < now,
        code_signing_eku=code_signing,
        chain_length=1 + len(additional or []),
    )
    return info, certificate.public_bytes(Encoding.DER), info.chain_length


def _pair_match(leaf_der, developer_certificates, der_profile):
    """Is this p12 one of the profile's authorised signers?

    The plist form carries each authorised certificate's DER verbatim; the
    DER-encoded profile carries a SHA-256 checksum of it instead.
    """
    if leaf_der in developer_certificates:
        return True, "der"
    if der_profile and hashlib.sha256(leaf_der).digest() in der_profile:
        return True, "sha256"
    return False, None


def _warnings(certificate, profile, match):
    out = []
    if not match:
        out.append(
            "This certificate is not listed in the profile's authorised signers, "
            "so the pair cannot legitimately sign."
        )
    if certificate.expired:
        out.append("The certificate itself has expired.")
    if profile.expired:
        out.append("The provisioning profile has expired.")
    if not certificate.code_signing_eku:
        out.append(
            "The certificate does not carry the Code Signing extended key usage."
        )
    if profile.device_binding == BINDING_UDID:
        out.append(
            f"Installs only on the {profile.provisioned_device_count} device(s) "
            "listed in this profile; any other device will refuse it."
        )
    elif profile.device_binding == BINDING_APP_STORE:
        out.append(
            "This is an App Store distribution profile: it lists no devices and "
            "cannot be installed directly."
        )
    if (
        profile.team_identifier
        and certificate.team_id
        and certificate.team_id not in profile.team_identifier
    ):
        out.append(
            "The certificate's team does not match the profile's team identifier."
        )
    return tuple(out)


def inspect_pair(p12_data, profile_data, passphrase=None, now=None):
    """Inspect a p12 + profile pair. Nothing is stored, nothing is signed."""
    now = now or datetime.now(timezone.utc)
    profile, developer_certificates, der_profile = inspect_profile(profile_data, now=now)
    certificate, leaf_der, _ = inspect_p12(p12_data, passphrase, now=now)
    match, method = _pair_match(leaf_der, developer_certificates, der_profile)
    return PairInspection(
        certificate=certificate,
        profile=profile,
        match=match,
        method=method,
        certificate_count=len(developer_certificates),
        warnings=_warnings(certificate, profile, match),
    )


def as_payload(inspection):
    """The JSON shape the admin UI consumes."""
    certificate = inspection.certificate
    profile = inspection.profile
    return {
        "ok": True,
        "certificate": {
            "subject": certificate.subject,
            "issuer": certificate.issuer,
            "commonName": certificate.common_name,
            "teamId": certificate.team_id,
            "notBefore": certificate.not_before,
            "notAfter": certificate.not_after,
            "expired": certificate.expired,
            "codeSigningEku": certificate.code_signing_eku,
            "chainLength": certificate.chain_length,
        },
        "profile": {
            "name": profile.name,
            "appIdName": profile.app_id_name,
            "teamName": profile.team_name,
            "teamIdentifier": list(profile.team_identifier),
            "applicationIdentifier": profile.application_identifier,
            "uuid": profile.uuid,
            "creationDate": profile.creation_date,
            "expirationDate": profile.expiration_date,
            "expired": profile.expired,
            "deviceBinding": profile.device_binding,
            "provisionedDeviceCount": profile.provisioned_device_count,
            "entitlementKeys": list(profile.entitlement_keys),
            "isXcodeManaged": profile.is_xcode_managed,
            "platforms": list(profile.platforms),
        },
        "pair": {
            "match": inspection.match,
            "method": inspection.method,
            "certificateCount": inspection.certificate_count,
        },
        "warnings": list(inspection.warnings),
    }
