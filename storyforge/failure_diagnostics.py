from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Final


DIAGNOSTIC_SCHEMA_VERSION: Final[int] = 2
DEFAULT_MAX_READ_BYTES: Final[int] = 64 * 1024
DEFAULT_MAX_LOG_CHARS: Final[int] = 6_000

FailureDiagnostics = dict[str, str | bool | int]


_SUMMARY_BY_CODE: Final[dict[str, str]] = {
    "missing_input": "渲染所需的素材或中间文件不存在。",
    "corrupt_media": "素材文件损坏，或 FFmpeg 无法读取其编码。",
    "permission_denied": "制作电脑没有读取素材或写入成品的权限。",
    "disk_full": "制作电脑的可用磁盘空间不足。",
    "filter_or_subtitle": "画面滤镜、字体或字幕处理失败。",
    "encoder_init": "视频编码器启动失败，可能需要切换编码方式。",
    "resource_exhausted": (
        "FFmpeg 无法申请本次渲染所需的系统资源。"
        "可能是滤镜帧积压、分页文件、线程或可用内存受限，不等同于物理内存容量不足。"
    ),
    "out_of_memory": (
        "FFmpeg 报告内存申请失败。"
        "这也可能由滤镜帧积压或分页文件限制引起，不能仅据此判定电脑物理内存不足。"
    ),
    "unknown": "FFmpeg 渲染失败，未识别到常见原因。",
}

_CLASSIFICATION_PATTERNS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "missing_input",
        (
            r"no such file or directory",
            r"cannot find the (?:file|path)",
            r"the system cannot find",
            r"does not exist",
            r"error opening input",
            r"failed to open (?:an? )?input",
            r"could not open (?:file|input)",
            r"找不到(?:指定的)?(?:文件|路径)",
            r"文件不存在",
        ),
    ),
    (
        "corrupt_media",
        (
            r"invalid data found when processing input",
            r"moov atom not found",
            r"could not find codec parameters",
            r"invalid nal unit",
            r"error while decoding",
            r"(?:file|input|media).{0,40}corrupt",
            r"corrupt(?:ed|ion)?",
            r"素材(?:文件)?损坏",
        ),
    ),
    (
        "permission_denied",
        (
            r"permission denied",
            r"access (?:is )?denied",
            r"operation not permitted",
            r"read-only file system",
            r"拒绝访问",
            r"没有权限",
            r"权限不足",
        ),
    ),
    (
        "disk_full",
        (
            r"no space left on device",
            r"disk (?:is )?full",
            r"not enough (?:free )?(?:disk )?space",
            r"insufficient disk space",
            r"磁盘(?:空间)?已满",
            r"磁盘空间不足",
        ),
    ),
    (
        "resource_exhausted",
        (
            r"resource temporarily unavailable",
            r"pthread_create.{0,40}failed",
            r"cannot create (?:a )?thread",
            r"insufficient system resources",
            r"paging file is too small",
        ),
    ),
    (
        "out_of_memory",
        (
            r"cannot allocate memory",
            r"out of memory",
            r"failed to allocate (?:memory|buffer)",
            r"not enough (?:available )?memory",
            r"bad_alloc",
            r"内存不足",
        ),
    ),
    (
        "filter_or_subtitle",
        (
            r"error (?:initializing|reinitializing) (?:complex )?filters?",
            r"failed to configure output pad",
            r"error (?:parsing|applying) filter",
            r"no such filter",
            r"filtergraph",
            r"parsed_(?:ass|subtitles)",
            r"libass",
            r"fontconfig",
            r"(?:ass|subtitle) (?:filter|renderer|parsing).{0,30}(?:error|failed)",
            r"字幕.{0,20}(?:错误|失败)",
            r"滤镜.{0,20}(?:错误|失败)",
        ),
    ),
    (
        "encoder_init",
        (
            r"error while opening encoder",
            r"error initializing output stream",
            r"failed to (?:open|initialize|initialise) encoder",
            r"unknown encoder",
            r"encoder .{0,30} not found",
            r"cannot load nvcuda",
            r"no capable devices found",
            r"device setup failed",
            r"failed to initialize nvenc",
            r"failed to initialise nvenc",
            r"(?:nvenc|amf|qsv|videotoolbox).{0,40}(?:error|failed|unavailable)",
            r"编码器.{0,20}(?:错误|失败|不可用)",
        ),
    ),
)

