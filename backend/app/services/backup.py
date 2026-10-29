"""Portable SQLite snapshots. No archive extraction and no active-state replacement."""

import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import mkdtemp
from uuid import uuid4

from app.services.storage import Storage

COMPONENTS = ["documents", "chunks", "originals", "services", "incidents"]


def safe_path(path: Path) -> Path:
    path = path.absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Backup/restore paths must not traverse symlinks.")
    return path.resolve()


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect_snapshot(path):
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        Storage.check_schema(db)
        if db.execute("PRAGMA user_version").fetchone()[0] != Storage.SCHEMA_VERSION:
            raise ValueError("Start Grimoire once to upgrade legacy storage before backup.")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup database integrity check failed.")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Backup database has invalid references.")
        rows = db.execute("SELECT vector FROM chunks").fetchall()
        dimension = Storage.check_vectors(rows)
        model = db.execute("SELECT value FROM metadata WHERE key='embedding_model'").fetchone()
        return {
            "schema_version": Storage.SCHEMA_VERSION,
            "embedding_identity": model[0] if model else None,
            "vector_index": {
                "backend": "qdrant-memory-rebuilt-from-sqlite",
                "dimension": dimension,
            },
            "components": {
                name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in COMPONENTS
            },
        }
    finally:
        db.close()


def sync_file(path):
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def create_backup(data_dir: Path, destination: Path) -> dict:
    source = safe_path(data_dir) / "grimoire.sqlite3"
    destination = safe_path(destination)
    safe_path(source)
    if not source.is_file():
        raise ValueError("No initialized database to back up.")
    if destination == source.parent or source.parent in destination.parents:
        raise ValueError("Backups must be outside the active data directory.")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(mkdtemp(prefix=".grimoire-backup-", dir=destination))
    try:
        snapshot = temporary / "grimoire.sqlite3"
        source_db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)
        target_db = sqlite3.connect(snapshot)
        try:
            deadline = time.monotonic() + 60

            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "Backup snapshot exceeded 60 seconds; retry when storage is quiet."
                    )

            source_db.backup(target_db, pages=256, progress=progress)
            target_db.execute("PRAGMA journal_mode=DELETE")
        finally:
            target_db.close()
            source_db.close()
        snapshot.chmod(0o600)
        manifest = {
            "format_version": 1,
            "grimoire_version": version("grimoire-ai"),
            "created_at": datetime.now(UTC).isoformat(),
            **inspect_snapshot(snapshot),
            "checksums": {"grimoire.sqlite3": checksum(snapshot)},
            "excluded": ["configuration/secrets", "model weights", "logs", "caches"],
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)
        sync_file(snapshot)
        sync_file(manifest_path)
        sync_directory(temporary)
        published = destination / ("grimoire-" + uuid4().hex)
        temporary.rename(published)
        sync_directory(destination)
        return {"backup": str(published), "manifest": manifest}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def restore_backup(backup: Path, destination: Path) -> dict:
    backup, destination = safe_path(backup), safe_path(destination)
    if destination.exists():
        raise ValueError("Restore destination must not exist; stop the service and use a new path.")
    snapshot = safe_path(backup / "grimoire.sqlite3")
    manifest_path = safe_path(backup / "manifest.json")
    if manifest_path.stat().st_size > 65536:
        raise ValueError("Backup manifest is too large.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format_version") != 1:
        raise ValueError("Unsupported backup format.")
    if manifest.get("checksums") != {"grimoire.sqlite3": checksum(snapshot)}:
        raise ValueError("Backup checksum mismatch; restore refused.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(mkdtemp(prefix=".grimoire-restore-", dir=destination.parent))
    try:
        target = temporary / "grimoire.sqlite3"
        shutil.copyfile(snapshot, target)
        target.chmod(0o600)
        if checksum(target) != manifest["checksums"]["grimoire.sqlite3"]:
            raise ValueError("Backup changed during restore.")
        details = inspect_snapshot(target)
        if any(manifest.get(key) != value for key, value in details.items()):
            raise ValueError("Backup manifest does not match its database.")
        sync_file(target)
        sync_directory(temporary)
        temporary.rename(destination)
        sync_directory(destination.parent)
        return {"restored": str(destination), "storage": details}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
