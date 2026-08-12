"""Tests for the Telegram forward-to-bot ingest worker (Plan 013).

No test in this file touches the network, Telegram, or Docker -- updates
and getFile responses are canned dicts, and the Bot API / feather HTTP
clients are hand-rolled fakes that just record calls, following the
FakeS3Client pattern in tests/test_storage.py. BOT_API_FILE_ROOT points at
pytest's tmp_path instead of a real shared volume.

See plans/013-telegram-bot-ingest.md's test plan for the eight cases below.
"""

import gzip
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
    def __init__(self, get_file_result=None):
        self.get_file_result = get_file_result or {}
        self.sent_messages = []  # (chat_id, text)

    def get_file(self, file_id):
        return self.get_file_result

    def send_message(self, chat_id, text):
        self.sent_messages.append((chat_id, text))


class FakeFeatherClient:
    def __init__(self, add_version_result=(True, "ok")):
        self.login_calls = 0
        self.add_version_calls = []
        self.add_version_result = add_version_result

    def login(self):
        self.login_calls += 1

    def add_version(self, bundle_id, version, path):
        self.add_version_calls.append((bundle_id, version, path))
        return self.add_version_result


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


def write_gzip_html(path):
    # Regression fixture for the three corrupt catalog entries: gzip of a
    # short HTML string, saved with an .ipa extension like the real bytes
    # that slipped through pre-Plan-007.
    with gzip.open(path, "wb") as f:
        f.write(b"<html><body>not an ipa</body></html>")
    return path


def document_update(user_id, file_id="file123", update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id},
            "chat": {"id": CHAT_ID},
            "document": {
                "file_id": file_id,
                "file_name": "Beegram.ipa",
                "file_size": 0,
            },
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
