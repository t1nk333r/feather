import io
import plistlib
import zipfile
from pathlib import Path

import pytest

from scripts.ipa_inspection import InspectionError, inspect_ipa


def write_ipa(path, plist, extra=None, top_level=True):
    with zipfile.ZipFile(path, "w") as archive:
        if top_level:
            archive.writestr("Payload/App.app/Info.plist", plistlib.dumps(plist))
        for name, value in (extra or {}).items():
            archive.writestr(name, value)


def test_inspects_ios_build_minimum_os_and_privacy(tmp_path):
    path = tmp_path / "app.ipa"
    write_ipa(path, {
        "CFBundleIdentifier": "com.example.app",
        "CFBundleShortVersionString": "2.0",
        "CFBundleVersion": "42",
        "CFBundleDisplayName": "Example",
        "MinimumOSVersion": "16.0",
        "CFBundleSupportedPlatforms": ["iPhoneOS"],
        "UIDeviceFamily": [1, 2],
        "NSCameraUsageDescription": " Camera access ",
        "NSBadUsageDescription": 42,
        "Unrelated": "secret",
    })
    result = inspect_ipa(path, "app.ipa")
    assert result.bundle_identifier == "com.example.app"
    assert result.version == "2.0"
    assert result.build_version == "42"
    assert result.minimum_os_version == "16.0"
    assert result.platform == "ios"
    assert result.device_families == (1, 2)
    assert result.privacy == {"NSCameraUsageDescription": "Camera access"}


def test_detects_tvos_and_unknown(tmp_path):
    tv = tmp_path / "tv.ipa"
    write_ipa(tv, {
        "CFBundleIdentifier": "com.example.tv", "CFBundleVersion": "1",
        "DTPlatformName": "appletvos", "UIDeviceFamily": [3],
    })
    assert inspect_ipa(tv).platform == "tvos"
    unknown = tmp_path / "unknown.ipa"
    write_ipa(unknown, {"CFBundleIdentifier": "com.example.app", "CFBundleVersion": "1"})
    assert inspect_ipa(unknown).platform == "unknown"
    assert inspect_ipa(unknown).minimum_os_version is None


def test_explicit_iphone_platform_wins_over_tv_device_family(tmp_path):
    path = tmp_path / "mixed.ipa"
    write_ipa(path, {
        "CFBundleIdentifier": "com.example.app", "CFBundleVersion": "1",
        "CFBundleSupportedPlatforms": ["iPhoneOS"], "UIDeviceFamily": [3],
    })
    assert inspect_ipa(path).platform == "ios"


def test_tv_device_family_corrobates_tvos_without_platform_key(tmp_path):
    path = tmp_path / "family-only.ipa"
    write_ipa(path, {
        "CFBundleIdentifier": "com.example.tv", "CFBundleVersion": "1",
        "UIDeviceFamily": [3],
    })
    assert inspect_ipa(path).platform == "tvos"


def test_nested_plist_is_ignored_and_multiple_top_level_rejected(tmp_path):
    path = tmp_path / "nested.ipa"
    write_ipa(path, {
        "CFBundleIdentifier": "com.example.app", "CFBundleVersion": "1",
    }, {"Payload/App.app/Frameworks/X.framework/Info.plist": b"broken"})
    assert inspect_ipa(path).bundle_identifier == "com.example.app"
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("Payload/Other.app/Info.plist", plistlib.dumps({}))
    with pytest.raises(InspectionError, match="exactly one"):
        inspect_ipa(path)


@pytest.mark.parametrize("kind", ["invalid", "missing_payload", "malformed"])
def test_rejects_invalid_archives(tmp_path, kind):
    path = tmp_path / "bad.ipa"
    if kind == "invalid":
        path.write_bytes(b"not zip")
    elif kind == "missing_payload":
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Info.plist", b"x")
    else:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Payload/App.app/Info.plist", b"bad plist")
    with pytest.raises(InspectionError):
        inspect_ipa(path, "bad.ipa")


def test_published_images_include_shared_inspector():
    root = Path(__file__).resolve().parents[1]
    assert "COPY scripts/ipa_inspection.py ./scripts/ipa_inspection.py" in (root / "Dockerfile").read_text()
    assert "COPY scripts/ipa_inspection.py ." in (root / "Dockerfile.bot").read_text()
    assert "!scripts/ipa_inspection.py" in (root / ".dockerignore").read_text()


# --- entitlements ----------------------------------------------------------------------------

import struct

GROUPS = "com.apple.security.application-groups"


