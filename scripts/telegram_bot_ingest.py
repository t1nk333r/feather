"""Telegram forward-to-bot IPA and APK ingest worker.

Long-polls a self-hosted Telegram Bot API server (`telegram-bot-api --local`)
for updates. An allowlisted operator forwards an IPA or APK as an ordinary
document, then sends `/add` to confirm publication. IPA metadata can be
overridden with `/add <bundleIdentifier> <version> [name]`; APK identity and
version are inspected by Feather's Android API. The worker validates the file,
computes its sha256, and publishes through `/api/add-version`, `/api/add-app`,
or `/api/android/add-apk`. It never writes catalog or artifact storage
directly.

Two things this module deliberately does NOT do:

  * No HTTP download of the file. With `--local`, `getFile` returns an
    absolute path inside the Bot API server's data directory, and that same
    directory is mounted read-only into this container at the same
    mountpoint (`BOT_API_FILE_ROOT`). A 353 MB "download" is a file read.
    If the path getFile reports does not exist here, the volume mounts
    disagree -- see `resolve_local_path` / `MountMismatchError`. That is
    never papered over with a fallback HTTP fetch, which would reintroduce
    the 20 MB cloud Bot API cap this whole plan exists to avoid.

  * No allowance for an empty allowlist. `TELEGRAM_ALLOWED_USER_IDS` is
    checked before anything else happens with an update, and an empty or
    unset allowlist is a fatal startup error, not "allow everyone" -- see
    `load_config`.

No network call happens at import time; `main()` is the only entry point
that touches the network, so this module can be imported and unit tested
without a network connection. See plans/013-telegram-bot-ingest.md.
"""

import hashlib
import json
import logging
import os
import plistlib
import re
import sqlite3
import sys
import time
import traceback
import zipfile
from dataclasses import asdict

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder

try:
    from .ipa_inspection import InspectionError, IpaInspection, inspect_ipa
except ImportError:  # direct execution from scripts/
    from ipa_inspection import InspectionError, IpaInspection, inspect_ipa

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("telegram_bot_ingest")

REQUIRED_VARS = [
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USER_IDS",
    "BOT_API_BASE_URL",
    "BOT_API_FILE_ROOT",
    "FEATHER_BASE_URL",
    "FEATHER_ADMIN_PASSWORD",
]

DEFAULT_PENDING_DB = "/var/lib/feather-bot/pending.sqlite3"
USAGE_HINT = "Send:  /add <bundleIdentifier> <version> [name]"
NOT_A_FILE_REPLY = (
    "That isn't a file. Forward an IPA or APK, then send /add."
)

# getFile blocks for the entire download in --local mode (not documented by
# the cloud Bot API docs, which describe the cloud behaviour). 900s at a
# pessimistic 1 MB/s covers ~900 MB -- comfortably past the largest file in
# the catalog (353 MB). It's a ceiling, not a delay: a fast transfer
# returns immediately. Optional/plan-020 -- BOT_API_GETFILE_TIMEOUT is
# deliberately not in REQUIRED_VARS, so an unset value must not break the
# running deployment on restart.
DEFAULT_GETFILE_TIMEOUT = 900


class ConfigError(RuntimeError):
    """Required configuration is missing or invalid. Raised at startup only."""


class ValidationError(RuntimeError):
    """A forwarded IPA or APK failed a hard validation check."""


class MountMismatchError(RuntimeError):
    """getFile returned a path that does not exist inside this worker.

    This means the shared volume mount between telegram-bot-api and this
    worker disagrees with what the API server reported. This is never
    handled by falling back to an HTTP download -- see module docstring.
    """


class FeatherAuthError(RuntimeError):
    """feather's /api/login (or a later call) returned 401.

    Called out as its own exception because, post-Plan-010, a bad or
    missing FEATHER_ADMIN_PASSWORD is the most likely first failure and a
    generic error would send someone hunting in the wrong place.
    """


_MISSING = object()


