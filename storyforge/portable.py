from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.request import urlopen


PORTABLE_DATA_DIRECTORY = "StoryForgeData"
MIGRATION_MARKER = ".legacy-appdata-migration-v1.json"
MIGRATION_LOCK = ".legacy-appdata-migration.lock"
_FROZEN_HUB_DATA_ROOT_ENV = "STORYFORGE_FROZEN_HUB_DATA_ROOT"
_DEPLOYMENT_ROLE_ENV = "STORYFORGE_DEPLOYMENT_ROLE"

# These folders contain disposable process state. Copying an old update marker
# can reinstall the release that just performed the migration, while copying a
# live render workspace can produce a torn FFmpeg job. The legacy copy remains
# untouched so an administrator can still inspect it after the new Worker is
# accepted.
_TRANSIENT_LEGACY_ROOTS = frozenset(
    {
        "cache",
        "render-work",
        "runtime-temp",
        "updates",
    }
)
_TRANSIENT_LEGACY_FILES = frozenset({"local-worker.json"})
_WORKER_PORTS = tuple(range(18765, 18771))
_REGENERABLE_RENDER_SUFFIXES = frozenset(
    {
        ".aac",
        ".avi",
        ".bmp",
        ".flac",
        ".jpeg",
        ".jpg",
        ".m2ts",
        ".m4a",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".opus",
        ".part",
        ".png",
        ".tmp",
        ".ts",
        ".wav",
        ".webm",
        ".webp",
    }
)


def _legacy_roaming_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    return root / "StoryForgeStudio"


def _legacy_local_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "StoryForgeStudio"


def portable_install_root(executable: str | Path | None = None) -> Path:
    value = Path(executable or sys.executable).expanduser().resolve(strict=False)
    return value.parent


def portable_data_dir(executable: str | Path | None = None) -> Path:
    return portable_install_root(executable) / PORTABLE_DATA_DIRECTORY


def _is_frozen(value: bool | None = None) -> bool:
    return bool(getattr(sys, "frozen", False)) if value is None else bool(value)


def _has_employee_connection_profile(install_root: Path) -> bool:
    return (install_root / "storyforge-connection.json").is_file()


def _has_authorized_frozen_hub_data_root() -> bool:
    """Return whether run.py authorized the exact explicit Hub DataRoot."""

    configured = str(os.environ.get("STORYFORGE_DATA_DIR") or "").strip()
    authorized = str(os.environ.get(_FROZEN_HUB_DATA_ROOT_ENV) or "").strip()
    role = str(os.environ.get(_DEPLOYMENT_ROLE_ENV) or "").strip().casefold()
    if role != "hub" or not configured or not authorized:
        return False
    return (
        Path(configured).expanduser().resolve(strict=False)
        == Path(authorized).expanduser().resolve(strict=False)
    )


def _settings_hub_mode(path: Path) -> str:
    """Read only enough settings state to classify its Hub role.

    Very old settings files can contain text written by a broken Windows code
    page and are not always valid JSON. A narrow textual fallback therefore
    follows the normal JSON read.
    """

    try:
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""
    try:
        raw = json.loads(content)
        settings = raw.get("settings") if isinstance(raw, dict) else None
        hub = settings.get("hub") if isinstance(settings, dict) else None
        mode = (
            str(hub.get("mode") or "").strip().casefold()
            if isinstance(hub, dict)
            else ""
        )
        return mode if mode in {"client", "host", "local"} else ""
    except (AttributeError, json.JSONDecodeError):
        compact = "".join(content.split()).casefold()
        hub_index = compact.find('"hub":{')
        if hub_index < 0:
            return ""
        hub_section = compact[hub_index : hub_index + 3000]
        for mode in ("host", "client", "local"):
            if f'"mode":"{mode}"' in hub_section:
                return mode
        return ""


def _legacy_mode_is_client() -> bool:
    """Return whether the old roaming profile is an enrolled workstation."""

    return (
        _settings_hub_mode(_legacy_roaming_data_dir() / "settings.json")
        == "client"
    )


