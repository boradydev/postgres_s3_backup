import hashlib
import json
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest

from backup_service import Backup, BackupError, Settings, scheduled_slot


class MemoryS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads = 0
        self.deleted: list[str] = []
        self.corrupt = False
        self.fail_delete = False

    def upload_file(self, path, bucket, key, **kwargs) -> None:
        assert bucket == "bucket"
        assert "ExtraArgs" in kwargs
        self.uploads += 1
        self.objects[key] = Path(path).read_bytes()

    def get_object(self, *, Bucket, Key):
        assert Bucket == "bucket"
        value = self.objects[Key]
        if self.corrupt and Key.endswith(".dump"):
            value = b"corruption"
        return {"Body": BytesIO(value)}

    def put_object(self, *, Bucket, Key, Body, **kwargs) -> None:
        assert Bucket == "bucket"
        assert kwargs["ContentType"] == "application/json"
        self.objects[Key] = Body

    def head_object(self, *, Bucket, Key):
        assert Bucket == "bucket"
        return {"ContentLength": len(self.objects[Key])}

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, *, Bucket, Prefix):
        assert Bucket == "bucket"
        # Несколько страниц, чтобы ротация не полагалась на единственную страницу.
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        for offset in range(0, len(keys), 3):
            yield {"Contents": [{"Key": key} for key in keys[offset : offset + 3]]}

    def delete_object(self, *, Bucket, Key) -> None:
        assert Bucket == "bucket"
        if self.fail_delete:
            raise OSError("private storage error")
        self.deleted.append(Key)
        self.objects.pop(Key, None)


@pytest.fixture
def backup(tmp_path, monkeypatch):
    worker = Backup(
        Settings("bucket", "app/production/db/", "https://example.invalid", "ru-1", tmp_path, "db", 2), MemoryS3()
    )
    monkeypatch.setattr(worker, "locked", nullcontext)
    monkeypatch.setattr(worker, "dump", Mock(side_effect=lambda path: path.write_bytes(b"archive")))
    return worker


def moment(day: int, hour: int = 14) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


def markers(backup, category="daily"):
    return [key for key in backup.client.objects if f"/{category}/" in key and key.endswith(".json")]


def test_schedule_uses_moscow_17_and_previous_day_before_cutoff() -> None:
    assert scheduled_slot(moment(22, 13)) == moment(21).isoformat()
    assert scheduled_slot(moment(22)) == moment(22).isoformat()


def test_initial_backup_and_restart_do_not_duplicate_slot(backup) -> None:
    backup.run("daily", scheduled=True, now=moment(22, 8))
    backup.run("daily", scheduled=True, now=moment(22, 10))
    assert backup.client.uploads == 1
    assert backup.state()["coveredSlot"] == moment(21).isoformat()
    backup.run("daily", scheduled=True, now=moment(22))
    assert backup.client.uploads == 2


def test_days_off_produce_one_catchup_and_do_not_delete_old_copies(backup) -> None:
    backup.run("daily", scheduled=True, now=moment(16))
    assert not backup.client.deleted
    backup.run("daily", scheduled=True, now=moment(19, 8))
    backup.run("daily", scheduled=True, now=moment(19, 9))
    assert backup.client.uploads == 2
    assert not backup.client.deleted


def test_success_rotates_by_count_in_same_category_only(backup) -> None:
    for day in (1, 2, 5):
        backup.run("daily", scheduled=True, now=moment(day))
    backup.run("predeploy")
    assert len(markers(backup)) == 2
    assert len(markers(backup, "predeploy")) == 1
    assert len(backup.client.deleted) == 2


def test_corrupt_download_preserves_old_backups_and_does_not_confirm_job(backup) -> None:
    backup.run("daily", scheduled=True, now=moment(1))
    backup.run("daily", scheduled=True, now=moment(2))
    covered = backup.state()["coveredSlot"]
    backup.client.corrupt = True
    with pytest.raises(BackupError, match="verification|exceeds"):
        backup.run("daily", scheduled=True, now=moment(3))
    assert len(markers(backup)) == 2
    assert backup.state()["coveredSlot"] == covered
    assert not backup.client.deleted
    assert (backup.directory / "pending.json").exists()


def test_retry_after_upload_failure_reuses_pending_archive_and_key(backup) -> None:
    backup.client.corrupt = True
    with pytest.raises(BackupError):
        backup.run("daily", scheduled=True, now=moment(3))
    keys = set(backup.client.objects)
    backup.client.corrupt = False
    backup.run("daily", scheduled=True, now=moment(3))
    backup.dump.assert_called_once()
    assert keys <= backup.client.objects.keys()
    assert len(markers(backup)) == 1


def test_retention_failure_preserves_success_and_retries_without_new_dump(backup) -> None:
    backup.run("daily", scheduled=True, now=moment(1))
    backup.run("daily", scheduled=True, now=moment(2))
    backup.client.fail_delete = True
    state = backup.run("daily", scheduled=True, now=moment(3))
    assert state["coveredSlot"] == moment(3).isoformat()
    assert state["cleanupRequired"]
    assert state["cleanupError"] == "OSError"
    assert len(markers(backup)) == 3
    backup.client.fail_delete = False
    backup.run("daily", scheduled=True, now=moment(3))
    assert len(markers(backup)) == 2
    assert backup.client.uploads == 3
    assert not backup.state()["cleanupRequired"]


