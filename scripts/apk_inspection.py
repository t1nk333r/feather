"""Inspection of an APK's binary AndroidManifest via pyaxmlparser."""

import re
from dataclasses import dataclass


ANDROID_PACKAGE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$')


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
    return ApkInspection(
        package=package,
        version_code=version_code,
        version_name=apk.version_name or str(version_code),
        min_sdk=apk.get_min_sdk_version(),
        target_sdk=apk.get_target_sdk_version(),
        app_name=apk.get_app_name() or package,
    )
