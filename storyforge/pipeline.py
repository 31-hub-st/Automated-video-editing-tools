from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
import zlib
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .cancellation import (
    JobCancelledError,
    raise_if_cancelled,
    run_cancellable_process,
)
from .config import default_data_dir
from .failure_diagnostics import capture_failure_diagnostics, classify_failure
from .models import AppSettings, JobStatus, PlatformProfile, RenderJob
from .providers.base import ProviderConfig, ProviderError
from .providers.text import TextRequest, TextResult, create_text_provider
from .providers.tts import (
    TTSResult,
    create_tts_provider,
    female_voice_candidates,
    release_embedded_kokoro_runtime,
)
from .services.media import (
    MediaError,
    MusicPlan,
    VIDEO_FADE_SECONDS,
    VideoSegment,
    build_ffmpeg_plan,
    build_low_memory_segment_plan,
    canonical_mood,
    increment_usage_record,
    plan_video_segments,
    select_music_asset,
)
from .services.quality import (
    QualityCheck,
    QualityExpectation,
    QualityReport,
    run_fast_quality_check,
)
from .services.subtitles import (
    AssStyleConfig,
    SubtitleCue,
    resolve_cover_split_geometry,
    write_ass,
)
from .services.text_processing import (
    ManuscriptAnalysis,
    analyze_manuscript,
    split_english_sentences,
)
from .system import available_encoders, mark_encoder_unavailable, resolve_ffmpeg


ProgressCallback = Callable[[JobStatus, float, str], None]
TextProviderFactory = Callable[[Any], Any]
TTSProviderFactory = Callable[[Any], Any]
CommandRunner = Callable[..., subprocess.CompletedProcess[Any]]
QualityChecker = Callable[..., QualityReport]
CHAPTER_MARKER = "[[CHAPTER_BREAK]]"
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_GEOMETRY_RE = re.compile(
    r"Video:.*?(?P<width>\d{2,5})x(?P<height>\d{2,5})(?:\s|,|\[)",
    re.IGNORECASE,
)
_SAMPLE_ASPECT_RE = re.compile(r"SAR\s+(?P<num>\d+):(?P<den>\d+)", re.IGNORECASE)
_ROTATION_RE = re.compile(r"rotation of\s+(?P<degrees>-?\d+(?:\.\d+)?)", re.IGNORECASE)
_SAFE_COMPONENT_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_GEOMETRY_CACHE: dict[tuple[str, int, int], tuple[int, int] | None] = {}
_GEOMETRY_CACHE_LOCK = threading.RLock()
_MEDIA_DECODE_CACHE: dict[
    tuple[str, str, int, int], tuple[bool, str]
] = {}
_MEDIA_DECODE_CACHE_LOCK = threading.RLock()
_MEDIA_DECODE_SAMPLE_SECONDS = 0.35
_MEDIA_DECODE_CACHE_LIMIT = 4096
_MEBIBYTE = 1024**2
_VIDEO_OUTPUT_BYTES_PER_SECOND = 4 * _MEBIBYTE
_AUDIO_OUTPUT_BYTES_PER_SECOND = 32 * 1024
# Serial rendering keeps the normalized clips and one stream-concatenated copy
# beside the final partial file.  Reserve two additional delivery-sized video
# streams instead of pretending that only the final MP4 exists.
_SERIAL_STAGING_BYTES_PER_SECOND = 8 * _MEBIBYTE
_VIDEO_OUTPUT_SAFETY_BYTES = 512 * _MEBIBYTE
_AUDIO_OUTPUT_SAFETY_BYTES = 64 * _MEBIBYTE
_WORKSPACE_BYTES_PER_SECOND = 512 * 1024
_WORKSPACE_SAFETY_BYTES = 512 * _MEBIBYTE
_WINDOWS_OUT_OF_MEMORY_RETURN_CODES = {
    0xC0000017,  # STATUS_NO_MEMORY
    0xC000009A,  # STATUS_INSUFFICIENT_RESOURCES
    0xC000012D,  # STATUS_COMMITMENT_LIMIT
}
_NARRATION_ID3_DESCRIPTION = "StoryForgeNarration"
_NARRATION_METADATA_LIMIT = 4 * _MEBIBYTE
_LOCAL_VOICE_PROFILES = {
    "dramatic": "af_heart",
    "warm": "af_bella",
    "calm": "af_nicole",
    "confident": "af_sarah",
}
_DEEPGRAM_VOICE_PROFILES = {
    "dramatic": "aura-2-andromeda-en",
    "warm": "aura-2-cordelia-en",
    "calm": "aura-2-athena-en",
    "confident": "aura-2-thalia-en",
}
_LEGACY_VOICE_PROFILES = {
    "af_heart": "dramatic",
    "af_bella": "warm",
    "af_nicole": "calm",
    "af_sarah": "confident",
}
_PROMPT_LANGUAGE_NAMES = {
    "en": "English",
    "en-us": "American English",
    "en-gb": "British English",
    "ja": "Japanese",
    "zh": "Mandarin Chinese",
    "zh-hans": "Simplified Chinese",
    "zh-hant": "Traditional Chinese",
    "es": "Spanish",
    "fr": "French",
    "pt": "Brazilian Portuguese",
    "pt-br": "Brazilian Portuguese",
    "id": "Indonesian",
    "de": "German",
    "ko": "Korean",
    "hi": "Hindi",
    "it": "Italian",
}


def _prompt_language(job: RenderJob) -> str:
    raw = str(job.settings_snapshot.get("source_language") or "en").strip()
    return _PROMPT_LANGUAGE_NAMES.get(raw.casefold(), raw or "English")


def _story_card_final_label(job: RenderJob, story_card_template: bool) -> str:
    """Return the only series-stage label allowed in rendered story cards."""

    if (
        story_card_template
        and bool(job.is_final_episode)
        and int(job.episode_count or 0) > 0
    ):
        return "FINAL PART"
    return ""


def _cover_outro_enabled(settings: AppSettings, job: RenderJob) -> bool:
    """Resolve the frozen job switch without letting either layer re-enable it."""

    return bool(
        settings.cover_outro_enabled
        and getattr(job, "cover_outro_enabled", True)
    )