class PendingStore(dict):
    """Persistent pending upload state keyed by Telegram user id.

    The bot's in-memory `pending` dict was lost on process restarts, which
    made `/add` fail with "No pending file" after a restart even though the
    operator had just forwarded the IPA. This keeps the pending set in a
    SQLite database so the bot can recover the last upload after a restart.
    """

    def __init__(self, db_path=None):
        self.db_path = db_path or os.environ.get("TELEGRAM_PENDING_DB") or DEFAULT_PENDING_DB
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_db()
        super().__init__()
        self._load()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending (
                    user_id INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )

    def _load(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, payload FROM pending"
            ).fetchall()
        for user_id, payload in rows:
            value = json.loads(payload)
            inspection = value.get("inspection")
            if inspection is not None:
                inspection["supported_platforms"] = tuple(
                    inspection.get("supported_platforms", ())
                )
                inspection["device_families"] = tuple(
                    inspection.get("device_families", ())
                )
                value["inspection"] = IpaInspection(**inspection)
            self[int(user_id)] = value

    def _persist(self, user_id, value):
        payload = dict(value)
        inspection = payload.get("inspection")
        if isinstance(inspection, IpaInspection):
            payload["inspection"] = asdict(inspection)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending(user_id, payload)
                VALUES(?, ?)
                ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload
                """,
                (int(user_id), json.dumps(payload, separators=(",", ":"))),
            )

    def _delete(self, user_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM pending WHERE user_id = ?", (int(user_id),))

    def __setitem__(self, user_id, value):
        super().__setitem__(user_id, value)
        self._persist(user_id, value)

    def __delitem__(self, user_id):
        super().__delitem__(user_id)
        self._delete(user_id)

    def pop(self, key, default=_MISSING):
        if key in self:
            value = super().pop(key)
            self._delete(key)
            return value
        if default is not _MISSING:
            return default
        raise KeyError(key)

    def clear(self):
        super().clear()
        with self._connect() as conn:
            conn.execute("DELETE FROM pending")


def _redact(text, *secrets):
    """Replace every occurrence of any given secret substring with <REDACTED>.

    The Bot API puts the token in the URL *path*, and `requests` embeds the
    full request URL in every exception it raises, so any log line or
    user-facing message built from an exception -- present or future --
    can carry a live credential unless it passes through here first. This
    is called centrally at every site that logs or replies with exception
    text, rather than patched in at the one call site that is known to
    leak today.

    Accepts multiple secrets (bot token, feather admin password, Telegram
    API hash) so callers can scrub everything sensitive that might appear
    in a single pass. Only genuine credentials belong here -- non-secret
    configuration (e.g. BOT_API_FILE_ROOT, a mountpoint) must NOT be
    passed, because scrubbing a path prefix destroys the diagnostic value
    of path errors.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<REDACTED>")
    return text


def _log_exception(config, msg):
    """logger.exception, but with secrets scrubbed from the traceback.

    The default `logger.exception` lets the logging module format the
    traceback straight from `sys.exc_info()`, which would bypass any
    redaction applied to a message string. This formats the traceback
    itself so it can be redacted before it reaches the log handler.
    """
    logger.error(
        _redact(
            f"{msg}\n{traceback.format_exc()}",
            config.get("bot_token"),
            config.get("feather_admin_password"),
            config.get("telegram_api_hash"),
        )
    )


def _format_size(num_bytes):
    """Human-readable size for the pre-fetch acknowledgement, e.g. '31.1 MB'."""
    if num_bytes is None:
        return "unknown size"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _chat_id_from_update(update):
    """Best-effort chat id, for the polling loop's failure reply.

    Mirrors the extraction `handle_update` does for the happy path. Used
    when the loop needs to reply to a chat *after* `handle_update` has
    already raised, so it cannot rely on a value computed inside the
    handler that just failed.
    """
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    return chat.get("id")


