from __future__ import annotations

import json
import math
import os
import platform
import re
import shutil
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .failure_diagnostics import classify_failure, sanitize_failure_log


DEFAULT_MIN_AGE_DAYS = 30.0
DEFAULT_MAX_DELETE_BYTES = 512 * 1024**2
DEFAULT_MAX_LOG_FILES = 5
DEFAULT_LOG_TAIL_BYTES = 32 * 1024

_REMOVABLE_CATEGORIES = (
    "regenerable_cache",
    "diagnostic_logs",
    "update_rollback",
)
_ALL_CATEGORIES = (*_REMOVABLE_CATEGORIES, "protected")
_SOURCE_OR_OUTPUT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".doc",
        ".docx",
        ".pdf",
        ".epub",
        ".rtf",
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".webm",
        ".mp3",
        ".m4a",
        ".aac",
        ".flac",
        ".wav",
        ".ass",
        ".srt",
        ".vtt",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
    }
)
_SOURCE_DOCUMENT_SUFFIXES = frozenset(
    {".txt", ".md", ".doc", ".docx", ".pdf", ".epub", ".rtf"}
)
_CACHE_DIRECTORY_NAMES = frozenset(
    {"cache", "runtime-temp", "voice-previews", ".work", ".previews"}
)
_UPDATE_DIRECTORY_NAMES = frozenset(
    {"downloads", "staging", "rollback", "rollbacks", "backups"}
)
_SYSTEM_KEYS = frozenset(
    {
        "platform",
        "python",
        "machine",
        "os_name",
        "ffmpeg_ready",
        "encoders",
        "recommended_encoder",
        "webview_runtime",
        "embedded_kokoro_ready",
        "edge_tts_runtime_ready",
    }
)
_RESOURCE_KEYS = frozenset(
    {
        "memory",
        "data_disk",
        "work_disk",
        "output_disk",
        "storage",
        "observable",
        "total_bytes",
        "free_bytes",
        "available_bytes",
        "low",
        "low_space",
        "admission_allowed",
        "accepting_new_work",
        "state",
        "health_state",
        "reason",
        "heavy_task_limit",
        "active_heavy_tasks",
        "retry_in_seconds",
        "cooling_until_unix",
    }
)
_REQUEST_BODY_HINT_RE = re.compile(
    r"(?i)(?:^|[\s{,\[])[\"']?(?:request(?:[_ -]?body)?|body|prompt|messages?|"
    r"content|input(?:[_ -]?text)?|source[_ -]?text|story|manuscript|"
    r"novel(?:[_ -]?text)?)[\"']?\s*[:=]"
)
_EXCEPTION_TYPE_RE = re.compile(
    r"^\s*(?P<type>(?:[A-Za-z_][A-Za-z0-9_.]*)(?:Error|Exception|Exit|"
    r"Interrupt|Warning))\s*(?::|$)"
)
_TRACEBACK_FILE_RE = re.compile(
    r"^\s*File\s+(?:[\"']?)<path>(?:[\"']?)"
    r"(?:,\s*line\s*(?P<line>\d+))?"
    r"(?:,\s*in\s*(?P<function>[A-Za-z_][A-Za-z0-9_.<>]*))?\s*$",
    re.IGNORECASE,
)
_PATH_ONLY_RE = re.compile(
    r"^\s*(?:source|input|output|path|file|log|error[_ -]?log)\s*[:=]\s*"
    r"<path>\s*$",
    re.IGNORECASE,
)
_STAGE_RE = re.compile(
    r"^\s*(?:stage|phase|attempt[_ -]?label)\s*[:=]\s*"
    r"(?P<value>[A-Za-z0-9_. -]{1,64})\s*$",
    re.IGNORECASE,
)
_INTEGER_DIAGNOSTIC_RE = re.compile(
    r"^\s*(?P<key>return[_ -]?code|exit[_ -]?code|errno|winerror|attempt)"
    r"\s*[:=]\s*(?P<value>-?\d{1,10})\s*$",
    re.IGNORECASE,
)
_HTTP_STATUS_RE = re.compile(
    r"(?i)\bHTTP(?:\s+status)?\s*[:=]?\s*(?P<status>[1-5]\d{2})\b"
)
_ERRNO_RE = re.compile(r"(?i)\[(?:Errno|WinError)\s+(?P<code>-?\d{1,10})\]")
_FFMPEG_SUMMARY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), summary)
    for pattern, summary in (
        (r"\bno such file or directory\b", "FFmpeg no such file or directory"),
        (
            r"\binvalid data found when processing input\b",
            "FFmpeg invalid data found when processing input",
        ),
        (r"\bmoov atom not found\b", "FFmpeg moov atom not found"),
        (
            r"\bcould not find codec parameters\b",
            "FFmpeg could not find codec parameters",
        ),
        (r"\berror while decoding\b", "FFmpeg error while decoding"),
        (r"\bpermission denied\b", "FFmpeg permission denied"),
        (r"\bno space left on device\b", "FFmpeg no space left on device"),
        (
            r"\bresource temporarily unavailable\b",
            "FFmpeg resource temporarily unavailable",
        ),
        (r"\bcannot allocate memory\b", "FFmpeg cannot allocate memory"),
        (r"\bout of memory\b", "FFmpeg out of memory"),
        (
            r"\berror (?:initializing|reinitializing) (?:complex )?filters?\b",
            "FFmpeg error initializing filters",
        ),
        (
            r"\bfailed to configure output pad\b",
            "FFmpeg failed to configure output pad",
        ),
        (r"\bno such filter\b", "FFmpeg no such filter"),
        (
            r"\berror while opening encoder\b",
            "FFmpeg error while opening encoder",
        ),
        (
            r"\berror initializing output stream\b",
            "FFmpeg error initializing output stream",
        ),
        (r"\bunknown encoder\b", "FFmpeg unknown encoder"),
        (r"\bcannot load nvcuda\b", "FFmpeg cannot load nvcuda"),
        (
            r"\bno capable devices found\b",
            "FFmpeg no capable devices found",
        ),
        (r"\bdevice setup failed\b", "FFmpeg device setup failed"),
        (
            r"\bfailed to initiali[sz]e nvenc\b",
            "FFmpeg failed to initialize NVENC",
        ),
    )
)


