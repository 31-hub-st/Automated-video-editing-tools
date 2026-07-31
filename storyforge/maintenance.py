from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .providers.tts import prune_tts_cache


_MEBIBYTE = 1024**2
VOICE_PREVIEW_CACHE_MAX_BYTES = 256 * _MEBIBYTE
VOICE_PREVIEW_CACHE_MAX_AGE_DAYS = 14.0

_STARTUP_MAINTENANCE_LOCK = threading.RLock()
_MAINTAINED_DATA_DIRS: set[str] = set()
_TTS_CACHE_MAINTAINED = False


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _remove_tree(path: Path) -> tuple[int, int]:
    """Best-effort removal with useful, non-fatal maintenance totals."""

    files = 0
    bytes_removed = 0
    try:
        candidates = tuple(item for item in path.rglob("*") if item.is_file())
    except OSError:
        candidates = ()
    for item in candidates:
        files += 1
        bytes_removed += _file_size(item)
    try:
        shutil.rmtree(path)
    except (FileNotFoundError, OSError):
        return 0, 0
    return files, bytes_removed


def prune_render_work_residuals(work_root: str | Path) -> dict[str, int]:
    """Remove only reproducible, crash-prone media from private job folders.

    Successful jobs already run the same WAV cleanup in ``PipelineRunner``.
    Files found here are therefore normally leftovers from a killed process or
    an older preview build.  Manifests, commands and error logs are retained so
    production history and diagnostics remain useful after maintenance.
    """

    try:
        root = Path(work_root).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return {"files": 0, "bytes": 0}
    if not root.is_dir():
        return {"files": 0, "bytes": 0}

    files_removed = 0
    bytes_removed = 0
    try:
        work_dirs = tuple(
            path for path in root.rglob(".work") if path.is_dir()
        )
        preview_dirs = tuple(
            path for path in root.rglob(".previews") if path.is_dir()
        )
    except OSError:
        return {"files": 0, "bytes": 0}

    for work_dir in work_dirs:
        voice_dir = work_dir / "voice"
        removed_files, removed_bytes = _remove_tree(voice_dir)
        files_removed += removed_files
        bytes_removed += removed_bytes
        try:
            candidates = tuple(work_dir.rglob("*"))
        except OSError:
            candidates = ()
        for path in candidates:
            try:
                is_reproducible = path.is_file() and (
                    path.suffix.casefold() == ".wav"
                    or path.name.casefold().endswith(".tmp.wav")
                )
            except OSError:
                continue
            if not is_reproducible:
                continue
            size = _file_size(path)
            try:
                path.unlink()
            except OSError:
                continue
            files_removed += 1
            bytes_removed += size

    # Preview MP4s are internal and fully reproducible.  The current product
    # uses live visual previews, so retaining a crashed/legacy sample provides
    # no employee-facing value and can consume several gigabytes on C:.
    for preview_dir in preview_dirs:
        removed_files, removed_bytes = _remove_tree(preview_dir)
        files_removed += removed_files
        bytes_removed += removed_bytes

    return {"files": files_removed, "bytes": bytes_removed}


def _remove_empty_directories(root: Path) -> None:
    try:
        directories = sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
    except OSError:
        return
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def _cache_entry_files(path: Path) -> tuple[Path, ...]:
    try:
        if path.is_file():
            return (path,)
        return tuple(
            item
            for item in path.rglob("*")
            if item.is_file() and not item.is_symlink()
        )
    except OSError:
        return ()


def _remove_voice_preview_entry(path: Path) -> tuple[int, int]:
    """Remove one WAV/JSON cache pair without exposing a half-deleted pair."""

    files = _cache_entry_files(path)
    size = sum(_file_size(item) for item in files)
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            return 0, 0
        return 1, size

    temporary = path.with_name(
        f".{path.name}.prune-{os.getpid()}-{time.time_ns()}"
    )
    try:
        path.replace(temporary)
    except OSError:
        return 0, 0
    try:
        shutil.rmtree(temporary)
    except OSError:
        # Directory renaming is the pair boundary.  Restore it when a locked
        # file prevents deletion so the cache is still either complete or a
        # clean miss, never a WAV whose JSON sidecar vanished independently.
        try:
            if not path.exists():
                temporary.replace(path)
        except OSError:
            pass
        return 0, 0
    return len(files), size


