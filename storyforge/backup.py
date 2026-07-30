from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import zipfile
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4


BACKUP_SCHEMA_VERSION = 1
DEFAULT_RETENTION = timedelta(hours=72)
PARTIAL_RETENTION = timedelta(hours=24)
DEFAULT_DAILY_CHECK_SECONDS = 15 * 60
BACKUP_EXTENSION = ".sfbak"

_CATALOG_NAME = "storyforge-catalog.sqlite3"
_SETTINGS_NAME = "settings.json"
_PROVIDER_USAGE_NAME = "provider-usage.json"
_PRODUCTION_PRESETS_NAME = "production-presets.json"
_MANIFEST_NAME = "manifest.json"
_ATTACHMENT_ROOT = "hub-attachments"
_ALLOWED_ATTACHMENT_GROUPS = frozenset(
    {"covers", "platform-assets", "voice-previews", "preset-assets"}
)
_TRANSIENT_SUFFIXES = (".part", ".partial", ".tmp")
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_ARCHIVE_FILES = 50_000
_MAX_ARCHIVE_BYTES = 20 * 1024 * 1024 * 1024
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


class BackupError(RuntimeError):
    """Base class for managed Hub backup failures."""


class BackupSecurityError(BackupError):
    """Raised when a source or archive violates the fixed backup boundary."""


class BackupValidationError(BackupError):
    """Raised when a backup is corrupt, incomplete, or internally inconsistent."""


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stream_sha256(stream: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise BackupValidationError("backup manifest created_at is invalid") from error
    if parsed.tzinfo is None:
        raise BackupValidationError("backup manifest created_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise BackupValidationError("backup manifest is not JSON serializable") from error


def _manifest_digest(value: Mapping[str, Any]) -> str:
    payload = dict(value)
    payload.pop("manifest_sha256", None)
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        result = path.stat(follow_symlinks=False)
    except OSError:
        return False
    attributes = int(getattr(result, "st_file_attributes", 0) or 0)
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & flag)


def _ensure_regular_source(path: Path, *, directory: bool = False) -> None:
    if _is_reparse_point(path):
        raise BackupSecurityError(f"backup source is a link or reparse point: {path}")
    try:
        if directory:
            valid = path.is_dir()
        else:
            valid = path.is_file()
    except OSError as error:
        raise BackupError(f"cannot inspect backup source: {path}") from error
    if not valid:
        expected = "directory" if directory else "regular file"
        raise BackupSecurityError(f"backup source is not a {expected}: {path}")


def _is_transient(path: Path) -> bool:
    name = path.name.casefold()
    return name.startswith(".") or name.endswith(_TRANSIENT_SUFFIXES)


def _walk_regular_files(root: Path) -> Iterator[Path]:
    _ensure_regular_source(root, directory=True)
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name.casefold())
        except OSError as error:
            raise BackupError(f"cannot enumerate backup source: {current}") from error
        for entry in entries:
            path = Path(entry.path)
            if _is_transient(path):
                continue
            if _is_reparse_point(path) or entry.is_symlink():
                raise BackupSecurityError(
                    f"backup source is a link or reparse point: {path}"
                )
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path
                else:
                    raise BackupSecurityError(
                        f"backup source is not a regular file or directory: {path}"
                    )
            except OSError as error:
                raise BackupError(f"cannot inspect backup source: {path}") from error


def _copy_stable_file(source: Path, destination: Path, *, attempts: int = 3) -> None:
    _ensure_regular_source(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(attempts):
        try:
            before = source.stat(follow_symlinks=False)
            temporary = destination.with_name(f".{destination.name}-{uuid4().hex}.tmp")
            try:
                shutil.copyfile(source, temporary, follow_symlinks=False)
                after = source.stat(follow_symlinks=False)
                stable = (
                    before.st_size == after.st_size
                    and before.st_mtime_ns == after.st_mtime_ns
                    and getattr(before, "st_ino", None) == getattr(after, "st_ino", None)
                )
                if stable:
                    os.replace(temporary, destination)
                    return
            finally:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        except OSError as error:
            if attempt + 1 >= attempts:
                raise BackupError(f"cannot copy stable backup source: {source}") from error
        if attempt + 1 >= attempts:
            break
    raise BackupError(f"backup source changed repeatedly while copying: {source}")


def _sqlite_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    value = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()[0]
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _validate_sqlite(path: Path) -> int:
    try:
        connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=30,
        )
    except sqlite3.Error as error:
        raise BackupValidationError("backup catalog cannot be opened") from error
    try:
        quick_check = [
            str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()
        ]
        if quick_check != ["ok"]:
            raise BackupValidationError(
                "backup catalog failed quick_check: " + "; ".join(quick_check[:10])
            )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise BackupValidationError(
                f"backup catalog has {len(violations)} foreign-key violation(s)"
            )
        return _sqlite_schema_version(connection)
    except sqlite3.Error as error:
        raise BackupValidationError("backup catalog consistency check failed") from error
    finally:
        connection.close()