def signed_macho(entitlements, bits=64, signature=None):
    """A minimal thin Mach-O whose LC_CODE_SIGNATURE points at an embedded
    SuperBlob: a code-directory stand-in, then the XML entitlements blob
    (slot 5), as codesign and rcodesign lay it out. `entitlements=None`
    leaves the binary unsigned; `signature` replaces the SuperBlob bytes."""
    magic, header_size = (b"\xcf\xfa\xed\xfe", 32) if bits == 64 else (b"\xce\xfa\xed\xfe", 28)
    commands = struct.pack("<II", 0x2, 24) + b"\0" * 16                  # an LC_SYMTAB first
    code = b"\x1f\x20\x03\xd5" * 16
    if entitlements is not None or signature is not None:
        if signature is None:
            directory = struct.pack(">II", 0xFADE0C02, 16) + b"\0" * 8
            xml = plistlib.dumps(entitlements)
            blob = struct.pack(">II", 0xFADE7171, 8 + len(xml)) + xml
            index_size = 12 + 8 * 2
            signature = (struct.pack(">III", 0xFADE0CC0, index_size + len(directory) + len(blob), 2)
                         + struct.pack(">IIII", 0, index_size, 5, index_size + len(directory))
                         + directory + blob)
        dataoff = header_size + len(commands) + 16 + len(code)
        commands += struct.pack("<IIII", 0x1D, 16, dataoff, len(signature))
    else:
        signature = b""
    ncmds = 2 if signature else 1
    header = magic + struct.pack("<IIIIII", 0x0100000C, 0, 2, ncmds, len(commands), 0)
    if bits == 64:
        header += b"\0" * 4
    return header + commands + code + signature


def fat_macho(*slices):
    """A universal binary holding `slices`, each 16-byte aligned."""
    offset = (8 + 20 * len(slices) + 15) & ~15
    table, body = b"", b""
    for thin in slices:
        table += struct.pack(">IIIII", 0x0100000C, 0, offset + len(body), len(thin), 4)
        body += thin + b"\0" * (-len(thin) % 16)
    head = struct.pack(">II", 0xCAFEBABE, len(slices)) + table
    return head + b"\0" * (offset - len(head)) + body


def bundle(executable="App", **extra):
    return {"CFBundleIdentifier": "com.example.app", "CFBundleShortVersionString": "1.0",
            "CFBundleExecutable": executable, **extra}


def test_reads_entitlement_keys_of_app_and_extensions(tmp_path):
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(NSCameraUsageDescription="App text"), extra={
        "Payload/App.app/App": signed_macho({GROUPS: ["group.example"], "application-identifier": "T.x"}),
        "Payload/App.app/PlugIns/Share.appex/Info.plist": plistlib.dumps(
            {"CFBundleExecutable": "Share", "NSCameraUsageDescription": "Share text",
             "NSPhotoLibraryUsageDescription": "Photos"}),
        "Payload/App.app/PlugIns/Share.appex/Share": signed_macho({"keychain-access-groups": ["T.shared"]}),
        "Payload/App.app/Extensions/Widget.appex/Info.plist": plistlib.dumps({"CFBundleExecutable": "Widget"}),
        "Payload/App.app/Extensions/Widget.appex/Widget": signed_macho({"com.apple.developer.siri": True}, bits=32),
    })
    result = inspect_ipa(path, "app.ipa")
    assert result.entitlements == ("application-identifier", "com.apple.developer.siri", GROUPS,
                                   "keychain-access-groups")
    # Entitlement values are never kept, only keys; the app's privacy text wins.
    assert result.privacy == {"NSCameraUsageDescription": "App text", "NSPhotoLibraryUsageDescription": "Photos"}


def test_reads_every_slice_of_a_universal_binary(tmp_path):
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={"Payload/App.app/App": fat_macho(
        signed_macho({GROUPS: ["g"]}), signed_macho({"get-task-allow": False}, bits=32))})
    assert inspect_ipa(path).entitlements == (GROUPS, "get-task-allow")


@pytest.mark.parametrize("executable", [
    signed_macho(None),                                                  # unsigned
    signed_macho(None, signature=b"\xfa\xde\x0c\xc0" + b"\0" * 20),     # signed, no entitlements slot
    signed_macho(None, signature=b"\0" * 40),                            # not a SuperBlob
    signed_macho({GROUPS: ["g"]})[:-30],                                # signature cut short
    signed_macho({GROUPS: ["g"]}).replace(b"\xfa\xde\x71\x71", b"\xfa\xde\x71\x72"),  # wrong blob magic
    b"\xca\xfe\xba\xbe\0\0\0\x09" + b"\0" * 200,                         # implausible slice count
    b"#!/bin/sh\n",                                                      # not a Mach-O
    b"",
])
def test_unreadable_signature_contributes_nothing_and_never_fails(tmp_path, executable):
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={"Payload/App.app/App": executable})
    result = inspect_ipa(path)
    assert result.bundle_identifier == "com.example.app" and result.entitlements == ()


def test_oversized_entitlements_blob_is_skipped(tmp_path, monkeypatch):
    import scripts.ipa_inspection as module
    monkeypatch.setattr(module, "MAX_ENTITLEMENTS_BYTES", 64)
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={"Payload/App.app/App": signed_macho({GROUPS: ["g" * 100]})})
    assert inspect_ipa(path).entitlements == ()


def test_executable_must_be_the_named_member_of_its_bundle(tmp_path):
    path = tmp_path / "app.ipa"
    signed = signed_macho({GROUPS: ["g"]})
    write_ipa(path, bundle(executable="../Other/App"), extra={"Payload/Other/App": signed})
    assert inspect_ipa(path).entitlements == ()
    write_ipa(path, bundle(executable="Missing"), extra={"Payload/App.app/App": signed})
    assert inspect_ipa(path).entitlements == ()
