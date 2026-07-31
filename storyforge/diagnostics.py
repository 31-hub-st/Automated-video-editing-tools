from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
import wave
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from . import __version__
from .providers.base import ProviderConfig
from .providers.tts import create_tts_provider


ProviderFactory = Callable[[Any], Any]


def _write_json(path: Path, value: dict[str, Any]) -> None:
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def run_kokoro_self_test(
    output_dir: str | Path,
    *,
    provider_factory: ProviderFactory = create_tts_provider,
) -> int:
    """Run a real embedded-Kokoro synthesis and persist a machine-readable result.

    This entry point is intentionally usable from the frozen windowed EXE. It
    catches packaging-only failures that source-environment tests cannot see.
    """

    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    result_path = root / "kokoro-self-test.json"
    log_path = root / "kokoro-self-test.log"
    payload: dict[str, Any] = {
        "ok": False,
        "status": "running",
        "app_version": __version__,
        "started_utc": datetime.now(UTC).isoformat(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "voice": "af_heart",
        "tts_cache_bypassed": True,
        "log_path": str(log_path),
    }
    _write_json(result_path, payload)

    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as log:
            with redirect_stdout(log), redirect_stderr(log):
                # A health check must execute the model. Reusing a sentence WAV
                # from the normal TTS cache can report success even when this
                # computer is missing the model needed for every new sentence.
                provider = provider_factory(
                    ProviderConfig(
                        name="local_kokoro",
                        options={"cache_enabled": False},
                    )
                )
                result = provider.synthesize(
                    ["StoryForge packaged local voice is ready."],
                    root,
                    voice="af_heart",
                    speed=1.0,
                    file_stem="kokoro-self-test",
                )
        wav_path = Path(result.path).resolve()
        with wave.open(str(wav_path), "rb") as stream:
            sample_rate = stream.getframerate()
            frame_count = stream.getnframes()
            channel_count = stream.getnchannels()
            sample_width = stream.getsampwidth()
        if (
            sample_rate != 24_000
            or frame_count <= 0
            or channel_count != 1
            or sample_width != 2
        ):
            raise RuntimeError(
                "Unexpected Kokoro WAV: "
                f"{sample_rate} Hz, {channel_count} channels, "
                f"{sample_width}-byte samples, {frame_count} frames."
            )
        payload.update(
            ok=True,
            status="passed",
            provider=result.provider,
            model=result.model,
            duration_seconds=result.duration_seconds,
            sample_rate=sample_rate,
            frame_count=frame_count,
            channel_count=channel_count,
            sample_width=sample_width,
            wav_path=str(wav_path),
            wav_bytes=wav_path.stat().st_size,
        )
        exit_code = 0
    except BaseException as error:
        payload.update(
            status="failed",
            error_type=type(error).__name__,
            error=str(error) or type(error).__name__,
            traceback=traceback.format_exc(),
        )
        exit_code = 1

    payload["finished_utc"] = datetime.now(UTC).isoformat()
    _write_json(result_path, payload)
    return exit_code


def run_startup_self_test(
    output_dir: str | Path,
    *,
    ui_root: str | Path,
) -> int:
    """Exercise the frozen startup path without opening a desktop window.

    Unlike the voice-only health check, this initializes the real repository
    and catalog, imports the packaged WebView2 backend, locates FFmpeg, starts
    the localhost production worker and reads its health endpoint.  Build and
    release scripts use it to catch an old executable against a newer catalog
    before that executable reaches another computer.
    """

    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    result_path = root / "startup-self-test.json"
    log_path = root / "startup-self-test.log"
    payload: dict[str, Any] = {
        "ok": False,
        "status": "running",
        "app_version": __version__,
        "started_utc": datetime.now(UTC).isoformat(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "data_dir": os.environ.get("STORYFORGE_DATA_DIR", ""),
        "log_path": str(log_path),
    }
    _write_json(result_path, payload)
    api: Any = None

    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as log:
            with redirect_stdout(log), redirect_stderr(log):
                from imageio_ffmpeg import get_ffmpeg_exe

                # ``webview.start(gui="edgechromium")`` imports the WinForms
                # backend and Python.NET on Windows.  Import both explicitly in
                # the frozen smoke test; importing edgechromium alone missed a
                # real employee-machine failure where Windows blocked
                # Python.Runtime.dll after ZIP extraction.
                import clr  # noqa: F401
                import webview
                import webview.platforms.edgechromium
                import webview.platforms.winforms

                from .api import StoryForgeApi
                from .catalog import SCHEMA_VERSION
                from .pipeline import PipelineRunner

                ui = Path(ui_root).resolve(strict=True)
                for name in ("index.html", "app.js", "styles.css"):
                    asset = ui / name
                    if not asset.is_file() or asset.stat().st_size <= 0:
                        raise FileNotFoundError(f"packaged UI asset is missing: {asset}")

                ffmpeg = Path(get_ffmpeg_exe()).resolve(strict=True)
                api = StoryForgeApi(
                    hub_listen_host="127.0.0.1",
                    hub_listen_port=0,
                )
                api._queue.set_processor(
                    PipelineRunner(
                        lambda: api._state.settings,
                        text_provider_factory=api._runtime_text_provider_factory,
                        work_root=api._repository.data_dir / "render-work",
                    )
                )
                worker = api._ensure_local_worker_server(
                    ui,
                    serve_ui=True,
                    use_port_override=True,
                )
                with urlopen(worker.base_url + "/worker/api/health", timeout=5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                health_data = dict(health.get("data") or {})
                if not bool(health.get("ok")) or health_data.get("service") != (
                    "storyforge-local-worker"
                ):
                    raise RuntimeError("localhost production worker health check failed")

                payload.update(
                    ok=True,
                    status="passed",
                    catalog_schema_version=SCHEMA_VERSION,
                    runtime_hub_mode=api._runtime_hub_mode,
                    ui_root=str(ui),
                    ffmpeg_path=str(ffmpeg),
                    webview_version=str(getattr(webview, "__version__", "")),
                    pythonnet_bridge_loaded=True,
                    worker_url=worker.base_url,
                    worker_ready=bool(health_data.get("ready")),
                )
        exit_code = 0
    except BaseException as error:
        payload.update(
            status="failed",
            error_type=type(error).__name__,
            error=str(error) or type(error).__name__,
            traceback=traceback.format_exc(),
        )
        exit_code = 1
    finally:
        if api is not None:
            try:
                api._shutdown()
            except BaseException as shutdown_error:
                payload.setdefault(
                    "shutdown_error",
                    str(shutdown_error) or type(shutdown_error).__name__,
                )

    payload["finished_utc"] = datetime.now(UTC).isoformat()
    _write_json(result_path, payload)
    return exit_code