def load_config(env=None):
    """Read and validate the eight required variables, plus two optional ones.

    Refuses to start (raises ConfigError) if any required variable is
    missing -- naming only the missing *names*, never a value -- or if the
    allowlist is empty. BOT_API_GETFILE_TIMEOUT is optional and defaults to
    DEFAULT_GETFILE_TIMEOUT; TELEGRAM_DEFAULT_DEVELOPER (plan 023) is
    optional and defaults to "Unknown". Both are deliberately not in
    REQUIRED_VARS -- an unset value must not break the running deployment
    on restart.
    """
    env = os.environ if env is None else env

    missing = [name for name in REQUIRED_VARS if not env.get(name)]
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )

    raw_ids = env["TELEGRAM_ALLOWED_USER_IDS"]
    try:
        allowed_user_ids = {
            int(part.strip()) for part in raw_ids.split(",") if part.strip()
        }
    except ValueError:
        raise ConfigError(
            "TELEGRAM_ALLOWED_USER_IDS must be a comma-separated list of "
            "numeric Telegram user ids"
        )

    if not allowed_user_ids:
        # Deliberately fatal, not "allow everyone". See module docstring.
        raise ConfigError(
            "TELEGRAM_ALLOWED_USER_IDS is empty -- refusing to start with an "
            "allowlist that would permit anyone to publish to the catalog"
        )

    raw_timeout = env.get("BOT_API_GETFILE_TIMEOUT")
    if raw_timeout:
        try:
            getfile_timeout = int(raw_timeout)
        except ValueError:
            raise ConfigError(
                "BOT_API_GETFILE_TIMEOUT must be an integer number of seconds"
            )
    else:
        getfile_timeout = DEFAULT_GETFILE_TIMEOUT

    default_developer = env.get("TELEGRAM_DEFAULT_DEVELOPER") or "Unknown"
    pending_db = env.get("TELEGRAM_PENDING_DB") or DEFAULT_PENDING_DB

    return {
        "telegram_api_id": env["TELEGRAM_API_ID"],
        "telegram_api_hash": env["TELEGRAM_API_HASH"],
        "bot_token": env["TELEGRAM_BOT_TOKEN"],
        "allowed_user_ids": allowed_user_ids,
        "bot_api_base_url": env["BOT_API_BASE_URL"].rstrip("/"),
        "bot_api_file_root": env["BOT_API_FILE_ROOT"],
        "feather_base_url": env["FEATHER_BASE_URL"].rstrip("/"),
        "feather_admin_password": env["FEATHER_ADMIN_PASSWORD"],
        "bot_api_getfile_timeout": getfile_timeout,
        "telegram_default_developer": default_developer,
        "telegram_pending_db": pending_db,
    }


