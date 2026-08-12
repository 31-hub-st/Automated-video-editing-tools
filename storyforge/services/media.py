"""Media discovery and FFmpeg planning for StoryForge.

The functions in this module deliberately separate *planning* from *execution*.
That makes it possible for the UI to show a complete, copyable command before
starting a long render, and it keeps tests independent of an FFmpeg install.

All subprocess calls use argument lists and explicitly disable the shell.  A
path containing spaces, quotes, or Windows drive letters is therefore kept as
one argument rather than interpolated into a command string.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Callable, Iterable, Mapping, Sequence

from ..cancellation import run_cancellable_process


PathLike = str | os.PathLike[str]
DurationResolver = Callable[[Path], float]

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm"})
AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
)
DEFAULT_USAGE_FILENAME = ".storyforge-media-usage.json"
_VARIANT_CROP_SCALES = (1.0, 1.025, 1.05)
VIDEO_TRANSITIONS = frozenset({"cut", "fade"})
VIDEO_FADE_SECONDS = 0.2
MIN_PLAYBACK_SPEED = 0.8
MAX_PLAYBACK_SPEED = 3.0

# The media index is deliberately technical rather than "smart": it records
# only paths, file snapshots, and durations.  Employee footage remains a
# manual choice.  A five-minute refresh window prevents every queued job from
# recursively walking a large local library, while ``refresh_media_index`` is
# available when an employee has just copied new files into that library.
MEDIA_INDEX_REFRESH_SECONDS = 5 * 60.0
MEDIA_LAZY_PROBE_LIMIT = 32
MEDIA_INDEX_SCHEMA_VERSION = 1

_DurationCacheKey = tuple[str, int, int]
_DURATION_CACHE: dict[_DurationCacheKey, float] = {}
_DURATION_CACHE_LOCKS: dict[_DurationCacheKey, threading.RLock] = {}
_DURATION_CACHE_GUARD = threading.RLock()
_INDEXED_DURATION_SNAPSHOTS: dict[str, tuple[int, int, float, float]] = {}
_USAGE_RECORD_LOCKS: dict[str, threading.RLock] = {}
_USAGE_RECORD_GUARD = threading.RLock()
_MEDIA_INDEX_LOCK = threading.RLock()
_MEDIA_DISCOVERY_CACHE: dict[
    tuple[str, str, tuple[str, ...]], tuple[float, tuple[Path, ...]]
] = {}

# Blurring a full 1080x1920 frame is one of the most expensive operations in
# the render graph.  The blurred layer contains no fine detail by design, so
# create it at one third of the output dimensions and upscale only after the
# blur.  This leaves the foreground, overlays, and ASS subtitles at the final
# resolution while reducing the blur branch to roughly one ninth as many
# pixels.
BACKGROUND_DOWNSCALE_FACTOR = 3
BACKGROUND_BLUR_SIGMA = 30

# These names are persisted in production presets, so keep them stable and
# validate them before building a filter graph.  An empty neutral filter is
# intentional: callers that do not opt into grading receive the exact graph
# used by older StoryForge releases.
COLOR_GRADE_FILTERS: Mapping[str, str] = {
    "neutral": "",
    "suspense_cool": (
        "eq=contrast=1.07:brightness=-0.025:saturation=0.82:gamma=0.96,"
        "colorbalance=rs=-0.035:gs=-0.01:bs=0.055:rm=-0.02:bm=0.035:pl=1,"
        "format=yuv420p"
    ),
    "romance_warm": (
        "eq=contrast=1.025:brightness=0.018:saturation=1.1:gamma=1.025,"
        "colorbalance=rs=0.055:gs=0.018:bs=-0.045:rm=0.028:bm=-0.022:pl=1,"
        "format=yuv420p"
    ),
    "sad_muted": (
        "eq=contrast=0.94:brightness=-0.025:saturation=0.62:gamma=0.97,"
        "colorbalance=rs=-0.018:bs=0.025:pl=1,format=yuv420p"
    ),
    "revenge_contrast": (
        "eq=contrast=1.18:brightness=-0.018:saturation=1.04:gamma=1.015:"
        "gamma_weight=0.85,format=yuv420p"
    ),
    "night_lift": (
        "eq=contrast=1.04:brightness=0.045:saturation=0.88:gamma=1.16:"
        "gamma_weight=0.82,"
        "colorbalance=rs=-0.015:gs=0.01:bs=0.04:pl=1,format=yuv420p"
    ),
}

COVER_ANIMATIONS = frozenset(
    {
        "none",
        "fade",
        "gentle_push",
        "gentle_pull",
        "slow_pan",
        "soft_parallax",
        "vertical_drift",
        "focus_reveal",
        "cinematic_push",
        "ken_burns_left",
        "ken_burns_right",
        "soft_flash",
    }
)

# A folder can be named with either the canonical English category or a common
# English/Chinese synonym.  The canonical name is what is stored in MusicPlan.
MOOD_ALIASES: Mapping[str, tuple[str, ...]] = {
    "suspense": (
        "suspense",
        "suspenseful",
        "mystery",
        "mysterious",
        "thriller",
        "悬疑",
        "惊悚",
        "神秘",
    ),
    "romance": (
        "romance",
        "romantic",
        "love",
        "sweet",
        "浪漫",
        "爱情",
        "恋爱",
        "甜宠",
    ),
    "sad": (
        "sad",
        "sadness",
        "melancholy",
        "tragic",
        "tragedy",
        "悲伤",
        "悲剧",
        "伤感",
        "虐文",
    ),
    "revenge": (
        "revenge",
        "comeback",
        "power",
        "爽文",
        "复仇",
        "逆袭",
        "打脸",
    ),
}


class MediaError(RuntimeError):
    """Base exception for an invalid or impossible media plan."""


class ProbeError(MediaError):
    """Raised when ffprobe cannot return a usable duration."""


@dataclass(frozen=True)
class VideoAsset:
    """A discovered video and its persisted use count."""

    path: Path
    usage_count: int
    duration: float | None = None


@dataclass(frozen=True)
class VideoSegment:
    """The portion of one source video used in the final concatenation."""

    path: Path
    source_duration: float
    duration: float
    start_time: float = 0.0
    mirror: bool = False
    usage_count_before: int = 0
    speed: float = 1.0
    crop_scale: float = 1.0
    source_width: float | None = None
    source_height: float | None = None

    def __post_init__(self) -> None:
        if self.source_duration <= 0:
            raise ValueError("source_duration must be positive")
        if self.duration <= 0:
            raise ValueError("duration must be positive")
        if self.start_time < 0:
            raise ValueError("start_time cannot be negative")
        if (
            isinstance(self.speed, bool)
            or not math.isfinite(self.speed)
            or not MIN_PLAYBACK_SPEED <= self.speed <= MAX_PLAYBACK_SPEED
        ):
            raise ValueError(
                f"speed must be between {MIN_PLAYBACK_SPEED} and {MAX_PLAYBACK_SPEED}"
            )
        if not math.isfinite(self.crop_scale) or not 1.0 <= self.crop_scale <= 1.15:
            raise ValueError("crop_scale must be between 1.0 and 1.15")
        if (self.source_width is None) != (self.source_height is None):
            raise ValueError("source_width and source_height must be provided together")
        if self.source_width is not None and self.source_height is not None:
            if (
                not math.isfinite(self.source_width)
                or not math.isfinite(self.source_height)
                or self.source_width <= 0
                or self.source_height <= 0
            ):
                raise ValueError("source_width and source_height must be positive finite values")
        if self.start_time + self.source_span > self.source_duration + 1e-6:
            raise ValueError("segment extends beyond its source video")

    @property
    def source_span(self) -> float:
        """Source seconds consumed to produce ``duration`` output seconds."""

        return self.duration * self.speed

    def fills_canvas(self, width: int, height: int) -> bool:
        """Whether the fitted foreground safely covers the complete canvas.

        ``source_width`` and ``source_height`` are optional *display-oriented*
        dimensions.  Callers must apply rotation and sample-aspect-ratio before
        storing them.  Unknown geometry deliberately returns ``False`` so the
        established blurred-background path remains the safe fallback.
        """

        if self.source_width is None or self.source_height is None:
            return False
        foreground_width = max(2, math.ceil(width * self.crop_scale / 2) * 2)
        foreground_height = max(2, math.ceil(height * self.crop_scale / 2) * 2)
        source_aspect = self.source_width / self.source_height
        box_aspect = foreground_width / foreground_height
        if source_aspect >= box_aspect:
            fitted_width = float(foreground_width)
            fitted_height = foreground_width / source_aspect
        else:
            fitted_height = float(foreground_height)
            fitted_width = foreground_height * source_aspect
        return fitted_width + 1e-6 >= width and fitted_height + 1e-6 >= height


@dataclass(frozen=True)
class MusicPlan:
    """A selected music track and the number of plays needed for the video."""

    path: Path
    duration: float
    loops: int
    category: str

    @property
    def needs_loop(self) -> bool:
        return self.loops > 1


@dataclass(frozen=True)
class FFmpegPlan:
    """A render command plus the filter graph shown in advanced UI details."""

    command: tuple[str, ...]
    filter_complex: str
    duration: float
    output_path: Path

    @property
    def readable_command(self) -> str:
        """Return a Windows-copyable representation without executing it."""

        return subprocess.list2cmdline(list(self.command))

    def as_list(self) -> list[str]:
        return list(self.command)


def _normalise_extension(path: Path) -> str:
    return path.suffix.casefold()


def _media_index_path() -> Path:
    """Return the persistent index path without importing application state.

    ``configure_runtime_environment`` sets both variables for an employee
    build before this module is imported.  The explicit path is useful for
    diagnostics and isolated tests; the data-directory fallback keeps source
    and Hub runs compatible with the established configuration layout.
    """

    configured = str(os.environ.get("STORYFORGE_MEDIA_INDEX_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    data_value = str(os.environ.get("STORYFORGE_DATA_DIR") or "").strip()
    if data_value:
        data_root = Path(data_value).expanduser().resolve(strict=False)
    else:
        appdata = str(os.environ.get("APPDATA") or "").strip()
        data_root = (
            Path(appdata)
            if appdata
            else Path.home() / "AppData" / "Roaming"
        ) / "StoryForgeStudio"
    return data_root / "cache" / "media-index.sqlite3"


def _media_refresh_seconds() -> float:
    raw = str(os.environ.get("STORYFORGE_MEDIA_INDEX_REFRESH_SECONDS") or "").strip()
    if not raw:
        return MEDIA_INDEX_REFRESH_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return MEDIA_INDEX_REFRESH_SECONDS
    if not math.isfinite(value):
        return MEDIA_INDEX_REFRESH_SECONDS
    return max(1.0, min(value, 24 * 60 * 60.0))


def _canonical_path_key(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve(strict=False)))


def _open_media_index() -> sqlite3.Connection:
    path = _media_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS roots (
            root_key TEXT PRIMARY KEY,
            root_path TEXT NOT NULL,
            scanned_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
            root_key TEXT NOT NULL,
            path_key TEXT NOT NULL,
            path TEXT NOT NULL,
            suffix TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            PRIMARY KEY (root_key, path_key)
        );
        CREATE INDEX IF NOT EXISTS files_root_suffix
            ON files(root_key, suffix, path_key);
        CREATE TABLE IF NOT EXISTS durations (
            path_key TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            duration REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (path_key, size_bytes, mtime_ns)
        );
        """
    )
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES ('schema_version', ?)",
        (str(MEDIA_INDEX_SCHEMA_VERSION),),
    )
    connection.commit()
    return connection


