"""Dependency-free inspection of an IPA's top-level application plist."""

import plistlib
import re
import zipfile
from dataclasses import dataclass


_APP_INFO_PLIST = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")


class InspectionError(RuntimeError):
    """An IPA archive or its top-level application plist is invalid."""


@dataclass(frozen=True)
class IpaInspection:
    bundle_identifier: str
    version: str
    build_version: str
    name: str = None
    minimum_os_version: str = None
    supported_platforms: tuple = ()
    device_families: tuple = ()
    privacy: dict = None

    def __post_init__(self):
        object.__setattr__(self, "privacy", dict(self.privacy or {}))

    @property
    def platform(self):
        platforms = {value.lower() for value in self.supported_platforms}
        if "iphoneos" in platforms:
            return "ios"
        if "appletvos" in platforms:
            return "tvos"
        if 3 in self.device_families:
            return "tvos"
        return "unknown"


def _clean_optional(value):
    if not isinstance(value, (str, int, float)):
        return None
    return str(value).strip() or None


def inspect_ipa(path, label="IPA"):
    """Inspect one IPA, retaining only normalized, non-sensitive plist fields."""
    safe_label = str(label or "IPA").replace("\n", " ").replace("\r", " ")[:200]
    if not zipfile.is_zipfile(path):
        raise InspectionError(f"{safe_label}: file is not a valid ZIP archive")
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if not any(name.startswith("Payload/") for name in names):
                raise InspectionError(f"{safe_label}: archive has no Payload entry")
            plist_names = [name for name in names if _APP_INFO_PLIST.match(name)]
            if len(plist_names) != 1:
                raise InspectionError(
                    f"{safe_label}: expected exactly one top-level app Info.plist, "
                    f"found {len(plist_names)}"
                )
            try:
                plist = plistlib.loads(archive.read(plist_names[0]))
            except Exception:
                raise InspectionError(f"{safe_label}: top-level Info.plist is malformed")
    except InspectionError:
        raise
    except (OSError, zipfile.BadZipFile):
        raise InspectionError(f"{safe_label}: IPA archive could not be read")

    if not isinstance(plist, dict):
        raise InspectionError(f"{safe_label}: top-level Info.plist is not a dictionary")
    bundle_identifier = _clean_optional(plist.get("CFBundleIdentifier"))
    version = _clean_optional(
        plist.get("CFBundleShortVersionString") or plist.get("CFBundleVersion")
    )
    build_version = _clean_optional(plist.get("CFBundleVersion")) or version
    if not bundle_identifier or not version:
        raise InspectionError(
            f"{safe_label}: IPA plist is missing a bundle identifier or version"
        )

    platforms_raw = plist.get("CFBundleSupportedPlatforms")
    if isinstance(platforms_raw, list):
        platforms = tuple(value for value in platforms_raw if isinstance(value, str))
    else:
        fallback = plist.get("DTPlatformName")
        platforms = (fallback,) if isinstance(fallback, str) and fallback.strip() else ()
    families_raw = plist.get("UIDeviceFamily")
    if isinstance(families_raw, list):
        families = tuple(value for value in families_raw if isinstance(value, int))
    elif isinstance(families_raw, int):
        families = (families_raw,)
    else:
        families = ()

    privacy = {
        key: value.strip()
        for key, value in plist.items()
        if isinstance(key, str)
        and key.startswith("NS")
        and key.endswith("UsageDescription")
        and isinstance(value, str)
        and value.strip()
    }
    return IpaInspection(
        bundle_identifier=bundle_identifier,
        version=version,
        build_version=build_version,
        name=_clean_optional(plist.get("CFBundleDisplayName") or plist.get("CFBundleName")),
        minimum_os_version=_clean_optional(plist.get("MinimumOSVersion")),
        supported_platforms=platforms,
        device_families=families,
        privacy=privacy,
    )