def sha256_of_file(path, chunk_size=1024 * 1024):
    """Stream sha256 in chunks -- never read a 353 MB file into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_local_path(file_path, file_root):
    """Turn getFile's reported path into a path this worker can read.

    With --local, file_path is already absolute and lives under the same
    mountpoint this worker has mounted read-only (BOT_API_FILE_ROOT). No
    translation, no HTTP fallback -- just confirm the mounts agree.
    """
    if not file_path:
        raise MountMismatchError("getFile response did not include a file_path")
    if not os.path.isabs(file_path):
        raise MountMismatchError(f"getFile returned a non-absolute path: {file_path!r}")
    if not os.path.exists(file_path):
        raise MountMismatchError(
            f"path from getFile does not exist inside the worker: {file_path!r} "
            f"(BOT_API_FILE_ROOT={file_root!r}) -- shared volume mounts disagree"
        )
    return file_path


def validate_ipa_file(path, filename, declared_size):
    """Checks 2-5 of the five hard validation checks.

    Check 1 ("the update contains a document") is the caller's job, since
    it depends on the raw update rather than a file on disk. All of these
    are hard failures -- do not downgrade any of them to a warning; three
    entries in the live catalog are gzip'd HTML precisely because bytes
    once went in unchecked.
    """
    if not filename.lower().endswith(".ipa"):
        raise ValidationError(f"filename {filename!r} does not end in .ipa")

    if not zipfile.is_zipfile(path):
        raise ValidationError("file is not a valid zip archive (not an IPA)")

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
    if not any(name.startswith("Payload/") for name in names):
        raise ValidationError("archive has no Payload/ entry (not an IPA)")

    actual_size = os.path.getsize(path)
    if actual_size != declared_size:
        raise ValidationError(
            f"size mismatch: on-disk {actual_size} bytes, Telegram reported "
            f"{declared_size} bytes"
        )


def validate_apk_file(path, filename, declared_size):
    """Reject obvious non-APK content before sending bytes to Feather."""
    if not filename.lower().endswith(".apk"):
        raise ValidationError(f"filename {filename!r} does not end in .apk")

    if not zipfile.is_zipfile(path):
        raise ValidationError("file is not a valid zip archive (not an APK)")

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    if "AndroidManifest.xml" not in names:
        raise ValidationError("archive has no AndroidManifest.xml entry (not an APK)")

    actual_size = os.path.getsize(path)
    if actual_size != declared_size:
        raise ValidationError(
            f"size mismatch: on-disk {actual_size} bytes, Telegram reported "
            f"{declared_size} bytes"
        )


def inspect_ipa_metadata(path, config=None):
    """Return the shared inspection model, or None for operator-overridable failures."""
    try:
        return inspect_ipa(path, os.path.basename(path) or "IPA")
    except InspectionError as exc:
        secrets = []
        if config:
            secrets = [
                config.get("bot_token"),
                config.get("feather_admin_password"),
                config.get("telegram_api_hash"),
            ]
        logger.warning(_redact(f"Failed to inspect IPA: {exc}", *secrets))
        return None


def extract_ipa_metadata(path, config=None):
    """Read (bundle_id, version, name) from an IPA's top-level Info.plist.

    Returns (None, None, None) if anything is unreadable -- a missing or
    odd plist is not a validation failure, it just means the operator must
    supply the values explicitly.

    The regex is deliberately exact: an IPA contains an Info.plist for
    every bundled framework and app extension (235 of them in one of the
    IPAs on disk), and a loose match would return a framework's identifier
    instead of the app's.

    `name` (plan 023) is CFBundleDisplayName, falling back to
    CFBundleName -- the display name is the user-facing one
    ("YouTube" vs "anymex"). A missing name is not a failure: bundle id
    and version are what matter, and `name` is simply None when absent.
    It is only used at app-creation time, when the bot must invent a
    catalogue entry that didn't exist before.

    `config`, if given, is used only to redact secrets out of the warning
    logged on failure -- it is never required for a successful read, which
    is why the standalone verification command in plans/021 can call this
    with just a path.
    """
    inspection = inspect_ipa_metadata(path, config)
    if inspection is None:
        return None, None, None
    return inspection.bundle_identifier, inspection.version, inspection.name


class BotAPIClient:
    """Thin wrapper over the self-hosted Bot API's HTTP surface.

    Only two endpoints are used (getUpdates, getFile) plus sendMessage for
    replies -- plain `requests`, no telegram framework. See plan rationale.
    """

    def __init__(self, session, base_url, token, getfile_timeout=DEFAULT_GETFILE_TIMEOUT):
        self.session = session
        self.base_url = base_url
        self.token = token
        # Only get_file uses this. getUpdates (long-poll) and sendMessage
        # keep their own fixed, short timeouts -- see plan 020.
        self.getfile_timeout = getfile_timeout

    def _url(self, method):
        return f"{self.base_url}/bot{self.token}/{method}"

    def get_updates(self, offset, timeout=30):
        resp = self.session.get(
            self._url("getUpdates"),
            params={"offset": offset, "timeout": timeout},
            timeout=timeout + 10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"getUpdates failed: {data}")
        return data["result"]

    def get_file(self, file_id):
        # --local mode blocks for the entire download here, unlike the
        # cloud API -- see DEFAULT_GETFILE_TIMEOUT's comment.
        resp = self.session.get(
            self._url("getFile"),
            params={"file_id": file_id},
            timeout=self.getfile_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"getFile failed: {data}")
        return data["result"]

    def send_message(self, chat_id, text):
        resp = self.session.post(
            self._url("sendMessage"),
            data={"chat_id": chat_id, "text": text},
            timeout=30,
        )
        resp.raise_for_status()


class FeatherClient:
    """Talks to feather's existing HTTP API -- never touches storage directly."""

    def __init__(self, session, base_url, password):
        self.session = session
        self.base_url = base_url
        self.password = password
        self._inspection = None

    def set_preflight(self, inspection):
        self._inspection = inspection

    def login(self):
        resp = self.session.post(
            f"{self.base_url}/api/login",
            json={"password": self.password},
            timeout=30,
        )
        if resp.status_code == 401:
            raise FeatherAuthError("feather /api/login returned 401")
        resp.raise_for_status()

    def add_version(self, bundle_id, version, path):
        # MultipartEncoder reads bounded chunks; requests' files= buffers
        # the entire multipart body before sending it.
        with open(path, "rb") as fh:
            data = {"bundleIdentifier": bundle_id, "version": version}
            if self._inspection is not None:
                data["buildVersion"] = self._inspection.build_version
                if self._inspection.minimum_os_version:
                    data["minOSVersion"] = self._inspection.minimum_os_version
            data["ipaFile"] = (os.path.basename(path), fh)
            encoder = MultipartEncoder(fields=data)
            resp = self.session.post(
                f"{self.base_url}/api/add-version",
                data=encoder,
                headers={"Content-Type": encoder.content_type},
            )
        if resp.status_code == 401:
            raise FeatherAuthError("feather /api/add-version returned 401")
        payload = resp.json()
        return bool(payload.get("success")), payload.get("error") or payload.get(
            "message"
        )

    def add_app(self, bundle_id, version, name, developer, path):
        # Plan 023: called only when add_version fails with the exact
        # message "App not found" -- creates the catalogue entry. Sends
        # the IPA (ipaFile) exactly like add_version does; a created app
        # with no binary would point at nothing. Never send an icon here
        # -- /api/add-app's multipart branch reads ipaFile only and drops
        # icon_file on the floor; /api/update-app is the endpoint that
        # accepts one (see set_icon).
        with open(path, "rb") as fh:
            data = {
                "bundleIdentifier": bundle_id,
                "version": version,
                "name": name,
                "developerName": developer,
            }
            if self._inspection is not None:
                data["buildVersion"] = self._inspection.build_version
                if self._inspection.minimum_os_version:
                    data["minOSVersion"] = self._inspection.minimum_os_version
                if self._inspection.privacy:
                    data["privacy"] = json.dumps(self._inspection.privacy)
            data["ipaFile"] = (os.path.basename(path), fh)
            encoder = MultipartEncoder(fields=data)
            resp = self.session.post(
                f"{self.base_url}/api/add-app",
                data=encoder,
                headers={"Content-Type": encoder.content_type},
            )
        if resp.status_code == 401:
            raise FeatherAuthError("feather /api/add-app returned 401")
        payload = resp.json()
        return bool(payload.get("success")), payload.get("error") or payload.get(
            "message"
        )

    def add_apk(self, path):
        """Stream an APK to Feather; the server owns manifest inspection."""
        with open(path, "rb") as fh:
            encoder = MultipartEncoder(
                fields={"apkFile": (os.path.basename(path), fh)}
            )
            resp = self.session.post(
                f"{self.base_url}/api/android/add-apk",
                data=encoder,
                headers={"Content-Type": encoder.content_type},
            )
        if resp.status_code == 401:
            raise FeatherAuthError("feather /api/android/add-apk returned 401")
        payload = resp.json()
        return (
            bool(payload.get("success")),
            payload.get("error") or payload.get("message"),
            payload,
        )

    def set_icon(self, bundle_id, path):
        # Plan 023: only ever called right after add_app, on the creation
        # path -- never when merely appending a version, so an operator's
        # hand-chosen icon is never reverted by a later forward. Callers
        # must treat any failure here as non-fatal to the publish.
        with open(path, "rb") as fh:
            data = {"bundleIdentifier": bundle_id}
            data["iconFile"] = (os.path.basename(path), fh)
            encoder = MultipartEncoder(fields=data)
            resp = self.session.post(
                f"{self.base_url}/api/update-app",
                data=encoder,
                headers={"Content-Type": encoder.content_type},
            )
        if resp.status_code == 401:
            raise FeatherAuthError("feather /api/update-app returned 401")
        payload = resp.json()
        return bool(payload.get("success")), payload.get("error") or payload.get(
            "message"
        )


