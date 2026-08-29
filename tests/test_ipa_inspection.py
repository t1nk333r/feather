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
