from flask import Flask, render_template, request, jsonify, send_file, redirect, session, send_from_directory, g
import json
import os
import re
import copy
import errno
import shutil
import logging
import qrcode
import io
import requests
import tempfile
import hashlib
import hmac
import secrets
import threading
import time
import uuid
import boto3
import zipfile
import plistlib
import zlib
import struct
import fnmatch
import yaml
from PIL import Image
from functools import wraps
from botocore.exceptions import ClientError
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from werkzeug.utils import secure_filename
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import HTTPException

import sys
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import release_source_ingest as release_ingest
from apk_inspection import inspect_apk, ApkInspectionError, extract_apk_icon
from ipa_inspection import inspect_ipa, InspectionError as IpaInspectionError
import certificate_inspection

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Added by plan 051 -- in-memory ring buffer of recent WARNING+ log records,
# surfaced (auth-gated, secrets redacted) via GET /api/diagnostics.
import collections

_DIAG_BUFFER = collections.deque(maxlen=200)

def _redact_secret(text):
    """Never echo secret env VALUES into the diagnostics buffer (Hard Rule 4)."""
    s = text
    for name in ("ADMIN_PASSWORD", "SECRET_KEY", "GARAGE_S3_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "GITLAB_TOKEN"):
        val = os.environ.get(name)
        if val and len(val) >= 4 and val in s:
            s = s.replace(val, "***REDACTED***")
    return s

class _RingBufferLogHandler(logging.Handler):
    def emit(self, record):
        try:
            _DIAG_BUFFER.append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "message": _redact_secret(self.format(record)),
            })
        except Exception:
            pass  # a logging handler must never raise

_ring_handler = _RingBufferLogHandler()
_ring_handler.setLevel(logging.WARNING)
_ring_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_ring_handler)

app = Flask(__name__)

# Configuration
def _env(name, default=None):
    """os.environ.get, except that a blank value counts as unset.

    `cp .env.example .env` ships keys like `PORT=`; Compose passes those into
    the container as empty strings, which os.environ.get returns instead of
    the default -- int('') crashed startup, and blank TELEGRAM_NOTIFY_EVENTS,
    ICON/APK_STORAGE_BACKEND or GARAGE_S3_REGION silently replaced their
    documented defaults."""
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value


DATA_DIR = _env("DATA_DIR", "/app/data")
SOURCE_FILE = os.path.join(DATA_DIR, "source.json")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
IPA_FOLDER = os.path.join(DATA_DIR, "ipas")
ICON_FOLDER = os.path.join(DATA_DIR, "icons")
BACKUP_FOLDER = os.path.join(DATA_DIR, "backups")
# Plan 073: Android / F-Droid repository. Everything lives under
# DATA_DIR/fdroid, which is ALSO the working directory of the fdroid-index
# sidecar (compose service, profile "android"). Layout inside it:
#   repo/          APKs + the signed index the sidecar writes (served at /fdroid/repo/)
#   metadata/      one <package>.yml per app, written by feather
#   repo-config.json     repo name/description, written by feather, read by the sidecar
#   .update-requested    marker: feather touches it, the sidecar consumes it
#   last-update.json     sidecar's last result {ok, finished_at, log_tail}
#   fingerprint.txt      64-hex SHA-256 of the signing cert, written by the sidecar
#   config.yml / keystore.p12   owned by the sidecar (root); feather never reads them
FDROID_DIR = os.path.join(DATA_DIR, "fdroid")
FDROID_REPO_DIR = os.path.join(FDROID_DIR, "repo")
FDROID_METADATA_DIR = os.path.join(FDROID_DIR, "metadata")
FDROID_REPO_CONFIG = os.path.join(FDROID_DIR, "repo-config.json")
FDROID_UPDATE_MARKER = os.path.join(FDROID_DIR, ".update-requested")
FDROID_LAST_UPDATE = os.path.join(FDROID_DIR, "last-update.json")
FDROID_FINGERPRINT = os.path.join(FDROID_DIR, "fingerprint.txt")
ALLOWED_APK_EXTENSIONS = {'apk'}
# Android package names: Java identifiers separated by dots, at least two segments.
ANDROID_PACKAGE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$')
ANDROID_APK_FILENAME_RE = re.compile(r'^(.+)_(\d+)\.apk$')
SOURCE_ARTWORK_URL = "https://f002.backblazeb2.com/file/S30000PUBLIC/MEDIA-PUBLIC/feather-tinker-1024.png"
LEGACY_SOURCE_ARTWORK_URLS = {
    "iconURL": "https://f000.backblazeb2.com/file/rileytestut/ExampleSource/OctoSource.png",
    "headerURL": "https://f000.backblazeb2.com/file/rileytestut/ExampleSource/OceanHeader.png",
}
ALLOWED_EXTENSIONS = {'ipa'}
ALLOWED_ICON_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
ICON_MIME_TYPES = {
    'png': 'image/png',
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'webp': 'image/webp',
    'gif': 'image/gif',
}

# Environment-driven configuration
SECRET_KEY = _env("SECRET_KEY")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise RuntimeError(
        "ADMIN_PASSWORD is not set. Refusing to start with unauthenticated "
        "admin routes. Set it in .env (compose.yml loads it via env_file)."
    )
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL")
PORT = int(_env("PORT", "5000"))
MAX_CONTENT_LENGTH = int(_env("MAX_CONTENT_LENGTH", 2 * 1024 * 1024 * 1024))

# Added by plan 014 -- optional Telegram notification on catalog changes.
# Disabled unless both TELEGRAM_BOT_TOKEN and TELEGRAM_NOTIFY_CHAT_ID are
# set; see notify() below. TELEGRAM_BOT_TOKEN and BOT_API_BASE_URL are
# shared with plan 013's bot ingest script where that has landed --
# neither is redeclared in .env.example for this plan.
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_NOTIFY_CHAT_ID = _env("TELEGRAM_NOTIFY_CHAT_ID")
TELEGRAM_API_BASE = _env("BOT_API_BASE_URL", "https://api.telegram.org")
TELEGRAM_NOTIFY_EVENTS = set(
    event.strip()
    for event in _env("TELEGRAM_NOTIFY_EVENTS", "add_app,add_version,delete_app,android_add_apk").split(",")
    if event.strip()
)

# IPA storage backend (Plan 011). Defaults to "local" -- today's behaviour,
# unchanged -- so merging this is a no-op until the flag is deliberately
# flipped. See GarageIpaStorage below for the "refuse to start" validation.
STORAGE_BACKEND = _env("STORAGE_BACKEND", "local")
# Icons can use a different backend than IPAs (e.g. IPAs on Garage, icons on
# local disk at /app/data/icons). Defaults to STORAGE_BACKEND for backward
# compatibility, so existing single-backend deployments are unaffected.
ICON_STORAGE_BACKEND = _env("ICON_STORAGE_BACKEND", STORAGE_BACKEND)
# APKs always keep a local copy in data/fdroid/repo -- fdroidserver must
# read them to build and sign the index. With "garage" each APK is also
# uploaded, byte-identical, and downloads are redirected to the bucket.
APK_STORAGE_BACKEND = _env("APK_STORAGE_BACKEND", STORAGE_BACKEND)
# Wall-clock cap for any URL download. requests' timeout bounds each socket
# read, not the transfer: a server sending a byte every 299 s held a worker
# thread -- and, on the admin add paths, the catalogue lock -- indefinitely.
DOWNLOAD_DEADLINE_SECONDS = int(_env("DOWNLOAD_DEADLINE_SECONDS", "1800"))
# Debuggable APKs are published with a warning by default (fdroidserver itself
# only warns); set this to refuse them outright.
APK_REJECT_DEBUGGABLE = _env("APK_REJECT_DEBUGGABLE", "false").strip().lower() in ("1", "true", "yes", "on")
GARAGE_S3_ENDPOINT = _env("GARAGE_S3_ENDPOINT")
GARAGE_S3_REGION = _env("GARAGE_S3_REGION", "garage")
GARAGE_S3_ACCESS_KEY_ID = _env("GARAGE_S3_ACCESS_KEY_ID")
GARAGE_S3_SECRET_ACCESS_KEY = _env("GARAGE_S3_SECRET_ACCESS_KEY")
GARAGE_BUCKET = _env("GARAGE_BUCKET")
GARAGE_PUBLIC_BASE_URL = _env("GARAGE_PUBLIC_BASE_URL")
GARAGE_KEY_PREFIX = _env("GARAGE_KEY_PREFIX", "ipas")

app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# SESSION_COOKIE_SECURE defaults off: the app itself speaks plain HTTP and a
# Secure cookie is never stored over HTTP. Behind a TLS proxy, set it on so
# the admin cookie is never sent in clear text.
app.config["SESSION_COOKIE_SECURE"] = _env("SESSION_COOKIE_SECURE", "false").strip().lower() in ("1", "true", "yes", "on")
if SECRET_KEY:
    app.secret_key = SECRET_KEY
else:
    app.secret_key = os.urandom(32)
    logging.warning("SECRET_KEY not set — using a random key; sessions will not survive restart")

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def allowed_icon_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_ICON_EXTENSIONS

def get_file_size(filepath):
    """Get file size in bytes.

    Returns None (not 0) if the size cannot be determined, so "unknown"
    is distinguishable from "genuinely empty file". Callers that write
    this into a version's "size" field must normalize None themselves --
    a `null` size must never reach source.json.
    """
    try:
        return os.path.getsize(filepath)
    except OSError as e:
        logging.warning(f"Could not determine size of {filepath}: {e}")
        return None

def resolve_base_url():
    """The externally-reachable base URL for links written into source.json.

    Prefers PUBLIC_BASE_URL. Falls back to the request's Host header, which
    is client-controlled — so an admin browsing by LAN IP would otherwise
    bake that IP permanently into the catalog.
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip('/')
    logging.warning(
        "PUBLIC_BASE_URL is not set — falling back to the request Host header. "
        "URLs written to source.json will reflect however this request reached the server."
    )
    return request.url_root.rstrip('/')


def normalize_source(source_data):
    """Fill in AltStore-required fields that may be absent from the stored
    catalog, without ever overwriting a value that is already present.

    Plan 024: the AltStore source spec requires 'nsfw' at the top level,
    'appPermissions' per app, and 'buildVersion' per version. Older or
    hand-edited catalogs can lack these, which makes a strict client
    decoder reject the whole document. This runs at serve time only —
    it never writes to disk — so it fixes every existing entry on the
    next request without a migration.

    Works on a deep copy; the argument is never mutated. Tolerates
    malformed input (missing 'apps', a non-dict app or version) by
    leaving the offending element untouched rather than raising, since
    this sits on the /source.json request path that every subscribed
    device polls and which must never 500.
    """
    if not isinstance(source_data, dict):
        return source_data

    data = copy.deepcopy(source_data)

    data.setdefault("nsfw", False)

    apps = data.get('apps')
    if isinstance(apps, list):
        for app_entry in apps:
            if not isinstance(app_entry, dict):
                continue
            if 'appPermissions' not in app_entry:
                app_entry['appPermissions'] = {"entitlements": [], "privacy": {}}

            icon = app_entry.get('iconURL')
            if not (isinstance(icon, str) and icon.strip()):
                top_icon = data.get('iconURL')
                app_entry['iconURL'] = top_icon if (isinstance(top_icon, str) and top_icon.strip()) else SOURCE_ARTWORK_URL

            versions = app_entry.get('versions')
            if isinstance(versions, list):
                seen = set()
                deduped = []
                for version_entry in versions:
                    if not isinstance(version_entry, dict):
                        deduped.append(version_entry)  # leave malformed entries untouched
                        continue
                    v = version_entry.get('version')
                    if v in seen:
                        continue
                    seen.add(v)
                    if 'buildVersion' not in version_entry:
                        version_entry['buildVersion'] = str(version_entry.get('version', ''))
                    deduped.append(version_entry)
                app_entry['versions'] = deduped

    return data


def notify(event, text):
    """Fire-and-forget Telegram message. Never raises, never blocks the caller.

    Plan 014. Call this from the route layer only, after SourceManager has
    already returned -- never from inside SourceManager, which holds
    self._lock across its whole read-modify-write and would otherwise
    serialise every publish behind Telegram's latency.

    Returns immediately with no network call at all if notifications are
    disabled (TELEGRAM_BOT_TOKEN / TELEGRAM_NOTIFY_CHAT_ID unset) or if
    `event` is not in the configured TELEGRAM_NOTIFY_EVENTS set. Otherwise
    the request is sent on a daemon thread, so the caller never waits on
    Telegram, and any failure is swallowed and logged at warning -- no
    notification problem may ever reach the HTTP response.
    """
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_NOTIFY_CHAT_ID):
        return
    if event not in TELEGRAM_NOTIFY_EVENTS:
        return

    def _send():
        try:
            requests.post(
                f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TELEGRAM_NOTIFY_CHAT_ID,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
        except Exception as e:
            logging.warning(f"Telegram notification failed: {str(e)}")

    threading.Thread(target=_send, daemon=True).start()


if TELEGRAM_BOT_TOKEN and TELEGRAM_NOTIFY_CHAT_ID:
    logging.info("Telegram notifications enabled for events: %s", TELEGRAM_NOTIFY_EVENTS)
else:
    logging.info("Telegram notifications disabled (TELEGRAM_BOT_TOKEN / TELEGRAM_NOTIFY_CHAT_ID not set)")


def _require_garage_config():
    """Refuse to start with STORAGE_BACKEND=garage and any required Garage
    variable unset. Logs which *names* are missing -- never values -- and
    raises. A half-configured object store that silently fell back to
    local disk is how a catalog ends up pointing at files nobody wrote.
    """
    required = {
        "GARAGE_S3_ENDPOINT": GARAGE_S3_ENDPOINT,
        "GARAGE_S3_ACCESS_KEY_ID": GARAGE_S3_ACCESS_KEY_ID,
        "GARAGE_S3_SECRET_ACCESS_KEY": GARAGE_S3_SECRET_ACCESS_KEY,
        "GARAGE_BUCKET": GARAGE_BUCKET,
        "GARAGE_PUBLIC_BASE_URL": GARAGE_PUBLIC_BASE_URL,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        message = (
            "STORAGE_BACKEND=garage requires the following environment "
            f"variable(s), which are not set: {', '.join(missing)}"
        )
        logging.error(message)
        raise RuntimeError(message)


def _classify_storage_error(e):
    """Classify a ClientError raised during a storage self-test probe
    (plan 050). A 403 -- by HTTP status or an AccessDenied/Forbidden
    error code -- is reported as "forbidden" so a permission problem is
    never confused with a plain "error". Returns (status, detail), where
    detail is the S3 error code.
    """
    status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = e.response.get("Error", {}).get("Code", "unknown")
    if status == 403 or code in ("403", "AccessDenied", "Forbidden"):
        return "forbidden", code
    return "error", code


def _move_file(src, dest):
    """os.replace that also works across mounts.

    compose.yml mounts ./data/ipas, ./data/icons and ./data/fdroid as their
    own bind mounts beside ./data, and rename(2) never crosses a mount, so a
    temp file in data/uploads cannot be renamed into them (EXDEV): every
    IPA/icon download-from-URL failed on the default deployment. Fall back
    to copy + fsync + atomic replace inside the destination directory."""
    try:
        os.replace(src, dest)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    fd, staged = tempfile.mkstemp(dir=os.path.dirname(dest) or ".", prefix=f".{os.path.basename(dest)}.", suffix=".part")
    os.close(fd)
    try:
        shutil.copyfile(src, staged)
        with open(staged, "rb") as copied:
            os.fsync(copied.fileno())
        os.replace(staged, dest)
        staged = None
    finally:
        if staged and os.path.exists(staged):
            try:
                os.remove(staged)
            except OSError:
                pass
    os.remove(src)


class LocalIpaStorage:
    """IPA storage on local disk -- today's behaviour, unchanged.

    Reproduces Plan 007's guarantees exactly: the bundle directory is
    created only on write, a partial file is cleaned up if the write
    raises, and the size returned is None (not 0) when it cannot be
    determined. public_url() always returns None, which is what tells
    serve_ipa to fall back to send_file instead of redirecting.
    """

    def _path(self, bundle_id, version):
        bundle_folder = os.path.join(IPA_FOLDER, secure_filename(bundle_id))
        filename = f"{secure_filename(version)}.ipa"
        return os.path.join(bundle_folder, filename)

    def put(self, src, bundle_id, version):
        """Write src -- a path, or a file-like object with .save() (a
        Werkzeug FileStorage) -- to the final local path.

        A plain path is assumed to already be fully and successfully
        written (the "local temp file" staging step callers do before
        calling put()), so it is moved into place with os.replace(),
        which is atomic on the same filesystem: the destination either
        keeps its old content or gets the complete new content, never a
        partial write. A FileStorage is saved directly, since there is
        nothing at the destination yet to protect from a partial write in
        the common "add a new version" case.

        Returns the resulting size, or None on failure.
        """
        filepath = self._path(bundle_id, version)
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            if hasattr(src, "save"):
                src.save(filepath)
            else:
                _move_file(src, filepath)
            file_size = get_file_size(filepath)
            logging.info(f"Saved IPA file: {filepath} ({file_size} bytes)")
            return file_size
        except Exception as e:
            logging.error(f"Error saving IPA file: {str(e)}")
            if hasattr(src, "save") and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass
            return None

    def delete(self, bundle_id, version):
        filepath = self._path(bundle_id, version)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                logging.info(f"Deleted IPA file: {filepath}")
                # Try to remove bundle folder if empty
                bundle_folder = os.path.dirname(filepath)
                try:
                    if not os.listdir(bundle_folder):
                        os.rmdir(bundle_folder)
                except OSError as e:
                    logging.warning(f"Could not remove empty bundle folder {bundle_folder}: {e}")
                return True
            return False
        except Exception as e:
            logging.error(f"Error deleting IPA file: {str(e)}")
            return False

    def exists(self, bundle_id, version):
        return os.path.exists(self._path(bundle_id, version))

    def selftest(self):
        """Write, read, then delete a probe file under IPA_FOLDER to
        confirm the mount is writable and readable (plan 050). All
        capabilities are "ok" unless an OSError occurs, in which case
        that capability is "error" with the errno in detail. The probe
        file is always deleted, even if the read step fails. Never
        raises.
        """
        result = {"backend": "local", "write": "error", "read": "error", "delete": "error", "detail": None}

        def note(detail):
            if detail and result["detail"] is None:
                result["detail"] = detail

        probe_dir = os.path.join(IPA_FOLDER, "__selftest__")
        probe_path = os.path.join(probe_dir, "probe.tmp")
        wrote = False
        try:
            os.makedirs(probe_dir, exist_ok=True)
            with open(probe_path, "wb") as f:
                f.write(b"feather-selftest")
            result["write"] = "ok"
            wrote = True
        except OSError as e:
            note(f"errno {e.errno}: {e.strerror}")

        if wrote:
            try:
                with open(probe_path, "rb") as f:
                    f.read()
                result["read"] = "ok"
            except OSError as e:
                note(f"errno {e.errno}: {e.strerror}")

        try:
            if os.path.exists(probe_path):
                os.remove(probe_path)
            if os.path.isdir(probe_dir) and not os.listdir(probe_dir):
                os.rmdir(probe_dir)
            result["delete"] = "ok"
        except OSError as e:
            result["delete"] = "error"
            note(f"errno {e.errno}: {e.strerror}")

        return result

    def public_url(self, bundle_id, version):
        return None


class GarageIpaStorage:
    """IPA storage on the self-hosted Garage S3-compatible object store.

    public_url() returns the object's Garage web-endpoint URL, which is
    what lets serve_ipa redirect (302) instead of proxying bytes through
    Flask -- see plans/011-garage-s3-ipa-storage.md for the design.
    """

    def __init__(self):
        _require_garage_config()
        self._client = boto3.client(
            "s3",
            endpoint_url=GARAGE_S3_ENDPOINT,
            region_name=GARAGE_S3_REGION,
            aws_access_key_id=GARAGE_S3_ACCESS_KEY_ID,
            aws_secret_access_key=GARAGE_S3_SECRET_ACCESS_KEY,
        )

    def _key(self, bundle_id, version):
        return f"{GARAGE_KEY_PREFIX}/{secure_filename(bundle_id)}/{secure_filename(version)}.ipa"

    @staticmethod
    def _remaining_size(src):
        """Return remaining bytes without consuming a file-like source.

        Duplicated from GarageIconStorage._remaining_size (plan 065): a
        tiny, dependency-free helper, kept identical on both classes
        rather than hoisted, so this class's behaviour never depends on
        GarageIconStorage's internals.
        """
        if isinstance(src, (str, bytes, os.PathLike)):
            return os.path.getsize(src)
        stream = getattr(src, "stream", src)
        try:
            position = stream.tell()
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            stream.seek(position, os.SEEK_SET)
            return end - position
        except (AttributeError, OSError, ValueError):
            content_length = getattr(src, "content_length", None)
            return content_length if content_length and content_length >= 0 else None

    def put(self, src, bundle_id, version):
        """Upload src -- a path, or a file-like object with .save() (a
        Werkzeug FileStorage) -- to the object's final key.

        Uses upload_file/upload_fileobj (not put_object) so large objects
        multipart automatically instead of being buffered whole. Returns
        the uploaded size (from a post-upload head_object), or None on
        failure. Never logs the secret key or response headers.
        """
        key = self._key(bundle_id, version)
        extra_args = {"ContentType": "application/octet-stream"}
        try:
            expected_size = self._remaining_size(src)
        except OSError:
            expected_size = None
        try:
            if hasattr(src, "save"):
                self._client.upload_fileobj(src, GARAGE_BUCKET, key, ExtraArgs=extra_args)
            else:
                self._client.upload_file(src, GARAGE_BUCKET, key, ExtraArgs=extra_args)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "unknown")
            logging.error(f"Error uploading IPA to Garage ({key}): {code}")
            return None
        except Exception as e:
            logging.error(f"Error uploading IPA to Garage ({key}): {str(e)}")
            return None

        try:
            head = self._client.head_object(Bucket=GARAGE_BUCKET, Key=key)
            return head.get("ContentLength")
        except ClientError as e:
            status, code = _classify_storage_error(e)
            if status == "forbidden" and expected_size is not None:
                logging.warning(
                    f"Uploaded IPA but cannot verify it ({key}): {code} -- "
                    f"treating upload as successful (size {expected_size} from source; "
                    f"grant the key read to re-enable verification)"
                )
                return expected_size
            logging.error(f"Uploaded IPA but could not verify it ({key}): {code}")
            return None

    def delete(self, bundle_id, version):
        key = self._key(bundle_id, version)
        existed = self.exists(bundle_id, version)
        try:
            self._client.delete_object(Bucket=GARAGE_BUCKET, Key=key)
            if existed:
                logging.info(f"Deleted IPA object: {key}")
            return existed
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "unknown")
            logging.error(f"Error deleting IPA object ({key}): {code}")
            return False

    def exists(self, bundle_id, version):
        key = self._key(bundle_id, version)
        try:
            self._client.head_object(Bucket=GARAGE_BUCKET, Key=key)
            return True
        except ClientError as e:
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = e.response.get("Error", {}).get("Code", "unknown")
            if status == 404 or code in ("404", "NoSuchKey"):
                return False
            logging.error(f"Error checking IPA existence ({key}): {code}")
            return False

    def selftest(self):
        """Write, read, then delete a dedicated probe object -- never a
        real IPA key -- and report each capability (plan 050). A 403 is
        reported as "forbidden", distinct from a plain "error" or "ok",
        so a permission problem is never mistaken for the object simply
        being missing. The probe is always deleted, even if the read
        step fails. Never raises.
        """
        probe_key = self._key("__selftest__", "probe")
        result = {"backend": "garage", "write": "error", "read": "error", "delete": "error", "detail": None}

        def note(detail):
            if detail and result["detail"] is None:
                result["detail"] = detail

        wrote = False
        try:
            self._client.put_object(Bucket=GARAGE_BUCKET, Key=probe_key, Body=b"feather-selftest")
            result["write"] = "ok"
            wrote = True
        except ClientError as e:
            status, detail = _classify_storage_error(e)
            result["write"] = status
            note(detail)
        except Exception as e:
            result["write"] = "error"
            note(str(e))

        try:
            if wrote:
                self._client.head_object(Bucket=GARAGE_BUCKET, Key=probe_key)
                result["read"] = "ok"
        except ClientError as e:
            status, detail = _classify_storage_error(e)
            result["read"] = status
            note(detail)
        except Exception as e:
            result["read"] = "error"
            note(str(e))
        finally:
            try:
                self._client.delete_object(Bucket=GARAGE_BUCKET, Key=probe_key)
                result["delete"] = "ok"
            except ClientError as e:
                status, detail = _classify_storage_error(e)
                result["delete"] = status
                note(detail)
            except Exception as e:
                result["delete"] = "error"
                note(str(e))

        return result

    def public_url(self, bundle_id, version):
        key = self._key(bundle_id, version)
        return f"{GARAGE_PUBLIC_BASE_URL.rstrip('/')}/{key}"


class GarageApkMirror:
    """Garage copy of the F-Droid repo's APKs, keyed apks/<file>.apk.

    The local file in FDROID_REPO_DIR stays the source of truth (the
    sidecar signs from it); this only moves download bandwidth to the
    bucket. Every method is best effort and never raises: a missing or
    failed object just means the download is served from local disk.
    """

    CONTENT_TYPE = "application/vnd.android.package-archive"

    def __init__(self):
        _require_garage_config()
        self._client = boto3.client(
            "s3",
            endpoint_url=GARAGE_S3_ENDPOINT,
            region_name=GARAGE_S3_REGION,
            aws_access_key_id=GARAGE_S3_ACCESS_KEY_ID,
            aws_secret_access_key=GARAGE_S3_SECRET_ACCESS_KEY,
        )

    @staticmethod
    def key(filename):
        return f"apks/{secure_filename(filename)}"

    def put(self, path):
        """Upload a local APK; True only if the object's size matches."""
        key = self.key(os.path.basename(path))
        try:
            self._client.upload_file(path, GARAGE_BUCKET, key, ExtraArgs={"ContentType": self.CONTENT_TYPE})
            head = self._client.head_object(Bucket=GARAGE_BUCKET, Key=key)
            if head.get("ContentLength") != os.path.getsize(path):
                logging.error("Garage APK %s has the wrong size after upload", key)
                return False
            return True
        except ClientError as e:
            logging.error("Error uploading APK to Garage (%s): %s", key, e.response.get("Error", {}).get("Code", "unknown"))
        except Exception as e:
            logging.error("Error uploading APK to Garage (%s): %s", key, e)
        return False

    def exists(self, filename):
        try:
            self._client.head_object(Bucket=GARAGE_BUCKET, Key=self.key(filename))
            return True
        except Exception:
            return False

    def delete(self, filename):
        try:
            self._client.delete_object(Bucket=GARAGE_BUCKET, Key=self.key(filename))
            return True
        except Exception as e:
            logging.error("Error deleting APK object %s: %s", self.key(filename), e)
            return False

    def public_url(self, filename):
        return f"{GARAGE_PUBLIC_BASE_URL.rstrip('/')}/{self.key(filename)}"


def garage_icon_key(bundle_id, ext):
    """Return the stable Garage key for an app-owned icon."""
    return f"icons/{secure_filename(bundle_id)}/icon.{secure_filename(ext)}"


class LocalIconStorage:
    """Atomic app-icon storage on the local filesystem."""

    def _path(self, bundle_id, ext):
        return os.path.join(ICON_FOLDER, secure_filename(bundle_id), f"icon.{secure_filename(ext)}")

    def put(self, src, bundle_id, ext):
        destination = self._path(bundle_id, ext)
        temporary = None
        try:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=os.path.dirname(destination), prefix=".icon-")
            os.close(fd)
            if hasattr(src, "save"):
                src.save(temporary)
            else:
                _move_file(src, temporary)
            os.replace(temporary, destination)
            temporary = None
            return True
        except Exception as e:
            logging.error(f"Error saving icon file: {str(e)}")
            if temporary and os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass
            return False

    def delete(self, bundle_id):
        deleted = False
        for ext in ALLOWED_ICON_EXTENSIONS:
            path = self._path(bundle_id, ext)
            try:
                if os.path.exists(path):
                    os.remove(path)
                    deleted = True
            except OSError as e:
                logging.warning(f"Could not delete icon file {path}: {e}")
        bundle_folder = os.path.dirname(self._path(bundle_id, "png"))
        try:
            if os.path.isdir(bundle_folder) and not os.listdir(bundle_folder):
                os.rmdir(bundle_folder)
        except OSError as e:
            logging.warning(f"Could not remove empty icon folder {bundle_folder}: {e}")
        return deleted

    def cleanup_variants(self, bundle_id, keep_ext):
        for ext in ALLOWED_ICON_EXTENSIONS - {secure_filename(keep_ext)}:
            path = self._path(bundle_id, ext)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                logging.warning(f"Could not clean stale icon {path}: {e}")

    def exists(self, bundle_id, ext):
        return os.path.exists(self._path(bundle_id, ext))

    def selftest(self):
        """Write, read, then delete a probe file under ICON_FOLDER to
        confirm the mount is writable and readable (plan 050). All
        capabilities are "ok" unless an OSError occurs, in which case
        that capability is "error" with the errno in detail. The probe
        file is always deleted, even if the read step fails. Never
        raises.
        """
        result = {"backend": "local", "write": "error", "read": "error", "delete": "error", "detail": None}

        def note(detail):
            if detail and result["detail"] is None:
                result["detail"] = detail

        probe_dir = os.path.join(ICON_FOLDER, "__selftest__")
        probe_path = os.path.join(probe_dir, "probe.tmp")
        wrote = False
        try:
            os.makedirs(probe_dir, exist_ok=True)
            with open(probe_path, "wb") as f:
                f.write(b"feather-selftest")
            result["write"] = "ok"
            wrote = True
        except OSError as e:
            note(f"errno {e.errno}: {e.strerror}")

        if wrote:
            try:
                with open(probe_path, "rb") as f:
                    f.read()
                result["read"] = "ok"
            except OSError as e:
                note(f"errno {e.errno}: {e.strerror}")

        try:
            if os.path.exists(probe_path):
                os.remove(probe_path)
            if os.path.isdir(probe_dir) and not os.listdir(probe_dir):
                os.rmdir(probe_dir)
            result["delete"] = "ok"
        except OSError as e:
            result["delete"] = "error"
            note(f"errno {e.errno}: {e.strerror}")

        return result

    def public_url(self, bundle_id, ext):
        return None


