"""Dependency-free inspection of an IPA's top-level application plist and of
the entitlements its executables are signed with."""

import plistlib
import re
import struct
import zipfile
from dataclasses import dataclass


_APP_INFO_PLIST = re.compile(r"^Payload/[^/]+\.app/Info\.plist$")
# App extensions. AltStore and SideStore check their entitlements and privacy
# keys against the source along with the app's own.
_EXTENSION_INFO_PLIST = re.compile(r"^Payload/[^/]+\.app/(?:PlugIns|Extensions)/[^/]+\.appex/Info\.plist$")
# Real Info.plists are a few KB. Checked before inflating, and again on the
# bytes actually read (the central directory's size is attacker-controlled):
# a 1.5 MB IPA whose plist inflates to 1.5 GB otherwise costs ~3 GB of RAM.
MAX_INFO_PLIST_BYTES = 4 * 1024 * 1024
# Reaching an executable's code signature inflates everything before it, so
# the executable's declared size bounds that work (the reader never returns
# more than it). Real iOS executables are well under it. The other caps bound
# what is held in memory at once.
MAX_EXECUTABLE_BYTES = 1024 * 1024 * 1024
MAX_LOAD_COMMANDS_BYTES = 4 * 1024 * 1024
MAX_ENTITLEMENTS_BYTES = 1024 * 1024
MAX_FAT_SLICES = 8
MAX_SIGNATURE_SLOTS = 64
MAX_EXTENSIONS = 64

# fat_arch and fat_arch_64: cputype, cpusubtype, offset, size, ...
_FAT_ARCH_LAYOUTS = {0xCAFEBABE: (20, ">IIII"), 0xCAFEBABF: (32, ">IIQQ")}
# 32- and 64-bit Mach-O header sizes, by their little-endian magic.
_MACHO_HEADER_SIZES = {b"\xce\xfa\xed\xfe": 28, b"\xcf\xfa\xed\xfe": 32}
_LC_CODE_SIGNATURE = 0x1D
_CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
_CSMAGIC_EMBEDDED_ENTITLEMENTS = 0xFADE7171
_CSSLOT_ENTITLEMENTS = 5


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
    # Entitlement keys the app's and its extensions' executables are signed
    # with, sorted. Empty for an unsigned IPA; an executable whose signature
    # cannot be read contributes nothing.
    entitlements: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "privacy", dict(self.privacy or {}))
        object.__setattr__(self, "entitlements", tuple(self.entitlements or ()))

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


# Identity strings end up in URLs, file names and the admin UI. Apple allows
# letters, digits, "." and "-" in a bundle ID; "_" is tolerated because real
# patched builds use it. Versions in the wild include "408.1.0_TH" and
# "1.0 (2)". Quotes, angle brackets, slashes and control characters are never
# legitimate and are refused before anything is stored.
BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+()~-]{0,63}$")


def _clean_optional(value):
    if not isinstance(value, (str, int, float)):
        return None
    return str(value).strip() or None


def _privacy(plist):
    return {
        key: value.strip()
        for key, value in plist.items()
        if isinstance(key, str)
        and key.startswith("NS")
        and key.endswith("UsageDescription")
        and isinstance(value, str)
        and value.strip()
    }


def _read_exact(handle, size):
    data = handle.read(size)
    if len(data) != size:
        raise ValueError("truncated")
    return data


def _extension_plist(archive, name):
    info = archive.getinfo(name)
    if info.file_size > MAX_INFO_PLIST_BYTES:
        return None
    with archive.open(info) as handle:
        raw = handle.read(MAX_INFO_PLIST_BYTES + 1)
    if len(raw) > MAX_INFO_PLIST_BYTES:
        return None
    plist = plistlib.loads(raw)
    return plist if isinstance(plist, dict) else None


def _slice_entitlements(handle, start, end):
    """Entitlement keys of the thin Mach-O at [start, end) of the executable,
    from the XML entitlements blob of its embedded code signature."""
    handle.seek(start)
    magic = _read_exact(handle, 4)
    header_size = _MACHO_HEADER_SIZES.get(magic)
    if header_size is None:
        return set()
    header = magic + _read_exact(handle, header_size - 4)
    ncmds, sizeofcmds = struct.unpack_from("<II", header, 16)
    if sizeofcmds > MAX_LOAD_COMMANDS_BYTES or start + header_size + sizeofcmds > end:
        return set()
    commands = _read_exact(handle, sizeofcmds)
    offset, signature = 0, None
    for _ in range(ncmds):
        if offset + 8 > sizeofcmds:
            break
        cmd, cmdsize = struct.unpack_from("<II", commands, offset)
        if cmdsize < 8 or offset + cmdsize > sizeofcmds:
            break
        if cmd == _LC_CODE_SIGNATURE and cmdsize >= 16:
            signature = struct.unpack_from("<II", commands, offset + 8)
            break
        offset += cmdsize
    if signature is None:
        return set()                                   # unsigned
    dataoff, datasize = signature
    sig_start = start + dataoff
    if datasize < 12 or sig_start + datasize > end:
        return set()
    handle.seek(sig_start)
    magic, length, count = struct.unpack(">III", _read_exact(handle, 12))
    index_end = 12 + 8 * count
    if magic != _CSMAGIC_EMBEDDED_SIGNATURE or count > MAX_SIGNATURE_SLOTS or length > datasize or index_end > length:
        return set()
    index = _read_exact(handle, 8 * count)
    blob_offset = next((off for slot, off in struct.iter_unpack(">II", index) if slot == _CSSLOT_ENTITLEMENTS), None)
    if blob_offset is None or blob_offset < index_end or blob_offset + 8 > length:
        return set()                                   # signed without entitlements
    handle.seek(sig_start + blob_offset)
    magic, blob_length = struct.unpack(">II", _read_exact(handle, 8))
    if (magic != _CSMAGIC_EMBEDDED_ENTITLEMENTS or not 8 <= blob_length <= MAX_ENTITLEMENTS_BYTES
            or blob_offset + blob_length > length):
        return set()
    entitlements = plistlib.loads(_read_exact(handle, blob_length - 8))
    if not isinstance(entitlements, dict):
        return set()
    return {key for key in entitlements if isinstance(key, str)}


