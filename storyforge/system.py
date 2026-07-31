from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from .cancellation import run_cancellable_process, windows_process_creation_flags
from .providers.tts import edge_tts_runtime_available


def resolve_ffmpeg() -> Path | None:
    configured = os.environ.get("STORYFORGE_FFMPEG")
    if configured and Path(configured).is_file():
        return Path(configured).resolve()
    on_path = shutil.which("ffmpeg")
    if on_path:
        return Path(on_path).resolve()
    try:
        import imageio_ffmpeg

        bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
        return bundled.resolve() if bundled.is_file() else None
    except (ImportError, RuntimeError, OSError):
        return None


def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    return windows_process_creation_flags(
        ("ffmpeg.exe",),
        int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )


_DISABLED_ENCODERS: set[tuple[str, str]] = set()
_DISABLED_ENCODERS_LOCK = threading.RLock()
_ENCODER_PROBE_LOCK = threading.RLock()


def mark_encoder_unavailable(ffmpeg: str | Path, encoder: str) -> None:
    """Avoid retrying a hardware encoder that failed in this app session."""

    # Keep disablement and cache invalidation atomic with respect to a first
    # capability probe.  Otherwise a browser self-check could republish the
    # just-failed backend while the render thread is clearing the cache.
    with _ENCODER_PROBE_LOCK:
        key = (os.path.normcase(str(Path(ffmpeg).resolve())), str(encoder))
        with _DISABLED_ENCODERS_LOCK:
            _DISABLED_ENCODERS.add(key)
        available_encoders.cache_clear()


def _encoder_is_disabled(ffmpeg: str | Path, encoder: str) -> bool:
    key = (os.path.normcase(str(Path(ffmpeg).resolve())), str(encoder))
    with _DISABLED_ENCODERS_LOCK:
        return key in _DISABLED_ENCODERS


@lru_cache(maxsize=8)
def _runtime_encoder_works(executable: str, encoder: str) -> bool:
    """Verify that a compiled hardware encoder can initialize on this PC.

    FFmpeg can list NVENC/QSV/AMF even when the matching GPU or driver is not
    present.  A short delivery-resolution probe prevents `auto` from selecting
    an encoder that initializes at 64x64 but fails at StoryForge's real size.
    """

    try:
        result = run_cancellable_process(
            [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=1080x1920:r=60:d=0.25",
                "-frames:v",
                "15",
                "-an",
                "-c:v",
                encoder,
                "-pix_fmt",
                "yuv420p",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            # Hardware backends either initialize almost immediately or are
            # unavailable on the current PC.  A short bound keeps the desktop
            # bootstrap responsive when a driver advertises an encoder that it
            # cannot actually start.
            timeout=8,
            check=False,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@lru_cache(maxsize=4)
def _available_encoders_cached(executable_value: str) -> tuple[str, ...]:
    executable = Path(executable_value)
    try:
        result = run_cancellable_process(
            [str(executable), "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    output = f"{result.stdout}\n{result.stderr}"
    compiled = {
        name
        for name in ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")
        if name in output
    }
    hardware = [
        name
        for name in ("h264_nvenc", "h264_qsv", "h264_amf")
        if name in compiled and not _encoder_is_disabled(executable, name)
    ]
    # ``auto`` consumes only the first usable hardware backend.  Initialising
    # NVENC, QSV and AMF simultaneously needlessly makes three 1080x1920/60
    # FFmpeg processes compete for the display GPU during Worker startup.  On
    # low-memory PCs or unstable drivers that can freeze the UI or reset the
    # display. Probe in preference order and stop as soon as one works.
    working: list[str] = []
    for name in hardware:
        if _runtime_encoder_works(str(executable), name):
            working.append(name)
            break
    if "libx264" in compiled:
        working.append("libx264")
    return tuple(working)


def available_encoders(ffmpeg: Path | None = None) -> list[str]:
    """Return one safe hardware choice plus the CPU fallback.

    ``functools.lru_cache`` permits duplicate execution when several threads
    miss the same key concurrently.  The explicit lock sits outside the
    cached helper so only one request performs the first driver probe; waiting
    browser/self-check requests reuse its completed value.
    """

    executable = ffmpeg or resolve_ffmpeg()
    if executable is None:
        return []
    with _ENCODER_PROBE_LOCK:
        return list(_available_encoders_cached(str(executable)))


# Preserve the small public cache-control surface used by diagnostics/tests
# and by ``mark_encoder_unavailable`` without exposing the private helper.
available_encoders.cache_clear = (  # type: ignore[attr-defined]
    _available_encoders_cached.cache_clear
)


def embedded_kokoro_available() -> bool:
    """Return whether the in-process Kokoro stack is present in this runtime."""

    try:
        return all(find_spec(module) is not None for module in ("kokoro", "numpy", "soundfile"))
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def system_snapshot() -> dict[str, Any]:
    ffmpeg = resolve_ffmpeg()
    encoders = available_encoders(ffmpeg)
    return {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "machine": platform.machine(),
        "ffmpeg_path": str(ffmpeg) if ffmpeg else "",
        "ffmpeg_ready": ffmpeg is not None,
        "encoders": encoders,
        "recommended_encoder": encoders[0] if encoders else "",
        "webview_runtime": "Edge WebView2" if os.name == "nt" else "WebKit",
        "embedded_kokoro_ready": embedded_kokoro_available(),
        # This means the no-key client is installed.  The upstream online
        # service and language-specific voices are probed only on demand.
        "edge_tts_runtime_ready": edge_tts_runtime_available(),
    }