def _backup_sqlite(source_path: Path, destination_path: Path) -> int:
    _ensure_regular_source(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(
            source_path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=30,
            isolation_level=None,
        )
        destination = sqlite3.connect(str(destination_path), timeout=30)
        source.backup(destination, pages=1024, sleep=0.01)
        destination.commit()
    except sqlite3.Error as error:
        raise BackupError("could not create a consistent SQLite backup") from error
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
    schema_version = _validate_sqlite(destination_path)
    # A catalog backed up from WAL mode retains that journal setting. Merely
    # opening the staged copy for validation can therefore create empty
    # ``-wal``/``-shm`` companions. They are runtime coordination files, not
    # backup payloads; the main database already contains the consistent
    # snapshot produced by sqlite3_backup().
    for suffix in ("-wal", "-shm"):
        sidecar = destination_path.with_name(destination_path.name + suffix)
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise BackupError(
                f"could not remove staged SQLite sidecar: {sidecar}"
            ) from error
    return schema_version


def _safe_archive_member(name: str) -> PurePosixPath:
    if not name or "\\" in name or any(ord(character) < 32 for character in name):
        raise BackupSecurityError("backup archive contains an invalid path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupSecurityError(f"backup archive path is unsafe: {name}")
    for part in path.parts:
        stem = part.split(".", 1)[0].casefold()
        if (
            ":" in part
            or part.endswith((" ", "."))
            or stem in _WINDOWS_RESERVED_NAMES
        ):
            raise BackupSecurityError(f"backup archive path is unsafe on Windows: {name}")
    return path


def _allowed_payload_member(path: PurePosixPath) -> bool:
    parts = path.parts
    if parts in {
        ("data", _CATALOG_NAME),
        ("data", _SETTINGS_NAME),
        ("data", _PROVIDER_USAGE_NAME),
        ("data", _PRODUCTION_PRESETS_NAME),
    }:
        return True
    return (
        len(parts) >= 3
        and parts[0] == "attachments"
        and parts[1] in _ALLOWED_ATTACHMENT_GROUPS
    )


class HubBackupManager:
    """Create and inspect narrow, verified backups of Hub-owned data.

    The manager never traverses employee media/output folders.  Only the
    authoritative catalog, Hub settings/counters and the explicitly listed
    shared attachment groups are eligible for an archive.
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        backup_dir: str | Path | None = None,
        retention: timedelta = DEFAULT_RETENTION,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.backup_dir = (
            Path(backup_dir).expanduser().resolve()
            if backup_dir is not None
            else self.data_dir / "hub-backups"
        )
        if retention.total_seconds() <= 0:
            raise ValueError("retention must be positive")
        self.retention = retention
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._daily_stop = threading.Event()
        self._daily_thread: threading.Thread | None = None
        self._daily_check_seconds = float(DEFAULT_DAILY_CHECK_SECONDS)
        self._daily_enabled = False
        self._daily_date = ""
        self._daily_snapshot_id = ""
        self._daily_snapshot_path = ""
        self._status: dict[str, Any] = {
            "state": "idle",
            "last_backup_id": "",
            "last_backup_at": "",
            "last_backup_reason": "",
            "last_daily_at": "",
            "last_error": "",
            "next_check_at": "",
        }

    @property
    def catalog_path(self) -> Path:
        return self.data_dir / _CATALOG_NAME

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("backup clock must return datetime")
        return _utc(value)

    def status(self, *, include_error: bool = True) -> dict[str, Any]:
        """Return a JSON-safe scheduler and most-recent-snapshot summary."""

        with self._lock:
            thread = self._daily_thread
            result = {
                **self._status,
                "enabled": bool(self._daily_enabled),
                "running": bool(thread is not None and thread.is_alive()),
                "retention_hours": round(self.retention.total_seconds() / 3600, 3),
            }
        if not include_error:
            result["has_error"] = bool(result.pop("last_error", ""))
        return result

    def health_status(self) -> dict[str, Any]:
        """Return the non-sensitive subset suitable for unauthenticated health."""

        return self.status(include_error=False)

    def _record_snapshot_success(
        self,
        snapshot: Mapping[str, Any],
        *,
        update_recent: bool = True,
    ) -> None:
        created_at = _parse_utc(snapshot.get("created_at"))
        reason = str(snapshot.get("reason") or "")
        snapshot_id = str(snapshot.get("id") or "")
        with self._lock:
            if update_recent:
                self._status.update(
                    {
                        "last_backup_id": snapshot_id,
                        "last_backup_at": _iso_utc(created_at),
                        "last_backup_reason": reason,
                    }
                )
            if reason == "daily":
                self._daily_date = created_at.date().isoformat()
                self._daily_snapshot_id = snapshot_id
                self._daily_snapshot_path = str(snapshot.get("path") or "")
                self._status["last_daily_at"] = _iso_utc(created_at)
            self._status["state"] = "ready"
            self._status["last_error"] = ""

    def _record_scheduler_error(self, error: BaseException) -> None:
        with self._lock:
            self._status["state"] = "error"
            self._status["last_error"] = str(error) or type(error).__name__

    def ensure_daily_snapshot(self) -> dict[str, Any]:
        """Create at most one verified ``daily`` snapshot per UTC date."""

        current = self._now()
        current_date = current.date().isoformat()
        with self._lock:
            self._status["state"] = "checking"
            self._status["last_error"] = ""
            cached_path = Path(self._daily_snapshot_path) if self._daily_snapshot_path else None
            if (
                self._daily_date == current_date
                and self._daily_snapshot_id
                and cached_path is not None
                and cached_path.is_file()
                and not _is_reparse_point(cached_path)
            ):
                self._status["state"] = "ready"
                return {
                    "created": False,
                    "snapshot_id": self._daily_snapshot_id,
                    "created_at": self._status["last_daily_at"],
                }

            snapshots = self.list_snapshots(validate=True)
            valid_snapshots = [item for item in snapshots if item.get("valid") is True]
            if valid_snapshots:
                # Preserve the actual newest snapshot in the public status even
                # when today's daily snapshot is an older item in the list.
                self._record_snapshot_success(valid_snapshots[0])
            for snapshot in valid_snapshots:
                if str(snapshot.get("reason") or "") != "daily":
                    continue
                try:
                    snapshot_date = _parse_utc(snapshot.get("created_at")).date().isoformat()
                except BackupValidationError:
                    continue
                if snapshot_date != current_date:
                    continue
                self._record_snapshot_success(snapshot, update_recent=False)
                self.cleanup(now=current)
                return {
                    "created": False,
                    "snapshot_id": str(snapshot.get("id") or ""),
                    "created_at": str(snapshot.get("created_at") or ""),
                }

            snapshot = self.create_snapshot("daily", cleanup=True)
            return {
                "created": True,
                "snapshot_id": str(snapshot["id"]),
                "created_at": str(snapshot["created_at"]),
            }

    def _daily_loop(self) -> None:
        try:
            while not self._daily_stop.is_set():
                try:
                    self.ensure_daily_snapshot()
                except Exception as error:  # pragma: no branch - thread boundary
                    self._record_scheduler_error(error)
                if self._daily_stop.is_set():
                    break
                with self._lock:
                    next_check = self._now() + timedelta(
                        seconds=self._daily_check_seconds
                    )
                    self._status["next_check_at"] = _iso_utc(next_check)
                self._daily_stop.wait(self._daily_check_seconds)
        finally:
            with self._lock:
                self._status["next_check_at"] = ""
                if self._daily_stop.is_set():
                    self._status["state"] = "stopped"

    def start_daily(self, *, check_seconds: float = DEFAULT_DAILY_CHECK_SECONDS) -> None:
        """Start the idempotent daemon that maintains one snapshot per day."""

        interval = float(check_seconds)
        if interval <= 0:
            raise ValueError("daily backup check interval must be positive")
        with self._lock:
            if self._daily_thread is not None and self._daily_thread.is_alive():
                return
            self._daily_check_seconds = interval
            self._daily_stop.clear()
            self._daily_enabled = True
            self._status["state"] = "starting"
            thread = threading.Thread(
                target=self._daily_loop,
                name="storyforge-hub-backup",
                daemon=True,
            )
            self._daily_thread = thread
            thread.start()

    def stop_daily(self, *, timeout: float = 10.0) -> bool:
        """Request scheduler shutdown and report whether its thread exited."""

        with self._lock:
            self._daily_enabled = False
            self._daily_stop.set()
            thread = self._daily_thread
            if thread is not None and thread.is_alive():
                self._status["state"] = "stopping"
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=max(0.0, float(timeout)))
        stopped = bool(thread is None or not thread.is_alive())
        with self._lock:
            if stopped:
                self._daily_thread = None
                self._status["state"] = "stopped"
                self._status["next_check_at"] = ""
        return stopped

    @staticmethod
    def _clean_reason(value: str) -> str:
        reason = str(value or "").strip().casefold()
        if not _REASON_RE.fullmatch(reason):
            raise ValueError(
                "backup reason must contain only lowercase letters, numbers, "
                "hyphens or underscores"
            )
        return reason

    def _stage_sources(self, staging_root: Path) -> tuple[int, int | None]:
        catalog_schema = _backup_sqlite(
            self.catalog_path,
            staging_root / "data" / _CATALOG_NAME,
        )

        settings_schema: int | None = None
        settings_path = self.data_dir / _SETTINGS_NAME
        if settings_path.exists() or _is_reparse_point(settings_path):
            _copy_stable_file(settings_path, staging_root / "data" / _SETTINGS_NAME)
            try:
                raw = json.loads(
                    (staging_root / "data" / _SETTINGS_NAME).read_text(
                        encoding="utf-8"
                    )
                )
                settings_schema = int(raw.get("schema_version") or 0)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                settings_schema = None

        provider_usage = self.data_dir / _PROVIDER_USAGE_NAME
        if provider_usage.exists() or _is_reparse_point(provider_usage):
            _copy_stable_file(
                provider_usage,
                staging_root / "data" / _PROVIDER_USAGE_NAME,
            )

        production_presets = self.data_dir / _PRODUCTION_PRESETS_NAME
        if production_presets.exists() or _is_reparse_point(production_presets):
            _copy_stable_file(
                production_presets,
                staging_root / "data" / _PRODUCTION_PRESETS_NAME,
            )

        attachment_root = self.data_dir / _ATTACHMENT_ROOT
        if attachment_root.exists() or _is_reparse_point(attachment_root):
            _ensure_regular_source(attachment_root, directory=True)
            for group in sorted(_ALLOWED_ATTACHMENT_GROUPS):
                source_root = attachment_root / group
                if not source_root.exists() and not _is_reparse_point(source_root):
                    continue
                _ensure_regular_source(source_root, directory=True)
                for source in _walk_regular_files(source_root):
                    relative = source.relative_to(source_root)
                    _copy_stable_file(
                        source,
                        staging_root / "attachments" / group / relative,
                    )
        return catalog_schema, settings_schema

    @staticmethod
    def _staged_entries(staging_root: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for path in sorted(
            (item for item in staging_root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(staging_root).as_posix(),
        ):
            relative = path.relative_to(staging_root).as_posix()
            member = _safe_archive_member(relative)
            if not _allowed_payload_member(member):
                raise BackupSecurityError(
                    f"staged backup file is outside the allowlist: {relative}"
                )
            entries.append(
                {
                    "path": relative,
                    "size_bytes": path.stat().st_size,
                    "sha256": file_sha256(path),
                }
            )
        if not any(item["path"] == f"data/{_CATALOG_NAME}" for item in entries):
            raise BackupValidationError("staged backup does not contain the catalog")
        return entries

    def create_snapshot(
        self,
        reason: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        cleanup: bool = True,
    ) -> dict[str, Any]:
        reason = self._clean_reason(reason)
        created_at = self._now()
        backup_id = uuid4().hex
        timestamp = created_at.strftime("%Y%m%dT%H%M%S%fZ")
        filename = f"{timestamp}-{reason}-{backup_id[:12]}{BACKUP_EXTENSION}"

        with self._lock:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            if _is_reparse_point(self.backup_dir):
                raise BackupSecurityError("backup directory cannot be a link or reparse point")
            partial_path = self.backup_dir / f".partial-{uuid4().hex}{BACKUP_EXTENSION}"
            final_path = self.backup_dir / filename
            try:
                with tempfile.TemporaryDirectory(
                    prefix=".snapshot-", dir=self.backup_dir
                ) as temporary:
                    staging_root = Path(temporary)
                    catalog_schema, settings_schema = self._stage_sources(staging_root)
                    entries = self._staged_entries(staging_root)
                    clean_metadata: dict[str, Any] = {}
                    if metadata is not None:
                        try:
                            clean_metadata = json.loads(
                                _canonical_json(dict(metadata)).decode("utf-8")
                            )
                        except (json.JSONDecodeError, TypeError, ValueError) as error:
                            raise BackupValidationError(
                                "backup metadata must be a JSON object"
                            ) from error
                    manifest: dict[str, Any] = {
                        "schema_version": BACKUP_SCHEMA_VERSION,
                        "id": backup_id,
                        "reason": reason,
                        "created_at": _iso_utc(created_at),
                        "catalog_schema_version": catalog_schema,
                        "settings_schema_version": settings_schema,
                        "file_count": len(entries),
                        "total_size_bytes": sum(
                            int(item["size_bytes"]) for item in entries
                        ),
                        "files": entries,
                        "metadata": clean_metadata,
                    }
                    manifest["manifest_sha256"] = _manifest_digest(manifest)
                    manifest_bytes = _canonical_json(manifest)

                    with zipfile.ZipFile(
                        partial_path,
                        "x",
                        compression=zipfile.ZIP_DEFLATED,
                        compresslevel=6,
                        allowZip64=True,
                    ) as archive:
                        for entry in entries:
                            archive.write(
                                staging_root / Path(str(entry["path"])),
                                arcname=str(entry["path"]),
                            )
                        archive.writestr(_MANIFEST_NAME, manifest_bytes)
                    # Windows does not permit FlushFileBuffers through a
                    # read-only CRT descriptor. Open the completed archive for
                    # update without changing its contents, then fsync before
                    # the atomic rename.
                    with partial_path.open("r+b") as stream:
                        os.fsync(stream.fileno())
                    self._validate_path(partial_path, expected_id=backup_id)
                    os.replace(partial_path, final_path)
            except BaseException:
                try:
                    partial_path.unlink()
                except OSError:
                    pass
                raise

            result = self._validate_path(final_path, expected_id=backup_id)
            result["path"] = str(final_path)
            result["archive_size_bytes"] = final_path.stat().st_size
            if cleanup:
                result["cleanup"] = self.cleanup(now=created_at)
            self._record_snapshot_success(result)
            return result

    def _read_manifest(self, archive: zipfile.ZipFile) -> dict[str, Any]:
        infos = [item for item in archive.infolist() if item.filename == _MANIFEST_NAME]
        if len(infos) != 1:
            raise BackupValidationError("backup archive must contain one manifest.json")
        info = infos[0]
        if info.file_size > _MAX_MANIFEST_BYTES:
            raise BackupValidationError("backup manifest is too large")
        try:
            raw = archive.read(info)
            manifest = json.loads(raw.decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BackupValidationError("backup manifest is unreadable") from error
        if not isinstance(manifest, dict):
            raise BackupValidationError("backup manifest must be an object")
        try:
            schema = int(manifest.get("schema_version") or 0)
        except (TypeError, ValueError) as error:
            raise BackupValidationError("backup schema version is invalid") from error
        if schema != BACKUP_SCHEMA_VERSION:
            raise BackupValidationError(
                f"unsupported backup schema {schema}; expected {BACKUP_SCHEMA_VERSION}"
            )
        expected_manifest_hash = str(manifest.get("manifest_sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_hash):
            raise BackupValidationError("backup manifest SHA-256 is invalid")
        if _manifest_digest(manifest) != expected_manifest_hash:
            raise BackupValidationError("backup manifest SHA-256 does not match")
        return manifest

    def _validate_path(
        self, path: Path, *, expected_id: str | None = None
    ) -> dict[str, Any]:
        _ensure_regular_source(path)
        try:
            with zipfile.ZipFile(path, "r") as archive:
                infos = archive.infolist()
                if len(infos) > _MAX_ARCHIVE_FILES + 1:
                    raise BackupValidationError("backup archive contains too many files")
                seen: set[str] = set()
                seen_casefold: set[str] = set()
                total_declared = 0
                for info in infos:
                    member = _safe_archive_member(info.filename)
                    normalized = member.as_posix()
                    folded = normalized.casefold()
                    if normalized in seen or folded in seen_casefold:
                        raise BackupSecurityError(
                            f"backup archive contains a duplicate path: {normalized}"
                        )
                    seen.add(normalized)
                    seen_casefold.add(folded)
                    if info.flag_bits & 0x1:
                        raise BackupSecurityError("encrypted backup entries are not supported")
                    unix_mode = int(info.external_attr) >> 16
                    if unix_mode and stat.S_ISLNK(unix_mode):
                        raise BackupSecurityError(
                            f"backup archive contains a symbolic link: {normalized}"
                        )
                    if info.is_dir():
                        raise BackupSecurityError(
                            f"backup archive contains an unexpected directory entry: {normalized}"
                        )
                    if normalized != _MANIFEST_NAME and not _allowed_payload_member(member):
                        raise BackupSecurityError(
                            f"backup archive entry is outside the allowlist: {normalized}"
                        )
                    total_declared += int(info.file_size)
                    if total_declared > _MAX_ARCHIVE_BYTES:
                        raise BackupValidationError("backup archive is too large")

                manifest = self._read_manifest(archive)
                backup_id = str(manifest.get("id") or "")
                if not re.fullmatch(r"[0-9a-f]{32}", backup_id):
                    raise BackupValidationError("backup id is invalid")
                if expected_id is not None and backup_id != expected_id:
                    raise BackupValidationError("backup id does not match the requested snapshot")
                try:
                    reason = self._clean_reason(str(manifest.get("reason") or ""))
                except ValueError as error:
                    raise BackupValidationError("backup reason is invalid") from error
                created_at = _parse_utc(manifest.get("created_at"))
                raw_metadata = manifest.get("metadata")
                if raw_metadata is None:
                    raw_metadata = {}
                if not isinstance(raw_metadata, dict):
                    raise BackupValidationError("backup metadata must be an object")

                raw_entries = manifest.get("files")
                if not isinstance(raw_entries, list):
                    raise BackupValidationError("backup manifest files must be an array")
                expected_entries: dict[str, dict[str, Any]] = {}
                expected_casefold: set[str] = set()
                for raw in raw_entries:
                    if not isinstance(raw, dict):
                        raise BackupValidationError("backup manifest file entry is invalid")
                    member = _safe_archive_member(str(raw.get("path") or ""))
                    name = member.as_posix()
                    folded = name.casefold()
                    if (
                        not _allowed_payload_member(member)
                        or name in expected_entries
                        or folded in expected_casefold
                    ):
                        raise BackupSecurityError(
                            f"backup manifest file is outside the allowlist or duplicated: {name}"
                        )
                    try:
                        size = int(raw.get("size_bytes"))
                    except (TypeError, ValueError) as error:
                        raise BackupValidationError(
                            f"backup manifest size is invalid: {name}"
                        ) from error
                    digest = str(raw.get("sha256") or "")
                    if size < 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
                        raise BackupValidationError(
                            f"backup manifest checksum is invalid: {name}"
                        )
                    expected_entries[name] = {"size_bytes": size, "sha256": digest}
                    expected_casefold.add(folded)

                archive_payload = seen - {_MANIFEST_NAME}
                if archive_payload != set(expected_entries):
                    raise BackupValidationError(
                        "backup archive files do not match the manifest"
                    )
                if f"data/{_CATALOG_NAME}" not in expected_entries:
                    raise BackupValidationError("backup catalog is missing")

                total_verified = 0
                for name, expected in expected_entries.items():
                    info = archive.getinfo(name)
                    if int(info.file_size) != int(expected["size_bytes"]):
                        raise BackupValidationError(
                            f"backup file size does not match: {name}"
                        )
                    with archive.open(info, "r") as stream:
                        digest, size = _stream_sha256(stream)
                    if size != int(expected["size_bytes"]) or digest != expected["sha256"]:
                        raise BackupValidationError(
                            f"backup file SHA-256 does not match: {name}"
                        )
                    total_verified += size

                try:
                    declared_count = int(manifest.get("file_count"))
                    declared_total = int(manifest.get("total_size_bytes"))
                except (TypeError, ValueError) as error:
                    raise BackupValidationError(
                        "backup manifest totals are invalid"
                    ) from error
                if declared_count != len(expected_entries) or declared_total != total_verified:
                    raise BackupValidationError("backup manifest totals do not match")

                with tempfile.TemporaryDirectory(
                    prefix=".validate-", dir=self.backup_dir
                ) as temporary:
                    catalog_copy = Path(temporary) / _CATALOG_NAME
                    with archive.open(f"data/{_CATALOG_NAME}", "r") as source:
                        with catalog_copy.open("wb") as destination:
                            shutil.copyfileobj(source, destination, length=1024 * 1024)
                    catalog_schema = _validate_sqlite(catalog_copy)
                if int(manifest.get("catalog_schema_version") or 0) != catalog_schema:
                    raise BackupValidationError(
                        "backup catalog schema does not match the manifest"
                    )

                return {
                    "id": backup_id,
                    "reason": reason,
                    "created_at": _iso_utc(created_at),
                    "catalog_schema_version": catalog_schema,
                    "settings_schema_version": manifest.get(
                        "settings_schema_version"
                    ),
                    "file_count": len(expected_entries),
                    "total_size_bytes": total_verified,
                    "manifest_sha256": str(manifest["manifest_sha256"]),
                    "metadata": dict(raw_metadata),
                    "valid": True,
                }
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            raise BackupValidationError("backup archive cannot be opened") from error

    def _resolve_snapshot(self, value: str | Path) -> Path:
        raw = str(value or "").strip()
        if not raw:
            raise BackupValidationError("backup snapshot is required")
        direct = Path(raw)
        if direct.is_absolute() or direct.parent != Path("."):
            candidate = direct.expanduser().resolve()
            try:
                candidate.relative_to(self.backup_dir)
            except ValueError as error:
                raise BackupSecurityError(
                    "backup snapshot must be inside the managed backup directory"
                ) from error
            if candidate.parent != self.backup_dir:
                raise BackupSecurityError(
                    "backup snapshots cannot be loaded from nested directories"
                )
            return candidate

        candidate = self.backup_dir / raw
        if candidate.is_file():
            return candidate
        for path in sorted(self.backup_dir.glob(f"*{BACKUP_EXTENSION}")):
            if path.name.startswith(".partial-") or _is_reparse_point(path):
                continue
            try:
                inspected = self._validate_path(path)
            except BackupError:
                continue
            if inspected["id"] == raw:
                return path
        raise BackupValidationError("backup snapshot was not found")

    def validate_snapshot(self, value: str | Path) -> dict[str, Any]:
        with self._lock:
            path = self._resolve_snapshot(value)
            result = self._validate_path(path)
            result["path"] = str(path)
            result["archive_size_bytes"] = path.stat().st_size
            return result

    def list_snapshots(self, *, validate: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            if not self.backup_dir.is_dir():
                return []
            items: list[dict[str, Any]] = []
            for path in self.backup_dir.glob(f"*{BACKUP_EXTENSION}"):
                if path.name.startswith(".partial-"):
                    continue
                try:
                    if validate:
                        item = self._validate_path(path)
                    else:
                        _ensure_regular_source(path)
                        with zipfile.ZipFile(path, "r") as archive:
                            manifest = self._read_manifest(archive)
                        raw_metadata = manifest.get("metadata")
                        if raw_metadata is None:
                            raw_metadata = {}
                        if not isinstance(raw_metadata, dict):
                            raise BackupValidationError(
                                "backup metadata must be an object"
                            )
                        item = {
                            "id": str(manifest.get("id") or ""),
                            "reason": str(manifest.get("reason") or ""),
                            "created_at": _iso_utc(
                                _parse_utc(manifest.get("created_at"))
                            ),
                            "catalog_schema_version": manifest.get(
                                "catalog_schema_version"
                            ),
                            "settings_schema_version": manifest.get(
                                "settings_schema_version"
                            ),
                            "file_count": manifest.get("file_count"),
                            "total_size_bytes": manifest.get("total_size_bytes"),
                            "manifest_sha256": manifest.get("manifest_sha256"),
                            "metadata": dict(raw_metadata),
                            "valid": None,
                        }
                    item["path"] = str(path)
                    item["archive_size_bytes"] = path.stat().st_size
                except (BackupError, OSError, zipfile.BadZipFile) as error:
                    item = {
                        "id": "",
                        "path": str(path),
                        "archive_size_bytes": (
                            path.stat().st_size if path.is_file() else 0
                        ),
                        "valid": False,
                        "error": str(error) or type(error).__name__,
                    }
                items.append(item)

            def sort_key(item: Mapping[str, Any]) -> tuple[datetime, str]:
                try:
                    created = _parse_utc(item.get("created_at"))
                except BackupError:
                    created = datetime.min.replace(tzinfo=timezone.utc)
                return created, str(item.get("path") or "")

            items.sort(key=sort_key, reverse=True)
            return items

    def cleanup(self, *, now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            current = _utc(now) if now is not None else self._now()
            cutoff = current - self.retention
            partial_cutoff = current - PARTIAL_RETENTION
            removed: list[str] = []
            partial_removed: list[str] = []
            invalid_removed: list[str] = []
            errors: list[dict[str, str]] = []

            if not self.backup_dir.is_dir():
                return {
                    "removed": removed,
                    "partial_removed": partial_removed,
                    "invalid_removed": invalid_removed,
                    "retained": [],
                    "invalid": [],
                    "errors": errors,
                }

            valid: list[tuple[datetime, Path, dict[str, Any]]] = []
            invalid: list[str] = []
            for path in self.backup_dir.glob(f"*{BACKUP_EXTENSION}"):
                if path.name.startswith(".partial-"):
                    try:
                        if _is_reparse_point(path):
                            invalid.append(str(path))
                            continue
                        modified = datetime.fromtimestamp(
                            path.stat().st_mtime, tz=timezone.utc
                        )
                        if modified < partial_cutoff:
                            path.unlink()
                            partial_removed.append(str(path))
                    except OSError as error:
                        errors.append({"path": str(path), "error": str(error)})
                    continue
                try:
                    inspected = self._validate_path(path)
                    valid.append(
                        (_parse_utc(inspected["created_at"]), path, inspected)
                    )
                except (BackupError, OSError) as error:
                    invalid.append(str(path))
                    errors.append({"path": str(path), "error": str(error)})
                    # A payload may be corrupt while its self-hashed manifest
                    # is still a trustworthy marker that this module created
                    # the archive. Such managed failures should not accumulate
                    # forever. Unknown files with an unreadable/forged manifest
                    # are deliberately left untouched for manual inspection.
                    try:
                        _ensure_regular_source(path)
                        with zipfile.ZipFile(path, "r") as archive:
                            manifest = self._read_manifest(archive)
                        created_at = _parse_utc(manifest.get("created_at"))
                        if created_at < cutoff:
                            path.unlink()
                            invalid_removed.append(str(path))
                    except (BackupError, OSError, zipfile.BadZipFile):
                        pass

            valid.sort(key=lambda item: (item[0], item[1].name), reverse=True)
            newest_path = valid[0][1] if valid else None
            retained: list[str] = []
            for created_at, path, _inspected in valid:
                if path == newest_path or created_at >= cutoff:
                    retained.append(str(path))
                    continue
                try:
                    path.unlink()
                    removed.append(str(path))
                except OSError as error:
                    retained.append(str(path))
                    errors.append({"path": str(path), "error": str(error)})

            return {
                "removed": removed,
                "partial_removed": partial_removed,
                "invalid_removed": invalid_removed,
                "retained": retained,
                "invalid": invalid,
                "errors": errors,
            }


__all__ = [
    "BACKUP_EXTENSION",
    "BACKUP_SCHEMA_VERSION",
    "BackupError",
    "BackupSecurityError",
    "BackupValidationError",
    "DEFAULT_DAILY_CHECK_SECONDS",
    "DEFAULT_RETENTION",
    "HubBackupManager",
    "file_sha256",
]
