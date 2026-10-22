"""Локальные pg_dump → S3: расписание, подтверждение загрузки и ротация."""

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger("backup")
MOSCOW = ZoneInfo("Europe/Moscow")
CATEGORIES = ("daily", "predeploy")
NAME = re.compile(r"\d{8}T\d{6}Z-[a-f0-9]{32}\.json\Z")
STOP = threading.Event()


class BackupError(Exception):
    """Безопасное техническое сообщение без секретов и тела запросов."""


@dataclass(frozen=True)
class Settings:
    bucket: str
    prefix: str
    endpoint: str
    region: str
    directory: Path
    database: str
    keep: int = 30
    timeout: int = 1800
    schedule: str = "17:00"
    timezone: str = "Europe/Moscow"

    @classmethod
    def from_env(cls) -> "Settings":
        endpoint = os.environ["BACKUP_S3_ENDPOINT"]
        if not endpoint.startswith("https://") and os.environ.get("BACKUP_ALLOW_HTTP") != "1":
            raise BackupError("S3 endpoint must use HTTPS")
        prefix = os.environ.get("BACKUP_S3_PREFIX", "postgres-backups/production").strip("/")
        database = os.environ["POSTGRES_DB"]
        if not re.fullmatch(r"[a-zA-Z0-9_/-]+", prefix) or ".." in prefix:
            raise BackupError("Invalid S3 prefix")
        if not re.fullmatch(r"[a-zA-Z0-9_]+", database):
            raise BackupError("Invalid database name")
        keep = int(os.environ.get("BACKUP_KEEP", "30"))
        if keep < 1:
            raise BackupError("BACKUP_KEEP must be positive")
        schedule = os.environ.get("BACKUP_TIME", "17:00")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule):
            raise BackupError("BACKUP_TIME must be HH:MM")
        timezone = os.environ.get("BACKUP_TIMEZONE", "Europe/Moscow")
        ZoneInfo(timezone)
        return cls(
            bucket=os.environ["BACKUP_S3_BUCKET"],
            prefix=f"{prefix}/{database}/",
            endpoint=endpoint,
            region=os.environ.get("BACKUP_S3_REGION", "ru-1"),
            directory=Path(os.environ.get("BACKUP_STATE_DIR", "/state")),
            database=database,
            keep=keep,
            schedule=schedule,
            timezone=timezone,
        )


def scheduled_slot(now: datetime, schedule: str = "17:00", timezone: str = "Europe/Moscow") -> str:
    local = now.astimezone(ZoneInfo(timezone))
    hour, minute = map(int, schedule.split(":"))
    slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < slot:
        slot -= timedelta(days=1)
    return slot.astimezone(UTC).isoformat()


def save_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=True, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_error(exc: Exception) -> str:
    if isinstance(exc, BackupError):
        return str(exc)
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", "Unknown"))
        return f"S3 {code}" if re.fullmatch(r"[A-Za-z0-9]+", code) else "S3 error"
    return type(exc).__name__


