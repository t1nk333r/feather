from flask import Flask, render_template, request, jsonify, send_file, redirect, session
import json
import os
import copy
import logging
import qrcode
import io
import requests
import tempfile
import hashlib
import hmac
import threading
import boto3
import zipfile
from functools import wraps
from botocore.exceptions import ClientError
from datetime import datetime, timezone
from urllib.parse import urlparse
from werkzeug.utils import secure_filename
from werkzeug.datastructures import FileStorage

import sys
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import release_source_ingest as release_ingest

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

app = Flask(__name__)

# Configuration
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
SOURCE_FILE = os.path.join(DATA_DIR, "source.json")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
IPA_FOLDER = os.path.join(DATA_DIR, "ipas")
ICON_FOLDER = os.path.join(DATA_DIR, "icons")
BACKUP_FOLDER = os.path.join(DATA_DIR, "backups")
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
SECRET_KEY = os.environ.get("SECRET_KEY")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise RuntimeError(
        "ADMIN_PASSWORD is not set. Refusing to start with unauthenticated "
        "admin routes. Set it in .env (compose.yml loads it via env_file)."
    )
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL")
PORT = int(os.environ.get("PORT", "5000"))
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 2 * 1024 * 1024 * 1024))

# Added by plan 014 -- optional Telegram notification on catalog changes.
# Disabled unless both TELEGRAM_BOT_TOKEN and TELEGRAM_NOTIFY_CHAT_ID are
# set; see notify() below. TELEGRAM_BOT_TOKEN and BOT_API_BASE_URL are
# shared with plan 013's bot ingest script where that has landed --
# neither is redeclared in .env.example for this plan.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_NOTIFY_CHAT_ID = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID")
TELEGRAM_API_BASE = os.environ.get("BOT_API_BASE_URL", "https://api.telegram.org")
TELEGRAM_NOTIFY_EVENTS = set(
    event.strip()
    for event in os.environ.get("TELEGRAM_NOTIFY_EVENTS", "add_app,add_version,delete_app").split(",")
    if event.strip()
)

# IPA storage backend (Plan 011). Defaults to "local" -- today's behaviour,
# unchanged -- so merging this is a no-op until the flag is deliberately
# flipped. See GarageIpaStorage below for the "refuse to start" validation.
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")
GARAGE_S3_ENDPOINT = os.environ.get("GARAGE_S3_ENDPOINT")
GARAGE_S3_REGION = os.environ.get("GARAGE_S3_REGION", "garage")
GARAGE_S3_ACCESS_KEY_ID = os.environ.get("GARAGE_S3_ACCESS_KEY_ID")
GARAGE_S3_SECRET_ACCESS_KEY = os.environ.get("GARAGE_S3_SECRET_ACCESS_KEY")
GARAGE_BUCKET = os.environ.get("GARAGE_BUCKET")
GARAGE_PUBLIC_BASE_URL = os.environ.get("GARAGE_PUBLIC_BASE_URL")
GARAGE_KEY_PREFIX = os.environ.get("GARAGE_KEY_PREFIX", "ipas")