def _scan_media_files(root: Path) -> list[tuple[str, str, str, int, int]]:
    """Walk a local media root once without following links or reparse trees."""

    discovered: list[tuple[str, str, str, int, int]] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names[:] = sorted(
            (
                name
                for name in directory_names
                if not (Path(directory) / name).is_symlink()
            ),
            key=str.casefold,
        )
        for name in sorted(file_names, key=str.casefold):
            path = Path(directory) / name
            suffix = path.suffix.casefold()
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            path_key = _canonical_path_key(path)
            discovered.append(
                (
                    path_key,
                    str(path.resolve(strict=False)),
                    suffix,
                    max(0, int(stat.st_size)),
                    int(stat.st_mtime_ns),
                )
            )
    discovered.sort(key=lambda item: item[1].casefold())
    return discovered


def _remember_indexed_durations(
    rows: Iterable[tuple[str, int, int, float | None]],
    *,
    expires_at: float,
) -> None:
    with _DURATION_CACHE_GUARD:
        for path_key, size_bytes, mtime_ns, raw_duration in rows:
            if raw_duration is None:
                continue
            duration = float(raw_duration)
            if not math.isfinite(duration) or duration <= 0:
                continue
            key = (str(path_key), int(size_bytes), int(mtime_ns))
            _DURATION_CACHE[key] = duration
            _INDEXED_DURATION_SNAPSHOTS[str(path_key)] = (
                int(size_bytes),
                int(mtime_ns),
                duration,
                expires_at,
            )


