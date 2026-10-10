"""Tests for the Telegram forward-to-bot ingest worker (Plan 013).

No test contacts Telegram, Docker, or an external service. Most HTTP
clients are hand-rolled fakes; upload contract tests use a loopback HTTP
server that decodes the multipart body as Feather does. BOT_API_FILE_ROOT
points at pytest's tmp_path instead of a real shared volume.

See plans/013-telegram-bot-ingest.md's test plan for the eight cases below.
"""

import builtins
import gzip
import hashlib
import json
import plistlib
import threading
from types import SimpleNamespace
import zipfile

import pytest
from werkzeug.serving import make_server
from werkzeug.wrappers import Request, Response

from scripts import telegram_bot_ingest as ingest


ALLOWED_USER_ID = 12345
OTHER_USER_ID = 99999
CHAT_ID = 555


def make_env(**overrides):
    env = {
        "TELEGRAM_API_ID": "1",
        "TELEGRAM_API_HASH": "hash",
        "TELEGRAM_BOT_TOKEN": "not-a-real-token",
        "TELEGRAM_ALLOWED_USER_IDS": str(ALLOWED_USER_ID),
        "BOT_API_BASE_URL": "http://telegram-bot-api:8081",
        "BOT_API_FILE_ROOT": "/var/lib/telegram-bot-api",
        "FEATHER_BASE_URL": "http://altstore-manager:5000",
        "FEATHER_ADMIN_PASSWORD": "test-password-not-a-real-secret",
    }
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# Fakes -- record calls, never touch the network.
# ---------------------------------------------------------------------------


class FakeBotAPI:
    def __init__(
        self,
        get_file_result=None,
        get_file_results=None,
        get_file_error=None,
        send_message_error=None,
    ):
        self.get_file_result = get_file_result or {}
        # Plan 023: per-file_id results, so a thumbnail getFile can return
        # something different from the main document's getFile in the
        # same test. Falls back to get_file_result when a file_id isn't
        # in the map -- existing tests that only ever fetch one file_id
        # are unaffected.
        self.get_file_results = get_file_results or {}
        self.get_file_error = get_file_error
        self.send_message_error = send_message_error
        self.sent_messages = []  # (chat_id, text)
        self.calls = []  # ordered list of "get_file" / "send_message"

    def get_file(self, file_id):
        self.calls.append("get_file")
        if self.get_file_error is not None:
            raise self.get_file_error
        return self.get_file_results.get(file_id, self.get_file_result)

    def send_message(self, chat_id, text):
        self.calls.append("send_message")
        if self.send_message_error is not None:
            raise self.send_message_error
        self.sent_messages.append((chat_id, text))


class _FakeResponse:
    """Records only what BotAPIClient touches -- status/json, nothing real."""

    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """Records the kwargs BotAPIClient's requests are made with.

    Used only to verify timeout plumbing (Step 1) -- never touches the
    network, mirroring the rest of this file's no-network rule.
    """

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"ok": True, "result": {}}
        self.calls = []  # (method, url, kwargs)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _FakeResponse(self.payload)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _FakeResponse(self.payload)