def should_use_portable_data(
    argv: Sequence[str] | None = None,
    *,
    frozen: bool | None = None,
    executable: str | Path | None = None,
) -> bool:
    """Return whether this process is an employee-side portable runtime.

    An explicit ``STORYFORGE_DATA_DIR`` remains authoritative in every mode.
    Source development and the Hub's ``--web`` process retain their historical
    AppData location. Frozen employee builds are recognized by their bundled
    connection profile, local-worker argument, or an existing client profile.
    """

    if str(os.environ.get("STORYFORGE_DATA_DIR") or "").strip():
        return True
    return _is_employee_portable_runtime(
        argv, frozen=frozen, executable=executable
    )


def _is_employee_portable_runtime(
    argv: Sequence[str] | None = None,
    *,
    frozen: bool | None = None,
    executable: str | Path | None = None,
) -> bool:
    if not _is_frozen(frozen):
        return False
    arguments = tuple(str(item).casefold() for item in (argv or sys.argv[1:]))
    if "--web" in arguments or "--web-only" in arguments:
        return False
    if "--local-worker" in arguments:
        return True
    # The same frozen bundle contains an employee connection profile even when
    # installed as the Hub. run.py records this process-local authorization
    # before portable setup; only an exact match lets the ordinary Hub desktop
    # avoid employee migration and portable-mode restrictions.
    if _has_authorized_frozen_hub_data_root():
        return False
    root = portable_install_root(executable)
    return _has_employee_connection_profile(root) or _legacy_mode_is_client()


