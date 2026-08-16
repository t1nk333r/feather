"""No-network tests for the dry-run-first icon migration."""
import importlib
import json
import os


def load_migration(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password-not-a-real-secret")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app as app_module
    app_module = importlib.reload(app_module)
    import scripts.migrate_icons_to_garage as migration
    migration = importlib.reload(migration)
    migration.app_module = app_module
    return app_module, migration


class FakeMigrationClient:
    def __init__(self):
        self.objects = {}
        self.uploads = []

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise RuntimeError("missing")
        return {"ContentLength": self.objects[(Bucket, Key)]}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.uploads.append((path, bucket, key, ExtraArgs))
        self.objects[(bucket, key)] = os.path.getsize(path)


def test_migrate_icons_dry_run_writes_nothing(monkeypatch, tmp_path, capsys):
    app_module, migration = load_migration(monkeypatch, tmp_path)
    icon_dir = tmp_path / "icons" / "com.example.app"
    icon_dir.mkdir(parents=True)
    (icon_dir / "icon.png").write_bytes(b"png")
    (icon_dir / "other.txt").write_text("invalid")
    monkeypatch.setattr(app_module, "ICON_FOLDER", str(tmp_path / "icons"))
    monkeypatch.setattr(app_module, "SOURCE_FILE", str(tmp_path / "missing-source.json"))
    assert migration.migrate(apply=False) == 0
    output = capsys.readouterr().out
    assert "would-upload" in output
    assert "dry-run: nothing written" in output
    assert not hasattr(migration, "_client")


def test_migrate_icons_apply_is_idempotent_and_verifies_size(monkeypatch, tmp_path):
    app_module, migration = load_migration(monkeypatch, tmp_path)
    icon_dir = tmp_path / "icons" / "com.example.app"
    icon_dir.mkdir(parents=True)
    path = icon_dir / "icon.png"
    path.write_bytes(b"png")
    monkeypatch.setattr(app_module, "ICON_FOLDER", str(tmp_path / "icons"))
    monkeypatch.setattr(app_module, "SOURCE_FILE", str(tmp_path / "missing-source.json"))
    fake = FakeMigrationClient()
    monkeypatch.setattr(migration, "garage_client", lambda: fake)
    assert migration.migrate(apply=True) == 0
    assert len(fake.uploads) == 1
    assert migration.migrate(apply=True) == 0
    assert len(fake.uploads) == 1
    assert fake.uploads[0][3] == {"ContentType": "image/png"}