app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# SESSION_COOKIE_SECURE is intentionally left False: this deployment serves
# plain HTTP on a private network, and setting it would prevent the cookie
# from ever being stored. Set it to True as soon as TLS terminates in front.
app.config["SESSION_COOKIE_SECURE"] = False
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
                os.replace(src, filepath)
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
            code = e.response.get("Error", {}).get("Code", "unknown")
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

    def public_url(self, bundle_id, version):
        key = self._key(bundle_id, version)
        return f"{GARAGE_PUBLIC_BASE_URL.rstrip('/')}/{key}"


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
                os.replace(src, temporary)
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
            code = e.response.get("Error", {}).get("Code", "unknown")
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

    def public_url(self, bundle_id, ext):
        return f"{GARAGE_PUBLIC_BASE_URL.rstrip('/')}/{self._key(bundle_id, ext)}"


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
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > limit:
                        f.close()
                        os.remove(filepath)
                        raise ValueError(f"Download exceeded size limit of {limit} bytes")
                    f.write(chunk)

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
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if not chunk:
                        continue
                    total += len(chunk)
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
    
    def get_current_dates(self):
        """Get properly formatted dates for Feather compatibility"""
        current_date = datetime.now(timezone.utc)
        return {
            'feather_date': current_date.strftime("%Y-%m-%d"),
            'version_date': current_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
    
    def add_app_manual(self, data, ipa_file=None, download_from_url=False, icon_file=None, download_icon_from_url=False, base_url=None):
        """Add app manually with provided data"""
        with self._lock:
            source_data = self.load_source()
            if not source_data:
                return False, "Failed to load source data"
        
            dates = self.get_current_dates()
            bundle_id = data['bundleIdentifier']
            version = data['version']
            download_url = data.get('downloadURL', '')
            icon_url = data.get('iconURL', '')
        
            # Handle IPA file - upload, download, or use URL
            file_size = 0
            if ipa_file and allowed_file(ipa_file.filename):
                # Upload file
                filepath, file_size = self.save_ipa_file(ipa_file, bundle_id, version)
                if filepath:
                    download_url = self.get_local_ipa_url(bundle_id, version, base_url)
                else:
                    return False, f"Failed to save uploaded IPA for {bundle_id} {version}"
            elif download_from_url and download_url:
                # Download from URL
                filepath, file_size = self.download_ipa_from_url(download_url, bundle_id, version)
                if filepath:
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
            if icon_file and allowed_icon_file(icon_file.filename):
                # Upload icon file
                stored_icon_ext = self.save_icon_file(icon_file, bundle_id)
                if stored_icon_ext:
                    icon_url = self.get_hosted_icon_url(bundle_id, stored_icon_ext, base_url)
                else:
                    return False, f"Failed to save uploaded icon for {bundle_id}"
            elif download_icon_from_url and icon_url:
                # Download icon from URL
                stored_icon_ext = self.download_icon_from_url(icon_url, bundle_id)
                if stored_icon_ext:
                    icon_url = self.get_hosted_icon_url(bundle_id, stored_icon_ext, base_url)
                else:
                    return False, f"Failed to download icon from {icon_url}"

            new_app = {
                "name": data['name'],
                "bundleIdentifier": bundle_id,
                "developerName": data['developerName'],
                "localizedDescription": data.get('localizedDescription', ''),
                "iconURL": icon_url,
                "addedDate": dates['feather_date'],
                "versions": [{
                    "version": version,
                    "date": dates['version_date'],
                    "downloadURL": download_url,
                    "minOSVersion": data.get('minOSVersion', '14.0'),
                    "size": file_size
                }]
            }
        
            # Check if app already exists
            existing_index = None
            for i, app in enumerate(source_data['apps']):
                if app['bundleIdentifier'] == bundle_id:
                    existing_index = i
                    break
        
            if existing_index is not None:
                # Update existing app with new version
                source_data['apps'][existing_index]['versions'].insert(0, new_app['versions'][0])
                source_data['apps'][existing_index]['addedDate'] = dates['feather_date']
                logging.info(f"Updated app: {data['name']}")
            else:
                # Add new app
                source_data['apps'].append(new_app)
                logging.info(f"Added new app: {data['name']}")
        
            if self.save_source(source_data):
                if stored_icon_ext:
                    icon_storage.cleanup_variants(bundle_id, stored_icon_ext)
                return True, "App added successfully"
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
                # Delete IPA files before save (Plan 011 behavior), but only
                # delete the icon after the catalog removal is committed.
                if app_to_delete:
                    for version in app_to_delete.get('versions', []):
                        self.delete_ipa_file(bundle_identifier, version.get('version', ''))

                success = self.save_source(source_data)
                if success:
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
                return True, f"Version {version} already exists; nothing to add"

            download_url = version_data.get('downloadURL', '')
            file_size = version_data.get('size', 0)
        
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
                "date": dates['version_date'],
                "downloadURL": download_url,
                "minOSVersion": min_os,
                "size": file_size
            }
        
            # Insert at the beginning (latest version first)
            app['versions'].insert(0, new_version)
            app['addedDate'] = dates['feather_date']
        
            success = self.save_source(source_data)
            return success, "Version added successfully" if success else "Failed to add version"
    
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
        
            for key in ['name', 'subtitle', 'description', 'website', 'tintColor', 'iconURL']:
                if key in data and data[key]:
                    source_data[key] = data[key]
        
            success = self.save_source(source_data)
            return success, "Source information updated successfully" if success else "Failed to update source information"

# Initialize source manager
source_manager = SourceManager(SOURCE_FILE)

# Initialize IPA storage backend. STORAGE_BACKEND defaults to "local", so
# merging this is a no-op until the flag is deliberately flipped to
# "garage" -- see _require_garage_config for the refuse-to-start check.
if STORAGE_BACKEND == "garage":
    ipa_storage = GarageIpaStorage()
    icon_storage = GarageIconStorage()
