import io
import plistlib
import struct
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

GROUPS = "com.apple.security.application-groups"


def signed_macho(entitlements, bits=64, signature=None, ncmds=None, datasize_extra=0, length_delta=0):
    """A minimal thin Mach-O whose LC_CODE_SIGNATURE points at an embedded
    SuperBlob: a code-directory stand-in, then the XML entitlements blob
    (slot 5), as codesign and rcodesign lay it out. `entitlements=None`
    leaves the binary unsigned; `signature` replaces the SuperBlob bytes.
    The other arguments falsify one header field: the load-command count,
    the signature size in LC_CODE_SIGNATURE, the SuperBlob's length."""
    magic, header_size = (b"\xcf\xfa\xed\xfe", 32) if bits == 64 else (b"\xce\xfa\xed\xfe", 28)
    commands = struct.pack("<II", 0x2, 24) + b"\0" * 16                  # an LC_SYMTAB first
    code = b"\x1f\x20\x03\xd5" * 16
    if entitlements is not None or signature is not None:
        if signature is None:
            directory = struct.pack(">II", 0xFADE0C02, 16) + b"\0" * 8
            xml = plistlib.dumps(entitlements)
            blob = struct.pack(">II", 0xFADE7171, 8 + len(xml)) + xml
            index_size = 12 + 8 * 2
            signature = (struct.pack(">III", 0xFADE0CC0, index_size + len(directory) + len(blob) + length_delta, 2)
                         + struct.pack(">IIII", 0, index_size, 5, index_size + len(directory))
                         + directory + blob)
        dataoff = header_size + len(commands) + 16 + len(code)
        commands += struct.pack("<IIII", 0x1D, 16, dataoff, len(signature) + datasize_extra)
    else:
        signature = b""
    ncmds = ncmds or (2 if signature else 1)
    header = magic + struct.pack("<IIIIII", 0x0100000C, 0, 2, ncmds, len(commands), 0)
    if bits == 64:
        header += b"\0" * 4
    return header + commands + code + signature


def fat_macho(*slices, fat64=False, sizes=None):
    """A universal binary holding `slices`, each 16-byte aligned, with a
    fat_arch_64 table if `fat64`. `sizes` overrides the declared slice sizes."""
    entry_size, fields, extra = (32, ">IIQQII", (0,)) if fat64 else (20, ">IIIII", ())
    offset = (8 + entry_size * len(slices) + 15) & ~15
    table, body = b"", b""
    for i, thin in enumerate(slices):
        size = sizes[i] if sizes else len(thin)
        table += struct.pack(fields, 0x0100000C, 0, offset + len(body), size, 4, *extra)
        body += thin + b"\0" * (-len(thin) % 16)
    head = struct.pack(">II", 0xCAFEBABF if fat64 else 0xCAFEBABE, len(slices)) + table
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


def test_reads_a_fat64_universal_binary(tmp_path):
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={"Payload/App.app/App": fat_macho(
        signed_macho({GROUPS: ["g"]}), signed_macho({"get-task-allow": False}), fat64=True)})
    assert inspect_ipa(path).entitlements == (GROUPS, "get-task-allow")


@pytest.mark.parametrize("executable", [
    signed_macho({GROUPS: ["g"]}, ncmds=6),             # more commands than 40 bytes can hold
    signed_macho({GROUPS: ["g"]}, datasize_extra=100),  # signature runs past the slice
    signed_macho({GROUPS: ["g"]}, length_delta=-1),     # entitlements blob runs past the SuperBlob
    fat_macho(signed_macho({GROUPS: ["g"]}), sizes=[10_000]),  # slice runs past the file
])
def test_header_bounds_are_enforced(tmp_path, executable):
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={"Payload/App.app/App": executable})
    assert inspect_ipa(path).entitlements == ()


def test_overlapping_fat_slices_are_skipped(tmp_path):
    # Reading an overlapping slice would seek back, re-inflating the member.
    first, second = signed_macho({GROUPS: ["g"]}), signed_macho({"keychain-access-groups": ["k"]})
    padded = len(first) + (-len(first) % 16)
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={"Payload/App.app/App": fat_macho(
        first, second, sizes=[padded + len(second), len(second)])})
    assert inspect_ipa(path).entitlements == (GROUPS,)