def test_download_does_not_overwrite_existing_output(backup, tmp_path) -> None:
    backup.run("daily", scheduled=True, now=moment(1))
    manifest = backup.read_manifest(markers(backup)[0])
    output = tmp_path / "restore.dump"
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        backup.verify(manifest, output)
    assert output.read_bytes() == b"existing"


def test_successful_download_matches_uploaded_sha256(backup, tmp_path) -> None:
    backup.run("daily", scheduled=True, now=moment(1))
    manifest = backup.read_manifest(markers(backup)[0])
    output = tmp_path / "restore.dump"
    backup.verify(manifest, output)
    assert hashlib.sha256(output.read_bytes()).hexdigest() == manifest["sha256"]


def test_foreign_manifest_cannot_trigger_deletion_or_download(backup) -> None:
    with pytest.raises(BackupError, match="outside"):
        backup.read_manifest("other/app/daily/20261001T140000Z-" + "a" * 32 + ".json")
    backup.run("daily", scheduled=True, now=moment(1))
    key = markers(backup)[0]
    manifest = json.loads(backup.client.objects[key])
    manifest["dumpKey"] = "another/database.dump"
    backup.client.objects[key] = json.dumps(manifest).encode()
    with pytest.raises(BackupError, match="Invalid"):
        backup.prune()
    assert not backup.client.deleted


def test_failed_dump_is_not_uploaded_or_confirmed(backup) -> None:
    backup.dump.side_effect = OSError("private local path")
    with pytest.raises(OSError, match="private local path"):
        backup.run("daily", scheduled=True, now=moment(1))
    assert backup.client.uploads == 0
    assert not backup.client.deleted
    assert "coveredSlot" not in backup.state()



@pytest.fixture
def configured_env(monkeypatch):
    monkeypatch.setattr("os.environ", {
        "POSTGRES_HOST": "postgres", "POSTGRES_DB": "db", "POSTGRES_USER": "backup",
        "POSTGRES_PASSWORD": "test-only", "BACKUP_S3_ENDPOINT": "https://example.invalid",
        "BACKUP_S3_BUCKET": "bucket", "AWS_ACCESS_KEY_ID": "test-only",
        "AWS_SECRET_ACCESS_KEY": "test-only",
    })


def test_required_only_configuration_uses_behavior_defaults(configured_env) -> None:
    del configured_env
    settings = Settings.from_env()
    assert (settings.keep, settings.manual_keep, settings.predeploy_keep) == (30, 30, 30)
    assert (settings.timeout, settings.retry_interval, settings.poll_interval) == (1800, 300, 60)
    assert (settings.schedule, settings.timezone) == ("17:00", "Europe/Moscow")
    assert settings.prefix == "postgres-backups/production/db/"


def test_behavior_can_be_overridden(configured_env, monkeypatch) -> None:
    del configured_env
    values = {
        "BACKUP_KEEP": "4", "BACKUP_MANUAL_KEEP": "2", "BACKUP_PREDEPLOY_KEEP": "3",
        "BACKUP_DUMP_TIMEOUT": "900", "BACKUP_RETRY_INTERVAL": "120", "BACKUP_POLL_INTERVAL": "15",
        "BACKUP_TIME": "09:30", "BACKUP_TIMEZONE": "UTC",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert (settings.keep, settings.manual_keep, settings.predeploy_keep) == (4, 2, 3)
    assert (settings.timeout, settings.retry_interval, settings.poll_interval) == (900, 120, 15)
    assert (settings.schedule, settings.timezone) == ("09:30", "UTC")


@pytest.mark.parametrize("name", [
    "POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD",
    "BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
])
def test_missing_connection_setting_fails_before_backup(configured_env, monkeypatch, name) -> None:
    del configured_env
    monkeypatch.delenv(name)
    with pytest.raises(BackupError, match=f"{name} is required"):
        Settings.from_env()


@pytest.mark.parametrize(("name", "value"), [
    ("BACKUP_KEEP", "0"), ("BACKUP_MANUAL_KEEP", "-1"), ("BACKUP_PREDEPLOY_KEEP", "no"),
    ("BACKUP_DUMP_TIMEOUT", ""), ("BACKUP_RETRY_INTERVAL", "0"), ("BACKUP_POLL_INTERVAL", "1.5"),
    ("POSTGRES_PORT", "65536"),
])
def test_invalid_numeric_setting_fails_without_exposing_value(configured_env, monkeypatch, name, value) -> None:
    del configured_env
    monkeypatch.setenv(name, value)
    with pytest.raises(BackupError, match=name):
        Settings.from_env()


def test_manual_backups_have_independent_retention_and_do_not_cover_schedule(backup) -> None:
    backup.settings = replace(backup.settings, keep=2, manual_keep=1, predeploy_keep=3)
    for _ in range(4):
        backup.run("manual")
        backup.run("predeploy")
    assert "coveredSlot" not in backup.state()
    for day in (1, 2, 3):
        backup.run("daily", scheduled=True, now=moment(day))
    assert len(markers(backup, "manual")) == 1
    assert len(markers(backup, "predeploy")) == 3
    assert len(markers(backup, "daily")) == 2
    assert backup.state()["coveredSlot"] == moment(3).isoformat()