def handle_document(user_id, chat_id, document, config, bot, pending):
    filename = document.get("file_name") or ""
    declared_size = document.get("file_size")
    file_id = document.get("file_id")
    thumbnail = document.get("thumbnail")

    # A new forward always supersedes the previous one. Clearing only on a
    # validation error left the old file pending when getFile or the mount
    # check failed, and the user's next /add published that older file.
    pending.pop(user_id, None)

    # Acknowledge before the blocking call: getFile can take minutes for a
    # large file in --local mode, and without this a slow download is
    # indistinguishable from a dead bot. A failed courtesy message must not
    # block the actual fetch, so it's caught and only logged.
    try:
        bot.send_message(
            chat_id,
            f"Fetching {filename} ({_format_size(declared_size)}) from Telegram "
            "— this can take a few minutes for large files.",
        )
    except Exception:
        _log_exception(config, "Failed to send fetch acknowledgement to chat %s" % chat_id)

    file_info = bot.get_file(file_id)
    local_path = resolve_local_path(
        file_info.get("file_path"), config["bot_api_file_root"]
    )

    artifact_type = None
    try:
        if filename.lower().endswith(".ipa"):
            artifact_type = "ipa"
            validate_ipa_file(local_path, filename, declared_size)
        elif filename.lower().endswith(".apk"):
            artifact_type = "apk"
            validate_apk_file(local_path, filename, declared_size)
        else:
            raise ValidationError(
                f"filename {filename!r} must end in .ipa or .apk"
            )
    except ValidationError as e:
        pending.pop(user_id, None)
        bot.send_message(chat_id, f"Rejected: {e}")
        return

    digest = sha256_of_file(local_path)

    if artifact_type == "apk":
        pending[user_id] = {
            "artifact_type": "apk",
            "path": local_path,
            "filename": filename,
            "size": declared_size,
            "sha256": digest,
        }
        bot.send_message(
            chat_id,
            f"Got Android APK {filename} — {declared_size:,} bytes, "
            f"sha256 {digest}.\n\nSend /add to inspect and publish it.",
        )
        return
    inspection = inspect_ipa_metadata(local_path, config)
    if inspection is not None and inspection.platform == "tvos":
        pending.pop(user_id, None)
        bot.send_message(chat_id, "Rejected: tvOS binaries are not supported")
        return
    bundle_id = inspection.bundle_identifier if inspection else None
    version = inspection.version if inspection else None
    name = inspection.name if inspection else None

    # Plan 023: the icon source is Telegram's own thumbnail on the
    # forwarded document, fetched the same way as the main file --
    # getFile, then resolve_local_path. No HTTP fallback, same rule as
    # the IPA. Absence is not an error: a missing thumbnail just means
    # the created app (if any) gets no icon.
    thumb_path = None
    if thumbnail and thumbnail.get("file_id"):
        thumb_info = bot.get_file(thumbnail["file_id"])
        thumb_path = resolve_local_path(
            thumb_info.get("file_path"), config["bot_api_file_root"]
        )

    pending[user_id] = {
        "artifact_type": "ipa",
        "path": local_path,
        "filename": filename,
        "size": declared_size,
        "sha256": digest,
        "bundle_id": bundle_id,
        "version": version,
        "name": name,
        "inspection": inspection,
        "thumb_path": thumb_path,
    }

    header = f"Got {filename} — {declared_size:,} bytes, sha256 {digest}."
    if bundle_id and version:
        platform = inspection.platform
        minimum = inspection.minimum_os_version or "unknown"
        bot.send_message(
            chat_id,
            f"{header}\n"
            f"Detected: {bundle_id}  {version} (build {inspection.build_version}, "
            f"platform {platform}, minimum OS {minimum})\n\n"
            "Send /add to publish that, or /add <bundleIdentifier> <version> "
            "to override.",
        )
    else:
        bot.send_message(
            chat_id,
            f"{header}\n"
            "Could not read the bundle identifier from the IPA.\n"
            + USAGE_HINT,
        )


