"""Inspection of an APK's binary AndroidManifest via pyaxmlparser."""

import re
import zipfile
from dataclasses import dataclass


ANDROID_PACKAGE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$')

# pyaxmlparser inflates AndroidManifest.xml and resources.arsc fully in
# memory; a 1 MB APK with a bomb in resources.arsc otherwise costs ~2 GB.
# The manifest is tiny in practice; resources.arsc is only read for the
# app label, so an oversized one is skipped rather than rejected.
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_RESOURCES_BYTES = 64 * 1024 * 1024


def _member_sizes(path):
    """Declared uncompressed sizes, or None when the zip is unreadable (let
    pyaxmlparser produce its usual error)."""
    try:
        with zipfile.ZipFile(path) as archive:
            return {info.filename: info.file_size for info in archive.infolist()}
    except (OSError, zipfile.BadZipFile, ValueError):
        return None


class ApkInspectionError(RuntimeError):
    """An APK file is unreadable or declares invalid identity/version metadata."""


@dataclass(frozen=True)
class ApkInspection:
    package: str
    version_code: int
    version_name: str
    min_sdk: str = None
    target_sdk: str = None
    app_name: str = None


def inspect_apk(path, label="APK"):
    """Read identity and version out of an APK's binary AndroidManifest.

    Returns an ApkInspection or raises ApkInspectionError with an
    operator-readable message. pyaxmlparser returns version_code as a
    *string*; it is converted here so callers never compare "10" < "9".
    """
    safe_label = str(label or "APK").replace("\n", " ").replace("\r", " ")[:200]
    from pyaxmlparser import APK  # imported lazily: keeps import fast for tests
    sizes = _member_sizes(path)
    if sizes is not None and sizes.get("AndroidManifest.xml", 0) > MAX_MANIFEST_BYTES:
        raise ApkInspectionError(f"{safe_label}: AndroidManifest.xml is implausibly large")
    read_label = sizes is None or sizes.get("resources.arsc", 0) <= MAX_RESOURCES_BYTES
    try:
        apk = APK(path)
    except Exception as e:
        raise ApkInspectionError(f"{safe_label}: Not a readable APK: {e}")
    if not apk.is_valid_APK():
        raise ApkInspectionError(f"{safe_label}: Not a valid APK (no AndroidManifest.xml)")
    package = apk.package or ""
    if not ANDROID_PACKAGE_RE.match(package):
        raise ApkInspectionError(f"{safe_label}: APK declares an invalid package name: {package!r}")
    try:
        version_code = int(apk.version_code)
    except (TypeError, ValueError):
        raise ApkInspectionError(
            f"{safe_label}: APK declares a non-integer versionCode: {apk.version_code!r}"
        )
    if version_code <= 0:
        raise ApkInspectionError(f"{safe_label}: APK declares versionCode {version_code}; must be > 0")
    # `fdroid update` never publishes an unsigned APK: it logs "Skipping ...
    # with invalid signature" and still exits 0, so accepting one here would
    # report "Added" for a version that can never reach the index.
    if not apk.is_signed():
        raise ApkInspectionError(
            f"{safe_label}: APK is unsigned (no v1/v2/v3 signature); "
            "F-Droid cannot publish it -- upload the signed release build"
        )
    return ApkInspection(
        package=package,
        version_code=version_code,
        version_name=apk.version_name or str(version_code),
        min_sdk=apk.get_min_sdk_version(),
        target_sdk=apk.get_target_sdk_version(),
        app_name=(apk.get_app_name() if read_label else None) or package,
    )
