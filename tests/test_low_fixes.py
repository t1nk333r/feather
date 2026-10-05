"""Regression tests for the audit's low-severity findings."""

import io
import os
import re

import pytest

from tests.test_routes import client, authed_client  # noqa: F401
from tests.test_auto_import import build_ipa_bytes, make_candidate, make_fake_select_candidate, make_fake_stream_download
from tests import test_release_source_ingest as rsi
from tests import test_telegram_bot_ingest as tbi
from scripts import release_source_ingest as ingest
from scripts import telegram_bot_ingest as bot_ingest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_telegram_bot_token_is_redacted_from_diagnostics(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:SECRET-bot-token")
    text = "POST https://api.telegram.org/bot123456:SECRET-bot-token/sendMessage failed"
    assert "SECRET" not in client.app_module._redact_secret(text)


def test_one_off_import_of_existing_version_is_skipped(authed_client, monkeypatch):
    module = authed_client.app_module
    ri = module.release_ingest
    ipa = build_ipa_bytes(bundle_id="com.example.app", version="1.0.0")
    cand = make_candidate(ri, provider="github", project="owner/repo", declared_size=len(ipa))
    monkeypatch.setattr(ri, "select_candidate", make_fake_select_candidate(cand))
    monkeypatch.setattr(ri, "stream_download", make_fake_stream_download(ipa))
    notified = []
    monkeypatch.setattr(module, "notify", lambda *a: notified.append(a))
    body = authed_client.post("/api/import-release", json={
        "provider": "github", "project": "owner/repo", "assetGlob": "*.ipa"}).get_data(as_text=True)
    assert '"skipped": true' in body
    assert notified == []
    records = authed_client.get("/api/import-history").get_json()
    records = records.get("records", records) if isinstance(records, dict) else records
    assert records and records[0]["status"] == "skipped"


def test_admin_ui_does_not_suggest_brace_globs():
    assert "{ipa,apk}" not in open(os.path.join(ROOT, "templates", "index.html")).read()


def test_cron_android_provenance_survives_server_normalisation(tmp_path, monkeypatch, client):
    job = rsi._make_apk_job()
    session = rsi.make_github_session(b"fake-apk-bytes", asset_name="App.apk")
    rsi._patch_pyaxmlparser_apk(monkeypatch, package="org.example.app", version_code="7", version_name="1.7")

    class Feather(rsi.FakeFeatherClient):
        records = []

        def record_import_event(self, record):
            self.records.append(record)
    feather = Feather(add_apk_result=(True, "Added", True))
    ingest.process_job(job, session, rsi.NO_TOKENS, feather, ingest.load_state(str(tmp_path / "s.json")),
                       True, 30, 1_000_000, ingest.Summary())
    stored = client.app_module._normalize_import_record(Feather.records[-1])
    assert stored["bundleIdentifier"] == "org.example.app"
    assert stored["version"] == "1.7" and stored["buildVersion"] == "7"


def test_new_forward_clears_previous_pending_even_if_fetch_fails(tmp_path):
    config = bot_ingest.load_config(tbi.make_env(BOT_API_FILE_ROOT=str(tmp_path)))
    bot = tbi.FakeBotAPI(get_file_error=RuntimeError("getFile timed out"))
    pending = {tbi.ALLOWED_USER_ID: {"path": "/old/file.ipa"}}
    with pytest.raises(RuntimeError):       # process_update turns this into "Something went wrong"
        bot_ingest.handle_update(tbi.document_update(tbi.ALLOWED_USER_ID), config, bot,
                                 tbi.FakeFeatherClient(), pending)
    assert tbi.ALLOWED_USER_ID not in pending


def test_garage_migrations_ship_in_the_app_image():
    dockerignore = open(os.path.join(ROOT, ".dockerignore")).read()
    dockerfile = open(os.path.join(ROOT, "Dockerfile")).read()
    for script in ("migrate_ipas_to_garage.py", "migrate_icons_to_garage.py"):
        assert f"!scripts/{script}" in dockerignore
        assert re.search(rf"COPY scripts/{re.escape(script)} ", dockerfile)