def handle_add_apk_command(user_id, chat_id, text, config, bot, feather, pending):
    if text.strip() != "/add":
        bot.send_message(chat_id, "Usage: /add")
        return

    doc = pending[user_id]
    try:
        feather.login()
        ok, message, payload = feather.add_apk(doc["path"])
    except FeatherAuthError:
        bot.send_message(
            chat_id, "Login to feather failed (401) -- check FEATHER_ADMIN_PASSWORD."
        )
        return
    except Exception as e:  # noqa: BLE001 -- surfaced to the operator
        bot.send_message(chat_id, f"Publish failed: {e}")
        return

    if not ok:
        bot.send_message(chat_id, f"Publish failed: {message}")
        return

    if payload.get("added") is False:
        reply = message or "APK is already present."
    else:
        package = payload.get("package") or "unknown package"
        version_code = payload.get("versionCode")
        reply = f"Published Android APK {package} versionCode {version_code}."
    bot.send_message(chat_id, reply)
    pending.pop(user_id, None)


def handle_add_command(user_id, chat_id, text, config, bot, feather, pending):
    parts = text.split(maxsplit=3)
    if len(parts) not in (1, 3, 4):
        bot.send_message(chat_id, f"Usage: {USAGE_HINT.split(':', 1)[1].strip()}")
        return

    # Pending existence is checked before either branch reads values off of
    # it, so a bare /add with nothing pending gets "No pending file" rather
    # than a crash on `doc["bundle_id"]`.
    doc = pending.get(user_id)
    if doc is None:
        bot.send_message(
            chat_id,
            "No pending file. Forward an IPA or APK first, then send /add.",
        )
        return

    if doc.get("artifact_type") == "apk":
        handle_add_apk_command(
            user_id, chat_id, text, config, bot, feather, pending
        )
        return

    name_override = None
    if len(parts) == 1:
        bundle_id = doc.get("bundle_id")
        version = doc.get("version")
        if not bundle_id or not version:
            bot.send_message(
                chat_id,
                "No bundle identifier/version was detected for this file. "
                f"Usage: {USAGE_HINT.split(':', 1)[1].strip()}",
            )
            return
    else:
        bundle_id = parts[1]
        version = parts[2]
        if len(parts) == 4:
            name_override = parts[3].strip() or None

    created = False
    icon_note = None

    try:
        feather.login()
        if hasattr(feather, "set_preflight"):
            feather.set_preflight(doc.get("inspection"))
        ok, message = feather.add_version(bundle_id, version, doc["path"])

        # Plan 023: create the app only on the exact message "App not
        # found" -- anything looser (a bare `if not ok`, or a substring
        # match) would turn an unrelated failure, e.g. "Failed to save
        # source data", into a spurious catalogue entry. See test 5.
        if not ok and message == "App not found":
            app_name = name_override or doc.get("name") or bundle_id
            developer = config["telegram_default_developer"]
            ok, message = feather.add_app(
                bundle_id, version, app_name, developer, doc["path"]
            )
            if ok:
                created = True
                # Icon is set only on creation, never on a plain version
                # append -- otherwise an operator's hand-chosen icon
                # would be reverted by the next forward (test 9).
                thumb_path = doc.get("thumb_path")
                if thumb_path:
                    try:
                        feather.set_icon(bundle_id, thumb_path)
                        icon_note = "Icon set."
                    except Exception:
                        # The binary is the point; the icon is
                        # decoration -- a failure here must never fail
                        # the publish (test 10).
                        _log_exception(
                            config, f"Failed to set icon for {bundle_id}"
                        )
                        icon_note = "Could not set the icon."
    except FeatherAuthError:
        bot.send_message(
            chat_id, "Login to feather failed (401) -- check FEATHER_ADMIN_PASSWORD."
        )
        return
    except Exception as e:  # noqa: BLE001 -- surfaced to the operator, not silenced
        bot.send_message(chat_id, f"Publish failed: {e}")
        return

    if ok:
        lines = []
        if created:
            app_name = name_override or doc.get("name") or bundle_id
            lines.append(
                f'{bundle_id} is not in the catalog — creating it as "{app_name}".'
            )
        lines.append(f"Published {bundle_id} {version} ({doc['size']:,} bytes).")
        if icon_note:
            lines.append(icon_note)
        bot.send_message(chat_id, "\n".join(lines))
        pending.pop(user_id, None)
    else:
        bot.send_message(chat_id, f"Publish failed: {message}")