class GarageIconStorage:
    """App-icon storage in Garage, using the literal ``icons/`` prefix."""

    def __init__(self):
        _require_garage_config()
        self._client = boto3.client(
            "s3",
            endpoint_url=GARAGE_S3_ENDPOINT,
            region_name=GARAGE_S3_REGION,
            aws_access_key_id=GARAGE_S3_ACCESS_KEY_ID,
            aws_secret_access_key=GARAGE_S3_SECRET_ACCESS_KEY,
        )

    def _key(self, bundle_id, ext):
        return garage_icon_key(bundle_id, ext)

    @staticmethod
    def _remaining_size(src):
        """Return remaining bytes without consuming a file-like source."""
        if isinstance(src, (str, bytes, os.PathLike)):
            return os.path.getsize(src)
        stream = getattr(src, "stream", src)
        try:
            position = stream.tell()
            stream.seek(0, os.SEEK_END)
            end = stream.tell()
            stream.seek(position, os.SEEK_SET)
            return end - position
        except (AttributeError, OSError, ValueError):
            content_length = getattr(src, "content_length", None)
            return content_length if content_length and content_length >= 0 else None

    def put(self, src, bundle_id, ext):
        key = self._key(bundle_id, ext)
        extra_args = {"ContentType": ICON_MIME_TYPES[secure_filename(ext)]}
        try:
            expected_size = self._remaining_size(src)
        except OSError as e:
            logging.error(f"Could not determine icon size ({key}): {e}")
            return False
        if expected_size is None:
            logging.error(f"Could not determine icon size ({key})")
            return False
        try:
            if hasattr(src, "save") or hasattr(src, "read"):
                self._client.upload_fileobj(src, GARAGE_BUCKET, key, ExtraArgs=extra_args)
            else:
                self._client.upload_file(src, GARAGE_BUCKET, key, ExtraArgs=extra_args)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "unknown")
            logging.error(f"Error uploading icon to Garage ({key}): {code}")
            return False
        except Exception as e:
            logging.error(f"Error uploading icon to Garage ({key}): {str(e)}")
            return False
        try:
            head = self._client.head_object(Bucket=GARAGE_BUCKET, Key=key)
            actual_size = head.get("ContentLength")
            if actual_size != expected_size:
                logging.error(
                    f"Garage icon size mismatch ({key}): expected {expected_size}, got {actual_size}"
                )
                return False
            return True
        except ClientError as e:
            status, code = _classify_storage_error(e)
            if status == "forbidden":
                # The upload above already succeeded; we simply lack read/head
                # permission to verify size. Do NOT fail the write on a
                # permission-denied verify (that breaks a write-only key).
                logging.warning(
                    f"Uploaded icon but cannot verify it ({key}): {code} -- "
                    f"treating upload as successful (grant the key read to re-enable verification)"
                )
                return True
            logging.error(f"Uploaded icon but could not verify it ({key}): {code}")
            return False

    def delete(self, bundle_id):
        deleted = False
        for ext in ALLOWED_ICON_EXTENSIONS:
            key = self._key(bundle_id, ext)
            existed = self.exists(bundle_id, ext)
            try:
                self._client.delete_object(Bucket=GARAGE_BUCKET, Key=key)
                deleted = deleted or existed
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "unknown")
                logging.error(f"Error deleting icon object ({key}): {code}")
        return deleted

    def cleanup_variants(self, bundle_id, keep_ext):
        for ext in ALLOWED_ICON_EXTENSIONS - {secure_filename(keep_ext)}:
            key = self._key(bundle_id, ext)
            try:
                if self.exists(bundle_id, ext):
                    self._client.delete_object(Bucket=GARAGE_BUCKET, Key=key)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "unknown")
                logging.warning(f"Could not clean stale icon object ({key}): {code}")

    def exists(self, bundle_id, ext):
        key = self._key(bundle_id, ext)
        try:
            self._client.head_object(Bucket=GARAGE_BUCKET, Key=key)
            return True
        except ClientError as e:
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = e.response.get("Error", {}).get("Code", "unknown")
            if status == 404 or code in ("404", "NoSuchKey"):
                return False
            logging.error(f"Error checking icon existence ({key}): {code}")
            return False

    def selftest(self):
        """Write, read, then delete a dedicated probe object -- never a
        real icon key -- and report each capability (plan 050). A 403 is
        reported as "forbidden", distinct from a plain "error" or "ok",
        so a permission problem is never mistaken for the icon simply
        being missing. The probe is always deleted, even if the read
        step fails. Never raises.
        """
        probe_key = self._key("__selftest__", "probe")
        result = {"backend": "garage", "write": "error", "read": "error", "delete": "error", "detail": None}

        def note(detail):
            if detail and result["detail"] is None:
                result["detail"] = detail

        wrote = False
        try:
            self._client.put_object(Bucket=GARAGE_BUCKET, Key=probe_key, Body=b"feather-selftest")
            result["write"] = "ok"
            wrote = True
        except ClientError as e:
            status, detail = _classify_storage_error(e)
            result["write"] = status
            note(detail)
        except Exception as e:
            result["write"] = "error"
            note(str(e))

        try:
            if wrote:
                self._client.head_object(Bucket=GARAGE_BUCKET, Key=probe_key)
                result["read"] = "ok"
        except ClientError as e:
            status, detail = _classify_storage_error(e)
            result["read"] = status
            note(detail)
        except Exception as e:
            result["read"] = "error"
            note(str(e))
        finally:
            try:
                self._client.delete_object(Bucket=GARAGE_BUCKET, Key=probe_key)
                result["delete"] = "ok"
            except ClientError as e:
                status, detail = _classify_storage_error(e)
                result["delete"] = status
                note(detail)
            except Exception as e:
                result["delete"] = "error"
                note(str(e))

        return result

    def public_url(self, bundle_id, ext):
        return f"{GARAGE_PUBLIC_BASE_URL.rstrip('/')}/{self._key(bundle_id, ext)}"


_NEWS_KEYS = {
    'title', 'identifier', 'caption', 'date', 'tintColor', 'imageURL', 'notify',
    'url', 'appID',
}