_OSC_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(
    r"\x1b\][^\x07]*(?:\x07|\x1b\\)", re.DOTALL
)
_ANSI_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(
    r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])"
)
_URL_USERINFO_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@"
)
_BEARER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(Bearer)(?:\s+|%20)[^\s,;]+"
)
_AUTHORIZATION_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?im)\b(Authorization)(\s*:\s*)([^\r\n]+)"
)
_SECRET_ASSIGNMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|token|password|passwd|"
    r"api(?:[ _-]?key)|x-api-key|secret)\b(\s*[:=]\s*|[ \t]+)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s&,;]+)"
)
_URL_SECRET_PARAMETER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)([?&](?:access[_-]?token|refresh[_-]?token|token|password|passwd|"
    r"api[_-]?key|key|secret|signature|sig)=)[^&#\s]*"
)

_QUOTED_ABSOLUTE_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r'''(?i)(["'])(?:(?:[a-z]:[\\/])|(?:\\\\)|(?:/(?!/)))[^"'\r\n]+\1'''
)
_FILE_URL_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\bfile://[^\r\n]*")
_WINDOWS_MEDIA_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?<![a-z0-9])(?:[a-z]:[\\/]|\\\\)[^\r\n\"'<>|]*?\."
    r"(?:mp4|mov|mkv|avi|webm|m4v|mp3|wav|aac|m4a|flac|ogg|srt|ass|vtt|txt|"
    r"json|log|png|jpe?g|webp|bmp|gif|ttf|otf|exe|dll)"
)
_WINDOWS_PATH_TO_EOL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?<![a-z0-9])(?:[a-z]:[\\/]|\\\\)[^\r\n]*"
)
_UNIX_MEDIA_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w:/])/(?!/)[^\r\n\"']*?\."
    r"(?:mp4|mov|mkv|avi|webm|m4v|mp3|wav|aac|m4a|flac|ogg|srt|ass|vtt|txt|"
    r"json|log|png|jpe?g|webp|bmp|gif|ttf|otf|so)"
)
_UNIX_PATH_TO_EOL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w:/])/(?!/)[^\r\n]*"
)


def classify_failure(log_text: str) -> str:
    """Classify common FFmpeg failures without exposing the original log."""

    for code, patterns in _CLASSIFICATION_PATTERNS:
        if any(re.search(pattern, log_text, flags=re.IGNORECASE | re.DOTALL) for pattern in patterns):
            return code
    return "unknown"


def sanitize_failure_log(log_text: str) -> str:
    """Remove terminal controls, credentials and local absolute paths from a log."""

    value = log_text.replace("\r\n", "\n").replace("\r", "\n")
    value = _OSC_ESCAPE_RE.sub("", value)
    value = _ANSI_ESCAPE_RE.sub("", value)
    value = "".join(_safe_character(character) for character in value)

    value = _URL_USERINFO_RE.sub(r"\1<redacted>@", value)
    value = _URL_SECRET_PARAMETER_RE.sub(r"\1<redacted>", value)
    value = _BEARER_RE.sub(r"\1 <redacted>", value)
    value = _AUTHORIZATION_HEADER_RE.sub(_redact_authorization_header, value)
    value = _SECRET_ASSIGNMENT_RE.sub(r"\1\2<redacted>", value)

    # Replace quoted paths first so spaces inside a filename cannot leak.  The
    # extension-aware expressions retain useful FFmpeg error text following a
    # path; the broader fallbacks favor privacy when no safe boundary exists.
    value = _QUOTED_ABSOLUTE_PATH_RE.sub("<path>", value)
    value = _FILE_URL_RE.sub("<path>", value)
    value = _WINDOWS_MEDIA_PATH_RE.sub("<path>", value)
    value = _WINDOWS_PATH_TO_EOL_RE.sub("<path>", value)
    value = _UNIX_MEDIA_PATH_RE.sub("<path>", value)
    value = _UNIX_PATH_TO_EOL_RE.sub("<path>", value)
    return value.strip()


