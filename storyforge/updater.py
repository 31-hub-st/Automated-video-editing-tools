from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any


UPDATE_PACKAGE_METADATA = "storyforge-update.json"
UPDATE_MANIFEST_SCHEMA = 1
MAX_UPDATE_ENTRIES = 20_000
MAX_UPDATE_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
UPDATE_ROLLBACK_RETENTION = timedelta(days=3)
UPDATE_INSTALLING_GRACE = timedelta(minutes=10)
UPDATE_SPACE_HEADROOM_BYTES = 512 * 1024 * 1024
_VERSION_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?P<suffix>-(?:[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*))?$"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_version(value: Any) -> str:
    version = str(value or "").strip()
    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("版本号必须采用 major.minor.patch 格式，例如 2.1.0。")
    return version


def version_key(value: Any) -> tuple[int, int, int, int, tuple[tuple[int, Any], ...]]:
    """Return a deterministic SemVer-like ordering without another dependency."""

    version = normalize_version(value)
    matched = _VERSION_PATTERN.fullmatch(version)
    assert matched is not None
    suffix = str(matched.group("suffix") or "")
    if not suffix:
        prerelease: tuple[tuple[int, Any], ...] = ()
        final = 1
    else:
        final = 0
        # Desktop prereleases historically use compact labels such as ``rc7``.
        # Split every alpha/numeric run so rc10 correctly sorts after rc7
        # instead of being compared as the lexical strings "rc10" < "rc7".
        identifiers = re.findall(r"[A-Za-z]+|\d+", suffix[1:])
        prerelease = tuple(
            (0, int(item)) if item.isdecimal() else (1, item.casefold())
            for item in identifiers
        )
    return (
        int(matched.group("major")),
        int(matched.group("minor")),
        int(matched.group("patch")),
        final,
        prerelease,
    )


def is_newer_version(candidate: Any, current: Any) -> bool:
    return version_key(candidate) > version_key(current)


def _safe_relative_path(value: Any, *, label: str) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    if not raw or len(raw) > 1024 or "\x00" in raw:
        raise ValueError(f"{label}路径无效。")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"{label}路径必须位于更新包内部。")
    if pure.parts and ":" in pure.parts[0]:
        raise ValueError(f"{label}路径不能包含盘符。")
    return pure.as_posix()


