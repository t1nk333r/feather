"""Telegram forward-to-bot IPA ingest worker (Plan 013).

Long-polls a self-hosted Telegram Bot API server (`telegram-bot-api --local`)
for updates. An allowlisted operator forwards an IPA to the bot as an
ordinary document, then sends `/add <bundleIdentifier> <version>` in a
second message (forwards cannot carry a caption). The worker validates the
file, computes its sha256, and publishes it to feather through the existing
HTTP API (`/api/login`, `/api/add-version`) -- it never writes to
`data/ipas/` or S3 directly.

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
import logging
import os
import sys
import time
import zipfile

import requests

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

USAGE_HINT = "Send:  /add <bundleIdentifier> <version>"
NOT_A_FILE_REPLY = (
    "That isn't a file. Forward an IPA, then send /add <bundleIdentifier> <version>."
)


class ConfigError(RuntimeError):
    """Required configuration is missing or invalid. Raised at startup only."""


class ValidationError(RuntimeError):
    """One of the five hard validation checks failed. Never a warning."""


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


def load_config(env=None):
    """Read and validate the eight required variables.

    Refuses to start (raises ConfigError) if any is missing -- naming only
    the missing *names*, never a value -- or if the allowlist is empty.
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

    return {
        "telegram_api_id": env["TELEGRAM_API_ID"],
        "telegram_api_hash": env["TELEGRAM_API_HASH"],
        "bot_token": env["TELEGRAM_BOT_TOKEN"],
        "allowed_user_ids": allowed_user_ids,
        "bot_api_base_url": env["BOT_API_BASE_URL"].rstrip("/"),
        "bot_api_file_root": env["BOT_API_FILE_ROOT"],
        "feather_base_url": env["FEATHER_BASE_URL"].rstrip("/"),
        "feather_admin_password": env["FEATHER_ADMIN_PASSWORD"],
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


class BotAPIClient:
    """Thin wrapper over the self-hosted Bot API's HTTP surface.

    Only two endpoints are used (getUpdates, getFile) plus sendMessage for
    replies -- plain `requests`, no telegram framework. See plan rationale.
    """

    def __init__(self, session, base_url, token):
        self.session = session
        self.base_url = base_url
        self.token = token

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
        resp = self.session.get(
            self._url("getFile"), params={"file_id": file_id}, timeout=30
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
        # Stream the upload -- requests streams file objects, never loads
        # the whole IPA into memory.
        with open(path, "rb") as fh:
            files = {"ipaFile": (os.path.basename(path), fh)}
            data = {"bundleIdentifier": bundle_id, "version": version}
            resp = self.session.post(
                f"{self.base_url}/api/add-version", data=data, files=files
            )
        if resp.status_code == 401:
            raise FeatherAuthError("feather /api/add-version returned 401")
        payload = resp.json()
        return bool(payload.get("success")), payload.get("error") or payload.get(
            "message"
        )


def handle_document(user_id, chat_id, document, config, bot, pending):
    filename = document.get("file_name") or ""
    declared_size = document.get("file_size")
    file_id = document.get("file_id")

    file_info = bot.get_file(file_id)
    local_path = resolve_local_path(
        file_info.get("file_path"), config["bot_api_file_root"]
    )

    try:
        validate_ipa_file(local_path, filename, declared_size)
    except ValidationError as e:
        pending.pop(user_id, None)
        bot.send_message(chat_id, f"Rejected: {e}")
        return

    digest = sha256_of_file(local_path)
    pending[user_id] = {
        "path": local_path,
        "filename": filename,
        "size": declared_size,
        "sha256": digest,
    }
    bot.send_message(
        chat_id,
        f"Got {filename} — {declared_size:,} bytes, sha256 {digest}.\n"
        + USAGE_HINT,
    )


def handle_add_command(user_id, chat_id, text, config, bot, feather, pending):
    parts = text.split()
    if len(parts) != 3:
        bot.send_message(chat_id, f"Usage: {USAGE_HINT.split(':', 1)[1].strip()}")
        return

    _, bundle_id, version = parts

    doc = pending.get(user_id)
    if doc is None:
        bot.send_message(
            chat_id,
            "No pending file. Forward an IPA first, then " + USAGE_HINT.strip(),
        )
        return

    try:
        feather.login()
        ok, message = feather.add_version(bundle_id, version, doc["path"])
    except FeatherAuthError:
        bot.send_message(
            chat_id, "Login to feather failed (401) -- check FEATHER_ADMIN_PASSWORD."
        )
        return
    except Exception as e:  # noqa: BLE001 -- surfaced to the operator, not silenced
        bot.send_message(chat_id, f"Publish failed: {e}")
        return

    if ok:
        bot.send_message(
            chat_id, f"Published {bundle_id} {version} ({doc['size']:,} bytes)."
        )
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


def run(config):
    session = requests.Session()
    bot = BotAPIClient(session, config["bot_api_base_url"], config["bot_token"])
    feather = FeatherClient(
        requests.Session(), config["feather_base_url"], config["feather_admin_password"]
    )
    pending = {}

    logger.info(
        "ipa-ingest-bot starting; allowlist has %d user id(s)",
        len(config["allowed_user_ids"]),
    )

    offset = 0
    while True:
        try:
            updates = bot.get_updates(offset, timeout=30)
        except Exception:
            logger.exception("getUpdates failed; retrying in 5s")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(update, config, bot, feather, pending)
            except Exception:
                # A malformed update, a mount mismatch, or any other
                # surprise must not kill the worker -- restart:
                # unless-stopped would mask a crash-loop as "working".
                logger.exception(
                    "Error handling update %s", update.get("update_id")
                )


def main():
    try:
        config = load_config()
    except ConfigError as e:
        logger.error(str(e))
        sys.exit(1)
    run(config)


if __name__ == "__main__":
    main()