def _editorial_text(value, field, limit, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > limit or any(ord(ch) < 32 and ch not in ('\n', '\t') for ch in value):
        raise ValueError(f"{field} is invalid or too long")
    return value or None


def _normalize_featured_apps(payload, source_data):
    if not isinstance(payload, dict) or not isinstance(payload.get('bundleIdentifiers'), list):
        raise ValueError("bundleIdentifiers must be an array")
    values = payload['bundleIdentifiers']
    if len(values) > 5 or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("Choose zero to five bundle identifiers")
    if len(set(values)) != len(values):
        raise ValueError("Featured apps cannot contain duplicates")
    known = {app.get('bundleIdentifier') for app in source_data.get('apps', []) if isinstance(app, dict)}
    if any(value not in known for value in values):
        raise ValueError("Featured apps must reference existing apps")
    return list(values)


def _normalize_news_item(raw, source_data):
    if not isinstance(raw, dict):
        raise ValueError("News item must be an object")
    if set(raw) - _NEWS_KEYS:
        raise ValueError("News item contains unsupported fields")
    item = {
        'title': _editorial_text(raw.get('title'), 'title', 200, True),
        'identifier': _editorial_text(raw.get('identifier'), 'identifier', 128, True),
        'caption': _editorial_text(raw.get('caption'), 'caption', 1000, True),
    }
    if not re.fullmatch(r'[A-Za-z0-9._-]+', item['identifier']):
        raise ValueError("identifier may contain only letters, digits, dot, underscore, and hyphen")
    date_raw = _editorial_text(raw.get('date'), 'date', 64, True)
    try:
        parsed = datetime.fromisoformat(date_raw[:-1] + '+00:00' if date_raw.endswith('Z') else date_raw)
    except ValueError:
        raise ValueError("date must be a timezone-aware ISO-8601 value")
    if parsed.tzinfo is None:
        raise ValueError("date must include a timezone")
    item['date'] = parsed.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    color = raw.get('tintColor')
    if color not in (None, ''):
        if not isinstance(color, str) or not re.fullmatch(r'#?[0-9A-Fa-f]{6}', color.strip()):
            raise ValueError("tintColor must be a six-digit hex color")
        item['tintColor'] = '#' + color.strip().lstrip('#').upper()
    for field in ('imageURL', 'url'):
        value = raw.get(field)
        if value in (None, ''):
            continue
        value = _editorial_text(value, field, 2048, False)
        parsed_url = urlparse(value)
        if parsed_url.scheme not in ('http', 'https') or not parsed_url.hostname or parsed_url.username or parsed_url.password:
            raise ValueError(f"{field} must be an absolute HTTP(S) URL without credentials")
        item[field] = value
    if 'notify' in raw:
        if not isinstance(raw['notify'], bool):
            raise ValueError("notify must be a boolean")
        item['notify'] = raw['notify']
    app_id = raw.get('appID')
    if app_id not in (None, ''):
        app_id = _editorial_text(app_id, 'appID', 256, False)
        known = {app.get('bundleIdentifier') for app in source_data.get('apps', []) if isinstance(app, dict)}
        if app_id not in known:
            raise ValueError("appID must reference an existing app")
        item['appID'] = app_id
    return item


_VERSION_NUMBER_RE = re.compile(r"\d+(?:\.\d+)*")


def _version_key(version):
    """Numeric key for ordering versions, or None when there is no number."""
    match = _VERSION_NUMBER_RE.search(version or "") if isinstance(version, str) else None
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def _insert_version(versions, entry):
    """Insert a version entry so versions[0] stays the newest. Clients treat
    versions[0] as the latest build, so an older backport release imported
    after a newer one must not land there. Versions without a number keep
    the old "newest first by arrival" behaviour."""
    new = _version_key(entry.get("version"))
    if new is not None:
        for index, existing in enumerate(versions):
            current = _version_key(existing.get("version")) if isinstance(existing, dict) else None
            if current is None or current <= new:
                versions.insert(index, entry)
                return index
        versions.append(entry)
        return len(versions) - 1
    versions.insert(0, entry)
    return 0


def _is_http_url(value):
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


ALREADY_PRESENT = "already exists; nothing to add"


class SourceManager:
    """Manages the AltSource data and file operations"""
    
    def __init__(self, source_file):
        self.source_file = source_file
        # NOTE: this in-process lock only serializes writes within a single
        # Python process. app.run() runs one process (threaded=True), so
        # this holds today. If this app is ever served by gunicorn with
        # -w > 1 (or any multi-process WSGI setup), this lock silently stops
        # protecting anything -- it would need to become a cross-process
        # file lock instead.
        self._lock = threading.Lock()
        self.ensure_data_directory()
        self.initialize_source()

    def ensure_data_directory(self):
        """Ensure data and upload directories exist"""
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(IPA_FOLDER, exist_ok=True)
        os.makedirs(ICON_FOLDER, exist_ok=True)
        os.makedirs(BACKUP_FOLDER, exist_ok=True)
        logging.info("Data directories verified")
    
    def get_ipa_path(self, bundle_id, version):
        """Get the file path for an IPA file.

        Read-only: does not create the bundle subdirectory. Callers that
        write to this path are responsible for creating it first.
        """
        bundle_folder = os.path.join(IPA_FOLDER, secure_filename(bundle_id))
        filename = f"{secure_filename(version)}.ipa"
        return os.path.join(bundle_folder, filename)
    
    @staticmethod
    def _storage_conflict(source_data, bundle_id, version):
        """None, or why (bundle_id, version) cannot be stored safely.

        IPA paths, Garage keys, icon folders and download URLs are built with
        secure_filename(), which maps distinct strings to one name
        ('com.app' / 'com.app.', '1.2.3+4' / '1.2.34'). The catalogue compares
        raw strings, so a colliding ID or version looked new and overwrote
        another entry's binary."""
        bundle_key, version_key = secure_filename(bundle_id or ""), secure_filename(version or "")
        if not bundle_key or not version_key:
            return f"{bundle_id!r} {version!r} cannot be stored (empty file name after sanitising)"
        for app in source_data.get("apps", []):
            other = app.get("bundleIdentifier") or ""
            if other != bundle_id:
                if secure_filename(other) == bundle_key:
                    return f"Bundle identifier {bundle_id!r} collides with existing app {other!r} in storage"
                continue
            for entry in app.get("versions", []):
                existing = entry.get("version") if isinstance(entry, dict) else None
                if existing and existing != version and secure_filename(existing) == version_key:
                    return f"Version {version!r} collides with existing version {existing!r} in storage"
        return None

    def check_ipa(self, source, bundle_id, label):
        """Refuse anything that is not a real IPA before it is stored.

        `source` is a path or a seekable file object. Raises
        IpaInspectionError (not caught by the save/download helpers, so the
        route reports the reason). A different CFBundleIdentifier is only a
        warning: renamed patched builds are published under their own
        catalog ID on purpose, but devices install and update by the IPA's
        identifier, so the uploader should know. The warning is left in
        self.ipa_warning for the caller's success message; every caller
        holds self._lock.
        """
        self.ipa_warning = None
        self.extracted_icon = None
        inspection = inspect_ipa(source, label or "IPA")
        if getattr(self, "_icon_wanted", False):
            # The uploader gave no icon and the app has none: take it from
            # the IPA (Info.plist icon files / iTunesArtwork, CgBI decoded).
            self.extracted_icon = _extract_ipa_icon(source)
            if hasattr(source, "seek"):
                source.seek(0)
        if inspection.bundle_identifier != bundle_id:
            self.ipa_warning = (
                f"Note: this IPA's bundle identifier is {inspection.bundle_identifier}, "
                f"not {bundle_id}; devices install and update it as {inspection.bundle_identifier}."
            )
            logging.warning("IPA for %s declares bundle identifier %s", bundle_id, inspection.bundle_identifier)
        return inspection

    def save_ipa_file(self, file, bundle_id, version, dest_path=None):
        """Save uploaded IPA file via the configured storage backend
        (ipa_storage -- Plan 011).

        If dest_path is given, save there instead -- a local staging path
        used by update_version to fully capture the replacement IPA on
        local disk before it is committed to the storage backend (see
        update_version's docstring for why: the existing hosted object
        must never be touched until the replacement is known-complete).
        This staging write always happens on local disk, regardless of
        backend.

        Returns (truthy-on-success, size) to match the existing call
        sites, which only check truthiness of the first element.
        """
        filepath = None
        try:
            self.check_ipa(file.stream, bundle_id, file.filename)
            file.stream.seek(0)
            if dest_path:
                filepath = dest_path
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                file.save(filepath)
                file_size = get_file_size(filepath)
                logging.info(f"Staged IPA file: {filepath} ({file_size} bytes)")
                return filepath, file_size

            file_size = ipa_storage.put(file, bundle_id, version)
            if file_size is None:
                return None, 0
            return True, file_size
        except IpaInspectionError:
            raise
        except Exception as e:
            logging.error(f"Error saving IPA file: {str(e)}")
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass
            return None, 0

    def download_ipa_from_url(self, url, bundle_id, version, dest_path=None):
        """Download IPA file from URL and commit it via the configured
        storage backend (ipa_storage -- Plan 011).

        The bytes always land on local disk first -- that is where the
        size-limit enforcement and content-decoding below happen, and it
        doubles as the "local temp file" staging step Plan 011 stages
        through before committing to the backend. If dest_path is given,
        that local file *is* the destination (see save_ipa_file's
        docstring); otherwise it is a throwaway temp file that is removed
        once ipa_storage.put() has committed it (or failed).
        """
        filepath = None
        is_staging_write = dest_path is not None
        try:
            logging.info(f"Downloading IPA from: {url}")
            response = requests.get(url, stream=True, timeout=300)
            response.raise_for_status()

            if dest_path:
                filepath = dest_path
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
            else:
                os.makedirs(UPLOAD_FOLDER, exist_ok=True)
                fd, filepath = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=".ipa")
                os.close(fd)

            total = 0
            limit = app.config.get("MAX_CONTENT_LENGTH") or (2 * 1024 * 1024 * 1024)
            deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if time.monotonic() > deadline:
                        f.close()
                        os.remove(filepath)
                        filepath = None
                        raise ValueError(f"Download took longer than {DOWNLOAD_DEADLINE_SECONDS}s")
                    if total > limit:
                        f.close()
                        os.remove(filepath)
                        raise ValueError(f"Download exceeded size limit of {limit} bytes")
                    f.write(chunk)

            self.check_ipa(filepath, bundle_id, url.rsplit('/', 1)[-1].split('?')[0])

            if is_staging_write:
                file_size = get_file_size(filepath)
                logging.info(f"Downloaded IPA file: {filepath} ({file_size} bytes)")
                return filepath, file_size

            file_size = ipa_storage.put(filepath, bundle_id, version)
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass
            if file_size is None:
                return None, 0
            return True, file_size
        except IpaInspectionError:
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass
            raise
        except Exception as e:
            logging.error(f"Error downloading IPA file: {str(e)}")
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass
            return None, 0

    def delete_ipa_file(self, bundle_id, version):
        """Delete IPA file for a version via the configured storage
        backend (ipa_storage -- Plan 011)."""
        return ipa_storage.delete(bundle_id, version)
    
    def get_local_ipa_url(self, bundle_id, version, base_url=None):
        """Get the URL path for serving a local IPA file"""
        filename = f"{secure_filename(version)}.ipa"
        path = f"/ipas/{secure_filename(bundle_id)}/{filename}"
        if base_url:
            return f"{base_url.rstrip('/')}{path}"
        return path
    
    def save_icon_file(self, file, bundle_id):
        """Save uploaded icon file and return its stored extension."""
        try:
            ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'png'
            if ext not in ALLOWED_ICON_EXTENSIONS:
                return None
            if icon_storage.put(file, bundle_id, ext):
                logging.info(f"Saved icon file for {bundle_id} ({ext})")
                return ext
            return None
        except Exception as e:
            logging.error(f"Error saving icon file: {str(e)}")
            return None
    
    def download_icon_from_url(self, url, bundle_id):
        """Download icon file from URL and save it locally"""
        filepath = None
        try:
            logging.info(f"Downloading icon from: {url}")
            response = requests.get(url, stream=True, timeout=30)
            response.raise_for_status()
            
            # Determine file extension from content type or URL
            content_type = response.headers.get('content-type', '')
            ext = 'png'
            if 'jpeg' in content_type or 'jpg' in content_type:
                ext = 'jpg'
            elif 'webp' in content_type:
                ext = 'webp'
            elif 'gif' in content_type:
                ext = 'gif'
            elif url.lower().endswith(('.jpg', '.jpeg', '.webp', '.gif')):
                ext = url.lower().rsplit('.', 1)[1]
            
            fd, filepath = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=f".{ext}")
            os.close(fd)

            total = 0
            limit = app.config.get("MAX_CONTENT_LENGTH") or (2 * 1024 * 1024 * 1024)
            deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if time.monotonic() > deadline:
                        f.close()
                        os.remove(filepath)
                        filepath = None
                        raise ValueError(f"Download took longer than {DOWNLOAD_DEADLINE_SECONDS}s")
                    if total > limit:
                        f.close()
                        os.remove(filepath)
                        filepath = None
                        raise ValueError(f"Download exceeded size limit of {limit} bytes")
                    f.write(chunk)

            if icon_storage.put(filepath, bundle_id, ext):
                logging.info(f"Downloaded icon file for {bundle_id} ({ext})")
                return ext
            return None
        except Exception as e:
            logging.error(f"Error downloading icon file: {str(e)}")
            return None
        finally:
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass
    
    def delete_icon_file(self, bundle_id):
        """Delete all icon variants for an app via the configured backend."""
        return icon_storage.delete(bundle_id)
    
    def get_hosted_icon_url(self, bundle_id, ext, base_url=None):
        """Build the stable app-owned icon URL."""
        path = f"/icons/{secure_filename(bundle_id)}/icon.{secure_filename(ext)}"
        if base_url:
            return f"{base_url.rstrip('/')}{path}"
        return path
    
    def initialize_source(self):
        """Initialize source.json, or replace untouched template artwork."""
        if not os.path.exists(self.source_file):
            initial_source = {
                "name": "My AltStore Source",
                "subtitle": "Custom iOS app repository",
                "description": "A custom source for managing iOS apps with AltStore and Feather",
                "iconURL": SOURCE_ARTWORK_URL,
                "headerURL": SOURCE_ARTWORK_URL,
                "website": "https://example.com",
                "tintColor": "#4185A9",
                "featuredApps": [],
                "apps": [],
                "news": [],
                "nsfw": False
            }
            self.save_source(initial_source)
            logging.info("Initialized new source.json file")
            return

        source_data = self.load_source()
        if not isinstance(source_data, dict):
            return

        updated_fields = []
        for field, legacy_url in LEGACY_SOURCE_ARTWORK_URLS.items():
            if source_data.get(field) == legacy_url:
                source_data[field] = SOURCE_ARTWORK_URL
                updated_fields.append(field)

        if updated_fields and self.save_source(source_data):
            logging.info(
                "Replaced legacy source artwork: %s",
                ", ".join(updated_fields),
            )
    
    def load_source(self):
        """Load source data from JSON file"""
        try:
            with open(self.source_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error loading source: {str(e)}")
            return None
    
    def _backup_source(self):
        """Copy the current source.json into data/backups/ before it is
        replaced, then prune old backups down to the most recent 20.

        Best-effort only: a failure here is logged and swallowed. A failed
        backup is a nuisance; letting a backup failure block save_source
        (and therefore the live catalog write) would turn a nuisance into
        data loss.

        Filenames use a UTC timestamp plus an incrementing counter suffix
        (".001", ".002", ...) so that multiple backups taken within the
        same second -- which the plain one-second-resolution timestamp
        alone would collide on -- still get distinct filenames.
        """
        if not os.path.exists(self.source_file):
            return
        try:
            os.makedirs(BACKUP_FOLDER, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            counter = 0
            while True:
                suffix = f".{counter:03d}" if counter else ""
                backup_path = os.path.join(BACKUP_FOLDER, f"source-{timestamp}{suffix}.json")
                if not os.path.exists(backup_path):
                    break
                counter += 1
            with open(self.source_file, 'rb') as src, open(backup_path, 'wb') as dst:
                dst.write(src.read())

            # Prune to the most recent 20 backups.
            backups = sorted(
                (f for f in os.listdir(BACKUP_FOLDER) if f.startswith("source-") and f.endswith(".json")),
                reverse=True
            )
            for stale in backups[20:]:
                try:
                    os.remove(os.path.join(BACKUP_FOLDER, stale))
                except OSError as e:
                    logging.warning(f"Could not remove stale backup {stale}: {e}")
        except Exception as e:
            logging.error(f"Error backing up source: {str(e)}")

    def save_source(self, source_data):
        """Save source data to JSON file atomically.

        Writes to a temp file in the same directory then atomically renames
        it into position, so a crash mid-write can never truncate the live
        catalog. A timestamped copy of the previous catalog is kept in
        data/backups/.
        """
        tmp_path = None
        try:
            self._backup_source()
            directory = os.path.dirname(self.source_file) or "."
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".source-", suffix=".json.tmp")
            with os.fdopen(fd, 'w') as f:
                json.dump(source_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.source_file)
            return True
        except Exception as e:
            logging.error(f"Error saving source: {str(e)}")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False

    def restore_backup(self, filename, expected_sha256, allow_missing_artifacts=False):
        """Validate and atomically restore one exact catalog snapshot."""
        with self._lock:
            path = _resolve_backup_path(filename)
            actual_sha256 = _sha256_file(path)
            if not isinstance(expected_sha256, str) or not hmac.compare_digest(actual_sha256, expected_sha256):
                return False, "Snapshot changed since preview", None
            candidate, error = _load_catalog_snapshot(path)
            if error:
                return False, error, None
            current = self.load_source()
            issues = _scan_catalog_health(candidate)
            # Only real artifact errors block. The scan also emits severity
            # "info" notes under the same kinds (Garage cannot be ZIP-checked;
            # a downloadURL on another host), which made every restore on
            # Garage, or with an external URL, fail.
            blocking = [issue for issue in issues
                        if issue.get('severity') == 'error'
                        and issue.get('kind') in ('missing-ipa', 'not-a-zip', 'empty-or-nonpublic-downloadURL')]
            if blocking and not allow_missing_artifacts:
                return False, "Snapshot references missing or invalid IPA artifacts", None
            diff = _catalog_structural_diff(current or {}, candidate)
            if not self.save_source(candidate):
                return False, "Failed to restore catalog", None
            return True, "Catalog restored", {"diff": diff, "issues": issues}
    
    def get_current_dates(self):
        """Get properly formatted dates for Feather compatibility"""
        current_date = datetime.now(timezone.utc)
        return {
            'feather_date': current_date.strftime("%Y-%m-%d"),
            'version_date': current_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
    
    @staticmethod
    def _has_real_icon(app):
        icon = (app or {}).get('iconURL') if isinstance(app, dict) else None
        return isinstance(icon, str) and icon.strip() and icon.strip() != SOURCE_ARTWORK_URL

    def _store_extracted_icon(self, bundle_id, base_url):
        """Persist self.extracted_icon (a temp PNG) as the app's hosted icon.
        Best effort: returns (hosted_url, ext) or (None, None); always
        removes the temp file."""
        path = getattr(self, "extracted_icon", None)
        self.extracted_icon = None
        if not path:
            return None, None
        try:
            with open(path, "rb") as fh:
                ext = self.save_icon_file(FileStorage(stream=fh, filename="icon.png"), bundle_id)
            if ext:
                return self.get_hosted_icon_url(bundle_id, ext, base_url), ext
            return None, None
        except Exception as e:
            logging.warning("could not store icon extracted from IPA for %s: %s", bundle_id, e)
            return None, None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def backfill_icons(self, base_url=None):
        """Give every app without a real icon one extracted from its newest
        hosted IPA. Slow part (fetch + extract) runs outside the lock; the
        write re-checks under it so a concurrently set icon is never
        overwritten. Returns {"added": n, "missing": [bundle ids]}."""
        with self._lock:
            source_data = self.load_source() or {}
            targets = []
            for app_entry in source_data.get('apps', []):
                if not isinstance(app_entry, dict) or self._has_real_icon(app_entry):
                    continue
                versions = [v for v in app_entry.get('versions', []) if isinstance(v, dict) and v.get('version')]
                if versions:
                    targets.append((app_entry.get('bundleIdentifier'), versions[0]['version']))
        added, missing, found = 0, [], {}
        for bundle_id, version in targets:
            icon_path = None
            tmp_ipa = None
            try:
                if not bundle_id or not ipa_storage.exists(bundle_id, version):
                    missing.append(bundle_id)
                    continue
                url = ipa_storage.public_url(bundle_id, version)
                if url:
                    fd, tmp_ipa = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=".ipa")
                    os.close(fd)
                    with requests.get(url, stream=True, timeout=300) as resp:
                        resp.raise_for_status()
                        with open(tmp_ipa, "wb") as f:
                            for chunk in resp.iter_content(1 << 20):
                                f.write(chunk)
                    source = tmp_ipa
                else:
                    source = self.get_ipa_path(bundle_id, version)
                icon_path = _extract_ipa_icon(source)
                if not icon_path:
                    missing.append(bundle_id)
                    continue
                with open(icon_path, "rb") as fh:
                    ext = self.save_icon_file(FileStorage(stream=fh, filename="icon.png"), bundle_id)
                if ext:
                    found[bundle_id] = self.get_hosted_icon_url(bundle_id, ext, base_url)
                else:
                    missing.append(bundle_id)
            except Exception as e:
                logging.warning("icon backfill failed for %s: %s", bundle_id, e)
                missing.append(bundle_id)
            finally:
                for path in (icon_path, tmp_ipa):
                    if path and os.path.exists(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
        if found:
            with self._lock:
                source_data = self.load_source() or {}
                for app_entry in source_data.get('apps', []):
                    bid = app_entry.get('bundleIdentifier') if isinstance(app_entry, dict) else None
                    if bid in found and not self._has_real_icon(app_entry):
                        app_entry['iconURL'] = found[bid]
                        added += 1
                self.save_source(source_data)
        return {"added": added, "missing": missing}

    def add_app_manual(self, data, ipa_file=None, download_from_url=False, icon_file=None, download_icon_from_url=False, base_url=None):
        """Add app manually with provided data"""
        with self._lock:
            try:
                return self._add_app_manual_locked(data, ipa_file, download_from_url, icon_file, download_icon_from_url, base_url)
            finally:
                self._icon_wanted = False
                if getattr(self, "extracted_icon", None):
                    try:
                        os.remove(self.extracted_icon)
                    except OSError:
                        pass
                    self.extracted_icon = None

    def _add_app_manual_locked(self, data, ipa_file, download_from_url, icon_file, download_icon_from_url, base_url):
            self.ipa_warning = None
            source_data = self.load_source()
            if not source_data:
                return False, "Failed to load source data"
        
            dates = self.get_current_dates()
            bundle_id = data['bundleIdentifier']
            version = data['version']

            # Match add_version's idempotency rule before touching storage.
            # Manual form retries must not overwrite an existing binary or
            # append a duplicate catalog version.
            for existing_app in source_data.get('apps', []):
                if existing_app.get('bundleIdentifier') == bundle_id:
                    if any(
                        isinstance(existing_version, dict)
                        and existing_version.get('version') == version
                        for existing_version in existing_app.get('versions', [])
                    ):
                        return True, f"Version {version} {ALREADY_PRESENT}"

            conflict = self._storage_conflict(source_data, bundle_id, version)
            if conflict:
                return False, conflict

            download_url = data.get('downloadURL', '')
            icon_url = data.get('iconURL', '')
            if not (ipa_file and allowed_file(ipa_file.filename)) and not _is_http_url(download_url):
                # An empty or relative downloadURL makes AltStore reject the
                # whole source, not just this app.
                return False, "An IPA file or an http(s) download URL is required"

            existing_app = next((a for a in source_data.get('apps', []) if a.get('bundleIdentifier') == bundle_id), None)
            icon_supplied = bool(icon_file and allowed_icon_file(icon_file.filename)) or bool((icon_url or '').strip())
            # An icon the uploader supplied always wins over one extracted
            # from the IPA, for new and existing apps alike.
            if existing_app is not None:
                self._icon_wanted = not self._has_real_icon(existing_app) and not icon_supplied
            else:
                self._icon_wanted = not icon_supplied

            # Handle IPA file - upload, download, or use URL
            file_size = 0
            stored_ipa = False
            if ipa_file and allowed_file(ipa_file.filename):
                # Upload file
                filepath, file_size = self.save_ipa_file(ipa_file, bundle_id, version)
                if filepath:
                    stored_ipa = True
                    download_url = self.get_local_ipa_url(bundle_id, version, base_url)
                else:
                    return False, f"Failed to save uploaded IPA for {bundle_id} {version}"
            elif download_from_url and download_url:
                # Download from URL
                filepath, file_size = self.download_ipa_from_url(download_url, bundle_id, version)
                if filepath:
                    stored_ipa = True
                    download_url = self.get_local_ipa_url(bundle_id, version, base_url)
                else:
                    return False, f"Failed to download IPA from {download_url}"

            if file_size is None:
                # get_file_size could not stat the file even though the save/
                # download itself reported success. Record 0 rather than
                # publishing `null` into source.json -- clients expect an int.
                file_size = 0

            # Handle icon file - upload, download, or use URL
            stored_icon_ext = None
            user_icon_url = None
            if icon_file and allowed_icon_file(icon_file.filename):
                # Upload icon file
                stored_icon_ext = self.save_icon_file(icon_file, bundle_id)
                if stored_icon_ext:
                    icon_url = user_icon_url = self.get_hosted_icon_url(bundle_id, stored_icon_ext, base_url)
                else:
                    if stored_ipa:
                        self.delete_ipa_file(bundle_id, version)   # no orphan IPA without a catalog entry
                    return False, f"Failed to save uploaded icon for {bundle_id}"
            elif download_icon_from_url and icon_url:
                # Download icon from URL
                stored_icon_ext = self.download_icon_from_url(icon_url, bundle_id)
                if stored_icon_ext:
                    icon_url = user_icon_url = self.get_hosted_icon_url(bundle_id, stored_icon_ext, base_url)
                else:
                    if stored_ipa:
                        self.delete_ipa_file(bundle_id, version)   # no orphan IPA without a catalog entry
                    return False, f"Failed to download icon from {icon_url}"

            auto_icon_url, auto_icon_ext = self._store_extracted_icon(bundle_id, base_url)
            if auto_icon_url:
                icon_url = auto_icon_url
                stored_icon_ext = auto_icon_ext

            new_app = {
                "name": data['name'],
                "bundleIdentifier": bundle_id,
                "developerName": data['developerName'],
                "localizedDescription": data.get('localizedDescription', ''),
                "iconURL": icon_url,
                "addedDate": dates['feather_date'],
                "versions": [{
                    "version": version,
                    "buildVersion": str(data.get('buildVersion') or version),
                    "date": dates['version_date'],
                    "downloadURL": download_url,
                    "minOSVersion": data.get('minOSVersion') or '14.0',
                    "size": file_size
                }]
            }
            if data.get('versionDescription'):
                new_app['versions'][0]['localizedDescription'] = data['versionDescription']
            if data.get('subtitle'):
                new_app['subtitle'] = data['subtitle']
            permissions = data.get('appPermissions')
            if isinstance(permissions, dict) and permissions.get('privacy'):
                new_app['appPermissions'] = {
                    "entitlements": [],
                    "privacy": dict(permissions['privacy']),
                }
        
            # Check if app already exists
            existing_index = None
            for i, app in enumerate(source_data['apps']):
                if app['bundleIdentifier'] == bundle_id:
                    existing_index = i
                    break
        
            if existing_index is not None:
                # Update existing app with new version
                _insert_version(source_data['apps'][existing_index].setdefault('versions', []), new_app['versions'][0])
                source_data['apps'][existing_index]['addedDate'] = dates['feather_date']
                # Point the catalogue at whichever icon was just stored;
                # cleanup_variants below deletes every other extension, so
                # leaving the old iconURL here served a 404.
                if auto_icon_url or user_icon_url:
                    source_data['apps'][existing_index]['iconURL'] = auto_icon_url or user_icon_url
                logging.info(f"Updated app: {data['name']}")
            else:
                # Add new app
                source_data['apps'].append(new_app)
                logging.info(f"Added new app: {data['name']}")
        
            if self.save_source(source_data):
                if stored_icon_ext:
                    icon_storage.cleanup_variants(bundle_id, stored_icon_ext)
                return True, " ".join(filter(None, ["App added successfully", getattr(self, "ipa_warning", None)]))
            return False, "Failed to save source data"

    def delete_app(self, bundle_identifier):
        """Delete app by bundle identifier"""
        with self._lock:
            source_data = self.load_source()
            if not source_data:
                return False, "Failed to load source data"
        
            # Find app to delete IPA files
            app_to_delete = None
            for app in source_data['apps']:
                if app['bundleIdentifier'] == bundle_identifier:
                    app_to_delete = app
                    break
        
            initial_count = len(source_data['apps'])
            source_data['apps'] = [
                app for app in source_data['apps'] 
                if app['bundleIdentifier'] != bundle_identifier
            ]
        
            if len(source_data['apps']) < initial_count:
                # Drop editorial references to the deleted app: a dangling
                # featuredApps entry or news appID is published as-is and
                # makes the news item fail validation on its next save.
                featured = source_data.get('featuredApps')
                if isinstance(featured, list):
                    source_data['featuredApps'] = [b for b in featured if b != bundle_identifier]
                for item in source_data.get('news') or []:
                    if isinstance(item, dict) and item.get('appID') == bundle_identifier:
                        del item['appID']
                # Commit the catalogue first, then delete files (as
                # delete_version does): a failed save must not leave the live
                # catalogue pointing at IPAs that are already gone. A failed
                # file delete afterwards leaves only an orphan.
                success = self.save_source(source_data)
                if success:
                    if app_to_delete:
                        for version in app_to_delete.get('versions', []):
                            self.delete_ipa_file(bundle_identifier, version.get('version', ''))
                    self.delete_icon_file(bundle_identifier)
                return success, "App deleted successfully" if success else "Failed to save source after deletion"
            else:
                return False, "App not found"

    def delete_version(self, bundle_identifier, version):
        """Delete a single version entry (and its IPA) from an app."""
        with self._lock:
            source_data = self.load_source()
            if not source_data:
                return False, "Failed to load source data"

            app_index = None
            for i, app in enumerate(source_data['apps']):
                if app['bundleIdentifier'] == bundle_identifier:
                    app_index = i
                    break

            if app_index is None:
                return False, "App not found"

            app = source_data['apps'][app_index]
            version_index = None
            for i, v in enumerate(app.get('versions', [])):
                if v['version'] == version:
                    version_index = i
                    break

            if version_index is None:
                return False, "Version not found"

            if len(app.get('versions', [])) <= 1:
                return False, "Cannot delete the only version; delete the app instead"

            app['versions'].pop(version_index)

            success = self.save_source(source_data)
            if success:
                self.delete_ipa_file(bundle_identifier, version)
            return success, "Version deleted successfully" if success else "Failed to save source after deletion"

    def get_app(self, bundle_identifier):
        """Get app by bundle identifier"""
        source_data = self.load_source()
        if not source_data:
            return None
        
        for app in source_data.get('apps', []):
            if app['bundleIdentifier'] == bundle_identifier:
                return app
        return None
    
    def update_app(self, bundle_identifier, data, icon_file=None, download_icon_from_url=False, base_url=None):
        """Update app details"""
        with self._lock:
            source_data = self.load_source()
            if not source_data:
                return False, "Failed to load source data"
        
            app_index = None
            for i, app in enumerate(source_data['apps']):
                if app['bundleIdentifier'] == bundle_identifier:
                    app_index = i
                    break
        
            if app_index is None:
                return False, "App not found"
        
            # Update app fields
            app = source_data['apps'][app_index]
            updatable_fields = ['name', 'developerName', 'localizedDescription', 'subtitle', 'tintColor']
            for field in updatable_fields:
                if field in data and data[field] is not None:
                    app[field] = data[field]

            # Handle screenshotURLs separately: accept a JSON list or a
            # newline/comma-separated string; only overwrite when the key was
            # explicitly provided (mirrors the "only when present" convention
            # used for the scalar fields above).
            if 'screenshotURLs' in data and data['screenshotURLs'] is not None:
                raw_screenshots = data['screenshotURLs']
                if isinstance(raw_screenshots, list):
                    screenshot_items = raw_screenshots
                else:
                    normalized = raw_screenshots.replace(',', '\n')
                    screenshot_items = normalized.split('\n')
                app['screenshotURLs'] = [
                    item.strip() for item in screenshot_items if item and item.strip()
                ]

            # Handle icon file - upload, download, or use URL
            # Only update icon if a new one is provided
            icon_url = data.get('iconURL', '')
            icon_updated = False
            stored_icon_ext = None

            if icon_file and allowed_icon_file(icon_file.filename):
                # Upload icon file
                stored_icon_ext = self.save_icon_file(icon_file, bundle_identifier)
                if stored_icon_ext:
                    icon_url = self.get_hosted_icon_url(bundle_identifier, stored_icon_ext, base_url)
                    app['iconURL'] = icon_url
                    icon_updated = True
                else:
                    return False, f"Failed to save uploaded icon for {bundle_identifier}"
            elif download_icon_from_url and icon_url:
                # Download icon from URL
                stored_icon_ext = self.download_icon_from_url(icon_url, bundle_identifier)
                if stored_icon_ext:
                    icon_url = self.get_hosted_icon_url(bundle_identifier, stored_icon_ext, base_url)
                    app['iconURL'] = icon_url
                    icon_updated = True
                else:
                    return False, f"Failed to download icon from {icon_url}"
            elif icon_url and icon_url.strip():
                # Just update URL (only if not empty)
                app['iconURL'] = icon_url
                icon_updated = True
            # If icon_updated is False, preserve existing icon (don't modify app['iconURL'])
        
            success = self.save_source(source_data)
            if success and stored_icon_ext:
                icon_storage.cleanup_variants(bundle_identifier, stored_icon_ext)
            return success, "App updated successfully" if success else "Failed to update app"
    
    def add_version(self, bundle_identifier, version_data, ipa_file=None, download_from_url=False, base_url=None):
        """Add a new version to an existing app"""
        with self._lock:
            try:
                return self._add_version_locked(bundle_identifier, version_data, ipa_file, download_from_url, base_url)
            finally:
                self._icon_wanted = False
                if getattr(self, "extracted_icon", None):
                    try:
                        os.remove(self.extracted_icon)
                    except OSError:
                        pass
                    self.extracted_icon = None

    def _add_version_locked(self, bundle_identifier, version_data, ipa_file, download_from_url, base_url):
            self.ipa_warning = None
            source_data = self.load_source()
            if not source_data:
                return False, "Failed to load source data"
        
            app_index = None
            for i, app in enumerate(source_data['apps']):
                if app['bundleIdentifier'] == bundle_identifier:
                    app_index = i
                    break
        
            if app_index is None:
                return False, "App not found"
        
            dates = self.get_current_dates()
            app = source_data['apps'][app_index]
        
            # Ensure versions array exists
            if 'versions' not in app:
                app['versions'] = []
        
            # Get minOSVersion from provided data, or from existing version, or default
            min_os = version_data.get('minOSVersion')
            if not min_os and app['versions']:
                min_os = app['versions'][0].get('minOSVersion', '14.0')
            if not min_os:
                min_os = '14.0'
        
            version = version_data['version']
            if any(isinstance(v, dict) and v.get('version') == version for v in app.get('versions', [])):
                return True, f"Version {version} {ALREADY_PRESENT}"
            conflict = self._storage_conflict(source_data, bundle_identifier, version)
            if conflict:
                return False, conflict

            download_url = version_data.get('downloadURL', '')
            if not (ipa_file and allowed_file(ipa_file.filename)) and not _is_http_url(download_url):
                return False, "An IPA file or an http(s) download URL is required"
            file_size = version_data.get('size', 0)
            self._icon_wanted = not self._has_real_icon(app)
        
            # Handle IPA file - upload, download, or use URL
            if ipa_file and allowed_file(ipa_file.filename):
                # Upload file
                filepath, file_size = self.save_ipa_file(ipa_file, bundle_identifier, version)
                if filepath:
                    download_url = self.get_local_ipa_url(bundle_identifier, version, base_url)
                else:
                    return False, f"Failed to save uploaded IPA for {bundle_identifier} {version}"
            elif download_from_url and download_url:
                # Download from URL
                filepath, file_size = self.download_ipa_from_url(download_url, bundle_identifier, version)
                if filepath:
                    download_url = self.get_local_ipa_url(bundle_identifier, version, base_url)
                else:
                    return False, f"Failed to download IPA from {download_url}"

            if file_size is None:
                # Same "unknown size" rationale as add_app_manual above.
                file_size = 0

            new_version = {
                "version": version,
                "buildVersion": str(version_data.get('buildVersion') or version),
                "date": dates['version_date'],
                "downloadURL": download_url,
                "minOSVersion": min_os,
                "size": file_size
            }
            if version_data.get('localizedDescription'):
                new_version['localizedDescription'] = version_data['localizedDescription']
        
            _insert_version(app['versions'], new_version)
            app['addedDate'] = dates['feather_date']
            auto_icon_url, _ = self._store_extracted_icon(bundle_identifier, base_url)
            if auto_icon_url:
                app['iconURL'] = auto_icon_url
        
            success = self.save_source(source_data)
            if not success:
                return False, "Failed to add version"
            return True, " ".join(filter(None, ["Version added successfully", getattr(self, 'ipa_warning', None)]))
    
    def set_version_notes(self, bundle_identifier, version, text):
        """Set one published version's release notes (AltStore shows a
        version's localizedDescription as "what's new"). The binary and every
        other field are untouched."""
        with self._lock:
            source_data = self.load_source()
            app = next((a for a in (source_data or {}).get('apps', []) if a.get('bundleIdentifier') == bundle_identifier), None)
            if app is None:
                return False, "App not found"
            entry = next((v for v in app.get('versions', []) if isinstance(v, dict) and v.get('version') == version), None)
            if entry is None:
                return False, f"Version {version} not found"
            entry['localizedDescription'] = text
            return (True, "Release notes updated") if self.save_source(source_data) else (False, "Failed to save source data")

    def update_version(self, bundle_identifier, version, version_data, ipa_file=None, download_from_url=False, base_url=None):
        """Update a specific version of an app"""
        with self._lock:
            source_data = self.load_source()
            if not source_data:
                return False, "Failed to load source data"
        
            app_index = None
            for i, app in enumerate(source_data['apps']):
                if app['bundleIdentifier'] == bundle_identifier:
                    app_index = i
                    break
        
            if app_index is None:
                return False, "App not found"
        
            app = source_data['apps'][app_index]
            version_index = None
        
            for i, v in enumerate(app.get('versions', [])):
                if v['version'] == version:
                    version_index = i
                    break
        
            if version_index is None:
                return False, "Version not found"
        
            # Update version fields
            version_obj = app['versions'][version_index]
        
            # Handle IPA file update - upload, download, or use URL.
            #
            # Plan 011: fully and successfully capture the replacement to a
            # local temp file first, and only ever commit it to the storage
            # backend (ipa_storage.put(), which for the local backend is an
            # atomic os.replace() and for Garage is an upload straight to
            # the final key) once that capture has fully succeeded. A
            # failed fetch must never destroy a binary that is still being
            # served -- deleting the original before the replacement was
            # confirmed was the old (and dangerous) behaviour, and staging
            # locally first preserves that guarantee for either backend.
            if ipa_file and allowed_file(ipa_file.filename):
                tmp_path = self.get_ipa_path(bundle_identifier, version) + ".new"
                filepath, _ = self.save_ipa_file(ipa_file, bundle_identifier, version, dest_path=tmp_path)
                if filepath:
                    file_size = ipa_storage.put(filepath, bundle_identifier, version)
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                    if file_size is None:
                        return False, f"Failed to save uploaded IPA for {bundle_identifier} {version}; original file untouched"
                    version_obj['downloadURL'] = self.get_local_ipa_url(bundle_identifier, version, base_url)
                    version_obj['size'] = file_size
                else:
                    return False, f"Failed to save uploaded IPA for {bundle_identifier} {version}; original file untouched"
            elif download_from_url and version_data.get('downloadURL'):
                tmp_path = self.get_ipa_path(bundle_identifier, version) + ".new"
                filepath, _ = self.download_ipa_from_url(version_data['downloadURL'], bundle_identifier, version, dest_path=tmp_path)
                if filepath:
                    file_size = ipa_storage.put(filepath, bundle_identifier, version)
                    if os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except OSError:
                            pass
                    if file_size is None:
                        return False, f"Failed to download IPA from {version_data['downloadURL']}; original file untouched"
                    version_obj['downloadURL'] = self.get_local_ipa_url(bundle_identifier, version, base_url)
                    version_obj['size'] = file_size
                else:
                    return False, f"Failed to download IPA from {version_data['downloadURL']}; original file untouched"
            else:
                # Just update URL and other fields
                updatable_fields = ['downloadURL', 'minOSVersion']
                for field in updatable_fields:
                    if field in version_data and version_data[field] is not None:
                        version_obj[field] = version_data[field]
            
                # Update size if provided
                if 'size' in version_data and version_data['size'] is not None:
                    version_obj['size'] = version_data['size']
        
            success = self.save_source(source_data)
            return success, "Version updated successfully" if success else "Failed to update version"
    
    def update_source_info(self, data):
        """Update source metadata"""
        with self._lock:
            source_data = self.load_source()
            if not source_data:
                return False, "Failed to load source data"
        
            if not isinstance(data, dict):
                return False, "Expected a JSON object"
            updates = {}
            for key in ['name', 'subtitle', 'description', 'website', 'tintColor', 'iconURL']:
                value = data.get(key)
                if not value:
                    continue
                if not isinstance(value, str):
                    return False, f"{key} must be a string"
                value = value.strip()
                # Every client renders these, so hold them to the same rules
                # as news items: absolute http(s) URLs and a #RRGGBB colour.
                if key in ('website', 'iconURL'):
                    parsed = urlparse(value)
                    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                        return False, f"{key} must be an absolute http(s) URL"
                elif key == 'tintColor':
                    if not re.fullmatch(r'#?[0-9A-Fa-f]{6}', value):
                        return False, "tintColor must be a six-digit hex colour like #4185A9"
                    value = '#' + value.lstrip('#').upper()
                updates[key] = value
            source_data.update(updates)
        
            success = self.save_source(source_data)
            return success, "Source information updated successfully" if success else "Failed to update source information"

    def update_featured_apps(self, payload):
        with self._lock:
            source_data = self.load_source()
            if not source_data:
                return False, "Failed to load source data", None
            values = _normalize_featured_apps(payload, source_data)
            source_data['featuredApps'] = values
            success = self.save_source(source_data)
            return success, "Featured apps updated" if success else "Failed to update featured apps", values

    def upsert_news_item(self, payload):
        with self._lock:
            source_data = self.load_source()
            if not source_data:
                return False, "Failed to load source data", None
            item = _normalize_news_item(payload, source_data)
            news = source_data.get('news', [])
            if not isinstance(news, list):
                return False, "Stored news data is invalid", None
            news = [entry for entry in news if isinstance(entry, dict) and entry.get('identifier') != item['identifier']]
            news.append(item)
            news.sort(key=lambda entry: (entry.get('date', ''), entry.get('identifier', '')), reverse=True)
            source_data['news'] = news
            success = self.save_source(source_data)
            return success, "News item saved" if success else "Failed to save news item", item

    def delete_news_item(self, identifier):
        with self._lock:
            source_data = self.load_source()
            if not source_data:
                return False, "Failed to load source data"
            news = source_data.get('news', [])
            if not isinstance(news, list):
                return False, "Stored news data is invalid"
            kept = [entry for entry in news if not isinstance(entry, dict) or entry.get('identifier') != identifier]
            if len(kept) == len(news):
                return False, "News item not found"
            source_data['news'] = kept
            success = self.save_source(source_data)
            return success, "News item deleted" if success else "Failed to delete news item"


def _safe_base_url():
    """resolve_base_url() outside a request context raises RuntimeError.
    Callers that may run outside a request (module-scope smoke checks,
    scripts) use this instead and treat None as "unknown right now"."""
    try:
        return resolve_base_url()
    except RuntimeError:
        return None


class AndroidRepoManager:
    """Feather's side of the F-Droid repo. It owns repo/*.apk, metadata/*.yml
    and repo-config.json; the fdroid-index sidecar owns everything else
    under FDROID_DIR. Every mutation ends with request_update(), which is
    the only signal the sidecar listens for. Single-process lock, same
    caveat as SourceManager."""

    METADATA_KEYS = ("Name", "Summary", "Description", "AuthorName", "WebSite", "SourceCode", "License", "Categories")
    _LIMITS = {"Name": 50, "Summary": 80, "Description": 4000}

    def __init__(self):
        self._lock = threading.Lock()
        os.makedirs(FDROID_REPO_DIR, exist_ok=True)
        os.makedirs(FDROID_METADATA_DIR, exist_ok=True)

    # --- paths -------------------------------------------------------------
    @staticmethod
    def apk_filename(package, version_code):
        return f"{package}_{int(version_code)}.apk"     # matches fdroid's own --rename-apks convention

    def apk_path(self, package, version_code):
        if not ANDROID_PACKAGE_RE.match(package or ""):
            raise ValueError(f"Invalid package name: {package!r}")
        return os.path.join(FDROID_REPO_DIR, self.apk_filename(package, int(version_code)))

    def metadata_path(self, package):
        return os.path.join(FDROID_METADATA_DIR, f"{package}.yml")

    def icon_path(self, package):
        """fdroidserver publishes metadata/<package>/en-US/icon.png as the
        app's icon (index-v1 localized.en-US.icon, index-v2 icon)."""
        return os.path.join(FDROID_METADATA_DIR, package, "en-US", "icon.png")

    def _newest_apk(self, package):
        best = None
        if os.path.isdir(FDROID_REPO_DIR):
            for fn in os.listdir(FDROID_REPO_DIR):
                m = ANDROID_APK_FILENAME_RE.match(fn)
                if m and m.group(1) == package and (best is None or int(m.group(2)) > best[0]):
                    best = (int(m.group(2)), os.path.join(FDROID_REPO_DIR, fn))
        return best

    def mirror_apks(self):
        """Upload every local APK the Garage mirror lacks. Returns
        {"uploaded": n, "failed": [...]}; no-op without a mirror."""
        uploaded, failed = 0, []
        if apk_storage is None or not os.path.isdir(FDROID_REPO_DIR):
            return {"uploaded": 0, "failed": []}
        for fn in sorted(os.listdir(FDROID_REPO_DIR)):
            if not ANDROID_APK_FILENAME_RE.match(fn) or apk_storage.exists(fn):
                continue
            if apk_storage.put(os.path.join(FDROID_REPO_DIR, fn)):
                uploaded += 1
            else:
                failed.append(fn)
        return {"uploaded": uploaded, "failed": failed}

    def refresh_icon(self, package, apk_path):
        """Extract the launcher icon from apk_path into the metadata tree.
        fdroidserver's own extraction misses adaptive/WebP launcher icons,
        which leaves most modern apps iconless. Returns True if written."""
        png = extract_apk_icon(apk_path)
        if not png:
            return False
        dest = self.icon_path(package)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".icon-", suffix=".png.tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(png)
            os.replace(tmp, dest)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return True

    def backfill_icons(self):
        """Give every package without a published icon one from its newest
        APK. Returns {"added": n, "missing": [packages with no raster icon]}."""
        added, missing = 0, []
        packages = set()
        if os.path.isdir(FDROID_REPO_DIR):
            for fn in os.listdir(FDROID_REPO_DIR):
                m = ANDROID_APK_FILENAME_RE.match(fn)
                if m and ANDROID_PACKAGE_RE.match(m.group(1)):
                    packages.add(m.group(1))
        for package in sorted(packages):
            if os.path.exists(self.icon_path(package)):
                continue
            newest = self._newest_apk(package)
            try:
                if newest and self.refresh_icon(package, newest[1]):
                    added += 1
                else:
                    missing.append(package)
            except Exception as e:
                logging.warning("icon backfill failed for %s: %s", package, e)
                missing.append(package)
        if added:
            self.request_update()
        return {"added": added, "missing": missing}

    # --- reads ---------------------------------------------------------
    def read_metadata(self, package):
        return self._read_metadata(package)

    def changelog_path(self, package, version_code):
        """fdroidserver reads per-version release notes from
        metadata/<package>/<locale>/changelogs/<versionCode>.txt."""
        return os.path.join(FDROID_METADATA_DIR, package, "en-US", "changelogs", f"{int(version_code)}.txt")

    def write_changelog(self, package, version_code, text):
        if not ANDROID_PACKAGE_RE.match(package or ""):
            raise ValueError(f"Invalid package name: {package!r}")
        path = self.changelog_path(package, version_code)
        with self._lock:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                f.write(text.strip() + "\n")
            os.replace(tmp_path, path)

    def ensure_repo_icon(self):
        """Make the next rebuild publish data/fdroid/icon.png as the repo icon.

        config.yml names `repo_icon: icon.png`, which fdroidserver resolves
        against its working directory (data/fdroid/). Nothing ever wrote it,
        so fdroidserver generated a QR-code placeholder at repo/icons/icon.png
        -- and current fdroidserver only copies the real icon when that file
        does not exist yet, so the placeholder outlives any fix. Install the
        Feather icon when data/fdroid/icon.png is missing (an operator's own
        file is never overwritten), and remove a published repo icon that
        differs from it so the rebuild copies it afresh."""
        src = os.path.join(FDROID_DIR, "icon.png")
        published = os.path.join(FDROID_REPO_DIR, "icons", "icon.png")
        try:
            if not os.path.exists(src):
                os.makedirs(FDROID_DIR, exist_ok=True)
                shutil.copyfile(os.path.join(app.root_path, "static", "icon-512.png"), src)
            if os.path.exists(published):
                with open(src, "rb") as want, open(published, "rb") as have:
                    stale = want.read() != have.read()
                if stale:
                    os.remove(published)
                    return True
        except OSError as e:
            logging.warning(f"Could not install the F-Droid repo icon: {e}")
        return False

    def _read_metadata(self, package):
        path = self.metadata_path(package)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logging.error(f"Error reading Android metadata for {package}: {str(e)}")
            return None

    def load_index(self):
        """repo/index-v1.json as written by the sidecar, or None if it has never run."""
        path = os.path.join(FDROID_REPO_DIR, "index-v1.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error reading index-v1.json: {str(e)}")
            return None

    def list_apps(self):
        """Merge of metadata/*.yml (what feather intends) and index-v1.json
        (what is published)."""
        index = self.load_index()
        index_apps = {}
        index_packages = {}
        if index:
            for entry in index.get('apps', []):
                pkg = entry.get('packageName')
                if pkg:
                    index_apps[pkg] = entry
            index_packages = index.get('packages', {}) or {}

        packages = set(index_apps.keys())
        if os.path.isdir(FDROID_METADATA_DIR):
            for fn in os.listdir(FDROID_METADATA_DIR):
                if fn.endswith('.yml'):
                    packages.add(fn[:-4])

        disk_versions = {}  # package -> {version_code: filename}
        if os.path.isdir(FDROID_REPO_DIR):
            for fn in os.listdir(FDROID_REPO_DIR):
                m = ANDROID_APK_FILENAME_RE.match(fn)
                if not m:
                    continue
                pkg, vc = m.group(1), int(m.group(2))
                packages.add(pkg)
                disk_versions.setdefault(pkg, {})[vc] = fn

        marker_pending = os.path.exists(FDROID_UPDATE_MARKER)
        apps = []
        for package in sorted(packages):
            meta = self._read_metadata(package) or {}
            index_entry = index_apps.get(package, {})
            name = meta.get('Name') or index_entry.get('name') or package
            summary = meta.get('Summary', '')

            idx_pkg_list = index_packages.get(package, []) or []
            idx_by_vc = {p.get('versionCode'): p for p in idx_pkg_list}
            pkg_disk_versions = disk_versions.get(package, {})
            all_vcs = set(pkg_disk_versions.keys()) | set(idx_by_vc.keys())

            versions = []
            for vc in sorted(all_vcs, reverse=True):
                idx_v = idx_by_vc.get(vc)
                fn = pkg_disk_versions.get(vc) or (idx_v.get('apkName') if idx_v else self.apk_filename(package, vc))
                size = None
                if idx_v is not None:
                    size = idx_v.get('size')
                elif fn:
                    full = os.path.join(FDROID_REPO_DIR, fn)
                    if os.path.exists(full):
                        size = os.path.getsize(full)
                versions.append({
                    "versionCode": vc,
                    "versionName": idx_v.get('versionName') if idx_v else None,
                    "apkName": fn,
                    "size": size,
                    "minSdkVersion": idx_v.get('minSdkVersion') if idx_v else None,
                    "signer": idx_v.get('signer') if idx_v else None,
                    "published": idx_v is not None,
                })

            signers = {v['signer'] for v in versions if v.get('signer')}
            loc = index_entry.get('localized') or {}
            loc = loc.get('en-US') or next(iter(loc.values()), {}) if isinstance(loc, dict) else {}
            icon_url = None
            if isinstance(loc, dict) and loc.get('icon'):
                icon_url = f"/fdroid/repo/{package}/en-US/{loc['icon']}"
            elif index_entry.get('icon'):
                icon_url = f"/fdroid/repo/icons-640/{index_entry['icon']}"
            pending = marker_pending or any(not v['published'] for v in versions)
            apps.append({
                "package": package,
                "name": name,
                "summary": summary,
                "description": meta.get('Description', ''),
                "authorName": meta.get('AuthorName', ''),
                "license": meta.get('License', ''),
                "website": meta.get('WebSite', ''),
                "sourceCode": meta.get('SourceCode', ''),
                "categories": meta.get('Categories') or [],
                "versions": versions,
                "pending": pending,
                "mixed_signers": len(signers) > 1,
                "icon": icon_url,
            })
        return apps

    def status(self, sync_url=False):
        if sync_url and self._sync_repo_config_url():
            self.request_update()
        configured = os.path.exists(FDROID_FINGERPRINT)
        fingerprint = None
        if configured:
            try:
                with open(FDROID_FINGERPRINT, 'r') as f:
                    fingerprint = f.read().strip() or None
            except Exception as e:
                logging.error(f"Error reading fingerprint.txt: {str(e)}")
                fingerprint = None
            configured = fingerprint is not None

        base = _safe_base_url()
        if base:
            repo_url = base.rstrip('/') + '/fdroid/repo'
        else:
            repo_url = self.repo_config().get('repo_url', '')

        subscribe_url = None
        if configured and fingerprint and repo_url:
            subscribe_url = f"{repo_url}?fingerprint={fingerprint}"

        last_update = None
        if os.path.exists(FDROID_LAST_UPDATE):
            try:
                with open(FDROID_LAST_UPDATE, 'r') as f:
                    last_update = json.load(f)
            except Exception as e:
                logging.error(f"Error reading last-update.json: {str(e)}")
                last_update = None

        index = self.load_index()
        index_timestamp = (index.get('repo') or {}).get('timestamp') if index else None
        cfg = self.repo_config()

        return {
            "configured": configured,
            "fingerprint": fingerprint,
            "repo_url": repo_url,
            "subscribe_url": subscribe_url,
            "pending": os.path.exists(FDROID_UPDATE_MARKER),
            "last_update": last_update,
            "index_timestamp": index_timestamp,
            # Convenience for the admin UI -- not part of the plan's contract
            # sketch, sourced from repo_config() so the Android tab can
            # pre-fill its name/description fields without a second route.
            "name": cfg.get("name", ""),
            "description": cfg.get("description", ""),
        }

    def repo_config(self):
        """repo-config.json or defaults {'name': 'Feather Android', 'description': ''}"""
        if os.path.exists(FDROID_REPO_CONFIG):
            try:
                with open(FDROID_REPO_CONFIG, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logging.error(f"Error reading repo-config.json: {str(e)}")
        return {"name": "Feather Android", "description": ""}

    # --- writes (all under self._lock, all end with request_update) --------
    def _write_repo_config_file(self, cfg):
        os.makedirs(FDROID_DIR, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=FDROID_DIR, prefix=".repo-config-", suffix=".json.tmp")
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp_path, FDROID_REPO_CONFIG)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    REPO_NAME_MAX = 100
    REPO_DESCRIPTION_MAX = 2000

    def write_repo_config(self, name, description):
        """Validated: the values end up in fdroidserver's config.yml, and one
        it cannot parse makes every later rebuild fail."""
        for field, value, limit in (("name", name, self.REPO_NAME_MAX),
                                    ("description", description, self.REPO_DESCRIPTION_MAX)):
            if not isinstance(value, str):
                raise ValueError(f"{field} must be a string")
            if len(value) > limit:
                raise ValueError(f"{field} must be at most {limit} characters")
            if any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in value):
                raise ValueError(f"{field} contains control characters")
        name = name.strip() or "Feather Android"
        description = description.strip()
        base = _safe_base_url()
        repo_url = (base.rstrip('/') + '/fdroid/repo') if base else self.repo_config().get('repo_url', '')
        cfg = {"name": name, "description": description, "repo_url": repo_url}
        with self._lock:
            self._write_repo_config_file(cfg)
        self.request_update()

    def _sync_repo_config_url(self):
        """Persist the request/config-derived public URL when it changes."""
        base = _safe_base_url()
        if not base:
            if not self.repo_config().get('repo_url'):
                logging.warning(
                    "Android repo URL is unknown; the sidecar will advertise its "
                    "localhost fallback until PUBLIC_BASE_URL is set"
                )
            return False
        want = base.rstrip('/') + '/fdroid/repo'
        cfg = self.repo_config()
        if cfg.get('repo_url') == want:
            return False
        cfg = {
            "name": cfg.get("name") or "Feather Android",
            "description": cfg.get("description") or "",
            "repo_url": want,
        }
        with self._lock:
            self._write_repo_config_file(cfg)
        return True

    def validate_metadata(self, fields):
        """Validate and normalize API metadata without mutating storage."""
        normalized = {}
        for key, value in fields.items():
            if key not in self.METADATA_KEYS or value is None:
                continue
            if key == "Categories":
                if isinstance(value, str):
                    value = [category.strip() for category in value.split(',') if category.strip()]
                if (
                    not isinstance(value, list)
                    or not value
                    or any(not isinstance(category, str) or not category.strip() for category in value)
                ):
                    raise ValueError("Categories must be a non-empty list of strings")
                normalized[key] = [category.strip() for category in value]
                continue
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            limit = self._LIMITS.get(key)
            if limit is not None and len(value) > limit:
                raise ValueError(f"{key} must be at most {limit} characters")
            normalized[key] = value
        return normalized

    def write_metadata(self, package, fields):
        """fields: subset of METADATA_KEYS. Merges into the existing yml.
        Enforce: Name <= 50, Summary <= 80, Description <= 4000, Categories is
        a list (default ['Feather'])."""
        if not ANDROID_PACKAGE_RE.match(package or ""):
            raise ValueError(f"Invalid package name: {package!r}")
        fields = self.validate_metadata(fields)

        with self._lock:
            data = self._read_metadata(package) or {}
            for key in self.METADATA_KEYS:
                if key not in fields or fields[key] is None:
                    continue
                if key == "Categories":
                    data[key] = list(fields[key])
                else:
                    data[key] = fields[key]
            if not data.get("Categories"):
                data["Categories"] = ["Feather"]

            os.makedirs(FDROID_METADATA_DIR, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=FDROID_METADATA_DIR, prefix=f".{package}-", suffix=".yml.tmp")
            try:
                with os.fdopen(fd, 'w') as f:
                    yaml.safe_dump(data, f, sort_keys=True, allow_unicode=True, default_flow_style=False)
                os.replace(tmp_path, self.metadata_path(package))
            except Exception:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
        self.request_update()

    def add_apk(self, src_path, expected_package=None):
        """Inspect src_path with _inspect_apk. If expected_package is given and
        differs -> ValueError. If the destination apk already exists -> return
        (False, 'already present') WITHOUT overwriting. Else move src -> dest.
        Create metadata yml if missing, using app_name as Name. Return
        (True, info_dict)."""
        info = _inspect_apk(src_path)
        package = info["package"]
        if expected_package and expected_package != package:
            raise ValueError(
                f"APK package {package!r} does not match expected package {expected_package!r}"
            )
        # Every ingest path (admin upload, Telegram, release importers,
        # /api/publish) lands here, so the opt-in refusal lives here too.
        if info.get("debuggable") and APK_REJECT_DEBUGGABLE:
            raise ValueError("APK is debuggable (android:debuggable=true); upload a release build")

        dest = self.apk_path(package, info["version_code"])
        with self._lock:
            if os.path.exists(dest):
                return False, info
            os.makedirs(FDROID_REPO_DIR, exist_ok=True)
            try:
                os.replace(src_path, dest)
            except OSError as e:
                # UPLOAD_FOLDER and FDROID_REPO_DIR are usually the same
                # filesystem (both under DATA_DIR), but compose.yml mounts
                # ./data/fdroid as its own bind mount alongside ./data --
                # same layout as ipas/ and icons/ -- which some Docker
                # storage/mount configurations surface to the container as
                # a different device, making a plain rename cross-device.
                # Fall back to copy+remove in that case only.
                if e.errno != errno.EXDEV:
                    raise
                tmp_dest = os.path.join(FDROID_REPO_DIR, f".{os.path.basename(dest)}.part")
                try:
                    shutil.copy2(src_path, tmp_dest)
                    with open(tmp_dest, 'rb') as copied:
                        os.fsync(copied.fileno())
                    os.replace(tmp_dest, dest)
                finally:
                    if os.path.exists(tmp_dest):
                        try:
                            os.remove(tmp_dest)
                        except OSError:
                            pass
                os.remove(src_path)

        if not os.path.exists(self.metadata_path(package)):
            # The label is the APK's own, unvalidated: one over the Name limit
            # used to raise here, after the APK was already in repo/, leaving
            # it with no metadata, no index rebuild and no icon.
            name = (info.get("app_name") or package).strip() or package
            limit = self._LIMITS["Name"]
            if len(name) > limit:
                name = name[:limit - 1].rstrip() + "…"
            self.write_metadata(package, {"Name": name})
        if apk_storage is not None and not apk_storage.put(dest):
            logging.warning("APK %s kept on local disk only; Rebuild Index retries the Garage upload",
                            os.path.basename(dest))
        newest = self._newest_apk(package)
        if newest and newest[1] == dest:
            try:
                self.refresh_icon(package, dest)
            except Exception as e:
                logging.warning("could not extract icon for %s: %s", package, e)
        self._sync_repo_config_url()
        self.request_update()
        return True, info

    def delete_version(self, package, version_code):
        if not ANDROID_PACKAGE_RE.match(package or ""):
            raise ValueError(f"Invalid package name: {package!r}")
        try:
            version_code = int(version_code)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid versionCode: {version_code!r}")
        path = self.apk_path(package, version_code)
        with self._lock:
            if not os.path.exists(path):
                return False
            os.remove(path)
        if apk_storage is not None:
            apk_storage.delete(os.path.basename(path))
        self.request_update()
        return True

    def delete_app(self, package):
        if not ANDROID_PACKAGE_RE.match(package or ""):
            raise ValueError(f"Invalid package name: {package!r}")
        removed_something = False
        with self._lock:
            if os.path.isdir(FDROID_REPO_DIR):
                for fn in os.listdir(FDROID_REPO_DIR):
                    match = ANDROID_APK_FILENAME_RE.match(fn)
                    if not match or match.group(1) != package:
                        continue
                    try:
                        os.remove(os.path.join(FDROID_REPO_DIR, fn))
                        removed_something = True
                    except OSError:
                        pass
                    if apk_storage is not None:
                        apk_storage.delete(fn)
            meta_path = self.metadata_path(package)
            if os.path.exists(meta_path):
                os.remove(meta_path)
                removed_something = True
            meta_dir = os.path.join(FDROID_METADATA_DIR, package)
            if os.path.isdir(meta_dir):
                shutil.rmtree(meta_dir, ignore_errors=True)
        self.request_update()
        return removed_something

    def request_update(self):
        os.makedirs(FDROID_DIR, exist_ok=True)
        self.ensure_repo_icon()
        open(FDROID_UPDATE_MARKER, 'a').close()


# Initialize source manager
source_manager = SourceManager(SOURCE_FILE)

# Plan 073: Android / F-Droid repository manager (own directory tree under
# DATA_DIR/fdroid; does not touch source_manager's data at all).
android_repo = AndroidRepoManager()

# Initialize IPA storage backend. STORAGE_BACKEND defaults to "local", so
# merging this is a no-op until the flag is deliberately flipped to
# "garage" -- see _require_garage_config for the refuse-to-start check.
if STORAGE_BACKEND == "garage":
    ipa_storage = GarageIpaStorage()
else:
    ipa_storage = LocalIpaStorage()

# APK download mirror (APK_STORAGE_BACKEND, defaults to STORAGE_BACKEND).
apk_storage = GarageApkMirror() if APK_STORAGE_BACKEND == "garage" else None

# Icon storage backend (ICON_STORAGE_BACKEND, defaults to STORAGE_BACKEND).
# Independent so icons can live on local disk while IPAs stay on Garage.
if ICON_STORAGE_BACKEND == "garage":
    icon_storage = GarageIconStorage()
else:
    icon_storage = LocalIconStorage()


_FDROID_REPO_CSP = (
    "sandbox; default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"
)


@app.after_request
def _security_headers(response):
    """Baseline headers. /fdroid/repo/ holds pages fdroidserver generates
    (index.html interpolates the repo name and description unescaped); a
    sandboxing CSP gives them an opaque origin so nothing there can reach
    the admin session or API, whatever fdroidserver writes."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.path.startswith("/fdroid/repo/"):
        response.headers["Content-Security-Policy"] = _FDROID_REPO_CSP
    else:
        response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.before_request
def _reject_malformed_json():
    """Every JSON API here takes an object. Reject anything else up front
    with a 400 instead of letting a list/str/None or pathological nesting
    surface as an AttributeError/RecursionError 500 deep in a handler."""
    if request.method != "POST" or not request.is_json:
        return None
    raw = request.get_data(cache=True)
    if not raw.strip():
        return None
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return jsonify({"success": False, "error": "Request body is not valid JSON"}), 400
    if not isinstance(body, dict):
        return jsonify({"success": False, "error": "Request body must be a JSON object"}), 400
    return None


# Failed-login throttle: per client address, sliding window, in-process
# (the app is single-process by design). Behind a reverse proxy every
# client shares the proxy's address, so this becomes a global limit --
# still bounded to LOGIN_MAX_FAILURES per window, and a success clears it.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 60
_login_failures = {}
_login_failures_lock = threading.Lock()


def _login_retry_after(addr, now):
    with _login_failures_lock:
        recent = [t for t in _login_failures.get(addr, []) if now - t < LOGIN_WINDOW_SECONDS]
        _login_failures[addr] = recent
        if len(recent) >= LOGIN_MAX_FAILURES:
            return max(1, int(LOGIN_WINDOW_SECONDS - (now - recent[0])) + 1)
        return 0


def _record_login_failure(addr, now):
    with _login_failures_lock:
        _login_failures.setdefault(addr, []).append(now)
        if len(_login_failures) > 10000:           # bound memory under address spraying
            _login_failures.clear()


def _password_fingerprint():
    return hmac.new(app.secret_key if isinstance(app.secret_key, bytes) else app.secret_key.encode(),
                    ADMIN_PASSWORD.encode(), hashlib.sha256).hexdigest()[:32]


def _session_authed():
    """A signed session cookie is stateless, so it used to outlive a password
    change. It now carries a fingerprint of ADMIN_PASSWORD: changing the
    password (or SECRET_KEY) signs every existing session out."""
    if not session.get('authed'):
        return False
    return hmac.compare_digest(str(session.get('pw', '')), _password_fingerprint())


def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not _session_authed():
            return jsonify({"success": False, "error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return wrapper


# API tokens for agents and CI. A token can publish IPAs/APKs and read the
# catalogue (requires_publish) but never manage tokens, delete apps, or change
# settings -- those stay session-only (requires_auth). Only a SHA-256 of each
# token is stored; the plaintext is shown once, at creation.
API_TOKENS_FILE = os.path.join(DATA_DIR, "api-tokens.json")
API_TOKEN_PREFIX = "ftr_"
_api_tokens_lock = threading.Lock()


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_api_tokens():
    try:
        with open(API_TOKENS_FILE, "r") as f:
            data = json.load(f)
        return [t for t in data if isinstance(t, dict) and t.get("hash")] if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        logging.error(f"Could not read {API_TOKENS_FILE}: {e}")
        return []


def _save_api_tokens(tokens):
    tmp = API_TOKENS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tokens, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, API_TOKENS_FILE)


def _public_token(t):
    return {k: t.get(k) for k in ("id", "name", "prefix", "created", "lastUsed", "apps", "expires")}


def _bearer_token():
    """The live token entry for `Authorization: Bearer ftr_...`, else None.
    Expired tokens are refused like revoked ones."""
    header = request.headers.get("Authorization", "")
    scheme, _, supplied = header.partition(" ")
    supplied = supplied.strip()
    if scheme.lower() != "bearer" or not supplied.startswith(API_TOKEN_PREFIX):
        return None
    digest = _hash_token(supplied)
    with _api_tokens_lock:
        tokens = _load_api_tokens()
        match = next((t for t in tokens if hmac.compare_digest(t["hash"], digest)), None)
        if match is None:
            return None
        now = datetime.now(timezone.utc).replace(microsecond=0)
        if match.get("expires") and _parse_utc(match["expires"]) <= now:
            return None
        last = match.get("lastUsed")
        # Record use at most once a minute so a busy agent is not a write storm.
        if not last or (now - _parse_utc(last)).total_seconds() >= 60:
            match["lastUsed"] = _utc_iso(now)
            try:
                _save_api_tokens(tokens)
            except OSError as e:
                logging.warning(f"Could not record API token use: {e}")
    return match


def _utc_iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _parse_utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _token_may_touch(app_id):
    """Sessions may touch anything; a token limited to `apps` only those IDs."""
    token = g.get("api_token")
    if token is None or not token.get("apps"):
        return True
    return app_id in token["apps"]


def _scope_refusal(app_id):
    return jsonify({"success": False, "id": app_id,
                    "error": f"This token may only publish: {', '.join(g.api_token['apps'])}"}), 403


def requires_publish(f):
    """Admin session OR a valid API token."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        g.api_token = None
        if not _session_authed():
            g.api_token = _bearer_token()
            if g.api_token is None:
                return jsonify({"success": False, "error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return wrapper


# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/version')
def version():
    """Public: the commit the running image was built from (CI passes it as
    FEATHER_VERSION). The way to check a deploy -- route probes stop working
    once the probed route exists in both images."""
    return jsonify({"version": _env("FEATHER_VERSION", "dev")})

@app.route('/store')
def store_preview():
    """Public read-only storefront. Renders client-side from the same public
    documents subscribers fetch (/source.json, /fdroid/repo/index-v1.json),
    so it can never show more than a phone already sees."""
    return render_template('store.html')

@app.route('/api/login', methods=['POST'])
def login():
    addr = request.remote_addr or "unknown"
    now = time.monotonic()
    retry_after = _login_retry_after(addr, now)
    if retry_after:
        resp = jsonify({"success": False, "error": f"Too many failed attempts; try again in {retry_after}s"})
        resp.headers["Retry-After"] = str(retry_after)
        return resp, 429
    data = request.get_json(silent=True) or {}
    supplied = data.get('password', '')
    if not isinstance(supplied, str):
        supplied = ''
    if hmac.compare_digest(supplied.encode(), ADMIN_PASSWORD.encode()):
        with _login_failures_lock:
            _login_failures.pop(addr, None)
        session['authed'] = True
        session['pw'] = _password_fingerprint()
        session.permanent = True
        return jsonify({"success": True})
    _record_login_failure(addr, now)
    logging.warning("Failed login attempt from %s", request.remote_addr)
    return jsonify({"success": False, "error": "Invalid password"}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"success": True})

@app.route('/api/session')
def session_status():
    return jsonify({"authed": _session_authed()})

@app.route('/source.json')
def serve_source():
    try:
        source_data = source_manager.load_source()
        if source_data is None:
            return send_file(SOURCE_FILE, mimetype='application/json')
        return jsonify(normalize_source(source_data))
    except Exception as e:
        logging.error(f"Error serving source: {str(e)}")
        return jsonify({"error": "Source unavailable"}), 404

@app.route('/ipas/<bundle_id>/<filename>')
def serve_ipa(bundle_id, filename):
    """Serve IPA files.

    Plan 011: the catalog URL is permanent regardless of storage backend.
    ipa_storage.public_url() returns None for the local backend (send_file
    below, unchanged) or a Garage web-endpoint URL for the garage backend
    (302 redirect). exists() is checked first so a missing object still
    gives a clean 404 rather than a redirect into a Garage NoSuchKey page.
    """
    try:
        # Security: ensure both URL segments are safe
        safe_bundle_id = secure_filename(bundle_id)
        safe_filename = secure_filename(filename)
        version = safe_filename[:-4] if safe_filename.lower().endswith('.ipa') else safe_filename

        if not ipa_storage.exists(safe_bundle_id, version):
            return jsonify({"error": "IPA file not found"}), 404

        url = ipa_storage.public_url(safe_bundle_id, version)
        if url:
            return redirect(url, code=302)

        filepath = os.path.join(IPA_FOLDER, safe_bundle_id, safe_filename)
        return send_file(filepath, mimetype='application/octet-stream', as_attachment=True, download_name=filename)
    except Exception as e:
        logging.error(f"Error serving IPA: {str(e)}")
        return jsonify({"error": "IPA unavailable"}), 500

@app.route('/icons/<bundle_id>/icon.<ext>')
def serve_icon(bundle_id, ext):
    """Serve app icons through the configured storage backend."""
    try:
        # Security: ensure bundle_id and extension are safe
        safe_bundle_id = secure_filename(bundle_id)
        safe_ext = secure_filename(ext)

        if not safe_bundle_id or safe_bundle_id != bundle_id or not safe_ext or safe_ext != ext:
            return jsonify({"error": "Invalid icon path"}), 400
        
        if safe_ext not in ALLOWED_ICON_EXTENSIONS:
            return jsonify({"error": "Invalid icon format"}), 400
        
        if not icon_storage.exists(safe_bundle_id, safe_ext):
            return jsonify({"error": "Icon file not found"}), 404

        url = icon_storage.public_url(safe_bundle_id, safe_ext)
        if url:
            return redirect(url, code=302)

        filepath = os.path.join(ICON_FOLDER, safe_bundle_id, f"icon.{safe_ext}")
        mimetype = ICON_MIME_TYPES.get(safe_ext, 'image/png')
        
        return send_file(filepath, mimetype=mimetype)
    except Exception as e:
        logging.error(f"Error serving icon: {str(e)}")
        return jsonify({"error": "Icon unavailable"}), 500

@app.route('/qr')
def generate_qr():
    try:
        # Use feather:// URL scheme for QR code
        source_url = resolve_base_url() + '/source.json'
        feather_url = source_url.replace('https://', 'feather://').replace('http://', 'feather://')
        
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(feather_url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        
        return send_file(buf, mimetype='image/png')
    except Exception as e:
        logging.error(f"QR generation error: {str(e)}")
        return jsonify({"error": "QR generation failed"}), 500

@app.route('/sw.js')
def service_worker():
    js = """
self.addEventListener('install', (e) => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
// Network-first for navigations; never cache API or authenticated pages.
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(caches.open('feather-static-v1').then(async (c) => {
      const hit = await c.match(e.request);
      if (hit) return hit;
      const res = await fetch(e.request);
      if (res.ok) c.put(e.request, res.clone());
      return res;
    }));
  }
  // everything else: default network handling (no caching)
});
""".strip()
    resp = app.response_class(js, mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.route('/api/apps')
def get_apps():
    try:
        source_data = source_manager.load_source()
        if source_data:
            return jsonify(source_data.get('apps', []))
        return jsonify([])
    except Exception as e:
        logging.error(f"Error getting apps: {str(e)}")
        return jsonify([])

@app.route('/api/add-app', methods=['POST'])
@requires_auth
def add_app():
    try:
        base_url = resolve_base_url()
        # Check if request is form-data (file upload) or JSON
        if request.content_type and 'multipart/form-data' in request.content_type:
            # Form data request
            ipa_file = request.files.get('ipaFile')
            download_from_url = request.form.get('downloadFromUrl', 'false').lower() == 'true'
            icon_file = request.files.get('iconFile')
            download_icon_from_url = request.form.get('downloadIconFromUrl', 'false').lower() == 'true'
            data = {
                'name': request.form.get('name'),
                'bundleIdentifier': request.form.get('bundleIdentifier'),
                'developerName': request.form.get('developerName'),
                'localizedDescription': request.form.get('localizedDescription', ''),
                'iconURL': request.form.get('iconURL', ''),
                'version': request.form.get('version'),
                'buildVersion': request.form.get('buildVersion'),
                'downloadURL': request.form.get('downloadURL', ''),
                'minOSVersion': request.form.get('minOSVersion', '14.0')
            }
            privacy_raw = request.form.get('privacy')
            if privacy_raw:
                privacy = json.loads(privacy_raw)
                if not isinstance(privacy, dict):
                    raise ValueError("privacy must be a JSON object")
                data['appPermissions'] = {"entitlements": [], "privacy": privacy}
        else:
            # JSON request (backward compatibility)
            data = request.get_json() if request.is_json else {}
            ipa_file = None
            download_from_url = False
            icon_file = None
            download_icon_from_url = False

        if not data.get('bundleIdentifier'):
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400

        success, message = source_manager.add_app_manual(
            data,
            ipa_file=ipa_file if ipa_file and ipa_file.filename else None,
            download_from_url=download_from_url,
            icon_file=icon_file if icon_file and icon_file.filename else None,
            download_icon_from_url=download_icon_from_url,
            base_url=base_url,
        )

        if success:
            notify("add_app", f"New app published: {data.get('name')} ({data.get('bundleIdentifier')}) v{data.get('version')}\n{base_url}/source.json")
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400

    except Exception as e:
        logging.error(f"Error adding app: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/delete-app', methods=['POST'])
@requires_auth
def delete_app():
    try:
        data = request.json
        bundle_id = data.get('bundleIdentifier')
        
        if not bundle_id:
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400
        
        success, message = source_manager.delete_app(bundle_id)

        if success:
            notify("delete_app", f"App removed: {bundle_id}")
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
            
    except Exception as e:
        logging.error(f"Error deleting app: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/app/<bundle_identifier>')
def get_app(bundle_identifier):
    try:
        app = source_manager.get_app(bundle_identifier)
        if app:
            return jsonify(app)
        return jsonify({"error": "App not found"}), 404
    except Exception as e:
        logging.error(f"Error getting app: {str(e)}")
        return jsonify({"error": str(e)}), 400

@app.route('/api/update-app', methods=['POST'])
@requires_auth
def update_app():
    try:
        base_url = resolve_base_url()
        # Check if request is form-data (file upload) or JSON
        if request.content_type and 'multipart/form-data' in request.content_type:
            # Form data request
            icon_file = request.files.get('iconFile')
            download_icon_from_url = request.form.get('downloadIconFromUrl', 'false').lower() == 'true'
            data = {
                'bundleIdentifier': request.form.get('bundleIdentifier'),
                'name': request.form.get('name'),
                'developerName': request.form.get('developerName'),
                'localizedDescription': request.form.get('localizedDescription', ''),
                'iconURL': request.form.get('iconURL', ''),
                'subtitle': request.form.get('subtitle'),
                'tintColor': request.form.get('tintColor'),
                'screenshotURLs': request.form.get('screenshotURLs')
            }
            bundle_id = data.get('bundleIdentifier')
        else:
            # JSON request (backward compatibility)
            data = request.get_json() if request.is_json else {}
            bundle_id = data.get('bundleIdentifier')
            icon_file = None
            download_icon_from_url = False
        
        if not bundle_id:
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400
        
        success, message = source_manager.update_app(
            bundle_id, 
            data, 
            icon_file=icon_file if icon_file and icon_file.filename else None,
            download_icon_from_url=download_icon_from_url,
            base_url=base_url
        )
        
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
            
    except Exception as e:
        logging.error(f"Error updating app: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/add-version', methods=['POST'])
@requires_auth
def add_version():
    try:
        base_url = resolve_base_url()
        # Check if request is form-data (file upload) or JSON
        if request.content_type and 'multipart/form-data' in request.content_type:
            # Form data request
            ipa_file = request.files.get('ipaFile')
            download_from_url = request.form.get('downloadFromUrl', 'false').lower() == 'true'
            bundle_id = request.form.get('bundleIdentifier')
            data = {
                'version': request.form.get('version'),
                'buildVersion': request.form.get('buildVersion'),
                'downloadURL': request.form.get('downloadURL', ''),
                'minOSVersion': request.form.get('minOSVersion', '')
            }
        else:
            # JSON request (backward compatibility)
            data = request.get_json() if request.is_json else {}
            bundle_id = data.get('bundleIdentifier')
            ipa_file = None
            download_from_url = False
        
        if not bundle_id:
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400
        
        if not data.get('version'):
            return jsonify({"success": False, "error": "Version is required"}), 400
        
        if not ipa_file and not data.get('downloadURL') and not download_from_url:
            return jsonify({"success": False, "error": "Either IPA file or download URL is required"}), 400
        
        success, message = source_manager.add_version(bundle_id, data, ipa_file=ipa_file if ipa_file and ipa_file.filename else None, download_from_url=download_from_url, base_url=base_url)

        if success:
            app_info = source_manager.get_app(bundle_id)
            app_name = app_info.get('name', bundle_id) if app_info else bundle_id
            size = app_info['versions'][0].get('size', 0) if app_info and app_info.get('versions') else 0
            notify("add_version", f"New version: {app_name} {data.get('version')} — {size} bytes\n{base_url}/source.json")
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
            
    except Exception as e:
        logging.error(f"Error adding version: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

def _normalize_repo_project(raw):
    """Strip browser-URL chrome from a pasted repo reference -> 'Owner/Repo'."""
    p = (raw or "").strip()
    for prefix in ("https://", "http://"):
        if p.lower().startswith(prefix):
            p = p[len(prefix):]
    if p.lower().startswith("www."):
        p = p[4:]
    for host in ("github.com/", "gitlab.com/"):
        if p.lower().startswith(host):
            p = p[len(host):]
    p = p.strip("/")
    if p.endswith(".git"):
        p = p[:-4]
    return p.strip("/")

def _repo_owner(project):
    """First path segment of an owner/repo (or group/project) string."""
    return (project or "").strip("/").split("/")[0].strip()

def _clean_description(text, limit=800):
    """Trim release notes to a catalog-friendly description."""
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _decode_cgbi_png(data):
    """Reverse Apple's ``-iphone`` pngcrush CgBI transform back into a
    standard RGBA Pillow Image.

    CgBI PNGs store BGRA pixels with premultiplied alpha and a raw-deflate
    (no zlib header) IDAT stream. This undoes both, following the
    well-known "pngdefry"/"iphone-png-normalizer" approach. Raises
    ``ValueError`` (or lets the underlying zlib/struct error propagate) on
    anything that isn't a well-formed 8-bit RGBA CgBI PNG -- callers must
    catch and fall back.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")

    pos = 8
    length = len(data)
    has_cgbi = False
    ihdr = None
    idat = bytearray()
    while pos + 8 <= length:
        chunk_len = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        chunk_data = data[pos + 8:pos + 8 + chunk_len]
        if chunk_type == b"CgBI":
            has_cgbi = True
        elif chunk_type == b"IHDR":
            ihdr = chunk_data
        elif chunk_type == b"IDAT":
            idat += chunk_data
        elif chunk_type == b"IEND":
            break
        pos += 8 + chunk_len + 4  # data + CRC

    if not has_cgbi:
        raise ValueError("not a CgBI PNG")
    if not ihdr or len(ihdr) < 10:
        raise ValueError("missing/short IHDR")

    width, height, bit_depth, color_type = struct.unpack(">IIBB", ihdr[:10])
    if bit_depth != 8 or color_type != 6:
        raise ValueError(f"unsupported CgBI IHDR (bit_depth={bit_depth}, color_type={color_type})")
    # The pixel loops below are pure Python and run under the catalogue lock;
    # an attacker-declared 8192x8192 icon cost 76 s of CPU and 1.1 GB. Apple
    # icons are at most 1024x1024.
    if not (0 < width <= CGBI_MAX_SIDE and 0 < height <= CGBI_MAX_SIDE):
        raise ValueError(f"CgBI icon {width}x{height} exceeds {CGBI_MAX_SIDE}px")

    expected = (width * 4 + 1) * height
    raw = zlib.decompressobj(-zlib.MAX_WBITS).decompress(bytes(idat), expected + 1)

    bpp = 4
    stride = width * bpp
    if len(raw) < (stride + 1) * height:
        raise ValueError("truncated CgBI pixel data")

    out = bytearray(width * height * bpp)
    prev_row = bytearray(stride)
    read_pos = 0
    for y in range(height):
        filter_type = raw[read_pos]
        read_pos += 1
        row = bytearray(raw[read_pos:read_pos + stride])
        read_pos += stride

        if filter_type == 0:  # None
            pass
        elif filter_type == 1:  # Sub
            for x in range(bpp, stride):
                row[x] = (row[x] + row[x - bpp]) & 0xFF
        elif filter_type == 2:  # Up
            for x in range(stride):
                row[x] = (row[x] + prev_row[x]) & 0xFF
        elif filter_type == 3:  # Average
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                b = prev_row[x]
                row[x] = (row[x] + ((a + b) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                b = prev_row[x]
                c = prev_row[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    pred = a
                elif pb <= pc:
                    pred = b
                else:
                    pred = c
                row[x] = (row[x] + pred) & 0xFF
        else:
            raise ValueError(f"unsupported PNG filter type {filter_type}")

        row_offset = y * stride
        for x in range(0, stride, bpp):
            b, g, r, a = row[x], row[x + 1], row[x + 2], row[x + 3]
            if a == 0:
                rr = gg = bb = 0
            else:
                rr = min(255, (r * 255) // a)
                gg = min(255, (g * 255) // a)
                bb = min(255, (b * 255) // a)
            o = row_offset + x
            out[o] = rr
            out[o + 1] = gg
            out[o + 2] = bb
            out[o + 3] = a

        prev_row = row

    return Image.frombytes("RGBA", (width, height), bytes(out))


CGBI_MAX_SIDE = 1024
ICON_MAX_SIDE = 4096       # refuse to decode anything larger (decompression bomb)
ICON_STORED_SIDE = 512     # what is stored and served; matches the APK path


def _open_icon(raw):
    """Decode an untrusted icon image, refusing oversized dimensions before
    any pixel data is inflated (Image.open only reads the header)."""
    img = Image.open(io.BytesIO(raw))
    if img.size[0] > ICON_MAX_SIDE or img.size[1] > ICON_MAX_SIDE:
        raise ValueError(f"icon {img.size[0]}x{img.size[1]} is implausibly large")
    img.load()
    return img


def _save_temp_png(img):
    """Save a PIL Image as a standard PNG (at most ICON_STORED_SIDE square)
    to a new temp file and return its path."""
    if img.size[0] > ICON_STORED_SIDE or img.size[1] > ICON_STORED_SIDE:
        img = img.copy()
        img.thumbnail((ICON_STORED_SIDE, ICON_STORED_SIDE))
    fd, path = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=".png")
    os.close(fd)
    img.save(path, format="PNG")
    return path


def _add_app_with_avatar_icon(new_app, fs, owner, base_url):
    """Create an app using the GitHub owner avatar as its icon. The avatar
    is cosmetic: if it cannot be fetched (404 for some org/renamed owners,
    rate limits, network), create the app iconless -- the source icon is
    served in its place -- instead of failing the whole import."""
    with_icon = dict(new_app, iconURL=f"https://github.com/{owner}.png")
    ok, message = source_manager.add_app_manual(
        with_icon, ipa_file=fs, download_icon_from_url=True, base_url=base_url)
    if ok or not str(message).startswith("Failed to download icon"):
        return ok, message
    logging.warning("import: owner avatar for %s unavailable; creating %s without an icon",
                    owner, new_app.get("bundleIdentifier"))
    fs.stream.seek(0)
    return source_manager.add_app_manual(dict(new_app, iconURL=""), ipa_file=fs, base_url=base_url)


_MAX_ICON_MEMBER_BYTES = 8 * 1024 * 1024


def _zip_read_capped(zf, name):
    """zf.read with a size cap: icon candidates and Info.plist come from an
    untrusted archive and would otherwise be inflated whole (zip bomb)."""
    info = zf.getinfo(name)
    if info.file_size > _MAX_ICON_MEMBER_BYTES:
        raise ValueError(f"{name} is implausibly large")
    with zf.open(info) as handle:
        data = handle.read(_MAX_ICON_MEMBER_BYTES + 1)
    if len(data) > _MAX_ICON_MEMBER_BYTES:
        raise ValueError(f"{name} is implausibly large")
    return data


def _extract_ipa_icon(ipa_path):
    """Best-effort extraction of the app icon from an .ipa, normalized to a
    standard PNG. Returns a path to a new temporary PNG file (the caller
    must delete it) or ``None`` if no icon could be extracted. Never raises
    -- any failure anywhere just yields ``None`` so the caller falls back.
    """
    try:
        with zipfile.ZipFile(ipa_path) as zf:
            names = zf.namelist()

            # 1. iTunesArtwork shortcut: a root-level, usually-standard PNG.
            for wanted in ("itunesartwork@2x", "itunesartwork.png", "itunesartwork"):
                for name in names:
                    if "/" not in name and name.lower() == wanted:
                        try:
                            return _save_temp_png(_open_icon(_zip_read_capped(zf, name)))
                        except Exception:
                            pass

            # 2. Find the .app directory inside Payload/.
            app_dirs = sorted({
                n.split("/", 2)[1] for n in names
                if n.startswith("Payload/") and n.count("/") >= 1
                and len(n.split("/", 2)) > 1 and n.split("/", 2)[1].endswith(".app")
            })
            if not app_dirs:
                return None
            app_prefix = f"Payload/{app_dirs[0]}/"

            # 3. Candidate icon base names from Info.plist.
            icon_bases = []
            info_plist_path = f"{app_prefix}Info.plist"
            if info_plist_path in names:
                try:
                    plist = plistlib.loads(_zip_read_capped(zf, info_plist_path))
                except Exception:
                    plist = {}
                primary = ((plist.get("CFBundleIcons") or {}).get("CFBundlePrimaryIcon") or {})
                files = primary.get("CFBundleIconFiles")
                if isinstance(files, list):
                    icon_bases.extend(b for b in files if isinstance(b, str))
                top_files = plist.get("CFBundleIconFiles")
                if isinstance(top_files, list):
                    icon_bases.extend(b for b in top_files if isinstance(b, str))
                top_file = plist.get("CFBundleIconFile")
                if isinstance(top_file, str) and top_file:
                    icon_bases.append(top_file)

            candidates = []
            seen = set()
            for base in icon_bases:
                base_noext = base[:-4] if base.lower().endswith(".png") else base
                pattern = f"{app_prefix}{base_noext}*.png"
                for name in names:
                    if name not in seen and fnmatch.fnmatch(name, pattern):
                        candidates.append(name)
                        seen.add(name)

            if not candidates:
                pattern = f"{app_prefix}AppIcon*.png"
                candidates = [n for n in names if fnmatch.fnmatch(n, pattern)]

            if not candidates:
                return None

            # 4. Pick the largest candidate (decoded pixel area, or a
            # declared/uncompressed-size proxy when it won't decode yet),
            # preferring @3x/@2x on ties.
            def scale_rank(name):
                lowered = name.lower()
                if "@3x" in lowered:
                    return 2
                if "@2x" in lowered:
                    return 1
                return 0

            best_name = None
            best_key = None
            for name in candidates:
                try:
                    raw = _zip_read_capped(zf, name)
                except Exception:
                    continue
                try:
                    probe = Image.open(io.BytesIO(raw))
                    size_measure = probe.size[0] * probe.size[1]
                except Exception:
                    try:
                        size_measure = zf.getinfo(name).file_size
                    except KeyError:
                        size_measure = len(raw)
                key = (size_measure, scale_rank(name))
                if best_key is None or key > best_key:
                    best_key = key
                    best_name = name

            if best_name is None:
                return None

            raw = _zip_read_capped(zf, best_name)

            # 5. A minority of IPAs ship standard (non-CgBI) PNGs here.
            try:
                return _save_temp_png(_open_icon(raw))
            except Exception:
                pass

            # 6. Otherwise treat it as CgBI and normalize it.
            try:
                img = _decode_cgbi_png(raw)
                return _save_temp_png(img)
            except Exception:
                return None
    except Exception:
        return None


def _inspect_apk(path):
    """Read identity and version out of an APK's binary AndroidManifest.

    Thin wrapper over scripts/apk_inspection.py (shared with the release
    importer). Returns the same dict and raises the same ValueError as
    before, so every existing caller is unaffected.
    """
    try:
        inspection = inspect_apk(path)
    except ApkInspectionError as e:
        raise ValueError(str(e))
    return {
        "package": inspection.package,
        "version_code": inspection.version_code,
        "version_name": inspection.version_name,
        "min_sdk": inspection.min_sdk,
        "target_sdk": inspection.target_sdk,
        "app_name": inspection.app_name,
        "debuggable": inspection.debuggable,
    }


def _apply_icon_to_existing(bundle_id, existing, ipa_path, *, provider, project,
                            icon_url_in="", base_url=None):
    """Best-effort: (re)apply an icon to an already-existing app during import.

    An explicit ``icon_url_in`` (user-supplied) always wins. Otherwise auto-detect
    ONLY when the current icon is missing/placeholder (never clobber a good icon on
    a routine version import): IPA-extracted icon, else GitHub owner avatar.
    Never raises -- a failed icon step must not fail the version import.
    """
    try:
        if icon_url_in:
            ok, msg = source_manager.update_app(
                bundle_id, {"iconURL": icon_url_in},
                download_icon_from_url=True, base_url=base_url)
            if not ok:
                logging.warning("import: icon URL update failed for %s: %s", bundle_id, msg)
            return
        cur_icon = (existing.get("iconURL") or "").strip()
        if cur_icon and cur_icon != SOURCE_ARTWORK_URL:
            return  # already has a real icon — leave it
        extracted_icon = _extract_ipa_icon(ipa_path)
        try:
            if extracted_icon:
                icon_fs = FileStorage(stream=open(extracted_icon, "rb"), filename="icon.png")
                source_manager.update_app(bundle_id, {}, icon_file=icon_fs, base_url=base_url)
            else:
                owner = _repo_owner(project) if provider == "github" else ""
                if owner:
                    source_manager.update_app(
                        bundle_id, {"iconURL": f"https://github.com/{owner}.png"},
                        download_icon_from_url=True, base_url=base_url)
        finally:
            if extracted_icon:
                try:
                    os.remove(extracted_icon)
                except OSError:
                    pass
    except Exception:
        logging.exception("import: applying icon to existing app %s failed", bundle_id)


# ---------------------------------------------------------------------------
# Import provenance: private, bounded operational history outside source.json.
# ---------------------------------------------------------------------------

_IMPORT_HISTORY_LOCK = threading.Lock()
_IMPORT_HISTORY_MAX = 200
_IMPORT_HISTORY_STRING_FIELDS = {
    "jobId", "provider", "project", "releaseId", "releaseTag", "assetId",
    "assetName", "bundleIdentifier", "version", "buildVersion", "platform",
    "sha256", "message",
}


def _import_history_path():
    return os.path.join(DATA_DIR, "import-history.json")


def _clean_history_text(value, limit=256):
    if not isinstance(value, str):
        return None
    value = _redact_secret(value)
    value = ''.join(ch for ch in value if ch >= ' ' and ch != '\x7f').strip()
    return value[:limit] or None


def _normalize_import_record(raw, force_trigger=None):
    if not isinstance(raw, dict):
        raise ValueError("Import record must be an object")
    trigger = force_trigger or raw.get("trigger")
    if trigger not in ("one-off", "auto", "standalone"):
        raise ValueError("Invalid import trigger")
    status = raw.get("status")
    if status not in ("published", "skipped", "error"):
        raise ValueError("Invalid import status")
    stage = raw.get("stage")
    if stage not in ("selection", "download", "preflight", "publish"):
        raise ValueError("Invalid import stage")
    record = {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "status": status,
        "stage": stage,
    }
    duration = raw.get("durationMs")
    if isinstance(duration, int) and not isinstance(duration, bool) and 0 <= duration <= 604800000:
        record["durationMs"] = duration
    for key in _IMPORT_HISTORY_STRING_FIELDS:
        value = _clean_history_text(raw.get(key), 500 if key == "message" else 256)
        if value is not None:
            record[key] = value
    return record


def load_import_history():
    try:
        with open(_import_history_path(), 'r') as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or data.get("schemaVersion") != 1 or not isinstance(data.get("records"), list):
            raise ValueError("unsupported import history schema")
        return data
    except FileNotFoundError:
        return {"schemaVersion": 1, "records": []}
    except Exception as exc:
        logging.warning("Could not load import history: %s", exc)
        return {"schemaVersion": 1, "records": []}


def append_import_record(raw, force_trigger=None):
    """Best-effort append; provenance can never determine import success."""
    try:
        record = _normalize_import_record(raw, force_trigger=force_trigger)
        with _IMPORT_HISTORY_LOCK:
            data = load_import_history()
            data["records"] = (data.get("records", []) + [record])[-_IMPORT_HISTORY_MAX:]
            path = _import_history_path()
            directory = os.path.dirname(path) or '.'
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.import-history-', suffix='.tmp')
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w') as handle:
                    json.dump(data, handle, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, path)
                os.chmod(path, 0o600)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        return True
    except Exception:
        logging.exception("Failed to append import history")
        return False


def _candidate_history_fields(candidate):
    if candidate is None:
        return {}
    return {
        "provider": candidate.provider, "project": candidate.project,
        "releaseId": candidate.release_id, "releaseTag": candidate.release_tag,
        "assetId": candidate.asset_id, "assetName": candidate.asset_name,
    }


def _inspection_history_fields(inspection):
    if inspection is None:
        return {}
    return {
        "bundleIdentifier": inspection.bundle_identifier,
        "version": inspection.version, "buildVersion": inspection.build_version,
        "platform": inspection.platform,
    }


# ---------------------------------------------------------------------------
# Auto-import (Plan 048): an app-managed job store + in-process scheduler
# that runs the same release-ingest engine as /api/import-release above, on
# a schedule, without a host crontab or .env manifest.
# ---------------------------------------------------------------------------

_AUTO_IMPORT_WRITE_LOCK = threading.Lock()
_auto_import_run_lock = threading.Lock()
_auto_import_wake_event = threading.Event()

_AUTO_IMPORT_DEFAULT = {"enabled": False, "intervalHours": 6, "lastRunAt": None, "jobs": []}


def _auto_import_path():
    return os.path.join(DATA_DIR, "auto-import.json")


def load_auto_import():
    """Load the auto-import job store. Never raises -- a missing or corrupt
    file returns the safe default so GET /api/auto-import can never 500."""
    path = _auto_import_path()
    try:
        with open(path, 'r') as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return copy.deepcopy(_AUTO_IMPORT_DEFAULT)
        data = copy.deepcopy(_AUTO_IMPORT_DEFAULT)
        data.update(raw)
        if not isinstance(data.get('jobs'), list):
            data['jobs'] = []
        return data
    except FileNotFoundError:
        return copy.deepcopy(_AUTO_IMPORT_DEFAULT)
    except Exception as e:
        logging.error(f"Error loading auto-import store: {str(e)}")
        return copy.deepcopy(_AUTO_IMPORT_DEFAULT)


def _save_auto_import_unlocked(data):
    """Write the store while the caller owns _AUTO_IMPORT_WRITE_LOCK."""
    tmp_path = None
    try:
        path = _auto_import_path()
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".auto-import-", suffix=".json.tmp")
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        return True
    except Exception as e:
        logging.error(f"Error saving auto-import store: {str(e)}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def save_auto_import(data):
    """Atomically replace the auto-import store."""
    with _AUTO_IMPORT_WRITE_LOCK:
        return _save_auto_import_unlocked(data)


def mutate_auto_import(fn):
    """Atomically load, mutate, and save the auto-import store."""
    with _AUTO_IMPORT_WRITE_LOCK:
        data = load_auto_import()
        result = fn(data)
        if not _save_auto_import_unlocked(data):
            raise OSError("Failed to save auto-import store")
        return result


def _validate_asset_match_mode(mode):
    """Accept only the exact strings 'glob' and 'regex'. Raises ValueError
    naming both valid values otherwise."""
    if mode not in ('glob', 'regex'):
        raise ValueError(f"assetMatchMode must be 'glob' or 'regex', got {mode!r}")
    return mode


def _validate_asset_pattern(field_name, pattern, mode):
    """Validate one assetGlob/assetExcludeGlob value at save time, never at
    run time inside the scheduler. In regex mode: reject patterns over
    release_ingest.ASSET_PATTERN_MAX_LENGTH characters (a deliberate, cheap
    guard against catastrophic backtracking -- Python's `re` has no match
    timeout, so a pathological pattern would block the scheduler thread;
    these routes are admin-only via @requires_auth, so this is defence in
    depth rather than a boundary against untrusted input) and surface a bad
    pattern as ValueError, never a raw re.error."""
    if mode != 'regex' or not pattern:
        return
    if len(pattern) > release_ingest.ASSET_PATTERN_MAX_LENGTH:
        raise ValueError(
            f"{field_name} pattern is too long "
            f"(max {release_ingest.ASSET_PATTERN_MAX_LENGTH} characters)"
        )
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"{field_name} is not a valid regular expression: {e}")


def _validate_auto_import_job(raw):
    """Normalize + validate one job dict from the API. Raises ValueError
    with a user-facing message on any problem; rules mirror the engine's
    own manifest validation (see release_source_ingest.parse_manifest_dict)."""
    if not isinstance(raw, dict):
        raise ValueError("Job must be an object")

    job_id = (raw.get('id') or '').strip()
    if not job_id:
        raise ValueError("id is required")

    provider = (raw.get('provider') or '').strip().lower()
    if provider not in ('github', 'gitlab'):
        raise ValueError("provider must be 'github' or 'gitlab'")

    project = _normalize_repo_project(raw.get('project') or '')
    if not project:
        raise ValueError("project is required")
    if provider == 'github' and not release_ingest._GITHUB_PROJECT_RE.match(project):
        raise ValueError("GitHub repository must be owner/repo, e.g. RyanYuuki/AnymeX")

    bundle_id = (raw.get('bundleIdentifier') or '').strip() or None
    package = (raw.get('package') or '').strip() or None
    if package is not None and not ANDROID_PACKAGE_RE.match(package):
        raise ValueError(f"package {package!r} is not a valid Android package name")
    # Neither identity field is required: both are auto-detected from the
    # artifact. The iOS path reads bundleIdentifier out of the IPA's
    # Info.plist (_run_auto_import_ios_candidate) and the Android path lets
    # android_repo.add_apk re-derive the package from the APK's manifest.
    # When set, they act as assertions -- publish is refused if the artifact
    # disagrees -- which is why they stay optional rather than required.

    asset_glob = (raw.get('assetGlob') or '*.ipa').strip()
    if not asset_glob:
        raise ValueError("assetGlob is required")
    asset_exclude_raw = raw.get('assetExcludeGlob')
    if asset_exclude_raw is not None and not isinstance(asset_exclude_raw, str):
        raise ValueError("assetExcludeGlob must be a string")
    asset_exclude_glob = (asset_exclude_raw or '').strip() or None

    asset_match_mode = _validate_asset_match_mode(raw.get('assetMatchMode') or 'glob')
    _validate_asset_pattern('assetGlob', asset_glob, asset_match_mode)
    _validate_asset_pattern('assetExcludeGlob', asset_exclude_glob, asset_match_mode)

    hosts_in = raw.get('allowedDownloadHosts') or []
    if not isinstance(hosts_in, list):
        raise ValueError("allowedDownloadHosts must be a list of hostnames")
    hosts = [h.strip().lower() for h in hosts_in if isinstance(h, str) and h.strip()]
    if provider == 'gitlab' and not hosts:
        raise ValueError("GitLab imports require at least one allowed download host")

    create_if_missing = bool(raw.get('createIfMissing'))
    name = (raw.get('name') or '').strip() or None
    developer_name = (raw.get('developerName') or '').strip() or None
    if create_if_missing and not (name and developer_name):
        raise ValueError("createIfMissing requires a name and developerName")

    return {
        "id": job_id,
        "provider": provider,
        "project": project,
        "bundleIdentifier": bundle_id,
        "package": package,
        "assetGlob": asset_glob,
        "assetExcludeGlob": asset_exclude_glob,
        "assetMatchMode": asset_match_mode,
        "includePrereleases": bool(raw.get('includePrereleases')),
        "createIfMissing": create_if_missing,
        "name": name,
        "developerName": developer_name,
        "allowedDownloadHosts": hosts,
        "enabled": bool(raw.get('enabled', True)),
    }


def _run_auto_import_ios_candidate(job_obj, job, base_url, session_req, tokens, candidate, run_started):
    """Download, validate, and publish one selected iOS candidate. Never
    raises -- always returns a per-platform result dict and appends its own
    import-history record. Unchanged in behaviour from the pre-084
    single-candidate code path (selection now happens once, in the caller,
    for every platform the job has an identity for)."""
    tmp_path = None
    inspection = None
    digest = None
    stage = "download"
    result = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=".ipa")
        os.close(fd)
        _downloaded, digest = release_ingest.stream_download(
            session_req, candidate, job_obj, tmp_path, tokens,
            release_ingest.DEFAULT_TIMEOUT, release_ingest.DEFAULT_MAX_BYTES,
        )
        stage = "preflight"
        inspection = release_ingest.inspect_ipa_metadata(
            tmp_path, candidate.asset_name, job_obj, candidate)
        bundle_id = inspection.bundle_identifier
        version = inspection.version
        # A configured bundleIdentifier is a check, as in the one-off import
        # and the cron importer: an upstream IPA claiming another app's ID
        # must not become a new version of that other app.
        if job_obj.bundle_identifier and bundle_id != job_obj.bundle_identifier:
            raise ValueError(f"IPA bundle id {bundle_id} does not match the job's bundleIdentifier "
                             f"({job_obj.bundle_identifier}); not published")
        detected_name = inspection.name
        preflight = {
            "platform": inspection.platform,
            "bundleIdentifier": bundle_id,
            "name": detected_name,
            "version": version,
            "buildVersion": inspection.build_version,
            "minOSVersion": inspection.minimum_os_version,
            "deviceFamilies": list(inspection.device_families),
            "privacyKeys": sorted(inspection.privacy),
        }

        existing = source_manager.get_app(bundle_id)
        already_present = bool(existing) and any(
            isinstance(v, dict) and v.get('version') == version for v in existing.get('versions', [])
        )
        if already_present:
            result = {"status": "skipped", "version": version, "preflight": preflight}
        else:
            stage = "publish"
            ok = False
            message = ""
            fs = FileStorage(stream=open(tmp_path, "rb"), filename=f"{secure_filename(version)}.ipa")
            if existing:
                ok, message = source_manager.add_version(
                    bundle_id, {
                        "version": version,
                        "buildVersion": inspection.build_version,
                        "minOSVersion": inspection.minimum_os_version,
                    }, ipa_file=fs, base_url=base_url)
                if ok:
                    _apply_icon_to_existing(
                        bundle_id, existing, tmp_path,
                        provider=job_obj.provider, project=job_obj.project,
                        icon_url_in="", base_url=base_url,
                    )
            elif job_obj.create_if_missing:
                new_name = job_obj.name or detected_name or bundle_id
                new_developer = job_obj.developer_name or _repo_owner(job_obj.project) or "Unknown"
                new_app = {"name": new_name, "bundleIdentifier": bundle_id,
                           "developerName": new_developer, "version": version,
                           "buildVersion": inspection.build_version,
                           "minOSVersion": inspection.minimum_os_version}
                if inspection.privacy:
                    new_app["appPermissions"] = {
                        "entitlements": [], "privacy": inspection.privacy,
                    }
                description = _clean_description(getattr(candidate, "release_body", ""))
                if description:
                    new_app["localizedDescription"] = description
                extracted_icon = _extract_ipa_icon(tmp_path)
                try:
                    if extracted_icon:
                        icon_fs = FileStorage(stream=open(extracted_icon, "rb"), filename="icon.png")
                        ok, message = source_manager.add_app_manual(
                            new_app, ipa_file=fs, icon_file=icon_fs, base_url=base_url)
                    else:
                        # Fallback: GitHub owner avatar, same as import-release.
                        # GitLab avatars need an extra API call, so GitLab
                        # auto-imports simply go iconless here.
                        owner = _repo_owner(job_obj.project) if job_obj.provider == "github" else ""
                        if owner:
                            ok, message = _add_app_with_avatar_icon(new_app, fs, owner, base_url)
                        else:
                            ok, message = source_manager.add_app_manual(new_app, ipa_file=fs, base_url=base_url)
                finally:
                    if extracted_icon:
                        try:
                            os.remove(extracted_icon)
                        except OSError:
                            pass
            else:
                message = (f"App {bundle_id} is not in the catalog. Tick 'create if missing' "
                           "with a name + developer to add it.")

            if ok:
                notify("add_version",
                       f"Auto-imported {bundle_id} {version} from {job_obj.provider}:{job_obj.project}\n{base_url}/source.json")
                result = {"status": "published", "version": version, "preflight": preflight}
            else:
                result = {"status": "error", "message": message}
    except Exception as e:
        result = {"status": "error", "message": str(e)}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    history = {
        "trigger": "auto", "jobId": job.get('id'),
        "status": result.get("status") if result and result.get("status") in ("published", "skipped") else "error",
        "stage": stage,
        "durationMs": int((time.monotonic() - run_started) * 1000),
        "message": result.get("message") if result else None,
        **_candidate_history_fields(candidate),
        **_inspection_history_fields(inspection),
    }
    if digest:
        history["sha256"] = digest
    append_import_record(history)
    return result


def _run_auto_import_android_candidate(job_obj, job, session_req, tokens, candidate, run_started):
    """Download, validate, and publish one selected Android candidate, via
    the same in-process android_repo.add_apk pair /api/android/add-apk uses
    -- never a second HTTP hop through that route. Never raises -- always
    returns a per-platform result dict and appends its own import-history
    record. Treats add_apk reporting this versionCode as already present as
    an idempotent skip, not a failure, and never notifies twice (add_apk's
    own metadata seeding runs, but this function -- not the route -- is the
    only place that calls `notify`, matching one artifact = one
    notification)."""
    tmp_path = None
    inspection = None
    digest = None
    stage = "download"
    result = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=".apk")
        os.close(fd)
        _downloaded, digest = release_ingest.stream_download(
            session_req, candidate, job_obj, tmp_path, tokens,
            release_ingest.DEFAULT_TIMEOUT, release_ingest.DEFAULT_MAX_BYTES,
        )
        stage = "preflight"
        inspection = release_ingest.inspect_apk_metadata(
            tmp_path, candidate.asset_name, job_obj, candidate)
        preflight = {
            "platform": "android",
            "package": inspection.package,
            "versionCode": inspection.version_code,
            "versionName": inspection.version_name,
            "minSdk": inspection.min_sdk,
            "targetSdk": inspection.target_sdk,
            "appName": inspection.app_name,
        }
        stage = "publish"
        # android_repo.add_apk re-derives identity from the file itself (it
        # cannot accept a pre-computed ApkInspection); the extra pyaxmlparser
        # pass is the cost of reusing the exact route helper rather than
        # re-implementing the move-into-place + idempotency logic here.
        added, info = android_repo.add_apk(tmp_path, expected_package=job_obj.package)
        if not added:
            result = {
                "status": "skipped", "package": info["package"],
                "versionCode": info["version_code"], "versionName": info["version_name"],
                "preflight": preflight,
            }
        else:
            # Metadata (Name/Summary/...) is not part of a watcher job today
            # -- android_repo.add_apk already seeds a default Name for a
            # brand-new package, matching /api/android/add-apk's own
            # `if fields: write_metadata(...)` (empty fields here -> no call).
            notify("android_add_apk",
                   f"Auto-imported Android APK {info['package']} {info['version_name']} "
                   f"from {job_obj.provider}:{job_obj.project}")
            result = {
                "status": "published", "package": info["package"],
                "versionCode": info["version_code"], "versionName": info["version_name"],
                "preflight": preflight,
            }
    except Exception as e:
        result = {"status": "error", "message": str(e)}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    history = {
        "trigger": "auto", "jobId": job.get('id'),
        "status": result.get("status") if result and result.get("status") in ("published", "skipped") else "error",
        "stage": stage,
        "platform": "android",
        "durationMs": int((time.monotonic() - run_started) * 1000),
        "message": result.get("message") if result else None,
        **_candidate_history_fields(candidate),
    }
    if result and result.get("package"):
        history["bundleIdentifier"] = result["package"]
    if result and result.get("versionName"):
        history["version"] = result["versionName"]
    if digest:
        history["sha256"] = digest
    append_import_record(history)
    return result


def _summarize_auto_import_platforms(platforms):
    """`platforms`: {"ios": result, "android": result} for whichever
    platforms select_candidate found a match for -- each already run and
    already appended its own import-history record. Returns the dict stored
    as the job's lastResult.

    Always carries the full per-platform breakdown under "platforms" so a
    publish on one platform can never be masked by a skip/error on the
    other. When the job only has one platform's identity configured --
    every job stored before this plan, plus any new job that stays
    single-platform -- the legacy top-level status/version/message/
    preflight keys are also set, mirroring the pre-084 shape exactly (extra
    keys, never removed ones, so existing callers reading result["status"]
    / result["version"] are unaffected)."""
    if not platforms:
        return {"status": "error", "message": "No matching release asset found", "platforms": {}}
    if len(platforms) == 1:
        ((_, only),) = platforms.items()
        legacy = dict(only)
        legacy["platforms"] = platforms
        return legacy

    statuses = {r.get("status") for r in platforms.values()}
    if "published" in statuses:
        overall = "published"
    elif "error" in statuses:
        overall = "error"
    else:
        overall = "skipped"
    messages = [
        f"{platform}: {r.get('message')}" for platform, r in platforms.items()
        if r.get("status") == "error" and r.get("message")
    ]
    return {
        "status": overall,
        "message": "; ".join(messages) or None,
        "platforms": platforms,
    }


def _run_auto_import_job(job, base_url, session_req, tokens):
    """Run one auto-import job end to end for every platform it has an
    identity for (select -> download -> extract -> publish), reusing the
    exact release_ingest + source_manager/android_repo calls the
    /api/import-release route above uses. Never raises -- always returns a
    result dict (see `_summarize_auto_import_platforms`), and persists it
    onto the stored job's lastRunAt/lastResult.

    plan 083's `select_candidate` returns at most one candidate per
    platform; each is downloaded, validated, and published independently
    (`_run_auto_import_ios_candidate` / `_run_auto_import_android_candidate`)
    so one platform's failure or skip never blocks or hides the other."""
    run_started = time.monotonic()
    job_obj = release_ingest.Job(
        id=job.get('id') or '',
        provider=job.get('provider') or '',
        project=job.get('project') or '',
        bundle_identifier=job.get('bundleIdentifier') or None,
        package=job.get('package') or None,
        asset_glob=job.get('assetGlob') or "*.ipa",
        asset_exclude_glob=job.get('assetExcludeGlob') or None,
        asset_match_mode=job.get('assetMatchMode') or "glob",
        include_prereleases=bool(job.get('includePrereleases')),
        create_if_missing=bool(job.get('createIfMissing')),
        allowed_download_hosts=frozenset(
            h.strip().lower() for h in (job.get('allowedDownloadHosts') or []) if h.strip()
        ),
        name=job.get('name') or None,
        developer_name=job.get('developerName') or None,
    )

    try:
        candidates = release_ingest.select_candidate(job_obj, session_req, tokens, timeout=30)
    except Exception as e:
        result = {"status": "error", "message": str(e), "platforms": {}}
        append_import_record({
            "trigger": "auto", "jobId": job.get('id'),
            "status": "error", "stage": "selection",
            "durationMs": int((time.monotonic() - run_started) * 1000),
            "message": str(e),
        })
        _persist_auto_import_job_result(job, result)
        return result

    platforms = {}
    for candidate in candidates or []:
        if candidate.platform == "ios":
            platforms["ios"] = _run_auto_import_ios_candidate(
                job_obj, job, base_url, session_req, tokens, candidate, run_started)
        elif candidate.platform == "android":
            platforms["android"] = _run_auto_import_android_candidate(
                job_obj, job, session_req, tokens, candidate, run_started)

    result = _summarize_auto_import_platforms(platforms)
    _persist_auto_import_job_result(job, result)
    return result


def _persist_auto_import_job_result(job, result):
    """Persist lastRunAt/lastResult onto the stored job. Best-effort --
    never raises, matching `_run_auto_import_job`'s own contract."""
    try:
        def stamp_job(data):
            now = datetime.now(timezone.utc).isoformat()
            for stored in data.get('jobs', []):
                if stored.get('id') == job.get('id'):
                    stored['lastRunAt'] = now
                    stored['lastResult'] = result
                    break

        mutate_auto_import(stamp_job)
    except Exception:
        logging.exception("Failed to persist auto-import job result for %s", job.get('id'))


def _run_all_enabled_jobs(base_url):
    """Run every enabled job once, isolated (one job's failure never stops
    the others), then stamp the top-level lastRunAt."""
    data = load_auto_import()
    jobs = [j for j in data.get('jobs', []) if j.get('enabled', True)]
    session_req = requests.Session()
    tokens = {"github": _env("GITHUB_TOKEN"), "gitlab": _env("GITLAB_TOKEN")}
    results = {}
    for job in jobs:
        try:
            results[job.get('id')] = _run_auto_import_job(job, base_url, session_req, tokens)
        except Exception as e:
            # _run_auto_import_job already catches internally, but guard the
            # loop itself too so one pathological job can never abort the batch.
            results[job.get('id')] = {"status": "error", "message": str(e)}

    mutate_auto_import(
        lambda data: data.__setitem__('lastRunAt', datetime.now(timezone.utc).isoformat())
    )
    return results


def _auto_import_due(data, now):
    """Return whether an enabled schedule is due at aware UTC `now`."""
    if not data.get('enabled'):
        return False
    interval_hours = data.get('intervalHours') or 6
    last_run_at = data.get('lastRunAt')
    if not last_run_at:
        return True
    try:
        last_dt = datetime.fromisoformat(last_run_at)
    except (TypeError, ValueError):
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return (now - last_dt).total_seconds() >= interval_hours * 3600


def _auto_import_scheduler_cycle():
    """Run one scheduler decision cycle and return its polling interval."""
    data = load_auto_import()
    interval_hours = data.get('intervalHours') or 6
    if not _auto_import_due(data, datetime.now(timezone.utc)):
        return interval_hours
    if not _auto_import_run_lock.acquire(blocking=False):
        return interval_hours
    try:
        base_url = _safe_base_url()
        if base_url is None:
            logging.warning(
                "auto-import scheduler: PUBLIC_BASE_URL is not set and there is no "
                "request context to derive a base URL from -- skipping this cycle. "
                "Set PUBLIC_BASE_URL to enable scheduled imports."
            )
        else:
            _run_all_enabled_jobs(base_url)
    finally:
        _auto_import_run_lock.release()
    return interval_hours


def _auto_import_scheduler_loop(stop_event):
    """Daemon loop: each cycle, run enabled jobs if intervalHours has
    elapsed since lastRunAt (or lastRunAt is unset), then wait up to a short
    poll interval so config changes (POST /api/auto-import/config) are
    picked up promptly -- that route also sets _auto_import_wake_event to
    wake this loop early. Started ONLY from `__main__` at the bottom of this
    file -- see _start_auto_import_scheduler."""
    while not stop_event.is_set():
        interval_hours = 6
        try:
            interval_hours = _auto_import_scheduler_cycle()
        except Exception:
            logging.exception("auto-import scheduler cycle failed")

        wait_s = min(max(interval_hours, 1) * 3600, 300)
        _auto_import_wake_event.wait(timeout=wait_s)
        _auto_import_wake_event.clear()


def _start_auto_import_scheduler():
    """Start the auto-import scheduler daemon thread. Call ONLY from
    `__main__` -- never at module import time, or the test suite (which
    imports this module) would spawn a background thread on every test run."""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_auto_import_scheduler_loop, args=(stop_event,),
        daemon=True, name="auto-import-scheduler",
    )
    thread.start()
    return thread, stop_event


def _stream_download_with_progress(session_req, candidate, job, tmp_path, tokens):
    """Run release_ingest.stream_download in a worker thread while yielding
    live progress, so a generator-based SSE route can `for kind, a, b in
    ...: yield event(...)` without duplicating the queue/thread plumbing per
    platform. Yields ("downloading", downloaded, total) while in flight,
    then exactly one of ("ok", downloaded, sha256_hex) or ("err", message,
    None) and stops."""
    import queue
    q = queue.Queue()

    def cb(total, declared):
        q.put(("downloading", total, declared))

    def worker():
        try:
            downloaded_result = release_ingest.stream_download(
                session_req, candidate, job, tmp_path, tokens,
                release_ingest.DEFAULT_TIMEOUT, release_ingest.DEFAULT_MAX_BYTES,
                progress_cb=cb,
            )
            q.put(("ok", downloaded_result[0], downloaded_result[1]))
        except Exception as e:
            q.put(("err", str(e), None))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        while True:
            kind, a, b = q.get()
            if kind == "downloading":
                yield ("downloading", a, b)
            else:
                yield (kind, a, b)
                return
    finally:
        t.join()


def _release_job_from_payload(payload, job_id="ui-import"):
    """Normalize the provider/selector fields shared by inspect and import."""
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")
    provider = (payload.get('provider') or '').strip().lower()
    project = _normalize_repo_project(payload.get('project') or '')
    if provider not in ('github', 'gitlab'):
        raise ValueError("Provider must be github or gitlab")
    if not project:
        raise ValueError("Repository is required")
    if provider == 'github' and not release_ingest._GITHUB_PROJECT_RE.match(project):
        raise ValueError("GitHub repository must be owner/repo, e.g. RyanYuuki/AnymeX")

    package_raw = payload.get('package')
    package = (package_raw or '').strip() or None
    if package is not None and not ANDROID_PACKAGE_RE.match(package):
        raise ValueError(f"package {package!r} is not a valid Android package name")

    asset_glob_raw = payload.get('assetGlob', '*.ipa')
    if not isinstance(asset_glob_raw, str) or not asset_glob_raw.strip():
        raise ValueError("Asset glob is required")
    exclude_raw = payload.get('assetExcludeGlob')
    if exclude_raw is not None and not isinstance(exclude_raw, str):
        raise ValueError("Asset exclude glob must be a string")
    asset_match_mode = _validate_asset_match_mode(payload.get('assetMatchMode') or 'glob')
    _validate_asset_pattern('assetGlob', asset_glob_raw.strip(), asset_match_mode)
    _validate_asset_pattern(
        'assetExcludeGlob', (exclude_raw or '').strip() or None, asset_match_mode
    )
    hosts_in = payload.get('allowedDownloadHosts') or []
    if not isinstance(hosts_in, list) or any(not isinstance(h, str) for h in hosts_in):
        raise ValueError("Allowed download hosts must be a list of hostnames")
    allowed = frozenset(h.strip().lower() for h in hosts_in if h.strip())
    for host in allowed:
        release_ingest._validate_download_host(job_id, host)
    if provider == 'gitlab' and not allowed:
        raise ValueError("GitLab imports require at least one allowed download host")

    return release_ingest.Job(
        id=job_id,
        provider=provider,
        project=project,
        bundle_identifier=(payload.get('bundleIdentifier') or '').strip(),
        package=package,
        asset_glob=asset_glob_raw.strip(),
        asset_exclude_glob=(exclude_raw or '').strip() or None,
        asset_match_mode=asset_match_mode,
        include_prereleases=bool(payload.get('includePrereleases')),
        create_if_missing=bool(payload.get('createIfMissing')),
        allowed_download_hosts=allowed,
        name=(payload.get('name') or '').strip() or None,
        developer_name=(payload.get('developerName') or '').strip() or None,
    )


@app.route('/api/import-release/inspect', methods=['POST'])
@requires_auth
def inspect_import_release():
    payload = request.get_json(silent=True) or {}
    try:
        job = _release_job_from_payload(payload, job_id="ui-inspect")
        tokens = {
            "github": _env("GITHUB_TOKEN"),
            "gitlab": _env("GITLAB_TOKEN"),
        }
        releases = release_ingest.inspect_release_assets(
            job, requests.Session(), tokens, timeout=30, limit=5
        )
        # Label each asset's inferred platform for the UI -- inspect_release_assets
        # itself doesn't (plan 083 scope), so tag it here with the engine's own
        # extension-inference helper rather than re-implementing it.
        for release in releases:
            for asset in release.get("assets") or []:
                asset["platform"] = release_ingest._platform_for_asset(asset.get("name"))
        return jsonify({"releases": releases})
    except (ValueError, release_ingest.ConfigError, release_ingest.ProviderError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logging.exception("import-release inspection failed")
        return jsonify({"error": "Asset inspection failed"}), 500


@app.route('/api/import-release', methods=['POST'])
@requires_auth
def import_release():
    payload = request.get_json(silent=True) or {}

    def text(key, default=''):
        # A non-string (list, number, object) is treated as absent so the
        # validation below rejects it, instead of .strip() raising a 500.
        value = payload.get(key)
        return value.strip() if isinstance(value, str) else default

    provider = text('provider').lower()
    project = _normalize_repo_project(text('project'))
    bundle_id_in = text('bundleIdentifier')
    asset_glob = text('assetGlob') or '*.ipa'
    asset_exclude_glob = payload.get('assetExcludeGlob')
    include_pre = bool(payload.get('includePrereleases'))
    create_if_missing = bool(payload.get('createIfMissing'))
    name_in = text('name')
    developer_in = text('developerName')
    icon_url_in = text('iconURL')
    hosts_in = payload.get('allowedDownloadHosts') or []
    base_url = resolve_base_url()
    started = time.monotonic()

    def event(**kw):
        return json.dumps(kw) + "\n"

    def handle_ios(job, candidate, session_req, tokens):
        """Download, validate, and publish one selected iOS candidate as SSE
        events. Unchanged in behaviour from the pre-084 single-candidate
        code path; every event carries a new "platform" field but no
        existing field is renamed, matching what templates/index.html's
        preflight/done handlers already read. Appends its own
        import-history record."""
        tmp_path = None
        inspection = None
        digest = None
        history_stage = "download"
        history_status = "error"
        history_message = None
        try:
            yield event(stage="resolved", platform="ios", asset=candidate.asset_name,
                        release=candidate.release_tag or candidate.release_id, size=candidate.declared_size)
            fd, tmp_path = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=".ipa")
            os.close(fd)
            for kind, a, b in _stream_download_with_progress(session_req, candidate, job, tmp_path, tokens):
                if kind == "downloading":
                    pct = int(a * 100 / b) if b else None
                    yield event(stage="downloading", platform="ios", downloaded=a, total=b, pct=pct)
                elif kind == "ok":
                    digest = b
                else:
                    history_message = a
                    yield event(stage="error", platform="ios", error=a)
                    return
            yield event(stage="validating", platform="ios")
            history_stage = "preflight"
            inspection = release_ingest.inspect_ipa_metadata(
                tmp_path, candidate.asset_name, job, candidate)
            bundle_id = inspection.bundle_identifier
            version = inspection.version
            detected_name = inspection.name
            if bundle_id_in and bundle_id != bundle_id_in:
                yield event(stage="error", platform="ios",
                            error=f"IPA bundle id {bundle_id} does not match the id you entered ({bundle_id_in})")
                history_message = "IPA bundle identifier did not match the configured value"
                return
            yield event(
                stage="preflight",
                platform=inspection.platform,
                bundleIdentifier=bundle_id,
                name=detected_name,
                version=version,
                buildVersion=inspection.build_version,
                minOSVersion=inspection.minimum_os_version,
                deviceFamilies=list(inspection.device_families),
                privacyKeys=sorted(inspection.privacy),
            )
            yield event(stage="publishing", platform="ios")
            history_stage = "publish"
            existing = source_manager.get_app(bundle_id)
            fs = FileStorage(stream=open(tmp_path, "rb"), filename=f"{secure_filename(version)}.ipa")
            if existing:
                ok, message = source_manager.add_version(bundle_id, {
                    "version": version,
                    "buildVersion": inspection.build_version,
                    "minOSVersion": inspection.minimum_os_version,
                }, ipa_file=fs, base_url=base_url)
                if ok:
                    _apply_icon_to_existing(
                        bundle_id, existing, tmp_path,
                        provider=provider, project=project,
                        icon_url_in=icon_url_in, base_url=base_url,
                    )
            elif create_if_missing:
                new_name = name_in or detected_name or bundle_id
                new_developer = developer_in or _repo_owner(project) or "Unknown"
                new_app = {"name": new_name, "bundleIdentifier": bundle_id,
                           "developerName": new_developer, "version": version,
                           "buildVersion": inspection.build_version,
                           "minOSVersion": inspection.minimum_os_version}
                if inspection.privacy:
                    new_app["appPermissions"] = {
                        "entitlements": [], "privacy": inspection.privacy,
                    }
                description = _clean_description(getattr(candidate, "release_body", ""))
                if description:
                    new_app["localizedDescription"] = description
                extracted_icon = None
                if not icon_url_in:
                    extracted_icon = _extract_ipa_icon(tmp_path)
                try:
                    if icon_url_in:
                        new_app["iconURL"] = icon_url_in
                        ok, message = source_manager.add_app_manual(
                            new_app, ipa_file=fs, download_icon_from_url=True, base_url=base_url)
                    elif extracted_icon:
                        icon_fs = FileStorage(stream=open(extracted_icon, "rb"), filename="icon.png")
                        ok, message = source_manager.add_app_manual(
                            new_app, ipa_file=fs, icon_file=icon_fs, base_url=base_url)
                    else:
                        # Fallback: GitHub owner avatar. GitLab avatars need an
                        # extra API call, so GitLab imports simply go iconless here.
                        owner = _repo_owner(project) if provider == "github" else ""
                        if owner:
                            ok, message = _add_app_with_avatar_icon(new_app, fs, owner, base_url)
                        else:
                            ok, message = source_manager.add_app_manual(new_app, ipa_file=fs, base_url=base_url)
                finally:
                    if extracted_icon:
                        try:
                            os.remove(extracted_icon)
                        except OSError:
                            pass
            else:
                yield event(stage="error", platform="ios",
                            error=f"App {bundle_id} is not in the catalog. Tick 'create if missing' with a name + developer to add it.")
                return
            if ok:
                notify("add_version", f"Imported {bundle_id} {version} from {provider}:{project}\n{base_url}/source.json")
                history_status = "published"
                yield event(stage="done", platform="ios", success=True, message=message, bundleIdentifier=bundle_id, version=version)
            else:
                history_message = message
                yield event(stage="error", platform="ios", error=message)
        except (ValueError, release_ingest.ProviderError, release_ingest.ValidationError, release_ingest.ConfigError) as e:
            history_message = str(e)
            yield event(stage="error", platform="ios", error=str(e))
        except Exception:
            logging.exception("import-release (ios) failed")
            history_message = "Import failed"
            yield event(stage="error", platform="ios", error="Import failed")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            history = {
                "trigger": "one-off", "status": history_status,
                "stage": history_stage,
                "durationMs": int((time.monotonic() - started) * 1000),
                "provider": provider, "project": project,
                "message": history_message,
                **_candidate_history_fields(candidate),
                **_inspection_history_fields(inspection),
            }
            if digest:
                history["sha256"] = digest
            append_import_record(history)

    def handle_android(job, candidate, session_req, tokens):
        """Download, validate, and publish one selected Android candidate as
        SSE events tagged platform="android", reusing the bundleIdentifier/
        version field names (holding package/versionName) so no event field
        the frontend already reads is renamed -- see handle_ios's docstring.
        Publishes through the exact android_repo.add_apk/write_metadata pair
        /api/android/add-apk uses -- never a second HTTP hop through that
        route -- and treats add_apk reporting this versionCode as already
        present as an idempotent skip, not a failure. Appends its own
        import-history record."""
        tmp_path = None
        inspection = None
        digest = None
        history_stage = "download"
        history_status = "error"
        history_message = None
        try:
            yield event(stage="resolved", platform="android", asset=candidate.asset_name,
                        release=candidate.release_tag or candidate.release_id, size=candidate.declared_size)
            fd, tmp_path = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=".apk")
            os.close(fd)
            for kind, a, b in _stream_download_with_progress(session_req, candidate, job, tmp_path, tokens):
                if kind == "downloading":
                    pct = int(a * 100 / b) if b else None
                    yield event(stage="downloading", platform="android", downloaded=a, total=b, pct=pct)
                elif kind == "ok":
                    digest = b
                else:
                    history_message = a
                    yield event(stage="error", platform="android", error=a)
                    return
            yield event(stage="validating", platform="android")
            history_stage = "preflight"
            inspection = release_ingest.inspect_apk_metadata(
                tmp_path, candidate.asset_name, job, candidate)
            yield event(
                stage="preflight",
                platform="android",
                bundleIdentifier=inspection.package,
                name=inspection.app_name,
                version=inspection.version_name,
                buildVersion=str(inspection.version_code),
                minOSVersion=inspection.min_sdk,
                deviceFamilies=[],
                privacyKeys=[],
            )
            yield event(stage="publishing", platform="android")
            history_stage = "publish"
            added, info = android_repo.add_apk(tmp_path, expected_package=job.package)
            if added:
                notify("android_add_apk",
                       f"Imported Android APK {info['package']} {info['version_name']} from {provider}:{project}")
                history_status = "published"
                yield event(stage="done", platform="android", success=True,
                            message=f"Added {info['package']} version {info['version_code']}",
                            bundleIdentifier=info["package"], version=info["version_name"])
            else:
                history_status = "skipped"
                history_message = f"Already present: {info['package']} versionCode {info['version_code']}"
                yield event(stage="done", platform="android", success=True,
                            message=history_message,
                            bundleIdentifier=info["package"], version=info["version_name"])
        except (ValueError, release_ingest.ProviderError, release_ingest.ValidationError, release_ingest.ConfigError) as e:
            history_message = str(e)
            yield event(stage="error", platform="android", error=str(e))
        except Exception:
            logging.exception("import-release (android) failed")
            history_message = "Import failed"
            yield event(stage="error", platform="android", error="Import failed")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            history = {
                "trigger": "one-off", "status": history_status,
                "stage": history_stage,
                "platform": "android",
                "durationMs": int((time.monotonic() - started) * 1000),
                "provider": provider, "project": project,
                "message": history_message,
                **_candidate_history_fields(candidate),
            }
            if inspection is not None:
                history["bundleIdentifier"] = inspection.package
                history["version"] = inspection.version_name
            if digest:
                history["sha256"] = digest
            append_import_record(history)

    def run():
        try:
            job = _release_job_from_payload(payload)
            tokens = {"github": _env("GITHUB_TOKEN"), "gitlab": _env("GITLAB_TOKEN")}
            session_req = requests.Session()
            yield event(stage="resolving")
            candidates = release_ingest.select_candidate(job, session_req, tokens, timeout=30)
        except (ValueError, release_ingest.ProviderError, release_ingest.ValidationError, release_ingest.ConfigError) as e:
            yield event(stage="error", error=str(e))
            append_import_record({
                "trigger": "one-off", "status": "error", "stage": "selection",
                "durationMs": int((time.monotonic() - started) * 1000),
                "provider": provider, "project": project, "message": str(e),
            })
            return
        except Exception:
            logging.exception("import-release failed")
            yield event(stage="error", error="Import failed")
            append_import_record({
                "trigger": "one-off", "status": "error", "stage": "selection",
                "durationMs": int((time.monotonic() - started) * 1000),
                "provider": provider, "project": project, "message": "Import failed",
            })
            return

        for candidate in candidates or []:
            if candidate.platform == "ios":
                yield from handle_ios(job, candidate, session_req, tokens)
            elif candidate.platform == "android":
                yield from handle_android(job, candidate, session_req, tokens)

    return app.response_class(run(), mimetype="application/x-ndjson")


@app.route('/api/import-history/record', methods=['POST'])
@requires_auth
def record_import_history():
    payload = request.get_json(silent=True)
    try:
        normalized = _normalize_import_record(payload, force_trigger="standalone")
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    if not append_import_record(normalized, force_trigger="standalone"):
        return jsonify({"success": False, "error": "Could not record import history"}), 500
    return jsonify({"success": True})


@app.route('/api/import-history', methods=['GET'])
@requires_auth
def get_import_history():
    try:
        limit = int(request.args.get('limit', '20'))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    if limit < 1 or limit > 50:
        return jsonify({"error": "limit must be between 1 and 50"}), 400
    filters = {
        "bundleIdentifier": request.args.get('bundleIdentifier'),
        "version": request.args.get('version'),
        "jobId": request.args.get('jobId'),
    }
    for value in filters.values():
        if value is not None and (not value or len(value) > 256 or any(ord(ch) < 32 for ch in value)):
            return jsonify({"error": "Invalid history filter"}), 400
    records = load_import_history().get('records', [])
    records = [
        record for record in reversed(records)
        if all(value is None or record.get(key) == value for key, value in filters.items())
    ][:limit]
    return jsonify({"records": records})


@app.route('/api/import-provenance/<bundle_id>/<version>', methods=['GET'])
@requires_auth
def get_import_provenance(bundle_id, version):
    for record in reversed(load_import_history().get('records', [])):
        if (record.get('status') == 'published'
                and record.get('bundleIdentifier') == bundle_id
                and record.get('version') == version):
            return jsonify(record)
    return jsonify({"error": "Import provenance not found"}), 404


@app.route('/api/auto-import', methods=['GET'])
@requires_auth
def get_auto_import():
    return jsonify(load_auto_import())


@app.route('/api/auto-import/config', methods=['POST'])
@requires_auth
def auto_import_config():
    payload = request.get_json(silent=True) or {}
    interval = None
    if 'intervalHours' in payload:
        try:
            interval = int(payload['intervalHours'])
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "intervalHours must be an integer"}), 400
        if interval < 1 or interval > 168:
            return jsonify({"success": False, "error": "intervalHours must be between 1 and 168"}), 400

    def update_config(data):
        if interval is not None:
            data['intervalHours'] = interval
        if 'enabled' in payload:
            data['enabled'] = bool(payload['enabled'])
        return copy.deepcopy(data)

    data = mutate_auto_import(update_config)
    _auto_import_wake_event.set()
    return jsonify(data)


@app.route('/api/auto-import/job', methods=['POST'])
@requires_auth
def auto_import_upsert_job():
    payload = request.get_json(silent=True) or {}
    try:
        job = _validate_auto_import_job(payload)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    def upsert(data):
        jobs = data.setdefault('jobs', [])
        existing_idx = next((i for i, stored in enumerate(jobs) if stored.get('id') == job['id']), None)
        stored_job = dict(job)
        if existing_idx is not None:
            stored_job['lastRunAt'] = jobs[existing_idx].get('lastRunAt')
            stored_job['lastResult'] = jobs[existing_idx].get('lastResult')
            jobs[existing_idx] = stored_job
        else:
            stored_job['lastRunAt'] = None
            stored_job['lastResult'] = None
            jobs.append(stored_job)
        return stored_job

    stored_job = mutate_auto_import(upsert)
    return jsonify({"success": True, "job": stored_job})


@app.route('/api/auto-import/job/delete', methods=['POST'])
@requires_auth
def auto_import_delete_job():
    payload = request.get_json(silent=True) or {}
    job_id = (payload.get('id') or '').strip()
    def delete(data):
        jobs = data.get('jobs', [])
        remaining = [job for job in jobs if job.get('id') != job_id]
        if len(remaining) == len(jobs):
            return False
        data['jobs'] = remaining
        return True

    if not mutate_auto_import(delete):
        return jsonify({"success": False, "error": "Job not found"}), 404
    return jsonify({"success": True})


@app.route('/api/auto-import/run', methods=['POST'])
@requires_auth
def auto_import_run():
    if not _auto_import_run_lock.acquire(blocking=False):
        return jsonify({"status": "busy"})
    try:
        payload = request.get_json(silent=True) or {}
        job_id = (payload.get('id') or '').strip()
        base_url = resolve_base_url()
        tokens = {"github": _env("GITHUB_TOKEN"), "gitlab": _env("GITLAB_TOKEN")}
        if job_id:
            data = load_auto_import()
            job = next((j for j in data.get('jobs', []) if j.get('id') == job_id), None)
            if not job:
                return jsonify({"success": False, "error": "Job not found"}), 404
            session_req = requests.Session()
            result = _run_auto_import_job(job, base_url, session_req, tokens)
            return jsonify({"status": "done", "results": {job_id: result}})
        else:
            results = _run_all_enabled_jobs(base_url)
            return jsonify({"status": "done", "results": results})
    finally:
        _auto_import_run_lock.release()


@app.route('/api/update-version', methods=['POST'])
@requires_auth
def update_version():
    try:
        base_url = resolve_base_url()
        # Check if request is form-data (file upload) or JSON
        if request.content_type and 'multipart/form-data' in request.content_type:
            # Form data request
            ipa_file = request.files.get('ipaFile')
            download_from_url = request.form.get('downloadFromUrl', 'false').lower() == 'true'
            bundle_id = request.form.get('bundleIdentifier')
            version = request.form.get('version')
            data = {
                'downloadURL': request.form.get('downloadURL', ''),
                'minOSVersion': request.form.get('minOSVersion', '')
            }
        else:
            # JSON request (backward compatibility)
            data = request.get_json() if request.is_json else {}
            bundle_id = data.get('bundleIdentifier')
            version = data.get('version')
            ipa_file = None
            download_from_url = False
        
        if not bundle_id:
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400
        
        if not version:
            return jsonify({"success": False, "error": "Version is required"}), 400
        
        # Allow updating just the URL without requiring file upload
        if not ipa_file and not data.get('downloadURL') and not download_from_url:
            return jsonify({"success": False, "error": "Download URL is required"}), 400
        
        success, message = source_manager.update_version(bundle_id, version, data, ipa_file=ipa_file if ipa_file and ipa_file.filename else None, download_from_url=download_from_url, base_url=base_url)
        
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
            
    except Exception as e:
        logging.error(f"Error updating version: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/delete-version', methods=['POST'])
@requires_auth
def delete_version():
    try:
        data = request.json
        bundle_id = data.get('bundleIdentifier')
        version = data.get('version')

        if not bundle_id:
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400

        if not version:
            return jsonify({"success": False, "error": "Version is required"}), 400

        success, message = source_manager.delete_version(bundle_id, version)

        if success:
            notify("delete_version", f"Version removed: {bundle_id} {version}")
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400

    except Exception as e:
        logging.error(f"Error deleting version: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/update-source', methods=['POST'])
@requires_auth
def update_source():
    try:
        data = request.json
        success, message = source_manager.update_source_info(data)
        
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
            
    except Exception as e:
        logging.error(f"Error updating source: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400


@app.route('/api/editorial', methods=['GET'])
@requires_auth
def get_editorial():
    try:
        source_data = source_manager.load_source()
        if not isinstance(source_data, dict) or not isinstance(source_data.get('apps'), list):
            raise ValueError("invalid catalog")
        featured = source_data.get('featuredApps', [])
        news = source_data.get('news', [])
        if not isinstance(featured, list) or not isinstance(news, list):
            raise ValueError("invalid editorial data")
        apps = [
            {"bundleIdentifier": entry.get('bundleIdentifier'), "name": entry.get('name')}
            for entry in source_data['apps'] if isinstance(entry, dict)
        ]
        news = sorted(news, key=lambda entry: entry.get('date', '') if isinstance(entry, dict) else '', reverse=True)
        return jsonify({"success": True, "apps": apps, "featuredApps": featured, "news": news})
    except Exception:
        logging.exception("Failed to load editorial data")
        return jsonify({"success": False, "error": "Editorial data is unavailable"}), 500


@app.route('/api/featured-apps', methods=['POST'])
@requires_auth
def update_featured_apps():
    try:
        success, message, values = source_manager.update_featured_apps(request.get_json(silent=True))
        return jsonify({"success": success, "message" if success else "error": message,
                        "featuredApps": values}), 200 if success else 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route('/api/news', methods=['POST'])
@requires_auth
def upsert_news():
    try:
        success, message, item = source_manager.upsert_news_item(request.get_json(silent=True))
        return jsonify({"success": success, "message" if success else "error": message,
                        "item": item}), 200 if success else 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route('/api/news/delete', methods=['POST'])
@requires_auth
def delete_news():
    payload = request.get_json(silent=True) or {}
    identifier = payload.get('identifier')
    if not isinstance(identifier, str) or not identifier:
        return jsonify({"success": False, "error": "identifier is required"}), 400
    success, message = source_manager.delete_news_item(identifier)
    status = 200 if success else (404 if message == "News item not found" else 400)
    return jsonify({"success": success, "message" if success else "error": message}), status


_HEALTH_MONITOR_LOCK = threading.Lock()
_health_monitor_wake_event = threading.Event()
_HEALTH_MONITOR_DEFAULT = {
    "schemaVersion": 1, "enabled": False, "intervalHours": 6,
    "lastRunAt": None, "lastState": None, "snapshots": [],
}


def _health_monitor_path():
    return os.path.join(DATA_DIR, 'health-monitor.json')


def load_health_monitor():
    try:
        with open(_health_monitor_path(), 'r') as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or data.get('schemaVersion') != 1 or not isinstance(data.get('snapshots'), list):
            raise ValueError("unsupported health monitor schema")
        return data
    except FileNotFoundError:
        return copy.deepcopy(_HEALTH_MONITOR_DEFAULT)
    except Exception as exc:
        logging.warning("Could not load health monitor: %s", exc)
        return copy.deepcopy(_HEALTH_MONITOR_DEFAULT)


def _save_health_monitor_unlocked(data):
    path = _health_monitor_path()
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.health-monitor-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _record_health_snapshot(issues, trigger):
    counts = {"error": 0, "warning": 0, "info": 0}
    identities = []
    for issue in issues:
        severity = issue.get('severity')
        if severity in counts:
            counts[severity] += 1
        identities.append({
            key: issue.get(key) for key in
            ('kind', 'severity', 'bundleIdentifier', 'version')
        })
    state = 'degraded' if counts['error'] or counts['warning'] else 'healthy'
    now = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "timestamp": now, "trigger": trigger, "state": state,
        "counts": counts, "issues": identities,
    }
    previous = None
    try:
        with _HEALTH_MONITOR_LOCK:
            data = load_health_monitor()
            previous = data.get('lastState')
            data['lastRunAt'] = now
            data['lastState'] = state
            data['snapshots'] = (data.get('snapshots', []) + [snapshot])[-30:]
            _save_health_monitor_unlocked(data)
    except Exception:
        logging.exception("Failed to persist health snapshot")
        return snapshot
    if previous and previous != state:
        notify('health_transition', f"Catalog health changed from {previous} to {state}")
    return snapshot


_BACKUP_NAME_RE = re.compile(r'^source-\d{8}T\d{6}Z(?:\.\d{3})?\.json$')
_SOURCE_DIFF_FIELDS = (
    'name', 'subtitle', 'description', 'website', 'iconURL', 'headerURL',
    'tintColor', 'featuredApps', 'news',
)


def _resolve_backup_path(filename):
    if (not isinstance(filename, str) or not _BACKUP_NAME_RE.fullmatch(filename)
            or secure_filename(filename) != filename):
        raise ValueError("Invalid backup filename")
    backup_root = os.path.realpath(BACKUP_FOLDER)
    path = os.path.join(backup_root, filename)
    if os.path.islink(path) or os.path.realpath(os.path.dirname(path)) != backup_root:
        raise ValueError("Invalid backup filename")
    if not os.path.isfile(path):
        raise FileNotFoundError("Backup not found")
    return path


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_catalog_snapshot(candidate):
    if not isinstance(candidate, dict):
        return "Catalog root must be an object"
    if not isinstance(candidate.get('name'), str) or not candidate['name'].strip():
        return "Catalog name is required"
    if not isinstance(candidate.get('apps'), list) or not isinstance(candidate.get('news'), list):
        return "Catalog apps and news must be lists"
    bundles = set()
    for app_entry in candidate['apps']:
        if not isinstance(app_entry, dict):
            return "Every app must be an object"
        bundle = app_entry.get('bundleIdentifier')
        if not isinstance(bundle, str) or not bundle or bundle in bundles:
            return "App bundle identifiers must be non-empty and unique"
        bundles.add(bundle)
        versions = app_entry.get('versions')
        if not isinstance(versions, list):
            return "Every app must contain a versions list"
        seen = set()
        for version_entry in versions:
            if not isinstance(version_entry, dict):
                return "Every version must be an object"
            version = version_entry.get('version')
            if not isinstance(version, str) or not version or version in seen:
                return "Versions must be non-empty and unique within each app"
            seen.add(version)
    return None


def _load_catalog_snapshot(path):
    try:
        with open(path, 'r') as handle:
            candidate = json.load(handle)
    except Exception:
        return None, "Snapshot is not valid JSON"
    error = _validate_catalog_snapshot(candidate)
    return (None, error) if error else (candidate, None)


def _catalog_structural_diff(current, candidate):
    current = current if isinstance(current, dict) else {}
    def app_versions(catalog):
        return {
            app.get('bundleIdentifier'): {
                version.get('version') for version in app.get('versions', [])
                if isinstance(version, dict) and isinstance(version.get('version'), str)
            }
            for app in catalog.get('apps', []) if isinstance(app, dict)
            and isinstance(app.get('bundleIdentifier'), str)
        }
    current_apps = app_versions(current if isinstance(current, dict) else {})
    candidate_apps = app_versions(candidate)
    common = sorted(set(current_apps) & set(candidate_apps))
    return {
        "changedSourceFields": [field for field in _SOURCE_DIFF_FIELDS if current.get(field) != candidate.get(field)],
        "appsAdded": sorted(set(candidate_apps) - set(current_apps)),
        "appsRemoved": sorted(set(current_apps) - set(candidate_apps)),
        "versions": {
            bundle: {
                "added": sorted(candidate_apps[bundle] - current_apps[bundle]),
                "removed": sorted(current_apps[bundle] - candidate_apps[bundle]),
            }
            for bundle in common
            if candidate_apps[bundle] != current_apps[bundle]
        },
    }


def _scan_catalog_health(source_data=None, base_url=None):
    """Read-only scan of the live catalog + storage for known problem
    classes (plan 038): duplicate versions, zero/missing sizes, empty or
    non-public downloadURLs, missing icons, missing IPAs, and (local
    backend only) IPA files that exist but aren't actually valid ZIPs --
    the gzip-HTML corruption class.

    Never mutates anything. Tolerates a malformed catalog shape (missing
    'apps', a non-dict app/version, a non-string bundleIdentifier) the
    same way normalize_source() does on the /source.json path, by
    skipping the offending element instead of raising -- this backs a
    GET route that must never 500.

    Returns a list of {"bundleIdentifier", "version", "kind", "detail",
    "severity"} dicts. "version" is None for app-level issues (and for
    the one Garage limitation note, "bundleIdentifier" is None too).
    """
    issues = []
    if source_data is None:
        source_data = source_manager.load_source()
    if not isinstance(source_data, dict):
        return issues

    apps = source_data.get('apps')
    if not isinstance(apps, list):
        return issues

    try:
        resolved = base_url or resolve_base_url()
        this_host = urlparse(resolved).netloc.lower() if resolved else None
    except Exception:
        this_host = None

    public_host = None
    if PUBLIC_BASE_URL:
        try:
            public_host = urlparse(PUBLIC_BASE_URL).netloc.lower()
        except Exception:
            public_host = None

    is_local_backend = STORAGE_BACKEND != "garage"

    def add(bundle_id, version, kind, detail, severity):
        issues.append({
            "bundleIdentifier": bundle_id,
            "version": version,
            "kind": kind,
            "detail": detail,
            "severity": severity,
        })

    for app_entry in apps:
        if not isinstance(app_entry, dict):
            continue
        bundle_id = app_entry.get('bundleIdentifier')
        if not (isinstance(bundle_id, str) and bundle_id):
            continue

        # missing-icon: empty iconURL, or (best-effort, only when the URL
        # points at this host's /icons/ path) the hosted icon is absent.
        icon_url = app_entry.get('iconURL')
        if not (isinstance(icon_url, str) and icon_url.strip()):
            add(bundle_id, None, "missing-icon", "iconURL is empty", "warning")
        else:
            try:
                parsed_icon = urlparse(icon_url)
                if this_host and parsed_icon.netloc.lower() == this_host and parsed_icon.path.startswith('/icons/'):
                    filename = parsed_icon.path.rsplit('/', 1)[-1]
                    if filename.startswith('icon.') and len(filename) > len('icon.'):
                        icon_ext = filename[len('icon.'):]
                        if not icon_storage.exists(bundle_id, icon_ext):
                            add(bundle_id, None, "missing-icon",
                                f"hosted icon not found for {icon_url}", "warning")
            except Exception:
                pass

        versions = app_entry.get('versions')
        if not isinstance(versions, list):
            continue

        seen_versions = set()
        for version_entry in versions:
            if not isinstance(version_entry, dict):
                continue
            raw_version = version_entry.get('version')
            version_label = raw_version if isinstance(raw_version, str) and raw_version else None

            # duplicate-version
            if version_label is not None:
                if version_label in seen_versions:
                    add(bundle_id, version_label, "duplicate-version",
                        f"version '{version_label}' appears more than once", "error")
                else:
                    seen_versions.add(version_label)

            # zero-size: missing, 0, or not an int (bool excluded explicitly --
            # it is technically an int subtype but never a legitimate size).
            size = version_entry.get('size')
            if isinstance(size, bool) or not isinstance(size, int) or size == 0:
                add(bundle_id, version_label, "zero-size", f"size is {size!r}", "error")

            # empty-or-nonpublic-downloadURL + missing-ipa
            download_url = version_entry.get('downloadURL')
            if not (isinstance(download_url, str) and download_url.strip()):
                add(bundle_id, version_label, "empty-or-nonpublic-downloadURL",
                    "downloadURL is empty", "error")
            else:
                try:
                    parsed = urlparse(download_url)
                    if public_host and parsed.netloc.lower() != public_host:
                        add(bundle_id, version_label, "empty-or-nonpublic-downloadURL",
                            f"downloadURL host '{parsed.netloc}' does not match "
                            f"PUBLIC_BASE_URL host '{public_host}'", "info")
                    if (this_host and version_label and parsed.netloc.lower() == this_host
                            and parsed.path.startswith('/ipas/')
                            and not ipa_storage.exists(bundle_id, version_label)):
                        add(bundle_id, version_label, "missing-ipa",
                            f"hosted IPA not found for {download_url}", "error")
                except Exception:
                    pass

            # not-a-zip (local backend only -- checking a Garage object's
            # content would mean downloading it, which this scan never does)
            if is_local_backend and version_label:
                try:
                    path = source_manager.get_ipa_path(bundle_id, version_label)
                    if os.path.exists(path) and not zipfile.is_zipfile(path):
                        add(bundle_id, version_label, "not-a-zip",
                            f"local IPA is not a valid ZIP: {path}", "error")
                except Exception:
                    pass

    if not is_local_backend:
        add(None, None, "not-a-zip",
            "Content validation (ZIP-file check) is unavailable for the Garage "
            "backend -- only object existence is verified.", "info")

    return issues


@app.route('/api/health')
@requires_auth
def catalog_health():
    """Read-only diagnostics: scan the live catalog + storage for known
    problem classes and report them. Never mutates anything, never 500s
    (plan 038) -- a scan failure degrades to an empty report rather than
    a broken response, since this exists to surface problems, not add one.
    """
    try:
        issues = _scan_catalog_health()
        _record_health_snapshot(issues, 'manual')
        return jsonify({"issues": issues})
    except Exception as e:
        logging.error(f"Health scan error: {str(e)}")
        return jsonify({"issues": [], "error": "scan failed"}), 200


@app.route('/api/health-monitor', methods=['GET'])
@requires_auth
def get_health_monitor():
    data = load_health_monitor()
    response = dict(data)
    response['snapshots'] = list(reversed(data.get('snapshots', [])))
    return jsonify(response)


@app.route('/api/health-monitor/config', methods=['POST'])
@requires_auth
def configure_health_monitor():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get('enabled'), bool):
        return jsonify({"success": False, "error": "enabled must be a boolean"}), 400
    interval = payload.get('intervalHours')
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 168:
        return jsonify({"success": False, "error": "intervalHours must be between 1 and 168"}), 400
    with _HEALTH_MONITOR_LOCK:
        data = load_health_monitor()
        data['enabled'] = payload['enabled']
        data['intervalHours'] = interval
        _save_health_monitor_unlocked(data)
    _health_monitor_wake_event.set()
    return jsonify({"success": True})


def _health_monitor_cycle(now=None):
    data = load_health_monitor()
    if not data.get('enabled'):
        return
    now = now or datetime.now(timezone.utc)
    last = data.get('lastRunAt')
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if (now - last_dt).total_seconds() < data.get('intervalHours', 6) * 3600:
                return
        except (TypeError, ValueError):
            pass
    issues = _scan_catalog_health(base_url=PUBLIC_BASE_URL)
    _record_health_snapshot(issues, 'scheduled')


def _health_monitor_loop(stop_event):
    while not stop_event.is_set():
        try:
            _health_monitor_cycle()
        except Exception:
            logging.exception("health monitor cycle failed")
        _health_monitor_wake_event.wait(timeout=300)
        _health_monitor_wake_event.clear()


def _start_health_monitor():
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_health_monitor_loop, args=(stop_event,), daemon=True,
        name='health-monitor',
    )
    thread.start()
    return thread, stop_event


@app.route('/api/catalog-backups', methods=['GET'])
@requires_auth
def catalog_backups():
    snapshots = []
    try:
        names = sorted(
            (name for name in os.listdir(BACKUP_FOLDER) if _BACKUP_NAME_RE.fullmatch(name)),
            reverse=True,
        )[:20]
    except FileNotFoundError:
        names = []
    for name in names:
        try:
            path = _resolve_backup_path(name)
            candidate, error = _load_catalog_snapshot(path)
            stat = os.stat(path)
            snapshots.append({
                "filename": name, "size": stat.st_size, "sha256": _sha256_file(path),
                "valid": error is None, "error": error,
                "sourceName": candidate.get('name') if candidate else None,
                "appCount": len(candidate.get('apps', [])) if candidate else None,
                "versionCount": sum(len(app.get('versions', [])) for app in candidate.get('apps', [])) if candidate else None,
                "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })
        except Exception:
            snapshots.append({"filename": name, "valid": False, "error": "Snapshot could not be read"})
    return jsonify({"backups": snapshots})


@app.route('/api/catalog-backups/<filename>/download', methods=['GET'])
@requires_auth
def download_catalog_backup(filename):
    try:
        path = _resolve_backup_path(filename)
    except (ValueError, FileNotFoundError):
        return jsonify({"error": "Backup not found"}), 404
    return send_file(path, as_attachment=True, download_name=filename, mimetype='application/json')


@app.route('/api/catalog-backups/preview', methods=['POST'])
@requires_auth
def preview_catalog_backup():
    payload = request.get_json(silent=True) or {}
    try:
        path = _resolve_backup_path(payload.get('filename'))
    except (ValueError, FileNotFoundError):
        return jsonify({"error": "Backup not found"}), 404
    candidate, error = _load_catalog_snapshot(path)
    if error:
        return jsonify({"error": error}), 400
    return jsonify({
        "filename": payload['filename'], "sha256": _sha256_file(path),
        "diff": _catalog_structural_diff(source_manager.load_source() or {}, candidate),
        "issues": _scan_catalog_health(candidate),
    })


@app.route('/api/catalog-backups/restore', methods=['POST'])
@requires_auth
def restore_catalog_backup():
    payload = request.get_json(silent=True) or {}
    filename = payload.get('filename')
    if payload.get('confirm') != filename or not filename:
        return jsonify({"success": False, "error": "Type the exact backup filename to confirm"}), 400
    try:
        success, message, summary = source_manager.restore_backup(
            filename, payload.get('expectedSha256'),
            allow_missing_artifacts=payload.get('allowMissingArtifacts') is True,
        )
    except (ValueError, FileNotFoundError):
        return jsonify({"success": False, "error": "Backup not found"}), 404
    if not success:
        return jsonify({"success": False, "error": message}), 400
    logging.info("Restored catalog snapshot %s (%s)", filename, str(payload.get('expectedSha256'))[:12])
    return jsonify({"success": True, "message": message, **summary})


def _reconcile_icons(apply=False):
    """Scan ICON_FOLDER for on-disk icons and upload any missing from the
    active backend (plan 040). Mirrors scripts/migrate_icons_to_garage.py's
    logic in-app so an operator without a repo checkout can repair icons
    that were never migrated after an icon backend switch.

    Dry-run by default (apply=False): reports what would be uploaded without
    writing anything. apply=True uploads only icons missing from the active
    backend. Never deletes or modifies local icon files.

    For the local backend this is a no-op: on-disk icons ARE what's served.
    """
    if ICON_STORAGE_BACKEND != "garage":
        return {
            "backend": "local",
            "uploaded": 0,
            "note": "local backend serves on-disk icons directly; nothing to reconcile",
        }

    checked = 0
    would_upload = 0
    uploaded = 0
    skipped_existing = 0
    failed = 0
    items = []

    if os.path.isdir(ICON_FOLDER):
        try:
            bundles = sorted(os.listdir(ICON_FOLDER))
        except OSError as e:
            logging.error(f"Could not read icon folder for reconcile: {e}")
            return {
                "backend": "garage",
                "checked": 0,
                "would_upload": 0,
                "uploaded": 0,
                "skipped_existing": 0,
                "failed": 0,
                "items": [],
                "note": "Could not read the local icon folder; nothing was reconciled.",
            }
        for bundle in bundles:
            safe_bundle = secure_filename(bundle)
            if not safe_bundle or safe_bundle != bundle:
                continue
            bundle_dir = os.path.join(ICON_FOLDER, bundle)
            if not os.path.isdir(bundle_dir):
                continue
            try:
                filenames = sorted(os.listdir(bundle_dir))
            except OSError as e:
                logging.error(f"Could not read icon dir for {bundle}: {e}")
                failed += 1
                items.append({"bundleIdentifier": bundle, "ext": None, "status": "scan_failed"})
                continue
            for filename in filenames:
                path = os.path.join(bundle_dir, filename)
                if not os.path.isfile(path) or not filename.startswith("icon."):
                    continue
                ext = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
                safe_ext = secure_filename(ext)
                if filename != f"icon.{ext}" or not safe_ext or safe_ext != ext or ext not in ALLOWED_ICON_EXTENSIONS:
                    continue

                checked += 1
                try:
                    already_exists = icon_storage.exists(bundle, ext)
                except Exception as e:
                    logging.error(f"Error checking icon existence ({bundle}/{ext}): {str(e)}")
                    failed += 1
                    items.append({"bundleIdentifier": bundle, "ext": ext, "status": "failed"})
                    continue

                if already_exists:
                    skipped_existing += 1
                    items.append({"bundleIdentifier": bundle, "ext": ext, "status": "skipped_existing"})
                    continue

                if not apply:
                    would_upload += 1
                    items.append({"bundleIdentifier": bundle, "ext": ext, "status": "would_upload"})
                    continue

                try:
                    ok = icon_storage.put(path, bundle, ext)
                except Exception as e:
                    logging.error(f"Error uploading icon ({bundle}/{ext}): {str(e)}")
                    ok = False
                if ok:
                    uploaded += 1
                    items.append({"bundleIdentifier": bundle, "ext": ext, "status": "uploaded"})
                else:
                    failed += 1
                    items.append({"bundleIdentifier": bundle, "ext": ext, "status": "failed"})

    result = {
        "backend": "garage",
        "checked": checked,
        "would_upload": would_upload,
        "uploaded": uploaded,
        "skipped_existing": skipped_existing,
        "failed": failed,
        "items": items,
    }
    if checked == 0 and "note" not in result:
        result["note"] = ("No local icon files found to reconcile. Icons missing from "
                          "Garage that have no local copy must be re-uploaded via Edit App.")
    return result


@app.route('/api/icons/backfill', methods=['POST'])
@requires_auth
def backfill_icons():
    """Fill missing icons from the binaries: iOS apps from their newest
    hosted IPA, Android packages from their newest APK."""
    try:
        ios = source_manager.backfill_icons(base_url=resolve_base_url())
        android = android_repo.backfill_icons()
        return jsonify({"success": True, "ios": ios, "android": android})
    except Exception as e:
        logging.error(f"Error backfilling icons: {str(e)}")
        return jsonify({"success": False, "error": "Icon backfill failed"}), 400


@app.route('/api/reconcile-icons', methods=['POST'])
@requires_auth
def reconcile_icons():
    apply = bool((request.get_json(silent=True) or {}).get('apply'))
    try:
        return jsonify(_reconcile_icons(apply=apply))
    except Exception as e:
        logging.error(f"Icon reconcile error: {str(e)}")
        return jsonify({"error": "reconcile failed"}), 500


@app.route('/api/storage-selftest', methods=['POST'])
@requires_auth
def storage_selftest():
    """Write-read-delete round-trip against the active storage backend
    for both icon_storage and ipa_storage (plan 050). Distinguishes a 403
    ("forbidden") from a real 404/missing object, which exists()'s bool
    contract cannot do -- see the class docstrings on each storage
    backend's selftest() for the discrimination logic. Only ever touches
    a dedicated probe key, which is always deleted; never mutates a real
    catalog object.
    """
    try:
        return jsonify({"icon": icon_storage.selftest(), "ipa": ipa_storage.selftest()})
    except Exception as e:
        logging.error(f"Storage self-test error: {str(e)}")
        return jsonify({"error": "self-test failed"}), 500


@app.route('/api/diagnostics', methods=['GET'])
@requires_auth
def diagnostics():
    # newest first, capped
    return jsonify({"entries": list(_DIAG_BUFFER)[::-1]})


# ---------------------------------------------------------------------------
# Certificate inspection (plans/086 section 8e).
#
# Validate-and-display only: the operator uploads a .p12 + .mobileprovision
# pair and gets back what it is, when it expires (twice -- the certificate and
# the profile expire independently), which devices it covers, and whether the
# two files actually belong together. Nothing is signed, nothing is written,
# and the passphrase is never persisted or logged. Signing server-side was
# rejected in plans/086 section 7; this route answers the question that was
# actually being asked, which is whether a certificate is usable.
# ---------------------------------------------------------------------------

@app.route('/api/certificate/inspect', methods=['POST'])
@requires_auth
def inspect_certificate():
    p12_file = request.files.get('p12File')
    provision_file = request.files.get('provisionFile')
    if not p12_file or not p12_file.filename or not provision_file or not provision_file.filename:
        return jsonify({
            "ok": False,
            "reason": "MISSING_FILE",
            "error": "Both a .p12 and a .mobileprovision file are required.",
        }), 400

    # Read into memory: both files are small by definition, and a temp file
    # would leave key material on disk for no gain.
    p12_data = p12_file.read(certificate_inspection.MAX_P12_BYTES + 1)
    provision_data = provision_file.read(certificate_inspection.MAX_PROFILE_BYTES + 1)
    passphrase = request.form.get('p12Password') or ''
    try:
        inspection = certificate_inspection.inspect_pair(p12_data, provision_data, passphrase)
    except certificate_inspection.CertificateInspectionError as e:
        # The reason, never the exception chain: a crypto error message can
        # carry file structure the operator did not ask us to publish.
        return jsonify({"ok": False, "reason": e.reason, "error": e.message}), 400
    except Exception:
        logging.error("Certificate inspection failed", exc_info=False)
        return jsonify({
            "ok": False,
            "reason": "NOT_A_PKCS12",
            "error": "That certificate pair could not be inspected.",
        }), 400

    return jsonify(certificate_inspection.as_payload(inspection))


# ---------------------------------------------------------------------------
# Plan 073: Android / F-Droid repository routes.
#
# /fdroid/repo/<path> and /fdroid/qr are PUBLIC by contract, like the four
# iOS routes (/source.json, /ipas/..., /icons/..., /qr) -- the F-Droid
# client sends no credentials. Everything under /api/android/* is
# session-gated, same convention as the rest of the admin API.
# ---------------------------------------------------------------------------

FDROID_REPO_MIME_TYPES = {
    'apk': 'application/vnd.android.package-archive',
    'jar': 'application/java-archive',
    'json': 'application/json',
}

# Maps the lowerCamel keys the UI/API accept to AndroidRepoManager's
# METADATA_KEYS (fdroidserver's own YAML key casing).
ANDROID_METADATA_FIELD_MAP = {
    "name": "Name",
    "summary": "Summary",
    "description": "Description",
    "authorName": "AuthorName",
    "website": "WebSite",
    "sourceCode": "SourceCode",
    "license": "License",
    "categories": "Categories",
}


def _android_metadata_fields_from(source):
    """Pick out and rename whichever metadata fields were supplied."""
    fields = {}
    for api_key, meta_key in ANDROID_METADATA_FIELD_MAP.items():
        val = source.get(api_key)
        if val:
            fields[meta_key] = val
    return fields


def _download_to_temp(url, suffix):
    """Stream a URL to a temp file under UPLOAD_FOLDER, honoring
    MAX_CONTENT_LENGTH. Returns the temp file path; raises ValueError on
    any failure (the temp file is removed first). Mirrors
    SourceManager.download_ipa_from_url's streaming logic, kept separate
    since Android downloads are not IPAs and do not go through
    ipa_storage."""
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    fd, filepath = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=suffix)
    os.close(fd)
    try:
        logging.info(f"Downloading from: {url}")
        response = requests.get(url, stream=True, timeout=300)
        response.raise_for_status()
        total = 0
        limit = app.config.get("MAX_CONTENT_LENGTH") or (2 * 1024 * 1024 * 1024)
        deadline = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                total += len(chunk)
                if time.monotonic() > deadline:
                    raise ValueError(f"Download took longer than {DOWNLOAD_DEADLINE_SECONDS}s")
                if total > limit:
                    raise ValueError(f"Download exceeded size limit of {limit} bytes")
                f.write(chunk)
        return filepath
    except Exception as e:
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass
        raise ValueError(f"Failed to download from {url}: {e}")


# ---------------------------------------------------------------------------
# API tokens (session-only management) and the agent publish endpoint
# ---------------------------------------------------------------------------

@app.route('/api/tokens')
@requires_auth
def list_api_tokens():
    with _api_tokens_lock:
        return jsonify([_public_token(t) for t in _load_api_tokens()])


@app.route('/api/tokens', methods=['POST'])
@requires_auth
def create_api_token():
    data = request.get_json(silent=True) or {}
    name = data.get('name')
    name = name.strip() if isinstance(name, str) else ""
    if not name or len(name) > 80:
        return jsonify({"success": False, "error": "name is required (at most 80 characters)"}), 400
    apps = data.get('apps') or []
    if isinstance(apps, str):
        apps = [a.strip() for a in apps.replace('\n', ',').split(',') if a.strip()]
    if (not isinstance(apps, list) or len(apps) > 100
            or any(not isinstance(a, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,255}", a) for a in apps)):
        return jsonify({"success": False, "error": "apps must be a list of bundle IDs / package names"}), 400
    days = data.get('expiresInDays')
    expires = None
    if days not in (None, ""):
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 0
        if not 1 <= days <= 3650:
            return jsonify({"success": False, "error": "expiresInDays must be 1-3650"}), 400
        expires = _utc_iso(datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=days))
    token = API_TOKEN_PREFIX + secrets.token_urlsafe(32)
    entry = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "hash": _hash_token(token),
        "prefix": token[:len(API_TOKEN_PREFIX) + 6],
        "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "lastUsed": None,
        "apps": sorted(set(apps)) or None,
        "expires": expires,
    }
    with _api_tokens_lock:
        tokens = _load_api_tokens()
        if len(tokens) >= 100:
            return jsonify({"success": False, "error": "Too many tokens; revoke some first"}), 400
        tokens.append(entry)
        _save_api_tokens(tokens)
    return jsonify({"success": True, "token": token, **_public_token(entry)})


@app.route('/api/tokens/revoke', methods=['POST'])
@requires_auth
def revoke_api_token():
    data = request.get_json(silent=True) or {}
    token_id = data.get('id')
    with _api_tokens_lock:
        tokens = _load_api_tokens()
        kept = [t for t in tokens if t.get("id") != token_id]
        if len(kept) == len(tokens):
            return jsonify({"success": False, "error": "Token not found"}), 404
        _save_api_tokens(kept)
    return jsonify({"success": True})


def _sniff_platform(path, filename):
    """'ios' / 'android' from the archive layout, falling back to the
    extension (an unreadable archive is then rejected by the inspector)."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        names = []
    if "AndroidManifest.xml" in names:
        return "android"
    if any(n.startswith("Payload/") and ".app/" in n for n in names):
        return "ios"
    ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
    return {"apk": "android", "ipa": "ios"}.get(ext)


def _truthy(value, default):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no", "off")


# App-level fields an agent may set: (request key, max length). URLs are
# checked separately. Categories may be a list or a comma-separated string.
_DETAIL_FIELDS = (("name", 50), ("developerName", 100), ("summary", 80), ("description", 4000),
                  ("license", 100), ("website", 2048), ("sourceCode", 2048))


def _read_details(src):
    """Validated app-detail fields present in a form or JSON body. Absent or
    blank fields are left out, so an update never blanks what it wasn't sent."""
    out = {}
    for key, limit in _DETAIL_FIELDS:
        value = src.get(key)
        value = value.strip() if isinstance(value, str) else ""
        if not value:
            continue
        if len(value) > limit:
            raise ValueError(f"{key} must be at most {limit} characters")
        if key in ("website", "sourceCode") and urlparse(value).scheme not in ("http", "https"):
            raise ValueError(f"{key} must be an http(s) URL")
        out[key] = value
    cats = src.getlist("categories") if hasattr(src, "getlist") else src.get("categories")
    if isinstance(cats, list) and len(cats) == 1 and isinstance(cats[0], str):
        cats = cats[0]
    if isinstance(cats, str):
        cats = [c.strip() for c in cats.split(",") if c.strip()]
    if cats:
        if not isinstance(cats, list) or any(not isinstance(c, str) or len(c) > 50 for c in cats) or len(cats) > 10:
            raise ValueError("categories must be up to 10 names of at most 50 characters")
        out["categories"] = cats
    return out


def _derived_summary(details):
    """F-Droid lists show Summary, not Description; an app created with only a
    description would otherwise show a blank line under its name."""
    if details.get("summary") or not details.get("description"):
        return details.get("summary", "")
    first = details["description"].strip().splitlines()[0].strip()
    return first if len(first) <= 80 else first[:79].rstrip() + "…"


def _android_fields(details):
    mapping = {"name": "Name", "summary": "Summary", "description": "Description", "developerName": "AuthorName",
               "website": "WebSite", "sourceCode": "SourceCode", "license": "License", "categories": "Categories"}
    return {meta: details[key] for key, meta in mapping.items() if details.get(key)}


def _ios_fields(details):
    mapping = {"name": "name", "developerName": "developerName", "description": "localizedDescription",
               "summary": "subtitle"}
    return {meta: details[key] for key, meta in mapping.items() if details.get(key)}


def _publish_ios(tmp_path, opts, base_url):
    try:
        inspection = inspect_ipa(tmp_path, "IPA")
    except IpaInspectionError as e:
        raise ValueError(str(e))
    bundle_id, version = inspection.bundle_identifier, inspection.version
    if not _token_may_touch(bundle_id):
        return _scope_refusal(bundle_id)
    result = {"platform": "ios", "id": bundle_id, "version": version,
              "build": inspection.build_version, "created": False, "warnings": []}
    existing = source_manager.get_app(bundle_id)
    if existing and any(isinstance(v, dict) and v.get('version') == version for v in existing.get('versions', [])):
        return jsonify({**result, "success": True, "added": False, "name": existing.get("name"),
                        "message": f"Already present: {bundle_id} {version}"}), 200
    if not existing and not opts["createIfMissing"]:
        return jsonify({**result, "success": False,
                        "error": f"{bundle_id} is not in the catalogue and createIfMissing is false"}), 404
    details = opts["details"]
    ignored = [k for k in details if k not in ("name", "developerName", "summary", "description")]
    if ignored:
        result["warnings"].append(f"iOS sources have no {', '.join(ignored)}; ignored")

    with open(tmp_path, "rb") as fh:
        fs = FileStorage(stream=fh, filename="app.ipa")
        if existing:
            ok, message = source_manager.add_version(bundle_id, {
                "version": version,
                "buildVersion": inspection.build_version,
                "minOSVersion": inspection.minimum_os_version,
                "localizedDescription": opts["whatsNew"],
            }, ipa_file=fs, base_url=base_url)
        else:
            new_app = {
                "name": details.get("name") or inspection.name or bundle_id,
                "bundleIdentifier": bundle_id,
                "developerName": details.get("developerName") or "Unknown",
                "localizedDescription": details.get("description", ""),
                "subtitle": _derived_summary(details),
                "version": version,
                "buildVersion": inspection.build_version,
                "minOSVersion": inspection.minimum_os_version,
                "versionDescription": opts["whatsNew"],
            }
            if inspection.privacy:
                new_app["appPermissions"] = {"entitlements": [], "privacy": inspection.privacy}
            ok, message = source_manager.add_app_manual(new_app, ipa_file=fs, base_url=base_url)
            result["created"] = ok
    if ok and ALREADY_PRESENT in message:
        # A concurrent retry stored it first: same answer as the early check.
        return jsonify({**result, "success": True, "added": False, "created": False,
                        "message": f"Already present: {bundle_id} {version}"}), 200
    if not ok:
        # Save/load failures are the server's (disk, Garage); say so, so a
        # client retries instead of treating it as a bad upload.
        server_side = message.startswith(("Failed to save", "Failed to load", "Failed to download"))
        return jsonify({**result, "success": False, "error": message}), 503 if server_side else 400
    if existing and details:
        result["warnings"].append("App details are only set when an app is created; use update_app / "
                                  "POST /api/app-details to change them")

    app_info = source_manager.get_app(bundle_id) or {}
    entry = next((v for v in app_info.get("versions", []) if v.get("version") == version), {})
    notify("add_version" if existing else "add_app",
           f"Published via API: {app_info.get('name', bundle_id)} ({bundle_id}) {version}\n{base_url}/source.json")
    return jsonify({**result, "success": True, "added": True, "name": app_info.get("name"),
                    "downloadURL": entry.get("downloadURL"), "size": entry.get("size"), "message": message}), 200


def _publish_android(tmp_path, opts, base_url):
    info = _inspect_apk(tmp_path)
    package = info["package"]
    if not _token_may_touch(package):
        return _scope_refusal(package)
    result = {"platform": "android", "id": package, "version": info["version_name"],
              "build": info["version_code"], "created": False, "warnings": []}
    if info.get("debuggable"):
        if APK_REJECT_DEBUGGABLE:
            return jsonify({**result, "success": False,
                            "error": "APK is debuggable (android:debuggable=true); upload a release build"}), 400
        result["warnings"].append(
            "APK is debuggable (android:debuggable=true): anyone with the device can read the app's private "
            "data over adb. Publish a release build.")
    known = os.path.exists(android_repo.metadata_path(package))
    if not known and not opts["createIfMissing"]:
        return jsonify({**result, "success": False,
                        "error": f"{package} is not in the repo and createIfMissing is false"}), 404
    details = opts["details"]
    fields = {}
    if not known:
        fields = android_repo.validate_metadata(_android_fields({**details, "summary": _derived_summary(details)}))
    already = os.path.exists(android_repo.apk_path(package, info["version_code"]))
    if opts["whatsNew"] and not already:
        # Written before the APK lands so the rebuild that publishes the
        # version already sees its notes.
        android_repo.write_changelog(package, info["version_code"], opts["whatsNew"])
    added, _ = android_repo.add_apk(tmp_path, expected_package=package)
    url = f"{base_url}/fdroid/repo/{android_repo.apk_filename(package, info['version_code'])}"
    if not added:
        return jsonify({**result, "success": True, "added": False, "downloadURL": url,
                        "message": f"Already present: {package} versionCode {info['version_code']}"}), 200
    if fields:
        android_repo.write_metadata(package, fields)
    elif details:
        result["warnings"].append("App details are only set when an app is created; use update_app / "
                                  "POST /api/app-details to change them")
    notify("android_add_apk", f"Published via API: {package} {info['version_name']}")
    return jsonify({**result, "success": True, "added": True, "created": not known, "downloadURL": url,
                    "pending": True, "name": details.get("name") or info.get("app_name"),
                    "message": f"Added {package} versionCode {info['version_code']}; "
                               "the F-Droid index rebuilds on the sidecar's next pass"}), 200


def _request_fields():
    is_multipart = bool(request.content_type and 'multipart/form-data' in request.content_type)
    src = request.form if is_multipart else (request.get_json(silent=True) or {})
    if not hasattr(src, "get"):
        raise ValueError("Expected a JSON object or multipart form")
    return is_multipart, src


@app.route('/api/publish', methods=['POST'])
@requires_publish
def publish():
    """One call for agents and CI: upload (multipart `file`) or point at a
    `url` of an .ipa or .apk. Platform, bundle ID / package, and version come
    from the file itself. New apps are created unless createIfMissing=false;
    app details apply only when an app is created (see /api/app-details)."""
    temp_path = None
    try:
        is_multipart, src = _request_fields()
        whats_new = src.get("whatsNew")
        whats_new = whats_new.strip() if isinstance(whats_new, str) else ""
        if len(whats_new) > 4000:
            raise ValueError("whatsNew must be at most 4000 characters")
        opts = {
            "details": _read_details(src),
            "whatsNew": whats_new,
            "createIfMissing": _truthy(src.get("createIfMissing"), True),
        }
        upload = None
        if is_multipart:
            upload = next((request.files[k] for k in ("file", "ipaFile", "apkFile")
                           if k in request.files and request.files[k].filename), None)
        url = src.get("url")
        url = url.strip() if isinstance(url, str) else ""

        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        if upload is not None:
            filename = upload.filename
            fd, temp_path = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=".upload")
            os.close(fd)
            upload.save(temp_path)
        elif url:
            if g.get("api_token") is not None:
                # A server-side fetch on a token's behalf would let any token
                # holder -- including an app-scoped one -- reach hosts only the
                # server can see (LAN, localhost, metadata endpoints) and read
                # back what answered. Tokens upload; the MCP server downloads
                # `url` on the agent's machine and uploads it.
                return jsonify({"success": False, "error": "url is not accepted with an API token; "
                                "download the file and upload it (the MCP server does this for you)"}), 400
            if len(url) > 2048 or urlparse(url).scheme not in ("http", "https"):
                return jsonify({"success": False, "error": "url must be an http(s) URL"}), 400
            filename = os.path.basename(urlparse(url).path)
            temp_path = _download_to_temp(url, ".upload")
        else:
            return jsonify({"success": False, "error": "Send a multipart `file` or a `url`"}), 400

        platform = _sniff_platform(temp_path, filename)
        if platform is None:
            return jsonify({"success": False, "error": "Not an .ipa or .apk"}), 400
        publisher = _publish_ios if platform == "ios" else _publish_android
        return publisher(temp_path, opts, resolve_base_url())
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except HTTPException:
        raise                    # e.g. 413 RequestEntityTooLarge -> its own status
    except Exception as e:
        logging.error(f"Error publishing via API: {e}")
        return jsonify({"success": False, "error": "Publish failed"}), 500
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


@app.route('/api/app-details', methods=['POST'])
@requires_publish
def update_app_details():
    """Change an existing app's details (iOS or Android, found by `id`).
    Only the fields sent are changed."""
    try:
        _is_multipart, src = _request_fields()
        app_id = src.get("id")
        app_id = app_id.strip() if isinstance(app_id, str) else ""
        if not app_id:
            return jsonify({"success": False, "error": "id is required"}), 400
        if not _token_may_touch(app_id):
            return _scope_refusal(app_id)
        details = _read_details(src)
        whats_new = src.get("whatsNew")
        whats_new = whats_new.strip() if isinstance(whats_new, str) else ""
        notes_version = src.get("version")
        notes_version = str(notes_version).strip() if notes_version not in (None, "") else ""
        if len(whats_new) > 4000:
            raise ValueError("whatsNew must be at most 4000 characters")
        if whats_new and not notes_version:
            raise ValueError("version is required with whatsNew (iOS version string or Android versionCode)")
        if not details and not whats_new:
            return jsonify({"success": False, "error": "Nothing to update"}), 400
        warnings = []
        platform = src.get("platform")
        platform = platform.strip().lower() if isinstance(platform, str) else ""
        if platform not in ("", "ios", "android"):
            raise ValueError("platform must be 'ios' or 'android'")
        on_ios = bool(source_manager.get_app(app_id))
        on_android = bool(ANDROID_PACKAGE_RE.match(app_id)) and os.path.exists(android_repo.metadata_path(app_id))
        if not platform and on_ios and on_android:
            # Cross-platform apps often share one reverse-DNS ID; guessing
            # silently sent every Android update to the iOS entry.
            return jsonify({"success": False, "id": app_id,
                            "error": f"{app_id} exists on iOS and Android; send platform 'ios' or 'android'"}), 409
        if platform == "ios" and not on_ios or platform == "android" and not on_android:
            return jsonify({"success": False, "error": f"No {platform} app with id {app_id!r}"}), 404
        if on_ios and platform != "android":
            if whats_new:
                ok, message = source_manager.set_version_notes(app_id, notes_version, whats_new)
                if not ok:
                    return jsonify({"success": False, "error": message}), 404
            if not details:
                return jsonify({"success": True, "platform": "ios", "id": app_id, "warnings": warnings,
                                "app": source_manager.get_app(app_id)})
            ignored = [k for k in details if k not in ("name", "developerName", "summary", "description")]
            if ignored:
                warnings.append(f"iOS sources have no {', '.join(ignored)}; ignored")
            ok, message = source_manager.update_app(app_id, _ios_fields(details), base_url=resolve_base_url())
            if not ok:
                return jsonify({"success": False, "error": message}), 400
            return jsonify({"success": True, "platform": "ios", "id": app_id, "warnings": warnings,
                            "app": source_manager.get_app(app_id)})
        if on_android:
            if whats_new:
                if not notes_version.isdigit() or not os.path.exists(android_repo.apk_path(app_id, int(notes_version))):
                    return jsonify({"success": False,
                                    "error": f"versionCode {notes_version} of {app_id} is not published"}), 404
                android_repo.write_changelog(app_id, int(notes_version), whats_new)
            if details:
                android_repo.write_metadata(app_id, _android_fields(details))
            else:
                android_repo.request_update()
            return jsonify({"success": True, "platform": "android", "id": app_id, "warnings": warnings,
                            "pending": True, "metadata": android_repo.read_metadata(app_id)})
        return jsonify({"success": False, "error": f"No app with id {app_id!r}"}), 404
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except HTTPException:
        raise                    # e.g. 413 RequestEntityTooLarge -> its own status
    except Exception as e:
        logging.error(f"Error updating app details via API: {e}")
        return jsonify({"success": False, "error": "Update failed"}), 500


@app.route('/fdroid/repo/<path:filename>')
def fdroid_repo_file(filename):
    """Serve the F-Droid repo (index, jars, APKs, icons) that the
    fdroid-index sidecar builds. Must stay public: the F-Droid client
    sends no credentials. send_from_directory rejects path traversal on
    its own (safe_join), so a crafted "../..." filename 404s here too."""
    try:
        if (apk_storage is not None and ANDROID_APK_FILENAME_RE.match(filename) and "/" not in filename
                and os.path.exists(os.path.join(FDROID_REPO_DIR, filename)) and apk_storage.exists(filename)):
            return redirect(apk_storage.public_url(filename), code=302)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        mimetype = FDROID_REPO_MIME_TYPES.get(ext)
        if mimetype:
            return send_from_directory(FDROID_REPO_DIR, filename, mimetype=mimetype)
        return send_from_directory(FDROID_REPO_DIR, filename)
    except Exception as e:
        logging.error(f"Error serving fdroid repo file: {str(e)}")
        return jsonify({"error": "Not found"}), 404


@app.route('/fdroid/qr')
def fdroid_qr():
    try:
        subscribe_url = android_repo.status().get('subscribe_url')
        if not subscribe_url:
            return jsonify({"error": "F-Droid repo not initialised — start the fdroid-index service"}), 503

        if subscribe_url.startswith('https://'):
            qr_url = 'fdroidrepos://' + subscribe_url[len('https://'):]
        elif subscribe_url.startswith('http://'):
            qr_url = 'fdroidrepo://' + subscribe_url[len('http://'):]
        else:
            qr_url = subscribe_url

        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(qr_url)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)

        return send_file(buf, mimetype='image/png')
    except Exception as e:
        logging.error(f"Android QR generation error: {str(e)}")
        return jsonify({"error": "QR generation failed"}), 500


@app.route('/api/android/status')
@requires_publish
def android_status():
    try:
        return jsonify(android_repo.status(sync_url=True))
    except Exception as e:
        logging.error(f"Error getting Android repo status: {str(e)}")
        return jsonify({"error": "Android repo status unavailable"}), 500


@app.route('/api/android/apps')
@requires_publish
def android_apps():
    try:
        return jsonify(android_repo.list_apps())
    except Exception as e:
        logging.error(f"Error listing Android apps: {str(e)}")
        return jsonify({"error": "Android apps unavailable"}), 500


@app.route('/api/android/add-apk', methods=['POST'])
@requires_auth
def android_add_apk():
    temp_path = None
    try:
        apk_file = request.files.get('apkFile')
        download_from_url = request.form.get('downloadFromUrl', 'false').lower() == 'true'
        download_url = request.form.get('downloadURL', '')
        package_hint = request.form.get('package') or None

        if apk_file and apk_file.filename:
            if not apk_file.filename.lower().endswith('.apk'):
                return jsonify({"success": False, "error": "File must be a .apk"}), 400
            os.makedirs(UPLOAD_FOLDER, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix='.apk')
            os.close(fd)
            apk_file.save(temp_path)
        elif download_from_url:
            if not download_url:
                return jsonify({"success": False, "error": "downloadURL is required when downloadFromUrl is set"}), 400
            if not download_url.lower().split('?')[0].endswith('.apk'):
                return jsonify({"success": False, "error": "downloadURL must point to a .apk"}), 400
            temp_path = _download_to_temp(download_url, '.apk')
        else:
            return jsonify({"success": False, "error": "Either an APK file or downloadFromUrl is required"}), 400

        fields = _android_metadata_fields_from(request.form)
        fields = android_repo.validate_metadata(fields)
        success, result = android_repo.add_apk(temp_path, expected_package=package_hint)

        if not success:
            return jsonify({
                "success": True,
                "added": False,
                "message": f"Already present: {result['package']} versionCode {result['version_code']}",
                "package": result["package"],
                "versionCode": result["version_code"],
            })

        if fields:
            android_repo.write_metadata(result["package"], fields)

        notify("android_add_apk", f"New Android APK published: {result['package']} {result['version_name']}")
        return jsonify({
            "success": True,
            "added": True,
            "message": f"Added {result['package']} version {result['version_code']}",
            "package": result["package"],
            "versionCode": result["version_code"],
            "pending": True,
            "warnings": ["APK is debuggable (android:debuggable=true): publish a release build"]
                        if result.get("debuggable") else [],
        })
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logging.error(f"Error adding APK: {str(e)}")
        return jsonify({"success": False, "error": "Failed to add APK"}), 400
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


@app.route('/api/android/update-app', methods=['POST'])
@requires_auth
def android_update_app():
    try:
        data = request.get_json() if request.is_json else {}
        package = data.get('package')
        if not package:
            return jsonify({"success": False, "error": "package is required"}), 400
        fields = _android_metadata_fields_from(data)
        android_repo.write_metadata(package, fields)
        return jsonify({"success": True, "message": f"Updated metadata for {package}"})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logging.error(f"Error updating Android app metadata: {str(e)}")
        return jsonify({"success": False, "error": "Failed to update Android app"}), 400


@app.route('/api/android/delete-version', methods=['POST'])
@requires_auth
def android_delete_version():
    try:
        data = request.get_json() if request.is_json else {}
        package = data.get('package')
        version_code = data.get('versionCode')
        if not package or version_code is None:
            return jsonify({"success": False, "error": "package and versionCode are required"}), 400
        removed = android_repo.delete_version(package, version_code)
        if not removed:
            return jsonify({"success": False, "error": "Version not found"}), 404
        return jsonify({"success": True, "message": f"Deleted {package} version {version_code}"})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logging.error(f"Error deleting Android version: {str(e)}")
        return jsonify({"success": False, "error": "Failed to delete Android version"}), 400


@app.route('/api/android/delete-app', methods=['POST'])
@requires_auth
def android_delete_app():
    try:
        data = request.get_json() if request.is_json else {}
        package = data.get('package')
        if not package:
            return jsonify({"success": False, "error": "package is required"}), 400
        removed = android_repo.delete_app(package)
        if not removed:
            return jsonify({"success": False, "error": "App not found"}), 404
        return jsonify({"success": True, "message": f"Deleted {package}"})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logging.error(f"Error deleting Android app: {str(e)}")
        return jsonify({"success": False, "error": "Failed to delete Android app"}), 400


@app.route('/api/android/repo-config', methods=['POST'])
@requires_auth
def android_repo_config():
    try:
        data = request.get_json() if request.is_json else {}
        name = data.get('name') or 'Feather Android'
        description = data.get('description', '')
        android_repo.write_repo_config(name, description)
        return jsonify({"success": True, "message": "Repository configuration updated"})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logging.error(f"Error updating Android repo config: {str(e)}")
        return jsonify({"success": False, "error": "Failed to update Android repo configuration"}), 400


@app.route('/api/android/request-update', methods=['POST'])
@requires_auth
def android_request_update():
    try:
        icons = android_repo.backfill_icons()
        mirror = android_repo.mirror_apks()
        android_repo.request_update()
        message = "Index rebuild requested"
        if mirror["uploaded"]:
            message += f"; uploaded {mirror['uploaded']} APK(s) to Garage"
        if mirror["failed"]:
            message += f"; Garage upload failed for {len(mirror['failed'])} APK(s)"
        if icons["added"]:
            message += f"; extracted {icons['added']} missing icon(s)"
        if icons["missing"]:
            message += f"; no raster icon in: {', '.join(icons['missing'][:5])}"
        return jsonify({"success": True, "message": message, "icons": icons, "garage": mirror})
    except Exception as e:
        logging.error(f"Error requesting Android index update: {str(e)}")
        return jsonify({"success": False, "error": "Failed to request Android index update"}), 400


@app.errorhandler(413)
def too_large(error):
    return jsonify({"success": False,
                    "error": f"Upload is larger than MAX_CONTENT_LENGTH ({MAX_CONTENT_LENGTH} bytes)"}), 413

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500

if __name__ == '__main__':
    logging.info("Starting AltStore Source Manager (Waitress)...")
    _start_auto_import_scheduler()
    _start_health_monitor()
    # Single process, many threads: SourceManager's in-process lock and the
    # auto-import scheduler thread both assume exactly one process. Do NOT
    # scale this out to multiple Waitress/gunicorn/uWSGI worker processes
    # without first externalizing the lock and ensuring only one process
    # owns the scheduler.
    from waitress import serve
    serve(app, host='0.0.0.0', port=PORT, threads=int(_env("WAITRESS_THREADS", "8")),
          # Waitress's own default (1 GiB) silently undercut MAX_CONTENT_LENGTH
          # (2 GiB documented). Leave headroom for multipart framing; Flask
          # enforces the real limit and answers 413 JSON.
          max_request_body_size=MAX_CONTENT_LENGTH + 16 * 1024 * 1024)
