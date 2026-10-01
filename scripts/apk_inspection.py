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
    debuggable: bool = False


def _is_debuggable(apk):
    """android:debuggable="true" on <application>. fdroidserver only warns
    about it and publishes anyway, so the uploader has to be told here."""
    try:
        value = apk.get_attribute_value("application", "debuggable")
    except Exception:
        return False
    return str(value).strip().lower() in ("true", "1", "-1")


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
        debuggable=_is_debuggable(apk),
    )


# --- launcher icon -----------------------------------------------------------

MAX_ICON_MEMBER_BYTES = 8 * 1024 * 1024
_ICON_OUTPUT_PX = 512
_RASTER_EXTS = (".png", ".webp", ".jpg", ".jpeg")
_DENSITY_RANK = {"ldpi": 1, "mdpi": 2, "tvdpi": 3, "hdpi": 4, "xhdpi": 5, "xxhdpi": 6, "xxxhdpi": 7}
_LAUNCHER_NAMES = ("ic_launcher", "ic_launcher_round", "app_icon", "launcher_icon", "icon")


def _density(name):
    folder = name.split("/")[1] if name.count("/") >= 2 else ""
    for part in folder.split("-")[1:]:
        if part in _DENSITY_RANK:
            return _DENSITY_RANK[part]
    return 0


def _open_raster(archive, name):
    """Decoded RGBA image for one archive member, or None."""
    from PIL import Image
    import io
    try:
        info = archive.getinfo(name)
        if info.file_size > MAX_ICON_MEMBER_BYTES:
            return None
        with archive.open(info) as handle:
            raw = handle.read(MAX_ICON_MEMBER_BYTES + 1)
        img = Image.open(io.BytesIO(raw))
        if img.size[0] * img.size[1] > 4096 * 4096:
            return None
        img.load()
        return img.convert("RGBA")
    except Exception:
        return None


def _best_by_basename(names, basenames):
    """Highest-density raster whose file name (sans extension) is in basenames."""
    hits = [n for n in names
            if n.startswith("res/") and n.lower().endswith(_RASTER_EXTS)
            and n.rsplit("/", 1)[-1].rsplit(".", 1)[0] in basenames
            and ("/mipmap" in n or "/drawable" in n)]
    return sorted(hits, key=lambda n: (_density(n), n), reverse=True)


def extract_apk_icon(path):
    """Best-effort launcher icon as PNG bytes (at most 512x512), or None.

    Order: the manifest's icon resource resolved through resources.arsc at
    <= xxxhdpi -- which skips the anydpi adaptive-icon XML and lands on the
    legacy raster most APKs still ship (Flutter/React Native always do,
    native apps whenever minSdk < 26); then launcher-named rasters; then a
    raster adaptive foreground composited over its background. Vector-only
    icons are not rendered. Never raises."""
    import io
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            sizes = {i.filename: i.file_size for i in archive.infolist()}
            ordered = []
            if (sizes.get("AndroidManifest.xml", 0) <= MAX_MANIFEST_BYTES
                    and sizes.get("resources.arsc", 0) <= MAX_RESOURCES_BYTES):
                try:
                    from pyaxmlparser import APK
                    apk = APK(path)
                    for max_dpi in (640, 65536):
                        resolved = apk.get_app_icon(max_dpi=max_dpi)
                        if isinstance(resolved, str) and resolved.lower().endswith(_RASTER_EXTS):
                            ordered.append(resolved)
                            stem = resolved.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                            ordered.extend(_best_by_basename(names, {stem}))
                        elif isinstance(resolved, str) and resolved.lower().endswith(".xml"):
                            stem = resolved.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                            ordered.extend(_best_by_basename(names, {stem}))
                except Exception:
                    pass
            ordered.extend(_best_by_basename(names, set(_LAUNCHER_NAMES)))

            icon = None
            for name in dict.fromkeys(ordered):
                if name in sizes:
                    icon = _open_raster(archive, name)
                    if icon is not None:
                        break

            if icon is None:
                fg_names = _best_by_basename(names, {"ic_launcher_foreground"})
                fg = _open_raster(archive, fg_names[0]) if fg_names else None
                if fg is not None:
                    bg_names = _best_by_basename(names, {"ic_launcher_background"})
                    bg = _open_raster(archive, bg_names[0]) if bg_names else None
                    from PIL import Image
                    base = bg.resize(fg.size) if bg is not None else Image.new("RGBA", fg.size, (255, 255, 255, 255))
                    base.alpha_composite(fg)
                    icon = base
            if icon is None:
                return None
            if max(icon.size) > _ICON_OUTPUT_PX:
                icon.thumbnail((_ICON_OUTPUT_PX, _ICON_OUTPUT_PX))
            out = io.BytesIO()
            icon.save(out, format="PNG")
            return out.getvalue()
    except Exception:
        return None
