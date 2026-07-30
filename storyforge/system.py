from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from importlib.util import find_spec
from functools import lru_cache
from pathlib import Path
from typing import Any

from .cancellation import run_cancellable_process
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
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


@lru_cache(maxsize=8)
def _runtime_encoder_works(executable: str, encoder: str) -> bool:
    """Verify that a compiled hardware encoder can initialize on this PC.

    FFmpeg can list NVENC/QSV/AMF even when the matching GPU or driver is not
    present.  A one-frame probe prevents `auto` from selecting an unusable
    encoder and failing only after a long narration has already been created.
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
                "color=c=black:s=64x64:r=1:d=1",
                "-frames:v",
                "1",
                "-an",
                "-c:v",
                encoder,
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            # Hardware backends either initialize almost immediately or are
            # unavailable on the current PC.  A short bound keeps the desktop
            # bootstrap responsive when a driver advertises an encoder that it
            # cannot actually start.
            timeout=5,
            check=False,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@lru_cache(maxsize=4)
def available_encoders(ffmpeg: Path | None = None) -> list[str]:
    executable = ffmpeg or resolve_ffmpeg()
    if executable is None:
        return []
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
        return []
    output = f"{result.stdout}\n{result.stderr}"
    compiled = {
        name
        for name in ("h264_nvenc", "h264_qsv", "h264_amf", "libx264")
        if name in output
    }
    hardware = [
        name
        for name in ("h264_nvenc", "h264_qsv", "h264_amf")
        if name in compiled
    ]
    # Probe independent GPU backends concurrently.  The previous sequential
    # implementation could make the splash status appear frozen for as long as
    # 36 seconds on machines with stale NVENC/QSV/AMF driver registrations.
    with ThreadPoolExecutor(max_workers=max(1, len(hardware))) as executor:
        futures = [
            executor.submit(
                copy_context().run,
                _runtime_encoder_works,
                str(executable),
                name,
            )
            for name in hardware
        ]
        results = [future.result() for future in futures]
    working = [name for name, ready in zip(hardware, results, strict=True) if ready]
    if "libx264" in compiled:
        working.append("libx264")
    return working


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