def test_signature_must_follow_the_load_commands(tmp_path, monkeypatch):
    import scripts.ipa_inspection as module
    # With the forward-only seek disabled, only the bounds check refuses a
    # signature placed back inside the load commands.
    monkeypatch.setattr(module, "_seek_forward", lambda handle, offset: handle.seek(offset))
    thin = signed_macho({GROUPS: ["g"]})
    signature = thin[thin.index(b"\xfa\xde\x0c\xc0"):]
    commands = (struct.pack("<II", 0x99, 8 + len(signature)) + signature
                + struct.pack("<IIII", 0x1D, 16, 32 + 8, len(signature)))
    header = b"\xcf\xfa\xed\xfe" + struct.pack("<IIIIII", 0x0100000C, 0, 2, 2, len(commands), 0) + b"\0" * 4
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={"Payload/App.app/App": header + commands + b"\0" * 64})
    assert inspect_ipa(path).entitlements == ()


def test_seeks_only_forward():
    from scripts.ipa_inspection import _seek_forward
    handle = io.BytesIO(b"0123456789")
    _seek_forward(handle, 4)
    handle.read(2)
    with pytest.raises(ValueError):
        _seek_forward(handle, 5)


@pytest.mark.parametrize("cap, value", [
    ("MAX_EXECUTABLE_BYTES", 100),
    ("MAX_INFLATED_EXECUTABLE_BYTES", 100),
    ("MAX_LOAD_COMMANDS_BYTES", 32),
    ("MAX_FAT_SLICES", 1),
    ("MAX_SIGNATURE_SLOTS", 1),
])
def test_executable_caps(tmp_path, monkeypatch, cap, value):
    import scripts.ipa_inspection as module
    executable = fat_macho(signed_macho({GROUPS: ["g"]}), signed_macho({GROUPS: ["g"]}, bits=32))
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={"Payload/App.app/App": executable})
    assert inspect_ipa(path).entitlements == (GROUPS,)
    monkeypatch.setattr(module, cap, value)
    assert inspect_ipa(path).entitlements == ()


def _with_extensions(path, count):
    extra = {"Payload/App.app/App": signed_macho({GROUPS: ["g"]})}
    for i in range(count):
        extra[f"Payload/App.app/PlugIns/E{i}.appex/Info.plist"] = plistlib.dumps(
            {"CFBundleExecutable": "E", f"NSE{i}UsageDescription": "text"})
        extra[f"Payload/App.app/PlugIns/E{i}.appex/E"] = signed_macho({f"e{i}": True})
    write_ipa(path, bundle(), extra=extra)


def test_executables_share_one_budget_app_first(tmp_path, monkeypatch):
    import scripts.ipa_inspection as module
    path = tmp_path / "app.ipa"
    _with_extensions(path, 2)
    each = len(signed_macho({GROUPS: ["g"]}))
    monkeypatch.setattr(module, "MAX_INFLATED_EXECUTABLE_BYTES", 2 * each + 10)
    result = inspect_ipa(path)
    assert result.entitlements == (GROUPS, "e0")
    assert set(result.privacy) == {"NSE0UsageDescription", "NSE1UsageDescription"}


def test_extension_caps(tmp_path, monkeypatch):
    import scripts.ipa_inspection as module
    path = tmp_path / "app.ipa"
    _with_extensions(path, 3)
    assert inspect_ipa(path).entitlements == (GROUPS, "e0", "e1", "e2")
    monkeypatch.setattr(module, "MAX_EXTENSIONS", 2)
    assert inspect_ipa(path).entitlements == (GROUPS, "e0", "e1")
    monkeypatch.setattr(module, "MAX_EXTENSION_PLIST_BYTES", 10)
    result = inspect_ipa(path)
    assert result.entitlements == (GROUPS,) and result.privacy == {}


def test_kept_keys_and_text_are_capped(tmp_path, monkeypatch):
    import scripts.ipa_inspection as module
    for cap, value in (("MAX_KEY_LENGTH", 40), ("MAX_ENTITLEMENT_KEYS", 2),
                       ("MAX_PRIVACY_KEYS", 1), ("MAX_PRIVACY_TEXT", 5)):
        monkeypatch.setattr(module, cap, value)
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(**{"NSCameraUsageDescription": "  Camera text ", "NSMicrophoneUsageDescription": "Mic",
                              "NS" + "X" * 40 + "UsageDescription": "long key"}),
              extra={"Payload/App.app/App": signed_macho(
                  {"a.one": True, "b.two": True, "c.three": True, "a" + "x" * 40: True})})
    result = inspect_ipa(path)
    assert result.entitlements == ("a.one", "b.two")
    assert result.privacy == {"NSCameraUsageDescription": "Camer"}