else:
    ipa_storage = LocalIpaStorage()
    icon_storage = LocalIconStorage()


def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('authed'):
            return jsonify({"success": False, "error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return wrapper


# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    supplied = data.get('password', '')
    if hmac.compare_digest(supplied, ADMIN_PASSWORD):
        session['authed'] = True
        session.permanent = True
        return jsonify({"success": True})
    logging.warning("Failed login attempt from %s", request.remote_addr)
    return jsonify({"success": False, "error": "Invalid password"}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"success": True})

@app.route('/api/session')
def session_status():
    return jsonify({"authed": bool(session.get('authed'))})

@app.route('/source.json')
def serve_source():
    try:
        source_data = source_manager.load_source()
        if source_data is None:
            return send_file(SOURCE_FILE, mimetype='application/json')
        return jsonify(normalize_source(source_data))
    except Exception as e:
        logging.error(f"Error serving source: {str(e)}")
        return jsonify({"error": str(e)}), 404

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
        return jsonify({"error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500

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
                'downloadURL': request.form.get('downloadURL', ''),
                'minOSVersion': request.form.get('minOSVersion', '14.0')
            }
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

@app.route('/api/import-release', methods=['POST'])
@requires_auth
def import_release():
    payload = request.get_json(silent=True) or {}
    provider = (payload.get('provider') or '').strip().lower()
    project = _normalize_repo_project(payload.get('project') or '')
    bundle_id_in = (payload.get('bundleIdentifier') or '').strip()
    asset_glob = (payload.get('assetGlob') or '*.ipa').strip()
    include_pre = bool(payload.get('includePrereleases'))
    create_if_missing = bool(payload.get('createIfMissing'))
    name_in = (payload.get('name') or '').strip()
    developer_in = (payload.get('developerName') or '').strip()
    icon_url_in = (payload.get('iconURL') or '').strip()
    hosts_in = payload.get('allowedDownloadHosts') or []
    base_url = resolve_base_url()

    def event(**kw):
        return json.dumps(kw) + "\n"

    def run():
        tmp_path = None
        try:
            if provider not in ('github', 'gitlab'):
                yield event(stage="error", error="Provider must be github or gitlab")
                return
            if not project:
                yield event(stage="error", error="Repository is required")
                return
            if provider == 'github' and not release_ingest._GITHUB_PROJECT_RE.match(project):
                yield event(stage="error", error="GitHub repository must be owner/repo, e.g. RyanYuuki/AnymeX")
                return
            allowed = frozenset(h.strip().lower() for h in hosts_in if h.strip())
            if provider == 'gitlab' and not allowed:
                yield event(stage="error", error="GitLab imports require at least one allowed download host")
                return
            job = release_ingest.Job(
                id="ui-import", provider=provider, project=project,
                bundle_identifier=bundle_id_in or "", asset_glob=asset_glob,
                include_prereleases=include_pre, create_if_missing=create_if_missing,
                allowed_download_hosts=allowed, name=name_in or None, developer_name=developer_in or None,
            )
            tokens = {"github": os.environ.get("GITHUB_TOKEN"), "gitlab": os.environ.get("GITLAB_TOKEN")}
            session_req = requests.Session()
            yield event(stage="resolving")
            candidate = release_ingest.select_candidate(job, session_req, tokens, timeout=30)
            yield event(stage="resolved", asset=candidate.asset_name,
                        release=candidate.release_tag or candidate.release_id, size=candidate.declared_size)
            fd, tmp_path = tempfile.mkstemp(dir=UPLOAD_FOLDER, suffix=".ipa")
            os.close(fd)

            # ---- LIVE PROGRESS: queue + worker thread ----
            import queue
            q = queue.Queue()

            def cb(total, declared):
                q.put(("downloading", total, declared))

            def worker():
                try:
                    release_ingest.stream_download(
                        session_req, candidate, job, tmp_path, tokens,
                        release_ingest.DEFAULT_TIMEOUT, release_ingest.DEFAULT_MAX_BYTES,
                        progress_cb=cb,
                    )
                    q.put(("ok", None, None))
                except Exception as e:
                    q.put(("err", str(e), None))

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            while True:
                kind, a, b = q.get()
                if kind == "downloading":
                    pct = int(a * 100 / b) if b else None
                    yield event(stage="downloading", downloaded=a, total=b, pct=pct)
                elif kind == "ok":
                    break
                else:
                    yield event(stage="error", error=a)
                    return
            t.join()

            yield event(stage="validating")
            bundle_id, version, detected_name = release_ingest.extract_ipa_metadata(
                tmp_path, candidate.asset_name, job, candidate)
            if bundle_id_in and bundle_id != bundle_id_in:
                yield event(stage="error", error=f"IPA bundle id {bundle_id} does not match the id you entered ({bundle_id_in})")
                return
            yield event(stage="publishing")
            existing = source_manager.get_app(bundle_id)
            fs = FileStorage(stream=open(tmp_path, "rb"), filename=f"{secure_filename(version)}.ipa")
            if existing:
                ok, message = source_manager.add_version(bundle_id, {"version": version}, ipa_file=fs, base_url=base_url)
            elif create_if_missing:
                new_name = name_in or detected_name or bundle_id
                new_developer = developer_in or _repo_owner(project) or "Unknown"
                new_app = {"name": new_name, "bundleIdentifier": bundle_id,
                           "developerName": new_developer, "version": version}
                description = _clean_description(getattr(candidate, "release_body", ""))
                if description:
                    new_app["localizedDescription"] = description
                if icon_url_in:
                    new_app["iconURL"] = icon_url_in
                ok, message = source_manager.add_app_manual(
                    new_app, ipa_file=fs,
                    download_icon_from_url=bool(icon_url_in), base_url=base_url)
            else:
                yield event(stage="error", error=f"App {bundle_id} is not in the catalog. Tick 'create if missing' with a name + developer to add it.")
                return
            if ok:
                notify("add_version", f"Imported {bundle_id} {version} from {provider}:{project}\n{base_url}/source.json")
                yield event(stage="done", success=True, message=message, bundleIdentifier=bundle_id, version=version)
            else:
                yield event(stage="error", error=message)
        except (release_ingest.ProviderError, release_ingest.ValidationError, release_ingest.ConfigError) as e:
            yield event(stage="error", error=str(e))
        except Exception as e:
            logging.exception("import-release failed")
            yield event(stage="error", error=str(e))
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    return app.response_class(run(), mimetype="application/x-ndjson")

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


def _scan_catalog_health():
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
    source_data = source_manager.load_source()
    if not isinstance(source_data, dict):
        return issues

    apps = source_data.get('apps')
    if not isinstance(apps, list):
        return issues

    try:
        this_host = urlparse(resolve_base_url()).netloc.lower()
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
        return jsonify({"issues": _scan_catalog_health()})
    except Exception as e:
        logging.error(f"Health scan error: {str(e)}")
        return jsonify({"issues": [], "error": "scan failed"}), 200


def _reconcile_icons(apply=False):
    """Scan ICON_FOLDER for on-disk icons and upload any missing from the
    active backend (plan 040). Mirrors scripts/migrate_icons_to_garage.py's
    logic in-app so an operator without a repo checkout can repair icons
    that were never migrated after a STORAGE_BACKEND switch.

    Dry-run by default (apply=False): reports what would be uploaded without
    writing anything. apply=True uploads only icons missing from the active
    backend. Never deletes or modifies local icon files.

    For the local backend this is a no-op: on-disk icons ARE what's served.
    """
    if STORAGE_BACKEND != "garage":
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
        for bundle in sorted(os.listdir(ICON_FOLDER)):
            safe_bundle = secure_filename(bundle)
            if not safe_bundle or safe_bundle != bundle:
                continue
            bundle_dir = os.path.join(ICON_FOLDER, bundle)
            if not os.path.isdir(bundle_dir):
                continue
            for filename in sorted(os.listdir(bundle_dir)):
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

    return {
        "backend": "garage",
        "checked": checked,
        "would_upload": would_upload,
        "uploaded": uploaded,
        "skipped_existing": skipped_existing,
        "failed": failed,
        "items": items,
    }


@app.route('/api/reconcile-icons', methods=['POST'])
@requires_auth
def reconcile_icons():
    apply = bool((request.get_json(silent=True) or {}).get('apply'))
    try:
        return jsonify(_reconcile_icons(apply=apply))
    except Exception as e:
        logging.error(f"Icon reconcile error: {str(e)}")
        return jsonify({"error": "reconcile failed"}), 500


@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500

if __name__ == '__main__':
    logging.info("Starting AltStore Source Manager...")
    app.run(host='0.0.0.0', port=PORT, debug=False)
