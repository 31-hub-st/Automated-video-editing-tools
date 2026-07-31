from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..cancellation import run_cancellable_process


ProbeRunner = Callable[..., subprocess.CompletedProcess[Any]]
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(
    r"Stream\s+[^\r\n]*?Video:\s*([^,\s]+)[^\r\n]*?(\d{2,5})x(\d{2,5})",
    re.IGNORECASE,
)
_AUDIO_RE = re.compile(r"Stream\s+[^\r\n]*?Audio:\s*([^,\s]+)", re.IGNORECASE)
_FPS_RE = re.compile(r"(?:,|\s)(\d+(?:\.\d+)?)\s+fps(?:,|\s)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class QualityExpectation:
    width: int
    height: int
    duration_seconds: float
    fps: float | None = None
    video_codec: str = "h264"
    require_audio: bool = True
    minimum_size_bytes: int = 1024
    duration_tolerance_seconds: float | None = None
    checklist: Mapping[str, tuple[Any, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QualityCheck:
    name: str
    passed: bool
    expected: Any
    actual: Any
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class QualityReport:
    passed: bool
    backend: str
    elapsed_ms: int
    media: Mapping[str, Any]
    checks: tuple[QualityCheck, ...]
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "backend": self.backend,
            "elapsed_ms": self.elapsed_ms,
            "media": dict(self.media),
            "checks": [check.to_dict() for check in self.checks],
            "errors": list(self.errors),
        }


class QualityProbeError(RuntimeError):
    pass


def resolve_ffprobe(ffmpeg_path: Path | None = None) -> Path | None:
    """Resolve ffprobe without assuming that imageio-ffmpeg bundles it."""

    configured = str(os.environ.get("STORYFORGE_FFPROBE") or "").strip()
    if configured and Path(configured).is_file():
        return Path(configured).resolve()

    if ffmpeg_path is not None:
        executable = Path(ffmpeg_path)
        sibling_name = "ffprobe.exe" if executable.suffix.casefold() == ".exe" else "ffprobe"
        sibling = executable.with_name(sibling_name)
        if sibling.is_file():
            return sibling.resolve()

    on_path = shutil.which("ffprobe")
    return Path(on_path).resolve() if on_path else None


def _run_probe(
    command: list[str],
    *,
    runner: ProbeRunner,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[Any]:
    try:
        return run_cancellable_process(
            command,
            runner=runner,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_seconds,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QualityProbeError(str(error) or type(error).__name__) from error


def _positive_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _frame_rate(value: Any) -> float:
    """Parse ffprobe's decimal or rational frame-rate representation."""

    text = str(value or "").strip()
    if "/" in text:
        numerator, separator, denominator = text.partition("/")
        if separator:
            bottom = _positive_float(denominator)
            return _positive_float(numerator) / bottom if bottom else 0.0
    return _positive_float(text)


def _probe_with_ffprobe(
    media_path: Path,
    ffprobe_path: Path,
    *,
    runner: ProbeRunner,
    timeout_seconds: float,
) -> dict[str, Any]:
    completed = _run_probe(
        [
            str(ffprobe_path),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(media_path),
        ],
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    if completed.returncode != 0:
        detail = str(completed.stderr or completed.stdout or "unknown ffprobe error").strip()
        raise QualityProbeError(f"ffprobe exited with {completed.returncode}: {detail[-1200:]}")
    try:
        payload = json.loads(str(completed.stdout or "{}"))
    except json.JSONDecodeError as error:
        raise QualityProbeError("ffprobe returned invalid JSON") from error
    streams = [item for item in payload.get("streams", []) if isinstance(item, dict)]
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    video = videos[0] if videos else {}
    audio = audios[0] if audios else {}
    format_payload = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = _positive_float(format_payload.get("duration"))
    if not duration:
        duration = max((_positive_float(item.get("duration")) for item in streams), default=0.0)
    return {
        "backend": "ffprobe",
        "duration_seconds": duration,
        "size_bytes": media_path.stat().st_size,
        "reported_size_bytes": int(_positive_float(format_payload.get("size"))),
        "format_name": str(format_payload.get("format_name") or ""),
        "video_streams": len(videos),
        "audio_streams": len(audios),
        "video_codec": str(video.get("codec_name") or "").casefold(),
        "audio_codec": str(audio.get("codec_name") or "").casefold(),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": _frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
    }


def _probe_with_ffmpeg(
    media_path: Path,
    ffmpeg_path: Path,
    *,
    runner: ProbeRunner,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Decode only the opening 250 ms when a packaged ffprobe is unavailable."""

    completed = _run_probe(
        [
            str(ffmpeg_path),
            "-hide_banner",
            "-i",
            str(media_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-t",
            "0.25",
            "-f",
            "null",
            "-",
        ],
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    output = f"{completed.stderr or ''}\n{completed.stdout or ''}"
    if completed.returncode != 0:
        raise QualityProbeError(
            f"FFmpeg opening-frame probe exited with {completed.returncode}: {output.strip()[-1200:]}"
        )
    duration_match = _DURATION_RE.search(output)
    video_match = _VIDEO_RE.search(output)
    audio_match = _AUDIO_RE.search(output)
    fps_match = _FPS_RE.search(output)
    duration = 0.0
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return {
        "backend": "ffmpeg-fallback",
        "duration_seconds": duration,
        "size_bytes": media_path.stat().st_size,
        "reported_size_bytes": 0,
        "format_name": "",
        "video_streams": 1 if video_match else 0,
        "audio_streams": 1 if audio_match else 0,
        "video_codec": video_match.group(1).casefold() if video_match else "",
        "audio_codec": audio_match.group(1).casefold() if audio_match else "",
        "width": int(video_match.group(2)) if video_match else 0,
        "height": int(video_match.group(3)) if video_match else 0,
        "fps": _positive_float(fps_match.group(1)) if fps_match else 0.0,
    }


def _check(
    name: str,
    passed: bool,
    expected: Any,
    actual: Any,
    message: str = "",
) -> QualityCheck:
    return QualityCheck(name, bool(passed), expected, actual, message)


def run_fast_quality_check(
    media_path: str | Path,
    expectation: QualityExpectation,
    *,
    ffprobe_path: Path | None = None,
    ffmpeg_path: Path | None = None,
    runner: ProbeRunner = subprocess.run,
    timeout_seconds: float = 20.0,
) -> QualityReport:
    """Run a metadata probe and cheap opening-frame decode for render acceptance."""

    started = time.perf_counter()
    path = Path(media_path)
    checks: list[QualityCheck] = []
    errors: list[str] = []
    media: dict[str, Any] = {}
    backend = "unavailable"

    exists = path.is_file()
    checks.append(_check("file_exists", exists, True, exists))
    if exists:
        size_bytes = path.stat().st_size
        checks.append(
            _check(
                "file_size",
                size_bytes >= max(1, int(expectation.minimum_size_bytes)),
                f">={max(1, int(expectation.minimum_size_bytes))}",
                size_bytes,
            )
        )
    else:
        errors.append(f"output file does not exist: {path}")

    if exists:
        resolved_ffprobe = ffprobe_path or resolve_ffprobe(ffmpeg_path)
        try:
            if resolved_ffprobe is not None:
                media = _probe_with_ffprobe(
                    path,
                    resolved_ffprobe,
                    runner=runner,
                    timeout_seconds=timeout_seconds,
                )
            elif ffmpeg_path is not None:
                media = _probe_with_ffmpeg(
                    path,
                    ffmpeg_path,
                    runner=runner,
                    timeout_seconds=timeout_seconds,
                )
            else:
                raise QualityProbeError("neither ffprobe nor FFmpeg is available")
            backend = str(media.get("backend") or "unknown")
        except (QualityProbeError, OSError, ValueError, TypeError) as error:
            errors.append(str(error) or type(error).__name__)

    probe_ok = bool(media)
    checks.append(_check("media_probe", probe_ok, True, probe_ok, "; ".join(errors)))
    if probe_ok:
        video_streams = int(media.get("video_streams") or 0)
        audio_streams = int(media.get("audio_streams") or 0)
        checks.extend(
            [
                _check("video_stream", video_streams >= 1, ">=1", video_streams),
                _check(
                    "audio_stream",
                    audio_streams >= 1 if expectation.require_audio else True,
                    ">=1" if expectation.require_audio else "optional",
                    audio_streams,
                ),
                _check("width", int(media.get("width") or 0) == expectation.width, expectation.width, int(media.get("width") or 0)),
                _check("height", int(media.get("height") or 0) == expectation.height, expectation.height, int(media.get("height") or 0)),
            ]
        )
        actual_codec = str(media.get("video_codec") or "").casefold()
        expected_codec = expectation.video_codec.casefold()
        codec_aliases = {expected_codec}
        if expected_codec == "h264":
            codec_aliases.update({"avc", "avc1"})
        checks.append(
            _check("video_codec", actual_codec in codec_aliases, expected_codec, actual_codec)
        )
        if expectation.fps is not None:
            actual_fps = _positive_float(media.get("fps"))
            expected_fps = float(expectation.fps)
            checks.append(
                _check(
                    "fps",
                    actual_fps > 0 and abs(actual_fps - expected_fps) <= 0.2,
                    expected_fps,
                    actual_fps,
                )
            )
        actual_duration = _positive_float(media.get("duration_seconds"))
        tolerance = expectation.duration_tolerance_seconds
        if tolerance is None:
            tolerance = max(1.0, min(3.0, expectation.duration_seconds * 0.02))
        duration_ok = actual_duration > 0 and abs(actual_duration - expectation.duration_seconds) <= tolerance
        checks.append(
            _check(
                "duration_seconds",
                duration_ok,
                {
                    "target": expectation.duration_seconds,
                    "tolerance": tolerance,
                },
                actual_duration,
            )
        )

    for name, values in expectation.checklist.items():
        expected, actual = values
        checks.append(_check(f"checklist.{name}", actual == expected, expected, actual))

    elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
    passed = not errors and all(check.passed for check in checks)
    return QualityReport(
        passed=passed,
        backend=backend,
        elapsed_ms=elapsed_ms,
        media=media,
        checks=tuple(checks),
        errors=tuple(errors),
    )


__all__ = [
    "QualityCheck",
    "QualityExpectation",
    "QualityProbeError",
    "QualityReport",
    "resolve_ffprobe",
    "run_fast_quality_check",
]
