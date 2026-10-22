import hashlib
import json
from contextlib import nullcontext
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