def _story_card_platform_logo(
    platform: PlatformProfile,
    story_card_template: bool,
) -> str | None:
    """Return a renderable, machine-local platform logo for the intro card."""

    if not story_card_template:
        return None
    configured = str(platform.logo_path or "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    return str(path.resolve()) if path.is_file() else None


class PipelineError(RuntimeError):
    """Pipeline failure with optional machine-local and Hub-safe diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        error_log: str | Path = "",
        failure_diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_log = str(error_log or "")
        self.failure_diagnostics = dict(failure_diagnostics or {})


def _format_storage_size(value: int) -> str:
    value = max(0, int(value))
    if value >= 1024**3:
        return f"{value / (1024**3):.1f} GB"
    return f"{value / _MEBIBYTE:.0f} MB"


def _required_output_bytes(
    duration_seconds: float,
    output_mode: str,
    *,
    serial_staging: bool = False,
    export_narration_audio: bool = False,
) -> int:
    """Estimate the peak free space needed on the selected output disk.

    Full video is encoded directly into a private staging directory on the
    output disk and then atomically renamed, so this estimate covers the
    staged H.264 file and a safety margin for CRF variability.  A narration
    MP3 is included only for an explicitly frozen legacy export contract;
    current regular-video jobs publish MP4 only.  Audio-only production needs
    a much smaller reserve.
    """

    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        duration = 1.0
    if not math.isfinite(duration):
        duration = 1.0
    duration = max(1.0, duration)
    if output_mode == "audio_only":
        media_bytes = math.ceil(duration * _AUDIO_OUTPUT_BYTES_PER_SECOND)
        return media_bytes + _AUDIO_OUTPUT_SAFETY_BYTES
    video_bytes_per_second = _VIDEO_OUTPUT_BYTES_PER_SECOND
    if serial_staging:
        video_bytes_per_second += _SERIAL_STAGING_BYTES_PER_SECOND
    if export_narration_audio:
        video_bytes_per_second += _AUDIO_OUTPUT_BYTES_PER_SECOND
    media_bytes = math.ceil(duration * video_bytes_per_second)
    return media_bytes + _VIDEO_OUTPUT_SAFETY_BYTES


def _preflight_output_directory(
    output_folder: str | Path,
    *,
    duration_seconds: float,
    output_mode: str,
    serial_staging: bool = False,
    export_narration_audio: bool = False,
) -> Path:
    """Verify the exact employee output disk before starting FFmpeg.

    ``os.access`` alone is unreliable on Windows and network shares.  A real
    create/write/delete probe catches ACL, read-only and disconnected-drive
    failures using the same account that will launch FFmpeg.
    """

    raw_folder = Path(output_folder).expanduser()
    try:
        output_root = raw_folder.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise PipelineError(
            f"输出文件夹不可用，无法创建目录：{raw_folder}。"
            "请在当前制作电脑重新选择输出文件夹。"
        ) from error
    if not output_root.is_dir():
        raise PipelineError(
            f"输出路径不是文件夹：{output_root}。"
            "请在当前制作电脑重新选择输出文件夹。"
        )

    descriptor = -1
    probe_path: Path | None = None
    try:
        descriptor, probe_name = tempfile.mkstemp(
            prefix=".storyforge-write-check-",
            suffix=".tmp",
            dir=output_root,
        )
        probe_path = Path(probe_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(b"storyforge-output-check")
            stream.flush()
        probe_path.unlink()
        probe_path = None
    except OSError as error:
        raise PipelineError(
            f"输出文件夹不可写：{output_root}。"
            "请检查当前 Windows 账号的文件夹权限，"
            "或重新选择可写入的输出文件夹。"
        ) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if probe_path is not None:
            try:
                probe_path.unlink()
            except OSError:
                pass

    required_bytes = _required_output_bytes(
        duration_seconds,
        output_mode,
        serial_staging=serial_staging,
        export_narration_audio=export_narration_audio,
    )
    try:
        free_bytes = int(shutil.disk_usage(output_root).free)
    except OSError as error:
        raise PipelineError(
            f"无法读取输出磁盘剩余空间：{output_root}。"
            "请确认磁盘已连接，或重新选择输出文件夹。"
        ) from error
    if free_bytes < required_bytes:
        raise PipelineError(
            "输出磁盘空间不足："
            f"当前剩余 {_format_storage_size(free_bytes)}，"
            f"本次至少需要 {_format_storage_size(required_bytes)}"
            "（含渲染临时空间）。请清理磁盘或更换输出文件夹。"
        )
    return output_root


def _preflight_workspace_directory(
    work_root: str | Path,
    *,
    duration_seconds: float,
) -> Path:
    """Protect the system drive before sentence WAVs are assembled.

    The employee-facing output may live on D:/E:, while the private work tree
    defaults to AppData on C:.  Checking only the output disk therefore cannot
    prevent a long narration from exhausting the system volume.
    """

    root = Path(work_root).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        free_bytes = int(shutil.disk_usage(root).free)
    except OSError as error:
        raise PipelineError(
            f"StoryForge 工作目录不可用：{root}。请清理系统盘或更换工作目录。"
        ) from error
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        duration = 1.0
    if not math.isfinite(duration):
        duration = 1.0
    required_bytes = (
        math.ceil(max(1.0, duration) * _WORKSPACE_BYTES_PER_SECOND)
        + _WORKSPACE_SAFETY_BYTES
    )
    if free_bytes < required_bytes:
        raise PipelineError(
            "StoryForge 工作盘空间不足："
            f"当前剩余 {_format_storage_size(free_bytes)}，"
            f"本次至少需要 {_format_storage_size(required_bytes)}。"
            "请先清理系统盘，避免配音或渲染中途失败。"
        )
    return root


def _cleanup_large_job_work_files(job_dir: Path) -> None:
    """Remove reproducible WAV intermediates while retaining small diagnostics."""

    work_dir = job_dir / ".work"
    try:
        shutil.rmtree(work_dir / "voice")
    except (FileNotFoundError, OSError):
        pass
    if not work_dir.is_dir():
        return
    try:
        candidates = tuple(work_dir.rglob("*.wav"))
    except OSError:
        return
    for path in candidates:
        try:
            path.unlink()
        except OSError:
            pass


def _completed_failure_code(
    completed: subprocess.CompletedProcess[Any],
    *,
    output_exists: bool,
) -> str:
    """Classify one FFmpeg attempt, including resource-only exit codes."""

    if completed.returncode == 0 and output_exists:
        return ""
    return_code = int(completed.returncode)
    # CPython exposes Windows process status codes as unsigned DWORD values,
    # while test doubles and older wrappers may surface their signed form.
    # Masking handles both representations without changing POSIX ENOMEM.
    if (
        return_code == -12
        or (return_code & 0xFFFFFFFF) in _WINDOWS_OUT_OF_MEMORY_RETURN_CODES
    ):
        # A process status alone cannot distinguish physical RAM pressure from
        # filter-frame accumulation, pagefile commitment or thread/resource
        # exhaustion.  Keep that uncertainty visible in the diagnostic code.
        return "resource_exhausted"
    detail = completed.stderr or completed.stdout or ""
    return classify_failure(str(detail)[-3000:])


def _should_retry_with_cpu(
    failure_code: str,
    encoder: str,
    encoders: Sequence[str],
) -> bool:
    """Permit exactly the hardware-initialization fallback, nothing broader."""

    return (
        failure_code == "encoder_init"
        and encoder != "libx264"
        and "libx264" in encoders
    )


def _should_retry_in_low_memory_mode(
    failure_code: str,
    *,
    serial_render_prepared: bool,
) -> bool:
    """Use serial preparation only when it changes the failed graph.

    Once the source has already been normalized into one serial clip, the old
    "low-memory" retry rebuilds the same full-length overlay/subtitle graph.
    Retrying that topology after resource exhaustion only repeats the costly
    failure and can freeze an employee workstation a second time.
    """

    return (
        failure_code in {"out_of_memory", "resource_exhausted"}
        and not serial_render_prepared
    )


def _ffmpeg_progress_command(
    command: Sequence[str | os.PathLike[str]],
) -> list[str]:
    """Enable FFmpeg's machine-readable stdout progress stream."""

    arguments = [os.fspath(item) for item in command]
    if not arguments or "-progress" in arguments:
        return arguments
    progress_options = ["-progress", "pipe:1"]
    if "-nostats" not in arguments:
        progress_options.append("-nostats")
    return [arguments[0], *progress_options, *arguments[1:]]


def _ffmpeg_progress_seconds(line: str) -> float | None:
    """Parse an FFmpeg ``-progress`` timestamp into elapsed seconds."""

    key, separator, raw_value = str(line or "").strip().partition("=")
    if not separator:
        return None
    value = raw_value.strip()
    try:
        if key in {"out_time_us", "out_time_ms"}:
            # FFmpeg's historical out_time_ms field is also expressed in
            # microseconds.  Newer builds additionally expose out_time_us.
            seconds = float(value) / 1_000_000.0
        elif key == "out_time":
            hours, minutes, raw_seconds = value.split(":", 2)
            seconds = (
                float(hours) * 3600.0
                + float(minutes) * 60.0
                + float(raw_seconds)
            )
        else:
            return None
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _ffmpeg_progress_frame(line: str) -> int | None:
    """Parse FFmpeg's machine-readable output-frame counter."""

    key, separator, raw_value = str(line or "").strip().partition("=")
    if not separator or key != "frame":
        return None
    try:
        value = int(raw_value.strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


class _FFmpegProgressTracker:
    """Expose truthful per-attempt FFmpeg progress inside the render phase.

    FFmpeg may emit ``progress=end`` even when process teardown later returns a
    failure.  Therefore the stream callback never declares an attempt complete;
    the caller must do that after inspecting the process return code and output.
    A retry deliberately starts a new attempt range instead of inheriting a
    stale 94% from the failed process.
    """

    def __init__(
        self,
        callback: ProgressCallback,
        *,
        end: float = 0.94,
    ) -> None:
        self._callback = callback
        self._end = max(0.0, min(1.0, float(end)))
        self._last = 0.0
        self._last_label = ""
        self._attempt_number = 0
        self._attempt_label = ""
        self._attempt_start = 0.0
        self._attempt_ratio = 0.0
        self._attempt_frames = 0
        self._lock = threading.RLock()

    @property
    def value(self) -> float:
        with self._lock:
            return self._last

    @property
    def attempt_number(self) -> int:
        with self._lock:
            return self._attempt_number

    @property
    def attempt_label(self) -> str:
        with self._lock:
            return self._attempt_label

    @property
    def attempt_ratio(self) -> float:
        with self._lock:
            return self._attempt_ratio

    @property
    def attempt_frames(self) -> int:
        with self._lock:
            return self._attempt_frames

    def __call__(self, status: JobStatus, value: float, label: str) -> None:
        self.report(status, value, label)

    def report(
        self,
        status: JobStatus,
        value: float,
        label: str,
        *,
        allow_rewind: bool = False,
    ) -> None:
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            candidate = self.value
        if not math.isfinite(candidate):
            candidate = self.value
        with self._lock:
            lower_bound = 0.0 if allow_rewind else self._last
            candidate = min(self._end, max(lower_bound, candidate))
            if candidate == self._last and label == self._last_label:
                return
            self._last = candidate
            self._last_label = label
        self._callback(status, candidate, label)

    def begin_attempt(
        self,
        duration_seconds: float,
        label: str,
        *,
        minimum: float = 0.68,
    ) -> Callable[[str], None]:
        """Begin one numbered attempt and return its progress-line consumer."""

        try:
            duration = float(duration_seconds)
        except (TypeError, ValueError):
            duration = 1.0
        if not math.isfinite(duration) or duration <= 0:
            duration = 1.0
        try:
            attempt_start = float(minimum)
        except (TypeError, ValueError):
            attempt_start = 0.68
        if not math.isfinite(attempt_start):
            attempt_start = 0.68
        attempt_start = min(self._end, max(0.0, attempt_start))
        with self._lock:
            self._attempt_number += 1
            attempt_number = self._attempt_number
            self._attempt_label = str(label or "FFmpeg 渲染")
            self._attempt_start = attempt_start
            self._attempt_ratio = 0.0
            self._attempt_frames = 0
            display_label = self._attempt_display_label_locked(0.0)
        self.report(
            JobStatus.RENDERING,
            attempt_start,
            display_label,
            allow_rewind=True,
        )

        def consume(line: str) -> None:
            text = str(line or "").strip()
            frame = _ffmpeg_progress_frame(text)
            if frame is not None:
                with self._lock:
                    if self._attempt_number != attempt_number:
                        return
                    self._attempt_frames = max(self._attempt_frames, frame)
                return
            if text == "progress=end":
                # Only the CompletedProcess return code can prove success.
                return
            elapsed = _ffmpeg_progress_seconds(text)
            if elapsed is None:
                return
            ratio = max(0.0, min(1.0, elapsed / duration))
            with self._lock:
                if self._attempt_number != attempt_number:
                    return
                # Audio timestamps can reach the end even when the video
                # filter graph never emitted a frame.  Treating that as video
                # progress recreates the misleading frame=0 / 94% failure.
                if self._attempt_frames <= 0:
                    return
                self._attempt_ratio = max(self._attempt_ratio, ratio)
                ratio = self._attempt_ratio
                display_label = self._attempt_display_label_locked(ratio)
            mapped = attempt_start + (self._end - attempt_start) * ratio
            self.report(
                JobStatus.RENDERING,
                mapped,
                display_label,
            )

        return consume

    def finish_attempt(
        self,
        *,
        succeeded: bool,
        return_code: int,
        failure_code: str = "",
        stage: str = "ffmpeg_render",
    ) -> dict[str, str | int | float | bool]:
        """Close the current attempt after the child process has exited."""

        with self._lock:
            attempt = self._attempt_number
            label = self._attempt_label or "FFmpeg 渲染"
            start = self._attempt_start
            ratio = self._attempt_ratio
            frames = self._attempt_frames
        if succeeded:
            ratio = 1.0
            value = self._end
            status_label = f"尝试 {attempt} 成功 · {label} · 100%"
        else:
            value = start + (self._end - start) * ratio
            failure_label = failure_code or "unknown"
            frame_label = f"{frames} 帧" if frames else "0 帧"
            status_label = (
                f"尝试 {attempt} 失败 · {label} · {frame_label} · "
                f"返回码 {int(return_code)} · {failure_label}"
            )
        self.report(
            JobStatus.RENDERING,
            value,
            status_label,
            allow_rewind=not succeeded,
        )
        return {
            "attempt": attempt,
            "label": label,
            "stage": str(stage or "ffmpeg_render"),
            "return_code": int(return_code),
            "failure_code": str(failure_code or ""),
            "frames": frames,
            "ratio": round(ratio, 6),
            "succeeded": bool(succeeded),
        }

    def _attempt_display_label_locked(self, ratio: float) -> str:
        return (
            f"尝试 {self._attempt_number} · {self._attempt_label} · "
            f"{round(ratio * 100):d}%"
        )


def _copy_file_atomic(source: Path, destination: Path) -> Path:
    """Copy one completed artifact without ever exposing a partial file."""

    source = Path(source).resolve()
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-",
        suffix=".partial",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with source.open("rb") as input_stream, os.fdopen(handle, "wb") as output_stream:
            handle = -1
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.replace(temporary_path, destination)
    except BaseException:
        if handle >= 0:
            try:
                os.close(handle)
            except OSError:
                pass
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise
    return destination


@dataclass(frozen=True, slots=True)
class NarrationUnit:
    text: str = ""
    is_chapter_break: bool = False


def _concise_preview_sentence(text: str, *, max_words: int) -> str:
    """Return a natural short excerpt for the approval reel only."""

    cleaned = " ".join(str(text or "").split())
    words = cleaned.split()
    if len(words) <= max_words:
        return cleaned
    minimum_boundary = max(5, max_words // 2)
    boundary = max_words
    for index in range(minimum_boundary, max_words + 1):
        if re.search(r"[,;:!?]$", words[index - 1]):
            boundary = index
    excerpt = " ".join(words[:boundary]).rstrip(" ,;:")
    if excerpt and excerpt[-1] not in ".!?":
        excerpt += "."
    return excerpt


@dataclass(frozen=True, slots=True)
class NarrationAssembly:
    path: Path
    cues: tuple[SubtitleCue, ...]
    duration_seconds: float


class UsageLedger:
    """Local hard stop for optional cloud-character budgets."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_dir() / "provider-usage.json"
        self._lock = threading.RLock()

    @staticmethod
    def _period() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def check(self, provider: str, requested: int, limit: int) -> None:
        if limit <= 0:
            return
        with self._lock:
            raw = self._load()
            period = self._period()
            current = int(raw.get(period, {}).get(provider, 0))
            if current + requested > limit:
                raise PipelineError(
                    f"{provider} 本月本地计数将达到 {current + requested:,} 字符，"
                    f"超过设置的硬上限 {limit:,}。任务已在请求前停止。"
                )

    def commit(self, provider: str, used: int) -> None:
        if used <= 0:
            return
        with self._lock:
            raw = self._load()
            period = self._period()
            period_data = dict(raw.get(period) or {})
            period_data[provider] = int(period_data.get(provider, 0)) + used
            raw[period] = period_data
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(
                prefix=".provider-usage-", suffix=".tmp", dir=self.path.parent
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(raw, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temporary_name, self.path)
            except BaseException:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise


def safe_component(value: str, *, fallback: str = "story") -> str:
    clean = _SAFE_COMPONENT_RE.sub("_", value).strip(" ._")
    clean = re.sub(r"\s+", " ", clean)
    return clean[:110] or fallback


def publish_batch_directory(
    job: RenderJob,
    platform: PlatformProfile,
) -> Path:
    """Return the flat employee-facing folder for one production run."""

    effective_code = job.promo_code_snapshot or job.code
    run_id = safe_component(
        job.production_run_id or job.batch_id or job.id[:12],
        fallback=job.id[:12],
    )
    # Clamp the descriptive pieces separately so the run suffix is never
    # truncated from a long novel title.  The suffix is what prevents two
    # repeated production runs from sharing the same publish folder.
    platform_name = safe_component(platform.name, fallback="platform")[:24]
    code = safe_component(effective_code, fallback="code")[:24]
    title = safe_component(job.title, fallback="story")[:48]
    folder_name = f"{platform_name}_{code}_{title}_B{run_id[:8]}"
    return Path(job.output_folder).expanduser().resolve() / "待发布" / folder_name


def publish_media_stem(job: RenderJob, platform: PlatformProfile) -> str:
    """Build a stable, sortable filename that remains unique after copying."""

    effective_code = job.promo_code_snapshot or job.code
    total = max(1, int(job.batch_total_count or 0), int(job.batch_ordinal or 0))
    ordinal_width = max(3, len(str(total)))
    ordinal = max(1, int(job.batch_ordinal or 1))
    run_id = safe_component(
        job.production_run_id or job.batch_id or job.id[:12],
        fallback=job.id[:12],
    )
    platform_name = safe_component(platform.name, fallback="platform")[:24]
    code = safe_component(effective_code, fallback="code")[:32]
    episode_label = safe_component(
        job.episode_label or f"E{max(1, int(job.episode_number)):03d}",
        fallback=f"E{max(1, int(job.episode_number)):03d}",
    )
    return safe_component(
        f"{ordinal:0{ordinal_width}d}_{platform_name}_{code}_"
        f"{episode_label}_"
        f"V{max(1, int(job.variant_index)):02d}_B{run_id[:8]}",
        fallback=f"{ordinal:0{ordinal_width}d}_{job.id[:12]}",
    )


def job_workspace_directory(job: RenderJob, root: Path | None = None) -> Path:
    """Return the private directory that holds manifests, logs and caches."""

    workspace_root = (root or default_data_dir() / "render-work").resolve()
    run_id = safe_component(
        job.production_run_id or job.batch_id or "unbatched",
        fallback="unbatched",
    )
    return workspace_root / run_id / safe_component(job.id, fallback="job")


def production_output_mode(job: RenderJob) -> str:
    """Return the frozen employee-facing output contract for a full job.

    A current production batch publishes either a complete MP4, a narration
    MP3, or a new MP4 built from existing narration.  The historical
    ``video_and_mp3`` identifier is retained as a wire/storage compatibility
    value even though new jobs no longer publish the extra MP3.
    """

    value = str(job.settings_snapshot.get("output_mode") or "").strip().casefold()
    if value in {"video_and_mp3", "audio_only", "reuse_audio"}:
        return value
    # V0.3 queued jobs only carried an "extra narration export" checkbox.
    # False meant video-only, never audio-only. V0.4 no longer publishes an
    # incomplete video-only product, so both legacy states safely upgrade to
    # the complete MP4 + MP3 pair. Only an explicit V0.4 mode may be audio-only.
    legacy_export = job.settings_snapshot.get("export_narration_audio")
    if isinstance(legacy_export, bool):
        return "video_and_mp3"
    return "video_and_mp3"


def production_exports_narration_audio(job: RenderJob) -> bool:
    """Return whether a regular-video job has a frozen legacy MP3 export.

    Jobs queued by releases that promised an MP4+MP3 pair stored an explicit
    boolean in their immutable settings snapshot.  Honour that promise while
    allowing every newly submitted regular-video job to publish MP4 only.
    Audio-only jobs follow their own branch and do not use this helper.
    """

    if production_output_mode(job) != "video_and_mp3":
        return False
    return job.settings_snapshot.get("export_narration_audio") is True


def read_manuscript(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise PipelineError(f"无法读取 {path.name}；请将 TXT 保存为 UTF-8。")


def inject_chapter_markers(analysis: ManuscriptAnalysis) -> str:
    text = analysis.narration_text
    offsets = [
        boundary.character_offset
        for boundary in analysis.chapters
        if boundary.pause_before_seconds > 0
    ]
    for offset in sorted(offsets, reverse=True):
        text = text[:offset].rstrip() + f"\n{CHAPTER_MARKER}\n" + text[offset:].lstrip()
    return text


def narration_units(result: TextResult) -> list[NarrationUnit]:
    units: list[NarrationUnit] = []
    body = result.polished_text.strip()
    hook = result.hook.strip()
    if hook and not body.casefold().startswith(hook.casefold()):
        units.extend(NarrationUnit(sentence) for sentence in split_english_sentences(hook))
    parts = body.split(CHAPTER_MARKER)
    for index, part in enumerate(parts):
        if index:
            units.append(NarrationUnit(is_chapter_break=True))
        units.extend(
            NarrationUnit(sentence)
            for sentence in split_english_sentences(part.strip())
            if sentence.strip()
        )
    ending = result.ending_cta.strip()
    if ending:
        units.extend(NarrationUnit(sentence) for sentence in split_english_sentences(ending))
    if not any(not item.is_chapter_break for item in units):
        raise PipelineError("润色结果没有可朗读的句子。")
    return units


def group_narration_units(
    units: Sequence[NarrationUnit],
    *,
    max_characters: int = 280,
    max_sentences: int = 3,
) -> tuple[list[str], tuple[int, ...]]:
    """Group adjacent sentences for natural prosody without crossing chapter pauses."""

    chunks: list[str] = []
    counts: list[int] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            chunks.append(" ".join(buffer))
            counts.append(len(buffer))
            buffer.clear()

    for unit in units:
        if unit.is_chapter_break:
            flush()
            continue
        text = unit.text.strip()
        if not text:
            continue
        projected = len(text) + (sum(len(item) for item in buffer) + len(buffer) if buffer else 0)
        if buffer and (len(buffer) >= max_sentences or projected > max_characters):
            flush()
        buffer.append(text)
    flush()
    return chunks, tuple(counts)


def assemble_narration_wav(
    tts_result: TTSResult,
    units: Sequence[NarrationUnit],
    output_path: Path,
    *,
    chapter_pause_seconds: float,
    final_chapter_pause_seconds: float | None = None,
    initial_silence_seconds: float = 0.0,
    segment_unit_counts: Sequence[int] | None = None,
) -> NarrationAssembly:
    spoken_units = [item for item in units if not item.is_chapter_break]
    counts = (
        tuple(int(item) for item in segment_unit_counts)
        if segment_unit_counts is not None
        else tuple(1 for _item in tts_result.segments)
    )
    if (
        len(counts) != len(tts_result.segments)
        or any(item <= 0 for item in counts)
        or sum(counts) != len(spoken_units)
    ):
        raise PipelineError(
            "配音语块与字幕句数不一致，已停止以避免字幕错位。"
        )
    if not tts_result.segments:
        raise PipelineError("配音服务没有生成音频。")
    initial_silence_seconds = float(initial_silence_seconds)
    if not math.isfinite(initial_silence_seconds) or initial_silence_seconds < 0:
        raise PipelineError("Initial narration silence must be a non-negative finite number.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    first_path = Path(tts_result.segments[0].path)
    with wave.open(str(first_path), "rb") as first:
        channels = first.getnchannels()
        sample_width = first.getsampwidth()
        frame_rate = first.getframerate()
        compression = first.getcomptype()
        compression_name = first.getcompname()
    if compression != "NONE":
        raise PipelineError("本地拼接目前只支持 PCM WAV 配音。")

    cues: list[SubtitleCue] = []
    current_seconds = 0.0
    segment_index = 0
    final_break_index = max(
        (index for index, unit in enumerate(units) if unit.is_chapter_break),
        default=-1,
    )
    with wave.open(str(output_path), "wb") as destination:
        destination.setnchannels(channels)
        destination.setsampwidth(sample_width)
        destination.setframerate(frame_rate)
        destination.setcomptype(compression, compression_name)
        initial_silence_frames = max(0, round(initial_silence_seconds * frame_rate))
        if initial_silence_frames:
            fill = b"\x80" if sample_width == 1 else b"\x00"
            destination.writeframes(
                fill * initial_silence_frames * channels * sample_width
            )
            current_seconds = initial_silence_frames / frame_rate
        unit_index = 0
        while unit_index < len(units):
            unit = units[unit_index]
            if unit.is_chapter_break:
                pause_seconds = (
                    float(final_chapter_pause_seconds)
                    if final_chapter_pause_seconds is not None
                    and unit_index == final_break_index
                    else float(chapter_pause_seconds)
                )
                silence_frames = max(0, round(pause_seconds * frame_rate))
                fill = b"\x80" if sample_width == 1 else b"\x00"
                destination.writeframes(
                    fill * silence_frames * channels * sample_width
                )
                current_seconds += silence_frames / frame_rate
                unit_index += 1
                continue

            segment = tts_result.segments[segment_index]
            unit_count = counts[segment_index]
            segment_index += 1
            grouped_units: list[NarrationUnit] = []
            cursor = unit_index
            while cursor < len(units) and len(grouped_units) < unit_count:
                candidate = units[cursor]
                if candidate.is_chapter_break:
                    raise PipelineError("配音语块跨越了章节停顿，无法安全对齐字幕。")
                grouped_units.append(candidate)
                cursor += 1
            with wave.open(str(segment.path), "rb") as source:
                signature = (
                    source.getnchannels(),
                    source.getsampwidth(),
                    source.getframerate(),
                    source.getcomptype(),
                )
                expected = (channels, sample_width, frame_rate, compression)
                if signature != expected:
                    raise PipelineError(
                        "配音片段的采样格式不一致，无法安全拼接。"
                    )
                frames = source.readframes(source.getnframes())
                duration = source.getnframes() / frame_rate
                destination.writeframes(frames)
            weights = [
                max(
                    1.0,
                    float(len(re.findall(r"\b[\w'’\-]+\b", item.text)))
                    + 0.3 * len(re.findall(r"[,;:!?]", item.text)),
                )
                for item in grouped_units
            ]
            total_weight = sum(weights)
            cue_start = current_seconds
            for cue_index, (grouped_unit, weight) in enumerate(
                zip(grouped_units, weights, strict=True)
            ):
                cue_end = (
                    current_seconds + duration
                    if cue_index == len(grouped_units) - 1
                    else cue_start + duration * weight / total_weight
                )
                cues.append(
                    SubtitleCue(
                        start=cue_start,
                        end=cue_end,
                        text=grouped_unit.text,
                    )
                )
                cue_start = cue_end
            current_seconds += duration
            unit_index = cursor
    return NarrationAssembly(output_path, tuple(cues), current_seconds)


def _fit_preview_tts_to_duration(
    tts_result: TTSResult,
    units: Sequence[NarrationUnit],
    segment_unit_counts: Sequence[int],
    *,
    preview_seconds: float,
    chapter_pause_seconds: float,
) -> tuple[TTSResult, list[NarrationUnit], tuple[int, ...]]:
    """Trim complete body-sentence groups so a preview keeps its full CTA.

    ``_preview_units`` can only estimate speech length from words. Real voices
    vary enough that the body plus the narrated ending can exceed a fixed
    approval sample. The chapter marker added immediately before the ending is
    a safe edit boundary: remove only complete body groups, never clip audio or
    a sentence, and preserve every ending segment.
    """

    counts = tuple(int(item) for item in segment_unit_counts)
    if len(counts) != len(tts_result.segments) or any(item <= 0 for item in counts):
        raise PipelineError("Preview narration groups do not match the generated audio.")
    break_indexes = [index for index, unit in enumerate(units) if unit.is_chapter_break]
    if not break_indexes:
        raise PipelineError("A narrated preview ending requires a CTA boundary.")

    # Earlier markers belong to the selected story text. The final marker is
    # inserted by ``_preview_units`` specifically to separate the CTA.
    break_index = break_indexes[-1]
    body_units = list(units[:break_index])
    ending_units = list(units[break_index + 1 :])
    if (
        not any(not unit.is_chapter_break for unit in body_units)
        or not ending_units
        or any(unit.is_chapter_break for unit in ending_units)
    ):
        raise PipelineError("A narrated preview must contain both story text and an ending CTA.")

    body_unit_count = sum(not unit.is_chapter_break for unit in body_units)
    consumed_units = 0
    body_segment_count = 0
    for count in counts:
        if consumed_units >= body_unit_count:
            break
        consumed_units += count
        body_segment_count += 1
    if consumed_units != body_unit_count:
        raise PipelineError("Preview TTS groups crossed the CTA boundary.")

    ending_segments = tts_result.segments[body_segment_count:]
    ending_counts = counts[body_segment_count:]
    if sum(ending_counts) != sum(not unit.is_chapter_break for unit in ending_units):
        raise PipelineError("Preview ending TTS groups do not match the CTA text.")
    ending_duration = sum(float(item.duration_seconds) for item in ending_segments)
    fixed_ending_duration = float(chapter_pause_seconds) + ending_duration
    if float(preview_seconds) - fixed_ending_duration <= 0:
        raise PipelineError("The narrated ending CTA is longer than the preview duration.")

    def body_prefix(spoken_count: int) -> list[NarrationUnit]:
        prefix: list[NarrationUnit] = []
        spoken = 0
        for unit in body_units:
            if spoken >= spoken_count:
                break
            prefix.append(unit)
            if not unit.is_chapter_break:
                spoken += 1
        return prefix

    kept_body_segments = 0
    kept_body_duration = 0.0
    kept_body_spoken = 0
    for index, segment in enumerate(tts_result.segments[:body_segment_count]):
        next_duration = kept_body_duration + float(segment.duration_seconds)
        next_spoken = kept_body_spoken + counts[index]
        candidate_units = body_prefix(next_spoken)
        body_pause_duration = (
            sum(unit.is_chapter_break for unit in candidate_units)
            * float(chapter_pause_seconds)
        )
        total_duration = next_duration + body_pause_duration + fixed_ending_duration
        if total_duration > float(preview_seconds) + 1e-6:
            break
        kept_body_duration = next_duration
        kept_body_spoken = next_spoken
        kept_body_segments += 1
    if kept_body_segments <= 0:
        raise PipelineError("The first preview sentence is too long to fit before the CTA.")
    if kept_body_segments == body_segment_count:
        return tts_result, list(units), counts

    kept_body_counts = counts[:kept_body_segments]
    kept_body_units = body_prefix(sum(kept_body_counts))
    fitted_units = [
        *kept_body_units,
        NarrationUnit(is_chapter_break=True),
        *ending_units,
    ]
    fitted_segments = (
        *tts_result.segments[:kept_body_segments],
        *ending_segments,
    )
    fitted_counts = (*kept_body_counts, *ending_counts)
    return (
        TTSResult(tuple(fitted_segments), tts_result.provider, tts_result.model),
        fitted_units,
        tuple(fitted_counts),
    )


def _geometry_cache_key(media_path: Path) -> tuple[str, int, int] | None:
    try:
        resolved = media_path.expanduser().resolve(strict=True)
        stat = resolved.stat()
    except OSError:
        return None
    return (os.path.normcase(str(resolved)), int(stat.st_size), int(stat.st_mtime_ns))


def _media_decode_cache_key(
    media_path: Path,
    stream_kind: str,
) -> tuple[str, str, int, int] | None:
    try:
        resolved = media_path.expanduser().resolve(strict=True)
        stat = resolved.stat()
    except OSError:
        return None
    return (
        stream_kind,
        os.path.normcase(str(resolved)),
        int(stat.st_size),
        int(stat.st_mtime_ns),
    )


def _remember_media_decode_result(
    key: tuple[str, str, int, int],
    result: tuple[bool, str],
) -> None:
    """Cache one real decode result and discard stale identities for the path."""

    with _MEDIA_DECODE_CACHE_LOCK:
        stale = [
            cached_key
            for cached_key in _MEDIA_DECODE_CACHE
            if cached_key[:2] == key[:2] and cached_key != key
        ]
        for cached_key in stale:
            _MEDIA_DECODE_CACHE.pop(cached_key, None)
        if len(_MEDIA_DECODE_CACHE) >= _MEDIA_DECODE_CACHE_LIMIT:
            _MEDIA_DECODE_CACHE.pop(next(iter(_MEDIA_DECODE_CACHE)), None)
        _MEDIA_DECODE_CACHE[key] = result


def _clear_media_decode_cache() -> None:
    """Test/startup helper; normal jobs retain results for the process lifetime."""

    with _MEDIA_DECODE_CACHE_LOCK:
        _MEDIA_DECODE_CACHE.clear()


def _decode_media_sample(
    ffmpeg_path: Path,
    media_path: Path,
    stream_kind: str,
    *,
    duration_seconds: float | None = None,
    runner: CommandRunner = subprocess.run,
) -> tuple[bool, str]:
    """Decode short samples across a file instead of trusting metadata only."""

    if stream_kind not in {"video", "audio", "image"}:
        raise ValueError("stream_kind must be 'video', 'audio', or 'image'")
    key = _media_decode_cache_key(media_path, stream_kind)
    if key is None:
        return False, "文件不存在、已移动或当前账号无权读取"
    with _MEDIA_DECODE_CACHE_LOCK:
        cached = _MEDIA_DECODE_CACHE.get(key)
    if cached is not None:
        return cached

    duration = float(duration_seconds or 0.0)
    if not math.isfinite(duration) or duration <= 0:
        duration = 0.0
    if stream_kind == "image" or not duration:
        sample_points = (0.0,)
    else:
        last_start = max(0.0, duration - _MEDIA_DECODE_SAMPLE_SECONDS)
        if stream_kind == "video":
            candidates = (0.0, last_start / 2.0, last_start)
        else:
            candidates = (0.0, last_start)
        points: list[float] = []
        for point in candidates:
            if not any(abs(point - existing) < 0.05 for existing in points):
                points.append(point)
        sample_points = tuple(points)

    for index, sample_start in enumerate(sample_points, start=1):
        command = [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-xerror",
        ]
        if sample_start > 0.0:
            command.extend(("-ss", f"{sample_start:.3f}"))
        command.extend(
            (
                "-i",
                str(media_path),
                "-map",
                "0:a:0" if stream_kind == "audio" else "0:v:0",
            )
        )
        if stream_kind == "image":
            command.extend(("-frames:v", "1", "-an"))
        else:
            command.extend(("-t", str(_MEDIA_DECODE_SAMPLE_SECONDS)))
        if stream_kind == "video":
            command.append("-an")
        elif stream_kind == "audio":
            command.append("-vn")
        command.extend(("-sn", "-dn", "-f", "null", os.devnull))
        try:
            completed = run_cancellable_process(
                command,
                runner=runner,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired:
            # A busy USB disk/network mount can recover on the next batch. Do
            # not turn one transient timeout into a process-lifetime verdict.
            return False, "解码抽样超过 20 秒，素材磁盘可能暂时繁忙"
        except OSError as error:
            # Process creation and temporary access failures are equally
            # transient, so deliberately leave them out of the cache.
            return False, f"无法启动解码检查：{error}"
        if completed.returncode != 0:
            raw_detail = str(completed.stderr or completed.stdout or "FFmpeg 未返回详情")
            compact_detail = " ".join(raw_detail.split())[-600:]
            position = "开头" if sample_start <= 0 else f"{sample_start:.1f} 秒处"
            result = (
                False,
                f"第 {index}/{len(sample_points)} 个抽样（{position}）失败：{compact_detail}",
            )
            _remember_media_decode_result(key, result)
            return result

    result = (True, "")
    _remember_media_decode_result(key, result)
    return result


def _remember_video_geometry(media_path: Path, probe_output: str) -> tuple[int, int] | None:
    key = _geometry_cache_key(media_path)
    match = _VIDEO_GEOMETRY_RE.search(probe_output)
    geometry: tuple[int, int] | None = None
    if match:
        width = int(match.group("width"))
        height = int(match.group("height"))
        sar = _SAMPLE_ASPECT_RE.search(probe_output)
        if sar:
            numerator = int(sar.group("num"))
            denominator = int(sar.group("den"))
            if numerator > 0 and denominator > 0:
                width = max(1, round(width * numerator / denominator))
        rotation = _ROTATION_RE.search(probe_output)
        if rotation and round(abs(float(rotation.group("degrees")))) % 180 == 90:
            width, height = height, width
        if width > 0 and height > 0:
            geometry = (width, height)
    if key is not None:
        with _GEOMETRY_CACHE_LOCK:
            _GEOMETRY_CACHE[key] = geometry
    return geometry


def _probe_media_output(ffmpeg: Path, media_path: Path) -> str:
    try:
        completed = run_cancellable_process(
            [str(ffmpeg), "-hide_banner", "-i", str(media_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MediaError(f"无法读取媒体时长：{media_path.name}: {error}") from error
    return f"{completed.stderr}\n{completed.stdout}"


def probe_duration_with_ffmpeg(ffmpeg: Path, media_path: Path) -> float:
    probe_output = _probe_media_output(ffmpeg, media_path)
    _remember_video_geometry(media_path, probe_output)
    match = _DURATION_RE.search(probe_output)
    if not match:
        raise MediaError(f"FFmpeg 无法识别媒体时长：{media_path.name}")
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError(f"媒体时长无效：{media_path.name}")
    return duration


def probe_video_geometry_with_ffmpeg(
    ffmpeg: Path,
    media_path: Path,
) -> tuple[int, int] | None:
    """Return rotation/SAR-normalised display dimensions, cached by file."""

    key = _geometry_cache_key(media_path)
    if key is not None:
        with _GEOMETRY_CACHE_LOCK:
            if key in _GEOMETRY_CACHE:
                return _GEOMETRY_CACHE[key]
    return _remember_video_geometry(
        media_path,
        _probe_media_output(ffmpeg, media_path),
    )


def _segments_with_geometry(ffmpeg: Path, segments: Sequence[Any]) -> list[Any]:
    enriched: list[Any] = []
    for segment in segments:
        try:
            geometry = probe_video_geometry_with_ffmpeg(ffmpeg, Path(segment.path))
        except (MediaError, OSError, ValueError):
            geometry = None
        if geometry is None:
            enriched.append(segment)
        else:
            enriched.append(
                replace(
                    segment,
                    source_width=geometry[0],
                    source_height=geometry[1],
                )
            )
    return enriched


def _serial_fallback_segments(
    segments: Sequence[VideoSegment],
    video_transition: str,
) -> list[VideoSegment]:
    """Convert an overlap plan into an equivalent cut timeline.

    The low-memory renderer cannot keep two full-resolution clips alive for an
    xfade.  Removing the overlap from the beginning of each later segment
    preserves the target timeline length while degrading only the transition
    itself from a short fade to a cut.
    """

    if video_transition != "fade":
        return list(segments)
    serial: list[VideoSegment] = []
    for index, segment in enumerate(segments):
        if index == 0:
            serial.append(segment)
            continue
        duration = segment.duration - VIDEO_FADE_SECONDS
        if duration <= 0:
            raise PipelineError(
                "低内存渲染无法处理短于转场时长的视频片段。"
            )
        serial.append(
            replace(
                segment,
                start_time=segment.start_time + VIDEO_FADE_SECONDS * segment.speed,
                duration=duration,
            )
        )
    return serial


def _remove_cancelled_media(path: Path) -> None:
    """Do not leave a partial MP4 looking like a completed local output."""

    try:
        path.unlink()
    except OSError:
        pass


def _remove_empty_directory(path: Path) -> None:
    """Best-effort cleanup for a publish folder that never produced media."""

    try:
        path.rmdir()
    except OSError:
        pass


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Durably replace a small JSON control file without exposing a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}-",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _publish_signature(path: Path) -> dict[str, int]:
    """Return a rename-stable, cheap identity for one already-rendered artifact."""

    stat = path.stat()
    if not path.is_file() or stat.st_size <= 0:
        raise PipelineError(f"Staged publish artifact is missing or empty: {path}")
    return {
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _matches_publish_signature(path: Path, signature: Any) -> bool:
    if not isinstance(signature, dict):
        return False
    try:
        stat = path.stat()
        return path.is_file() and (
            int(stat.st_size) == int(signature["size_bytes"])
            and int(stat.st_mtime_ns) == int(signature["mtime_ns"])
        )
    except (OSError, KeyError, TypeError, ValueError):
        return False


def _subtitle_style(settings: AppSettings) -> AssStyleConfig:
    subtitle = settings.subtitle
    intro = settings.intro_card
    card = settings.code_card
    outro = settings.outro_card
    requested_word_mode = str(
        getattr(settings, "subtitle_word_mode", "") or ""
    ).strip().casefold()
    if requested_word_mode not in {"off", "cumulative", "single"}:
        requested_word_mode = "cumulative" if subtitle.word_sync_enabled else "off"
    style = AssStyleConfig(
        play_res_x=settings.output_width,
        play_res_y=settings.output_height,
        font_name=subtitle.font_family,
        subtitle_font_size=subtitle.font_size,
        subtitle_text_color=subtitle.text_color,
        subtitle_outline_color=subtitle.outline_color,
        subtitle_outline=float(subtitle.outline_width),
        subtitle_shadow=float(subtitle.shadow_width),
        subtitle_bold=bool(subtitle.bold),
        subtitle_italic=bool(subtitle.italic),
        subtitle_margin_left=subtitle.horizontal_margin,
        subtitle_margin_right=subtitle.horizontal_margin,
        subtitle_margin_bottom=subtitle.bottom_margin,
        subtitle_background_color=subtitle.background_color,
        subtitle_background_opacity=float(subtitle.background_opacity),
        subtitle_alignment=subtitle.alignment,
        subtitle_position_x_percent=float(subtitle.position_x_percent),
        max_chars_per_line=subtitle.max_chars_per_line,
        max_subtitle_lines=subtitle.max_lines,
        card_font_size=card.font_size,
        card_text_color=card.text_color,
        card_background_color=card.background_color,
        card_background_opacity=card.opacity,
        card_margin_left=card.horizontal_margin,
        card_margin_right=card.horizontal_margin,
        card_margin_top=card.top_margin,
        card_bold=bool(card.bold),
        card_outline_color=card.outline_color,
        card_outline_width=float(card.outline_width),
        card_alignment=card.alignment,
        card_position_x_percent=float(card.position_x_percent),
        card_position_y_percent=float(card.position_y_percent),
        card_width_percent=float(card.width_percent),
        card_padding=card.padding,
        card_radius=card.radius,
        intro_font_name=intro.font_family,
        intro_headline_font_size=intro.headline_font_size,
        intro_headline_color=intro.headline_color,
        intro_body_font_size=intro.body_font_size,
        intro_body_color=intro.body_color,
        intro_label_font_size=intro.label_font_size,
        intro_label_color=intro.label_color,
        intro_background_color=intro.background_color,
        intro_background_opacity=float(intro.background_opacity),
        intro_border_color=intro.border_color,
        intro_border_width=intro.border_width,
        intro_shadow_opacity=float(intro.shadow_opacity),
        intro_width_percent=float(intro.width_percent),
        intro_position_x_percent=float(intro.position_x_percent),
        intro_position_y_percent=float(intro.position_y_percent),
        intro_padding=intro.padding,
        intro_radius=intro.radius,
        intro_text_alignment=intro.text_alignment,
        intro_max_lines=intro.max_lines,
        intro_layout=str(getattr(intro, "layout", "standard") or "standard"),
        intro_animation=getattr(settings, "intro_animation", "fade_rise"),
        outro_font_name=outro.font_family,
        outro_title_font_size=outro.title_font_size,
        outro_title_color=outro.title_color,
        outro_body_font_size=outro.body_font_size,
        outro_body_color=outro.body_color,
        outro_code_font_size=outro.code_font_size,
        outro_code_color=outro.code_color,
        outro_background_color=outro.background_color,
        outro_background_opacity=float(outro.background_opacity),
        outro_border_color=outro.border_color,
        outro_border_width=outro.border_width,
        outro_width_percent=float(outro.width_percent),
        outro_height_percent=float(outro.height_percent),
        outro_position_x_percent=float(outro.position_x_percent),
        outro_position_y_percent=float(outro.position_y_percent),
        outro_padding=outro.padding,
        outro_radius=outro.radius,
        outro_text_alignment=outro.text_alignment,
        word_sync_enabled=requested_word_mode != "off",
        word_display_mode=requested_word_mode,
        word_unread_color=subtitle.unread_color,
        word_active_color=subtitle.active_color,
        word_read_color=subtitle.read_color,
        word_pop_scale=subtitle.pop_scale,
        word_pop_duration_ms=subtitle.pop_duration_ms,
        word_pop_intensity=float(subtitle.pop_intensity),
        semantic_short_phrases=settings.caption_mode == "semantic",
        subtitle_animation=settings.subtitle_animation,
    )
    return style


def _preview_subtitle_style(settings: AppSettings) -> AssStyleConfig:
    """Return the exact half-resolution style used by preview ASS and media."""

    full_style = _subtitle_style(settings)
    return replace(
        full_style,
        play_res_x=540,
        play_res_y=960,
        subtitle_font_size=max(24, round(full_style.subtitle_font_size / 2)),
        subtitle_margin_left=max(80, round(full_style.subtitle_margin_left / 2)),
        subtitle_margin_right=max(80, round(full_style.subtitle_margin_right / 2)),
        subtitle_margin_bottom=max(130, round(full_style.subtitle_margin_bottom / 2)),
        card_font_size=max(24, round(full_style.card_font_size / 2)),
        card_margin_left=max(60, round(full_style.card_margin_left / 2)),
        card_margin_right=max(60, round(full_style.card_margin_right / 2)),
        card_margin_top=max(70, round(full_style.card_margin_top / 2)),
    )


def _intro_card_media_options(
    settings: AppSettings,
    cover_path: Path | None,
    *,
    intro_card_text: str = "",
    code: str = "",
    style: AssStyleConfig | None = None,
    intro_duration: float | None = None,
) -> dict[str, Any]:
    """Resolve one geometry contract shared by full and preview renders."""

    resolved_style = (style or _subtitle_style(settings)).safe()
    layout = resolved_style.intro_layout
    cover_split = settings.video_template == "platform_story_card" and layout in {
        "cover_split",
        "cover_split_noir",
    }
    if not cover_split:
        return {
            "platform_logo_x_percent": max(
                10.0,
                min(90.0, float(resolved_style.intro_position_x_percent)),
            ),
            "platform_logo_y_percent": max(
                5.0,
                min(
                    60.0,
                    float(resolved_style.intro_position_y_percent) + 1.5625,
                ),
            ),
            "intro_card_cover_path": None,
        }

    geometry = resolve_cover_split_geometry(
        resolved_style,
        intro_card_text,
        code=code,
        cover_present=bool(cover_path),
    )
    duration = (
        float(settings.intro_card_duration_seconds)
        if intro_duration is None
        else float(intro_duration)
    )
    return {
        "platform_logo_x_percent": geometry.platform_logo_x_percent,
        "platform_logo_y_percent": geometry.platform_logo_y_percent,
        "intro_card_cover_path": cover_path,
        "intro_card_cover_start": 0.0,
        "intro_card_cover_duration": max(2.5, min(8.0, duration)),
        "intro_card_cover_x_percent": geometry.cover_center_x_percent,
        "intro_card_cover_y_percent": geometry.cover_center_y_percent,
        "intro_card_cover_width_percent": geometry.cover_width_percent,
        "intro_card_cover_height_percent": geometry.cover_height_percent,
        "intro_card_cover_rotation_degrees": geometry.cover_rotation_degrees,
    }


def _commit_video_usage(video_folder: str, segments: Sequence[Any]) -> None:
    increment_usage_record(video_folder, (segment.path for segment in segments))


def _commit_music_usage(music_folder: str, music: Any) -> None:
    """Count one successful output use, independent of FFmpeg loop count."""

    if music is None:
        return
    increment_usage_record(music_folder, (music.path,))


def _plan_category_video_segments(
    video_folder: str,
    target_duration: float,
    *,
    mood: str,
    duration_resolver: Callable[[Path], float],
    variant_seed: int | str | bytes | None,
    selection_report: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    excluded_paths: Sequence[str | os.PathLike[str]] | None = None,
    playback_speed: float = 1.0,
    video_transition: str = "cut",
) -> list[Any]:
    """Plan footage only from the employee-selected folder tree.

    ``mood`` remains in this compatibility interface because existing queued
    drafts and callers still provide it. It must not influence video
    selection: employees choose the footage folder, while story type remains
    available independently for voice and music decisions.
    """

    _ = mood
    report = selection_report if selection_report is not None else {}
    report.clear()
    report.update(
        {
            "mode": "employee_folder",
            "requested_category": None,
            "matched_category": None,
            "fallback": False,
            "fallback_reason": "",
            "source_scope": "selected_root_recursive",
            "source_root": str(Path(video_folder)),
            "warning": "",
        }
    )

    try:
        segments = plan_video_segments(
            video_folder,
            target_duration,
            mood=None,
            duration_resolver=duration_resolver,
            commit_usage=False,
            variant_seed=(
                variant_seed if variant_seed is not None else os.urandom(16)
            ),
            excluded_paths=excluded_paths,
            playback_speed=playback_speed,
            video_transition=video_transition,
        )
    except MediaError as error:
        raise PipelineError(
            "员工选择的视频素材文件夹及其子文件夹中没有可用视频。"
            "请检查文件夹是否正确、素材是否完整且格式受支持后重试。"
            f" 详情：{error}"
        ) from error

    path_counts: dict[str, int] = {}
    for segment in segments:
        key = str(Path(segment.path).resolve()).casefold()
        path_counts[key] = path_counts.get(key, 0) + 1
    repeated_segment_count = sum(max(0, count - 1) for count in path_counts.values())
    max_usage_before = max(
        (int(getattr(segment, "usage_count_before", 0)) for segment in segments),
        default=0,
    )
    report.update(
        {
            "segment_count": len(segments),
            "unique_asset_count": len(path_counts),
            "repeated_segment_count": repeated_segment_count,
            "max_usage_count_before": max_usage_before,
        }
    )
    if repeated_segment_count:
        reuse_warning = (
            f"素材复用提示：当前视频有 {repeated_segment_count} 个片段需要循环复用；"
            "系统仍按使用次数优先去重，并启用起点、镜像和裁切变化；"
            "本批始终保持员工选择的固定播放速度。"
        )
        report["reuse_warning"] = reuse_warning
        if warnings is not None:
            warnings.append(reuse_warning)
    else:
        report["reuse_warning"] = ""
    return segments


def resolve_voice(provider: str, profile: str, mood: str) -> tuple[str, str]:
    """Map one user-facing voice profile to the active provider's female voice."""

    normalized_profile = _LEGACY_VOICE_PROFILES.get(
        profile.strip().casefold(), profile.strip().casefold()
    )
    if normalized_profile not in _LOCAL_VOICE_PROFILES:
        normalized_profile = {
            "suspense": "dramatic",
            "romance": "warm",
            "sad": "calm",
            "revenge": "confident",
        }.get(mood, "dramatic")
    normalized_provider = provider.strip().casefold().replace("-", "_")
    if normalized_provider in {"deepgram", "deepgram_aura", "aura", "aura_2"}:
        return normalized_profile, _DEEPGRAM_VOICE_PROFILES[normalized_profile]
    return normalized_profile, _LOCAL_VOICE_PROFILES[normalized_profile]


def narration_speed_for_wpm(
    words_per_minute: float,
    provider: str = "",
) -> float:
    """Map target WPM to a provider-calibrated speed multiplier.

    Local Kokoro's American voices measure around 185 WPM on StoryForge's
    1–3-sentence narration chunks at speed 1.0.  Treating them as 155 WPM made
    the former 210 setting render near 270 WPM and flattened the performance.
    Deepgram keeps the previous baseline because its speed parameter and voice
    cadence are different.
    """

    value = float(words_per_minute)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("words_per_minute must be a positive finite number")
    normalized_provider = str(provider or "").strip().casefold().replace("-", "_")
    local_names = {
        "kokoro",
        "local",
        "local_kokoro",
        "kokoro_local",
        "kokoro_http",
        "kokoro_cli",
    }
    edge_names = {"edge", "edge_tts", "microsoft_edge", "microsoft_edge_tts"}
    baseline_wpm = (
        185.0
        if normalized_provider in local_names
        else 180.0
        if normalized_provider in edge_names
        else 155.0
    )
    # Provider adapters accept the complete 0.5–2.0 application contract.
    # Deepgram itself caps its query parameter at 1.5; its adapter applies the
    # small remaining factor with FFmpeg ``atempo`` so 260/280 WPM are not
    # silently reported at a slower pace.
    return max(0.7, min(2.0, value / baseline_wpm))


def prepared_recipe_hash(
    job: RenderJob,
    platform: PlatformProfile,
    settings: AppSettings,
    source_sha256: str,
) -> str:
    """Fingerprint every setting that can invalidate approved narration text."""

    payload = {
        "source_sha256": source_sha256,
        "platform_id": platform.id,
        "platform_name": platform.name,
        "search_template": platform.search_template,
        "ending_template": platform.ending_template,
        "code": job.promo_code_snapshot or job.code,
        "adult_mode": settings.adult_mode,
        "retention_min": settings.retention_min,
        "retention_max": settings.retention_max,
        "language": settings.language,
        "text_provider": settings.providers.text_provider,
        "text_model": settings.providers.text_model,
        "narration_wpm": settings.narration_wpm,
        "chapter_pause_seconds": settings.chapter_pause_seconds,
        "video_template": settings.video_template,
        "story_mood": str(job.story_mood or ""),
        "story_mood_source": str(job.story_mood_source or "auto"),
        "intro_card_text": str(job.intro_card_text or ""),
        "intro_card_source": str(job.intro_card_source or ""),
        "creative_line_index": max(1, int(job.variant_index)),
        "creative_line_count": max(
            1,
            int(job.variant_index),
            int(getattr(job, "variant_count", 1)),
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PipelineRunner:
    def __init__(
        self,
        settings_getter: Callable[[], AppSettings],
        *,
        ffmpeg_path: Path | None = None,
        text_provider_factory: TextProviderFactory = create_text_provider,
        tts_provider_factory: TTSProviderFactory = create_tts_provider,
        command_runner: CommandRunner = subprocess.run,
        quality_checker: QualityChecker = run_fast_quality_check,
        usage_ledger: UsageLedger | None = None,
        work_root: Path | None = None,
        heavy_resource_lock: Any | None = None,
    ) -> None:
        self.settings_getter = settings_getter
        self.ffmpeg_path = ffmpeg_path or resolve_ffmpeg()
        self.text_provider_factory = text_provider_factory
        self.tts_provider_factory = tts_provider_factory
        self.command_runner = command_runner
        self.quality_checker = quality_checker
        self.usage_ledger = usage_ledger or UsageLedger()
        inferred_work_root = self.usage_ledger.path.parent / "render-work"
        self.work_root = (work_root or inferred_work_root).expanduser().resolve()
        self.heavy_resource_lock = heavy_resource_lock
        self._publish_transaction_root = self.work_root / "publish-transactions"
        self._publish_transaction_lock = threading.RLock()
        # A process may be killed between the two final renames.  Recovering
        # journals here guarantees that the employee-facing folder never
        # keeps an uncommitted half-pair after StoryForge starts again.
        self._publish_recovery_warnings = self._recover_publish_transactions()

    def _prepare_low_memory_video_source(
        self,
        *,
        segments: Sequence[VideoSegment],
        staging_dir: Path,
        job_dir: Path,
        width: int,
        height: int,
        fps: int,
        color_grade: str,
        video_transition: str,
        progress: ProgressCallback,
    ) -> tuple[VideoSegment | None, subprocess.CompletedProcess[Any]]:
        """Serially normalize and stitch video inputs on the output drive.

        A normal FFmpeg graph is intentionally one process for speed, but that
        graph opens every selected clip at once. When FFmpeg reports memory
        exhaustion, this fallback keeps only one decoder/filter chain alive,
        stream-concatenates the normalized files, and hands one video input to
        the final subtitle/audio render. Large temporary media lives under the
        publish staging directory instead of filling the employee's C drive.
        """

        serial_segments = _serial_fallback_segments(segments, video_transition)
        fallback_dir = staging_dir / "low-memory"
        try:
            shutil.rmtree(fallback_dir)
        except FileNotFoundError:
            pass
        fallback_dir.mkdir(parents=True, exist_ok=True)

        command_lines: list[str] = []
        normalized_paths: list[Path] = []
        completed: subprocess.CompletedProcess[Any] | None = None
        total_steps = max(1, len(serial_segments) + 2)
        for index, segment in enumerate(serial_segments, start=1):
            normalized_path = fallback_dir / f"segment-{index:04d}.mp4"
            segment_plan = build_low_memory_segment_plan(
                segment,
                normalized_path,
                ffmpeg_path=self.ffmpeg_path,
                width=width,
                height=height,
                fps=fps,
                color_grade=color_grade,
            )
            command_lines.append(segment_plan.readable_command)
            progress(
                JobStatus.RENDERING,
                0.71 + 0.05 * (index - 1) / total_steps,
                f"低内存模式：整理视频素材 {index}/{len(serial_segments)}",
            )
            completed = run_cancellable_process(
                segment_plan.as_list(),
                runner=self.command_runner,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if completed.returncode != 0 or not normalized_path.is_file():
                detail = (
                    completed.stderr
                    or completed.stdout
                    or "FFmpeg 低内存素材整理失败"
                )[-3000:]
                (job_dir / "low-memory-render-error.log").write_text(
                    detail,
                    encoding="utf-8",
                    newline="\n",
                )
                (job_dir / "render-command-low-memory.txt").write_text(
                    "\n\n".join(command_lines) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                return None, completed
            normalized_paths.append(normalized_path)

        if len(normalized_paths) == 1:
            assert completed is not None
            (job_dir / "render-command-low-memory.txt").write_text(
                "\n\n".join(command_lines) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            only_segment = serial_segments[0]
            return (
                VideoSegment(
                    path=normalized_paths[0],
                    source_duration=only_segment.duration,
                    duration=only_segment.duration,
                    source_width=float(width),
                    source_height=float(height),
                ),
                completed,
            )

        concat_list = fallback_dir / "segments.ffconcat"
        concat_lines = ["ffconcat version 1.0"]
        for path in normalized_paths:
            escaped = path.resolve().as_posix().replace("'", "'\\''")
            concat_lines.append(f"file '{escaped}'")
        concat_list.write_text(
            "\n".join(concat_lines) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        stitched_path = fallback_dir / "stitched.mp4"
        concat_command = [
            str(self.ffmpeg_path),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-fflags",
            "+genpts",
            "-i",
            str(concat_list),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            str(stitched_path),
        ]
        command_lines.append(subprocess.list2cmdline(concat_command))
        (job_dir / "render-command-low-memory.txt").write_text(
            "\n\n".join(command_lines) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        progress(JobStatus.RENDERING, 0.76, "低内存模式：拼接已整理素材")
        completed = run_cancellable_process(
            concat_command,
            runner=self.command_runner,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if completed.returncode != 0 or not stitched_path.is_file():
            detail = (
                completed.stderr
                or completed.stdout
                or "FFmpeg 低内存素材拼接失败"
            )[-3000:]
            (job_dir / "low-memory-render-error.log").write_text(
                detail,
                encoding="utf-8",
                newline="\n",
            )
            return None, completed

        stitched_duration = sum(item.duration for item in serial_segments)
        return (
            VideoSegment(
                path=stitched_path,
                source_duration=stitched_duration,
                duration=stitched_duration,
                source_width=float(width),
                source_height=float(height),
            ),
            completed,
        )

    def _ensure_decodable_media(
        self,
        *,
        video_folder: str,
        music_folder: str,
        mood: str,
        target_duration: float,
        duration_resolver: Callable[[Path], float],
        variant_seed: int | str | bytes | None,
        segments: Sequence[Any],
        music: Any,
        selection_report: dict[str, Any],
        warnings: list[str],
        playback_speed: float = 1.0,
        video_transition: str = "cut",
        bgm_mode: str = "auto",
    ) -> tuple[list[Any], Any]:
        """Skip locally unreadable media once before building the render graph."""

        ffmpeg = self.ffmpeg_path
        # Unit-test and externally embedded runners may intentionally provide a
        # symbolic executable.  Normal desktop/worker startup resolves FFmpeg
        # to an existing absolute file before production reaches this point.
        if ffmpeg is None or not Path(ffmpeg).is_file():
            return list(segments), music

        def video_failures(items: Sequence[Any]) -> dict[Path, str]:
            failures: dict[Path, str] = {}
            seen: set[str] = set()
            for segment in items:
                path = Path(segment.path).expanduser().resolve(strict=False)
                key = os.path.normcase(str(path))
                if key in seen:
                    continue
                seen.add(key)
                passed, detail = _decode_media_sample(
                    ffmpeg,
                    path,
                    "video",
                    duration_seconds=float(
                        getattr(segment, "source_duration", 0.0) or 0.0
                    ),
                    runner=self.command_runner,
                )
                if not passed:
                    failures[path] = detail
            return failures

        selected_segments = list(segments)
        selected_music = music
        failed_videos = video_failures(selected_segments)
        music_path: Path | None = None
        music_passed = True
        if selected_music is not None:
            music_path = Path(selected_music.path).expanduser().resolve(strict=False)
            music_passed, _music_detail = _decode_media_sample(
                ffmpeg,
                music_path,
                "audio",
                duration_seconds=float(getattr(selected_music, "duration", 0.0) or 0.0),
                runner=self.command_runner,
            )
        if not failed_videos and music_passed:
            selection_report["decode_preflight"] = "passed"
            return selected_segments, selected_music

        if failed_videos:
            failed_names = "、".join(sorted(path.name for path in failed_videos))
            try:
                selected_segments = _plan_category_video_segments(
                    video_folder,
                    target_duration,
                    mood=mood,
                    duration_resolver=duration_resolver,
                    variant_seed=variant_seed,
                    selection_report=selection_report,
                    warnings=warnings,
                    excluded_paths=tuple(failed_videos),
                    playback_speed=playback_speed,
                    video_transition=video_transition,
                )
            except PipelineError as error:
                raise PipelineError(
                    f"视频素材“{failed_names}”无法解码，并且员工本机素材文件夹中"
                    "没有可替换的可用视频。请删除或重新下载损坏文件，或补充 "
                    "MP4/MOV/MKV/WEBM 素材后重试。"
                ) from error
            warnings.append(f"已跳过无法解码的视频素材并自动更换：{failed_names}")
            selection_report["excluded_undecodable_videos"] = sorted(
                path.name for path in failed_videos
            )

        if not music_passed and music_path is not None:
            if bgm_mode == "manual":
                raise PipelineError(
                    f"手动选择的背景音乐“{music_path.name}”无法解码。请更换文件，"
                    "或选择自动匹配/不使用背景音乐。"
                )
            try:
                selected_music = select_music_asset(
                    music_folder,
                    mood,
                    target_duration,
                    duration_resolver=duration_resolver,
                    excluded_paths=(music_path,),
                )
            except MediaError as error:
                raise PipelineError(
                    f"背景音乐“{music_path.name}”无法解码，并且员工本机音乐文件夹中"
                    "没有可替换的可用音频。请删除或重新下载损坏文件，或补充 "
                    "常见音频格式的素材后重试。"
                ) from error
            warnings.append(
                f"已跳过无法解码的背景音乐并自动更换：{music_path.name}"
            )
            selection_report["excluded_undecodable_music"] = music_path.name

        replacement_video_failures = video_failures(selected_segments)
        replacement_music_path: Path | None = None
        replacement_music_passed = True
        if selected_music is not None:
            replacement_music_path = Path(selected_music.path).expanduser().resolve(
                strict=False
            )
            replacement_music_passed, _replacement_music_detail = _decode_media_sample(
                ffmpeg,
                replacement_music_path,
                "audio",
                duration_seconds=float(getattr(selected_music, "duration", 0.0) or 0.0),
                runner=self.command_runner,
            )
        unresolved: list[str] = []
        if replacement_video_failures:
            unresolved.append(
                "视频 "
                + "、".join(
                    sorted(path.name for path in replacement_video_failures)
                )
            )
        if not replacement_music_passed and replacement_music_path is not None:
            unresolved.append(f"音乐 {replacement_music_path.name}")
        if unresolved:
            # One automatic replacement is intentional.  Continuing to cycle
            # through an employee's whole disk would make a bad batch appear
            # hung and could repeatedly open damaged files.
            raise PipelineError(
                "素材解码预检失败：自动更换后仍无法解码 "
                + "；".join(unresolved)
                + "。请检查员工本机文件是否损坏，或其编码是否受当前 FFmpeg 支持。"
            )

        selection_report["decode_preflight"] = "replaced"
        return selected_segments, selected_music

    def _validated_optional_image(
        self,
        image_path: str | os.PathLike[str] | None,
        *,
        asset_label: str,
        fallback_label: str,
        warnings: list[str],
    ) -> Path | None:
        """Validate a cover/logo frame and safely omit a broken optional image."""

        if not image_path:
            return None
        path = Path(image_path).expanduser().resolve(strict=False)
        if not path.is_file():
            warnings.append(
                f"{asset_label}“{path.name or '未命名文件'}”不存在或当前账号无权读取，"
                f"已{fallback_label}并继续生成；请在资料库重新上传后重试。"
            )
            return None
        ffmpeg = self.ffmpeg_path
        if ffmpeg is None or not Path(ffmpeg).is_file():
            return path
        passed, _detail = _decode_media_sample(
            ffmpeg,
            path,
            "image",
            runner=self.command_runner,
        )
        if passed:
            return path
        warnings.append(
            f"{asset_label}“{path.name}”无法解码，已{fallback_label}并继续生成；"
            "请在资料库重新上传正确的 JPG/PNG/WEBP 文件。"
        )
        return None

    def _publish_transaction_path(self, job_id: str) -> Path:
        clean = safe_component(job_id, fallback="job")[:64]
        digest = hashlib.sha256(job_id.encode("utf-8", errors="replace")).hexdigest()[:12]
        return self._publish_transaction_root / f"{clean}-{digest}.json"

    @staticmethod
    def _publish_backup_path(staging_dir: Path, index: int, final_path: Path) -> Path:
        return staging_dir / f".previous-{index:02d}{final_path.suffix}.bak"

    def _begin_publish_transaction(
        self,
        *,
        job_id: str,
        mode: str,
        staging_dir: Path,
        artifacts: Sequence[tuple[str, Path, Path]],
    ) -> tuple[Path, dict[str, Any]]:
        """Create the recovery journal before any publish-stage output exists."""

        journal_path = self._publish_transaction_path(job_id)
        with self._publish_transaction_lock:
            if journal_path.is_file():
                self._recover_publish_transaction(journal_path)
                if journal_path.is_file():
                    # Recovery deliberately leaves the journal in place when
                    # a half-published final is locked or no longer matches its
                    # frozen signature.  A retry with the same durable job ID
                    # must not erase that staging directory and overwrite the
                    # only remaining recovery evidence.
                    raise PipelineError(
                        "A previous publish transaction still requires recovery."
                    )
            # This exact per-job directory can only contain private output
            # from an earlier interrupted attempt with the same durable ID.
            if staging_dir.parent.name != ".storyforge-staging":
                raise PipelineError("Invalid publish staging directory.")
            try:
                shutil.rmtree(staging_dir)
            except FileNotFoundError:
                pass
            payload: dict[str, Any] = {
                "schema_version": 1,
                "job_id": job_id,
                "mode": mode,
                "state": "preparing",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "staging_dir": str(staging_dir.resolve()),
                "artifacts": [],
            }
            for index, (kind, staged, final) in enumerate(artifacts, start=1):
                payload["artifacts"].append(
                    {
                        "kind": kind,
                        "staged": str(staged.resolve()),
                        "final": str(final.resolve()),
                        "backup": str(
                            self._publish_backup_path(staging_dir, index, final).resolve()
                        ),
                        "signature": None,
                    }
                )
            _write_json_atomic(journal_path, payload)
            # The journal precedes the staging directory, so there is no
            # process-kill window in which real staged media can exist without
            # a startup recovery record.
            staging_dir.mkdir(parents=True, exist_ok=True)
        return journal_path, payload

    def _mark_publish_transaction_ready(
        self,
        journal_path: Path,
        payload: dict[str, Any],
    ) -> None:
        """Freeze both staged artifact identities before touching final names."""

        with self._publish_transaction_lock:
            for artifact in payload.get("artifacts") or []:
                artifact["signature"] = _publish_signature(Path(artifact["staged"]))
            payload["state"] = "ready"
            _write_json_atomic(journal_path, payload)

    @staticmethod
    def _cleanup_publish_transaction_files(payload: dict[str, Any]) -> None:
        staging_dir = Path(str(payload.get("staging_dir") or ""))
        if staging_dir.parent.name == ".storyforge-staging":
            try:
                shutil.rmtree(staging_dir)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            _remove_empty_directory(staging_dir.parent)

    def _rollback_publish_transaction(
        self,
        journal_path: Path,
        payload: dict[str, Any],
    ) -> bool:
        """Remove new uncommitted outputs and restore any prior completed pair."""

        artifacts = list(payload.get("artifacts") or [])
        state = str(payload.get("state") or "")
        publish_started = (
            state == "backed_up"
            or state == "committed"
            or state.startswith("published_")
        )
        if publish_started:
            # Once final names may contain output from this transaction, an
            # existing file whose frozen signature no longer matches is not
            # safe to delete and not safe to forget.  Sync software, a media
            # tool or another process may have touched it after a hard kill.
            # Preserve every recovery artifact and the journal for a later or
            # manual resolution instead of making a half-published pair
            # permanent by discarding the only recovery evidence.
            for artifact in artifacts:
                final = Path(str(artifact.get("final") or ""))
                if final.exists() and not _matches_publish_signature(
                    final, artifact.get("signature")
                ):
                    return False
        for artifact in artifacts:
            final = Path(str(artifact.get("final") or ""))
            signature = artifact.get("signature")
            if _matches_publish_signature(final, signature):
                try:
                    final.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    # On Windows Explorer, a media player or an antivirus may
                    # briefly hold the just-published file open.  Never discard
                    # the journal/staging directory in that case: doing so would
                    # make the exposed half-pair permanent and unrecoverable.
                    return False
        for artifact in artifacts:
            final = Path(str(artifact.get("final") or ""))
            backup = Path(str(artifact.get("backup") or ""))
            if backup.is_file():
                try:
                    final.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, final)
                except OSError:
                    # Keep the journal and backup for the next recovery pass.
                    return False
        self._cleanup_publish_transaction_files(payload)
        try:
            journal_path.unlink()
        except OSError:
            pass
        _remove_empty_directory(journal_path.parent)
        return True

    def _abort_publish_transaction(
        self,
        journal_path: Path,
        payload: dict[str, Any],
    ) -> None:
        with self._publish_transaction_lock:
            self._rollback_publish_transaction(journal_path, payload)

    def _commit_publish_transaction(
        self,
        journal_path: Path,
        payload: dict[str, Any],
    ) -> None:
        """Publish a complete artifact set and leave a recoverable commit marker."""

        with self._publish_transaction_lock:
            artifacts = list(payload.get("artifacts") or [])
            if payload.get("state") != "ready" or not artifacts:
                raise PipelineError("Publish transaction is not ready.")
            try:
                # Preserve a previous completed attempt until this replacement
                # pair has itself reached the committed state.
                for artifact in artifacts:
                    final = Path(artifact["final"])
                    backup = Path(artifact["backup"])
                    if final.is_file():
                        os.replace(final, backup)
                payload["state"] = "backed_up"
                _write_json_atomic(journal_path, payload)

                # Video is deliberately first: even a hard process kill can
                # never expose the reusable MP3 without its matching MP4.
                for index, artifact in enumerate(artifacts, start=1):
                    staged = Path(artifact["staged"])
                    final = Path(artifact["final"])
                    final.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staged, final)
                    payload["state"] = f"published_{index}"
                    _write_json_atomic(journal_path, payload)
                if not all(
                    _matches_publish_signature(
                        Path(artifact["final"]), artifact.get("signature")
                    )
                    for artifact in artifacts
                ):
                    raise OSError("Published artifact verification failed.")
                payload["state"] = "committed"
                _write_json_atomic(journal_path, payload)
            except BaseException:
                self._rollback_publish_transaction(journal_path, payload)
                raise

            self._cleanup_publish_transaction_files(payload)
            try:
                journal_path.unlink()
            except OSError:
                pass
            _remove_empty_directory(journal_path.parent)

    def _recover_publish_transaction(self, journal_path: Path) -> str | None:
        """Resolve one prior transaction by keeping only a verified committed set."""

        try:
            payload = json.loads(journal_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("transaction payload is not an object")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            try:
                journal_path.unlink()
            except OSError:
                pass
            return f"Ignored corrupt publish transaction {journal_path.name}: {error}"

        artifacts = list(payload.get("artifacts") or [])
        committed = payload.get("state") == "committed" and bool(artifacts)
        committed = committed and all(
            _matches_publish_signature(
                Path(str(artifact.get("final") or "")),
                artifact.get("signature"),
            )
            for artifact in artifacts
        )
        if committed:
            self._cleanup_publish_transaction_files(payload)
            try:
                journal_path.unlink()
            except OSError:
                pass
            _remove_empty_directory(journal_path.parent)
            return f"Recovered committed publish transaction {journal_path.name}."

        rolled_back = self._rollback_publish_transaction(journal_path, payload)
        if rolled_back:
            return f"Rolled back incomplete publish transaction {journal_path.name}."
        return f"Deferred rollback of locked publish transaction {journal_path.name}."

    def _recover_publish_transactions(self) -> list[str]:
        warnings: list[str] = []
        with self._publish_transaction_lock:
            try:
                journals = sorted(self._publish_transaction_root.glob("*.json"))
            except OSError as error:
                return [f"Unable to scan publish transactions: {error}"]
            for journal_path in journals:
                try:
                    message = self._recover_publish_transaction(journal_path)
                except BaseException as error:
                    warnings.append(
                        f"Unable to recover publish transaction {journal_path.name}: {error}"
                    )
                else:
                    if message:
                        warnings.append(message)
        return warnings

    def _run_quality_check(
        self,
        media_path: Path,
        expectation: QualityExpectation,
    ) -> QualityReport:
        try:
            return self.quality_checker(
                media_path,
                expectation,
                ffmpeg_path=self.ffmpeg_path,
            )
        except JobCancelledError:
            raise
        except Exception as error:
            detail = str(error) or type(error).__name__
            return QualityReport(
                passed=False,
                backend="quality-checker-error",
                elapsed_ms=0,
                media={},
                checks=(
                    QualityCheck(
                        name="quality_checker",
                        passed=False,
                        expected="successful probe",
                        actual=type(error).__name__,
                        message=detail,
                    ),
                ),
                errors=(detail,),
            )

    @staticmethod
    def _write_quality_log(path: Path, report: QualityReport) -> None:
        path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _export_narration_mp3(self, source: Path, destination: Path) -> Path:
        """Encode the merged narration once to a broadly compatible MP3."""

        if self.ffmpeg_path is None:
            raise PipelineError("未找到 FFmpeg，无法导出配音。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.stem}-",
            suffix=".partial.mp3",
            dir=destination.parent,
        )
        os.close(handle)
        temporary_path = Path(temporary_name)
        command = [
            str(self.ffmpeg_path),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-map_metadata",
            "-1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-f",
            "mp3",
            str(temporary_path),
        ]
        try:
            completed = run_cancellable_process(
                command,
                runner=self.command_runner,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if completed.returncode != 0 or not temporary_path.is_file():
                detail = (
                    completed.stderr or completed.stdout or "未知 FFmpeg 配音导出错误"
                )[-1200:]
                raise PipelineError(f"FFmpeg 配音导出失败：{detail}")
            os.replace(temporary_path, destination)
            return destination
        except BaseException:
            try:
                temporary_path.unlink()
            except OSError:
                pass
            raise

    def _prepare_existing_narration(
        self,
        source: Path,
        destination: Path,
        units: Sequence[NarrationUnit],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> NarrationAssembly:
        """Decode an existing narration and build deterministic caption timing.

        This mode is intentionally visual-only: it never calls the text or TTS
        providers for the audio.  Sentence intervals are apportioned by spoken
        word weight over the real decoded duration so existing StoryForge MP3s
        can be reused with a new material recipe.
        """

        if self.ffmpeg_path is None:
            raise PipelineError("未找到 FFmpeg，无法读取已有配音。")
        if not source.is_file():
            raise PipelineError("请选择员工本机存在的配音文件。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.ffmpeg_path),
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-map_metadata",
            "-1",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "1",
            str(destination),
        ]
        completed = run_cancellable_process(
            command,
            runner=self.command_runner,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if completed.returncode != 0 or not destination.is_file():
            detail = (completed.stderr or completed.stdout or "未知音频解码错误")[-1600:]
            raise PipelineError(f"已有配音无法解码：{detail}")
        try:
            with wave.open(str(destination), "rb") as stream:
                duration = stream.getnframes() / max(1, stream.getframerate())
        except (OSError, wave.Error) as error:
            raise PipelineError(f"已有配音解码结果无效：{error}") from error
        raw_cues = metadata.get("cues") if isinstance(metadata, dict) else None
        if isinstance(raw_cues, list):
            cues_from_metadata: list[SubtitleCue] = []
            try:
                for item in raw_cues:
                    if not isinstance(item, dict):
                        raise ValueError("invalid cue")
                    cue = SubtitleCue(
                        float(item["start"]),
                        float(item["end"]),
                        str(item["text"]),
                    )
                    if cue.end > duration + 0.25:
                        raise ValueError("cue exceeds audio")
                    cues_from_metadata.append(cue)
            except (KeyError, TypeError, ValueError):
                cues_from_metadata = []
            if cues_from_metadata:
                return NarrationAssembly(destination, tuple(cues_from_metadata), duration)

        spoken = [item for item in units if not item.is_chapter_break and item.text.strip()]
        if not spoken or not math.isfinite(duration) or duration <= 0:
            raise PipelineError("已有配音或当前小说文案没有可用于字幕对齐的内容。")
        weights = [
            max(
                1.0,
                float(len(re.findall(r"\b[\w'’\-]+\b", item.text)))
                + 0.3 * len(re.findall(r"[,;:!?]", item.text)),
            )
            for item in spoken
        ]
        total_weight = sum(weights)
        elapsed = 0.0
        cues: list[SubtitleCue] = []
        for index, (unit, weight) in enumerate(zip(spoken, weights, strict=True)):
            start = duration * elapsed / total_weight
            elapsed += weight
            end = duration if index == len(spoken) - 1 else duration * elapsed / total_weight
            cues.append(SubtitleCue(start, end, unit.text))
        return NarrationAssembly(destination, tuple(cues), duration)

    def _stage_narration_mp3(
        self,
        narration: NarrationAssembly,
        destination: Path,
        *,
        existing_source: Path | None = None,
    ) -> Path:
        """Publish an existing MP3 without a needless second lossy encode."""

        if existing_source is not None and existing_source.suffix.casefold() == ".mp3":
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(existing_source, destination)
            return destination
        return self._export_narration_mp3(narration.path, destination)

    @staticmethod
    def _narration_audio_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _narration_metadata_path(self, audio_path: Path) -> Path:
        digest = self._narration_audio_digest(audio_path)
        return default_data_dir() / "narration-index" / f"{digest}.json"

    @staticmethod
    def _id3_syncsafe(value: int) -> bytes:
        if not 0 <= int(value) <= 0x0FFFFFFF:
            raise ValueError("ID3 payload is too large")
        number = int(value)
        return bytes(
            (
                (number >> 21) & 0x7F,
                (number >> 14) & 0x7F,
                (number >> 7) & 0x7F,
                number & 0x7F,
            )
        )

    @staticmethod
    def _id3_syncsafe_value(value: bytes) -> int:
        if len(value) != 4 or any(item & 0x80 for item in value):
            raise ValueError("invalid ID3 syncsafe integer")
        return (
            (value[0] << 21)
            | (value[1] << 14)
            | (value[2] << 7)
            | value[3]
        )

    @classmethod
    def _embedded_narration_metadata(cls, audio_path: Path) -> dict[str, Any] | None:
        """Read StoryForge's portable alignment index from an MP3 ID3 tag."""

        try:
            with audio_path.open("rb") as stream:
                header = stream.read(10)
                if len(header) != 10 or header[:3] != b"ID3":
                    return None
                version = int(header[3])
                if version not in {3, 4}:
                    return None
                tag_size = cls._id3_syncsafe_value(header[6:10])
                if tag_size <= 0 or tag_size > _NARRATION_METADATA_LIMIT:
                    return None
                tag = stream.read(tag_size)
        except (OSError, ValueError):
            return None
        if len(tag) != tag_size:
            return None
        offset = 0
        while offset + 10 <= len(tag):
            frame_id = tag[offset : offset + 4]
            if frame_id == b"\0\0\0\0":
                break
            raw_size = tag[offset + 4 : offset + 8]
            try:
                frame_size = (
                    cls._id3_syncsafe_value(raw_size)
                    if version == 4
                    else int.from_bytes(raw_size, "big")
                )
            except ValueError:
                return None
            offset += 10
            if frame_size <= 0 or offset + frame_size > len(tag):
                return None
            frame = tag[offset : offset + frame_size]
            offset += frame_size
            if frame_id != b"TXXX" or len(frame) < 3 or frame[0] != 3:
                continue
            try:
                description, encoded = frame[1:].split(b"\0", 1)
                if description.decode("utf-8") != _NARRATION_ID3_DESCRIPTION:
                    continue
                packed_text = encoded.decode("ascii")
                if not packed_text.startswith("SF1:"):
                    return None
                packed = base64.urlsafe_b64decode(packed_text[4:].encode("ascii"))
                decompressor = zlib.decompressobj()
                raw = decompressor.decompress(packed, _NARRATION_METADATA_LIMIT)
                if not decompressor.eof or decompressor.unconsumed_tail:
                    return None
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError, zlib.error, json.JSONDecodeError):
                return None
            return value if isinstance(value, dict) else None
        return None

    @classmethod
    def _embed_narration_metadata(
        cls,
        audio_path: Path,
        metadata: dict[str, Any],
    ) -> None:
        """Embed the complete alignment index in the MP3 without re-encoding audio.

        The portable tag follows the MP3 to another employee computer, while
        the employee-facing folder still contains only the requested MP3/MP4.
        """

        raw = json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        packed = "SF1:" + base64.urlsafe_b64encode(
            zlib.compress(raw, level=9)
        ).decode("ascii")
        frame_payload = (
            b"\x03"
            + _NARRATION_ID3_DESCRIPTION.encode("utf-8")
            + b"\0"
            + packed.encode("ascii")
        )
        frame = (
            b"TXXX"
            + cls._id3_syncsafe(len(frame_payload))
            + b"\0\0"
            + frame_payload
        )
        tag = b"ID3\x04\x00\x00" + cls._id3_syncsafe(len(frame)) + frame
        existing_offset = 0
        try:
            with audio_path.open("rb") as source:
                header = source.read(10)
                if len(header) == 10 and header[:3] == b"ID3":
                    existing_size = cls._id3_syncsafe_value(header[6:10])
                    footer_size = 10 if header[3] == 4 and header[5] & 0x10 else 0
                    candidate = 10 + existing_size + footer_size
                    if candidate <= audio_path.stat().st_size:
                        existing_offset = candidate
                source.seek(existing_offset)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{audio_path.stem}-metadata-",
                    suffix=".mp3",
                    dir=audio_path.parent,
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(tag)
                    shutil.copyfileobj(source, temporary, length=1024 * 1024)
                    temporary.flush()
                    os.fsync(temporary.fileno())
            os.replace(temporary_path, audio_path)
        except BaseException:
            try:
                temporary_path.unlink()
            except (NameError, OSError):
                pass
            raise

    @staticmethod
    def _narration_metadata_value(
        *,
        job: RenderJob,
        promo_code: str,
        text_result: TextResult,
        narration: NarrationAssembly,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "novel_id": job.novel_id,
            "episode_ids": list(job.episode_ids),
            "promo_code": promo_code,
            "text": text_result.to_dict(),
            "duration_seconds": narration.duration_seconds,
            "cues": [
                {"start": cue.start, "end": cue.end, "text": cue.text}
                for cue in narration.cues
            ],
        }

    def _load_narration_metadata(self, audio_path: Path) -> dict[str, Any] | None:
        if not audio_path.is_file():
            return None
        try:
            path = self._narration_metadata_path(audio_path)
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict):
            return value
        return self._embedded_narration_metadata(audio_path)

    def _save_narration_metadata(
        self,
        audio_path: Path,
        *,
        job: RenderJob,
        promo_code: str,
        text_result: TextResult,
        narration: NarrationAssembly,
    ) -> None:
        """Keep private alignment metadata without cluttering publish folders."""

        try:
            path = self._narration_metadata_path(audio_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            value = self._narration_metadata_value(
                job=job,
                promo_code=promo_code,
                text_result=text_result,
                narration=narration,
            )
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, path)
        except OSError:
            # The MP3/MP4 product is already valid; a private convenience index
            # must never convert a successful publish into a failed job.
            return

    def __call__(
        self,
        job: RenderJob,
        platform: PlatformProfile,
        progress: ProgressCallback,
    ) -> str:
        """Run one job and never retain reproducible large WAV intermediates."""

        job_dir = job_workspace_directory(job, self.work_root)
        resource_scope = self.heavy_resource_lock or nullcontext()
        with resource_scope:
            try:
                return self._run_job(job, platform, progress)
            finally:
                # In-process Kokoro retains model tensors between calls.  Keeping
                # those allocations alive while FFmpeg starts its 1080x1920 graph
                # can push an otherwise healthy employee PC over its memory limit.
                release_embedded_kokoro_runtime()
                # Preview narration is still exposed as a review artifact. Full
                # production already publishes the reusable MP3, so its private
                # sentence and merged WAVs only waste the employee's system disk.
                if job.job_kind != "preview":
                    _cleanup_large_job_work_files(job_dir)

    def _run_job(
        self,
        job: RenderJob,
        platform: PlatformProfile,
        progress: ProgressCallback,
    ) -> str:
        raise_if_cancelled()
        live_settings = self.settings_getter()
        settings = self._settings_for_job(live_settings, job)
        if job.job_kind != "preview":
            job.narration_audio_file = ""
        if self.ffmpeg_path is None:
            raise PipelineError("未找到 FFmpeg，无法生成视频。")
        source_path = Path(job.source_file)
        if not source_path.is_file():
            raise PipelineError(f"小说文件不存在：{source_path}")
        effective_code = job.promo_code_snapshot or job.code
        publish_dir = publish_batch_directory(job, platform)
        job.publish_batch_folder = str(publish_dir)
        job_dir = job_workspace_directory(job, self.work_root)
        work_dir = job_dir / ".work"
        audio_dir = work_dir / "voice"
        job_dir.mkdir(parents=True, exist_ok=True)
        audio_dir.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []

        if job.job_kind == "preview":
            return self._render_preview(
                job,
                platform,
                progress,
                settings=settings,
                source_path=source_path,
                job_dir=job_dir,
                work_dir=work_dir,
                audio_dir=audio_dir,
            )

        progress(JobStatus.PREFLIGHT, 0.04, "分析小说")
        original = read_manuscript(source_path)
        analysis = analyze_manuscript(
            original,
            source_path.name,
            wpm=settings.narration_wpm,
            chapter_pause_seconds=settings.chapter_pause_seconds,
        )
        _preflight_workspace_directory(
            self.work_root,
            duration_seconds=max(1.0, analysis.estimated_duration_seconds),
        )
        output_mode = production_output_mode(job)
        publish_narration_audio = production_exports_narration_audio(job)
        existing_narration_source: Path | None = None
        reuse_metadata: dict[str, Any] | None = None
        if output_mode == "reuse_audio":
            existing_narration_source = Path(
                str(
                    job.settings_snapshot.get("source_narration_audio")
                    or getattr(settings, "source_narration_audio", "")
                    or ""
                )
            ).expanduser().resolve(strict=False)
            if not existing_narration_source.is_file():
                raise PipelineError("请选择员工本机存在的已有配音。")
            reuse_metadata = self._load_narration_metadata(existing_narration_source)
            if reuse_metadata is not None:
                indexed_code = str(reuse_metadata.get("promo_code") or "")
                if indexed_code and indexed_code != effective_code:
                    raise PipelineError(
                        f"已有配音朗读的口令是 {indexed_code}，当前选择的是 "
                        f"{effective_code}。请改回原口令，避免成片引导错误。"
                    )
                indexed_novel = str(reuse_metadata.get("novel_id") or "")
                if indexed_novel and job.novel_id and indexed_novel != job.novel_id:
                    raise PipelineError("已有配音不属于当前小说，请重新选择正确的配音文件。")
            else:
                raise PipelineError(
                    "这段配音缺少 StoryForge 的小说、口令和字幕索引，无法保证配音与字幕一致。"
                    "请选择本版 StoryForge 输出的 MP3 配音或原生成电脑上的成品视频。"
                )
        playback_speed = float(getattr(settings, "video_playback_speed", 1.0) or 1.0)
        if not 0.8 <= playback_speed <= 3.0:
            raise PipelineError("视频素材速度必须在 0.8–3.0× 之间。")
        video_transition = str(
            getattr(settings, "video_transition", "cut") or "cut"
        ).strip().casefold()
        if video_transition not in {"cut", "fade"}:
            raise PipelineError("视频素材拼接方式必须是硬切或 0.2 秒柔和过渡。")
        bgm_mode = str(getattr(settings, "bgm_mode", "auto") or "auto").strip().casefold()
        if bgm_mode not in {"auto", "manual", "none"}:
            raise PipelineError("背景音乐模式必须是自动匹配、手动指定或不使用。")
        # Fail fast on an employee-local disconnected/read-only/full output
        # disk before AI polishing or TTS spends several minutes and credits.
        # The exact duration is checked again after narration assembly.
        _preflight_output_directory(
            job.output_folder,
            duration_seconds=max(1.0, analysis.estimated_duration_seconds),
            output_mode=output_mode,
            export_narration_audio=publish_narration_audio,
        )
        reminder_seconds = float(settings.max_episode_minutes) * 60.0
        if analysis.estimated_duration_seconds > reminder_seconds:
            warnings.append(
                f"预计成片超过 {settings.max_episode_minutes:g} 分钟；"
                "仍会按完整剧情生成，请确认目标账号支持该上传时长。"
            )
        marked_text = inject_chapter_markers(analysis)
        (job_dir / "01-original.txt").write_text(original, encoding="utf-8", newline="\n")

        progress(JobStatus.POLISHING, 0.12, "润色小说文稿")
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        recipe_hash = prepared_recipe_hash(job, platform, settings, source_sha256)
        job.content_fingerprint = source_sha256
        job.recipe_hash = recipe_hash
        prepared_path = work_dir / "prepared.json"
        text_result: TextResult | None = None
        indexed_text = reuse_metadata.get("text") if reuse_metadata is not None else None
        if isinstance(indexed_text, dict):
            try:
                text_result = TextResult(
                    polished_text=str(indexed_text["polished_text"]),
                    hook=str(indexed_text.get("hook") or ""),
                    ending_cta=str(indexed_text.get("ending_cta") or ""),
                    mood=str(indexed_text.get("mood") or "suspense"),
                    provider=str(indexed_text.get("provider") or "existing_audio"),
                    model=str(indexed_text.get("model") or ""),
                    retention_ratio=float(indexed_text.get("retention_ratio") or 1.0),
                )
            except (KeyError, TypeError, ValueError):
                text_result = None
        if text_result is None:
            text_result = self._load_prepared_text(prepared_path, recipe_hash)
        if text_result is None:
            text_request = TextRequest(
                text=marked_text,
                title=job.title,
                platform=platform.name,
                code=effective_code,
                ending_template=platform.render_ending(effective_code),
                adult_mode=settings.adult_mode,
                retention_min=settings.retention_min,
                retention_max=settings.retention_max,
                language=_prompt_language(job),
                creative_line_index=max(1, int(job.variant_index)),
                creative_line_count=max(
                    1,
                    int(job.variant_index),
                    int(getattr(job, "variant_count", 1)),
                ),
            )
            text_result = self._polish(text_request, settings, warnings)
            self._save_prepared_text(
                prepared_path,
                recipe_hash=recipe_hash,
                source_sha256=source_sha256,
                text_result=text_result,
                job=job,
                platform=platform,
            )
        else:
            warnings.append(
                "已复用原配音对应的文稿与字幕时间。"
                if reuse_metadata is not None
                else "已复用此前准备的润色文稿。"
            )
        selected_mood = str(job.story_mood or text_result.mood)
        try:
            mood = canonical_mood(selected_mood)
        except MediaError:
            mood = "suspense"
            warnings.append(
                f"未知情绪分类 {selected_mood!r}，题材、声音和音乐已回退到 suspense。"
            )
        units = narration_units(text_result)
        script_text = "\n".join(
            CHAPTER_MARKER if unit.is_chapter_break else unit.text for unit in units
        )
        (job_dir / "02-narration-script.txt").write_text(
            script_text, encoding="utf-8", newline="\n"
        )

        voice_profile = settings.voice_by_mood.get(mood, "dramatic")
        if output_mode == "reuse_audio":
            assert existing_narration_source is not None
            progress(JobStatus.NARRATING, 0.27, "读取已有配音并匹配字幕")
            narration = self._prepare_existing_narration(
                existing_narration_source,
                work_dir / "narration.wav",
                units,
                metadata=reuse_metadata,
            )
            actual_tts_provider = "existing_audio"
            actual_voice = "existing_audio"
            warnings.append("已复用现有配音；未再次调用配音服务。")
        else:
            voice_stage = (
                "首次加载本地女声模型并生成旁白"
                if settings.providers.tts_provider in {"local", "local_kokoro", "kokoro"}
                else "调用云端女声并生成旁白"
            )
            progress(JobStatus.NARRATING, 0.27, voice_stage)
            sentences, segment_unit_counts = group_narration_units(units)
            # The last-used voice is the default, not a permanent novel lock.
            # Employees may explicitly switch voice providers between batches.
            requested_locked_voice = (
                job.locked_voice_id
                if not job.locked_voice_provider
                or job.locked_voice_provider == settings.providers.tts_provider
                else ""
            )
            tts_result, actual_tts_provider, actual_voice = self._narrate(
                sentences,
                audio_dir,
                voice_profile,
                mood,
                settings,
                warnings,
                locked_voice_id=requested_locked_voice,
            )
            job.locked_voice_provider = actual_tts_provider
            job.locked_voice_id = actual_voice
            narration = assemble_narration_wav(
                tts_result,
                units,
                work_dir / "narration.wav",
                chapter_pause_seconds=settings.chapter_pause_seconds,
                segment_unit_counts=segment_unit_counts,
            )
        # Release embedded model ownership at the phase boundary, before any
        # media decoder or FFmpeg encoder is opened.  The wrapper repeats this
        # best-effort cleanup for failures occurring earlier in the job.
        release_embedded_kokoro_runtime()
        final_stem = publish_media_stem(job, platform)
        if output_mode == "audio_only":
            _preflight_output_directory(
                job.output_folder,
                duration_seconds=narration.duration_seconds,
                output_mode=output_mode,
            )
            narration_audio_path = publish_dir / f"{final_stem}.mp3"
            publish_staging_dir = (
                Path(job.output_folder).expanduser().resolve()
                / ".storyforge-staging"
                / safe_component(job.id, fallback="job")
            )
            staged_audio_path = publish_staging_dir / f"{final_stem}.partial.mp3"
            journal_path, publish_transaction = self._begin_publish_transaction(
                job_id=job.id,
                mode=output_mode,
                staging_dir=publish_staging_dir,
                artifacts=(("narration", staged_audio_path, narration_audio_path),),
            )
            progress(JobStatus.RENDERING, 0.68, "导出纯旁白配音")
            try:
                self._export_narration_mp3(narration.path, staged_audio_path)
                self._embed_narration_metadata(
                    staged_audio_path,
                    self._narration_metadata_value(
                        job=job,
                        promo_code=effective_code,
                        text_result=text_result,
                        narration=narration,
                    ),
                )
                raise_if_cancelled()
                self._mark_publish_transaction_ready(
                    journal_path, publish_transaction
                )
                self._commit_publish_transaction(
                    journal_path, publish_transaction
                )
                self._save_narration_metadata(
                    narration_audio_path,
                    job=job,
                    promo_code=effective_code,
                    text_result=text_result,
                    narration=narration,
                )
            except JobCancelledError:
                self._abort_publish_transaction(journal_path, publish_transaction)
                _remove_empty_directory(publish_dir)
                raise
            except (OSError, PipelineError) as error:
                self._abort_publish_transaction(journal_path, publish_transaction)
                _remove_empty_directory(publish_dir)
                detail = f"{type(error).__name__}: {error}"
                export_error_log = job_dir / "narration-export-error.log"
                export_error_log.write_text(detail, encoding="utf-8", newline="\n")
                (job_dir / "manifest.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "job": {
                                **job.to_dict(),
                                "status": "failed",
                                "stage_label": "纯旁白导出失败",
                            },
                            "output_mode": output_mode,
                            "result": {
                                "status": "failed",
                                "output_file": "",
                                "narration_audio_file": "",
                                "error_log": str(export_error_log),
                            },
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                failure_diagnostics = capture_failure_diagnostics(
                    export_error_log,
                    stage="narration_mp3_export",
                )
                raise PipelineError(
                    "纯旁白配音导出失败，详情见 narration-export-error.log。",
                    error_log=export_error_log,
                    failure_diagnostics=failure_diagnostics,
                ) from error

            job.narration_audio_file = str(narration_audio_path)
            job.message = "；".join(warnings)
            (job_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "job": {
                            **job.to_dict(),
                            "status": "completed",
                            "progress": 1.0,
                            "stage_label": "已完成",
                            "output_file": str(narration_audio_path),
                            "narration_audio_file": str(narration_audio_path),
                        },
                        "business": {
                            "novel_id": job.novel_id,
                            "episode_ids": list(job.episode_ids),
                            "episode_label": job.episode_label,
                            "promo_code_snapshot": effective_code,
                            "production_run_id": job.production_run_id,
                            "variant_index": job.variant_index,
                        },
                        "source_sha256": source_sha256,
                        "recipe_hash": recipe_hash,
                        "output_mode": output_mode,
                        "voice": {
                            "provider": actual_tts_provider,
                            "profile": voice_profile,
                            "voice": actual_voice,
                            "duration_seconds": narration.duration_seconds,
                        },
                        "warnings": warnings,
                        "result": {
                            "status": "completed",
                            "output_file": str(narration_audio_path),
                            "narration_audio_file": str(narration_audio_path),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            try:
                shutil.rmtree(audio_dir)
            except OSError:
                pass
            progress(JobStatus.RENDERING, 0.98, "整理配音输出")
            return str(narration_audio_path)

        end_card_duration = max(5.0, min(7.0, settings.end_card_seconds))
        story_card_template = settings.video_template == "platform_story_card"
        intro_card_duration = (
            max(2.5, min(8.0, float(settings.intro_card_duration_seconds)))
            if story_card_template
            else 0.0
        )
        platform_logo_path = self._validated_optional_image(
            _story_card_platform_logo(platform, story_card_template),
            asset_label="平台 Logo",
            fallback_label="改用无 Logo 的简介卡布局",
            warnings=warnings,
        )
        # A full production always reserves a closing cover window.  If an
        # unusually short input cannot fit it, pad the output instead of
        # silently dropping either the intro or narrated call to action.
        render_duration = max(
            narration.duration_seconds,
            end_card_duration
            + (intro_card_duration + 1.0 if story_card_template else 4.0),
        )
        _preflight_output_directory(
            job.output_folder,
            duration_seconds=render_duration,
            output_mode=output_mode,
        )

        progress(JobStatus.COMPOSING, 0.53, "编排字幕与素材")
        subtitle_path = work_dir / "subtitles.ass"
        full_subtitle_style = _subtitle_style(settings)
        cover_image_was_present = bool(
            job.cover_path and Path(job.cover_path).expanduser().is_file()
        )
        requested_cover = self._validated_optional_image(
            job.cover_path,
            asset_label="小说封面",
            fallback_label="改用纯字幕结尾",
            warnings=warnings,
        )
        effective_cover = requested_cover
        write_ass(
            subtitle_path,
            narration.cues,
            platform=platform.name,
            code=effective_code,
            search_text=platform.render_search(effective_code),
            video_duration=render_duration,
            video_template=settings.video_template,
            intro_card_text=job.intro_card_text,
            intro_headline=text_result.hook,
            intro_card_duration=intro_card_duration,
            final_label=_story_card_final_label(job, story_card_template),
            platform_logo_present=bool(platform_logo_path),
            intro_card_cover_present=bool(effective_cover),
            platform_brand_color=platform.brand_color,
            config=full_subtitle_style,
        )
        resolver = lambda path: probe_duration_with_ffmpeg(self.ffmpeg_path, path)
        variant_seed: int | str | bytes | None = (
            job.variant_seed
            if job.variant_seed or job.production_draft_id or job.novel_id
            else None
        )
        video_selection: dict[str, Any] = {}
        segments = _plan_category_video_segments(
            job.video_folder,
            render_duration,
            mood=mood,
            duration_resolver=resolver,
            variant_seed=variant_seed,
            selection_report=video_selection,
            warnings=warnings,
            playback_speed=playback_speed,
            video_transition=video_transition,
        )
        if bgm_mode == "none":
            music = None
        elif bgm_mode == "manual":
            manual_music = Path(
                str(getattr(settings, "bgm_file", "") or "")
            ).expanduser().resolve(strict=False)
            if not manual_music.is_file():
                raise PipelineError("请选择员工本机存在的背景音乐文件，或改用自动匹配。")
            try:
                manual_duration = float(resolver(manual_music))
            except (MediaError, OSError, TypeError, ValueError) as error:
                raise PipelineError(
                    f"无法读取手动背景音乐“{manual_music.name}”：{error}"
                ) from error
            if not math.isfinite(manual_duration) or manual_duration <= 0:
                raise PipelineError(f"手动背景音乐“{manual_music.name}”时长无效。")
            music = MusicPlan(
                manual_music,
                manual_duration,
                max(1, math.ceil(render_duration / manual_duration)),
                mood,
            )
        else:
            music = select_music_asset(
                job.music_folder,
                mood,
                render_duration,
                duration_resolver=resolver,
            )
        segments, music = self._ensure_decodable_media(
            video_folder=job.video_folder,
            music_folder=job.music_folder,
            mood=mood,
            target_duration=render_duration,
            duration_resolver=resolver,
            variant_seed=variant_seed,
            segments=segments,
            music=music,
            selection_report=video_selection,
            warnings=warnings,
            playback_speed=playback_speed,
            video_transition=video_transition,
            bgm_mode=bgm_mode,
        )
        segments = _segments_with_geometry(self.ffmpeg_path, segments)
        _preflight_output_directory(
            job.output_folder,
            duration_seconds=render_duration,
            output_mode=output_mode,
            serial_staging=len(segments) > 1,
            export_narration_audio=publish_narration_audio,
        )
        final_path = publish_dir / f"{final_stem}.mp4"
        # os.replace is atomic only on the same filesystem. Keep the private
        # staging area beside the selected output root rather than AppData so
        # D:/E: output folders remain safe as well.
        publish_staging_dir = (
            Path(job.output_folder).expanduser().resolve()
            / ".storyforge-staging"
            / safe_component(job.id, fallback="job")
        )
        staged_video_path = publish_staging_dir / f"{final_stem}.partial.mp4"
        narration_audio_path = publish_dir / f"{final_stem}.mp3"
        staged_audio_path = publish_staging_dir / f"{final_stem}.partial.mp3"
        publish_artifacts: tuple[tuple[str, Path, Path], ...] = (
            ("video", staged_video_path, final_path),
        )
        if publish_narration_audio:
            # Compatibility for already queued 0.4.x work that explicitly
            # promised an MP4+MP3 pair. New regular-video jobs omit this flag.
            publish_artifacts += (
                ("narration", staged_audio_path, narration_audio_path),
            )
        journal_path, publish_transaction = self._begin_publish_transaction(
            job_id=job.id,
            mode=output_mode,
            staging_dir=publish_staging_dir,
            artifacts=publish_artifacts,
        )
        intro_card_media = _intro_card_media_options(
            settings,
            effective_cover,
            intro_card_text=job.intro_card_text,
            code=effective_code,
            style=full_subtitle_style,
            intro_duration=intro_card_duration,
        )
        effective_cover_outro = bool(
            _cover_outro_enabled(settings, job)
            and (effective_cover or not cover_image_was_present)
        )
        encoders = available_encoders(self.ffmpeg_path)
        encoder = (
            settings.video_encoder
            if settings.video_encoder != "auto"
            else (encoders[0] if encoders else "libx264")
        )
        render_segments = list(segments)
        render_color_grade = getattr(settings, "color_grade", "neutral")
        render_transition = video_transition
        serial_render_prepared = False
        render_progress = _FFmpegProgressTracker(progress)
        if len(segments) > 1:
            # A multi-input graph retains one decoder/filter chain per clip.
            # At 1080x1920/60 this can exhaust a normal employee workstation
            # before the encoder starts. Normalize deterministically one clip
            # at a time instead of first making a known-risk attempt.
            warnings.append(
                "多素材任务已使用逐段安全渲染，避免同时解码全部素材。"
            )
            render_progress(JobStatus.RENDERING, 0.64, "逐段整理视频素材")
            try:
                serial_source, serial_completed = self._prepare_low_memory_video_source(
                    segments=segments,
                    staging_dir=publish_staging_dir,
                    job_dir=job_dir,
                    width=settings.output_width,
                    height=settings.output_height,
                    fps=settings.output_fps,
                    color_grade=render_color_grade,
                    video_transition=video_transition,
                    progress=render_progress,
                )
            except JobCancelledError:
                self._abort_publish_transaction(journal_path, publish_transaction)
                _remove_empty_directory(publish_dir)
                raise
            except (MediaError, OSError, PipelineError, ValueError) as error:
                serial_source = None
                serial_completed = subprocess.CompletedProcess(
                    [str(self.ffmpeg_path)],
                    1,
                    stdout="",
                    stderr=f"Safe serial preparation failed: {error}",
                )
            if serial_source is None:
                self._abort_publish_transaction(journal_path, publish_transaction)
                _remove_empty_directory(publish_dir)
                detail = (
                    serial_completed.stderr
                    or serial_completed.stdout
                    or "FFmpeg safe serial preparation failed"
                )[-3000:]
                render_error_log = job_dir / "render-error.log"
                render_error_log.write_text(detail, encoding="utf-8", newline="\n")
                diagnostics = capture_failure_diagnostics(
                    render_error_log,
                    stage="ffmpeg_serial_prepare",
                    return_code=int(serial_completed.returncode),
                )
                raise PipelineError(
                    f"视频素材逐段整理失败：{diagnostics.get('summary') or 'FFmpeg 处理失败。'}",
                    error_log=render_error_log,
                    failure_diagnostics=diagnostics,
                )
            render_segments = [serial_source]
            render_color_grade = "neutral"
            render_transition = "cut"
            serial_render_prepared = True
        plan = build_ffmpeg_plan(
            render_segments,
            narration.path,
            music,
            subtitle_path,
            staged_video_path,
            render_duration,
            ffmpeg_path=self.ffmpeg_path,
            width=settings.output_width,
            height=settings.output_height,
            fps=settings.output_fps,
            bgm_volume=settings.bgm_volume,
            video_encoder=encoder,
            cover_path=effective_cover,
            cover_intro_start=2.0,
            cover_intro_duration=2.0,
            end_card_duration=end_card_duration,
            render_mode=settings.render_mode,
            cover_animation=settings.cover_animation,
            cover_outro_enabled=effective_cover_outro,
            color_grade=render_color_grade,
            end_card_without_cover=True,
            cover_intro_enabled=False,
            platform_logo_path=platform_logo_path,
            platform_logo_duration=intro_card_duration,
            video_transition=render_transition,
            **intro_card_media,
        )
        (job_dir / "render-command.txt").write_text(
            plan.readable_command, encoding="utf-8", newline="\n"
        )
        manifest_path = job_dir / "manifest.json"
        manifest = {
            "schema_version": 2,
            "output_mode": output_mode,
            "job": job.to_dict(),
            "business": {
                "novel_id": job.novel_id,
                "revision_id": job.revision_id,
                "episode_id": job.episode_id,
                "episode_ids": list(
                    job.episode_ids or ((job.episode_id,) if job.episode_id else ())
                ),
                "episode_label": (
                    job.episode_label
                    or f"E{max(1, int(job.episode_number)):03d}"
                ),
                "listing_id": job.listing_id,
                "promo_code_id": job.promo_code_id,
                "promo_code_snapshot": effective_code,
                "production_draft_id": job.production_draft_id,
                "production_run_id": job.production_run_id,
                "publishing_account_id": job.publishing_account_id,
                "episode_number": job.episode_number,
                "episode_count": job.episode_count,
                "is_final_episode": job.is_final_episode,
                "variant_index": job.variant_index,
                "variant_seed": job.variant_seed,
            },
            "platform": platform.to_dict(),
            "source_sha256": source_sha256,
            "recipe_hash": recipe_hash,
            "analysis": {
                "source_words": analysis.word_count,
                "source_chapters": len(analysis.chapters),
                "source_estimated_seconds": analysis.estimated_duration_seconds,
                "retention_ratio": text_result.retention_ratio,
            },
            "text": text_result.to_dict(),
            "voice": {
                "provider": actual_tts_provider,
                "profile": voice_profile,
                "voice": actual_voice,
                "requested_wpm": settings.narration_wpm,
                "speed_multiplier": narration_speed_for_wpm(
                    settings.narration_wpm, actual_tts_provider
                ),
                "duration_seconds": narration.duration_seconds,
                "sentence_count": len(narration.cues),
            },
            "media": {
                "mood": mood,
                "music": str(music.path) if music is not None else "",
                "music_loops": music.loops if music is not None else 0,
                "bgm_mode": bgm_mode,
                "video_playback_speed": playback_speed,
                "video_transition": video_transition,
                "videos": [str(item.path) for item in segments],
                "video_selection": video_selection,
                "safe_serial_render": serial_render_prepared,
                "encoder": encoder,
                "width": settings.output_width,
                "height": settings.output_height,
                "fps": settings.output_fps,
                "cover": str(effective_cover or ""),
                "video_template": settings.video_template,
                "intro_card": {
                    "headline": text_result.hook,
                    "text": job.intro_card_text,
                    "source": job.intro_card_source,
                    "duration_seconds": intro_card_duration if story_card_template else 0.0,
                },
                "ending_card": {
                    "kind": (
                        "cover_caption"
                        if effective_cover_outro
                        else "caption_only"
                    ),
                    "duration_seconds": end_card_duration,
                    "cover_outro_enabled": effective_cover_outro,
                    "cover_full_bleed": bool(
                        effective_cover and effective_cover_outro
                    ),
                    "animation": (
                        settings.cover_animation
                        if effective_cover_outro
                        else "none"
                    ),
                    "narrated_cta": text_result.ending_cta,
                },
                "narration_audio": {
                    "enabled": publish_narration_audio,
                    "output_file": "",
                    "contains_background_music": False,
                },
            },
            "warnings": warnings,
            "result": {
                "status": "rendering",
                "output_file": str(final_path),
            },
        }

        def persist_manifest() -> None:
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )

        persist_manifest()

        # Do not create employee-facing batch folders during text/TTS/media
        # preparation. A failure before rendering must not leave empty folders
        # mixed into the publishing queue.
        render_attempts: list[dict[str, str | int | float | bool]] = []
        progress_lines = render_progress.begin_attempt(
            render_duration,
            "渲染 1080 × 1920",
            minimum=0.68,
        )
        try:
            completed = run_cancellable_process(
                _ffmpeg_progress_command(plan.as_list()),
                runner=self.command_runner,
                stdout_line_callback=progress_lines,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
        except JobCancelledError:
            self._abort_publish_transaction(journal_path, publish_transaction)
            _remove_empty_directory(publish_dir)
            raise

        failure_code = _completed_failure_code(
            completed,
            output_exists=staged_video_path.is_file(),
        )
        failure_stage = "ffmpeg_render"
        failure_attempt: dict[str, str | int | float | bool] | None = None
        initial_attempt = render_progress.finish_attempt(
            succeeded=not failure_code,
            return_code=int(completed.returncode),
            failure_code=failure_code,
            stage="ffmpeg_render",
        )
        render_attempts.append(initial_attempt)
        if failure_code:
            failure_attempt = initial_attempt
        # A CPU retry is correct only when the selected hardware encoder could
        # not initialize. Missing files, filters, permissions and unknown
        # failures must remain visible instead of paying for a second render.
        if _should_retry_with_cpu(failure_code, encoder, encoders):
            hardware_detail = str(
                completed.stderr or completed.stdout or "硬件编码器未能完成渲染"
            )[-1200:]
            mark_encoder_unavailable(self.ffmpeg_path, encoder)
            warnings.append(
                f"{encoder} 初始化失败，本次仅切换一次 CPU 安全模式。"
            )
            try:
                staged_video_path.unlink()
            except OSError:
                pass
            fallback_plan = build_ffmpeg_plan(
                render_segments,
                narration.path,
                music,
                subtitle_path,
                staged_video_path,
                render_duration,
                ffmpeg_path=self.ffmpeg_path,
                width=settings.output_width,
                height=settings.output_height,
                fps=settings.output_fps,
                bgm_volume=settings.bgm_volume,
                video_encoder="libx264",
                cover_path=effective_cover,
                cover_intro_start=2.0,
                cover_intro_duration=2.0,
                end_card_duration=end_card_duration,
                render_mode="compatibility",
                cover_animation=settings.cover_animation,
                cover_outro_enabled=effective_cover_outro,
                color_grade=render_color_grade,
                end_card_without_cover=True,
                cover_intro_enabled=False,
                platform_logo_path=platform_logo_path,
                platform_logo_duration=intro_card_duration,
                video_transition=render_transition,
                **intro_card_media,
            )
            (job_dir / "render-command-fallback.txt").write_text(
                fallback_plan.readable_command,
                encoding="utf-8",
                newline="\n",
            )
            (job_dir / "hardware-render-error.log").write_text(
                hardware_detail,
                encoding="utf-8",
                newline="\n",
            )
            progress_lines = render_progress.begin_attempt(
                render_duration,
                "硬件编码初始化失败，CPU 安全重试",
                minimum=0.74,
            )
            try:
                completed = run_cancellable_process(
                    _ffmpeg_progress_command(fallback_plan.as_list()),
                    runner=self.command_runner,
                    stdout_line_callback=progress_lines,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    ),
                )
            except JobCancelledError:
                self._abort_publish_transaction(journal_path, publish_transaction)
                _remove_empty_directory(publish_dir)
                raise
            failure_code = _completed_failure_code(
                completed,
                output_exists=staged_video_path.is_file(),
            )
            fallback_attempt = render_progress.finish_attempt(
                succeeded=not failure_code,
                return_code=int(completed.returncode),
                failure_code=failure_code,
                stage="ffmpeg_cpu_fallback",
            )
            render_attempts.append(fallback_attempt)
            failure_stage = "ffmpeg_cpu_fallback"
            failure_attempt = fallback_attempt if failure_code else None
            if not failure_code:
                encoder = "libx264"
                plan = fallback_plan
                manifest["media"]["encoder"] = encoder
                manifest["warnings"] = warnings

        if _should_retry_in_low_memory_mode(
            failure_code,
            serial_render_prepared=serial_render_prepared,
        ):
            warnings.append(
                "检测到内存、分页文件或线程资源不足，已直接改用逐段安全模式；"
                "不会再重复高负载渲染。"
            )
            if video_transition == "fade":
                warnings.append(
                    "安全模式将片段间淡化改为直接切换，成片仍与旁白同步。"
                )
            try:
                staged_video_path.unlink()
            except OSError:
                pass
            low_memory_source = render_segments[0] if serial_render_prepared else None
            if low_memory_source is None:
                try:
                    _preflight_output_directory(
                        job.output_folder,
                        duration_seconds=render_duration,
                        output_mode=output_mode,
                        serial_staging=True,
                    )
                except PipelineError:
                    self._abort_publish_transaction(
                        journal_path, publish_transaction
                    )
                    _remove_empty_directory(publish_dir)
                    raise
                render_progress(
                    JobStatus.RENDERING,
                    0.71,
                    "资源不足，逐段整理视频素材",
                )
                try:
                    low_memory_source, completed = self._prepare_low_memory_video_source(
                        segments=segments,
                        staging_dir=publish_staging_dir,
                        job_dir=job_dir,
                        width=settings.output_width,
                        height=settings.output_height,
                        fps=settings.output_fps,
                        color_grade=getattr(settings, "color_grade", "neutral"),
                        video_transition=video_transition,
                        progress=render_progress,
                    )
                except JobCancelledError:
                    self._abort_publish_transaction(journal_path, publish_transaction)
                    _remove_empty_directory(publish_dir)
                    raise
                except (MediaError, OSError, PipelineError, ValueError) as error:
                    completed = subprocess.CompletedProcess(
                        [str(self.ffmpeg_path)],
                        1,
                        stdout="",
                        stderr=f"Low-memory preparation failed: {error}",
                    )
                    low_memory_source = None
                    failure_stage = "ffmpeg_serial_prepare"
                    failure_attempt = None

                if low_memory_source is None:
                    failure_stage = "ffmpeg_serial_prepare"
                    failure_attempt = None

            if low_memory_source is not None:
                low_memory_plan = build_ffmpeg_plan(
                    [low_memory_source],
                    narration.path,
                    music,
                    subtitle_path,
                    staged_video_path,
                    render_duration,
                    ffmpeg_path=self.ffmpeg_path,
                    width=settings.output_width,
                    height=settings.output_height,
                    fps=settings.output_fps,
                    bgm_volume=settings.bgm_volume,
                    video_encoder="libx264",
                    cover_path=effective_cover,
                    cover_intro_start=2.0,
                    cover_intro_duration=2.0,
                    end_card_duration=end_card_duration,
                    render_mode="compatibility",
                    cover_animation=settings.cover_animation,
                    cover_outro_enabled=effective_cover_outro,
                    color_grade="neutral",
                    end_card_without_cover=True,
                    cover_intro_enabled=False,
                    platform_logo_path=platform_logo_path,
                    platform_logo_duration=intro_card_duration,
                    video_transition="cut",
                    **intro_card_media,
                )
                with (job_dir / "render-command-low-memory.txt").open(
                    "a",
                    encoding="utf-8",
                    newline="\n",
                ) as stream:
                    stream.write("\n" + low_memory_plan.readable_command + "\n")
                progress_lines = render_progress.begin_attempt(
                    render_duration,
                    "安全模式：合成字幕与配音",
                    minimum=0.79,
                )
                try:
                    completed = run_cancellable_process(
                        _ffmpeg_progress_command(low_memory_plan.as_list()),
                        runner=self.command_runner,
                        stdout_line_callback=progress_lines,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        check=False,
                        creationflags=(
                            subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                        ),
                    )
                except JobCancelledError:
                    self._abort_publish_transaction(journal_path, publish_transaction)
                    _remove_empty_directory(publish_dir)
                    raise
                failure_code = _completed_failure_code(
                    completed,
                    output_exists=staged_video_path.is_file(),
                )
                low_memory_attempt = render_progress.finish_attempt(
                    succeeded=not failure_code,
                    return_code=int(completed.returncode),
                    failure_code=failure_code,
                    stage="ffmpeg_serial_fallback",
                )
                render_attempts.append(low_memory_attempt)
                failure_stage = "ffmpeg_serial_fallback"
                failure_attempt = low_memory_attempt if failure_code else None
                if not failure_code:
                    encoder = "libx264"
                    plan = low_memory_plan
                    manifest["media"]["encoder"] = encoder
                    manifest["media"]["low_memory_fallback"] = True
                    manifest["media"]["effective_video_transition"] = "cut"
                    manifest["warnings"] = warnings

        # Keep every automatic FFmpeg attempt auditable.  Commands and local
        # paths remain in machine-local files; the manifest stores only stage,
        # return code, frame count and observed progress.
        manifest["render_attempts"] = render_attempts
        if completed.returncode != 0 or not staged_video_path.is_file():
            self._abort_publish_transaction(journal_path, publish_transaction)
            _remove_empty_directory(publish_dir)
            detail = (completed.stderr or completed.stdout or "未知 FFmpeg 错误")[-3000:]
            render_error_log = job_dir / "render-error.log"
            render_error_log.write_text(
                detail, encoding="utf-8", newline="\n"
            )
            failure_diagnostics = capture_failure_diagnostics(
                render_error_log,
                stage=failure_stage,
                return_code=int(completed.returncode),
                attempt=(
                    int(failure_attempt.get("attempt") or 0)
                    if failure_attempt
                    else None
                ),
                attempt_label=(
                    str(failure_attempt.get("label") or "")
                    if failure_attempt
                    else ""
                ),
            )
            failure_summary = str(
                failure_diagnostics.get("summary") or "FFmpeg 渲染失败。"
            )
            manifest["job"].update(
                {
                    "status": "failed",
                    "stage_label": "生成失败",
                    "message": f"FFmpeg 渲染失败：{failure_summary}",
                }
            )
            manifest["result"] = {
                "status": "failed",
                "output_file": "",
                "error_log": str(render_error_log),
                "failure_diagnostics": failure_diagnostics,
                "render_attempts": render_attempts,
            }
            persist_manifest()
            raise PipelineError(
                f"FFmpeg 渲染失败：{failure_summary}",
                error_log=render_error_log,
                failure_diagnostics=failure_diagnostics,
            )
        render_progress(JobStatus.RENDERING, 0.94, "快速检查成片")
        try:
            subtitle_text = subtitle_path.read_text(encoding="utf-8-sig")
        except OSError:
            subtitle_text = ""
        quality_log = job_dir / "quality-check.log"
        quality_report = self._run_quality_check(
            staged_video_path,
            QualityExpectation(
                width=settings.output_width,
                height=settings.output_height,
                duration_seconds=render_duration,
                fps=settings.output_fps,
                minimum_size_bytes=max(1024, round(render_duration * 512)),
                checklist={
                    "novel_id": (job.novel_id, manifest["business"]["novel_id"]),
                    "episode_id": (job.episode_id, manifest["business"]["episode_id"]),
                    "platform_id": (job.platform_id, manifest["platform"]["id"]),
                    "platform_name": (platform.name, manifest["platform"]["name"]),
                    "promo_code_snapshot": (
                        effective_code,
                        manifest["business"]["promo_code_snapshot"],
                    ),
                    "voice_identity": (actual_voice, manifest["voice"]["voice"]),
                    "cover_snapshot": (
                        str(effective_cover or ""),
                        manifest["media"]["cover"],
                    ),
                    "output_file": (
                        str(final_path),
                        manifest["result"]["output_file"],
                    ),
                    "subtitle_contains_code": (True, effective_code in subtitle_text),
                    "narration_contains_code": (True, effective_code in script_text),
                },
            ),
        )
        self._write_quality_log(quality_log, quality_report)
        manifest["quality_control"] = quality_report.to_dict()
        if not quality_report.passed:
            self._abort_publish_transaction(journal_path, publish_transaction)
            _remove_empty_directory(publish_dir)
            manifest["job"].update(
                {
                    "status": "failed",
                    "stage_label": "快速质检失败",
                    "message": "成片未通过快速质检，详情见 quality-check.log。",
                }
            )
            manifest["result"] = {
                "status": "failed",
                "output_file": "",
                "error_log": str(quality_log),
            }
            persist_manifest()
            raise PipelineError("成片未通过快速质检，详情见 quality-check.log。")
        # Quality checking can take long enough for an operator to cancel the
        # task while it is running.  Re-check immediately before mutating the
        # durable media ledgers so a cancelled task never consumes a usage
        # count merely because its checker returned a passing result.
        raise_if_cancelled()
        progress(
            JobStatus.RENDERING,
            0.96,
            "导出纯旁白配音" if publish_narration_audio else "发布完整视频",
        )
        try:
            if publish_narration_audio:
                self._stage_narration_mp3(
                    narration,
                    staged_audio_path,
                    existing_source=existing_narration_source,
                )
                self._embed_narration_metadata(
                    staged_audio_path,
                    self._narration_metadata_value(
                        job=job,
                        promo_code=effective_code,
                        text_result=text_result,
                        narration=narration,
                    ),
                )
            raise_if_cancelled()
            self._mark_publish_transaction_ready(
                journal_path, publish_transaction
            )
            self._commit_publish_transaction(
                journal_path, publish_transaction
            )
            self._save_narration_metadata(
                narration_audio_path if publish_narration_audio else final_path,
                job=job,
                promo_code=effective_code,
                text_result=text_result,
                narration=narration,
            )
        except JobCancelledError:
            self._abort_publish_transaction(journal_path, publish_transaction)
            _remove_empty_directory(publish_dir)
            raise
        except (OSError, PipelineError) as error:
            # Publishing is atomic: never expose a partial video or a legacy
            # MP4+MP3 pair when either requested artifact could not be committed.
            self._abort_publish_transaction(journal_path, publish_transaction)
            _remove_empty_directory(publish_dir)
            job.narration_audio_file = ""
            detail = f"{type(error).__name__}: {error}"
            export_error_log = job_dir / "narration-export-error.log"
            export_error_log.write_text(detail, encoding="utf-8", newline="\n")
            failure_label = (
                "纯旁白导出失败" if publish_narration_audio else "成品发布失败"
            )
            failure_message = (
                "纯旁白配音导出失败，已撤回视频；详情见 narration-export-error.log。"
                if publish_narration_audio
                else "完整视频发布失败，已撤回临时文件；详情见 narration-export-error.log。"
            )
            manifest["job"].update(
                {
                    "status": "failed",
                    "stage_label": failure_label,
                    "message": failure_message,
                    "output_file": "",
                    "narration_audio_file": "",
                }
            )
            manifest["result"] = {
                "status": "failed",
                "output_file": "",
                "narration_audio_file": "",
                "error_log": str(export_error_log),
            }
            persist_manifest()
            raise PipelineError(failure_message) from error
        job.narration_audio_file = (
            str(narration_audio_path) if publish_narration_audio else ""
        )
        manifest["media"]["narration_audio"].update(
            {
                "output_file": job.narration_audio_file,
                "duration_seconds": narration.duration_seconds,
                "format": "mp3" if publish_narration_audio else "",
                "codec": "mp3" if publish_narration_audio else "",
                "sample_rate_hz": 48000 if publish_narration_audio else 0,
                "bitrate_kbps": 192 if publish_narration_audio else 0,
            }
        )
        # Usage ledgers are advisory deduplication metadata.  A read-only or
        # temporarily disconnected employee material folder must never turn a
        # fully rendered and committed product into a failed job.  The Hub
        # completion record still captures the selected material paths/counts.
        for usage_label, usage_commit in (
            (
                "视频",
                lambda: _commit_video_usage(job.video_folder, segments),
            ),
            (
                "音乐",
                lambda: _commit_music_usage(job.music_folder, music),
            ),
        ):
            try:
                usage_commit()
            except Exception as error:
                warnings.append(
                    f"素材使用次数记录失败（{usage_label}），成品已保留："
                    f"{type(error).__name__}: {error}"
                )
        manifest["warnings"] = warnings
        job.message = "；".join(warnings)
        manifest["job"].update(
            {
                "status": "completed",
                "progress": 1.0,
                "stage_label": "已完成",
                "message": job.message,
                "output_file": str(final_path),
                "narration_audio_file": job.narration_audio_file,
            }
        )
        manifest["result"] = {
            "status": "completed",
            "output_file": str(final_path),
            "narration_audio_file": job.narration_audio_file,
            "quality_log": str(quality_log),
        }
        persist_manifest()
        # Segment-level WAVs are needed only while assembling the merged
        # narration.  Keeping dozens of them per successful video wastes disk
        # space and makes later diagnostics harder to scan.
        try:
            shutil.rmtree(audio_dir)
        except OSError:
            pass
        progress(JobStatus.RENDERING, 0.98, "整理输出")
        return str(final_path)

    @staticmethod
    def _settings_for_job(live: AppSettings, job: RenderJob) -> AppSettings:
        """Overlay an approved non-secret recipe onto the current credentials."""

        if not job.settings_snapshot:
            return live
        merged = live.to_dict()
        snapshot = dict(job.settings_snapshot)
        provider_snapshot = dict(snapshot.pop("providers", {}) or {})
        for key, value in snapshot.items():
            if key in merged:
                merged[key] = value
        merged["providers"] = {
            **dict(merged.get("providers") or {}),
            **provider_snapshot,
        }
        return AppSettings.from_dict(merged)

    @staticmethod
    def _load_prepared_text(path: Path, recipe_hash: str) -> TextResult | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("recipe_hash") != recipe_hash:
                return None
            text = value.get("text")
            if not isinstance(text, dict):
                return None
            return TextResult(
                polished_text=str(text["polished_text"]),
                hook=str(text["hook"]),
                ending_cta=str(text["ending_cta"]),
                mood=str(text["mood"]),
                provider=str(text["provider"]),
                model=str(text.get("model") or ""),
                retention_ratio=float(text.get("retention_ratio") or 1.0),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _save_prepared_text(
        path: Path,
        *,
        recipe_hash: str,
        source_sha256: str,
        text_result: TextResult,
        job: RenderJob,
        platform: PlatformProfile,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "recipe_hash": recipe_hash,
            "source_sha256": source_sha256,
            "novel_id": job.novel_id,
            "revision_id": job.revision_id,
            "episode_id": job.episode_id,
            "platform_id": platform.id,
            "promo_code_snapshot": job.promo_code_snapshot or job.code,
            "text": text_result.to_dict(),
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    @staticmethod
    def _preview_units(
        text_result: TextResult,
        *,
        preview_seconds: float,
        narration_wpm: float,
        include_ending: bool = False,
        end_card_seconds: float = 6.0,
    ) -> list[NarrationUnit]:
        without_ending = TextResult(
            polished_text=text_result.polished_text,
            hook=text_result.hook,
            ending_cta="",
            mood=text_result.mood,
            provider=text_result.provider,
            model=text_result.model,
            retention_ratio=text_result.retention_ratio,
        )
        available = narration_units(without_ending)
        # Request a little more text than the nominal WPM estimate. Fixed-length
        # previews are fitted at complete sentence groups after real TTS timing.
        if include_ending:
            # Reserve the closing seconds for the real narrated CTA so one
            # compact approval sample demonstrates the complete template.
            # A conservative word budget avoids trimming the CTA on naturally
            # slower voices; FFmpeg pads any remaining tail to the configured
            # approval-sample duration.
            body_seconds = max(8.0, float(preview_seconds) - float(end_card_seconds) - 1.0)
            target_words = max(
                25,
                int(float(narration_wpm) * body_seconds / 60.0 * 0.92),
            )
        else:
            target_words = max(
                35,
                int(float(narration_wpm) * float(preview_seconds) / 60.0 * 1.35),
            )
        selected: list[NarrationUnit] = []
        spoken_words = 0
        for unit in available:
            selected.append(unit)
            if not unit.is_chapter_break:
                spoken_words += len(re.findall(r"\b[\w'’\-]+\b", unit.text))
            if spoken_words >= target_words:
                break
        while selected and selected[-1].is_chapter_break:
            selected.pop()
        if not any(not item.is_chapter_break for item in selected):
            raise PipelineError("小说开头没有足够的可朗读内容来生成样片。")
        if include_ending:
            # A 15-second approval reel is a style check, not a story excerpt.
            # Use one concise, natural clause so the opening, live captions and
            # full narrated CTA each retain their own visible time window.
            body_window_seconds = max(
                3.0,
                float(preview_seconds) - float(end_card_seconds),
            )
            body_word_cap = max(
                10,
                min(
                    18,
                    int(float(narration_wpm) * body_window_seconds / 60.0 * 0.62),
                ),
            )
            first_body = next(
                item for item in selected if not item.is_chapter_break
            )
            selected = [
                NarrationUnit(
                    _concise_preview_sentence(
                        first_body.text,
                        max_words=body_word_cap,
                    )
                )
            ]
            ending_units = [
                NarrationUnit(sentence)
                for sentence in split_english_sentences(text_result.ending_cta.strip())
                if sentence.strip()
            ]
            if ending_units:
                selected.append(NarrationUnit(is_chapter_break=True))
                selected.extend(ending_units)
        return selected

    def _render_preview(
        self,
        job: RenderJob,
        platform: PlatformProfile,
        progress: ProgressCallback,
        *,
        settings: AppSettings,
        source_path: Path,
        job_dir: Path,
        work_dir: Path,
        audio_dir: Path,
    ) -> str:
        warnings: list[str] = []
        effective_code = job.promo_code_snapshot or job.code
        preview_seconds = float(settings.preview_seconds)
        story_card_template = settings.video_template == "platform_story_card"
        platform_logo_path = self._validated_optional_image(
            _story_card_platform_logo(platform, story_card_template),
            asset_label="平台 Logo",
            fallback_label="改用无 Logo 的简介卡布局",
            warnings=warnings,
        )
        preview_cover_was_present = bool(
            job.cover_path and Path(job.cover_path).expanduser().is_file()
        )
        preview_cover = self._validated_optional_image(
            job.cover_path,
            asset_label="小说封面",
            fallback_label="改用纯字幕结尾",
            warnings=warnings,
        )
        # The approval sample is a compact review reel, not a blind prefix of
        # the final video. Five closing seconds guarantee that the real CTA,
        # subtitle and full-screen cover are visible; a shorter four-second
        # intro leaves a distinct body window in the 15-second default.
        end_card_duration = 5.0
        intro_card_duration = (
            min(
                4.0,
                max(2.5, preview_seconds - end_card_duration - 3.0),
            )
            if story_card_template
            else 0.0
        )
        preview_style = _preview_subtitle_style(settings)
        intro_card_media = _intro_card_media_options(
            settings,
            preview_cover,
            intro_card_text=job.intro_card_text,
            code=effective_code,
            style=preview_style,
            intro_duration=intro_card_duration,
        )
        preview_dir = job_dir / ".previews"
        preview_dir.mkdir(parents=True, exist_ok=True)

        progress(JobStatus.PREPARING, 0.05, "准备样片文稿")
        original = read_manuscript(source_path)
        analysis = analyze_manuscript(
            original,
            source_path.name,
            wpm=settings.narration_wpm,
            chapter_pause_seconds=settings.chapter_pause_seconds,
        )
        marked_text = inject_chapter_markers(analysis)
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        recipe_hash = prepared_recipe_hash(job, platform, settings, source_sha256)
        job.content_fingerprint = source_sha256
        job.recipe_hash = recipe_hash
        prepared_path = work_dir / "prepared.json"
        text_result = self._load_prepared_text(prepared_path, recipe_hash)
        if text_result is None:
            progress(JobStatus.POLISHING, 0.14, "生成真实开头钩子")
            request = TextRequest(
                text=marked_text,
                title=job.title,
                platform=platform.name,
                code=effective_code,
                ending_template=platform.render_ending(effective_code),
                adult_mode=settings.adult_mode,
                retention_min=settings.retention_min,
                retention_max=settings.retention_max,
                language=_prompt_language(job),
                creative_line_index=max(1, int(job.variant_index)),
                creative_line_count=max(
                    1,
                    int(job.variant_index),
                    int(getattr(job, "variant_count", 1)),
                ),
            )
            text_result = self._polish(request, settings, warnings)
            self._save_prepared_text(
                prepared_path,
                recipe_hash=recipe_hash,
                source_sha256=source_sha256,
                text_result=text_result,
                job=job,
                platform=platform,
            )

        selected_mood = str(job.story_mood or text_result.mood)
        try:
            mood = canonical_mood(selected_mood)
        except MediaError:
            mood = "suspense"
            warnings.append("样片题材无法识别，已使用悬疑素材预设。")
        preview_ending_cta = (
            text_result.ending_cta.strip()
            or platform.render_ending(effective_code).strip()
        )
        preview_text_result = replace(
            text_result,
            ending_cta=preview_ending_cta,
        )
        units = self._preview_units(
            preview_text_result,
            preview_seconds=preview_seconds - intro_card_duration,
            narration_wpm=settings.narration_wpm,
            include_ending=True,
            end_card_seconds=end_card_duration,
        )
        preview_intro_headline = _concise_preview_sentence(
            text_result.hook,
            max_words=8,
        )

        voice_stage = (
            "首次加载本地女声模型并生成样片"
            if settings.providers.tts_provider in {"local", "local_kokoro", "kokoro"}
            else "调用云端女声并生成样片"
        )
        progress(JobStatus.NARRATING, 0.30, voice_stage)
        if (
            job.locked_voice_provider
            and job.locked_voice_provider != settings.providers.tts_provider
        ):
            raise PipelineError("该小说已锁定到另一种配音服务，请恢复原服务后再生成。")
        voice_profile = settings.voice_by_mood.get(mood, "dramatic")
        # Keep one sentence per preview segment so actual voice duration can be
        # fitted at natural sentence boundaries without clipping the CTA.
        spoken, segment_unit_counts = group_narration_units(
            units,
            max_sentences=1,
        )
        tts_result, actual_provider, actual_voice = self._narrate(
            spoken,
            audio_dir,
            voice_profile,
            mood,
            settings,
            warnings,
            locked_voice_id=job.locked_voice_id,
        )
        job.locked_voice_provider = actual_provider
        job.locked_voice_id = actual_voice
        if preview_ending_cta:
            original_segment_count = len(tts_result.segments)
            tts_result, units, segment_unit_counts = _fit_preview_tts_to_duration(
                tts_result,
                units,
                segment_unit_counts,
                preview_seconds=preview_seconds - intro_card_duration,
                chapter_pause_seconds=settings.chapter_pause_seconds,
            )
            if len(tts_result.segments) < original_segment_count:
                warnings.append(
                    "样片已按真实配音时长缩短正文截取，完整保留结尾引导。"
                )
        narration = assemble_narration_wav(
            tts_result,
            units,
            work_dir / "preview-narration.wav",
            chapter_pause_seconds=settings.chapter_pause_seconds,
            initial_silence_seconds=intro_card_duration,
            segment_unit_counts=segment_unit_counts,
        )
        if preview_ending_cta:
            ending_count = len(split_english_sentences(preview_ending_cta))
            spoken_count = sum(1 for unit in units if not unit.is_chapter_break)
            ending_index = max(0, spoken_count - ending_count)
            if ending_count and ending_index < len(narration.cues):
                current_cta_start = narration.cues[ending_index].start
                desired_cta_start = preview_seconds - end_card_duration + 0.65
                available_padding = max(0.0, preview_seconds - narration.duration_seconds)
                additional_pause = min(
                    available_padding,
                    max(0.0, desired_cta_start - current_cta_start),
                )
                if additional_pause >= 0.05:
                    narration = assemble_narration_wav(
                        tts_result,
                        units,
                        work_dir / "preview-narration.wav",
                        chapter_pause_seconds=settings.chapter_pause_seconds,
                        final_chapter_pause_seconds=(
                            settings.chapter_pause_seconds + additional_pause
                        ),
                        initial_silence_seconds=intro_card_duration,
                        segment_unit_counts=segment_unit_counts,
                    )
        # Always render the complete configured window. The narration assembler
        # may finish earlier after removing a body sentence; FFmpeg pads that
        # gap so the closing cover is still reviewed at the end of the sample.
        target_duration = preview_seconds
        if target_duration < 3:
            raise PipelineError("样片旁白不足 3 秒，无法有效预览。")
        clipped_cues = tuple(
            SubtitleCue(cue.start, min(cue.end, target_duration), cue.text)
            for cue in narration.cues
            if cue.start < target_duration and min(cue.end, target_duration) > cue.start
        )

        progress(
            JobStatus.COMPOSING,
            0.55,
            f"编排{int(round(target_duration))}秒审核样片",
        )
        subtitle_path = work_dir / "preview-subtitles.ass"
        write_ass(
            subtitle_path,
            clipped_cues,
            platform=platform.name,
            code=effective_code,
            search_text=platform.render_search(effective_code),
            video_duration=target_duration,
            video_template=settings.video_template,
            intro_card_text=job.intro_card_text,
            intro_headline=preview_intro_headline,
            intro_card_duration=intro_card_duration,
            final_label=_story_card_final_label(job, story_card_template),
            platform_logo_present=bool(platform_logo_path),
            intro_card_cover_present=bool(preview_cover),
            platform_brand_color=platform.brand_color,
            config=preview_style,
        )
        resolver = lambda path: probe_duration_with_ffmpeg(self.ffmpeg_path, path)
        variant_seed: int | str | bytes | None = (
            job.variant_seed
            if job.variant_seed or job.production_draft_id or job.novel_id
            else None
        )
        video_selection: dict[str, Any] = {}
        segments = _plan_category_video_segments(
            job.video_folder,
            target_duration,
            mood=mood,
            duration_resolver=resolver,
            variant_seed=variant_seed,
            selection_report=video_selection,
            warnings=warnings,
        )
        music = select_music_asset(
            job.music_folder,
            mood,
            target_duration,
            duration_resolver=resolver,
        )
        segments, music = self._ensure_decodable_media(
            video_folder=job.video_folder,
            music_folder=job.music_folder,
            mood=mood,
            target_duration=target_duration,
            duration_resolver=resolver,
            variant_seed=variant_seed,
            segments=segments,
            music=music,
            selection_report=video_selection,
            warnings=warnings,
        )
        segments = _segments_with_geometry(self.ffmpeg_path, segments)
        variant = max(1, int(job.variant_index))
        episode_label = safe_component(
            job.episode_label or f"E{max(1, int(job.episode_number)):03d}",
            fallback=f"E{max(1, int(job.episode_number)):03d}",
        )
        preview_path = (
            preview_dir
            / f"{episode_label}_V{variant:02d}_{int(target_duration)}s.mp4"
        )
        encoders = available_encoders(self.ffmpeg_path)
        encoder = (
            settings.video_encoder
            if settings.video_encoder != "auto"
            else (encoders[0] if encoders else "libx264")
        )
        preview_cover_outro = bool(
            _cover_outro_enabled(settings, job)
            and (preview_cover or not preview_cover_was_present)
        )
        plan = build_ffmpeg_plan(
            segments,
            narration.path,
            music,
            subtitle_path,
            preview_path,
            target_duration,
            ffmpeg_path=self.ffmpeg_path,
            width=540,
            height=960,
            fps=settings.output_fps,
            bgm_volume=settings.bgm_volume,
            video_encoder=encoder,
            render_mode="speed",
            cover_path=preview_cover,
            end_card_duration=end_card_duration,
            cover_animation=settings.cover_animation,
            cover_outro_enabled=preview_cover_outro,
            color_grade=getattr(settings, "color_grade", "neutral"),
            end_card_without_cover=True,
            cover_intro_enabled=False,
            platform_logo_path=platform_logo_path,
            platform_logo_duration=intro_card_duration,
            **intro_card_media,
        )
        (preview_dir / "render-command.txt").write_text(
            plan.readable_command,
            encoding="utf-8",
            newline="\n",
        )
        progress(
            JobStatus.PREVIEWING,
            0.72,
            f"渲染{int(round(target_duration))}秒审核样片",
        )
        try:
            completed = run_cancellable_process(
                plan.as_list(),
                runner=self.command_runner,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except JobCancelledError:
            _remove_cancelled_media(preview_path)
            raise
        if (
            (completed.returncode != 0 or not preview_path.is_file())
            and encoder != "libx264"
            and "libx264" in encoders
        ):
            warnings.append(f"样片的 {encoder} 硬件编码失败，已使用 CPU 快速重试。")
            plan = build_ffmpeg_plan(
                segments,
                narration.path,
                music,
                subtitle_path,
                preview_path,
                target_duration,
                ffmpeg_path=self.ffmpeg_path,
                width=540,
                height=960,
                fps=settings.output_fps,
                bgm_volume=settings.bgm_volume,
                video_encoder="libx264",
                render_mode="speed",
                cover_path=preview_cover,
                end_card_duration=end_card_duration,
                cover_animation=settings.cover_animation,
                cover_outro_enabled=preview_cover_outro,
                color_grade=getattr(settings, "color_grade", "neutral"),
                end_card_without_cover=True,
                cover_intro_enabled=False,
                platform_logo_path=platform_logo_path,
                platform_logo_duration=intro_card_duration,
                **intro_card_media,
            )
            (preview_dir / "render-command-fallback.txt").write_text(
                plan.readable_command,
                encoding="utf-8",
                newline="\n",
            )
            try:
                completed = run_cancellable_process(
                    plan.as_list(),
                    runner=self.command_runner,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except JobCancelledError:
                _remove_cancelled_media(preview_path)
                raise
            if completed.returncode == 0 and preview_path.is_file():
                encoder = "libx264"
        if completed.returncode != 0 or not preview_path.is_file():
            detail = (completed.stderr or completed.stdout or "未知 FFmpeg 错误")[-3000:]
            (preview_dir / "render-error.log").write_text(
                detail,
                encoding="utf-8",
                newline="\n",
            )
            raise PipelineError("样片渲染失败，详情见 .previews/render-error.log。")
        progress(JobStatus.PREVIEWING, 0.94, "快速检查样片")
        preview_manifest = {
            "schema_version": 2,
            "job": job.to_dict(),
            "source_sha256": source_sha256,
            "recipe_hash": recipe_hash,
            "duration_seconds": target_duration,
            "review_timeline": {
                "structured": True,
                "opening_card": {
                    "kind": (
                        "platform_story_card"
                        if story_card_template
                        else "hook_and_code_card"
                    ),
                    "start_seconds": 0.0,
                    "end_seconds": intro_card_duration,
                },
                "story_body": {
                    "start_seconds": intro_card_duration,
                    "end_seconds": target_duration - end_card_duration,
                    "narration_and_captions": True,
                },
                "ending_card": {
                    "kind": (
                        "cover_caption"
                        if preview_cover_outro
                        else "caption_only"
                    ),
                    "start_seconds": target_duration - end_card_duration,
                    "end_seconds": target_duration,
                    "narrated_cta": preview_ending_cta,
                    "cover_outro_enabled": preview_cover_outro,
                    "cover_full_bleed": bool(
                        preview_cover_outro and preview_cover
                    ),
                    "animation": (
                        settings.cover_animation
                        if preview_cover_outro
                        else "none"
                    ),
                },
            },
            "voice": {
                "provider": actual_provider,
                "voice": actual_voice,
                "profile": voice_profile,
            },
            "platform": platform.to_dict(),
            "promo_code_snapshot": effective_code,
            "media": {
                "mood": mood,
                "videos": [str(item.path) for item in segments],
                "video_selection": video_selection,
                "encoder": encoder,
                "width": 540,
                "height": 960,
                "fps": settings.output_fps,
                "video_template": settings.video_template,
                "intro_card": {
                    "headline": preview_intro_headline,
                    "text": job.intro_card_text,
                    "source": job.intro_card_source,
                    "duration_seconds": intro_card_duration if story_card_template else 0.0,
                },
            },
            "warnings": warnings,
            "output_file": str(preview_path),
            "result": {
                "status": "rendered",
                "output_file": str(preview_path),
            },
        }
        try:
            subtitle_text = subtitle_path.read_text(encoding="utf-8-sig")
        except OSError:
            subtitle_text = ""
        quality_log = preview_dir / "quality-check.log"
        quality_report = self._run_quality_check(
            preview_path,
            QualityExpectation(
                width=540,
                height=960,
                duration_seconds=target_duration,
                fps=settings.output_fps,
                minimum_size_bytes=max(1024, round(target_duration * 512)),
                checklist={
                    "platform_id": (job.platform_id, preview_manifest["platform"]["id"]),
                    "platform_name": (platform.name, preview_manifest["platform"]["name"]),
                    "promo_code_snapshot": (
                        effective_code,
                        preview_manifest["promo_code_snapshot"],
                    ),
                    "voice_identity": (actual_voice, preview_manifest["voice"]["voice"]),
                    "output_file": (
                        str(preview_path),
                        preview_manifest["output_file"],
                    ),
                    "subtitle_contains_code": (True, effective_code in subtitle_text),
                },
            ),
        )
        self._write_quality_log(quality_log, quality_report)
        preview_manifest["quality_control"] = quality_report.to_dict()
        if quality_report.passed:
            preview_manifest["result"]["status"] = "completed"
            preview_manifest["result"]["quality_log"] = str(quality_log)
        else:
            preview_manifest["result"] = {
                "status": "failed",
                "output_file": str(preview_path),
                "error_log": str(quality_log),
            }
        (preview_dir / "preview-manifest.json").write_text(
            json.dumps(preview_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        if not quality_report.passed:
            raise PipelineError("样片未通过快速质检，详情见 .previews/quality-check.log。")
        job.message = "；".join(warnings)
        progress(JobStatus.PREVIEWING, 0.98, "样片已生成")
        return str(preview_path)

    def _polish(
        self,
        request: TextRequest,
        settings: AppSettings,
        warnings: list[str],
    ) -> TextResult:
        selected = settings.providers.text_provider
        provider = self.text_provider_factory(settings.providers)
        try:
            return provider.polish(request)
        except ProviderError as error:
            if (
                getattr(provider, "strict_quality", False)
                or not settings.providers.allow_provider_fallback
                or selected == "local"
            ):
                raise
            warnings.append(
                f"文本服务 {selected} 失败，已使用本地规则模式：{type(error).__name__}: {error}"
            )
            return self.text_provider_factory(ProviderConfig(name="local")).polish(request)

    def _tts_config(self, settings: AppSettings, *, local: bool = False) -> Any:
        provider_settings = settings.providers
        if local or provider_settings.tts_provider == "local_kokoro":
            options: dict[str, Any] = {}
            if provider_settings.kokoro_command:
                options["command"] = provider_settings.kokoro_command
            return ProviderConfig(
                name="local_kokoro",
                endpoint=provider_settings.kokoro_endpoint,
                options=options,
            )
        return provider_settings

    def _narrate(
        self,
        sentences: Sequence[str],
        output_dir: Path,
        voice_profile: str,
        mood: str,
        settings: AppSettings,
        warnings: list[str],
        *,
        locked_voice_id: str = "",
    ) -> tuple[TTSResult, str, str]:
        raise_if_cancelled()
        selected = settings.providers.tts_provider
        normalized_selected = str(selected or "").strip().casefold().replace("-", "_")
        unmetered = normalized_selected in {
            "local",
            "local_kokoro",
            "kokoro",
            "kokoro_local",
            "kokoro_http",
            "kokoro_cli",
            "edge",
            "edge_tts",
            "microsoft_edge",
            "microsoft_edge_tts",
        }
        normalized_profile, mapped_voice = resolve_voice(selected, voice_profile, mood)
        selected_voice = locked_voice_id.strip() or mapped_voice
        if not locked_voice_id and normalized_selected in {
            "edge",
            "edge_tts",
            "microsoft_edge",
            "microsoft_edge_tts",
        }:
            edge_voices = female_voice_candidates(selected, settings.language)
            matched = next(
                (item for item in edge_voices if item.profile == normalized_profile),
                edge_voices[0] if edge_voices else None,
            )
            if matched is None:
                raise ProviderError(
                    "Edge TTS 当前无法取得该语种的真实女声，请检查组件和网络后重试。",
                    provider=selected,
                    retryable=True,
                )
            selected_voice = matched.voice_id
        speed = narration_speed_for_wpm(settings.narration_wpm, selected)
        characters = sum(len(item) for item in sentences)
        if not unmetered:
            self.usage_ledger.check(
                selected,
                characters,
                settings.providers.monthly_character_limit,
            )
        try:
            provider = self.tts_provider_factory(self._tts_config(settings))
            result = provider.synthesize(
                sentences,
                output_dir,
                voice=selected_voice,
                speed=speed,
                file_stem="line",
            )
            raise_if_cancelled()
            if not unmetered:
                self.usage_ledger.commit(selected, characters)
            return result, selected, selected_voice
        except ProviderError as error:
            if locked_voice_id:
                # Voice/provider compatibility is validated before jobs enter
                # the queue.  Do not turn every later engine, dependency,
                # network or filesystem failure into a misleading "voice is
                # unavailable" message merely because this batch has a lock.
                # Preserving the concrete provider error is essential for both
                # user recovery and diagnostics (for example a missing runtime
                # component in a frozen desktop build).
                raise
            if not settings.providers.allow_provider_fallback or selected == "local_kokoro":
                raise
            warnings.append(
                f"配音服务 {selected} 失败，尝试本地 Kokoro：{type(error).__name__}: {error}"
            )
            local_provider = self.tts_provider_factory(
                self._tts_config(settings, local=True)
            )
            _profile, local_voice = resolve_voice(
                "local_kokoro", normalized_profile, mood
            )
            local_speed = narration_speed_for_wpm(
                settings.narration_wpm, "local_kokoro"
            )
            return (
                local_provider.synthesize(
                    sentences,
                    output_dir,
                    voice=local_voice,
                    speed=local_speed,
                    file_stem="line",
                ),
                "local_kokoro",
                local_voice,
            )