@pytest.fixture
def feather_http():
    """Decode real HTTP requests, not Requests' implementation kwargs."""
    calls = []
    replies = {}

    def app(environ, start_response):
        request = Request(environ)
        call = {"path": request.path, "method": request.method}
        if request.path == "/api/login":
            call["json"] = request.get_json()
        else:
            call["fields"] = request.form.to_dict()
            call["files"] = {}
            for field, upload in request.files.items():
                digest = hashlib.sha256()
                size = 0
                for chunk in iter(lambda: upload.stream.read(64 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
                call["files"][field] = {
                    "filename": upload.filename,
                    "sha256": digest.hexdigest(),
                    "size": size,
                }
            call["cookie"] = request.cookies.get("feather_session")
            call["client_header"] = request.headers.get("X-Test-Client")
            call["content_length"] = request.content_length
            call["content_type"] = request.mimetype
        calls.append(call)
        status, payload = replies.get(
            request.path, (200, {"success": True, "message": "ok"})
        )
        response = Response(
            json.dumps(payload), status=status, content_type="application/json"
        )
        if request.path == "/api/login" and status == 200:
            response.set_cookie("feather_session", "test-session")
        request.close()
        return response(environ, start_response)

    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(
            base_url=f"http://127.0.0.1:{server.server_port}",
            calls=calls,
            replies=replies,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def http_feather_client(feather_http):
    with ingest.requests.Session() as session:
        session.trust_env = False
        session.headers["X-Test-Client"] = "bot-regression"
        yield ingest.FeatherClient(session, feather_http.base_url, "test-password")


@pytest.fixture
def bounded_upload_reads(monkeypatch):
    """Fail on whole-file or oversized reads, including during preparation."""
    readers = []

    class Reader:
        def __init__(self, file):
            self.file = file
            self.read_sizes = []

        def __getattr__(self, name):
            return getattr(self.file, name)

        def read(self, size=-1):
            assert 0 <= size <= 1024 * 1024, "upload must use bounded file reads"
            self.read_sizes.append(size)
            return self.file.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

    def guarded_open(path, mode="r", *args, **kwargs):
        file = builtins.open(path, mode, *args, **kwargs)
        if mode != "rb":
            return file
        reader = Reader(file)
        readers.append(reader)
        return reader

    monkeypatch.setattr(ingest, "open", guarded_open, raising=False)
    return readers


class FakeFeatherClient:
    def __init__(
        self,
        add_version_result=(True, "ok"),
        add_app_result=(True, "ok"),
        set_icon_error=None,
    ):
        self.login_calls = 0
        self.add_version_calls = []
        self.add_version_result = add_version_result
        # Plan 023: app creation and icon-setting.
        self.add_app_calls = []
        self.add_app_result = add_app_result
        self.set_icon_calls = []
        self.set_icon_error = set_icon_error

    def login(self):
        self.login_calls += 1

    def add_version(self, bundle_id, version, path):
        self.add_version_calls.append((bundle_id, version, path))
        return self.add_version_result

    def add_app(self, bundle_id, version, name, developer, path):
        self.add_app_calls.append((bundle_id, version, name, developer, path))
        return self.add_app_result

    def set_icon(self, bundle_id, path):
        self.set_icon_calls.append((bundle_id, path))
        if self.set_icon_error is not None:
            raise self.set_icon_error


# ---------------------------------------------------------------------------
# Fixture helpers -- build IPA-shaped and non-IPA-shaped files on disk.
# ---------------------------------------------------------------------------


def write_minimal_ipa(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/App.app/Info.plist", "fake plist bytes")
    return path


def write_zip_without_payload(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", "not an ipa")
    return path


def write_ipa_with_plists(path, entries):
    """Build a zip at `path` with each `{zip_name: plist_dict_or_bytes}` entry.

    A dict value is serialised as a binary plist (what real IPAs carry);
    bytes are written as-is, for fixtures that need unparseable garbage.
    """
    with zipfile.ZipFile(path, "w") as zf:
        for name, value in entries.items():
            if isinstance(value, (bytes, bytearray)):
                zf.writestr(name, bytes(value))
            else:
                zf.writestr(name, plistlib.dumps(value, fmt=plistlib.FMT_BINARY))
    return path


def write_gzip_html(path):
    # Regression fixture for the three corrupt catalog entries: gzip of a
    # short HTML string, saved with an .ipa extension like the real bytes
    # that slipped through pre-Plan-007.
    with gzip.open(path, "wb") as f:
        f.write(b"<html><body>not an ipa</body></html>")
    return path


def document_update(user_id, file_id="file123", update_id=1, thumbnail=None):
    document = {
        "file_id": file_id,
        "file_name": "Beegram.ipa",
        "file_size": 0,
    }
    if thumbnail is not None:
        document["thumbnail"] = thumbnail
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": CHAT_ID},
            "document": document,
        },
    }


def text_update(user_id, text, update_id=2):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": CHAT_ID},
            "text": text,
        },
    }


# ---------------------------------------------------------------------------
# 1. test_allowlist_rejects_unknown_sender
# ---------------------------------------------------------------------------


def test_allowlist_rejects_unknown_sender(caplog):
    config = ingest.load_config(make_env())
    bot = FakeBotAPI()
    feather = FakeFeatherClient()
    pending = {}

    update = document_update(OTHER_USER_ID)

    with caplog.at_level("WARNING"):
        ingest.handle_update(update, config, bot, feather, pending)

    assert bot.sent_messages == []
    assert feather.login_calls == 0
    assert feather.add_version_calls == []
    assert pending == {}
    assert str(OTHER_USER_ID) in caplog.text


# ---------------------------------------------------------------------------
# 2. test_allowlist_empty_is_fatal
# ---------------------------------------------------------------------------


def test_allowlist_empty_is_fatal():
    with pytest.raises(ingest.ConfigError):
        ingest.load_config(make_env(TELEGRAM_ALLOWED_USER_IDS=""))

    env = make_env()
    del env["TELEGRAM_ALLOWED_USER_IDS"]
    with pytest.raises(ingest.ConfigError):
        ingest.load_config(env)


# ---------------------------------------------------------------------------
# 3. test_missing_config_names_missing_vars
# ---------------------------------------------------------------------------


def test_missing_config_names_missing_vars():
    env = make_env()
    del env["TELEGRAM_BOT_TOKEN"]
    del env["FEATHER_ADMIN_PASSWORD"]

    with pytest.raises(ingest.ConfigError) as exc_info:
        ingest.load_config(env)

    message = str(exc_info.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "FEATHER_ADMIN_PASSWORD" in message
    # No values anywhere in the message -- every *present* secret's value
    # must also be absent from the error text.
    for value in env.values():
        assert value not in message


# ---------------------------------------------------------------------------
# 4. test_validate_rejects_non_zip
# ---------------------------------------------------------------------------


def test_validate_rejects_non_zip(tmp_path):
    path = write_gzip_html(tmp_path / "Beegram.ipa")
    size = path.stat().st_size

    with pytest.raises(ingest.ValidationError):
        ingest.validate_ipa_file(str(path), "Beegram.ipa", size)


# ---------------------------------------------------------------------------
# 5. test_validate_rejects_zip_without_payload
# ---------------------------------------------------------------------------


def test_validate_rejects_zip_without_payload(tmp_path):
    path = write_zip_without_payload(tmp_path / "Beegram.ipa")
    size = path.stat().st_size

    with pytest.raises(ingest.ValidationError):
        ingest.validate_ipa_file(str(path), "Beegram.ipa", size)


# ---------------------------------------------------------------------------
# 6. test_validate_accepts_minimal_ipa
# ---------------------------------------------------------------------------


def test_validate_accepts_minimal_ipa(tmp_path):
    path = write_minimal_ipa(tmp_path / "Beegram.ipa")
    size = path.stat().st_size

    # Must not raise.
    ingest.validate_ipa_file(str(path), "Beegram.ipa", size)


# ---------------------------------------------------------------------------
# 7. test_validate_rejects_size_mismatch
# ---------------------------------------------------------------------------


def test_validate_rejects_size_mismatch(tmp_path):
    path = write_minimal_ipa(tmp_path / "Beegram.ipa")
    actual_size = path.stat().st_size

    with pytest.raises(ingest.ValidationError):
        ingest.validate_ipa_file(str(path), "Beegram.ipa", actual_size + 1)


# ---------------------------------------------------------------------------
# 8. test_add_command_parses_bundle_and_version
# ---------------------------------------------------------------------------


def test_add_command_parses_bundle_and_version(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    # -- too few args: usage reply, no ingest, pending document untouched --
    bot = FakeBotAPI()
    feather = FakeFeatherClient()
    pending = {
        ALLOWED_USER_ID: {
            "path": str(ipa_path),
            "filename": "Beegram.ipa",
            "size": ipa_path.stat().st_size,
            "sha256": "deadbeef",
        }
    }

    ingest.handle_update(text_update(ALLOWED_USER_ID, "/add"), config, bot, feather, pending)

    assert feather.add_version_calls == []
    assert feather.login_calls == 0
    assert len(bot.sent_messages) == 1
    assert "Usage" in bot.sent_messages[0][1]
    assert ALLOWED_USER_ID in pending  # nothing was consumed

    # -- well-formed /add: parses bundle id and version, ingests the
    #    pending document --
    ingest.handle_update(
        text_update(ALLOWED_USER_ID, "/add com.x.y 1.2.3", update_id=3),
        config,
        bot,
        feather,
        pending,
    )

    assert feather.add_version_calls == [("com.x.y", "1.2.3", str(ipa_path))]
    assert ALLOWED_USER_ID not in pending  # cleared after a successful publish
    assert any("Published" in text for _, text in bot.sent_messages)


# ---------------------------------------------------------------------------
# 9. test_getfile_timeout_defaults_to_900 (plan 020)
# ---------------------------------------------------------------------------


def test_getfile_timeout_defaults_to_900():
    config = ingest.load_config(make_env())
    assert config["bot_api_getfile_timeout"] == 900

    session = _FakeSession()
    bot = ingest.BotAPIClient(
        session, "http://x", "t", getfile_timeout=config["bot_api_getfile_timeout"]
    )
    bot.get_file("file123")

    _, _, kwargs = session.calls[0]
    assert kwargs["timeout"] == 900


# ---------------------------------------------------------------------------
# 10. test_getfile_timeout_is_configurable (plan 020)
# ---------------------------------------------------------------------------


def test_getfile_timeout_is_configurable():
    config = ingest.load_config(make_env(BOT_API_GETFILE_TIMEOUT="120"))
    assert config["bot_api_getfile_timeout"] == 120

    session = _FakeSession()
    bot = ingest.BotAPIClient(
        session, "http://x", "t", getfile_timeout=config["bot_api_getfile_timeout"]
    )
    bot.get_file("file123")

    _, _, kwargs = session.calls[0]
    assert kwargs["timeout"] == 120


# ---------------------------------------------------------------------------
# 11. test_ack_sent_before_getfile (plan 020) -- regression test for the
#     silence: a "Fetching..." message must reach the user before the
#     blocking getFile call, not after.
# ---------------------------------------------------------------------------


def test_ack_sent_before_getfile(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    bot = FakeBotAPI(get_file_result={"file_path": str(ipa_path)})
    feather = FakeFeatherClient()
    pending = {}

    update = document_update(ALLOWED_USER_ID)
    update["message"]["document"]["file_size"] = ipa_path.stat().st_size

    ingest.handle_update(update, config, bot, feather, pending)

    assert "send_message" in bot.calls
    assert "get_file" in bot.calls
    assert bot.calls.index("send_message") < bot.calls.index("get_file")

    ack_text = bot.sent_messages[0][1]
    assert "Fetching" in ack_text
    assert "Beegram.ipa" in ack_text


# ---------------------------------------------------------------------------
# 12. test_getfile_failure_replies_to_user (plan 020) -- also doubles as the
#     redaction test: the injected exception's message embeds a fake token,
#     and the reply the user receives must not.
# ---------------------------------------------------------------------------


def test_getfile_failure_replies_to_user():
    token = "123456:FAKE_TOKEN_FOR_TESTS"
    config = ingest.load_config(make_env(TELEGRAM_BOT_TOKEN=token))

    bot = FakeBotAPI(
        get_file_error=RuntimeError(
            "400 Client Error: Bad Request for url: "
            f"http://telegram-bot-api:8081/bot{token}/getFile?file_id=file123"
        )
    )
    feather = FakeFeatherClient()
    pending = {}

    # Must not raise -- process_update is the loop's per-update wrapper.
    ingest.process_update(document_update(ALLOWED_USER_ID), config, bot, feather, pending)

    assert len(bot.sent_messages) == 2  # the "Fetching..." ack, then the error
    ack_text = bot.sent_messages[0][1]
    assert "Fetching" in ack_text

    error_text = bot.sent_messages[1][1]
    assert "RuntimeError" in error_text
    assert token not in error_text
    assert "<REDACTED>" in error_text


# ---------------------------------------------------------------------------
# 13. test_reply_failure_does_not_kill_loop (plan 020)
# ---------------------------------------------------------------------------


def test_reply_failure_does_not_kill_loop():
    config = ingest.load_config(make_env())

    bot = FakeBotAPI(
        get_file_error=RuntimeError("boom"),
        send_message_error=RuntimeError("telegram unreachable"),
    )
    feather = FakeFeatherClient()
    pending = {}

    # Must not raise, even though both the fetch and every reply attempt
    # (the ack, and the error reply) fail.
    ingest.process_update(document_update(ALLOWED_USER_ID), config, bot, feather, pending)

    assert bot.sent_messages == []


# ---------------------------------------------------------------------------
# 14. test_extract_reads_bundle_id_and_version (plan 021)
# ---------------------------------------------------------------------------


def test_extract_reads_bundle_id_and_version(tmp_path):
    path = write_ipa_with_plists(
        tmp_path / "App.ipa",
        {
            "Payload/App.app/Info.plist": {
                "CFBundleIdentifier": "com.example.app",
                "CFBundleShortVersionString": "1.2.3",
            }
        },
    )

    assert ingest.extract_ipa_metadata(str(path)) == ("com.example.app", "1.2.3", None)


# ---------------------------------------------------------------------------
# 15. test_extract_ignores_nested_plists (plan 021) -- the important one:
#     proves the regex discriminates the app's own Info.plist from a
#     bundled extension's. See the demonstration in the plan/task report --
#     loosening `_APP_INFO_PLIST` to `Info.plist$` makes this fail.
# ---------------------------------------------------------------------------


def test_extract_ignores_nested_plists(tmp_path):
    path = write_ipa_with_plists(
        tmp_path / "App.ipa",
        {
            "Payload/App.app/Info.plist": {
                "CFBundleIdentifier": "com.example.app",
                "CFBundleShortVersionString": "1.2.3",
            },
            "Payload/App.app/PlugIns/Ext.appex/Info.plist": {
                "CFBundleIdentifier": "com.example.app.ext",
                "CFBundleShortVersionString": "1.2.3",
            },
        },
    )

    bundle_id, _, _ = ingest.extract_ipa_metadata(str(path))
    assert bundle_id == "com.example.app"


# ---------------------------------------------------------------------------
# 16. test_extract_prefers_short_version_string (plan 021)
# ---------------------------------------------------------------------------


def test_extract_prefers_short_version_string(tmp_path):
    path = write_ipa_with_plists(
        tmp_path / "App.ipa",
        {
            "Payload/App.app/Info.plist": {
                "CFBundleIdentifier": "com.example.app",
                "CFBundleShortVersionString": "1.2.3",
                "CFBundleVersion": "434010",
            }
        },
    )

    assert ingest.extract_ipa_metadata(str(path)) == ("com.example.app", "1.2.3", None)


# ---------------------------------------------------------------------------
# 17. test_extract_falls_back_to_bundle_version (plan 021)
# ---------------------------------------------------------------------------


def test_extract_falls_back_to_bundle_version(tmp_path):
    path = write_ipa_with_plists(
        tmp_path / "App.ipa",
        {
            "Payload/App.app/Info.plist": {
                "CFBundleIdentifier": "com.example.app",
                "CFBundleVersion": "434010",
            }
        },
    )

    assert ingest.extract_ipa_metadata(str(path)) == ("com.example.app", "434010", None)


# ---------------------------------------------------------------------------
# 18. test_extract_returns_none_on_unreadable_plist (plan 021)
# ---------------------------------------------------------------------------


def test_extract_returns_none_on_unreadable_plist(tmp_path):
    path = write_ipa_with_plists(
        tmp_path / "App.ipa",
        {"Payload/App.app/Info.plist": b"not a plist at all"},
    )

    # Must not raise.
    assert ingest.extract_ipa_metadata(str(path)) == (None, None, None)


# ---------------------------------------------------------------------------
# 19. test_add_with_no_args_uses_detected_values (plan 021)
# ---------------------------------------------------------------------------


def test_add_with_no_args_uses_detected_values(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    bot = FakeBotAPI()
    feather = FakeFeatherClient()
    pending = {
        ALLOWED_USER_ID: {
            "path": str(ipa_path),
            "filename": "Beegram.ipa",
            "size": ipa_path.stat().st_size,
            "sha256": "deadbeef",
            "bundle_id": "app.alextran.immich",
            "version": "3.1.0",
        }
    }

    ingest.handle_update(text_update(ALLOWED_USER_ID, "/add"), config, bot, feather, pending)

    assert feather.add_version_calls == [("app.alextran.immich", "3.1.0", str(ipa_path))]
    assert ALLOWED_USER_ID not in pending
    assert any("Published" in text for _, text in bot.sent_messages)


# ---------------------------------------------------------------------------
# 20. test_add_with_args_overrides_detection (plan 021)
# ---------------------------------------------------------------------------


def test_add_with_args_overrides_detection(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    bot = FakeBotAPI()
    feather = FakeFeatherClient()
    pending = {
        ALLOWED_USER_ID: {
            "path": str(ipa_path),
            "filename": "Beegram.ipa",
            "size": ipa_path.stat().st_size,
            "sha256": "deadbeef",
            "bundle_id": "app.alextran.immich",
            "version": "3.1.0",
        }
    }

    ingest.handle_update(
        text_update(ALLOWED_USER_ID, "/add other.id 9.9.9"), config, bot, feather, pending
    )

    assert feather.add_version_calls == [("other.id", "9.9.9", str(ipa_path))]
    assert ALLOWED_USER_ID not in pending
    assert any("Published" in text for _, text in bot.sent_messages)


# ---------------------------------------------------------------------------
# 21. test_add_with_no_args_and_no_detection_refuses (plan 021)
# ---------------------------------------------------------------------------


def test_add_with_no_args_and_no_detection_refuses(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    bot = FakeBotAPI()
    feather = FakeFeatherClient()
    pending = {
        ALLOWED_USER_ID: {
            "path": str(ipa_path),
            "filename": "Beegram.ipa",
            "size": ipa_path.stat().st_size,
            "sha256": "deadbeef",
            "bundle_id": None,
            "version": None,
        }
    }

    ingest.handle_update(text_update(ALLOWED_USER_ID, "/add"), config, bot, feather, pending)

    assert feather.add_version_calls == []
    assert feather.login_calls == 0
    assert ALLOWED_USER_ID in pending  # nothing consumed
    assert len(bot.sent_messages) == 1
    assert "Usage" in bot.sent_messages[0][1]


# ---------------------------------------------------------------------------
# Plan 023: create the app when it isn't in the catalog yet, and set its
# icon from Telegram's own thumbnail. See plans/023.
# ---------------------------------------------------------------------------


def _pending_doc(ipa_path, **overrides):
    doc = {
        "path": str(ipa_path),
        "filename": "Beegram.ipa",
        "size": ipa_path.stat().st_size,
        "sha256": "deadbeef",
        "bundle_id": "app.alextran.immich",
        "version": "3.1.0",
        "name": "Immich",
        "thumb_path": None,
    }
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# 22. test_extract_returns_display_name
# ---------------------------------------------------------------------------


def test_extract_returns_display_name(tmp_path):
    path = write_ipa_with_plists(
        tmp_path / "App.ipa",
        {
            "Payload/App.app/Info.plist": {
                "CFBundleIdentifier": "com.example.app",
                "CFBundleShortVersionString": "1.2.3",
                "CFBundleDisplayName": "Example App",
            }
        },
    )

    assert ingest.extract_ipa_metadata(str(path)) == (
        "com.example.app",
        "1.2.3",
        "Example App",
    )


# ---------------------------------------------------------------------------
# 23. test_extract_prefers_display_name_over_bundle_name
# ---------------------------------------------------------------------------


def test_extract_prefers_display_name_over_bundle_name(tmp_path):
    path = write_ipa_with_plists(
        tmp_path / "App.ipa",
        {
            "Payload/App.app/Info.plist": {
                "CFBundleIdentifier": "com.example.app",
                "CFBundleShortVersionString": "1.2.3",
                "CFBundleDisplayName": "Example App",
                "CFBundleName": "exampleapp",
            }
        },
    )

    _, _, name = ingest.extract_ipa_metadata(str(path))
    assert name == "Example App"


# ---------------------------------------------------------------------------
# 24. test_extract_name_none_when_absent
# ---------------------------------------------------------------------------


def test_extract_name_none_when_absent(tmp_path):
    path = write_ipa_with_plists(
        tmp_path / "App.ipa",
        {
            "Payload/App.app/Info.plist": {
                "CFBundleIdentifier": "com.example.app",
                "CFBundleShortVersionString": "1.2.3",
            }
        },
    )

    assert ingest.extract_ipa_metadata(str(path)) == ("com.example.app", "1.2.3", None)


# ---------------------------------------------------------------------------
# 25. test_add_creates_app_when_not_found
# ---------------------------------------------------------------------------


def test_add_creates_app_when_not_found(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    bot = FakeBotAPI()
    feather = FakeFeatherClient(add_version_result=(False, "App not found"))
    pending = {ALLOWED_USER_ID: _pending_doc(ipa_path)}

    ingest.handle_update(text_update(ALLOWED_USER_ID, "/add"), config, bot, feather, pending)

    assert feather.add_app_calls == [
        ("app.alextran.immich", "3.1.0", "Immich", "Unknown", str(ipa_path))
    ]
    assert ALLOWED_USER_ID not in pending
    assert any(
        "not in the catalog" in text and "creating" in text
        for _, text in bot.sent_messages
    )


# ---------------------------------------------------------------------------
# 26. test_add_does_not_create_app_on_other_errors -- proves the branch
#     discriminates on the exact message. See the executor's report for the
#     demonstration that loosening the match breaks this test.
# ---------------------------------------------------------------------------


def test_add_does_not_create_app_on_other_errors(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    bot = FakeBotAPI()
    feather = FakeFeatherClient(add_version_result=(False, "Failed to save source data"))
    pending = {ALLOWED_USER_ID: _pending_doc(ipa_path)}

    ingest.handle_update(text_update(ALLOWED_USER_ID, "/add"), config, bot, feather, pending)

    assert feather.add_app_calls == []
    assert ALLOWED_USER_ID in pending  # publish failed, nothing consumed
    assert bot.sent_messages == [(CHAT_ID, "Publish failed: Failed to save source data")]


# ---------------------------------------------------------------------------
# 27. test_created_app_uses_bundle_id_when_name_missing
# ---------------------------------------------------------------------------


def test_created_app_uses_bundle_id_when_name_missing(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    bot = FakeBotAPI()
    feather = FakeFeatherClient(add_version_result=(False, "App not found"))
    pending = {ALLOWED_USER_ID: _pending_doc(ipa_path, name=None)}

    ingest.handle_update(text_update(ALLOWED_USER_ID, "/add"), config, bot, feather, pending)

    assert feather.add_app_calls == [
        ("app.alextran.immich", "3.1.0", "app.alextran.immich", "Unknown", str(ipa_path))
    ]


# ---------------------------------------------------------------------------
# 28. test_default_developer_is_configurable
# ---------------------------------------------------------------------------


def test_default_developer_is_configurable(tmp_path):
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    # -- configured --
    config = ingest.load_config(
        make_env(BOT_API_FILE_ROOT=str(tmp_path), TELEGRAM_DEFAULT_DEVELOPER="Acme Inc")
    )
    bot = FakeBotAPI()
    feather = FakeFeatherClient(add_version_result=(False, "App not found"))
    pending = {ALLOWED_USER_ID: _pending_doc(ipa_path)}
    ingest.handle_update(text_update(ALLOWED_USER_ID, "/add"), config, bot, feather, pending)
    assert feather.add_app_calls[0][3] == "Acme Inc"

    # -- unset --
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    bot = FakeBotAPI()
    feather = FakeFeatherClient(add_version_result=(False, "App not found"))
    pending = {ALLOWED_USER_ID: _pending_doc(ipa_path)}
    ingest.handle_update(text_update(ALLOWED_USER_ID, "/add"), config, bot, feather, pending)
    assert feather.add_app_calls[0][3] == "Unknown"


# ---------------------------------------------------------------------------
# 29. test_icon_set_after_app_creation -- exercises the full pipeline: a
#     forwarded document carrying a Telegram thumbnail, handle_document
#     resolving it into thumb_path, then creation wiring it through to
#     set_icon.
# ---------------------------------------------------------------------------


def test_icon_set_after_app_creation(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_ipa_with_plists(
        tmp_path / "Beegram.ipa",
        {
            "Payload/App.app/Info.plist": {
                "CFBundleIdentifier": "app.alextran.immich",
                "CFBundleShortVersionString": "3.1.0",
                "CFBundleDisplayName": "Immich",
            }
        },
    )
    thumb_path = tmp_path / "thumb.jpg"
    thumb_path.write_bytes(b"fake jpeg bytes")

    bot = FakeBotAPI(
        get_file_results={
            "file123": {"file_path": str(ipa_path)},
            "thumb123": {"file_path": str(thumb_path)},
        }
    )
    feather = FakeFeatherClient(add_version_result=(False, "App not found"))
    pending = {}

    update = document_update(
        ALLOWED_USER_ID, file_id="file123", thumbnail={"file_id": "thumb123"}
    )
    update["message"]["document"]["file_size"] = ipa_path.stat().st_size

    ingest.handle_update(update, config, bot, feather, pending)
    assert pending[ALLOWED_USER_ID]["thumb_path"] == str(thumb_path)

    ingest.handle_update(
        text_update(ALLOWED_USER_ID, "/add", update_id=3), config, bot, feather, pending
    )

    assert feather.add_app_calls == [
        ("app.alextran.immich", "3.1.0", "Immich", "Unknown", str(ipa_path))
    ]
    assert feather.set_icon_calls == [("app.alextran.immich", str(thumb_path))]
    assert any("Icon set" in text for _, text in bot.sent_messages)


# ---------------------------------------------------------------------------
# 30. test_icon_not_set_when_only_adding_a_version -- guards an operator's
#     hand-chosen icon from being reverted by a later forward.
# ---------------------------------------------------------------------------


def test_icon_not_set_when_only_adding_a_version(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    bot = FakeBotAPI()
    feather = FakeFeatherClient(add_version_result=(True, "ok"))
    pending = {
        ALLOWED_USER_ID: _pending_doc(ipa_path, thumb_path=str(tmp_path / "thumb.jpg"))
    }

    ingest.handle_update(text_update(ALLOWED_USER_ID, "/add"), config, bot, feather, pending)

    assert feather.add_app_calls == []
    assert feather.set_icon_calls == []


# ---------------------------------------------------------------------------
# 31. test_icon_failure_does_not_fail_publish -- the binary is the point;
#     the icon is decoration.
# ---------------------------------------------------------------------------


def test_icon_failure_does_not_fail_publish(tmp_path, caplog):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")
    thumb_path = tmp_path / "thumb.jpg"
    thumb_path.write_bytes(b"fake jpeg bytes")

    bot = FakeBotAPI()
    feather = FakeFeatherClient(
        add_version_result=(False, "App not found"),
        set_icon_error=RuntimeError("update-app unreachable"),
    )
    pending = {ALLOWED_USER_ID: _pending_doc(ipa_path, thumb_path=str(thumb_path))}

    with caplog.at_level("ERROR"):
        ingest.handle_update(
            text_update(ALLOWED_USER_ID, "/add"), config, bot, feather, pending
        )

    assert feather.set_icon_calls == [("app.alextran.immich", str(thumb_path))]
    assert ALLOWED_USER_ID not in pending  # publish still succeeded
    reply = bot.sent_messages[-1][1]
    assert "Published" in reply
    assert "could not set the icon" in reply.lower()


# ---------------------------------------------------------------------------
# 32. test_no_thumbnail_publishes_without_icon
# ---------------------------------------------------------------------------


def test_no_thumbnail_publishes_without_icon(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_ipa_with_plists(
        tmp_path / "Beegram.ipa",
        {
            "Payload/App.app/Info.plist": {
                "CFBundleIdentifier": "com.example.app",
                "CFBundleShortVersionString": "1.0.0",
            }
        },
    )

    bot = FakeBotAPI(get_file_result={"file_path": str(ipa_path)})
    feather = FakeFeatherClient(add_version_result=(False, "App not found"))
    pending = {}

    update = document_update(ALLOWED_USER_ID)
    update["message"]["document"]["file_size"] = ipa_path.stat().st_size

    ingest.handle_update(update, config, bot, feather, pending)
    assert pending[ALLOWED_USER_ID]["thumb_path"] is None

    ingest.handle_update(
        text_update(ALLOWED_USER_ID, "/add", update_id=3), config, bot, feather, pending
    )

    assert feather.add_app_calls  # app created
    assert feather.set_icon_calls == []
    assert any("Published" in text for _, text in bot.sent_messages)
    assert not any("could not" in text.lower() for _, text in bot.sent_messages)


# ---------------------------------------------------------------------------
# 33. test_add_command_name_argument_sets_created_app_name (plan 036)
# ---------------------------------------------------------------------------


def test_add_command_name_argument_sets_created_app_name(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    bot = FakeBotAPI()
    feather = FakeFeatherClient(add_version_result=(False, "App not found"))
    pending = {ALLOWED_USER_ID: _pending_doc(ipa_path, name="Twitter")}

    ingest.handle_update(
        text_update(ALLOWED_USER_ID, "/add com.custom.patched 12.17 My Patched Twitter"),
        config,
        bot,
        feather,
        pending,
    )

    assert feather.add_app_calls == [
        ("com.custom.patched", "12.17", "My Patched Twitter", "Unknown", str(ipa_path))
    ]


# ---------------------------------------------------------------------------
# 34. test_add_command_name_defaults_to_detected_when_omitted (plan 036)
# ---------------------------------------------------------------------------


def test_add_command_name_defaults_to_detected_when_omitted(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    bot = FakeBotAPI()
    feather = FakeFeatherClient(add_version_result=(False, "App not found"))
    pending = {ALLOWED_USER_ID: _pending_doc(ipa_path, name="Twitter")}

    ingest.handle_update(
        text_update(ALLOWED_USER_ID, "/add com.custom.patched 12.17"),
        config,
        bot,
        feather,
        pending,
    )

    assert feather.add_app_calls == [
        ("com.custom.patched", "12.17", "Twitter", "Unknown", str(ipa_path))
    ]


# ---------------------------------------------------------------------------
# 35. test_add_command_name_ignored_when_app_exists (plan 036)
# ---------------------------------------------------------------------------


def test_add_command_name_ignored_when_app_exists(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    bot = FakeBotAPI()
    feather = FakeFeatherClient(add_version_result=(True, "ok"))
    pending = {ALLOWED_USER_ID: _pending_doc(ipa_path)}

    ingest.handle_update(
        text_update(ALLOWED_USER_ID, "/add com.x 1.0 Some Name"),
        config,
        bot,
        feather,
        pending,
    )

    assert feather.add_app_calls == []
    assert any("Published" in text for _, text in bot.sent_messages)


# ---------------------------------------------------------------------------
# 36. test_add_command_two_tokens_is_usage_error (plan 036)
# ---------------------------------------------------------------------------


def test_add_command_two_tokens_is_usage_error(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_minimal_ipa(tmp_path / "Beegram.ipa")

    bot = FakeBotAPI()
    feather = FakeFeatherClient()
    pending = {ALLOWED_USER_ID: _pending_doc(ipa_path)}

    ingest.handle_update(
        text_update(ALLOWED_USER_ID, "/add com.x"), config, bot, feather, pending
    )

    assert feather.add_version_calls == []
    assert feather.add_app_calls == []
    assert len(bot.sent_messages) == 1
    assert "Usage" in bot.sent_messages[0][1]
    assert ALLOWED_USER_ID in pending  # nothing was consumed


# ---------------------------------------------------------------------------
# 37. test_mount_mismatch_reply_keeps_root_and_path_readable (plan 022) --
#     BOT_API_FILE_ROOT is a mountpoint, not a secret. Redacting it mangled
#     the one error message operators most need to read, because the
#     reported path is *prefixed* by the root. A token that happens to sit
#     mid-path must still be scrubbed; the root and path around it must not.
# ---------------------------------------------------------------------------


def test_mount_mismatch_reply_keeps_root_and_path_readable():
    token = "123456:FAKE_TOKEN_FOR_TESTS"
    file_root = "/var/lib/telegram-bot-api"
    # Mirrors production: getFile reported a path under the file root that
    # does not exist inside this worker's mount -- a MountMismatchError.
    reported_path = f"{file_root}/{token}/documents/file_0.ipa"

    config = ingest.load_config(
        make_env(TELEGRAM_BOT_TOKEN=token, BOT_API_FILE_ROOT=file_root)
    )

    bot = FakeBotAPI(get_file_result={"file_path": reported_path})
    feather = FakeFeatherClient()
    pending = {}

    ingest.process_update(document_update(ALLOWED_USER_ID), config, bot, feather, pending)

    assert len(bot.sent_messages) == 2  # the "Fetching..." ack, then the error
    error_text = bot.sent_messages[1][1]

    assert "MountMismatchError" in error_text
    assert file_root in error_text
    assert "documents/file_0.ipa" in error_text
    assert token not in error_text
    assert "<REDACTED>" in error_text


class FakeAndroidFeatherClient(FakeFeatherClient):
    def __init__(self, result=None):
        super().__init__()
        self.add_apk_calls = []
        self.add_apk_result = result or (
            True,
            "Added com.x8bit.bitwarden version 42",
            {
                "success": True,
                "added": True,
                "package": "com.x8bit.bitwarden",
                "versionCode": 42,
            },
        )

    def add_apk(self, path):
        self.add_apk_calls.append(path)
        return self.add_apk_result


def write_minimal_apk(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"binary manifest placeholder")
    return path


def test_apk_document_is_staged_for_confirmation(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    apk_path = write_minimal_apk(tmp_path / "com.x8bit.bitwarden.apk")
    bot = FakeBotAPI(get_file_result={"file_path": str(apk_path)})
    feather = FakeAndroidFeatherClient()
    pending_path = tmp_path / "pending.sqlite3"
    pending = ingest.PendingStore(str(pending_path))
    update = document_update(ALLOWED_USER_ID)
    update["message"]["document"]["file_name"] = apk_path.name
    update["message"]["document"]["file_size"] = apk_path.stat().st_size

    ingest.handle_update(update, config, bot, feather, pending)

    assert pending[ALLOWED_USER_ID]["artifact_type"] == "apk"
    restored = ingest.PendingStore(str(pending_path))
    assert restored[ALLOWED_USER_ID] == pending[ALLOWED_USER_ID]
    assert feather.login_calls == 0
    assert any("Send /add" in text for _, text in bot.sent_messages)


def test_add_command_publishes_pending_apk(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    apk_path = write_minimal_apk(tmp_path / "com.x8bit.bitwarden.apk")
    bot = FakeBotAPI(get_file_result={"file_path": str(apk_path)})
    feather = FakeAndroidFeatherClient()
    pending = {}
    update = document_update(ALLOWED_USER_ID)
    update["message"]["document"]["file_name"] = apk_path.name
    update["message"]["document"]["file_size"] = apk_path.stat().st_size
    ingest.handle_update(update, config, bot, feather, pending)

    ingest.handle_update(
        text_update(ALLOWED_USER_ID, "/add"), config, bot, feather, pending
    )

    assert feather.login_calls == 1
    assert feather.add_apk_calls == [str(apk_path)]
    assert ALLOWED_USER_ID not in pending
    assert any(
        "com.x8bit.bitwarden" in text and "42" in text
        for _, text in bot.sent_messages
    )


def test_apk_document_without_manifest_is_rejected(tmp_path):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    apk_path = tmp_path / "broken.apk"
    with zipfile.ZipFile(apk_path, "w") as archive:
        archive.writestr("readme.txt", "not an APK")
    bot = FakeBotAPI(get_file_result={"file_path": str(apk_path)})
    pending = {}
    update = document_update(ALLOWED_USER_ID)
    update["message"]["document"]["file_name"] = apk_path.name
    update["message"]["document"]["file_size"] = apk_path.stat().st_size

    ingest.handle_update(
        update, config, bot, FakeAndroidFeatherClient(), pending
    )

    assert pending == {}
    assert any("AndroidManifest.xml" in text for _, text in bot.sent_messages)




@pytest.mark.parametrize("default", [None, False, 0, "fallback"])
def test_pending_store_pop_missing_key_returns_explicit_default(tmp_path, default):
    pending_path = tmp_path / "pending.sqlite3"
    pending = ingest.PendingStore(str(pending_path))
    retained = {"path": "/shared/retained.ipa"}
    pending[OTHER_USER_ID] = retained

    assert pending.pop(ALLOWED_USER_ID, default) is default
    assert ingest.PendingStore(str(pending_path)) == {OTHER_USER_ID: retained}


def test_pending_store_pop_without_default_raises_for_missing_key(tmp_path):
    pending = ingest.PendingStore(str(tmp_path / "pending.sqlite3"))

    with pytest.raises(KeyError) as error:
        pending.pop(ALLOWED_USER_ID)

    assert error.value.args == (ALLOWED_USER_ID,)


def test_pending_store_pop_removes_only_selected_persisted_upload(tmp_path):
    pending_path = tmp_path / "pending.sqlite3"
    pending = ingest.PendingStore(str(pending_path))
    selected = {"path": "/shared/selected.ipa"}
    retained = {"path": "/shared/retained.ipa"}
    pending[ALLOWED_USER_ID] = selected
    pending[OTHER_USER_ID] = retained

    assert pending.pop(ALLOWED_USER_ID, None) == selected
    assert pending == {OTHER_USER_ID: retained}
    assert ingest.PendingStore(str(pending_path)) == {OTHER_USER_ID: retained}


@pytest.mark.parametrize("outcome", ["version", "create", "failure"])
def test_pending_ipa_can_be_published_after_reopening_store(
    tmp_path, feather_http, http_feather_client, bounded_upload_reads, outcome
):
    config = ingest.load_config(make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    ipa_path = write_ipa_with_plists(
        tmp_path / "example.ipa",
        {
            "Payload/App.app/Info.plist": {
                "CFBundleIdentifier": "com.example.app",
                "CFBundleShortVersionString": "1.2",
                "CFBundleVersion": "42",
                "CFBundleDisplayName": "Example",
                "MinimumOSVersion": "15.0",
                "CFBundleSupportedPlatforms": ["iPhoneOS"],
                "UIDeviceFamily": [1, 2],
                "NSCameraUsageDescription": "Scan a code",
            }
        },
    )
    thumb_path = tmp_path / "thumb.jpg"
    thumb_path.write_bytes(b"test thumbnail bytes")
    pending_path = tmp_path / "pending.sqlite3"
    pending = ingest.PendingStore(str(pending_path))
    retained = {"path": "/shared/another-operator.ipa"}
    pending[OTHER_USER_ID] = retained
    bot = FakeBotAPI(
        get_file_results={
            "file123": {"file_path": str(ipa_path)},
            "thumb": {"file_path": str(thumb_path)},
        }
    )
    feather = http_feather_client
    if outcome != "version":
        feather_http.replies["/api/add-version"] = (
            400,
            {
                "success": False,
                "error": "App not found" if outcome == "create" else "Storage unavailable",
            },
        )
    update = document_update(ALLOWED_USER_ID, thumbnail={"file_id": "thumb"})
    update["message"]["document"].update(
        file_name=ipa_path.name, file_size=ipa_path.stat().st_size
    )

    ingest.handle_update(update, config, bot, feather, pending)
    restored = ingest.PendingStore(str(pending_path))
    inspection = restored[ALLOWED_USER_ID]["inspection"]
    assert inspection == pending[ALLOWED_USER_ID]["inspection"]
    assert inspection.platform == "ios"
    assert inspection.privacy["NSCameraUsageDescription"] == "Scan a code"

    ingest.handle_update(
        text_update(ALLOWED_USER_ID, "/add"), config, bot, feather, restored
    )

    assert feather_http.calls[0]["json"] == {"password": "test-password"}
    publish = feather_http.calls[1]
    assert publish["path"] == "/api/add-version"
    assert publish["fields"] == {
        "bundleIdentifier": "com.example.app",
        "version": "1.2",
        "buildVersion": "42",
        "minOSVersion": "15.0",
    }
    assert publish["files"] == {
        "ipaFile": {
            "filename": ipa_path.name,
            "sha256": hashlib.sha256(ipa_path.read_bytes()).hexdigest(),
            "size": ipa_path.stat().st_size,
        }
    }
    assert publish["cookie"] == "test-session"
    assert all(reader.file.closed for reader in bounded_upload_reads)
    stored = ingest.PendingStore(str(pending_path))
    assert stored[OTHER_USER_ID] == retained
    if outcome == "failure":
        assert stored[ALLOWED_USER_ID] == restored[ALLOWED_USER_ID]
        assert len(feather_http.calls) == 2
        assert bot.sent_messages[-1] == (CHAT_ID, "Publish failed: Storage unavailable")
    else:
        assert ALLOWED_USER_ID not in stored
        assert "Published com.example.app 1.2" in bot.sent_messages[-1][1]
        if outcome == "version":
            assert len(feather_http.calls) == 2
        else:
            created, icon = feather_http.calls[2:]
            assert created["path"] == "/api/add-app"
            assert created["fields"] == {
                **publish["fields"],
                "name": "Example",
                "developerName": "Unknown",
                "privacy": json.dumps(inspection.privacy),
            }
            assert created["files"] == publish["files"]
            assert icon["path"] == "/api/update-app"
            assert icon["fields"] == {"bundleIdentifier": "com.example.app"}
            assert icon["files"]["iconFile"] == {
                "filename": thumb_path.name,
                "sha256": hashlib.sha256(thumb_path.read_bytes()).hexdigest(),
                "size": thumb_path.stat().st_size,
            }
            assert created["cookie"] == icon["cookie"] == "test-session"
            assert "Icon set." in bot.sent_messages[-1][1]


UPLOAD_CASES = [
    (
        "add_version", "/api/add-version", "ipaFile", ("com.example.app", "1.2"),
        {
            "bundleIdentifier": "com.example.app", "version": "1.2",
            "buildVersion": "42", "minOSVersion": "15.0",
        },
    ),
    (
        "add_app", "/api/add-app", "ipaFile",
        ("com.example.app", "1.2", "Example App", "Example Developer"),
        {
            "bundleIdentifier": "com.example.app", "version": "1.2",
            "name": "Example App", "developerName": "Example Developer",
            "buildVersion": "42", "minOSVersion": "15.0",
            "privacy": json.dumps({"NSCameraUsageDescription": "Scan a code"}),
        },
    ),
    ("add_apk", "/api/android/add-apk", "apkFile", (), {}),
    (
        "set_icon", "/api/update-app", "iconFile", ("com.example.app",),
        {"bundleIdentifier": "com.example.app"},
    ),
]


@pytest.mark.parametrize("method,endpoint,file_field,args,fields", UPLOAD_CASES)
def test_upload_streams_decodable_multipart_with_bounded_reads(
    tmp_path, feather_http, http_feather_client, bounded_upload_reads,
    method, endpoint, file_field, args, fields,
):
    # Larger than the read guard so one full-file read cannot pass.
    content = bytes(range(256)) * 8192
    path = tmp_path / "forwarded artifact.bin"
    path.write_bytes(content)
    response = {
        "success": True, "message": "published", "added": True,
        "package": "com.example.android", "versionCode": 42,
    }
    feather_http.replies[endpoint] = (200, response)
    feather = http_feather_client
    feather.set_preflight(SimpleNamespace(
        build_version="42", minimum_os_version="15.0",
        privacy={"NSCameraUsageDescription": "Scan a code"},
    ))
    feather.login()

    result = getattr(feather, method)(*args, str(path))

    assert result == (
        (True, "published", response) if method == "add_apk"
        else (True, "published")
    )
    login, upload = feather_http.calls
    assert login == {
        "path": "/api/login", "method": "POST",
        "json": {"password": "test-password"},
    }
    assert upload["path"] == endpoint
    assert upload["method"] == "POST"
    assert upload["fields"] == fields
    assert upload["files"] == {
        file_field: {
            "filename": path.name,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    }
    assert upload["content_type"] == "multipart/form-data"
    assert upload["content_length"] > len(content)
    assert upload["cookie"] == "test-session"
    assert upload["client_header"] == "bot-regression"
    assert len(bounded_upload_reads) == 1
    reader = bounded_upload_reads[0]
    assert len(reader.read_sizes) > 1
    assert reader.file.closed


@pytest.mark.parametrize("method,endpoint,file_field,args,fields", UPLOAD_CASES)
@pytest.mark.parametrize("status", [400, 401])
def test_streamed_upload_preserves_api_errors_and_closes_file(
    tmp_path, feather_http, http_feather_client, bounded_upload_reads,
    method, endpoint, file_field, args, fields, status,
):
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"upload payload")
    response = {"success": False, "error": "Storage unavailable", "message": "ignored"}
    feather_http.replies[endpoint] = (status, response)
    feather = http_feather_client
    feather.login()

    if status == 401:
        with pytest.raises(ingest.FeatherAuthError, match=endpoint):
            getattr(feather, method)(*args, str(path))
    else:
        result = getattr(feather, method)(*args, str(path))
        assert result == (
            (False, "Storage unavailable", response) if method == "add_apk"
            else (False, "Storage unavailable")
        )
    assert feather_http.calls[-1]["files"][file_field]["sha256"] == (
        hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert bounded_upload_reads[0].file.closed