def validate_update_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("更新清单必须是对象。")
    try:
        schema_version = int(value.get("schema_version") or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("更新清单版本无效。") from error
    if schema_version != UPDATE_MANIFEST_SCHEMA:
        raise ValueError("更新清单版本不受支持。")
    version = normalize_version(value.get("version"))
    filename = Path(_safe_relative_path(value.get("filename"), label="更新文件")).name
    if not filename.casefold().endswith(".zip"):
        raise ValueError("更新文件必须是 ZIP 安装包。")
    digest = str(value.get("sha256") or "").strip().casefold()
    if len(digest) != 64 or any(item not in "0123456789abcdef" for item in digest):
        raise ValueError("更新文件 SHA-256 无效。")
    try:
        size_bytes = int(value.get("size_bytes"))
    except (TypeError, ValueError) as error:
        raise ValueError("更新文件大小无效。") from error
    if size_bytes <= 0:
        raise ValueError("更新文件不能为空。")
    entrypoint = _safe_relative_path(value.get("entrypoint"), label="启动文件")
    release_notes = str(value.get("release_notes") or "").strip()
    if len(release_notes) > 8000:
        raise ValueError("更新说明不能超过 8000 个字符。")
    published_at = str(value.get("published_at") or "").strip()
    if not published_at:
        raise ValueError("更新清单缺少发布时间。")
    return {
        "schema_version": UPDATE_MANIFEST_SCHEMA,
        "version": version,
        "filename": filename,
        "sha256": digest,
        "size_bytes": size_bytes,
        "entrypoint": entrypoint,
        "release_notes": release_notes,
        "published_at": published_at,
    }


def canonical_manifest_bytes(value: Mapping[str, Any] | None) -> bytes:
    return json.dumps(
        dict(value) if value is not None else None,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_update_manifest(token: str, value: Mapping[str, Any] | None) -> str:
    return hmac.new(
        str(token).encode("utf-8"),
        canonical_manifest_bytes(value),
        hashlib.sha256,
    ).hexdigest()


def verify_update_manifest_signature(
    token: str,
    value: Mapping[str, Any] | None,
    signature: Any,
) -> None:
    expected = sign_update_manifest(token, value)
    if not hmac.compare_digest(expected, str(signature or "").strip().casefold()):
        raise ValueError("更新清单签名验证失败。")


def inspect_update_package(
    package_path: str | Path,
    *,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Validate archive structure and return its embedded package metadata."""

    path = Path(package_path).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.casefold() != ".zip":
        raise ValueError("更新包必须是存在的 ZIP 文件。")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_UPDATE_ENTRIES:
                raise ValueError("更新包文件数量无效。")
            total_size = 0
            metadata_entry: zipfile.ZipInfo | None = None
            normalized_names: set[str] = set()
            for entry in entries:
                normalized = _safe_relative_path(entry.filename, label="压缩包文件")
                normalized_key = normalized.casefold()
                if normalized_key in normalized_names:
                    raise ValueError("更新包包含重复文件路径。")
                normalized_names.add(normalized_key)
                # POSIX symlinks are unsafe for an installer extraction root.
                if ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                    raise ValueError("更新包不能包含符号链接。")
                if entry.file_size < 0:
                    raise ValueError("更新包包含大小无效的文件。")
                total_size += int(entry.file_size)
                if total_size > MAX_UPDATE_UNCOMPRESSED_BYTES:
                    raise ValueError("更新包解压后体积超过安全限制。")
                if normalized == UPDATE_PACKAGE_METADATA:
                    metadata_entry = entry
            if metadata_entry is None:
                raise ValueError(f"更新包缺少 {UPDATE_PACKAGE_METADATA}。")
            if metadata_entry.file_size > 64 * 1024:
                raise ValueError("更新包元数据过大。")
            try:
                raw_metadata = json.loads(
                    archive.read(metadata_entry).decode("utf-8")
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("更新包元数据不是有效的 UTF-8 JSON。") from error
    except zipfile.BadZipFile as error:
        raise ValueError("更新包 ZIP 文件已损坏。") from error
    if not isinstance(raw_metadata, Mapping):
        raise ValueError("更新包元数据必须是对象。")
    version = normalize_version(raw_metadata.get("version"))
    if expected_version is not None and version != normalize_version(expected_version):
        raise ValueError("更新包内部版本与发布版本不一致。")
    entrypoint = _safe_relative_path(raw_metadata.get("entrypoint"), label="启动文件")
    if entrypoint.casefold() not in normalized_names:
        raise ValueError("更新包指定的启动文件不存在。")
    return {
        "version": version,
        "entrypoint": entrypoint,
        "uncompressed_size_bytes": total_size,
        "entry_count": len(entries),
    }


class UpdateRepository:
    """Atomic, local publisher store owned by the Hub computer."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.json"
        self._lock = threading.RLock()
        self._cached_manifest: dict[str, Any] | None = None
        self._cached_package_fingerprint: tuple[str, int, int] | None = None

    @staticmethod
    def _write_json_atomic(path: Path, value: Any) -> None:
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary_name, path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    def get_manifest(self) -> dict[str, Any] | None:
        with self._lock:
            if not self.manifest_path.is_file():
                return None
            try:
                raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                manifest = validate_update_manifest(raw)
                package = self.resolve_package(manifest)
                if package.stat().st_size != manifest["size_bytes"]:
                    raise ValueError("更新包大小与清单不一致。")
                stat = package.stat()
                fingerprint = (package.name, stat.st_size, stat.st_mtime_ns)
                if (
                    self._cached_manifest != manifest
                    or self._cached_package_fingerprint != fingerprint
                ):
                    if file_sha256(package) != manifest["sha256"]:
                        raise ValueError("更新包摘要与清单不一致。")
                    self._cached_manifest = dict(manifest)
                    self._cached_package_fingerprint = fingerprint
                return manifest
            except (OSError, json.JSONDecodeError, ValueError):
                # A partially copied or manually modified release is never
                # advertised to clients.
                return None

    def publish(
        self,
        package_path: str | Path,
        version: str,
        release_notes: str = "",
        *,
        progress: Callable[[float, str], None] | None = None,
    ) -> dict[str, Any]:
        version = normalize_version(version)
        notes = str(release_notes or "").strip()
        if len(notes) > 8000:
            raise ValueError("更新说明不能超过 8000 个字符。")
        source = Path(package_path).expanduser().resolve(strict=True)
        if progress is not None:
            progress(0.08, "正在检查更新包结构…")
        metadata = inspect_update_package(source, expected_version=version)
        if progress is not None:
            progress(0.22, "正在计算更新包校验值…")
        digest = file_sha256(source)
        filename = f"StoryForge-{version}-{digest[:12]}.zip"
        destination = (self.root / filename).resolve()
        destination.relative_to(self.root)
        with self._lock:
            if progress is not None:
                progress(0.42, "正在复制更新包到 Hub…")
            temporary = self.root / f".{filename}.{os.getpid()}.part"
            try:
                shutil.copy2(source, temporary)
                if temporary.stat().st_size != source.stat().st_size:
                    raise OSError("更新包复制不完整。")
                if file_sha256(temporary) != digest:
                    raise OSError("更新包复制校验失败。")
                os.replace(temporary, destination)
            finally:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            manifest = validate_update_manifest(
                {
                    "schema_version": UPDATE_MANIFEST_SCHEMA,
                    "version": version,
                    "filename": filename,
                    "sha256": digest,
                    "size_bytes": destination.stat().st_size,
                    "entrypoint": metadata["entrypoint"],
                    "release_notes": notes,
                    "published_at": utc_now(),
                }
            )
            if progress is not None:
                progress(0.86, "正在写入发布清单…")
            self._write_json_atomic(self.manifest_path, manifest)
            stat = destination.stat()
            self._cached_manifest = dict(manifest)
            self._cached_package_fingerprint = (
                destination.name,
                stat.st_size,
                stat.st_mtime_ns,
            )
            # The atomic manifest replacement above is the release commit
            # point. Only then is it safe to discard packages referenced by
            # older manifests. Cleanup remains best-effort because antivirus
            # or a concurrent client download can briefly lock a ZIP on
            # Windows; that must not make a valid publication fail.
            self._remove_superseded_packages(destination)
            if progress is not None:
                progress(1.0, "更新已经发布。")
            return manifest

    def _remove_superseded_packages(self, current_package: Path) -> None:
        current = current_package.resolve(strict=False)
        for candidate in self.root.glob("StoryForge-*.zip"):
            try:
                if candidate.resolve(strict=False) == current:
                    continue
                candidate.unlink()
            except OSError:
                # A later publication will retry cleanup of locked files.
                continue

    def clear(self) -> None:
        """Stop advertising a release without deleting the verified package."""

        with self._lock:
            try:
                self.manifest_path.unlink()
            except FileNotFoundError:
                pass
            self._cached_manifest = None
            self._cached_package_fingerprint = None

    def resolve_package(self, manifest: Mapping[str, Any]) -> Path:
        checked = validate_update_manifest(manifest)
        candidate = (self.root / checked["filename"]).resolve(strict=False)
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise ValueError("更新包路径超出发布目录。") from error
        if not candidate.is_file():
            raise FileNotFoundError("已发布的更新包不存在。")
        return candidate


class UpdateManager:
    """Client-side update monitor that never mutates the live installation."""

    def __init__(
        self,
        *,
        current_version: str,
        data_dir: str | Path,
        client_getter: Callable[[], Any | None],
        mode_getter: Callable[[], str],
        enabled_getter: Callable[[], bool],
        auto_download_getter: Callable[[], bool],
        interval_minutes_getter: Callable[[], int | float],
        rendering_busy_getter: Callable[[], bool],
        launcher: Callable[[Path, Path, int], None] | None = None,
    ) -> None:
        self.current_version = normalize_version(current_version)
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.state_root = self.data_dir / "updates"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.pending_path = self.state_root / "pending-update.json"
        self.worker_path = self.state_root / "apply-update.ps1"
        self.result_path = self.state_root / "last-update-result.json"
        self.storage_root = self._select_storage_root()
        self.cache_root = self.storage_root / "downloads"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._client_getter = client_getter
        self._mode_getter = mode_getter
        self._enabled_getter = enabled_getter
        self._auto_download_getter = auto_download_getter
        self._interval_minutes_getter = interval_minutes_getter
        self._rendering_busy_getter = rendering_busy_getter
        self._launcher = launcher or self._launch_windows_worker
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_ready = False
        self._pending_version = ""
        self._status: dict[str, Any] = {
            "current_version": self.current_version,
            "available_version": "",
            "scheduled_version": "",
            "state": "idle",
            "progress": 0.0,
            "message": "尚未检查更新。",
            "checked_at": "",
            "downloaded_at": "",
            "package_path": "",
            "downloaded": False,
            "apply_on_restart": False,
            "restart_required": False,
            "rendering_busy": False,
            "release_notes": "",
            "published_at": "",
            "error": "",
        }
        self._manifest: dict[str, Any] | None = None
        self._restore_pending_marker()
        # Antivirus and a previous interrupted updater can leave files locked.
        # Cleanup is intentionally best-effort and must never block startup.
        self._cleanup_update_storage()

    def _select_storage_root(self) -> Path:
        """Keep large frozen-client update data beside the installation.

        Employee installations normally live on D:/E:.  Downloads, staging
        and rollback copies therefore belong on that same volume instead of
        filling AppData on C:.  Source/development runs deliberately retain
        the old deterministic data-directory layout used by tests and tools.
        """

        fallback = self.state_root
        if not bool(getattr(sys, "frozen", False)):
            return fallback
        try:
            install_root = self.install_root().resolve()
            identity = hashlib.sha256(
                str(install_root).casefold().encode("utf-8")
            ).hexdigest()[:10]
            candidate = install_root.parent / f".storyforge-updates-{identity}"
            candidate.mkdir(parents=True, exist_ok=True)
            handle, probe_name = tempfile.mkstemp(
                prefix=".write-probe-", dir=candidate
            )
            os.close(handle)
            Path(probe_name).unlink()
            return candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            # Program Files and managed corporate directories may reject a
            # sibling folder. AppData remains the safe compatibility fallback.
            return fallback

    @staticmethod
    def _parse_utc(value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _path_is_within(path: Path, root: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(root.resolve(strict=False))
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _remove_path_best_effort(path: Path) -> bool:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return not path.exists()

    def _pending_package_path(self) -> Path | None:
        if not self.pending_path.is_file():
            return None
        try:
            marker = json.loads(self.pending_path.read_text(encoding="utf-8"))
            return Path(str(marker["package_path"])).resolve(strict=False)
        except (KeyError, OSError, json.JSONDecodeError, ValueError):
            return None

    def _cleanup_update_storage(self, *, now: datetime | None = None) -> None:
        """Bound update cache and rollback retention without risking startup."""

        try:
            current_time = (now or datetime.now(timezone.utc)).astimezone(
                timezone.utc
            )
            preserved_packages: set[Path] = set()
            pending_package = self._pending_package_path()
            if pending_package is not None:
                preserved_packages.add(pending_package.resolve(strict=False))
            status_package = str(self._status.get("package_path") or "").strip()
            if status_package:
                preserved_packages.add(Path(status_package).resolve(strict=False))

            # Also sweep the legacy AppData cache after a frozen client moves
            # its new downloads to the installation volume.
            cache_roots = {
                self.cache_root.resolve(strict=False),
                (self.state_root / "downloads").resolve(strict=False),
            }
            for cache_root in cache_roots:
                try:
                    children = list(cache_root.iterdir())
                except OSError:
                    continue
                for child in children:
                    resolved = child.resolve(strict=False)
                    if any(
                        package == resolved or resolved in package.parents
                        for package in preserved_packages
                    ):
                        continue
                    self._remove_path_best_effort(child)

            result: dict[str, Any] = {}
            try:
                raw_result = json.loads(self.result_path.read_text(encoding="utf-8"))
                if isinstance(raw_result, Mapping):
                    result = dict(raw_result)
            except (OSError, json.JSONDecodeError, ValueError):
                result = {}

            apply_roots = {
                self.storage_root.resolve(strict=False),
                self.state_root.resolve(strict=False),
            }
            retained_apply: Path | None = None
            # While the external installer is health-checking the newly
            # launched process, that process also constructs UpdateManager.
            # Preserve the installer's live rollback directory so startup
            # cleanup cannot erase the files needed for an automatic rollback.
            try:
                pending_marker = json.loads(
                    self.pending_path.read_text(encoding="utf-8")
                )
                active_at = self._parse_utc(pending_marker.get("installing_at"))
                active_value = str(
                    pending_marker.get("apply_work_root") or ""
                ).strip()
                if (
                    bool(pending_marker.get("installing"))
                    and active_at is not None
                    and current_time - active_at <= UPDATE_INSTALLING_GRACE
                    and active_value
                ):
                    active_candidate = Path(active_value).resolve(strict=False)
                    if active_candidate.name.startswith("apply-") and any(
                        self._path_is_within(active_candidate, root)
                        for root in apply_roots
                    ):
                        retained_apply = active_candidate
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
            installed_at = self._parse_utc(result.get("installed_at"))
            backup_value = str(result.get("backup_path") or "").strip()
            if (
                result.get("status") == "installed"
                and bool(result.get("rollback_available"))
                and installed_at is not None
                and backup_value
            ):
                backup = Path(backup_value).resolve(strict=False)
                candidate = backup.parent
                allowed = any(
                    candidate.name.startswith("apply-")
                    and self._path_is_within(candidate, root)
                    for root in apply_roots
                )
                age = current_time - installed_at
                if (
                    retained_apply is None
                    and allowed
                    and age <= UPDATE_ROLLBACK_RETENTION
                ):
                    retained_apply = candidate

            for root in apply_roots:
                try:
                    candidates = list(root.glob("apply-*"))
                except OSError:
                    continue
                for candidate in candidates:
                    if retained_apply is not None and (
                        candidate.resolve(strict=False) == retained_apply
                    ):
                        continue
                    removed = self._remove_path_best_effort(candidate)
                    if (
                        removed
                        and backup_value
                        and Path(backup_value).resolve(strict=False).parent
                        == candidate.resolve(strict=False)
                    ):
                        result["backup_path"] = ""
                        result["rollback_available"] = False
                        try:
                            self._write_json_atomic(self.result_path, result)
                        except OSError:
                            pass
        except (OSError, RuntimeError, TypeError, ValueError):
            # Cleanup is maintenance, never a startup prerequisite.
            return

    @staticmethod
    def _write_json_atomic(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary_name, path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

    def _restore_pending_marker(self) -> None:
        if not self.pending_path.is_file():
            return
        try:
            marker = json.loads(self.pending_path.read_text(encoding="utf-8"))
            manifest = validate_update_manifest(marker["manifest"])
            package = Path(str(marker["package_path"])).resolve(strict=True)
            installing_at = self._parse_utc(marker.get("installing_at"))
            installing_now = bool(marker.get("installing")) and (
                installing_at is not None
                and datetime.now(timezone.utc) - installing_at
                <= UPDATE_INSTALLING_GRACE
            )
            # The installer has already verified the archive before it writes
            # ``installing=true``. Re-hashing a 0.7-GB package in the newly
            # launched process would delay startup and can hold a Windows file
            # lock while the installer is trying to remove that package.
            if not installing_now:
                if package.stat().st_size != manifest["size_bytes"]:
                    raise ValueError("已下载更新包大小不一致。")
                if file_sha256(package) != manifest["sha256"]:
                    raise ValueError("已下载更新包摘要不一致。")
                inspect_update_package(
                    package, expected_version=manifest["version"]
                )
        except (KeyError, OSError, json.JSONDecodeError, ValueError) as error:
            self._status.update(
                {
                    "state": "error",
                    "message": "待安装更新记录已损坏，请重新下载并安排更新。",
                    "error": str(error) or type(error).__name__,
                }
            )
            return
        self._manifest = manifest
        if installing_now:
            # The external installer launches the new binary before it removes
            # the marker.  Suppress a second apply cycle during that health
            # window; an abandoned marker becomes retryable after ten minutes.
            self._pending_ready = False
            self._pending_version = manifest["version"]
            self._status.update(
                {
                    "available_version": manifest["version"],
                    "scheduled_version": manifest["version"],
                    "state": "applying_on_restart",
                    "progress": 1.0,
                    "message": "正在验证新版 StoryForge，请稍候。",
                    "package_path": str(package),
                    "downloaded": True,
                    "apply_on_restart": False,
                    "restart_required": False,
                    "release_notes": manifest["release_notes"],
                    "published_at": manifest["published_at"],
                }
            )
            return
        self._pending_ready = True
        self._pending_version = manifest["version"]
        self._status.update(
            {
                "available_version": manifest["version"],
                "scheduled_version": manifest["version"],
                "state": "scheduled",
                "progress": 1.0,
                "message": "更新已下载，将在安全退出 StoryForge 后安装。",
                "package_path": str(package),
                "downloaded": True,
                "apply_on_restart": True,
                "restart_required": True,
                "release_notes": manifest["release_notes"],
                "published_at": manifest["published_at"],
            }
        )

    def status(self) -> dict[str, Any]:
        with self._lock:
            result = dict(self._status)
        result["mode"] = str(self._mode_getter() or "local")
        result["enabled"] = bool(self._enabled_getter())
        result["auto_download"] = bool(self._auto_download_getter())
        result["check_interval_minutes"] = self._safe_interval_minutes()
        result["rendering_busy"] = bool(self._rendering_busy_getter())
        result["storage_root"] = str(self.storage_root)
        return result

    def _ensure_download_space(self, manifest: Mapping[str, Any]) -> None:
        checked = validate_update_manifest(manifest)
        required = int(checked["size_bytes"]) + UPDATE_SPACE_HEADROOM_BYTES
        try:
            free = int(shutil.disk_usage(self.storage_root).free)
        except OSError as error:
            raise OSError("无法检查更新存储盘剩余空间。") from error
        if free < required:
            required_gib = required / (1024**3)
            free_gib = free / (1024**3)
            raise OSError(
                f"更新存储盘空间不足：至少需要 {required_gib:.1f} GB，"
                f"当前可用 {free_gib:.1f} GB。"
            )

    def _estimate_backup_bytes(self, package: Path) -> int:
        install_root = self.install_root().resolve()
        total = 0
        with zipfile.ZipFile(package) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                relative = _safe_relative_path(
                    entry.filename, label="压缩包文件"
                )
                if relative == UPDATE_PACKAGE_METADATA:
                    continue
                target = (install_root / Path(*PurePosixPath(relative).parts)).resolve(
                    strict=False
                )
                try:
                    target.relative_to(install_root)
                except ValueError:
                    continue
                try:
                    if target.is_file():
                        total += int(target.stat().st_size)
                except OSError:
                    continue
        return total

    def _apply_space_requirements(
        self,
        package: Path,
        manifest: Mapping[str, Any],
    ) -> dict[str, int]:
        checked = validate_update_manifest(manifest)
        metadata = inspect_update_package(
            package, expected_version=checked["version"]
        )
        compressed = int(checked["size_bytes"])
        uncompressed = int(metadata["uncompressed_size_bytes"])
        backup = self._estimate_backup_bytes(package)
        total = compressed + uncompressed + backup + UPDATE_SPACE_HEADROOM_BYTES
        package_in_storage = self._path_is_within(package, self.storage_root)
        required_additional = (
            uncompressed + backup + UPDATE_SPACE_HEADROOM_BYTES
            if package_in_storage
            else total
        )
        try:
            free = int(shutil.disk_usage(self.storage_root).free)
        except OSError as error:
            raise OSError("无法检查更新安装盘剩余空间。") from error
        if free < required_additional:
            required_gib = required_additional / (1024**3)
            free_gib = free / (1024**3)
            raise OSError(
                f"更新安装空间不足：解压、回滚和安全余量至少需要 "
                f"{required_gib:.1f} GB，当前可用 {free_gib:.1f} GB。"
            )
        return {
            "compressed_bytes": compressed,
            "uncompressed_bytes": uncompressed,
            "backup_bytes": backup,
            "headroom_bytes": UPDATE_SPACE_HEADROOM_BYTES,
            "total_peak_bytes": total,
            "required_additional_bytes": required_additional,
            "free_bytes": free,
        }

    def _safe_interval_minutes(self) -> float:
        try:
            value = float(self._interval_minutes_getter())
        except (TypeError, ValueError):
            return 1.0
        return max(1.0, min(1440.0, value))

    def start(self) -> None:
        if str(self._mode_getter()).casefold() != "client" or not self._enabled_getter():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._wake.set()
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._monitor_loop,
                name="storyforge-update-monitor",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def wake(self) -> None:
        self._wake.set()

    def _monitor_loop(self) -> None:
        # Check immediately, then use a dynamic interval so a settings change
        # does not require restarting the desktop application.
        while not self._stop.is_set():
            if self._enabled_getter() and str(self._mode_getter()).casefold() == "client":
                try:
                    self.check_now(auto_download=bool(self._auto_download_getter()))
                except (OSError, RuntimeError, ValueError):
                    pass
            self._wake.clear()
            self._wake.wait(self._safe_interval_minutes() * 60.0)

    def check_now(self, *, auto_download: bool | None = None) -> dict[str, Any]:
        if str(self._mode_getter()).casefold() != "client":
            raise RuntimeError("只有连接 Hub 的制作电脑需要检查软件更新。")
        client = self._client_getter()
        if client is None:
            raise RuntimeError("StoryForge Hub 当前未连接，无法检查更新。")
        if not self._operation_lock.acquire(blocking=False):
            return self.status()
        try:
            with self._lock:
                self._status.update(
                    {
                        "state": "checking",
                        "progress": 0.05,
                        "message": "正在检查更新…",
                        "error": "",
                    }
                )
            manifest = client.get_update_manifest()
            checked_at = utc_now()
            if manifest is None or not is_newer_version(
                manifest["version"], self.current_version
            ):
                with self._lock:
                    if self._pending_ready:
                        self._status.update(
                            {
                                "state": "scheduled",
                                "progress": 1.0,
                                "message": "已下载的更新仍安排在安全退出后安装。",
                                "checked_at": checked_at,
                                "error": "",
                            }
                        )
                    else:
                        self._manifest = None
                        self._status.update(
                            {
                                "available_version": "",
                                "state": "up_to_date",
                                "progress": 1.0,
                                "message": "当前已经是最新版本。",
                                "checked_at": checked_at,
                                "release_notes": "",
                                "published_at": "",
                                "error": "",
                            }
                        )
                return self.status()
            manifest = validate_update_manifest(manifest)
            with self._lock:
                self._manifest = manifest
                self._status.update(
                    {
                        "available_version": manifest["version"],
                        "state": "available",
                        "progress": 0.12,
                        "message": (
                            f"发现新版本 {manifest['version']}；下载后将替换已安排的 {self._pending_version}。"
                            if self._pending_ready
                            and self._pending_version != manifest["version"]
                            else f"发现新版本 {manifest['version']}。"
                        ),
                        "checked_at": checked_at,
                        "release_notes": manifest["release_notes"],
                        "published_at": manifest["published_at"],
                        "error": "",
                    }
                )
            should_download = (
                bool(self._auto_download_getter())
                if auto_download is None
                else bool(auto_download)
            )
            if should_download:
                return self._download_locked(client, manifest)
            return self.status()
        except (OSError, RuntimeError, ValueError) as error:
            with self._lock:
                self._status.update(
                    {
                        "state": "error",
                        "progress": 0.0,
                        "message": "检查更新失败，请稍后重试。",
                        "checked_at": utc_now(),
                        "error": str(error) or type(error).__name__,
                    }
                )
            raise
        finally:
            self._operation_lock.release()

    def download(self) -> dict[str, Any]:
        client = self._client_getter()
        if client is None:
            raise RuntimeError("StoryForge Hub 当前未连接，无法下载更新。")
        if not self._operation_lock.acquire(blocking=False):
            return self.status()
        try:
            with self._lock:
                manifest = dict(self._manifest) if self._manifest else None
            if manifest is None:
                remote = client.get_update_manifest()
                if remote is None or not is_newer_version(
                    remote["version"], self.current_version
                ):
                    raise RuntimeError("当前没有可下载的新版本。")
                manifest = validate_update_manifest(remote)
                with self._lock:
                    self._manifest = manifest
            return self._download_locked(client, manifest)
        finally:
            self._operation_lock.release()

    def _download_locked(
        self, client: Any, manifest: Mapping[str, Any]
    ) -> dict[str, Any]:
        checked = validate_update_manifest(manifest)
        self._ensure_download_space(checked)
        version_directory = (self.cache_root / checked["version"]).resolve()
        version_directory.relative_to(self.cache_root)
        version_directory.mkdir(parents=True, exist_ok=True)
        destination = (version_directory / checked["filename"]).resolve()
        destination.relative_to(version_directory)
        with self._lock:
            self._status.update(
                {
                    "state": "downloading",
                    "progress": 0.25,
                    "message": "正在安全下载更新包…",
                    "error": "",
                }
            )
        try:
            client.download_update_package(checked, destination=destination)
            if destination.stat().st_size != checked["size_bytes"]:
                raise ValueError("下载后的更新包大小不一致。")
            if file_sha256(destination) != checked["sha256"]:
                raise ValueError("下载后的更新包 SHA-256 校验失败。")
            inspect_update_package(destination, expected_version=checked["version"])
            space_requirements = self._apply_space_requirements(
                destination, checked
            )
        except BaseException:
            try:
                destination.unlink()
            except OSError:
                pass
            raise
        # Downloading must also schedule the verified release. Previously the
        # monitor downloaded a large package and then waited for an employee to
        # find a second "install on restart" action. That left workstations on
        # old render contracts even though automatic updates were enabled.
        # Scheduling is safe while rendering: launch_scheduled_update() already
        # refuses to replace files until the queue is idle and StoryForge exits.
        marker = {
            "schema_version": 1,
            "scheduled_at": utc_now(),
            "manifest": checked,
            "package_path": str(destination),
            "install_root": str(self.install_root()),
            "update_storage_root": str(self.storage_root),
            "space_requirements": space_requirements,
        }
        self._write_json_atomic(self.pending_path, marker)
        self._pending_ready = True
        self._pending_version = checked["version"]
        busy = bool(self._rendering_busy_getter())
        message = (
            "更新已下载；当前任务完成后，在下次安全退出 StoryForge 时自动安装。"
            if busy
            else "更新已下载并安排安装；下次安全退出 StoryForge 后会自动更新。"
        )
        with self._lock:
            self._manifest = checked
            self._status.update(
                {
                    "available_version": checked["version"],
                    "scheduled_version": checked["version"],
                    "state": "deferred" if busy else "scheduled",
                    "progress": 1.0,
                    "message": message,
                    "downloaded_at": utc_now(),
                    "package_path": str(destination),
                    "downloaded": True,
                    "apply_on_restart": True,
                    "restart_required": True,
                    "rendering_busy": busy,
                    "release_notes": checked["release_notes"],
                    "published_at": checked["published_at"],
                    "error": "",
                }
            )
        self._cleanup_update_storage()
        return self.status()

    def schedule_on_restart(self) -> dict[str, Any]:
        with self._lock:
            manifest = dict(self._manifest) if self._manifest else None
            package_path = str(self._status.get("package_path") or "")
        if manifest is None or not package_path:
            raise RuntimeError("请先下载并校验更新包。")
        package = Path(package_path).resolve(strict=True)
        if package.stat().st_size != manifest["size_bytes"]:
            raise ValueError("更新包大小与清单不一致，请重新下载。")
        if file_sha256(package) != manifest["sha256"]:
            raise ValueError("更新包校验失败，请重新下载。")
        space_requirements = self._apply_space_requirements(package, manifest)
        marker = {
            "schema_version": 1,
            "scheduled_at": utc_now(),
            "manifest": manifest,
            "package_path": str(package),
            "install_root": str(self.install_root()),
            "update_storage_root": str(self.storage_root),
            "space_requirements": space_requirements,
        }
        self._write_json_atomic(self.pending_path, marker)
        self._pending_ready = True
        self._pending_version = manifest["version"]
        busy = bool(self._rendering_busy_getter())
        with self._lock:
            self._status.update(
                {
                    "state": "deferred" if busy else "scheduled",
                    "progress": 1.0,
                    "message": (
                        "已安排更新；当前正在生成视频，将在后续安全退出时安装。"
                        if busy
                        else "已安排更新，将在安全退出 StoryForge 后安装并重新打开。"
                    ),
                    "apply_on_restart": True,
                    "scheduled_version": manifest["version"],
                    "restart_required": True,
                    "rendering_busy": busy,
                    "error": "",
                }
            )
        return self.status()

    def cancel_schedule(self) -> dict[str, Any]:
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            pass
        self._pending_ready = False
        self._pending_version = ""
        with self._lock:
            downloaded = bool(self._status.get("downloaded"))
            self._status.update(
                {
                    "state": "downloaded" if downloaded else "idle",
                    "progress": 1.0 if downloaded else 0.0,
                    "message": (
                        "已取消自动安装；更新包仍保留，可稍后重新安排。"
                        if downloaded
                        else "已取消更新安排。"
                    ),
                    "apply_on_restart": False,
                    "scheduled_version": "",
                    "error": "",
                }
            )
        return self.status()

    @staticmethod
    def install_root() -> Path:
        if bool(getattr(sys, "frozen", False)):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent.parent

    def launch_scheduled_update(self) -> bool:
        """Hand off a scheduled release only after rendering is completely idle."""

        if not self._pending_ready or not self.pending_path.is_file():
            return False
        if self._rendering_busy_getter():
            with self._lock:
                self._status.update(
                    {
                        "state": "deferred",
                        "message": "检测到视频仍在生成，本次退出不会覆盖程序文件。",
                        "rendering_busy": True,
                    }
                )
            return False
        with self._lock:
            manifest = dict(self._manifest) if self._manifest else None
            package_value = str(self._status.get("package_path") or "").strip()
        if manifest is None or not package_value:
            return False
        package = Path(package_value).resolve(strict=True)
        space_requirements = self._apply_space_requirements(package, manifest)
        try:
            marker = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise RuntimeError("待安装更新记录无法读取。") from error
        marker["update_storage_root"] = str(self.storage_root)
        marker["space_requirements"] = space_requirements
        self._write_json_atomic(self.pending_path, marker)
        self._write_windows_worker()
        self._launcher(self.worker_path, self.pending_path, os.getpid())
        with self._lock:
            self._status.update(
                {
                    "state": "applying_on_restart",
                    "message": "StoryForge 退出后将安装更新并重新打开。",
                    "rendering_busy": False,
                }
            )
        return True

    def _write_windows_worker(self) -> None:
        self.worker_path.parent.mkdir(parents=True, exist_ok=True)
        script = r'''param(
  [Parameter(Mandatory=$true)][string]$MarkerPath,
  [Parameter(Mandatory=$true)][int]$ParentPid
)
$ErrorActionPreference = 'Stop'
function Write-JsonUtf8NoBom([string]$Path, [object]$Value, [int]$Depth = 12) {
  $json = $Value | ConvertTo-Json -Depth $Depth
  [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding($false)))
}
$marker = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
$package = [IO.Path]::GetFullPath([string]$marker.package_path)
$installRoot = [IO.Path]::GetFullPath([string]$marker.install_root)
$storageRoot = [IO.Path]::GetDirectoryName($MarkerPath)
if ($null -ne $marker.update_storage_root -and -not [string]::IsNullOrWhiteSpace([string]$marker.update_storage_root)) {
  $storageRoot = [IO.Path]::GetFullPath([string]$marker.update_storage_root)
}
$resultPath = Join-Path ([IO.Path]::GetDirectoryName($MarkerPath)) 'last-update-result.json'
$expected = ([string]$marker.manifest.sha256).ToLowerInvariant()
$version = [string]$marker.manifest.version
$entrypoint = ([string]$marker.manifest.entrypoint).Replace('/', [IO.Path]::DirectorySeparatorChar)
try { Wait-Process -Id $ParentPid -Timeout 120 -ErrorAction SilentlyContinue } catch {}
if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) { throw 'StoryForge is still running; update deferred.' }
$installedEntryPath = [IO.Path]::GetFullPath((Join-Path $installRoot $entrypoint))
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
  try {
    if ($_.Id -ne $PID -and [string]::Equals($_.Path, $installedEntryPath, [StringComparison]::OrdinalIgnoreCase)) {
      Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
  } catch {}
}
Start-Sleep -Milliseconds 500
$actual = (Get-FileHash -LiteralPath $package -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw 'StoryForge update SHA-256 verification failed.' }
New-Item -ItemType Directory -Path $storageRoot -Force | Out-Null
$requiredAdditional = 0
if ($null -ne $marker.space_requirements -and $null -ne $marker.space_requirements.required_additional_bytes) {
  $requiredAdditional = [int64]$marker.space_requirements.required_additional_bytes
}
if ($requiredAdditional -gt 0) {
  $storageDrive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($storageRoot))
  if ($storageDrive.AvailableFreeSpace -lt $requiredAdditional) { throw 'Insufficient disk space to stage and roll back the StoryForge update.' }
}
$workRoot = Join-Path $storageRoot ('apply-' + [guid]::NewGuid().ToString('N'))
$stage = Join-Path $workRoot 'stage'
$backup = Join-Path $workRoot 'backup'
New-Item -ItemType Directory -Path $stage,$backup -Force | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($package)
try {
  $stagePrefix = $stage.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
  foreach ($entry in $zip.Entries) {
    $relative = $entry.FullName.Replace('/', [IO.Path]::DirectorySeparatorChar)
    $target = [IO.Path]::GetFullPath((Join-Path $stage $relative))
    if (-not $target.StartsWith($stagePrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe update path.' }
    if ([string]::IsNullOrEmpty($entry.Name)) { New-Item -ItemType Directory -Path $target -Force | Out-Null; continue }
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($target)) -Force | Out-Null
    [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
  }
} finally { $zip.Dispose() }
$stageEntryPath = [IO.Path]::GetFullPath((Join-Path $stage $entrypoint))
if (-not (Test-Path -LiteralPath $stageEntryPath -PathType Leaf)) { throw 'Updated StoryForge entrypoint is missing from the staged package.' }
$copied = New-Object System.Collections.Generic.List[string]
$created = New-Object System.Collections.Generic.List[string]
$launched = $null
try {
  $stageFiles = Get-ChildItem -LiteralPath $stage -File -Recurse
  foreach ($source in $stageFiles) {
    $relative = $source.FullName.Substring($stage.Length).TrimStart([IO.Path]::DirectorySeparatorChar)
    if ($relative.Replace('\','/') -eq 'storyforge-update.json') { continue }
    $target = [IO.Path]::GetFullPath((Join-Path $installRoot $relative))
    $installPrefix = $installRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe install path.' }
    if (Test-Path -LiteralPath $target -PathType Leaf) {
      $backupTarget = Join-Path $backup $relative
      New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($backupTarget)) -Force | Out-Null
      Copy-Item -LiteralPath $target -Destination $backupTarget -Force
    } else {
      $created.Add($relative)
    }
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($target)) -Force | Out-Null
    $copied.Add($relative)
    Copy-Item -LiteralPath $source.FullName -Destination $target -Force
  }
  $entryPath = [IO.Path]::GetFullPath((Join-Path $installRoot $entrypoint))
  if (-not (Test-Path -LiteralPath $entryPath -PathType Leaf)) { throw 'Updated StoryForge entrypoint is missing.' }
  $marker | Add-Member -NotePropertyName installing -NotePropertyValue $true -Force
  $marker | Add-Member -NotePropertyName installing_at -NotePropertyValue ([DateTime]::UtcNow.ToString('o')) -Force
  $marker | Add-Member -NotePropertyName apply_work_root -NotePropertyValue $workRoot -Force
  Write-JsonUtf8NoBom -Path $MarkerPath -Value $marker
  if ([IO.Path]::GetExtension($entryPath).ToLowerInvariant() -eq '.py') {
    $pythonw = Join-Path $installRoot '.build-venv\Scripts\pythonw.exe'
    $python = Join-Path $installRoot '.build-venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $pythonw -PathType Leaf) { $launched = Start-Process -FilePath $pythonw -ArgumentList @($entryPath) -WorkingDirectory $installRoot -PassThru }
    elseif (Test-Path -LiteralPath $python -PathType Leaf) { $launched = Start-Process -FilePath $python -ArgumentList @($entryPath) -WorkingDirectory $installRoot -PassThru }
    else { throw 'Updated StoryForge Python runtime is missing.' }
  } else {
    $launched = Start-Process -FilePath $entryPath -WorkingDirectory $installRoot -PassThru
  }
  Start-Sleep -Seconds 4
  if ($null -eq $launched -or $launched.HasExited) { throw 'Updated StoryForge failed its startup health check.' }
} catch {
  $failureMessage = [string]$_.Exception.Message
  foreach ($relative in $copied) {
    $backupTarget = Join-Path $backup $relative
    $target = Join-Path $installRoot $relative
    if (Test-Path -LiteralPath $backupTarget -PathType Leaf) { Copy-Item -LiteralPath $backupTarget -Destination $target -Force }
  }
  foreach ($relative in $created) {
    $target = Join-Path $installRoot $relative
    if (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force }
  }
  try {
    $marker | Add-Member -NotePropertyName installing -NotePropertyValue $false -Force
    $marker | Add-Member -NotePropertyName installing_at -NotePropertyValue '' -Force
    $marker | Add-Member -NotePropertyName apply_work_root -NotePropertyValue '' -Force
    $marker | Add-Member -NotePropertyName last_error -NotePropertyValue $failureMessage -Force
    Write-JsonUtf8NoBom -Path $MarkerPath -Value $marker
  } catch {}
  try {
    Write-JsonUtf8NoBom -Path $resultPath -Value @{ status='failed'; version=$version; failed_at=[DateTime]::UtcNow.ToString('o'); error=$failureMessage; rollback_available=$false }
  } catch {}
  Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
  throw
}
Write-JsonUtf8NoBom -Path $resultPath -Value @{ status='installed'; version=$version; installed_at=[DateTime]::UtcNow.ToString('o'); backup_path=$backup; rollback_available=$true; health='process_alive' }
Remove-Item -LiteralPath $MarkerPath -Force
Remove-Item -LiteralPath $package -Force -ErrorAction SilentlyContinue
try {
  $packageDirectory = [IO.Path]::GetDirectoryName($package)
  if ($null -eq (Get-ChildItem -LiteralPath $packageDirectory -Force -ErrorAction SilentlyContinue | Select-Object -First 1)) {
    Remove-Item -LiteralPath $packageDirectory -Force -ErrorAction SilentlyContinue
  }
} catch {}
Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
'''
        self.worker_path.write_text(script, encoding="utf-8", newline="\n")

    @staticmethod
    def _launch_windows_worker(worker_path: Path, marker_path: Path, parent_pid: int) -> None:
        if os.name != "nt":
            raise RuntimeError("自动应用更新当前仅支持 Windows。")
        creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(worker_path),
                "-MarkerPath",
                str(marker_path),
                "-ParentPid",
                str(int(parent_pid)),
            ],
            cwd=str(worker_path.parent),
            close_fds=True,
            creationflags=creation_flags,
        )