@pytest.mark.parametrize("damage", ["corrupt-deflate", "encrypted"])
def test_unreadable_executable_member_contributes_nothing(tmp_path, damage):
    path = tmp_path / "app.ipa"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Payload/App.app/Info.plist", plistlib.dumps(bundle()))
        archive.writestr("Payload/App.app/App", signed_macho({GROUPS: ["g"]}), zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("Payload/App.app/App")
    raw = bytearray(path.read_bytes())
    if damage == "corrupt-deflate":
        data = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        raw[data:data + 4] = b"\xff" * 4                # an invalid deflate block type
    else:
        central = raw.rfind(b"PK\x01\x02")              # the executable's entry is written last
        raw[central + 8] |= 0x01                        # general-purpose flag: encrypted
    path.write_bytes(bytes(raw))
    result = inspect_ipa(path)
    assert result.bundle_identifier == "com.example.app" and result.entitlements == ()


def test_as_many_load_commands_as_fit_are_read(tmp_path):
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={"Payload/App.app/App": signed_macho({GROUPS: ["g"]}, ncmds=5)})
    assert inspect_ipa(path).entitlements == (GROUPS,)


def test_default_caps():
    import scripts.ipa_inspection as module
    # Security bounds (AGENTS.md invariant 4): a change here is deliberate.
    assert {name: getattr(module, name) for name in (
        "MAX_INFO_PLIST_BYTES", "MAX_EXECUTABLE_BYTES", "MAX_INFLATED_EXECUTABLE_BYTES",
        "MAX_ENTITLEMENTS_BYTES", "MAX_PARSED_ENTITLEMENTS_BYTES", "MAX_LOAD_COMMANDS_BYTES",
        "MAX_EXTENSION_PLIST_BYTES", "MAX_FAT_SLICES", "MAX_SIGNATURE_SLOTS", "MAX_EXTENSIONS",
        "MAX_KEY_LENGTH", "MAX_ENTITLEMENT_KEYS", "MAX_PRIVACY_KEYS", "MAX_PRIVACY_TEXT")} == {
        "MAX_INFO_PLIST_BYTES": 4 << 20, "MAX_EXECUTABLE_BYTES": 1 << 30,
        "MAX_INFLATED_EXECUTABLE_BYTES": 2 << 30, "MAX_ENTITLEMENTS_BYTES": 64 << 10,
        "MAX_PARSED_ENTITLEMENTS_BYTES": 4 << 20, "MAX_LOAD_COMMANDS_BYTES": 256 << 10,
        "MAX_EXTENSION_PLIST_BYTES": 256 << 10, "MAX_FAT_SLICES": 8, "MAX_SIGNATURE_SLOTS": 64,
        "MAX_EXTENSIONS": 64, "MAX_KEY_LENGTH": 256, "MAX_ENTITLEMENT_KEYS": 256,
        "MAX_PRIVACY_KEYS": 64, "MAX_PRIVACY_TEXT": 2000}


@pytest.mark.parametrize("filler, read", [(60 * 1024, True), (70 * 1024, False)])
def test_entitlements_blob_cap_at_its_default(tmp_path, filler, read):
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={"Payload/App.app/App": signed_macho({GROUPS: ["x" * filler]})})
    assert inspect_ipa(path).entitlements == ((GROUPS,) if read else ())


@pytest.mark.parametrize("slack, expected", [(0, (GROUPS, "e0", "e1")), (-1, (GROUPS, "e0"))])
def test_entitlements_blobs_share_one_parse_budget_app_first(tmp_path, monkeypatch, slack, expected):
    import scripts.ipa_inspection as module
    path = tmp_path / "app.ipa"
    _with_extensions(path, 2)
    # Each blob is charged in full, header included: one byte short skips the last.
    blob = len(plistlib.dumps({"e0": True})) + 8
    monkeypatch.setattr(module, "MAX_PARSED_ENTITLEMENTS_BYTES",
                        len(plistlib.dumps({GROUPS: ["g"]})) + 8 + 2 * blob + slack)
    assert inspect_ipa(path).entitlements == expected