def _active_legacy_worker() -> bool:
    """Avoid reading mutable AppData while an older queue owner is alive."""

    for port in _WORKER_PORTS:
        try:
            with urlopen(
                f"http://127.0.0.1:{port}/worker/api/health", timeout=0.12
            ) as response:
                payload = json.loads(response.read(64 * 1024).decode("utf-8"))
            data = payload.get("data") if isinstance(payload, dict) else None
            if (
                isinstance(payload, dict)
                and payload.get("ok") is True
                and isinstance(data, dict)
                and data.get("service") == "storyforge-local-worker"
                and data.get("worker_role") == "production-workstation"
            ):
                return True
        except (OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return False


def _acquire_migration_lock(root: Path) -> int | None:
    lock_path = root / MIGRATION_LOCK
    try:
        return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # Never guess that a slow migration is stale and start a second copy.
        # A crashed owner causes a clear timeout and leaves the lock/legacy
        # files for administrator inspection instead of risking corruption.
        return None


def _copy_sqlite_database(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.migration-{os.getpid()}.tmp")
    destination.parent.mkdir(parents=True, exist_ok=True)
    reader: sqlite3.Connection | None = None
    writer: sqlite3.Connection | None = None
    try:
        reader = sqlite3.connect(
            f"file:{source.as_posix()}?mode=ro", uri=True, timeout=5
        )
        writer = sqlite3.connect(temporary, timeout=5)
        reader.backup(writer, pages=1024, sleep=0.01)
        writer.commit()
        writer.close()
        writer = None
        reader.close()
        reader = None
        os.replace(temporary, destination)
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()
        try:
            temporary.unlink()
        except OSError:
            pass


def _copy_missing_file(source: Path, destination: Path) -> bool:
    if destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.casefold() in {".sqlite", ".sqlite3", ".db"}:
        _copy_sqlite_database(source, destination)
    else:
        temporary = destination.with_name(
            f".{destination.name}.migration-{os.getpid()}.tmp"
        )
        try:
            shutil.copy2(source, temporary)
            try:
                os.link(temporary, destination)
            except OSError:
                # ``replace`` is safe because this branch is reached only when
                # the destination was absent. A competing migration uses the
                # same outer lock and cannot enter concurrently.
                os.replace(temporary, destination)
            else:
                temporary.unlink()
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass
    return True


def _iter_legacy_files(source_root: Path) -> Iterable[tuple[Path, Path]]:
    try:
        entries = tuple(source_root.rglob("*"))
    except OSError:
        return
    for source in entries:
        try:
            relative = source.relative_to(source_root)
            if not relative.parts:
                continue
            if relative.parts[0].casefold() in _TRANSIENT_LEGACY_ROOTS:
                continue
            if len(relative.parts) == 1 and relative.name.casefold() in _TRANSIENT_LEGACY_FILES:
                continue
            if source.is_symlink() or not source.is_file():
                continue
        except OSError:
            continue
        # SQLite WAL/SHM files are transaction internals; the backup API above
        # produces a consistent standalone database without them.
        if source.name.casefold().endswith((".sqlite-shm", ".sqlite-wal", ".sqlite3-shm", ".sqlite3-wal")):
            continue
        yield source, relative


def _read_migration_marker(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return dict(raw) if isinstance(raw, dict) else None


def _write_migration_marker(path: Path, result: dict[str, Any]) -> None:
    """Atomically replace the migration marker while holding its outer lock."""

    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _legacy_cleanup_finished(recorded: dict[str, Any]) -> bool:
    cleanup = recorded.get("legacy_cleanup")
    return bool(
        isinstance(cleanup, dict)
        and cleanup.get("completed") is True
        and not cleanup.get("errors")
        and recorded.get("cleanup_pending") is not True
    )


def _path_is_within(path: Path, root: Path) -> bool:
    """Return true only when resolving ``path`` remains below ``root``."""

    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def _tree_regular_file_bytes(root: Path) -> int:
    """Best-effort size without following symlinks or reparse targets."""

    total = 0
    try:
        candidates = root.rglob("*")
        for candidate in candidates:
            try:
                if _is_link_or_junction(candidate) or not candidate.is_file():
                    continue
                total += max(0, int(candidate.stat().st_size))
            except OSError:
                continue
    except OSError:
        pass
    return total


def _cleanup_legacy_employee_data(
    source_roots: Sequence[Path],
) -> dict[str, Any]:
    """Remove only proven-regenerable legacy files after a safe migration.

    Settings, catalog databases, logs, command/manifests and unknown files are
    intentionally retained.  The function never follows a symlink outside the
    old ``StoryForgeStudio`` root and cleanup errors are diagnostic only.
    """

    result: dict[str, Any] = {
        "attempted": False,
        "completed": False,
        "skipped_reason": "",
        "released_bytes": 0,
        "removed_file_count": 0,
        "removed_roots": [],
        "errors": [],
    }
    if _active_legacy_worker():
        result["skipped_reason"] = "legacy_worker_active"
        return result

    result["attempted"] = True
    released_bytes = 0
    removed_file_count = 0
    removed_roots: list[str] = []
    errors: list[str] = []
    seen_roots: set[str] = set()
    allowed_roots = {
        os.path.normcase(str(path.resolve(strict=False)))
        for path in (_legacy_roaming_data_dir(), _legacy_local_data_dir())
    }

    for source_root in source_roots:
        try:
            source_path = Path(source_root)
            if _is_link_or_junction(source_path):
                errors.append(f"{source_path}: refused linked legacy root")
                continue
            legacy = source_path.resolve(strict=False)
        except OSError as error:
            errors.append(f"legacy root: {type(error).__name__}: {error}")
            continue
        identity = os.path.normcase(str(legacy))
        if identity not in allowed_roots:
            errors.append(f"{legacy}: refused unexpected legacy root")
            continue
        if identity in seen_roots or not legacy.is_dir():
            continue
        seen_roots.add(identity)

        # These directories contain only caches, staged updates or temporary
        # files.  They may be removed as a unit, but only when their resolved
        # target is a normal direct child of this exact legacy root.
        for name in ("updates", "cache", "runtime-temp"):
            candidate = legacy / name
            try:
                if not candidate.exists():
                    continue
                if (
                    _is_link_or_junction(candidate)
                    or candidate.parent.resolve(strict=False) != legacy
                    or not _path_is_within(candidate, legacy)
                ):
                    errors.append(f"{candidate}: refused path outside legacy root")
                    continue
                before = _tree_regular_file_bytes(candidate)
                if candidate.is_dir():
                    shutil.rmtree(candidate)
                else:
                    candidate.unlink()
                released_bytes += before
                removed_roots.append(str(candidate))
            except OSError as error:
                errors.append(f"{candidate}: {type(error).__name__}: {error}")

        # render-work also holds the command, manifests and failure logs that
        # an administrator needs for diagnosis. Delete only known media/temp
        # products, including media beneath .previews, and leave everything
        # else untouched.
        render_root = legacy / "render-work"
        try:
            if (
                not render_root.is_dir()
                or _is_link_or_junction(render_root)
                or not _path_is_within(render_root, legacy)
            ):
                continue
            candidates = sorted(
                render_root.rglob("*"),
                key=lambda item: len(item.parts),
                reverse=True,
            )
        except OSError as error:
            errors.append(f"{render_root}: {type(error).__name__}: {error}")
            continue
        for candidate in candidates:
            try:
                if _is_link_or_junction(candidate) or not _path_is_within(candidate, render_root):
                    continue
                if candidate.is_file():
                    if candidate.suffix.casefold() not in _REGENERABLE_RENDER_SUFFIXES:
                        continue
                    size = max(0, int(candidate.stat().st_size))
                    candidate.unlink()
                    released_bytes += size
                    removed_file_count += 1
                elif candidate.is_dir() and candidate.name.casefold() == ".previews":
                    # Only remove an empty preview directory. A retained .log
                    # or other unknown file prevents rmdir and remains intact.
                    try:
                        candidate.rmdir()
                    except OSError:
                        pass
            except OSError as error:
                errors.append(f"{candidate}: {type(error).__name__}: {error}")

    result.update(
        {
            "completed": not errors,
            "released_bytes": released_bytes,
            "removed_file_count": removed_file_count,
            "removed_roots": removed_roots,
            "errors": errors,
        }
    )
    return result


def migrate_legacy_employee_data(
    root: Path, *, wait_seconds: float = 120.0
) -> dict[str, Any]:
    """Copy durable legacy state once and retry only its safe cleanup phase."""

    root = Path(root).resolve(strict=False)
    marker = root / MIGRATION_MARKER
    recorded = _read_migration_marker(marker)
    # Copy errors are terminal and must keep their original failure semantics.
    # A completed copy with unfinished cleanup is different: its durable data
    # is already safe, so a later launch may retry only the idempotent cleanup.
    if recorded is not None and recorded.get("errors"):
        return recorded
    if (
        recorded is not None
        and recorded.get("completed") is True
        and _legacy_cleanup_finished(recorded)
    ):
        return recorded
    if not (recorded is not None and recorded.get("completed") is True) and _active_legacy_worker():
        return {
            "completed": False,
            "deferred": True,
            "reason": "legacy_worker_active",
        }

    lock_handle = _acquire_migration_lock(root)
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while lock_handle is None:
        # Another new Worker owns the migration. Never start a queue against a
        # half-copied database: wait for its durable success/failure marker.
        recorded = _read_migration_marker(marker)
        if recorded is not None and recorded.get("errors"):
            return recorded
        if (
            recorded is not None
            and recorded.get("completed") is True
            and _legacy_cleanup_finished(recorded)
        ):
            return recorded
        if time.monotonic() >= deadline:
            return {
                "completed": False,
                "deferred": False,
                "reason": "migration_lock_timeout",
                "errors": [
                    "timed out waiting for another StoryForge process to finish "
                    "the legacy AppData migration"
                ],
            }
        time.sleep(0.1)
        lock_handle = _acquire_migration_lock(root)
        if lock_handle is not None:
            # The owner writes its marker before releasing the lock. We may
            # win the next O_EXCL race immediately afterwards; re-read the
            # marker before doing the same copy a second time.
            recorded = _read_migration_marker(marker)
            if recorded is not None and recorded.get("errors"):
                os.close(lock_handle)
                lock_handle = None
                try:
                    (root / MIGRATION_LOCK).unlink()
                except OSError:
                    pass
                return recorded
            if (
                recorded is not None
                and recorded.get("completed") is True
                and _legacy_cleanup_finished(recorded)
            ):
                os.close(lock_handle)
                lock_handle = None
                try:
                    (root / MIGRATION_LOCK).unlink()
                except OSError:
                    pass
                return recorded
    lock_path = root / MIGRATION_LOCK
    copied: list[str] = []
    errors: list[str] = []
    sources: list[str] = []
    try:
        os.write(lock_handle, str(os.getpid()).encode("ascii"))
        # Re-read after taking the lock. Another process may have completed the
        # copy and released the lock between our previous marker read and this
        # O_EXCL win. Never repeat the copy in that case.
        recorded = _read_migration_marker(marker)
        if recorded is not None and recorded.get("errors"):
            return recorded
        if recorded is not None and recorded.get("completed") is True:
            if _legacy_cleanup_finished(recorded):
                return recorded
            raw_sources = recorded.get("sources")
            retry_sources = tuple(
                Path(item)
                for item in (raw_sources if isinstance(raw_sources, list) else ())
                if isinstance(item, str) and item.strip()
            )
            cleanup = _cleanup_legacy_employee_data(retry_sources)
            result = dict(recorded)
            result["legacy_cleanup"] = cleanup
            result["cleanup_pending"] = not bool(cleanup.get("completed"))
            result["cleanup_retried_at"] = datetime.now(UTC).isoformat()
            _write_migration_marker(marker, result)
            return result

        target_resolved = root.resolve(strict=False)
        legacy_roots: list[Path] = []
        for legacy in (_legacy_roaming_data_dir(), _legacy_local_data_dir()):
            try:
                legacy_resolved = legacy.resolve(strict=False)
            except OSError:
                continue
            if legacy_resolved == target_resolved or not legacy_resolved.is_dir():
                continue
            legacy_roots.append(legacy_resolved)

        # A packaged employee runtime must never clone a Hub installation into
        # its EXE-adjacent data root. Roaming and Local AppData are two halves
        # of one legacy installation, so classify and migrate them as a group.
        # A catalog is copied only when settings prove the old role was client
        # or standalone local. Host, missing, damaged and unknown roles all
        # fail closed and preserve the complete group for an administrator.
        legacy_modes = {
            str(legacy): _settings_hub_mode(legacy / "settings.json")
            for legacy in legacy_roots
        }
        legacy_host_detected = "host" in legacy_modes.values()
        legacy_catalog_detected = any(
            (legacy / "storyforge-catalog.sqlite3").is_file()
            for legacy in legacy_roots
        )
        proven_employee_role = any(
            mode in {"client", "local"} for mode in legacy_modes.values()
        )
        legacy_skip_reason = ""
        if legacy_host_detected:
            legacy_skip_reason = "host_role"
        elif legacy_catalog_detected and not proven_employee_role:
            legacy_skip_reason = "unclassified_catalog_role"
        skipped_legacy_roots = (
            [str(legacy) for legacy in legacy_roots]
            if legacy_skip_reason
            else []
        )
        skipped_legacy_host_roots = (
            [str(legacy) for legacy in legacy_roots]
            if legacy_host_detected
            else []
        )
        for legacy_resolved in (() if legacy_skip_reason else legacy_roots):
            sources.append(str(legacy_resolved))
            for source, relative in _iter_legacy_files(legacy_resolved):
                destination = root / relative
                try:
                    if _copy_missing_file(source, destination):
                        copied.append(relative.as_posix())
                except (OSError, sqlite3.Error) as error:
                    errors.append(f"{relative.as_posix()}: {type(error).__name__}: {error}")
        result: dict[str, Any] = {
            "schema_version": 1,
            "completed": not errors,
            "migrated_at": datetime.now(UTC).isoformat(),
            "sources": sources,
            "copied_file_count": len(copied),
            "copied_files": sorted(set(copied), key=str.casefold),
            "skipped_transient_roots": sorted(_TRANSIENT_LEGACY_ROOTS),
            "legacy_skip_reason": legacy_skip_reason,
            "skipped_legacy_roots": skipped_legacy_roots,
            "skipped_legacy_host_roots": skipped_legacy_host_roots,
            "legacy_preserved": True,
            "errors": errors,
        }
        if not errors:
            cleanup = _cleanup_legacy_employee_data(
                tuple(Path(item) for item in sources)
            )
            result["legacy_cleanup"] = cleanup
            result["cleanup_pending"] = not bool(cleanup.get("completed"))
        _write_migration_marker(marker, result)
        return result
    finally:
        os.close(lock_handle)
        try:
            lock_path.unlink()
        except OSError:
            pass


def _assert_writable_directory(root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=".write-test-", dir=root)
        os.close(handle)
        Path(name).unlink()
    except OSError as error:
        raise RuntimeError(
            "StoryForge 所在文件夹不可写。请把完整的 StoryForge 文件夹"
            "移到可写的 D 盘或 E 盘普通目录后重新打开，不要放在 Program Files。"
            f" 数据目录：{root}；系统错误：{error}"
        ) from error


def _assert_ascii_employee_install_root(
    *, executable: str | Path | None = None
) -> None:
    """Reject Windows paths that eSpeak cannot load reliably.

    The employee build keeps every mutable file below ``StoryForgeData`` next
    to the executable.  Falling back to an ASCII cache at a drive root would
    violate that contract and make later cleanup/diagnostics incomplete, so a
    non-ASCII install is rejected before ``StoryForgeData`` is created.
    """

    install_root = portable_install_root(executable)
    if str(install_root).isascii():
        return
    raise RuntimeError(
        "StoryForge 员工版所在路径包含中文或其他非 ASCII 字符，配音组件无法稳定加载。"
        "请把完整的 StoryForge 文件夹移动到例如 D:\\StoryForge 后重新打开；"
        "路径不要包含中文或特殊字符。"
        f" 当前路径：{install_root}"
    )


def configure_runtime_environment(
    argv: Sequence[str] | None = None,
    *,
    frozen: bool | None = None,
    executable: str | Path | None = None,
) -> Path | None:
    """Route a frozen employee runtime's mutable files beside the EXE."""

    if not should_use_portable_data(argv, frozen=frozen, executable=executable):
        return None
    portable_employee = _is_employee_portable_runtime(
        argv, frozen=frozen, executable=executable
    )
    if portable_employee:
        # This check deliberately precedes creation of StoryForgeData and all
        # cache/temp directories.  A failed employee launch must leave no
        # hidden fallback state elsewhere on the computer.
        _assert_ascii_employee_install_root(executable=executable)
    configured = str(os.environ.get("STORYFORGE_DATA_DIR") or "").strip()
    root = (
        Path(configured).expanduser().resolve(strict=False)
        if configured
        else portable_data_dir(executable)
    )
    # Set this before probing writability so an early startup failure is also
    # reported in the employee-selected StoryForge folder when possible.
    os.environ["STORYFORGE_DATA_DIR"] = str(root)
    if portable_employee:
        os.environ["STORYFORGE_PORTABLE_MODE"] = "1"
    else:
        os.environ.pop("STORYFORGE_PORTABLE_MODE", None)
    _assert_writable_directory(root)

    paths = {
        "logs": root / "logs",
        "runtime_temp": root / "runtime-temp",
        "cache": root / "cache",
        "tts": root / "cache" / "tts",
        "huggingface": root / "cache" / "huggingface",
        "huggingface_datasets": root / "cache" / "huggingface" / "datasets",
        "torch": root / "cache" / "torch",
        "torchinductor": root / "cache" / "torchinductor",
        "triton": root / "cache" / "triton",
        "numba": root / "cache" / "numba",
        "matplotlib": root / "cache" / "matplotlib",
        "pip": root / "cache" / "pip",
        "uv": root / "cache" / "uv",
        "espeak": root / "cache" / "espeak",
        "webview": root / "webview",
        "python": root / "cache" / "python",
        "data": root / "data",
        "config": root / "config",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)

    os.environ["STORYFORGE_TTS_CACHE_DIR"] = str(paths["tts"])
    os.environ["STORYFORGE_MEDIA_INDEX_PATH"] = str(
        paths["cache"] / "media-index.sqlite3"
    )
    os.environ["STORYFORGE_ESPEAK_CACHE"] = str(paths["espeak"])
    os.environ["STORYFORGE_WEBVIEW_DATA_DIR"] = str(paths["webview"])
    if not str(os.environ.get("WEBVIEW2_USER_DATA_FOLDER") or "").strip():
        os.environ["WEBVIEW2_USER_DATA_FOLDER"] = str(paths["webview"])
    os.environ["HF_HOME"] = str(paths["huggingface"])
    os.environ["HF_HUB_CACHE"] = str(paths["huggingface"] / "hub")
    os.environ["HF_DATASETS_CACHE"] = str(paths["huggingface_datasets"])
    os.environ["TRANSFORMERS_CACHE"] = str(paths["huggingface"] / "transformers")
    os.environ["TORCH_HOME"] = str(paths["torch"])
    os.environ["TORCHINDUCTOR_CACHE_DIR"] = str(paths["torchinductor"])
    os.environ["TRITON_CACHE_DIR"] = str(paths["triton"])
    os.environ["NUMBA_CACHE_DIR"] = str(paths["numba"])
    os.environ["MPLCONFIGDIR"] = str(paths["matplotlib"])
    os.environ["PIP_CACHE_DIR"] = str(paths["pip"])
    os.environ["UV_CACHE_DIR"] = str(paths["uv"])
    os.environ["PYTHONPYCACHEPREFIX"] = str(paths["python"])
    os.environ["XDG_CACHE_HOME"] = str(paths["cache"])
    os.environ["XDG_DATA_HOME"] = str(paths["data"])
    os.environ["XDG_CONFIG_HOME"] = str(paths["config"])
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(paths["runtime_temp"])
    # Some standard-library users cache gettempdir() before the environment is
    # configured. Setting the module value makes later NamedTemporaryFile and
    # TemporaryDirectory calls deterministic in this process.
    tempfile.tempdir = str(paths["runtime_temp"])

    if portable_employee and not configured:
        migration = migrate_legacy_employee_data(root)
        errors = migration.get("errors")
        if errors:
            detail = "; ".join(str(item) for item in errors[:3])
            raise RuntimeError(
                "StoryForge 旧数据迁移未完成，为避免两个队列使用不完整数据，"
                f"本次已停止启动。请把 {root / MIGRATION_MARKER} 发给管理员。"
                f" 详情：{detail}"
            )
    return root


def ensure_deferred_migration_complete() -> None:
    """Close the stop-between-probe race before a new queue is constructed."""

    if os.environ.get("STORYFORGE_PORTABLE_MODE") != "1":
        return
    configured = str(os.environ.get("STORYFORGE_DATA_DIR") or "").strip()
    if not configured:
        raise RuntimeError("portable StoryForge data directory is not configured")
    root = Path(configured).expanduser().resolve(strict=False)
    migration = migrate_legacy_employee_data(root)
    errors = migration.get("errors")
    if errors:
        detail = "; ".join(str(item) for item in errors[:3])
        raise RuntimeError(
            f"StoryForge 旧数据迁移未完成：{detail}。请把 "
            f"{root / MIGRATION_MARKER} 发给管理员。"
        )
    if migration.get("deferred"):
        raise RuntimeError(
            "检测到另一个 StoryForge 本机制作服务正在运行，"
            "本次不会启动第二个队列。"
        )


def configured_webview_storage_path() -> str | None:
    value = str(os.environ.get("STORYFORGE_WEBVIEW_DATA_DIR") or "").strip()
    return value or None


__all__ = [
    "MIGRATION_MARKER",
    "PORTABLE_DATA_DIRECTORY",
    "configure_runtime_environment",
    "configured_webview_storage_path",
    "ensure_deferred_migration_complete",
    "migrate_legacy_employee_data",
    "portable_data_dir",
    "portable_install_root",
    "should_use_portable_data",
]