def prune_voice_preview_cache(
    cache_root: str | Path,
    *,
    max_bytes: int = VOICE_PREVIEW_CACHE_MAX_BYTES,
    max_age_days: float = VOICE_PREVIEW_CACHE_MAX_AGE_DAYS,
    protected_paths: Iterable[str | Path] = (),
) -> dict[str, int]:
    """Bound generated audition audio by age and total size.

    The cache contains no source-of-truth data; a missing pair is regenerated
    on the next audition.  Each cache-key directory is treated as one WAV/JSON
    pair, and cleanup is best effort so a preview currently opened by another
    component cannot break application startup.
    """

    try:
        root = Path(cache_root).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return {"files": 0, "bytes": 0}
    if not root.is_dir():
        return {"files": 0, "bytes": 0}
    try:
        byte_limit = max(0, int(max_bytes))
        age_seconds = max(0.0, float(max_age_days)) * 24 * 60 * 60
    except (TypeError, ValueError, OverflowError):
        return {"files": 0, "bytes": 0}

    protected: set[Path] = set()
    for raw_path in protected_paths:
        try:
            protected.add(Path(raw_path).expanduser().resolve(strict=False))
        except (OSError, RuntimeError, TypeError, ValueError):
            continue

    # Every cache-key directory is one unit: preview.json and its WAV are
    # deleted together.  Legacy loose files are retained as individual units.
    candidate_dirs: list[Path] = []
    try:
        for cache_dir in root.rglob("cache"):
            try:
                if not cache_dir.is_dir() or cache_dir.is_symlink():
                    continue
                candidate_dirs.extend(
                    child
                    for child in cache_dir.iterdir()
                    if child.is_dir() and not child.is_symlink()
                )
            except OSError:
                continue
    except OSError:
        pass

    covered_files: set[Path] = set()
    entries: list[tuple[float, int, Path, bool]] = []
    for candidate_dir in candidate_dirs:
        files = _cache_entry_files(candidate_dir)
        if not files:
            continue
        covered_files.update(files)
        modified_at = 0.0
        total_size = 0
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            modified_at = max(modified_at, float(stat.st_mtime))
            total_size += int(stat.st_size)
        is_protected = any(
            protected_path == candidate_dir
            or candidate_dir in protected_path.parents
            for protected_path in protected
        )
        entries.append((modified_at, total_size, candidate_dir, is_protected))

    try:
        loose_files = tuple(
            path
            for path in root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and path not in covered_files
        )
    except OSError:
        loose_files = ()
    for path in loose_files:
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            (
                float(stat.st_mtime),
                int(stat.st_size),
                path,
                path in protected,
            )
        )

    files_removed = 0
    bytes_removed = 0
    cutoff = time.time() - age_seconds
    retained: list[tuple[float, int, Path, bool]] = []
    for modified_at, size, path, is_protected in entries:
        if is_protected or modified_at >= cutoff:
            retained.append((modified_at, size, path, is_protected))
            continue
        removed_files, removed_bytes = _remove_voice_preview_entry(path)
        if not removed_files:
            retained.append((modified_at, size, path, is_protected))
            continue
        files_removed += removed_files
        bytes_removed += removed_bytes

    total_bytes = sum(size for _modified_at, size, _path, _protected in retained)
    for _modified_at, size, path, is_protected in sorted(
        retained, key=lambda item: item[0]
    ):
        if total_bytes <= byte_limit:
            break
        if is_protected:
            continue
        removed_files, removed_bytes = _remove_voice_preview_entry(path)
        if not removed_files:
            continue
        files_removed += removed_files
        bytes_removed += removed_bytes
        total_bytes = max(0, total_bytes - size)

    _remove_empty_directories(root)
    return {"files": files_removed, "bytes": bytes_removed}


def run_startup_cache_maintenance(data_dir: str | Path) -> dict[str, Any]:
    """Run bounded local maintenance once while the workstation is idle."""

    global _TTS_CACHE_MAINTAINED

    try:
        root = Path(data_dir).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return {"skipped": True, "error": "invalid_data_directory"}
    key = str(root).casefold()
    with _STARTUP_MAINTENANCE_LOCK:
        if key in _MAINTAINED_DATA_DIRS:
            return {"skipped": True}
        _MAINTAINED_DATA_DIRS.add(key)
        clean_tts = not _TTS_CACHE_MAINTAINED
        _TTS_CACHE_MAINTAINED = True

    try:
        render_work = prune_render_work_residuals(root / "render-work")
    except Exception:
        render_work = {"files": 0, "bytes": 0}
    try:
        voice_previews = prune_voice_preview_cache(root / "voice-previews")
    except Exception:
        voice_previews = {"files": 0, "bytes": 0}
    try:
        tts_files = prune_tts_cache() if clean_tts else 0
    except Exception:
        tts_files = 0
    return {
        "skipped": False,
        "render_work": render_work,
        "voice_previews": voice_previews,
        "tts_files": int(tts_files),
    }


__all__ = [
    "VOICE_PREVIEW_CACHE_MAX_AGE_DAYS",
    "VOICE_PREVIEW_CACHE_MAX_BYTES",
    "prune_render_work_residuals",
    "prune_voice_preview_cache",
    "run_startup_cache_maintenance",
]