def capture_failure_diagnostics(
    error_log: str | Path,
    *,
    stage: str = "render",
    return_code: int | None = None,
    attempt: int | None = None,
    attempt_label: str = "",
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
    max_log_chars: int = DEFAULT_MAX_LOG_CHARS,
) -> FailureDiagnostics:
    """Build a small, JSON-safe failure report from the tail of a local log.

    Reading and sanitizing diagnostics is deliberately fail-open: missing,
    locked, or malformed logs produce an ``unknown`` report instead of raising
    another exception that could block the production queue.
    """

    path = Path(error_log)
    log_name = _safe_log_name(path.name)
    captured_at = datetime.now(UTC).isoformat()
    if max_read_bytes <= 0 or max_log_chars <= 0:
        return _empty_diagnostics(
            stage=stage,
            log_name=log_name,
            captured_at=captured_at,
            summary="错误日志读取上限无效，未能生成技术摘要。",
            return_code=return_code,
            attempt=attempt,
            attempt_label=attempt_label,
        )

    try:
        size = path.stat().st_size
        offset = max(0, size - max_read_bytes)
        with path.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(max_read_bytes)
    except (OSError, ValueError):
        return _empty_diagnostics(
            stage=stage,
            log_name=log_name,
            captured_at=captured_at,
            summary="未能读取制作电脑上的错误日志。",
            return_code=return_code,
            attempt=attempt,
            attempt_label=attempt_label,
        )

    truncated = offset > 0
    if offset > 0:
        newline = raw.find(b"\n")
        if newline >= 0:
            raw = raw[newline + 1 :]
    decoded = raw.decode("utf-8", errors="replace")
    code = classify_failure(decoded)
    safe_tail = sanitize_failure_log(decoded)
    if len(safe_tail) > max_log_chars:
        safe_tail = safe_tail[-max_log_chars:]
        truncated = True

    report: FailureDiagnostics = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "code": code,
        "summary": _SUMMARY_BY_CODE[code],
        "stage": _safe_stage(stage),
        "log_name": log_name,
        "log_tail": safe_tail,
        "truncated": truncated,
        "captured_at": captured_at,
    }
    _attach_attempt_context(
        report,
        return_code=return_code,
        attempt=attempt,
        attempt_label=attempt_label,
    )
    return report


def _safe_character(character: str) -> str:
    if character == "\n":
        return character
    if character == "\t":
        return "    "
    if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
        return ""
    return character


def _redact_authorization_header(match: re.Match[str]) -> str:
    value = match.group(3).strip()
    if value.casefold().startswith("bearer "):
        return f"{match.group(1)}{match.group(2)}Bearer <redacted>"
    return f"{match.group(1)}{match.group(2)}<redacted>"


def _safe_log_name(name: str) -> str:
    safe = sanitize_failure_log(name).replace("\n", " ").strip()
    if not safe:
        return "error.log"
    return safe[-128:]


def _safe_stage(stage: str) -> str:
    safe = sanitize_failure_log(str(stage)).replace("\n", " ").strip()
    return (safe or "render")[:64]


def _empty_diagnostics(
    *,
    stage: str,
    log_name: str,
    captured_at: str,
    summary: str,
    return_code: int | None = None,
    attempt: int | None = None,
    attempt_label: str = "",
) -> FailureDiagnostics:
    report: FailureDiagnostics = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "code": "unknown",
        "summary": summary,
        "stage": _safe_stage(stage),
        "log_name": log_name,
        "log_tail": "",
        "truncated": False,
        "captured_at": captured_at,
    }
    _attach_attempt_context(
        report,
        return_code=return_code,
        attempt=attempt,
        attempt_label=attempt_label,
    )
    return report


def _attach_attempt_context(
    report: FailureDiagnostics,
    *,
    return_code: int | None,
    attempt: int | None,
    attempt_label: str,
) -> None:
    """Attach bounded process context without leaking a command or local path."""

    if return_code is not None:
        try:
            report["return_code"] = int(return_code)
        except (TypeError, ValueError, OverflowError):
            pass
    if attempt is not None:
        try:
            normalized_attempt = int(attempt)
        except (TypeError, ValueError, OverflowError):
            normalized_attempt = 0
        if normalized_attempt > 0:
            report["attempt"] = normalized_attempt
    safe_label = _safe_stage(attempt_label) if str(attempt_label or "").strip() else ""
    if safe_label:
        report["attempt_label"] = safe_label