def _executable_entitlements(archive, name):
    """Entitlement keys of every architecture slice of one executable."""
    info = archive.getinfo(name)
    size = info.file_size
    if size > MAX_EXECUTABLE_BYTES:
        return set()
    with archive.open(info) as handle:
        head = _read_exact(handle, 8)
        layout = _FAT_ARCH_LAYOUTS.get(struct.unpack(">I", head[:4])[0])
        if layout is None:
            slices = [(0, size)]
        else:
            entry_size, fields = layout
            nfat = struct.unpack(">I", head[4:])[0]
            if not 0 < nfat <= MAX_FAT_SLICES:
                return set()
            table = _read_exact(handle, entry_size * nfat)
            slices = []
            for i in range(nfat):
                _cputype, _cpusubtype, offset, slice_size = struct.unpack_from(fields, table, i * entry_size)
                if 8 + entry_size * nfat <= offset and offset + slice_size <= size:
                    slices.append((offset, offset + slice_size))
        keys = set()
        for start, end in sorted(slices):              # in file order: forward seeks only
            try:
                keys |= _slice_entitlements(handle, start, end)
            except Exception:
                continue
        return keys


def _bundle_permissions(archive, names, app_plist_name, app_plist):
    """Entitlement keys of the app's and its extensions' executables, and the
    privacy keys of the extensions' Info.plists. Best effort: any part that
    cannot be read contributes nothing and never fails the inspection, so an
    IPA that was accepted before is accepted unchanged."""
    bundles = [(app_plist_name, app_plist)]
    for name in [n for n in names if _EXTENSION_INFO_PLIST.match(n)][:MAX_EXTENSIONS]:
        try:
            bundles.append((name, _extension_plist(archive, name)))
        except Exception:
            continue
    members = set(names)
    keys, extension_privacy = set(), {}
    for plist_name, plist in bundles:
        if not isinstance(plist, dict):
            continue
        if plist is not app_plist:
            extension_privacy.update(_privacy(plist))
        executable = plist.get("CFBundleExecutable")
        if not isinstance(executable, str) or not executable or "/" in executable:
            continue
        member = plist_name[: -len("Info.plist")] + executable
        if member not in members:
            continue
        try:
            keys |= _executable_entitlements(archive, member)
        except Exception:
            continue
    return tuple(sorted(keys)), extension_privacy


def inspect_ipa(path, label="IPA"):
    """Inspect one IPA, retaining only normalized, non-sensitive plist fields
    and the keys (never the values) of its signed entitlements."""
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
            info = archive.getinfo(plist_names[0])
            if info.file_size > MAX_INFO_PLIST_BYTES:
                raise InspectionError(f"{safe_label}: top-level Info.plist is implausibly large")
            with archive.open(info) as handle:
                raw = handle.read(MAX_INFO_PLIST_BYTES + 1)
            if len(raw) > MAX_INFO_PLIST_BYTES:
                raise InspectionError(f"{safe_label}: top-level Info.plist is implausibly large")
            try:
                plist = plistlib.loads(raw)
            except Exception:
                raise InspectionError(f"{safe_label}: top-level Info.plist is malformed")
            if isinstance(plist, dict):
                entitlements, extension_privacy = _bundle_permissions(archive, names, plist_names[0], plist)
    except InspectionError:
        raise
    except Exception as e:
        # Not only OSError/BadZipFile: an unsupported method (Deflate64),
        # corrupt deflate data (zlib.error) or an encrypted member
        # (RuntimeError) escaped as a crash in callers that expect only
        # InspectionError.
        raise InspectionError(f"{safe_label}: IPA archive could not be read ({type(e).__name__})")

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
    if not BUNDLE_ID_RE.match(bundle_identifier):
        raise InspectionError(f"{safe_label}: IPA declares an invalid bundle identifier: {bundle_identifier[:80]!r}")
    for field, value in (("version", version), ("build version", build_version)):
        if not VERSION_RE.match(value):
            raise InspectionError(f"{safe_label}: IPA declares an invalid {field}: {value[:80]!r}")

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

    return IpaInspection(
        bundle_identifier=bundle_identifier,
        version=version,
        build_version=build_version,
        name=_clean_optional(plist.get("CFBundleDisplayName") or plist.get("CFBundleName")),
        minimum_os_version=_clean_optional(plist.get("MinimumOSVersion")),
        supported_platforms=platforms,
        device_families=families,
        # The app's own text wins over an extension's for the same key.
        privacy={**extension_privacy, **_privacy(plist)},
        entitlements=entitlements,
    )