def handle_update(update, config, bot, feather, pending):
    message = update.get("message")
    if not message:
        # Not a plain message (e.g. edited_message, channel_post) -- ignore.
        return

    sender = message.get("from") or {}
    user_id = sender.get("id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    # Check 1 of the security model, before anything else: the allowlist.
    # Rejected senders get no reply at all -- a reply would confirm the bot
    # exists and is live.
    if user_id not in config["allowed_user_ids"]:
        logger.warning("Rejected update from disallowed user id %r", user_id)
        return

    if "document" in message:
        handle_document(user_id, chat_id, message["document"], config, bot, pending)
        return

    text = (message.get("text") or "").strip()
    if text.startswith("/add"):
        handle_add_command(user_id, chat_id, text, config, bot, feather, pending)
        return

    # Neither a document nor a recognised command -- covers validation
    # check 1 ("the update contains a document, else reply that isn't a
    # file") for anything else that reaches this point.
    bot.send_message(chat_id, NOT_A_FILE_REPLY)


def process_update(update, config, bot, feather, pending):
    """Handle one update, catching and reporting any failure.

    A malformed update, a mount mismatch, a getFile 400, or any other
    surprise must not kill the worker -- restart: unless-stopped would mask
    a crash-loop as "working" -- but it must not be silent either: that is
    what made a real failure four exchanges to diagnose. So this attempts a
    reply to the originating chat naming what went wrong, then swallows the
    error either way. The reply attempt has its own try/except: if
    Telegram itself is unreachable, the worker still must not die.
    """
    try:
        handle_update(update, config, bot, feather, pending)
    except Exception as e:
        _log_exception(config, f"Error handling update {update.get('update_id')}")

        chat_id = _chat_id_from_update(update)
        if chat_id is None:
            return
        try:
            bot.send_message(
                chat_id,
                _redact(
                    f"Something went wrong: {type(e).__name__}: {e}",
                    config.get("bot_token"),
                    config.get("feather_admin_password"),
                    config.get("telegram_api_hash"),
                ),
            )
        except Exception:
            _log_exception(config, f"Failed to send error reply to chat {chat_id}")


def run(config):
    session = requests.Session()
    bot = BotAPIClient(
        session,
        config["bot_api_base_url"],
        config["bot_token"],
        getfile_timeout=config["bot_api_getfile_timeout"],
    )
    feather = FeatherClient(
        requests.Session(), config["feather_base_url"], config["feather_admin_password"]
    )
    pending = PendingStore(config["telegram_pending_db"])

    logger.info(
        "telegram ingest bot starting; allowlist has %d user id(s)",
        len(config["allowed_user_ids"]),
    )

    offset = 0
    while True:
        try:
            updates = bot.get_updates(offset, timeout=30)
        except Exception:
            _log_exception(config, "getUpdates failed; retrying in 5s")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            process_update(update, config, bot, feather, pending)


def main():
    try:
        config = load_config()
    except ConfigError as e:
        logger.error(str(e))
        sys.exit(1)
    run(config)


if __name__ == "__main__":
    main()