class Backup:
    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        self.directory = settings.directory
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.scope = {"bucket": settings.bucket, "prefix": settings.prefix, "endpoint": settings.endpoint}
        self.client = (
            client
            if client is not None
            else boto3.client(
                "s3",
                endpoint_url=settings.endpoint,
                region_name=settings.region,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    connect_timeout=10,
                    read_timeout=60,
                    retries={"mode": "standard", "max_attempts": 3},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )
        )

    @contextmanager
    def locked(self) -> Generator[None]:
        # Контейнер Linux: advisory lock освобождается ОС даже после SIGKILL.
        import fcntl

        with (self.directory / "backup.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def state(self) -> dict[str, Any]:
        state = load_json(self.directory / "status.json")
        if state and state.get("scope") != self.scope:
            raise BackupError("Backup scope changed; use a separate state volume")
        return state or {"scope": self.scope}

    def store(self, state: dict[str, Any]) -> None:
        save_json(self.directory / "status.json", state)

    def record_error(self, exc: Exception) -> None:
        state = self.state()
        state["lastError"] = {"at": datetime.now(UTC).isoformat(), "reason": safe_error(exc)}
        self.store(state)

    def dump(self, output: Path) -> None:
        environment = os.environ.copy()
        environment.update(
            PGHOST=os.environ["POSTGRES_HOST"],
            PGPORT=os.environ.get("POSTGRES_PORT", "5432"),
            PGUSER=os.environ["POSTGRES_USER"],
            PGPASSWORD=os.environ["POSTGRES_PASSWORD"],
            PGCONNECT_TIMEOUT="10",
        )
        result = subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--file",
                str(output),
                "--dbname",
                self.settings.database,
                "--lock-wait-timeout=30s",
            ],
            env=environment,
            capture_output=True,
            timeout=self.settings.timeout,
        )
        if result.returncode != 0:
            raise BackupError("pg_dump failed; database contents were not uploaded")
        result = subprocess.run(["pg_restore", "--list", str(output)], capture_output=True, timeout=60)
        if result.returncode != 0:
            raise BackupError("pg_restore could not read the archive")

    def prepare(self, category: str, slot: str | None) -> dict[str, Any]:
        now = datetime.now(UTC)
        stem = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
        dump_key = f"{self.settings.prefix}{category}/{stem}.dump"
        path = self.directory / "pending.dump"
        self.dump(path)
        manifest = {
            "format": 1,
            "category": category,
            "database": self.settings.database,
            "createdAt": now.isoformat(),
            "slot": slot,
            "dumpKey": dump_key,
            "size": path.stat().st_size,
            "sha256": file_digest(path),
        }
        job = {"scope": self.scope, "manifest": manifest}
        save_json(self.directory / "pending.json", job)
        return job

    def verify(self, manifest: dict[str, Any], output: Path | None = None) -> None:
        response = self.client.get_object(Bucket=self.settings.bucket, Key=manifest["dumpKey"])
        digest, size = hashlib.sha256(), 0
        body = response["Body"]
        created = False
        try:
            with output.open("xb") if output else open(os.devnull, "wb") as destination:
                created = output is not None
                while chunk := body.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    if size > manifest["size"]:
                        raise BackupError("Downloaded archive exceeds manifest size")
                    destination.write(chunk)
                if size != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
                    raise BackupError("Downloaded S3 archive failed SHA-256 or size verification")
        except Exception:
            if output and created:
                output.unlink(missing_ok=True)
            raise
        finally:
            body.close()

    def finish_pending(self) -> dict[str, Any]:
        job = load_json(self.directory / "pending.json")
        if job.get("scope") != self.scope:
            raise BackupError("Pending backup belongs to another S3 scope")
        manifest = job["manifest"]
        path = self.directory / "pending.dump"
        if not path.exists() or path.stat().st_size != manifest["size"] or file_digest(path) != manifest["sha256"]:
            raise BackupError("Pending local archive is missing or corrupt; previous S3 copies were preserved")
        self.client.upload_file(
            str(path),
            self.settings.bucket,
            manifest["dumpKey"],
            ExtraArgs={"Metadata": {"sha256": manifest["sha256"]}},
            Config=TransferConfig(multipart_threshold=64 * 1024**2, max_concurrency=1, use_threads=False),
        )
        self.verify(manifest)
        key = manifest["dumpKey"].removesuffix(".dump") + ".json"
        self.client.put_object(
            Bucket=self.settings.bucket,
            Key=key,
            Body=json.dumps(manifest, sort_keys=True).encode(),
            ContentType="application/json",
        )
        if self.read_manifest(key) != manifest:
            raise BackupError("S3 manifest readback failed")
        state = self.state()
        if manifest["category"] == "daily":
            state["coveredSlot"] = max(state.get("coveredSlot", ""), manifest["slot"])
        state.update(lastSuccess={"at": datetime.now(UTC).isoformat(), "manifestKey": key}, cleanupRequired=True)
        state.pop("lastError", None)
        self.store(state)
        # Сначала состояние: при аварии здесь повтор использует тот же ключ, не новую копию.
        (self.directory / "pending.json").unlink()
        path.unlink(missing_ok=True)
        logger.info("Backup uploaded and verified: category=%s bytes=%s", manifest["category"], manifest["size"])
        return manifest

    def read_manifest(self, key: str) -> dict[str, Any]:
        relative = key.removeprefix(self.settings.prefix)
        pieces = relative.split("/")
        if not key.startswith(self.settings.prefix) or len(pieces) != 2 or pieces[0] not in CATEGORIES:
            raise BackupError("Manifest is outside this database backup prefix")
        if not NAME.fullmatch(pieces[1]):
            raise BackupError("Invalid manifest filename")
        response = self.client.get_object(Bucket=self.settings.bucket, Key=key)
        body = response["Body"]
        try:
            manifest = json.loads(body.read(65537))
        finally:
            body.close()
        if (
            manifest.get("format") != 1
            or manifest.get("database") != self.settings.database
            or manifest.get("category") != pieces[0]
            or manifest.get("dumpKey") != key.removesuffix(".json") + ".dump"
            or not isinstance(manifest.get("size"), int)
            or manifest["size"] <= 0
            or not re.fullmatch(r"[a-f0-9]{64}", manifest.get("sha256", ""))
        ):
            raise BackupError("Invalid backup manifest")
        datetime.fromisoformat(manifest["createdAt"])
        return manifest

    def prune(self) -> None:
        journal = self.directory / "retention.json"
        if journal.exists():
            self.finish_deletion(journal)
        for category in CATEGORIES:
            prefix = f"{self.settings.prefix}{category}/"
            candidates = []
            pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=self.settings.bucket, Prefix=prefix)
            for page in pages:
                for item in page.get("Contents", []):
                    key = item.get("Key", "")
                    if NAME.fullmatch(key.removeprefix(prefix)):
                        manifest = self.read_manifest(key)
                        head = self.client.head_object(Bucket=self.settings.bucket, Key=manifest["dumpKey"])
                        if head["ContentLength"] != manifest["size"]:
                            raise BackupError("Old backup size mismatch; retention aborted")
                        candidates.append((manifest["createdAt"], key, manifest["dumpKey"]))
            # До первой операции удаления проверяем все кандидаты категории.
            for _, key, dump_key in sorted(candidates, reverse=True)[self.settings.keep :]:
                save_json(journal, {"scope": self.scope, "manifestKey": key, "dumpKey": dump_key})
                self.finish_deletion(journal)

    def finish_deletion(self, journal: Path) -> None:
        item = load_json(journal)
        if item["scope"] != self.scope:
            raise BackupError("Retention scope changed")
        key = item["manifestKey"]
        relative = key.removeprefix(self.settings.prefix).split("/")
        if (
            not key.startswith(self.settings.prefix)
            or len(relative) != 2
            or relative[0] not in CATEGORIES
            or not NAME.fullmatch(relative[1])
            or item["dumpKey"] != key.removesuffix(".json") + ".dump"
        ):
            raise BackupError("Invalid retention journal")
        # Маркер исчезает первым; оставшийся dump удалится при повторе по журналу.
        self.client.delete_object(Bucket=self.settings.bucket, Key=key)
        self.client.delete_object(Bucket=self.settings.bucket, Key=item["dumpKey"])
        journal.unlink()

    def cleanup(self) -> None:
        state = self.state()
        try:
            self.prune()
        except Exception as exc:
            state["cleanupError"] = safe_error(exc)
            logger.error("Retention failed; verified backup remains valid: %s", safe_error(exc))
        else:
            state["cleanupRequired"] = False
            state.pop("cleanupError", None)
        self.store(state)

    def run(self, category: str, *, scheduled: bool = False, now: datetime | None = None) -> dict[str, Any]:
        with self.locked():
            self.state()
            resumed = None
            if (self.directory / "pending.json").exists():
                resumed = self.finish_pending()
            slot = scheduled_slot(now or datetime.now(UTC), self.settings.schedule, self.settings.timezone)
            state = self.state()
            if scheduled and state.get("coveredSlot", "") >= slot:
                if state.get("cleanupRequired"):
                    self.cleanup()
                return state
            # Ручной повтор того же вида завершает незаконченный job без второго dump.
            if not resumed or resumed["category"] != category or scheduled:
                self.prepare(category, slot if category == "daily" else None)
                self.finish_pending()
            self.cleanup()
            return self.state()


def request_stop(signum: int, frame: object) -> None:
    del signum, frame
    STOP.set()


def main() -> int:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    run = sub.add_parser("run")
    run.add_argument("--category", choices=CATEGORIES, default="daily")
    sub.add_parser("status")
    download = sub.add_parser("download")
    download.add_argument("--manifest", required=True)
    download.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        backup = Backup(Settings.from_env())
        if args.command == "status":
            print(json.dumps(backup.state(), sort_keys=True))
        elif args.command == "download":
            backup.verify(backup.read_manifest(args.manifest), args.output)
            logger.info("Archive downloaded and SHA-256 verified")
        elif args.command == "run":
            backup.run(args.category)
        else:
            signal.signal(signal.SIGTERM, request_stop)
            signal.signal(signal.SIGINT, request_stop)
            while not STOP.is_set():
                try:
                    backup.run("daily", scheduled=True)
                except Exception as exc:
                    logger.error("Backup attempt failed: %s", safe_error(exc))
                    backup.record_error(exc)
                    STOP.wait(300)
                else:
                    STOP.wait(60)
        return 0
    except (BackupError, BotoCoreError, ClientError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        logger.error("Backup command failed: %s", safe_error(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
