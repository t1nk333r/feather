"""Tests for scripts/apk_inspection.py.

A real binary AndroidManifest.xml cannot be authored by hand in a test (that
is why tests/test_android.py monkeypatches ``_inspect_apk`` wholesale). Here
we exercise the module directly: the "not a readable APK" path needs no
fixture at all (any non-zip file trips pyaxmlparser), and the metadata
validation paths are exercised by monkeypatching ``pyaxmlparser.APK`` with a
fake that returns fixed field values, modelled on tests/test_android.py's
``fake_inspect`` pattern.
"""

import pytest

from scripts.apk_inspection import ApkInspection, ApkInspectionError, inspect_apk


class _FakeApk:
    """Stand-in for pyaxmlparser.APK returning fixed metadata."""

    def __init__(self, package="com.example.app", version_code="42",
                 version_name="4.2", min_sdk=21, target_sdk=34,
                 app_name="Demo", valid=True, signed=True):
        self.package = package
        self.version_code = version_code
        self.version_name = version_name
        self._min_sdk = min_sdk
        self._target_sdk = target_sdk
        self._app_name = app_name
        self._valid = valid
        self._signed = signed

    def is_valid_APK(self):
        return self._valid

    def is_signed(self):
        return self._signed

    def get_min_sdk_version(self):
        return self._min_sdk

    def get_target_sdk_version(self):
        return self._target_sdk

    def get_app_name(self):
        return self._app_name


def _patch_apk(monkeypatch, **fake_kwargs):
    import pyaxmlparser

    def fake_ctor(path):
        return _FakeApk(**fake_kwargs)

    monkeypatch.setattr(pyaxmlparser, "APK", fake_ctor)


def test_rejects_unreadable_file(tmp_path):
    path = tmp_path / "not-an-apk.apk"
    path.write_bytes(b"just some garbage bytes, not a zip archive at all")
    with pytest.raises(ApkInspectionError, match="Not a readable APK"):
        inspect_apk(path, "not-an-apk.apk")


def test_rejects_invalid_androidmanifest(tmp_path, monkeypatch):
    path = tmp_path / "invalid.apk"
    path.write_bytes(b"fake-apk-bytes")
    _patch_apk(monkeypatch, valid=False)
    with pytest.raises(ApkInspectionError, match="Not a valid APK"):
        inspect_apk(path, "invalid.apk")


def test_rejects_invalid_package_name(tmp_path, monkeypatch):
    path = tmp_path / "bad-package.apk"
    path.write_bytes(b"fake-apk-bytes")
    _patch_apk(monkeypatch, package="not_a_valid_package")
    with pytest.raises(ApkInspectionError, match="invalid package name"):
        inspect_apk(path, "bad-package.apk")


def test_rejects_non_integer_version_code(tmp_path, monkeypatch):
    path = tmp_path / "bad-version.apk"
    path.write_bytes(b"fake-apk-bytes")
    _patch_apk(monkeypatch, version_code="not-a-number")
    with pytest.raises(ApkInspectionError, match="non-integer versionCode"):
        inspect_apk(path, "bad-version.apk")


def test_rejects_non_positive_version_code(tmp_path, monkeypatch):
    path = tmp_path / "zero-version.apk"
    path.write_bytes(b"fake-apk-bytes")
    _patch_apk(monkeypatch, version_code="0")
    with pytest.raises(ApkInspectionError, match="must be > 0"):
        inspect_apk(path, "zero-version.apk")


def test_rejects_unsigned_apk(tmp_path, monkeypatch):
    path = tmp_path / "unsigned.apk"
    path.write_bytes(b"fake-apk-bytes")
    _patch_apk(monkeypatch, signed=False)
    with pytest.raises(ApkInspectionError, match="APK is unsigned"):
        inspect_apk(path, "unsigned.apk")


def test_accepts_happy_path(tmp_path, monkeypatch):
    path = tmp_path / "good.apk"
    path.write_bytes(b"fake-apk-bytes")
    _patch_apk(monkeypatch)
    result = inspect_apk(path, "good.apk")
    assert isinstance(result, ApkInspection)
    assert result.package == "com.example.app"
    assert result.version_code == 42
    assert result.version_name == "4.2"
    assert result.min_sdk == 21
    assert result.target_sdk == 34
    assert result.app_name == "Demo"


def test_error_message_carries_label(tmp_path, monkeypatch):
    path = tmp_path / "labeled.apk"
    path.write_bytes(b"fake-apk-bytes")
    _patch_apk(monkeypatch, valid=False)
    with pytest.raises(ApkInspectionError, match=r"^job 42: Not a valid APK"):
        inspect_apk(path, "job 42")


def _zip_with(path, members):
    import zipfile
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, size in members.items():
            with zf.open(name, "w", force_zip64=True) as handle:
                remaining = size
                while remaining:
                    step = min(remaining, 1 << 20)
                    handle.write(b"\0" * step)
                    remaining -= step


def test_rejects_oversized_manifest_before_parsing(tmp_path, monkeypatch):
    import scripts.apk_inspection as mod
    path = tmp_path / "bomb.apk"
    _zip_with(path, {"AndroidManifest.xml": mod.MAX_MANIFEST_BYTES + 1})

    def never(*_a, **_k):
        raise AssertionError("pyaxmlparser must not be reached")
    import pyaxmlparser
    monkeypatch.setattr(pyaxmlparser, "APK", never)
    with pytest.raises(ApkInspectionError, match="implausibly large"):
        inspect_apk(path, "bomb.apk")


def test_oversized_resources_skips_label_instead_of_inflating(tmp_path, monkeypatch):
    import scripts.apk_inspection as mod
    path = tmp_path / "big.apk"
    _zip_with(path, {"AndroidManifest.xml": 10, "resources.arsc": mod.MAX_RESOURCES_BYTES + 1})

    class NoLabel(_FakeApk):
        def get_app_name(self):
            raise AssertionError("resources.arsc must not be read")
    import pyaxmlparser
    monkeypatch.setattr(pyaxmlparser, "APK", lambda _p: NoLabel())
    result = inspect_apk(path, "big.apk")
    assert result.app_name == "com.example.app"