def _indexed_discover_files(
    root: Path,
    allowed: frozenset[str],
    *,
    force_refresh: bool = False,
) -> list[Path]:
    database_key = _canonical_path_key(_media_index_path())
    root_key = _canonical_path_key(root)
    extension_key = tuple(sorted(allowed))
    cache_key = (database_key, root_key, extension_key)
    now_monotonic = time.monotonic()
    refresh_seconds = _media_refresh_seconds()

    with _MEDIA_INDEX_LOCK:
        cached = _MEDIA_DISCOVERY_CACHE.get(cache_key)
        if not force_refresh and cached is not None and cached[0] > now_monotonic:
            return list(cached[1])

        with closing(_open_media_index()) as connection:
            row = connection.execute(
                "SELECT scanned_at FROM roots WHERE root_key = ?",
                (root_key,),
            ).fetchone()
            scanned_at = float(row[0]) if row is not None else 0.0
            current_time = time.time()
            fresh = (
                not force_refresh
                and scanned_at > 0
                and current_time - scanned_at < refresh_seconds
            )
            if not fresh:
                entries = _scan_media_files(root)
                old_keys = {
                    str(item[0])
                    for item in connection.execute(
                        "SELECT path_key FROM files WHERE root_key = ?",
                        (root_key,),
                    )
                }
                new_keys = {item[0] for item in entries}
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM files WHERE root_key = ?", (root_key,))
                connection.executemany(
                    """
                    INSERT INTO files(
                        root_key, path_key, path, suffix, size_bytes, mtime_ns
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ((root_key, *item) for item in entries),
                )
                connection.execute(
                    """
                    INSERT INTO roots(root_key, root_path, scanned_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(root_key) DO UPDATE SET
                        root_path = excluded.root_path,
                        scanned_at = excluded.scanned_at
                    """,
                    (root_key, str(root), current_time),
                )
                for path_key, _path, _suffix, size_bytes, mtime_ns in entries:
                    connection.execute(
                        """
                        DELETE FROM durations
                        WHERE path_key = ?
                          AND NOT (size_bytes = ? AND mtime_ns = ?)
                        """,
                        (path_key, size_bytes, mtime_ns),
                    )
                removed_keys = old_keys - new_keys
                if removed_keys:
                    connection.executemany(
                        "DELETE FROM durations WHERE path_key = ?",
                        ((item,) for item in removed_keys),
                    )
                # Bound orphaned entries from roots that were removed outside
                # StoryForge.  This is a cache only; failures never block a job.
                connection.execute(
                    "DELETE FROM durations WHERE updated_at < ?",
                    (current_time - 30 * 24 * 60 * 60,),
                )
                stale_root_keys = tuple(
                    str(item[0])
                    for item in connection.execute(
                        "SELECT root_key FROM roots WHERE scanned_at < ?",
                        (current_time - 30 * 24 * 60 * 60,),
                    )
                )
                if stale_root_keys:
                    connection.executemany(
                        "DELETE FROM files WHERE root_key = ?",
                        ((item,) for item in stale_root_keys),
                    )
                    connection.executemany(
                        "DELETE FROM roots WHERE root_key = ?",
                        ((item,) for item in stale_root_keys),
                    )
                connection.commit()
                scanned_at = current_time

            placeholders = ",".join("?" for _item in extension_key)
            query = f"""
                SELECT f.path_key, f.path, f.size_bytes, f.mtime_ns, d.duration
                FROM files AS f
                LEFT JOIN durations AS d
                  ON d.path_key = f.path_key
                 AND d.size_bytes = f.size_bytes
                 AND d.mtime_ns = f.mtime_ns
                WHERE f.root_key = ? AND f.suffix IN ({placeholders})
                ORDER BY f.path COLLATE NOCASE
            """
            rows = list(connection.execute(query, (root_key, *extension_key)))

        expires_at = now_monotonic + refresh_seconds
        _remember_indexed_durations(
            (
                (str(path_key), int(size_bytes), int(mtime_ns), duration)
                for path_key, _path, size_bytes, mtime_ns, duration in rows
            ),
            expires_at=expires_at,
        )
        paths = tuple(Path(str(row[1])) for row in rows)
        _MEDIA_DISCOVERY_CACHE[cache_key] = (expires_at, paths)
        return list(paths)


def discover_files(folder: PathLike, extensions: Iterable[str]) -> list[Path]:
    """Return an indexed, deterministic recursive media listing.

    The first call scans the selected root.  Later queued jobs reuse the
    persistent snapshot instead of performing another complete ``rglob``.
    Index corruption or a read-only cache never makes production fail: the
    function falls back to a direct read-only walk for that call.
    """

    root = Path(folder).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise MediaError(f"Media folder does not exist: {root}")
    allowed = frozenset(extension.casefold() for extension in extensions)
    if not allowed:
        return []
    try:
        return _indexed_discover_files(root, allowed)
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        # The index is an optimization, never a production dependency.
        return sorted(
            (
                path
                for path in root.rglob("*")
                if path.is_file() and _normalise_extension(path) in allowed
            ),
            key=lambda path: path.as_posix().casefold(),
        )


def refresh_media_index(
    folder: PathLike,
    extensions: Iterable[str] = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS,
) -> list[Path]:
    """Force one incremental technical refresh after files were added."""

    root = Path(folder).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise MediaError(f"Media folder does not exist: {root}")
    allowed = frozenset(extension.casefold() for extension in extensions)
    try:
        return _indexed_discover_files(root, allowed, force_refresh=True)
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        return discover_files(root, allowed)


def clear_media_index_memory_cache() -> None:
    """Forget process-local snapshots while retaining the persistent index."""

    with _MEDIA_INDEX_LOCK:
        _MEDIA_DISCOVERY_CACHE.clear()
    with _DURATION_CACHE_GUARD:
        _DURATION_CACHE.clear()
        _DURATION_CACHE_LOCKS.clear()
        _INDEXED_DURATION_SNAPSHOTS.clear()


def _duration_cache_key(path: Path) -> _DurationCacheKey | None:
    try:
        resolved = path.expanduser().resolve(strict=True)
        stat = resolved.stat()
    except OSError:
        return None
    return (os.path.normcase(str(resolved)), int(stat.st_size), int(stat.st_mtime_ns))


def _duration_cache_lock(key: _DurationCacheKey) -> threading.RLock:
    with _DURATION_CACHE_GUARD:
        return _DURATION_CACHE_LOCKS.setdefault(key, threading.RLock())


def clear_duration_cache() -> None:
    """Clear the in-memory media duration cache, primarily for diagnostics/tests."""

    with _DURATION_CACHE_GUARD:
        _DURATION_CACHE.clear()
        _DURATION_CACHE_LOCKS.clear()
        _INDEXED_DURATION_SNAPSHOTS.clear()


def _persistent_duration(key: _DurationCacheKey) -> float | None:
    try:
        with _MEDIA_INDEX_LOCK, closing(_open_media_index()) as connection:
            row = connection.execute(
                """
                SELECT duration FROM durations
                WHERE path_key = ? AND size_bytes = ? AND mtime_ns = ?
                """,
                key,
            ).fetchone()
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        return None
    if row is None:
        return None
    try:
        duration = float(row[0])
    except (TypeError, ValueError):
        return None
    return duration if math.isfinite(duration) and duration > 0 else None


def _store_persistent_duration(key: _DurationCacheKey, duration: float) -> None:
    try:
        with _MEDIA_INDEX_LOCK, closing(_open_media_index()) as connection:
            connection.execute(
                "DELETE FROM durations WHERE path_key = ?",
                (key[0],),
            )
            connection.execute(
                """
                INSERT INTO durations(
                    path_key, size_bytes, mtime_ns, duration, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (*key, duration, time.time()),
            )
            connection.commit()
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        # A cache write can fail on antivirus locks or a read-only volume.
        # The successfully probed duration remains valid in memory.
        return


def _trusted_index_duration(path: Path) -> float | None:
    key = _canonical_path_key(path)
    with _DURATION_CACHE_GUARD:
        snapshot = _INDEXED_DURATION_SNAPSHOTS.get(key)
    if snapshot is None:
        return None
    _size_bytes, _mtime_ns, duration, expires_at = snapshot
    if expires_at <= time.monotonic():
        return None
    return duration


def _cached_duration(
    path: Path,
    resolver: DurationResolver,
    *,
    trust_index: bool = False,
) -> float:
    """Resolve one duration once per canonical path/size/mtime snapshot."""

    if trust_index:
        indexed = _trusted_index_duration(path)
        if indexed is not None:
            return indexed
    key = _duration_cache_key(path)
    if key is None:
        return float(resolver(path))
    lock = _duration_cache_lock(key)
    with lock:
        with _DURATION_CACHE_GUARD:
            cached = _DURATION_CACHE.get(key)
        if cached is not None:
            return cached
        persisted = _persistent_duration(key)
        if persisted is not None:
            with _DURATION_CACHE_GUARD:
                _DURATION_CACHE[key] = persisted
                _INDEXED_DURATION_SNAPSHOTS[key[0]] = (
                    key[1],
                    key[2],
                    persisted,
                    time.monotonic() + _media_refresh_seconds(),
                )
            return persisted
        duration = float(resolver(path))
        if math.isfinite(duration) and duration > 0:
            with _DURATION_CACHE_GUARD:
                # Keep only the current file snapshot so repeated media-library
                # refreshes cannot grow the cache for an edited path forever.
                stale = [
                    item
                    for item in _DURATION_CACHE
                    if item[0] == key[0] and item != key
                ]
                for item in stale:
                    _DURATION_CACHE.pop(item, None)
                    _DURATION_CACHE_LOCKS.pop(item, None)
                _DURATION_CACHE[key] = duration
                _INDEXED_DURATION_SNAPSHOTS[key[0]] = (
                    key[1],
                    key[2],
                    duration,
                    time.monotonic() + _media_refresh_seconds(),
                )
            _store_persistent_duration(key, duration)
        return duration


def _usage_key(path: Path, root: Path) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        relative = path.resolve()
    return relative.as_posix().casefold()


def load_usage_record(
    video_folder: PathLike,
    *,
    usage_filename: str = DEFAULT_USAGE_FILENAME,
) -> dict[str, int]:
    """Read the local video-use JSON, tolerating a missing/corrupt record.

    Version 1 records use ``{"version": 1, "usage": {...}}``.  A plain mapping
    is also accepted so early/manual records remain usable.
    """

    record_path = Path(video_folder) / usage_filename
    try:
        raw = json.loads(record_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}

    if isinstance(raw, dict) and isinstance(raw.get("usage"), dict):
        raw = raw["usage"]
    if not isinstance(raw, dict):
        return {}

    usage: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or isinstance(value, bool):
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count >= 0:
            usage[key.replace("\\", "/").casefold()] = count
    return usage


def summarize_video_usage(
    video_folder: PathLike,
    *,
    usage_filename: str = DEFAULT_USAGE_FILENAME,
) -> dict[str, int]:
    """Return every supported video's persisted use count without thresholds."""

    root = Path(video_folder)
    usage = load_usage_record(root, usage_filename=usage_filename)
    summary: dict[str, int] = {}
    for path in discover_files(root, VIDEO_EXTENSIONS):
        try:
            display_path = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            display_path = path.name
        summary[display_path] = usage.get(_usage_key(path, root), 0)
    return summary


def save_usage_record(
    video_folder: PathLike,
    usage: Mapping[str, int],
    *,
    usage_filename: str = DEFAULT_USAGE_FILENAME,
) -> Path:
    """Atomically persist video-use counts beside the media folder."""

    root = Path(video_folder)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / usage_filename
    clean_usage = {
        str(key).replace("\\", "/").casefold(): max(0, int(value))
        for key, value in sorted(usage.items(), key=lambda item: str(item[0]).casefold())
    }
    payload = {"version": 1, "usage": clean_usage}

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=root,
            prefix=f".{usage_filename}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(payload, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
        os.replace(temporary_name, destination)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
    return destination


def increment_usage_record(
    media_folder: PathLike,
    paths: Iterable[PathLike],
    *,
    usage_filename: str = DEFAULT_USAGE_FILENAME,
) -> Path:
    """Atomically add successful-use counts for local media paths.

    Selection only reads this ledger.  Render pipelines call this helper after
    the output has passed quality control, so failed/cancelled jobs never make
    an asset look more heavily used.  The per-ledger lock also prevents two
    concurrent jobs in one worker process from overwriting each other's count.
    """

    root = Path(media_folder)
    ledger_key = str((root / usage_filename).resolve()).casefold()
    with _USAGE_RECORD_GUARD:
        lock = _USAGE_RECORD_LOCKS.setdefault(ledger_key, threading.RLock())
    with lock:
        usage = load_usage_record(root, usage_filename=usage_filename)
        for raw_path in paths:
            path = Path(raw_path)
            key = _usage_key(path, root)
            usage[key] = int(usage.get(key, 0)) + 1
        return save_usage_record(
            root,
            usage,
            usage_filename=usage_filename,
        )


def select_video_assets(
    video_folder: PathLike,
    *,
    limit: int | None = None,
    usage_filename: str = DEFAULT_USAGE_FILENAME,
    duration_resolver: DurationResolver | None = None,
) -> list[VideoAsset]:
    """Return videos ordered from least-used to most-used.

    Durations are optional here because the UI can list candidates cheaply.
    ``plan_video_segments`` always resolves durations before making a plan.
    """

    root = Path(video_folder)
    paths = discover_files(root, VIDEO_EXTENSIONS)
    usage = load_usage_record(root, usage_filename=usage_filename)
    assets = [
        VideoAsset(
            path=path,
            usage_count=usage.get(_usage_key(path, root), 0),
            duration=(
                _planning_duration(path, duration_resolver)
                if duration_resolver
                else None
            ),
        )
        for path in paths
    ]
    assets.sort(key=lambda asset: (asset.usage_count, asset.path.as_posix().casefold()))
    if limit is not None:
        if limit < 0:
            raise ValueError("limit cannot be negative")
        return assets[:limit]
    return assets


def probe_duration(
    media_path: PathLike,
    *,
    ffprobe_path: PathLike = "ffprobe",
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> float:
    """Return a media duration using ffprobe without invoking a shell."""

    path = Path(media_path)

    def resolve(_path: Path) -> float:
        command = [
            str(ffprobe_path),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(_path),
        ]
        try:
            completed = run_cancellable_process(
                command,
                runner=runner,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except OSError as exc:
            raise ProbeError(f"Unable to start ffprobe for {media_path}: {exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or "unknown ffprobe error").strip()
            raise ProbeError(f"ffprobe failed for {media_path}: {detail}")
        try:
            duration = float((completed.stdout or "").strip())
        except ValueError as exc:
            raise ProbeError(f"ffprobe returned an invalid duration for {media_path}") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise ProbeError(f"ffprobe returned a non-positive duration for {media_path}")
        return duration

    return _cached_duration(path, resolve)


def _planning_duration(path: Path, resolver: DurationResolver) -> float:
    """Resolve a duration while trusting the fresh technical media snapshot."""

    indexed = _trusted_index_duration(path)
    if indexed is not None:
        return indexed
    if resolver is probe_duration:
        return probe_duration(path)
    return _cached_duration(path, resolver, trust_index=True)


def _stable_variant_seed(value: int | str | bytes) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    elif isinstance(value, bytes):
        encoded = value
    else:
        raise TypeError("variant_seed must be an int, str, bytes, or None")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big")


def _excluded_path_keys(paths: Iterable[PathLike] | None) -> set[str]:
    """Return stable, case-insensitive keys for assets skipped by a retry."""

    if not paths:
        return set()
    return {
        os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))
        for path in paths
    }


def plan_video_segments(
    video_folder: PathLike,
    target_duration: float,
    *,
    mood: str | None = None,
    duration_resolver: DurationResolver = probe_duration,
    usage_filename: str = DEFAULT_USAGE_FILENAME,
    commit_usage: bool = True,
    variant_seed: int | str | bytes | None = None,
    excluded_paths: Iterable[PathLike] | None = None,
    playback_speed: float = 1.0,
    video_transition: str = "cut",
) -> list[VideoSegment]:
    """Plan fixed-speed source clips until ``target_duration`` is covered.

    A least-used source that can cover the complete output is preferred.  When
    no single source is long enough, every distinct source is considered before
    any source is reused.  If all distinct footage is still too short, sources
    are reused in least-used order.  ``fade`` transitions overlap adjacent
    segments by :data:`VIDEO_FADE_SECONDS`, so the raw segment durations include
    those overlaps while the effective timeline remains exactly
    ``target_duration``.

    ``variant_seed`` deterministically rotates equal-use assets and varies
    start, mirror, and crop choices.  Playback speed is always the explicit
    ``playback_speed`` value and is never changed by the variant seed.
    """

    if not math.isfinite(target_duration) or target_duration <= 0:
        raise ValueError("target_duration must be a positive finite number")
    if (
        isinstance(playback_speed, bool)
        or not math.isfinite(playback_speed)
        or not MIN_PLAYBACK_SPEED <= playback_speed <= MAX_PLAYBACK_SPEED
    ):
        raise ValueError(
            f"playback_speed must be between {MIN_PLAYBACK_SPEED} and "
            f"{MAX_PLAYBACK_SPEED}"
        )
    if video_transition not in VIDEO_TRANSITIONS:
        raise ValueError("video_transition must be cut or fade")
    speed = float(playback_speed)
    transition_overlap = VIDEO_FADE_SECONDS if video_transition == "fade" else 0.0

    root = Path(video_folder)
    paths = (
        _category_media_files(root, mood, VIDEO_EXTENSIONS)
        if mood
        else discover_files(root, VIDEO_EXTENSIONS)
    )
    excluded = _excluded_path_keys(excluded_paths)
    if excluded:
        paths = [
            path
            for path in paths
            if os.path.normcase(str(path.expanduser().resolve(strict=False)))
            not in excluded
        ]
    if not paths:
        category_note = f" for category {canonical_mood(mood)!r}" if mood else ""
        raise MediaError(f"No supported videos{category_note} found in: {root}")

    initial_usage = load_usage_record(root, usage_filename=usage_filename)
    working_usage = dict(initial_usage)
    seed_value = _stable_variant_seed(variant_seed) if variant_seed is not None else None
    randomiser = random.Random(seed_value) if seed_value is not None else None

    def usage_count(path: Path, usage: Mapping[str, int]) -> int:
        return int(usage.get(_usage_key(path, root), 0))

    def rotate_equal_usage(paths_to_order: Iterable[Path]) -> list[Path]:
        """Order by usage while allowing seeded variants to rotate ties."""

        groups: dict[int, list[Path]] = {}
        for path in paths_to_order:
            groups.setdefault(usage_count(path, initial_usage), []).append(path)
        ordered: list[Path] = []
        for group_index, count in enumerate(sorted(groups)):
            tied = sorted(
                groups[count],
                key=lambda path: path.as_posix().casefold(),
            )
            if seed_value is not None and len(tied) > 1:
                offset = (seed_value + group_index) % len(tied)
                tied = tied[offset:] + tied[:offset]
            ordered.extend(tied)
        return ordered

    durations: dict[Path, float] = {}
    probe_errors: list[str] = []
    sufficient_candidate: Path | None = None
    usable_span = 0.0
    uncached_probes = 0
    # A fresh library can contain thousands of files. Probe only enough
    # least-used candidates to produce this job; cached candidates remain
    # effectively free and are still considered. Subsequent jobs gradually
    # fill the persistent duration index as usage rotates through the library.
    for path in rotate_equal_usage(paths):
        indexed_duration = _trusted_index_duration(path)
        if (
            indexed_duration is None
            and uncached_probes >= MEDIA_LAZY_PROBE_LIMIT
            and usable_span + 1e-6 >= target_duration
        ):
            continue
        if indexed_duration is None:
            uncached_probes += 1
        try:
            duration = _planning_duration(path, duration_resolver)
        except (MediaError, OSError, TypeError, ValueError) as exc:
            probe_errors.append(f"{path.name}: {exc}")
            continue
        if math.isfinite(duration) and duration > 0:
            durations[path] = duration
            available = duration / speed
            if available + 1e-6 >= target_duration:
                sufficient_candidate = path
                break
            if video_transition == "fade" and durations:
                contribution = max(
                    0.0,
                    available - (transition_overlap if len(durations) > 1 else 0.0),
                )
            else:
                contribution = available
            usable_span += contribution
        else:
            probe_errors.append(f"{path.name}: invalid duration {duration!r}")
    if not durations:
        details = "; ".join(probe_errors) or "no probe results"
        raise MediaError(f"No usable videos found in {root}: {details}")

    segments: list[VideoSegment] = []
    uses_this_plan: dict[str, int] = {}
    remaining = target_duration

    def append_segment(chosen: Path, desired_duration: float) -> float:
        """Append one segment and return its effective timeline contribution."""

        key = _usage_key(chosen, root)
        count_before = working_usage.get(key, 0)
        source_duration = durations[chosen]
        repeat_number = uses_this_plan.get(key, 0)
        if seed_value is None:
            crop_scale = 1.0
            start_time = 0.0
            mirror = repeat_number % 2 == 1
        else:
            crop_scale = _VARIANT_CROP_SCALES[
                (seed_value * 2 + len(segments) + repeat_number) % len(_VARIANT_CROP_SCALES)
            ]
            source_span = desired_duration * speed
            available_start = max(0.0, source_duration - source_span)
            if available_start > 1e-6:
                assert randomiser is not None
                start_fraction = 0.15 + 0.7 * randomiser.random()
                start_time = available_start * start_fraction
            else:
                start_time = 0.0
            mirror = (seed_value + len(segments) + repeat_number) % 2 == 1
        segment = VideoSegment(
            path=chosen,
            source_duration=source_duration,
            duration=desired_duration,
            start_time=start_time,
            mirror=mirror,
            usage_count_before=count_before,
            speed=speed,
            crop_scale=crop_scale,
        )
        segments.append(segment)
        uses_this_plan[key] = repeat_number + 1
        working_usage[key] = count_before + 1
        return desired_duration - (transition_overlap if len(segments) > 1 else 0.0)

    if sufficient_candidate is not None:
        append_segment(sufficient_candidate, target_duration)
        remaining = 0.0
    else:
        distinct_paths = rotate_equal_usage(durations)
        if video_transition == "fade":
            # Every segment in an xfade chain must be longer than the overlap;
            # shorter clips cannot add positive time to the final timeline.
            distinct_paths = [
                path
                for path in distinct_paths
                if durations[path] / speed > transition_overlap + 1e-6
            ]
            if not distinct_paths:
                raise MediaError(
                    "No usable videos are longer than the 0.2 second fade "
                    f"transition after {speed:g}x playback"
                )

        for chosen in distinct_paths:
            overlap = transition_overlap if segments else 0.0
            available_duration = durations[chosen] / speed
            segment_duration = min(available_duration, remaining + overlap)
            contribution = append_segment(chosen, segment_duration)
            remaining = max(0.0, remaining - contribution)
            if remaining <= 1e-6:
                break

        while remaining > 1e-6:
            lowest_usage = min(
                usage_count(path, working_usage) for path in distinct_paths
            )
            eligible = sorted(
                (
                    path
                    for path in distinct_paths
                    if usage_count(path, working_usage) == lowest_usage
                ),
                key=lambda path: path.as_posix().casefold(),
            )
            chosen = (
                eligible[(seed_value + len(segments)) % len(eligible)]
                if seed_value is not None
                else eligible[0]
            )
            available_duration = durations[chosen] / speed
            segment_duration = min(
                available_duration,
                remaining + transition_overlap,
            )
            contribution = append_segment(chosen, segment_duration)
            if contribution <= 1e-6:
                raise MediaError(
                    "Video segments cannot add usable time with the selected "
                    f"{video_transition} transition"
                )
            remaining = max(0.0, remaining - contribution)

            # This is a defensive guard against accidental sub-millisecond probe
            # results producing a practically unbounded plan.
            if len(segments) > 100_000:
                raise MediaError("Too many source segments required for this narration")

    if commit_usage:
        save_usage_record(root, working_usage, usage_filename=usage_filename)
    return segments


def canonical_mood(mood: str) -> str:
    """Map an English/Chinese mood label to one of four canonical categories."""

    normalised = mood.strip().casefold().replace("_", " ").replace("-", " ")
    compact = "".join(normalised.split())
    for category, aliases in MOOD_ALIASES.items():
        for alias in aliases:
            alias_normalised = alias.casefold()
            if compact == "".join(alias_normalised.split()):
                return category
    supported = ", ".join(MOOD_ALIASES)
    raise MediaError(f"Unknown music mood {mood!r}; expected one of: {supported}")


def _category_media_files(
    folder: PathLike,
    mood: str,
    extensions: Iterable[str],
) -> list[Path]:
    """Discover media only inside folders matching the story's mood.

    A batch may point directly at ``悬疑``/``romance`` or at a root containing
    the four category folders.  Nested category folders are also supported so
    users can keep separate source packs below one library root.
    """

    # ``discover_files`` returns canonical absolute paths.  Resolve the root
    # with the same semantics before testing ancestry so that a Windows 8.3
    # alias (for example ``RUNNER~1``) and its long form identify one library.
    root = Path(folder).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise MediaError(f"Media folder does not exist: {root}")
    category = canonical_mood(mood)
    accepted_names = {name.casefold() for name in MOOD_ALIASES[category]}
    indexed = discover_files(root, extensions)
    if root.name.casefold() in accepted_names:
        return indexed
    files = [
        path
        for path in indexed
        if any(
            parent.name.casefold() in accepted_names
            for parent in path.parents
            if parent != root and root in parent.parents
        )
    ]
    return sorted(files, key=lambda path: path.as_posix().casefold())


def _music_candidates(music_folder: PathLike, mood: str) -> tuple[str, list[Path]]:
    # Keep ancestry checks in the same canonical path space as the persistent
    # media index.  Without this, Windows short-path aliases can make every
    # correctly categorised track look as though it lives outside the root.
    root = Path(music_folder).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise MediaError(f"Music folder does not exist: {root}")
    category = canonical_mood(mood)
    accepted_names = {name.casefold() for name in MOOD_ALIASES[category]}
    indexed = discover_files(root, AUDIO_EXTENSIONS)
    if root.name.casefold() in accepted_names:
        candidates = set(indexed)
    else:
        candidates = {
            path
            for path in indexed
            if any(
                parent.name.casefold() in accepted_names
                for parent in path.parents
                if parent != root and root in parent.parents
            )
        }

    # A flat music folder remains usable during initial setup. Once recognised
    # category media exists, files from other categories are never mixed in.
    if not candidates:
        candidates.update(path for path in indexed if path.parent == root)
    ordered = sorted(candidates, key=lambda path: path.as_posix().casefold())
    return category, ordered


def select_music_asset(
    music_folder: PathLike,
    mood: str,
    target_duration: float,
    *,
    duration_resolver: DurationResolver = probe_duration,
    excluded_paths: Iterable[PathLike] | None = None,
) -> MusicPlan:
    """Choose least-used music from the compatible story category.

    Local successful-use counts are the primary ordering key.  For tracks with
    the same count, a non-looping track is preferred and the shortest adequate
    duration wins; if all tied tracks require looping, the longest wins.  The
    final path tie-break makes repeated selection deterministic.
    """

    if not math.isfinite(target_duration) or target_duration <= 0:
        raise ValueError("target_duration must be a positive finite number")
    category, candidates = _music_candidates(music_folder, mood)
    excluded = _excluded_path_keys(excluded_paths)
    if excluded:
        candidates = [
            path
            for path in candidates
            if os.path.normcase(str(path.expanduser().resolve(strict=False)))
            not in excluded
        ]
    if not candidates:
        raise MediaError(f"No music found for category {category!r} in {music_folder}")

    root = Path(music_folder)
    usage = load_usage_record(root)
    durations: list[tuple[Path, float, int]] = []
    errors: list[str] = []
    for path in candidates:
        try:
            duration = _planning_duration(path, duration_resolver)
        except (MediaError, OSError, TypeError, ValueError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if math.isfinite(duration) and duration > 0:
            durations.append((path, duration, usage.get(_usage_key(path, root), 0)))
        else:
            errors.append(f"{path.name}: invalid duration {duration!r}")
    if not durations:
        raise MediaError("No usable music tracks: " + "; ".join(errors))

    selected_path, selected_duration, _selected_usage = min(
        durations,
        key=lambda item: (
            item[2],
            0 if item[1] + 1e-6 >= target_duration else 1,
            item[1] if item[1] + 1e-6 >= target_duration else -item[1],
            item[0].as_posix().casefold(),
        ),
    )
    loops = max(1, math.ceil(target_duration / selected_duration))
    return MusicPlan(selected_path, selected_duration, loops, category)


def _ffmpeg_number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def escape_filter_path(path: PathLike) -> str:
    """Escape a filename embedded in an FFmpeg filter option.

    Input/output filenames are normal command arguments and need no manual
    quoting.  ASS filenames live inside ``filter_complex``, which has its own
    parser: drive-letter colons and single quotes therefore need escaping.

    A quote inside an already single-quoted FFmpeg option cannot be preserved
    with just ``\'``: the filter parser consumes it and silently looks for a
    different filename.  Close the quoted section, pass an escaped quote
    through both filter-parser layers, then reopen the section.  In the final
    graph this is the ``'\\\''`` sequence documented by FFmpeg's multi-level
    escaping rules.
    """

    normalised = str(Path(path).resolve(strict=False)).replace("\\", "/")
    return normalised.replace("'", r"'\\\''").replace(":", r"\:")


def build_low_memory_segment_plan(
    segment: VideoSegment,
    output_path: PathLike,
    *,
    ffmpeg_path: PathLike = "ffmpeg",
    width: int = 1080,
    height: int = 1920,
    fps: int = 60,
    color_grade: str = "neutral",
    overwrite: bool = True,
) -> FFmpegPlan:
    """Normalize one source clip without opening the rest of the batch.

    The normal render graph opens every selected source at once.  That is fast
    on a workstation with enough memory, but a long story assembled from many
    clips can exhaust a smaller employee computer before FFmpeg reaches the
    encoder.  This deliberately serial plan is used only by the automatic
    low-memory fallback: each source is trimmed, fitted and graded in its own
    process, after which the identical intermediates can be stream-concatenated
    and the final subtitle/audio graph sees one video input instead of many.

    Restricting decoder, filter and encoder threads also bounds the number of
    full-resolution frames held in flight.  Intermediates use a visually
    conservative CRF because the final delivery file is encoded once more.
    """

    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("width, height, and fps must be positive")
    if color_grade not in COLOR_GRADE_FILTERS:
        raise ValueError(f"Unknown color grade: {color_grade}")

    output = Path(output_path)
    start = _ffmpeg_number(segment.start_time)
    source_span = _ffmpeg_number(segment.source_span)
    duration = _ffmpeg_number(segment.duration)
    mirror_filter = ",hflip" if segment.mirror else ""
    setpts_filter = (
        "setpts=PTS-STARTPTS"
        if abs(segment.speed - 1.0) <= 1e-9
        else f"setpts=(PTS-STARTPTS)/{_ffmpeg_number(segment.speed)}"
    )
    foreground_width = max(2, math.ceil(width * segment.crop_scale / 2) * 2)
    foreground_height = max(2, math.ceil(height * segment.crop_scale / 2) * 2)
    color_filter = COLOR_GRADE_FILTERS[color_grade]
    color_suffix = f",{color_filter}" if color_filter else ""
    fps_tail_pad = _ffmpeg_number(2.0 / fps + 1e-6)
    source_prefix = (
        f"[0:v:0]trim=start={start}:duration={source_span},"
        f"{setpts_filter}{mirror_filter}"
    )

    graph: list[str] = []
    if segment.fills_canvas(width, height):
        graph.append(
            f"{source_prefix},"
            f"scale={foreground_width}:{foreground_height}:"
            "force_original_aspect_ratio=decrease,"
            f"crop={width}:{height}:x='(iw-ow)/2':y='(ih-oh)/2',"
            f"setsar=1,format=yuv420p{color_suffix},fps={fps},"
            f"tpad=stop_mode=clone:stop_duration={fps_tail_pad},"
            f"trim=duration={duration},setpts=PTS-STARTPTS[vout]"
        )
    else:
        background_width = max(
            2,
            math.ceil(width / (2 * BACKGROUND_DOWNSCALE_FACTOR)) * 2,
        )
        background_height = max(
            2,
            math.ceil(height / (2 * BACKGROUND_DOWNSCALE_FACTOR)) * 2,
        )
        background_blur_sigma = _ffmpeg_number(
            BACKGROUND_BLUR_SIGMA / BACKGROUND_DOWNSCALE_FACTOR
        )
        graph.extend(
            (
                f"{source_prefix},split=2[fg][bg]",
                f"[bg]scale={background_width}:{background_height}:"
                "force_original_aspect_ratio=increase,"
                f"crop={background_width}:{background_height},"
                f"gblur=sigma={background_blur_sigma}:steps=2,"
                f"scale={width}:{height}:flags=bilinear[bgp]",
                f"[fg]scale={foreground_width}:{foreground_height}:"
                "force_original_aspect_ratio=decrease[fgp]",
                "[bgp][fgp]overlay=(W-w)/2:(H-h)/2,"
                f"setsar=1,format=yuv420p{color_suffix},fps={fps},"
                f"tpad=stop_mode=clone:stop_duration={fps_tail_pad},"
                f"trim=duration={duration},setpts=PTS-STARTPTS[vout]",
            )
        )

    filter_complex = ";".join(graph)
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-nostdin",
        "-y" if overwrite else "-n",
        # Applied to the input decoder.
        "-threads",
        "1",
        "-i",
        str(segment.path),
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-an",
        "-t",
        duration,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        # Applied to the output encoder.
        "-threads",
        "1",
        str(output),
    ]
    return FFmpegPlan(tuple(command), filter_complex, segment.duration, output)


def build_ffmpeg_plan(
    segments: Sequence[VideoSegment],
    narration_path: PathLike,
    music: MusicPlan | PathLike | None,
    subtitle_path: PathLike,
    output_path: PathLike,
    target_duration: float,
    *,
    ffmpeg_path: PathLike = "ffmpeg",
    width: int = 1080,
    height: int = 1920,
    fps: int = 60,
    bgm_volume: float = 0.28,
    video_encoder: str = "libx264",
    overwrite: bool = True,
    cover_path: PathLike | None = None,
    cover_intro_start: float = 2.0,
    cover_intro_duration: float = 2.0,
    end_card_duration: float = 6.0,
    render_mode: str = "quality",
    cover_animation: str = "gentle_push",
    cover_outro_enabled: bool = True,
    color_grade: str = "neutral",
    end_card_without_cover: bool = False,
    cover_intro_enabled: bool = True,
    platform_logo_path: PathLike | None = None,
    platform_logo_start: float = 0.0,
    platform_logo_duration: float = 5.5,
    platform_logo_x_percent: float = 50.0,
    platform_logo_y_percent: float = 28.645833,
    intro_card_cover_path: PathLike | None = None,
    intro_card_cover_start: float = 0.0,
    intro_card_cover_duration: float = 5.5,
    intro_card_cover_x_percent: float = 74.0,
    intro_card_cover_y_percent: float = 35.0,
    intro_card_cover_width_percent: float = 30.0,
    intro_card_cover_height_percent: float = 32.0,
    intro_card_cover_rotation_degrees: float = -4.0,
    video_transition: str = "cut",
) -> FFmpegPlan:
    """Build the complete 9:16 narration render command without running it.

    Passing ``music=None`` creates a narration-only audio graph and omits the
    music input entirely.  Existing ``MusicPlan`` and path callers retain the
    established ducked background-music mix.
    """

    if not segments:
        raise MediaError("At least one video segment is required")
    if not math.isfinite(target_duration) or target_duration <= 0:
        raise ValueError("target_duration must be a positive finite number")
    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("width, height, and fps must be positive")
    if not 0 <= bgm_volume <= 1:
        raise ValueError("bgm_volume must be between 0 and 1")
    if render_mode not in {"speed", "quality", "compatibility"}:
        raise ValueError("render_mode must be speed, quality, or compatibility")
    if video_transition not in VIDEO_TRANSITIONS:
        raise ValueError("video_transition must be cut or fade")
    if not isinstance(cover_outro_enabled, bool):
        raise ValueError("cover_outro_enabled must be a boolean")
    if not isinstance(cover_intro_enabled, bool):
        raise ValueError("cover_intro_enabled must be a boolean")
    if cover_animation not in COVER_ANIMATIONS:
        raise ValueError(
            "cover_animation must be none, fade, gentle_push, gentle_pull, "
            "slow_pan, soft_parallax, vertical_drift, focus_reveal, "
            "cinematic_push, ken_burns_left, ken_burns_right, or soft_flash"
        )
    if color_grade not in COLOR_GRADE_FILTERS:
        raise ValueError(
            "color_grade must be neutral, suspense_cool, romance_warm, "
            "sad_muted, revenge_contrast, or night_lift"
        )
    if end_card_without_cover:
        if not math.isfinite(end_card_duration) or not 5.0 <= end_card_duration <= 7.0:
            raise ValueError("end_card_duration must be between 5 and 7 seconds")
        if end_card_duration > target_duration:
            raise ValueError("end_card_duration cannot exceed target_duration")
    if platform_logo_path is not None:
        if not math.isfinite(platform_logo_start) or platform_logo_start < 0:
            raise ValueError(
                "platform_logo_start must be a non-negative finite number"
            )
        if not math.isfinite(platform_logo_duration) or platform_logo_duration <= 0:
            raise ValueError("platform_logo_duration must be a positive finite number")
        if platform_logo_start + platform_logo_duration > target_duration + 1e-9:
            raise ValueError("platform logo display window cannot exceed target_duration")
        if not math.isfinite(platform_logo_x_percent) or not 10 <= platform_logo_x_percent <= 90:
            raise ValueError("platform_logo_x_percent must be between 10 and 90")
        if not math.isfinite(platform_logo_y_percent) or not 5 <= platform_logo_y_percent <= 60:
            raise ValueError("platform_logo_y_percent must be between 5 and 60")
    if intro_card_cover_path is not None:
        if not math.isfinite(intro_card_cover_start) or intro_card_cover_start < 0:
            raise ValueError("intro_card_cover_start must be non-negative and finite")
        if not math.isfinite(intro_card_cover_duration) or intro_card_cover_duration <= 0:
            raise ValueError(
                "intro_card_cover_duration must be a positive finite number"
            )
        if intro_card_cover_start + intro_card_cover_duration > target_duration + 1e-9:
            raise ValueError(
                "intro card cover display window cannot exceed target_duration"
            )
        for name, value in (
            ("intro_card_cover_x_percent", intro_card_cover_x_percent),
            ("intro_card_cover_y_percent", intro_card_cover_y_percent),
        ):
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100")
        for name, value in (
            ("intro_card_cover_width_percent", intro_card_cover_width_percent),
            ("intro_card_cover_height_percent", intro_card_cover_height_percent),
        ):
            if not math.isfinite(value) or not 1 <= value <= 100:
                raise ValueError(f"{name} must be between 1 and 100")
        if (
            not math.isfinite(intro_card_cover_rotation_degrees)
            or not -15 <= intro_card_cover_rotation_degrees <= 15
        ):
            raise ValueError(
                "intro_card_cover_rotation_degrees must be between -15 and 15"
            )

    effective_cover_path = (
        cover_path
        if cover_path is not None and (cover_intro_enabled or cover_outro_enabled)
        else None
    )
    if effective_cover_path is not None:
        cover_timings = {"end_card_duration": end_card_duration}
        if cover_intro_enabled:
            cover_timings.update(
                {
                    "cover_intro_start": cover_intro_start,
                    "cover_intro_duration": cover_intro_duration,
                }
            )
        for name, value in cover_timings.items():
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite when cover_path is set")
        if cover_intro_enabled and cover_intro_start < 0:
            raise ValueError("cover_intro_start must be non-negative")
        if cover_intro_enabled and not 1.5 <= cover_intro_duration <= 2.5:
            raise ValueError("cover_intro_duration must be between 1.5 and 2.5 seconds")
        if not 5.0 <= end_card_duration <= 7.0:
            raise ValueError("end_card_duration must be between 5 and 7 seconds")

        end_card_start = target_duration - end_card_duration
        cover_intro_end = cover_intro_start + cover_intro_duration
        if end_card_start < 0:
            raise ValueError("end_card_duration cannot exceed target_duration")
        if cover_intro_enabled and cover_intro_end > end_card_start + 1e-9:
            raise ValueError("cover intro must finish before the end card starts")

    segment_total = sum(segment.duration for segment in segments)
    transition_total = (
        VIDEO_FADE_SECONDS * max(0, len(segments) - 1)
        if video_transition == "fade"
        else 0.0
    )
    if video_transition == "fade" and len(segments) > 1:
        if any(segment.duration <= VIDEO_FADE_SECONDS + 1e-6 for segment in segments):
            raise MediaError(
                "Every video segment must be longer than the 0.2 second fade transition"
            )
    effective_segment_total = segment_total - transition_total
    if effective_segment_total + 1e-3 < target_duration:
        raise MediaError(
            f"Video segments cover {effective_segment_total:.3f}s after "
            f"{video_transition} transitions, shorter than "
            f"the {target_duration:.3f}s narration"
        )

    if music is None:
        music_path: Path | None = None
    elif isinstance(music, MusicPlan):
        music_path = music.path
    else:
        music_path = Path(music)
    output = Path(output_path)
    narration = Path(narration_path)
    subtitles = Path(subtitle_path)

    # Round up to an even dimension so common yuv420p/hardware paths can use
    # the intermediate frames without alignment surprises.  Scaling sigma by
    # the same factor preserves the apparent blur radius after upscaling.
    background_width = max(
        2,
        math.ceil(width / (2 * BACKGROUND_DOWNSCALE_FACTOR)) * 2,
    )
    background_height = max(
        2,
        math.ceil(height / (2 * BACKGROUND_DOWNSCALE_FACTOR)) * 2,
    )
    background_blur_sigma = _ffmpeg_number(
        BACKGROUND_BLUR_SIGMA / BACKGROUND_DOWNSCALE_FACTOR
    )

    command: list[str] = [str(ffmpeg_path), "-hide_banner", "-nostdin"]
    command.append("-y" if overwrite else "-n")
    for segment in segments:
        command.extend(["-i", str(segment.path)])
    narration_index = len(segments)
    command.extend(["-i", str(narration)])
    next_input_index = narration_index + 1
    music_index: int | None = None
    if music_path is not None:
        music_index = next_input_index
        next_input_index += 1
        # Infinite input looping is safe because both the graph and output are
        # explicitly trimmed to target_duration.
        command.extend(["-stream_loop", "-1", "-i", str(music_path)])
    cover_index: int | None = None
    if effective_cover_path is not None:
        cover_index = next_input_index
        next_input_index += 1
        # Image input options must precede the corresponding ``-i``.  Keeping
        # the path as its own argument supports spaces, Chinese characters,
        # and other platform-native filenames without filter-level escaping.
        command.extend(
            [
                "-loop",
                "1",
                "-framerate",
                str(fps),
                "-i",
                str(Path(effective_cover_path)),
            ]
        )
    intro_card_cover_index: int | None = None
    if intro_card_cover_path is not None:
        intro_card_cover_index = next_input_index
        next_input_index += 1
        # This is deliberately independent from ``cover_index``.  The latter
        # remains the full-screen intro/outro image, while this input is a
        # small, contain-fitted image inside the opening story card.
        command.extend(
            [
                "-loop",
                "1",
                "-framerate",
                str(fps),
                "-i",
                str(Path(intro_card_cover_path)),
            ]
        )
    platform_logo_index: int | None = None
    if platform_logo_path is not None:
        platform_logo_index = next_input_index
        command.extend(
            [
                "-loop",
                "1",
                "-framerate",
                str(fps),
                "-i",
                str(Path(platform_logo_path)),
            ]
        )

    graph: list[str] = []
    video_labels: list[str] = []
    color_grade_filter = COLOR_GRADE_FILTERS[color_grade]
    color_grade_suffix = f",{color_grade_filter}" if color_grade_filter else ""
    fade_cfr_suffix = (
        f",fps={fps},settb=AVTB" if video_transition == "fade" else ""
    )
    # FFmpeg's fps filter can finish one or two target frames early after a
    # preceding scale/overlay chain (for example 598 instead of 600 frames for
    # a 10 s 30->60 conversion).  Pad defensively, then trim to the exact
    # planned segment duration.  This prevents tiny losses from accumulating
    # across a long concat while never extending the finished composition.
    fps_tail_pad = _ffmpeg_number(2.0 / fps + 1e-6)
    for index, segment in enumerate(segments):
        start = _ffmpeg_number(segment.start_time)
        source_span = _ffmpeg_number(segment.source_span)
        segment_duration = _ffmpeg_number(segment.duration)
        mirror_filter = ",hflip" if segment.mirror else ""
        setpts_filter = (
            "setpts=PTS-STARTPTS"
            if abs(segment.speed - 1.0) <= 1e-9
            else f"setpts=(PTS-STARTPTS)/{_ffmpeg_number(segment.speed)}"
        )
        foreground_width = max(
            2, math.ceil(width * segment.crop_scale / 2) * 2
        )
        foreground_height = max(
            2, math.ceil(height * segment.crop_scale / 2) * 2
        )
        source_prefix = (
            f"[{index}:v:0]trim=start={start}:duration={source_span},"
            f"{setpts_filter}{mirror_filter}"
        )
        if segment.fills_canvas(width, height):
            # A display-oriented vertical source (or a subtle crop variation
            # that already overscans the frame) completely hides the blurred
            # bed.  Avoid decoding a duplicate branch, blurring it and then
            # covering every pixel with the foreground again.
            graph.append(
                f"{source_prefix},"
                f"scale={foreground_width}:{foreground_height}:"
                "force_original_aspect_ratio=decrease,"
                f"crop={width}:{height}:x='(iw-ow)/2':y='(ih-oh)/2',"
                f"setsar=1,format=yuv420p{color_grade_suffix},fps={fps},"
                f"tpad=stop_mode=clone:stop_duration={fps_tail_pad},"
                f"trim=duration={segment_duration},setpts=PTS-STARTPTS"
                f"{fade_cfr_suffix}[v{index}]"
            )
        else:
            graph.append(
                f"{source_prefix},split=2[fg{index}][bg{index}]"
            )
            graph.append(
                f"[bg{index}]scale={background_width}:{background_height}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={background_width}:{background_height},"
                f"gblur=sigma={background_blur_sigma}:steps=2,"
                f"scale={width}:{height}:flags=bilinear[bgp{index}]"
            )
            graph.append(
                f"[fg{index}]scale={foreground_width}:{foreground_height}:"
                f"force_original_aspect_ratio=decrease"
                f"[fgp{index}]"
            )
            graph.append(
                f"[bgp{index}][fgp{index}]overlay=(W-w)/2:(H-h)/2,"
                f"setsar=1,format=yuv420p{color_grade_suffix},fps={fps},"
                f"tpad=stop_mode=clone:stop_duration={fps_tail_pad},"
                f"trim=duration={segment_duration},setpts=PTS-STARTPTS"
                f"{fade_cfr_suffix}[v{index}]"
            )
        video_labels.append(f"[v{index}]")

    if video_transition == "cut" or len(segments) == 1:
        graph.append(
            "".join(video_labels)
            + f"concat=n={len(segments)}:v=1:a=0[joined]"
        )
    else:
        timeline_duration = segments[0].duration
        current_label = "v0"
        for index, segment in enumerate(segments[1:], start=1):
            transition_offset = timeline_duration - VIDEO_FADE_SECONDS
            output_label = f"xfade{index}"
            graph.append(
                f"[{current_label}][v{index}]"
                "xfade=transition=fade:"
                f"duration={_ffmpeg_number(VIDEO_FADE_SECONDS)}:"
                f"offset={_ffmpeg_number(transition_offset)},fps={fps},settb=AVTB"
                f"[{output_label}]"
            )
            current_label = output_label
            timeline_duration += segment.duration - VIDEO_FADE_SECONDS
        graph.append(
            f"[{current_label}]trim=duration={_ffmpeg_number(target_duration)},"
            "setpts=PTS-STARTPTS[joined]"
        )
    has_post_ass_overlay = (
        intro_card_cover_index is not None or platform_logo_index is not None
    )
    ass_output_label = "subtitled" if has_post_ass_overlay else "vout"
    if intro_card_cover_index is not None:
        # Keep the complete portrait visible inside a transparent box.  The
        # box scales with preview/output resolution, and rotation expands the
        # alpha canvas instead of clipping or stretching the artwork.
        card_cover_width = max(
            2,
            round(width * intro_card_cover_width_percent / 200.0) * 2,
        )
        card_cover_height = max(
            2,
            round(height * intro_card_cover_height_percent / 200.0) * 2,
        )
        card_cover_center_x = round(width * intro_card_cover_x_percent / 100.0)
        card_cover_center_y = round(height * intro_card_cover_y_percent / 100.0)
        card_cover_rotation = _ffmpeg_number(
            math.radians(intro_card_cover_rotation_degrees)
        )
        card_cover_start = _ffmpeg_number(intro_card_cover_start)
        card_cover_end = _ffmpeg_number(
            intro_card_cover_start + intro_card_cover_duration
        )
        graph.append(
            f"[{intro_card_cover_index}:v:0]fps={fps},"
            f"scale={card_cover_width}:{card_cover_height}:"
            "force_original_aspect_ratio=decrease,format=rgba,"
            f"pad={card_cover_width}:{card_cover_height}:"
            "(ow-iw)/2:(oh-ih)/2:color=black@0,"
            f"rotate={card_cover_rotation}:ow=rotw(iw):oh=roth(ih):c=none"
            "[intro_card_cover]"
        )
    if platform_logo_index is not None:
        # The brand mark is the visual anchor of the centred story card.  Keep
        # its own box centred on the physical frame (rather than on an
        # asymmetric UI-safe canvas) and scale it identically in 540p samples
        # and 1080p exports.
        logo_box = max(24, round(width * (64 / 1080)))
        requested_logo_center = round(width * platform_logo_x_percent / 100.0)
        safe_logo_margin = round(width * (188 / 1080))
        logo_x = max(
            safe_logo_margin,
            min(width - safe_logo_margin - logo_box, requested_logo_center - round(logo_box / 2)),
        )
        logo_y = max(
            round(height * (150 / 1920)),
            min(
                height - round(height * (360 / 1920)) - logo_box,
                round(height * platform_logo_y_percent / 100.0),
            ),
        )
        logo_start = _ffmpeg_number(platform_logo_start)
        logo_end = _ffmpeg_number(platform_logo_start + platform_logo_duration)
        graph.append(
            f"[{platform_logo_index}:v:0]fps={fps},"
            f"scale={logo_box}:{logo_box}:force_original_aspect_ratio=decrease,"
            f"pad={logo_box}:{logo_box}:(ow-iw)/2:(oh-ih)/2:color=white@0,"
            "format=rgba[platform_logo]"
        )
    escaped_subtitles = escape_filter_path(subtitles)
    if cover_index is None:
        if end_card_without_cover:
            end_start = _ffmpeg_number(target_duration - end_card_duration)
            end_fade_duration = _ffmpeg_number(min(0.5, end_card_duration / 4))
            end_progress = (
                f"clip((t-{end_start})/{end_fade_duration},0,1)"
            )
            # Apply the closing grade directly to each primary frame.  A split
            # branch trimmed from ``end_start`` cannot produce its first frame
            # until the decoder reaches the ending and therefore recreates the
            # same framesync backlog even when a tpad is appended afterwards.
            graph.append(
                "[joined]eq="
                f"brightness='-0.24*{end_progress}':"
                f"saturation='1-0.28*{end_progress}':eval=frame[end_composited]"
            )
            graph.append(
                f"[end_composited]ass=filename='{escaped_subtitles}'"
                f"[{ass_output_label}]"
            )
        else:
            graph.append(
                f"[joined]ass=filename='{escaped_subtitles}'[{ass_output_label}]"
            )
    else:
        intro_start = _ffmpeg_number(cover_intro_start)
        intro_duration = _ffmpeg_number(cover_intro_duration)
        intro_fade_duration_value = min(0.35, cover_intro_duration / 4)
        intro_fade_duration = _ffmpeg_number(intro_fade_duration_value)
        end_start_value = target_duration - end_card_duration
        end_start = _ffmpeg_number(end_start_value)
        end_duration = _ffmpeg_number(end_card_duration)
        end_fade_duration = _ffmpeg_number(min(0.5, end_card_duration / 4))
        cover_width = max(2, int(width * 0.78) // 2 * 2)
        cover_height = max(2, int(height * 0.82) // 2 * 2)

        # Every motion expression is a pure function of presentation time.
        # This keeps random-access preview frames and the final FFmpeg render
        # deterministic.  The intro intentionally keeps the complete cover on
        # a softly blurred bed; the closing shot below uses a separate
        # full-bleed crop, as requested for the final narrated CTA.
        # Cover scenes are animated on a local zero-based timeline.  Their
        # placement on the story timeline happens later via a transparent
        # prefix, so no overlay input starts at a future timestamp.
        intro_progress = f"clip(t/{intro_duration},0,1)"
        intro_ease = f"(0.5-0.5*cos(PI*{intro_progress}))"
        intro_zoom_amount = {
            "none": 0.0,
            "fade": 0.0,
            "gentle_push": 0.025,
            "gentle_pull": 0.02,
            "slow_pan": 0.015,
            "soft_parallax": 0.018,
            "vertical_drift": 0.016,
            "focus_reveal": 0.018,
            "cinematic_push": 0.055,
            "ken_burns_left": 0.038,
            "ken_burns_right": 0.038,
            "soft_flash": 0.02,
        }[cover_animation]
        intro_zoom = (
            f"1+{_ffmpeg_number(intro_zoom_amount)}*"
            f"{intro_ease}"
        )
        end_progress = f"clip(t/{_ffmpeg_number(end_card_duration)},0,1)"
        # Cosine easing mirrors the restrained, non-spring camera moves used by
        # modern mobile editors without depending on any external NLE.
        end_ease = f"(0.5-0.5*cos(PI*{end_progress}))"
        end_zoom = {
            "none": "1",
            "fade": "1",
            "gentle_push": f"1+0.065*{end_ease}",
            "gentle_pull": f"1.075-0.055*{end_ease}",
            "slow_pan": "1.075",
            "soft_parallax": "1.06+0.006*sin(t*1.1)",
            "vertical_drift": "1.09",
            "focus_reveal": f"1.05-0.025*{end_ease}",
            "cinematic_push": f"1+0.095*{end_ease}",
            "ken_burns_left": "1.11",
            "ken_burns_right": "1.11",
            "soft_flash": f"1+0.035*{end_ease}",
        }[cover_animation]
        end_crop_x = "(iw-ow)/2"
        end_crop_y = "(ih-oh)/2"
        if cover_animation == "slow_pan":
            end_crop_x = f"(iw-ow)*(0.18+0.64*{end_ease})"
        elif cover_animation == "soft_parallax":
            end_crop_x += "+(iw-ow)*0.14*sin(t*0.82)"
            end_crop_y += "+(ih-oh)*0.10*cos(t*0.67)"
        elif cover_animation == "vertical_drift":
            end_crop_y = f"(ih-oh)*(0.12+0.76*{end_ease})"
        elif cover_animation == "ken_burns_left":
            end_crop_x = f"(iw-ow)*(0.84-0.68*{end_ease})"
        elif cover_animation == "ken_burns_right":
            end_crop_x = f"(iw-ow)*(0.16+0.68*{end_ease})"
        intro_overlay_x = "(W-w)/2"
        intro_overlay_y = "(H-h)/2"
        if cover_animation == "soft_parallax":
            intro_overlay_x += "+8*sin(t*1.7)"
            intro_overlay_y += "+6*cos(t*1.3)"
        elif cover_animation == "vertical_drift":
            intro_overlay_y = f"'(H-h)/2+14*(0.5-{intro_ease})'"
        elif cover_animation == "ken_burns_left":
            intro_overlay_x = f"'(W-w)/2+16*(0.5-{intro_ease})'"
        elif cover_animation == "ken_burns_right":
            intro_overlay_x = f"'(W-w)/2+16*({intro_ease}-0.5)'"
        intro_fades = (
            ""
            if cover_animation == "none"
            else (
                f",fade=t=in:st=0:d={intro_fade_duration}:color=white"
                f",fade=t=out:st={_ffmpeg_number(cover_intro_duration - intro_fade_duration_value)}:"
                f"d={intro_fade_duration}:alpha=1"
            )
            if cover_animation == "soft_flash"
            else (
                f",fade=t=in:st=0:d={intro_fade_duration}:alpha=1"
                f",fade=t=out:st={_ffmpeg_number(cover_intro_duration - intro_fade_duration_value)}:"
                f"d={intro_fade_duration}:alpha=1"
            )
        )
        end_fade = (
            ""
            if cover_animation == "none"
            else f",fade=t=in:st=0:d={end_fade_duration}:color=white"
            if cover_animation == "soft_flash"
            else f",fade=t=in:st=0:d={end_fade_duration}:alpha=1"
        )
        if cover_intro_enabled and cover_outro_enabled:
            graph.append(
                f"[{cover_index}:v:0]fps={fps},split=2"
                "[cover_intro_window_source][cover_end_window_source]"
            )
        elif cover_intro_enabled:
            graph.append(
                f"[{cover_index}:v:0]fps={fps}[cover_intro_window_source]"
            )
        elif cover_outro_enabled:
            graph.append(
                f"[{cover_index}:v:0]fps={fps}[cover_end_window_source]"
            )

        if cover_outro_enabled:
            graph.append(
                f"[cover_end_window_source]trim=duration={end_duration},"
                "setpts=PTS-STARTPTS[cover_end_source]"
            )

        if cover_intro_enabled:
            graph.append(
                f"[cover_intro_window_source]trim=duration={intro_duration},"
                "setpts=PTS-STARTPTS,split=2"
                "[cover_intro_bg_source][cover_intro_fg_source]"
            )
            graph.append(
                f"[cover_intro_bg_source]scale={background_width}:{background_height}:"
                "force_original_aspect_ratio=increase,"
                f"crop={background_width}:{background_height},"
                f"gblur=sigma={background_blur_sigma}:steps=2,"
                f"scale={width}:{height}:flags=bilinear,setsar=1,format=rgba"
                "[cover_intro_bg]"
            )
            graph.append(
                f"[cover_intro_fg_source]scale={cover_width}:{cover_height}:"
                "force_original_aspect_ratio=decrease,setsar=1,format=rgba"
                "[cover_intro_fg_base]"
            )
            graph.append(
                "[cover_intro_fg_base]"
                f"scale=w='trunc(iw*({intro_zoom})/2)*2':"
                f"h='trunc(ih*({intro_zoom})/2)*2':eval=frame"
                "[cover_intro_fg]"
            )
            if cover_animation == "focus_reveal":
                intro_focus_duration = _ffmpeg_number(
                    min(0.55, cover_intro_duration * 0.32)
                )
                graph.append(
                    "[cover_intro_bg][cover_intro_fg]"
                    f"overlay={intro_overlay_x}:{intro_overlay_y}:shortest=1,"
                    f"setsar=1{color_grade_suffix},format=rgba[cover_intro_focus_base]"
                )
                # Blur the fixed-size composed frame, not the dynamically
                # scaled foreground.  FFmpeg cannot safely renegotiate two
                # split overlay inputs when their dimensions change on
                # different frames; composing first keeps this effect stable
                # while still preserving the animated camera move.
                graph.append(
                    "[cover_intro_focus_base]split=2"
                    "[cover_intro_sharp][cover_intro_blur_source]"
                )
                graph.append(
                    "[cover_intro_blur_source]gblur=sigma=9:steps=2,"
                    f"fade=t=out:st=0:d={intro_focus_duration}:alpha=1"
                    "[cover_intro_blur]"
                )
                graph.append(
                    "[cover_intro_sharp][cover_intro_blur]"
                    "overlay=0:0:shortest=1:format=auto,format=rgba"
                    f"{intro_fades}[cover_intro_scene]"
                )
            else:
                graph.append(
                    "[cover_intro_bg][cover_intro_fg]"
                    f"overlay={intro_overlay_x}:{intro_overlay_y}:shortest=1,"
                    f"setsar=1{color_grade_suffix},format=rgba"
                    f"{intro_fades}"
                    "[cover_intro_scene]"
                )
            graph.append(
                "[cover_intro_scene]"
                f"tpad=start_mode=add:start_duration={intro_start}:color=black@0.0"
                "[cover_intro_timeline]"
            )
            graph.append(
                "[joined][cover_intro_timeline]"
                "overlay=(W-w)/2:(H-h)/2:"
                "eof_action=pass:repeatlast=0[cover_intro_composited]"
            )
            cover_base_label = "cover_intro_composited"
        else:
            cover_base_label = "joined"

        if cover_outro_enabled:
            # The closing cover is a true 9:16 full-bleed shot. ``increase``
            # plus the centred crop deliberately removes letterboxing; a small
            # overscan then supplies enough room for push, pull and pan presets.
            graph.append(
                f"[cover_end_source]scale={width}:{height}:"
                "force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1{color_grade_suffix},"
                "format=rgba[cover_end_base]"
            )
            end_motion_label = (
                "cover_end_motion"
                if cover_animation == "focus_reveal"
                else "cover_end_scene"
            )
            graph.append(
                "[cover_end_base]"
                f"scale=w='trunc(iw*({end_zoom})/2)*2':"
                f"h='trunc(ih*({end_zoom})/2)*2':eval=frame,"
                f"crop={width}:{height}:x='{end_crop_x}':y='{end_crop_y}',"
                "setsar=1,format=rgba"
                f"{'' if cover_animation == 'focus_reveal' else end_fade}"
                f"[{end_motion_label}]"
            )
            if cover_animation == "focus_reveal":
                end_focus_duration = _ffmpeg_number(
                    min(0.8, end_card_duration * 0.16)
                )
                graph.append(
                    "[cover_end_motion]split=2"
                    "[cover_end_sharp][cover_end_blur_source]"
                )
                graph.append(
                    "[cover_end_blur_source]gblur=sigma=11:steps=2,"
                    f"fade=t=out:st=0:d={end_focus_duration}:alpha=1"
                    "[cover_end_blur]"
                )
                graph.append(
                    "[cover_end_sharp][cover_end_blur]"
                    "overlay=0:0:shortest=1:format=auto,format=rgba"
                    f"{end_fade}[cover_end_scene]"
                )
            graph.append(
                "[cover_end_scene]"
                f"tpad=start_mode=add:start_duration={end_start}:color=black@0.0"
                "[cover_end_timeline]"
            )
            graph.append(
                f"[{cover_base_label}][cover_end_timeline]"
                "overlay=0:0:"
                "eof_action=pass:repeatlast=0[covered]"
            )
            cover_caption_base = "covered"
        elif end_card_without_cover:
            end_progress = (
                f"clip((t-{end_start})/{end_fade_duration},0,1)"
            )
            graph.append(
                f"[{cover_base_label}]eq="
                f"brightness='-0.24*{end_progress}':"
                f"saturation='1-0.28*{end_progress}':eval=frame[end_composited]"
            )
            cover_caption_base = "end_composited"
        else:
            cover_caption_base = cover_base_label
        # Subtitles carry both the persistent search-code card and the spoken
        # ending CTA, so they remain burned after either visual ending mode.
        graph.append(
            f"[{cover_caption_base}]ass=filename='{escaped_subtitles}'"
            f"[{ass_output_label}]"
        )

    post_ass_label = ass_output_label
    if intro_card_cover_index is not None:
        card_cover_output_label = (
            "intro_card_covered" if platform_logo_index is not None else "vout"
        )
        graph.append(
            f"[{post_ass_label}][intro_card_cover]overlay="
            f"x='max(0,min(W-w,{card_cover_center_x}-w/2))':"
            f"y='max(0,min(H-h,{card_cover_center_y}-h/2))':"
            f"enable='between(t,{card_cover_start},{card_cover_end})':"
            f"eof_action=pass[{card_cover_output_label}]"
        )
        post_ass_label = card_cover_output_label
    if platform_logo_index is not None:
        graph.append(
            f"[{post_ass_label}][platform_logo]overlay={logo_x}:{logo_y}:"
            f"enable='between(t,{logo_start},{logo_end})':eof_action=pass[vout]"
        )

    target = _ffmpeg_number(target_duration)
    if music_index is None:
        graph.append(
            f"[{narration_index}:a:0]aresample=48000,asetpts=PTS-STARTPTS,"
            f"apad=whole_dur={target},atrim=0:{target}[aout]"
        )
    else:
        graph.append(
            f"[{narration_index}:a:0]aresample=48000,asetpts=PTS-STARTPTS,"
            f"apad=whole_dur={target},atrim=0:{target},"
            "asplit=2[voice_sc][voice_mix]"
        )
        graph.append(
            f"[{music_index}:a:0]aresample=48000,asetpts=PTS-STARTPTS,"
            f"atrim=0:{target},volume={_ffmpeg_number(bgm_volume)}[bgm]"
        )
        # Duck the music enough to keep narration intelligible without making a
        # continuously narrated video sound as if it has no soundtrack.  The old
        # 10:1 compressor stayed active for almost the entire story and pushed a
        # 0.16 music bed to roughly -39 LUFS.  A gentler 4:1 curve at the higher
        # threshold keeps the same sample around -32 LUFS at the recommended 0.28
        # setting (about 8 LU below the narration).
        graph.append(
            "[bgm][voice_sc]sidechaincompress="
            "threshold=0.05:ratio=4:attack=20:release=250:makeup=1[ducked]"
        )
        # FFmpeg's amix normalises by the number of inputs by default, which was
        # reducing both the narration and already-ducked music by another ~6 dB.
        # Keep each input at its planned level and let the limiter catch the rare
        # coincident peak instead. ``bgm_volume`` still controls the music before
        # side-chain compression, so the user's setting remains authoritative.
        graph.append(
            "[voice_mix][ducked]amix=inputs=2:duration=first:"
            "dropout_transition=2:normalize=0,alimiter=limit=0.95[aout]"
        )
    filter_complex = ";".join(graph)

    if render_mode == "compatibility":
        encoder_options = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"]
    elif video_encoder == "h264_nvenc":
        encoder_options = [
            "-c:v", video_encoder,
            "-preset", "p3" if render_mode == "speed" else "p5",
            "-cq", "23" if render_mode == "speed" else "20",
            "-b:v", "0",
        ]
    elif video_encoder == "h264_qsv":
        encoder_options = [
            "-c:v", video_encoder,
            "-preset", "veryfast" if render_mode == "speed" else "medium",
            "-global_quality", "24" if render_mode == "speed" else "22",
        ]
    elif video_encoder == "h264_amf":
        encoder_options = [
            "-c:v", video_encoder, "-quality", "speed", "-rc", "cqp",
            "-qp_i", "22", "-qp_p", "24",
        ]
    else:
        encoder_options = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast" if render_mode == "speed" else "medium",
            "-crf",
            "22" if render_mode == "speed" else "20",
        ]

    # FFmpeg otherwise derives worker counts from every logical CPU.  On
    # employee PCs that can create dozens of filter and x264 threads while a
    # 1080x1920/60 graph is holding large frame buffers.  Bound the normal
    # path as well as compatibility mode; GPU encoders keep their own driver
    # managed worker count.
    filter_threads = "1" if render_mode == "compatibility" else "2"
    bounded_thread_options = [
        "-filter_threads",
        filter_threads,
        "-filter_complex_threads",
        filter_threads,
    ]
    effective_video_encoder = (
        "libx264" if render_mode == "compatibility" else video_encoder
    )
    encoder_thread_options = (
        ["-threads", "2"] if effective_video_encoder == "libx264" else []
    )
    command.extend(
        [
            *bounded_thread_options,
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-t",
            target,
            *encoder_options,
            *encoder_thread_options,
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return FFmpegPlan(tuple(command), filter_complex, target_duration, output)


def build_ffmpeg_command(*args: object, **kwargs: object) -> list[str]:
    """Compatibility convenience returning only the safe argument list."""

    return build_ffmpeg_plan(*args, **kwargs).as_list()  # type: ignore[arg-type]


def format_command(command: Sequence[str]) -> str:
    """Format an argument list for display/logging, never for shell execution."""

    return subprocess.list2cmdline(list(command))


def execute_ffmpeg(
    plan: FFmpegPlan | Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Execute an already-reviewed plan with ``shell=False``."""

    command = plan.as_list() if isinstance(plan, FFmpegPlan) else list(plan)
    return run_cancellable_process(
        command,
        runner=runner,
        check=check,
        shell=False,
    )


__all__ = [
    "AUDIO_EXTENSIONS",
    "COLOR_GRADE_FILTERS",
    "COVER_ANIMATIONS",
    "DEFAULT_USAGE_FILENAME",
    "FFmpegPlan",
    "MAX_PLAYBACK_SPEED",
    "MEDIA_INDEX_REFRESH_SECONDS",
    "MEDIA_LAZY_PROBE_LIMIT",
    "MIN_PLAYBACK_SPEED",
    "MOOD_ALIASES",
    "MediaError",
    "MusicPlan",
    "ProbeError",
    "VIDEO_EXTENSIONS",
    "VIDEO_FADE_SECONDS",
    "VIDEO_TRANSITIONS",
    "VideoAsset",
    "VideoSegment",
    "build_ffmpeg_command",
    "build_ffmpeg_plan",
    "build_low_memory_segment_plan",
    "canonical_mood",
    "clear_duration_cache",
    "clear_media_index_memory_cache",
    "discover_files",
    "escape_filter_path",
    "execute_ffmpeg",
    "format_command",
    "load_usage_record",
    "plan_video_segments",
    "probe_duration",
    "refresh_media_index",
    "save_usage_record",
    "select_music_asset",
    "select_video_assets",
    "summarize_video_usage",
]
