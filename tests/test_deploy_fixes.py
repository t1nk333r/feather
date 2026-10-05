"""Regression tests for the audit's deployment breakers (#7-#9, #23)."""

import errno
import importlib
import json
import os
import shutil
import subprocess

import pytest

from tests.test_routes import (  # noqa: F401
    TEST_ADMIN_PASSWORD, TEST_SECRET_KEY, seed_source, client, authed_client, _minimal_ipa,
    _local_stub_server, _GzipIpaHandler,
)

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _blank_env_keys():
    keys = []
    for line in open(os.path.join(ROOT, ".env.example")):
        line = line.strip()
        if line and not line.startswith("#") and line.endswith("=") and "=" in line:
            keys.append(line[:-1])
    return keys


def test_app_imports_with_every_blank_key_from_env_example(tmp_path, monkeypatch):
    keys = _blank_env_keys()
    assert {"PORT", "MAX_CONTENT_LENGTH", "TELEGRAM_NOTIFY_EVENTS", "ICON_STORAGE_BACKEND"} <= set(keys)
    for key in keys:
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("SECRET_KEY", TEST_SECRET_KEY)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    import app as module
    importlib.reload(module)
    assert module.PORT == 5000
    assert module.MAX_CONTENT_LENGTH == 2 * 1024 ** 3
    assert module.TELEGRAM_NOTIFY_EVENTS == {"add_app", "add_version", "delete_app", "android_add_apk"}
    assert module.ICON_STORAGE_BACKEND == module.APK_STORAGE_BACKEND == "local"
    assert module.GARAGE_S3_REGION == "garage" and module.GARAGE_KEY_PREFIX == "ipas"
    assert module.TELEGRAM_API_BASE == "https://api.telegram.org"


def test_blank_storage_backends_follow_storage_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", TEST_ADMIN_PASSWORD)
    monkeypatch.setenv("STORAGE_BACKEND", "garage")
    for key, value in (("GARAGE_S3_ENDPOINT", "http://127.0.0.1:9"), ("GARAGE_S3_ACCESS_KEY_ID", "k"),
                       ("GARAGE_S3_SECRET_ACCESS_KEY", "s"), ("GARAGE_BUCKET", "b"),
                       ("GARAGE_PUBLIC_BASE_URL", "http://127.0.0.1:9/b")):
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ICON_STORAGE_BACKEND", "")
    monkeypatch.setenv("APK_STORAGE_BACKEND", "")
    import app as module
    try:
        importlib.reload(module)
        assert module.ICON_STORAGE_BACKEND == module.APK_STORAGE_BACKEND == "garage"
    finally:
        monkeypatch.setenv("STORAGE_BACKEND", "local")
        monkeypatch.delenv("ICON_STORAGE_BACKEND")
        monkeypatch.delenv("APK_STORAGE_BACKEND")
        importlib.reload(module)


def _cross_device_replace(real_replace, upload_dir):
    """os.replace that fails like rename(2) across bind mounts: anything
    leaving data/uploads raises EXDEV."""
    def fake(src, dst, *a, **k):
        if os.path.dirname(os.path.abspath(src)) == os.path.abspath(upload_dir):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(src, dst, *a, **k)
    return fake


@pytest.fixture
def gzip_ipa_server():
    yield from _local_stub_server(_GzipIpaHandler)


def test_ipa_and_icon_downloads_survive_cross_mount_moves(authed_client, tmp_path, monkeypatch, gzip_ipa_server):
    module = authed_client.app_module
    monkeypatch.setattr(module.os, "replace", _cross_device_replace(os.replace, module.UPLOAD_FOLDER))
    resp = authed_client.post("/api/add-version", content_type="multipart/form-data", data={
        "bundleIdentifier": "com.example.app", "version": "1.0", "downloadFromUrl": "true",
        "downloadURL": gzip_ipa_server})
    assert resp.status_code == 200, resp.get_json()
    assert (tmp_path / "ipas" / "com.example.app" / "1.0.ipa").exists()
    assert os.listdir(module.UPLOAD_FOLDER) == []

    src = tmp_path / "uploads" / "icon-src.png"
    from PIL import Image
    Image.new("RGBA", (8, 8)).save(src)
    assert module.icon_storage.put(str(src), "com.example.app", "png") is True
    assert (tmp_path / "icons" / "com.example.app" / "icon.png").exists() and not src.exists()


def test_move_file_reraises_other_errors(client, tmp_path, monkeypatch):
    module = client.app_module
    def boom(*a, **k):
        raise OSError(errno.EACCES, "denied")
    monkeypatch.setattr(module.os, "replace", boom)
    with pytest.raises(OSError):
        module._move_file(str(tmp_path / "a"), str(tmp_path / "b"))


@pytest.mark.skipif(not shutil.which("docker"), reason="docker CLI not installed")
def test_compose_renders_without_android_settings(tmp_path):
    shutil.copy(os.path.join(ROOT, "compose.yml"), tmp_path / "compose.yml")
    env = open(os.path.join(ROOT, ".env.example")).read().replace("\nADMIN_PASSWORD=\n", "\nADMIN_PASSWORD=x\n")
    (tmp_path / ".env").write_text(env)
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("FDROID_", "COMPOSE_"))}
    out = subprocess.run(["docker", "compose", "config", "--services"], cwd=tmp_path,
                         capture_output=True, text=True, env=clean)
    if "unknown command" in out.stderr or "is not a docker command" in out.stderr:
        pytest.skip("docker compose plugin not installed")
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["altstore-manager"]


def test_readme_creates_mounted_dirs_before_chown():
    readme = open(os.path.join(ROOT, "README.md")).read()
    mounts = [line.split(":")[0].strip().lstrip("- ").removeprefix("./")
              for line in open(os.path.join(ROOT, "compose.yml"))
              if line.strip().startswith("- ./data/") and ":/app/data/" in line]
    assert mounts, "expected nested data mounts in compose.yml"
    mkdir = readme.index("mkdir -p data")
    chown = readme.index("chown -R 999:999 data")
    line = readme[mkdir:readme.index("\n", mkdir)]
    assert mkdir < chown and all(m in line.split() for m in mounts), (mounts, line)