def test_keys_are_trimmed_as_they_accumulate(tmp_path, monkeypatch):
    import scripts.ipa_inspection as module
    monkeypatch.setattr(module, "MAX_ENTITLEMENT_KEYS", 2)
    path = tmp_path / "app.ipa"
    _with_extensions(path, 2)                                            # GROUPS, e0, e1
    assert inspect_ipa(path).entitlements == (GROUPS, "e0")
    fat = fat_macho(signed_macho({"a": True, "b": True}), signed_macho({"c": True, "d": True}))
    write_ipa(path, bundle(), extra={"Payload/App.app/App": fat})
    with zipfile.ZipFile(path) as archive:
        assert module._executable_entitlements(
            archive, archive.getinfo("Payload/App.app/App"), module._Budget()) == {"a", "b"}


@pytest.mark.parametrize("method, read", [
    (zipfile.ZIP_STORED, True), (zipfile.ZIP_DEFLATED, True),
    (zipfile.ZIP_BZIP2, False), (zipfile.ZIP_LZMA, False),
])
def test_only_stored_and_deflated_executables_are_read(tmp_path, method, read):
    path = tmp_path / "app.ipa"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Payload/App.app/Info.plist", plistlib.dumps(bundle()))
        archive.writestr("Payload/App.app/App", signed_macho({GROUPS: ["g"]}), method)
    assert inspect_ipa(path).entitlements == ((GROUPS,) if read else ())


@pytest.mark.parametrize("plist", [b"not a plist", plistlib.dumps(["a", "list"])])
def test_unreadable_extension_plist_contributes_nothing(tmp_path, plist):
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={
        "Payload/App.app/App": signed_macho({GROUPS: ["g"]}),
        "Payload/App.app/PlugIns/Bad.appex/Info.plist": plist,
        "Payload/App.app/PlugIns/Good.appex/Info.plist": plistlib.dumps(
            {"CFBundleExecutable": "Good", "NSCameraUsageDescription": "Scan"}),
        "Payload/App.app/PlugIns/Good.appex/Good": signed_macho({"e.good": True}),
    })
    result = inspect_ipa(path)
    assert result.entitlements == (GROUPS, "e.good") and result.privacy == {"NSCameraUsageDescription": "Scan"}


def test_an_unreadable_slice_keeps_its_siblings(tmp_path):
    broken = signed_macho({"e.broken": True}).replace(b"<dict>", b"<dicx>")   # malformed XML, same length
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={"Payload/App.app/App": fat_macho(broken, signed_macho({GROUPS: ["g"]}))})
    assert inspect_ipa(path).entitlements == (GROUPS,)


@pytest.mark.parametrize("blob_length", range(8))
def test_a_short_entitlements_blob_never_reads_to_the_end(tmp_path, monkeypatch, blob_length):
    import scripts.ipa_inspection as module
    sizes, read_exact = [], module._read_exact
    monkeypatch.setattr(module, "_read_exact", lambda handle, size: sizes.append(size) or read_exact(handle, size))
    thin = signed_macho({GROUPS: ["g"]})
    at = thin.index(b"\xfa\xde\x71\x71")
    thin = thin[:at + 4] + struct.pack(">I", blob_length) + thin[at + 8:]
    path = tmp_path / "app.ipa"
    write_ipa(path, bundle(), extra={"Payload/App.app/App": thin})
    assert inspect_ipa(path).entitlements == () and sizes and min(sizes) >= 0


def test_a_slice_behind_the_reader_is_refused():
    import scripts.ipa_inspection as module
    handle = io.BytesIO(signed_macho({GROUPS: ["g"]}))
    handle.read(16)
    with pytest.raises(ValueError):
        module._slice_entitlements(handle, 0, len(handle.getvalue()), module._Budget())


def test_extension_privacy_is_trimmed_as_it_accumulates(tmp_path, monkeypatch):
    import scripts.ipa_inspection as module
    monkeypatch.setattr(module, "MAX_PRIVACY_KEYS", 1)
    path = tmp_path / "app.ipa"
    _with_extensions(path, 3)                                            # NSE0..NSE2UsageDescription
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        _keys, privacy = module._bundle_permissions(archive, names, "Payload/App.app/Info.plist", bundle())
    assert privacy == {"NSE0UsageDescription": "text"}