@dataclass(frozen=True, slots=True)
class _StorageEntry:
    path: Path
    root: Path
    category: str
    size: int
    modified_ns: int
    device: int
    inode: int


@dataclass(slots=True)
class _ScanResult:
    entries: list[_StorageEntry]
    skipped_links: int = 0
    unreadable_entries: int = 0


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(os.path, "isjunction", None)
        return bool(is_junction and is_junction(path))
    except OSError:
        return True


def _has_link_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists() and _is_link_or_junction(current):
            return True
    return False


def _validated_root(value: str | Path, label: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ValueError(f"{label} must be an absolute directory")
    absolute = raw.absolute()
    if absolute.parent == absolute or absolute == Path(absolute.anchor):
        raise ValueError(f"{label} cannot be a filesystem root")
    if _has_link_component(absolute):
        raise ValueError(f"{label} cannot contain a symlink or junction")
    try:
        resolved = absolute.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError(f"{label} does not exist") from error
    if not resolved.is_dir() or _is_link_or_junction(resolved):
        raise ValueError(f"{label} must be a normal directory")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _diagnostic_name(name: str) -> bool:
    lowered = name.casefold()
    return bool(
        lowered.endswith((".log", ".trace"))
        or lowered.startswith(("render-command", "ffmpeg-command"))
    )


def _category_for(
    path: Path,
    root: Path,
    root_kind: str,
    protected_paths: frozenset[Path],
) -> str:
    relative = path.relative_to(root)
    parts = tuple(part.casefold() for part in relative.parts)
    name = path.name.casefold()
    suffix = path.suffix.casefold()
    if path in protected_paths:
        return "protected"
    if _diagnostic_name(name):
        return "diagnostic_logs"
    in_known_cache = bool(
        (root_kind == "data" and parts and parts[0] in _CACHE_DIRECTORY_NAMES)
        or (root_kind == "work" and any(part in _CACHE_DIRECTORY_NAMES for part in parts[:-1]))
    )
    if in_known_cache:
        if suffix in _SOURCE_DOCUMENT_SUFFIXES:
            return "protected"
        explicit_intermediate = bool(
            any(part in {".work", ".previews"} for part in parts[:-1])
            or (root_kind == "data" and parts and parts[0] in {"runtime-temp", "voice-previews"})
        )
        if suffix in _SOURCE_OR_OUTPUT_SUFFIXES and not explicit_intermediate:
            return "protected"
        return "regenerable_cache"
    if root_kind == "data" and parts and parts[0] == "updates":
        if any(part in _UPDATE_DIRECTORY_NAMES for part in parts[1:-1]) or suffix in {
            ".bak",
            ".old",
            ".rollback",
            ".zip",
        }:
            return "update_rollback"
    if suffix in _SOURCE_OR_OUTPUT_SUFFIXES:
        return "protected"
    return "protected"


def _pending_update_paths(data_root: Path) -> frozenset[Path]:
    marker = data_root / "updates" / "pending-update.json"
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        candidate = Path(str(payload.get("package_path") or "")).expanduser()
        if not candidate.is_absolute() or _is_link_or_junction(candidate):
            return frozenset()
        resolved = candidate.resolve(strict=False)
        return frozenset({resolved}) if _is_within(resolved, data_root) else frozenset()
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return frozenset()


def _scan_storage(data_dir: str | Path, work_dir: str | Path | None) -> _ScanResult:
    data_root = _validated_root(data_dir, "data_dir")
    roots: list[tuple[str, Path]] = [("data", data_root)]
    if work_dir is None:
        automatic_work_root = data_root / "render-work"
        if automatic_work_root.is_dir() and not _is_link_or_junction(automatic_work_root):
            roots.append(("work", _validated_root(automatic_work_root, "work_dir")))
    else:
        work_root = _validated_root(work_dir, "work_dir")
        if work_root != data_root:
            roots.append(("work", work_root))
    protected_paths = _pending_update_paths(data_root)
    result = _ScanResult(entries=[])

    for root_kind, root in roots:
        excluded_roots = {
            other
            for _other_kind, other in roots
            if other != root and _is_within(other, root)
        }

        def on_error(_error: OSError) -> None:
            result.unreadable_entries += 1

        for current_raw, directory_names, file_names in os.walk(
            root, topdown=True, onerror=on_error, followlinks=False
        ):
            current = Path(current_raw)
            safe_directories: list[str] = []
            for directory_name in directory_names:
                candidate = current / directory_name
                try:
                    if _is_link_or_junction(candidate):
                        result.skipped_links += 1
                        continue
                    resolved = candidate.resolve(strict=True)
                    if not _is_within(resolved, root):
                        result.skipped_links += 1
                        continue
                    if resolved in excluded_roots:
                        continue
                    safe_directories.append(directory_name)
                except (OSError, RuntimeError):
                    result.unreadable_entries += 1
            directory_names[:] = safe_directories

            for file_name in file_names:
                candidate = current / file_name
                try:
                    if _is_link_or_junction(candidate):
                        result.skipped_links += 1
                        continue
                    resolved = candidate.resolve(strict=True)
                    if not _is_within(resolved, root):
                        result.skipped_links += 1
                        continue
                    metadata = candidate.stat(follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    result.entries.append(
                        _StorageEntry(
                            path=resolved,
                            root=root,
                            category=_category_for(
                                resolved,
                                root,
                                root_kind,
                                protected_paths,
                            ),
                            size=max(0, int(metadata.st_size)),
                            modified_ns=int(metadata.st_mtime_ns),
                            device=int(metadata.st_dev),
                            inode=int(metadata.st_ino),
                        )
                    )
                except (OSError, RuntimeError, ValueError):
                    result.unreadable_entries += 1
    return result


def _empty_category_totals(categories: tuple[str, ...] = _ALL_CATEGORIES) -> dict[str, dict[str, int]]:
    return {category: {"files": 0, "bytes": 0} for category in categories}


def _totals(entries: list[_StorageEntry]) -> dict[str, dict[str, int]]:
    totals = _empty_category_totals()
    for entry in entries:
        group = totals[entry.category]
        group["files"] += 1
        group["bytes"] += entry.size
    return totals


def inspect_local_storage(
    data_dir: str | Path,
    work_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Classify local storage without exposing or mutating individual paths."""

    scan = _scan_storage(data_dir, work_dir)
    categories = _totals(scan.entries)
    removable = {
        "files": sum(categories[name]["files"] for name in _REMOVABLE_CATEGORIES),
        "bytes": sum(categories[name]["bytes"] for name in _REMOVABLE_CATEGORIES),
    }
    return {
        "schema_version": 1,
        "categories": categories,
        "removable": removable,
        "skipped_links": scan.skipped_links,
        "unreadable_entries": scan.unreadable_entries,
    }


def _validated_cleanup_limits(
    min_age_days: float,
    max_delete_bytes: int,
) -> tuple[float, int]:
    try:
        age = float(min_age_days)
        quota = int(max_delete_bytes)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("cleanup limits are invalid") from error
    if not math.isfinite(age) or age < 0:
        raise ValueError("min_age_days must be finite and non-negative")
    if quota < 0:
        raise ValueError("max_delete_bytes must be non-negative")
    return age, quota


def _selected_entries(
    entries: list[_StorageEntry],
    *,
    min_age_days: float,
    max_delete_bytes: int,
) -> list[_StorageEntry]:
    if max_delete_bytes == 0:
        return []
    cutoff_ns = int((time.time() - min_age_days * 24 * 60 * 60) * 1_000_000_000)
    candidates = sorted(
        (
            entry
            for entry in entries
            if entry.category in _REMOVABLE_CATEGORIES
            and entry.modified_ns <= cutoff_ns
        ),
        key=lambda entry: (entry.modified_ns, str(entry.path).casefold()),
    )
    selected: list[_StorageEntry] = []
    selected_bytes = 0
    for entry in candidates:
        if entry.size > max_delete_bytes - selected_bytes:
            continue
        selected.append(entry)
        selected_bytes += entry.size
    return selected


def _entry_unchanged_and_safe(entry: _StorageEntry) -> bool:
    try:
        if _is_link_or_junction(entry.path):
            return False
        resolved = entry.path.resolve(strict=True)
        if resolved != entry.path or not _is_within(resolved, entry.root):
            return False
        metadata = entry.path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            return False
        if int(metadata.st_size) != entry.size or int(metadata.st_mtime_ns) != entry.modified_ns:
            return False
        if entry.device and int(metadata.st_dev) != entry.device:
            return False
        if entry.inode and int(metadata.st_ino) != entry.inode:
            return False
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _selection_totals(entries: list[_StorageEntry]) -> dict[str, Any]:
    categories = _empty_category_totals(_REMOVABLE_CATEGORIES)
    for entry in entries:
        categories[entry.category]["files"] += 1
        categories[entry.category]["bytes"] += entry.size
    return {
        "files": len(entries),
        "bytes": sum(entry.size for entry in entries),
        "categories": categories,
    }


def cleanup_local_storage(
    data_dir: str | Path,
    work_dir: str | Path | None = None,
    *,
    confirm: bool = False,
    min_age_days: float = DEFAULT_MIN_AGE_DAYS,
    max_delete_bytes: int = DEFAULT_MAX_DELETE_BYTES,
) -> dict[str, Any]:
    """Plan or apply a bounded cleanup; deletion requires ``confirm is True``."""

    age, quota = _validated_cleanup_limits(min_age_days, max_delete_bytes)
    scan = _scan_storage(data_dir, work_dir)
    selected = _selected_entries(
        scan.entries,
        min_age_days=age,
        max_delete_bytes=quota,
    )
    selected_totals = _selection_totals(selected)
    dry_run = confirm is not True
    deleted_entries: list[_StorageEntry] = []
    skipped_changed = 0
    errors: list[str] = []
    if not dry_run:
        for entry in selected:
            if not _entry_unchanged_and_safe(entry):
                skipped_changed += 1
                continue
            try:
                entry.path.unlink()
                deleted_entries.append(entry)
            except OSError as error:
                errors.append(type(error).__name__)
    return {
        "schema_version": 1,
        "dry_run": dry_run,
        "confirmed": not dry_run,
        "min_age_days": age,
        "max_delete_bytes": quota,
        "selected": selected_totals,
        "deleted": _selection_totals(deleted_entries),
        "skipped_links": scan.skipped_links,
        "skipped_changed": skipped_changed,
        "unreadable_entries": scan.unreadable_entries,
        "errors": errors,
    }


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return sanitize_failure_log(value)[:500]
    if isinstance(value, (list, tuple)):
        return [_safe_scalar(item) for item in value[:32]]
    return None


def _safe_system_overview(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _safe_scalar(item)
        for key, item in value.items()
        if str(key) in _SYSTEM_KEYS
    }


def _safe_resource_status(value: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        if key not in _RESOURCE_KEYS:
            continue
        if isinstance(item, Mapping):
            safe[key] = _safe_resource_status(item)
        else:
            safe[key] = _safe_scalar(item)
    return safe


def _disk_summary(path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        return {
            "observable": True,
            "total_bytes": int(usage.total),
            "free_bytes": int(usage.free),
        }
    except OSError:
        return {"observable": False, "total_bytes": 0, "free_bytes": 0}


def _append_unique(lines: list[str], value: str) -> None:
    if value and value not in lines:
        lines.append(value)


def _structured_diagnostic_lines(decoded: str) -> list[str]:
    """Extract only fixed-shape operational evidence from an untrusted log.

    Error logs can contain complete provider requests and novel prose.  A
    sanitizing blacklist is therefore insufficient: this parser emits only
    recognized exception types, redacted traceback locations, bounded numeric
    process context, HTTP/OS error codes, and canonical FFmpeg summaries.
    """

    lines: list[str] = []
    classification = classify_failure(decoded)
    if classification != "unknown":
        _append_unique(lines, f"error.classification: {classification}")

    for raw_line in decoded.splitlines()[-200:]:
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        exception_match = _EXCEPTION_TYPE_RE.match(raw_line)
        if exception_match:
            _append_unique(
                lines,
                f"exception.type: {exception_match.group('type')}",
            )

        # Provider request bodies and model inputs are never diagnostic
        # evidence.  Drop the entire line even if it also contains words such
        # as "error" or a recognized FFmpeg phrase.
        if _REQUEST_BODY_HINT_RE.search(raw_line):
            continue

        safe_line = sanitize_failure_log(raw_line).strip()
        if not safe_line:
            continue
        if safe_line.casefold() == "traceback (most recent call last):":
            _append_unique(lines, "traceback: present")
            continue

        traceback_match = _TRACEBACK_FILE_RE.match(safe_line)
        if traceback_match:
            location = "traceback.file: <path>"
            if traceback_match.group("line"):
                location += f" line {traceback_match.group('line')}"
            if traceback_match.group("function"):
                location += f" in {traceback_match.group('function')}"
            _append_unique(lines, location)
            continue

        if _PATH_ONLY_RE.match(safe_line):
            _append_unique(lines, "path: <path>")
            continue

        stage_match = _STAGE_RE.match(safe_line)
        if stage_match:
            _append_unique(lines, f"stage: {stage_match.group('value').strip()}")
            continue

        integer_match = _INTEGER_DIAGNOSTIC_RE.match(safe_line)
        if integer_match:
            key = integer_match.group("key").casefold().replace(" ", "_").replace("-", "_")
            _append_unique(lines, f"{key}: {integer_match.group('value')}")
            continue

        errno_match = _ERRNO_RE.search(safe_line)
        if errno_match:
            _append_unique(lines, f"os.error_code: {errno_match.group('code')}")
        http_match = _HTTP_STATUS_RE.search(safe_line)
        if http_match:
            _append_unique(lines, f"http.status: {http_match.group('status')}")
        if "<path>" in safe_line:
            _append_unique(lines, "path: <path>")

        for pattern, summary in _FFMPEG_SUMMARY_PATTERNS:
            if pattern.search(safe_line):
                _append_unique(lines, f"ffmpeg.summary: {summary}")

    return lines[:200]


def _read_sanitized_log_tail(path: Path, max_bytes: int) -> str:
    try:
        size = path.stat().st_size
        offset = max(0, size - max_bytes)
        with path.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(max_bytes)
        decoded = raw.decode("utf-8", errors="replace")
        if offset and "\n" in decoded:
            decoded = decoded.split("\n", 1)[1]
    except OSError:
        return ""
    return "\n".join(_structured_diagnostic_lines(decoded)).strip()


def _zip_write_text(archive: zipfile.ZipFile, name: str, value: str) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    archive.writestr(info, value.encode("utf-8"))


def create_support_bundle(
    destination: str | Path,
    *,
    data_dir: str | Path,
    work_dir: str | Path | None = None,
    system_overview: Mapping[str, Any] | None = None,
    resource_status: Mapping[str, Any] | None = None,
    max_log_files: int = DEFAULT_MAX_LOG_FILES,
    log_tail_bytes: int = DEFAULT_LOG_TAIL_BYTES,
) -> Path:
    """Create an atomic, allowlisted ZIP with sanitized operational evidence."""

    scan = _scan_storage(data_dir, work_dir)
    data_root = _validated_root(data_dir, "data_dir")
    work_root = (
        _validated_root(work_dir, "work_dir") if work_dir is not None else None
    )
    try:
        log_limit = max(0, min(20, int(max_log_files)))
        tail_limit = max(1, min(64 * 1024, int(log_tail_bytes)))
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("support bundle limits are invalid") from error

    output = Path(destination).expanduser()
    if not output.is_absolute() or output.suffix.casefold() != ".zip":
        raise ValueError("support bundle destination must be an absolute .zip path")
    parent = _validated_root(output.parent, "destination parent")
    output = parent / output.name
    if output.exists() and (_is_link_or_junction(output) or not output.is_file()):
        raise ValueError("support bundle destination must be a normal file")

    if system_overview is None:
        system_overview = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
            "os_name": os.name,
        }
    if resource_status is None:
        resource_status = {"data_disk": _disk_summary(data_root)}
        if work_root is not None and work_root != data_root:
            resource_status["work_disk"] = _disk_summary(work_root)

    error_logs = sorted(
        (
            entry
            for entry in scan.entries
            if entry.category == "diagnostic_logs"
            and entry.path.suffix.casefold() in {".log", ".trace"}
            and any(token in entry.path.name.casefold() for token in ("error", "failure"))
        ),
        key=lambda entry: entry.modified_ns,
        reverse=True,
    )[:log_limit]
    safe_logs = [
        tail
        for entry in error_logs
        if (tail := _read_sanitized_log_tail(entry.path, tail_limit))
    ]
    inventory = _totals(scan.entries)
    summary = {
        "schema_version": 1,
        "app_version": __version__,
        "created_at": datetime.now(UTC).isoformat(),
        "system": _safe_system_overview(system_overview),
        "resources": _safe_resource_status(resource_status),
        "storage_categories": inventory,
        "included_error_logs": len(safe_logs),
        "skipped_links": scan.skipped_links,
        "unreadable_entries": scan.unreadable_entries,
    }

    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}-", suffix=".zip", dir=parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=False,
        ) as archive:
            _zip_write_text(
                archive,
                "summary.json",
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            )
            for index, log_tail in enumerate(safe_logs, start=1):
                _zip_write_text(archive, f"logs/error-{index:03d}.log", log_tail + "\n")
        os.replace(temporary, output)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return output.resolve()


__all__ = [
    "DEFAULT_LOG_TAIL_BYTES",
    "DEFAULT_MAX_DELETE_BYTES",
    "DEFAULT_MAX_LOG_FILES",
    "DEFAULT_MIN_AGE_DAYS",
    "cleanup_local_storage",
    "create_support_bundle",
    "inspect_local_storage",
]
