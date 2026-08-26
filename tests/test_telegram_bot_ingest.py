"""Tests for the Telegram forward-to-bot ingest worker (Plan 013).

No test in this file touches the network, Telegram, or Docker -- updates
and getFile responses are canned dicts, and the Bot API / feather HTTP
clients are hand-rolled fakes that just record calls, following the
FakeS3Client pattern in tests/test_storage.py. BOT_API_FILE_ROOT points at
pytest's tmp_path instead of a real shared volume.

See plans/013-telegram-bot-ingest.md's test plan for the eight cases below.
"""

import gzip
import plistlib
import zipfile

import pytest

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
