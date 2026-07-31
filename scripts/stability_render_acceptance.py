from __future__ import annotations

"""Repeatable, offline acceptance test for StoryForge's real render pipeline.

This is deliberately not a unit-test mock of FFmpeg.  It creates real 60 FPS
H.264 source clips, a real MP3 music track and a cover image, then invokes the
same :class:`PipelineRunner` used by production.  Only the text and TTS
providers are deterministic offline fakes so the result does not depend on an
API key, a network connection, or a large local speech model.

``--quick`` is only a packaging/diagnostic smoke test.  It can never approve a
stable release.  ``--stress`` is the release gate: it renders an exact 311.05
second 30 FPS case and a configurable (600 second by default) 60 FPS case,
including 300+ real ASS events and both closing-cover states.

Examples::

    python scripts/stability_render_acceptance.py --quick --ffprobe C:\\ffmpeg\\bin\\ffprobe.exe
    python scripts/stability_render_acceptance.py --stress --stress-seconds 600 \
        --app-root "D:\\StoryForge Studio"
"""

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import platform as runtime_platform
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import wave
import zipfile


SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = SCRIPT_DIR.parent
if (SOURCE_ROOT / "storyforge").is_dir() and str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from storyforge import __version__  # noqa: E402
from storyforge.models import AppSettings, PlatformProfile, RenderJob  # noqa: E402
from storyforge.pipeline import (  # noqa: E402
    PipelineRunner,
    UsageLedger,
    job_workspace_directory,
)
from storyforge.providers.text import TextResult  # noqa: E402
from storyforge.providers.tts import SpeechSegment, TTSResult  # noqa: E402
from storyforge.services.quality import resolve_ffprobe  # noqa: E402
from storyforge.system import resolve_ffmpeg  # noqa: E402


class AcceptanceError(RuntimeError):
    pass


RELEASE_EXACT_SECONDS = 311.05
RELEASE_DEFAULT_STRESS_SECONDS = 600.0
RELEASE_MINIMUM_ASS_EVENTS = 300
RESOURCE_SAMPLE_INTERVAL_SECONDS = 0.25


def _console_print(*values: object, file: object | None = None) -> None:
    """Best-effort console output that is safe in a windowed frozen EXE."""

    stream = file if file is not None else sys.stdout
    if stream is None:
        return
    try:
        print(*values, file=stream, flush=True)
    except (OSError, ValueError):
        # PyInstaller's windowed bootloader has no valid stdout/stderr handle.
        # The acceptance JSON is authoritative and must still be persisted.
        return


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = (
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    )


class _PerformanceInformation(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wintypes.DWORD),
        ("ProcessCount", wintypes.DWORD),
        ("ThreadCount", wintypes.DWORD),
    )


class _ProcessMemoryCountersEx(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    )


def _windows_resource_snapshot() -> dict[str, int | float | None]:
    """Return commit, physical-memory and current-process counters.

    This deliberately uses only Win32 APIs so the release test does not add a
    dependency that might be absent from the employee package it is testing.
    Values are nullable on non-Windows hosts or when an individual API is not
    available; an unavailable metric never aborts the render itself.
    """

    snapshot: dict[str, int | float | None] = {
        "system_available_physical_bytes": None,
        "system_total_physical_bytes": None,
        "system_commit_used_bytes": None,
        "system_commit_limit_bytes": None,
        "system_commit_ratio": None,
        "process_working_set_bytes": None,
        "process_private_bytes": None,
        "process_os_peak_working_set_bytes": None,
    }
    if os.name != "nt":
        return snapshot
    try:
        memory = _MemoryStatusEx()
        memory.dwLength = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            snapshot["system_available_physical_bytes"] = int(memory.ullAvailPhys)
            snapshot["system_total_physical_bytes"] = int(memory.ullTotalPhys)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    try:
        performance = _PerformanceInformation()
        performance.cb = ctypes.sizeof(performance)
        if ctypes.windll.psapi.GetPerformanceInfo(
            ctypes.byref(performance), performance.cb
        ):
            page_size = int(performance.PageSize)
            commit_used = int(performance.CommitTotal) * page_size
            commit_limit = int(performance.CommitLimit) * page_size
            snapshot["system_commit_used_bytes"] = commit_used
            snapshot["system_commit_limit_bytes"] = commit_limit
            snapshot["system_commit_ratio"] = (
                round(commit_used / commit_limit, 6) if commit_limit else None
            )
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    try:
        counters = _ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCountersEx),
            wintypes.DWORD,
        )
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(
            process, ctypes.byref(counters), counters.cb
        ):
            snapshot["process_working_set_bytes"] = int(counters.WorkingSetSize)
            snapshot["process_private_bytes"] = int(counters.PrivateUsage)
            snapshot["process_os_peak_working_set_bytes"] = int(
                counters.PeakWorkingSetSize
            )
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return snapshot


class ResourceSampler:
    """Sample resource pressure while the production pipeline is running."""

    def __init__(self, interval_seconds: float = RESOURCE_SAMPLE_INTERVAL_SECONDS) -> None:
        self.interval_seconds = max(0.05, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._samples: list[dict[str, int | float | None]] = []

    def start(self) -> None:
        self._sample()
        self._thread = threading.Thread(
            target=self._run,
            name="storyforge-acceptance-resource-sampler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 4.0))
        self._sample()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def _sample(self) -> None:
        sample = _windows_resource_snapshot()
        sample["monotonic_seconds"] = round(time.perf_counter(), 6)
        with self._lock:
            self._samples.append(sample)

    def report(self) -> dict[str, object]:
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return {"available": False, "sample_count": 0}

        def values(key: str) -> list[int | float]:
            return [
                value
                for sample in samples
                if isinstance((value := sample.get(key)), (int, float))
            ]

        available_values = values("system_available_physical_bytes")
        commit_values = values("system_commit_used_bytes")
        commit_ratios = values("system_commit_ratio")
        working_set_values = values("process_working_set_bytes")
        private_values = values("process_private_bytes")
        return {
            "available": bool(available_values or working_set_values),
            "sample_interval_seconds": self.interval_seconds,
            "sample_count": len(samples),
            "initial": samples[0],
            "final": samples[-1],
            "minimum_system_available_physical_bytes": (
                int(min(available_values)) if available_values else None
            ),
            "peak_system_commit_used_bytes": (
                int(max(commit_values)) if commit_values else None
            ),
            "peak_system_commit_ratio": (
                round(float(max(commit_ratios)), 6) if commit_ratios else None
            ),
            "peak_process_working_set_bytes": (
                int(max(working_set_values)) if working_set_values else None
            ),
            "peak_process_private_bytes": (
                int(max(private_values)) if private_values else None
            ),
        }


def _default_root() -> Path:
    configured = str(os.environ.get("STORYFORGE_ACCEPTANCE_ROOT") or "").strip()
    if configured:
        return Path(configured)
    if os.name == "nt" and Path("D:/").is_dir():
        return Path("D:/StoryForgeBuildTemp/acceptance")
    return SOURCE_ROOT / ".runtime-acceptance"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_zip_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    with archive.open(member, "r") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_RELEASE_VALIDATION_NAME = "BUILD_RELEASE_VALIDATION.json"
_UPDATE_METADATA_NAME = "storyforge-update.json"
_PORTABLE_DATA_NAME = "StoryForgeData"


def _manifest_summary(
    records: list[tuple[str, int, str]],
) -> dict[str, object]:
    records.sort(key=lambda item: item[0].casefold())
    digest = hashlib.sha256()
    for relative, size, file_hash in records:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return {
        "bundle_manifest_sha256": digest.hexdigest(),
        "bundle_file_count": len(records),
        "bundle_size_bytes": sum(item[1] for item in records),
        "bundle_files": [item[0] for item in records],
    }


def _included_release_relative(relative: str) -> bool:
    parts = Path(relative).parts
    if not parts:
        return False
    if parts[0].casefold() == _PORTABLE_DATA_NAME.casefold():
        return False
    if relative.casefold() in {
        _RELEASE_VALIDATION_NAME.casefold(),
        _UPDATE_METADATA_NAME.casefold(),
    }:
        return False
    return not any(part in {".git", "__pycache__"} for part in parts)


def _zip_release_manifest(archive: zipfile.ZipFile) -> dict[str, object]:
    records: list[tuple[str, int, str]] = []
    normalized: set[str] = set()
    for info in archive.infolist():
        if info.is_dir():
            continue
        relative = info.filename.replace("\\", "/").strip("/")
        if not _included_release_relative(relative):
            continue
        key = relative.casefold()
        if key in normalized:
            raise AcceptanceError(
                f"Package contains a case-insensitive duplicate: {relative}"
            )
        normalized.add(key)
        records.append((relative, int(info.file_size), _sha256_zip_member(archive, info)))
    return _manifest_summary(records)


def _directory_release_manifest(root: Path) -> dict[str, object]:
    records: list[tuple[str, int, str]] = []
    normalized: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if not _included_release_relative(relative):
            continue
        key = relative.casefold()
        if key in normalized:
            raise AcceptanceError(
                f"Runtime bundle contains a case-insensitive duplicate: {relative}"
            )
        normalized.add(key)
        records.append((relative, int(path.stat().st_size), _sha256_file(path)))
    return _manifest_summary(records)


def _manifest_matches_attestation(
    manifest: dict[str, object], attestation: dict[str, object]
) -> bool:
    return bool(
        str(manifest.get("bundle_manifest_sha256") or "").casefold()
        == str(attestation.get("bundle_manifest_sha256") or "").casefold()
        and int(manifest.get("bundle_file_count") or -1)
        == int(attestation.get("bundle_file_count") or -2)
        and int(manifest.get("bundle_size_bytes") or -1)
        == int(attestation.get("bundle_size_bytes") or -2)
        and list(manifest.get("bundle_files") or [])
        == list(attestation.get("bundle_files") or [])
    )


def _runtime_identity() -> dict[str, object]:
    executable = Path(sys.executable).resolve()
    frozen = bool(getattr(sys, "frozen", False))
    return {
        "frozen": frozen,
        "executable": str(executable),
        "executable_sha256": (
            _sha256_file(executable) if executable.is_file() else None
        ),
        "executable_bytes": (
            executable.stat().st_size if executable.is_file() else None
        ),
    }


def _package_identity(args: argparse.Namespace) -> dict[str, object]:
    """Identify the exact package or executable under acceptance."""

    requested = getattr(args, "package_artifact", None)
    target = requested.expanduser().resolve() if requested else None
    identity_kind = "explicit_artifact"
    if target is None and args.app_root:
        app_root = args.app_root.expanduser().resolve()
        target = _find_named_binary(
            app_root,
            (
                "StoryForge Studio.exe",
                "StoryForgeStudio.exe",
                "storyforge.exe",
            ),
        )
        identity_kind = "packaged_executable"
    if target is None:
        target = (SOURCE_ROOT / "storyforge" / "__init__.py").resolve()
        identity_kind = "source_version_file"
    if not target.is_file():
        return {
            "kind": identity_kind,
            "path": str(target),
            "sha256": None,
            "bytes": None,
            "error": "artifact_not_found",
        }
    identity: dict[str, object] = {
        "kind": identity_kind,
        "path": str(target),
        "sha256": _sha256_file(target),
        "bytes": target.stat().st_size,
    }
    runtime = _runtime_identity()
    identity["runtime"] = runtime

    if target.suffix.casefold() == ".zip":
        try:
            with zipfile.ZipFile(target, "r") as archive:
                metadata_info = archive.getinfo("storyforge-update.json")
                if metadata_info.file_size > 64 * 1024:
                    raise AcceptanceError("Package metadata is unexpectedly large.")
                metadata = json.loads(archive.read(metadata_info).decode("utf-8"))
                if not isinstance(metadata, dict):
                    raise AcceptanceError("Package metadata is not an object.")
                package_version = str(metadata.get("version") or "").strip()
                entrypoint = str(metadata.get("entrypoint") or "").replace("\\", "/").strip("/")
                if package_version != __version__:
                    raise AcceptanceError(
                        f"Package version {package_version or '<missing>'} does not match {__version__}."
                    )
                if not entrypoint or ".." in Path(entrypoint).parts:
                    raise AcceptanceError("Package entrypoint is unsafe or missing.")
                entry_info = archive.getinfo(entrypoint)
                entry_hash = _sha256_zip_member(archive, entry_info)
                validation_info = archive.getinfo(_RELEASE_VALIDATION_NAME)
                if validation_info.file_size > 1024 * 1024:
                    raise AcceptanceError("Release validation is unexpectedly large.")
                attestation = json.loads(
                    archive.read(validation_info).decode("utf-8")
                )
                if not isinstance(attestation, dict):
                    raise AcceptanceError("Release validation is not an object.")
                if (
                    attestation.get("ok") is not True
                    or attestation.get("frozen") is not True
                    or str(attestation.get("app_version") or "") != __version__
                    or str(attestation.get("entrypoint") or "").replace("\\", "/")
                    != entrypoint
                    or str(attestation.get("entrypoint_sha256") or "").casefold()
                    != entry_hash
                ):
                    raise AcceptanceError("Release validation identity does not match the package.")
                zip_manifest = _zip_release_manifest(archive)
                runtime_root = Path(str(runtime.get("executable") or "")).parent
                runtime_validation = runtime_root / _RELEASE_VALIDATION_NAME
                runtime_validation_hash = (
                    _sha256_file(runtime_validation)
                    if runtime_validation.is_file()
                    else None
                )
                runtime_manifest = _directory_release_manifest(runtime_root)
                validation_hash = _sha256_zip_member(archive, validation_info)
                zip_manifest_matches = _manifest_matches_attestation(
                    zip_manifest, attestation
                )
                runtime_manifest_matches = _manifest_matches_attestation(
                    runtime_manifest, attestation
                )
                identity.update(
                    format="storyforge_update_zip",
                    package_version=package_version,
                    entrypoint=entrypoint,
                    entrypoint_sha256=entry_hash,
                    entrypoint_bytes=entry_info.file_size,
                    metadata_valid=True,
                    release_attestation_valid=True,
                    release_validation_sha256=validation_hash,
                    release_validation_matches_runtime=(
                        runtime_validation_hash == validation_hash
                    ),
                    zip_bundle_manifest=zip_manifest,
                    zip_bundle_manifest_matches=zip_manifest_matches,
                    runtime_bundle_manifest=runtime_manifest,
                    runtime_bundle_manifest_matches=runtime_manifest_matches,
                    runtime_entrypoint_matches=bool(
                        runtime.get("frozen")
                        and Path(str(runtime.get("executable") or "")).name.casefold()
                        == Path(entrypoint).name.casefold()
                        and runtime.get("executable_sha256") == entry_hash
                        and runtime_validation_hash == validation_hash
                        and zip_manifest_matches
                        and runtime_manifest_matches
                    ),
                )
        except (KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile, AcceptanceError) as error:
            identity.update(
                format="invalid_zip",
                metadata_valid=False,
                release_attestation_valid=False,
                runtime_entrypoint_matches=False,
                error=f"{type(error).__name__}: {error}",
            )
    elif target.suffix.casefold() == ".exe":
        identity.update(
            format="frozen_executable",
            metadata_valid=True,
            release_attestation_valid=False,
            runtime_entrypoint_matches=bool(
                runtime.get("frozen")
                and runtime.get("executable_sha256") == identity.get("sha256")
                and Path(str(runtime.get("executable") or "")).name.casefold()
                == target.name.casefold()
            ),
        )
    else:
        identity.update(
            format="unverified_file",
            metadata_valid=False,
            release_attestation_valid=False,
            runtime_entrypoint_matches=False,
        )
    return identity


def _explicit_package_artifact_is_bound(
    args: argparse.Namespace, package: dict[str, object]
) -> bool:
    """Return whether this run is cryptographically bound to a real artifact."""

    return bool(
        getattr(args, "package_artifact", None)
        and package.get("kind") == "explicit_artifact"
        and package.get("sha256")
        and isinstance(package.get("bytes"), int)
        and int(package["bytes"]) > 0
        and not package.get("error")
        and package.get("metadata_valid") is True
        and package.get("runtime_entrypoint_matches") is True
    )


_VIDEO_ENCODER_OPTION = re.compile(
    r"(?:^|\s)(?:-c:v|-codec:v|-vcodec)\s+(?:\"([^\"]+)\"|(\S+))",
    re.IGNORECASE,
)
_RENDER_STAGE_COMMAND_FILE = {
    "ffmpeg_render": "render-command.txt",
    "ffmpeg_cpu_fallback": "render-command-fallback.txt",
    "ffmpeg_serial_fallback": "render-command-low-memory.txt",
}


def _actual_render_command_encoder(
    job_dir: Path, render_attempts: object
) -> dict[str, object]:
    """Read the final real FFmpeg command artifact and extract ``-c:v``.

    The attempt ledger selects the successful FFmpeg stage; the encoder itself
    is then independently parsed from that stage's command artifact. This
    avoids mistaking multi-source preparation commands for the final encode
    and deliberately does not trust the manifest's summary encoder field.
    """

    attempts = [item for item in (render_attempts or []) if isinstance(item, dict)]
    successful = [item for item in attempts if bool(item.get("succeeded"))]
    if not successful:
        raise AcceptanceError("Manifest contains no successful real FFmpeg attempt.")
    stage = str(successful[-1].get("stage") or "")
    filename = _RENDER_STAGE_COMMAND_FILE.get(stage)
    if not filename:
        raise AcceptanceError(f"Unknown successful FFmpeg stage: {stage or '<missing>'}")
    path = job_dir / filename
    if not path.is_file():
        raise AcceptanceError(
            f"Successful FFmpeg stage {stage} has no command artifact: {path}"
        )
    command_text = path.read_text(encoding="utf-8", errors="replace")
    matches = list(_VIDEO_ENCODER_OPTION.finditer(command_text))
    if not matches:
        raise AcceptanceError(f"Real render command does not contain -c:v: {path}")
    match = matches[-1]
    encoder = str(match.group(1) or match.group(2) or "").strip()
    if not encoder:
        raise AcceptanceError(f"Real render command has an empty -c:v value: {path}")
    return {
        "encoder": encoder,
        "path": str(path.resolve()),
        "filename": filename,
        "stage": stage,
        "command_text": command_text,
    }


def _source_revision() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(SOURCE_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5.0,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = str(completed.stdout or "").strip()
    return revision if completed.returncode == 0 and revision else None


def _ffmpeg_version(ffmpeg: Path) -> str:
    try:
        completed = run_command([str(ffmpeg), "-version"], timeout=15.0)
    except (AcceptanceError, OSError, subprocess.SubprocessError):
        return ""
    return str(completed.stdout or completed.stderr or "").splitlines()[0].strip()


def _find_named_binary(app_root: Path | None, names: tuple[str, ...]) -> Path | None:
    if app_root is None or not app_root.is_dir():
        return None
    direct_roots = (app_root, app_root / "_internal", app_root / "bin")
    for root in direct_roots:
        for name in names:
            candidate = root / name
            if candidate.is_file():
                return candidate.resolve()
    for pattern in names:
        matches = sorted(app_root.rglob(pattern), key=lambda item: len(item.parts))
        if matches:
            return matches[0].resolve()
    return None


def resolve_tools(args: argparse.Namespace) -> tuple[Path, Path]:
    app_root = args.app_root.expanduser().resolve() if args.app_root else None
    ffmpeg = args.ffmpeg.expanduser().resolve() if args.ffmpeg else None
    if ffmpeg is None:
        ffmpeg = _find_named_binary(
            app_root,
            ("ffmpeg.exe", "ffmpeg", "ffmpeg-win-*.exe", "ffmpeg-*.exe"),
        )
    if ffmpeg is None:
        ffmpeg = resolve_ffmpeg()
    if ffmpeg is None or not ffmpeg.is_file():
        raise AcceptanceError(
            "FFmpeg was not found. Pass --ffmpeg or --app-root pointing at the "
            "unpacked StoryForge application."
        )

    ffprobe = args.ffprobe.expanduser().resolve() if args.ffprobe else None
    if ffprobe is None:
        ffprobe = _find_named_binary(app_root, ("ffprobe.exe", "ffprobe"))
    if ffprobe is None:
        ffprobe = resolve_ffprobe(ffmpeg)
    if ffprobe is None or not ffprobe.is_file():
        raise AcceptanceError(
            "ffprobe is required for acceptance verification. Pass --ffprobe "
            "or use --app-root with a package containing ffprobe."
        )
    return ffmpeg, ffprobe


def run_command(command: list[str], *, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "unknown command error")[-3000:]
        raise AcceptanceError(
            f"Command exited with {completed.returncode}: {subprocess.list2cmdline(command)}\n{detail}"
        )
    return completed


def create_video(ffmpeg: Path, path: Path, *, duration: float, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=360x640:r=60:d={duration:.3f}",
            "-vf",
            "drawbox=x=mod(t*90\\,300):y=180:w=60:h=160:color=white@0.7:t=fill",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ]
    )


def create_music(ffmpeg: Path, path: Path, *, duration: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=220:sample_rate=48000:duration={duration:.3f}",
            "-af",
            "volume=0.08",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(path),
        ]
    )


def create_cover(ffmpeg: Path, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x18243A:s=720x1280:d=0.1",
            "-frames:v",
            "1",
            "-c:v",
            "png",
            "-threads",
            "1",
            str(path),
        ]
    )


def write_tone_wav(path: Path, duration: float, *, frequency: float) -> None:
    rate = 48_000
    frames = max(1, round(duration * rate))
    amplitude = 1_600
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        # Reuse a short deterministic waveform block.  The acceptance input is
        # intentionally cheap to synthesize so measured time and memory belong
        # to the production renderer rather than this offline TTS fake.
        template_frames = min(frames, 4_800)
        block = bytearray()
        for index in range(template_frames):
            value = round(amplitude * math.sin(2.0 * math.pi * frequency * index / rate))
            block.extend(struct.pack("<h", value))
        full_blocks, remainder = divmod(frames, template_frames)
        for _ in range(full_blocks):
            stream.writeframesraw(block)
        if remainder:
            stream.writeframesraw(block[: remainder * 2])


class OfflineTextProvider:
    def polish(self, request) -> TextResult:
        return TextResult(
            polished_text=request.text,
            hook="The message on her phone changed everything.",
            ending_cta=(
                f"Download {request.platform} and search code {request.code} "
                "to continue reading."
            ),
            mood="suspense",
            provider="acceptance-offline-text",
            model="deterministic",
            retention_ratio=1.0,
        )


class OfflineTTSProvider:
    def __init__(self, target_seconds: float) -> None:
        self.target_seconds = max(2.0, float(target_seconds))

    def synthesize(self, sentences, output_dir, *, voice, speed, file_stem) -> TTSResult:
        spoken = [str(item).strip() for item in sentences if str(item).strip()]
        if not spoken:
            raise AcceptanceError("The production pipeline supplied no narration sentences.")
        seconds = self.target_seconds / len(spoken)
        segments: list[SpeechSegment] = []
        for index, sentence in enumerate(spoken, start=1):
            path = Path(output_dir) / f"{file_stem}-{index:04d}.wav"
            write_tone_wav(path, seconds, frequency=170.0 + index * 11.0)
            segments.append(
                SpeechSegment(
                    index=index,
                    text=sentence,
                    path=str(path),
                    duration_seconds=seconds,
                    voice=voice or "acceptance-female",
                    provider="acceptance-offline-tts",
                )
            )
        return TTSResult(tuple(segments), provider="acceptance-offline-tts")


def probe(ffprobe: Path, path: Path) -> dict[str, object]:
    completed = run_command(
        [
            str(ffprobe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=60.0,
    )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as error:
        raise AcceptanceError(f"ffprobe returned invalid JSON for {path}") from error
    if not isinstance(payload, dict):
        raise AcceptanceError(f"ffprobe returned an invalid payload for {path}")
    return payload


def _rate(value: object) -> float:
    text = str(value or "")
    if "/" in text:
        top, bottom = text.split("/", 1)
        return float(top) / float(bottom) if float(bottom) else 0.0
    return float(text or 0.0)


def verify_video(
    ffprobe: Path,
    path: Path,
    *,
    expected_fps: int,
    expected_duration_seconds: float,
) -> dict[str, object]:
    payload = probe(ffprobe, path)
    streams = [item for item in payload.get("streams", []) if isinstance(item, dict)]
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    if not videos or not audios:
        raise AcceptanceError(f"{path.name} must contain video and audio streams")
    video = videos[0]
    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    fps = _rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    expected = {
        "codec": str(video.get("codec_name") or "").casefold(),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": fps,
        "duration": duration,
        "video_streams": len(videos),
        "audio_streams": len(audios),
    }
    if expected["codec"] != "h264":
        raise AcceptanceError(f"Expected H.264, got {expected['codec']!r}")
    if (expected["width"], expected["height"]) != (1080, 1920):
        raise AcceptanceError(
            f"Expected 1080x1920, got {expected['width']}x{expected['height']}"
        )
    if abs(float(expected["fps"]) - float(expected_fps)) > 0.2:
        raise AcceptanceError(
            f"Expected {expected_fps} FPS, got {expected['fps']}"
        )
    if duration <= 0.25 or path.stat().st_size <= 1_024:
        raise AcceptanceError(f"Rendered video is empty or too short: {path}")
    if abs(duration - float(expected_duration_seconds)) > 0.75:
        raise AcceptanceError(
            "Rendered duration does not match the acceptance workload: "
            f"expected {expected_duration_seconds:.3f}s, got {duration:.3f}s"
        )
    return expected


def verify_mp3(ffprobe: Path, path: Path) -> dict[str, object]:
    payload = probe(ffprobe, path)
    streams = [item for item in payload.get("streams", []) if isinstance(item, dict)]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    videos = [item for item in streams if item.get("codec_type") == "video"]
    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    codec = str(audios[0].get("codec_name") or "").casefold() if audios else ""
    if len(audios) != 1 or videos or codec not in {"mp3", "mp3float"}:
        raise AcceptanceError(f"Narration is not a standalone MP3 stream: {path}")
    if duration <= 0.25 or path.stat().st_size <= 1_024:
        raise AcceptanceError(f"Narration MP3 is empty or too short: {path}")
    return {"codec": codec, "duration": duration, "audio_streams": len(audios)}


def prepare_inputs(
    ffmpeg: Path,
    root: Path,
    *,
    narration_seconds: float,
    sentence_count: int,
) -> dict[str, Path]:
    input_root = root / "inputs"
    source = input_root / "B73165_Acceptance Story.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    sentences = [
        (
            f"Mara followed clue number {index:03d}, and the secret behind "
            "the locked door became more dangerous."
        )
        for index in range(1, max(4, int(sentence_count)) + 1)
    ]
    midpoint = max(1, len(sentences) // 2)
    source.write_text(
        "Chapter 1\n"
        + " ".join(sentences[:midpoint])
        + "\n\nChapter 2\n"
        + " ".join(sentences[midpoint:]),
        encoding="utf-8",
    )
    cover = input_root / "cover.png"
    create_cover(ffmpeg, cover)

    single_video = input_root / "single-videos" / "suspense" / "single.mp4"
    create_video(
        ffmpeg,
        single_video,
        # The sustained 60 FPS release scenario must exercise one genuinely
        # long source clip. Looping a 15-second input forces the renderer to
        # CPU-normalise roughly 41 segments before the measured final encode,
        # obscuring the hardware graph this gate is intended to validate.
        # Quick mode still requests only six seconds here.
        duration=max(6.0, narration_seconds + 1.0),
        color="0x23497A",
    )

    multi_root = input_root / "multi-videos" / "suspense"
    colors = ("0x7A2349", "0x236A55", "0x58418A", "0x8A5A24")
    multi_clip_seconds = 1.0 if narration_seconds < 60.0 else 15.0
    for index, color in enumerate(colors, start=1):
        create_video(
            ffmpeg,
            multi_root / f"clip-{index}.mp4",
            duration=multi_clip_seconds,
            color=color,
        )

    music = input_root / "music" / "suspense" / "acceptance-music.mp3"
    create_music(ffmpeg, music, duration=max(3.0, min(30.0, narration_seconds)))
    (input_root / "no-music").mkdir(parents=True, exist_ok=True)
    return {
        "source": source,
        "cover": cover,
        "single_videos": single_video.parents[1],
        "multi_videos": multi_root.parents[0],
        "music": music.parents[1],
        "no_music": input_root / "no-music",
    }


def render_scenario(
    *,
    name: str,
    root: Path,
    ffmpeg: Path,
    ffprobe: Path,
    inputs: dict[str, Path],
    narration_seconds: float,
    output_fps: int,
    cover_outro_enabled: bool,
    minimum_ass_events: int,
    encoder: str,
    multi: bool,
    bgm: bool,
) -> dict[str, object]:
    scenario_root = root / name
    output_root = scenario_root / "output"
    work_root = scenario_root / "private-work"
    settings = AppSettings(
        narration_wpm=240,
        output_width=1080,
        output_height=1920,
        output_fps=int(output_fps),
        output_mode="video_and_mp3",
        video_encoder=encoder,
        bgm_mode="auto" if bgm else "none",
        bgm_volume=0.18,
        caption_mode="semantic",
        subtitle_preset="clear_outline",
        subtitle_animation="none",
        video_template="classic",
        cover_animation="gentle_push",
        cover_outro_enabled=bool(cover_outro_enabled),
        end_card_seconds=1.0,
        # A named hardware encoder must exercise the same speed path used on
        # employee workstations.  libx264 remains the deterministic CPU
        # fallback/release baseline.
        render_mode="compatibility" if encoder == "libx264" else "speed",
    )
    settings.providers.text_provider = "local"
    settings.providers.tts_provider = "local_kokoro"
    job = RenderJob(
        id=f"acceptance-{name}",
        batch_id=f"acceptance-{name}",
        platform_id="goodnovel",
        source_file=str(inputs["source"]),
        title=f"Acceptance {name}",
        code="B73165",
        promo_code_snapshot="B73165",
        video_folder=str(inputs["multi_videos"] if multi else inputs["single_videos"]),
        music_folder=str(inputs["music"] if bgm else inputs["no_music"]),
        output_folder=str(output_root),
        novel_id="acceptance-novel",
        revision_id="acceptance-revision",
        episode_id="acceptance-episode",
        episode_ids=("acceptance-episode",),
        episode_label="E001",
        production_draft_id=f"acceptance-draft-{name}",
        production_run_id=f"acceptance-run-{name}",
        episode_number=1,
        episode_count=1,
        is_final_episode=True,
        variant_seed=73,
        cover_path=str(inputs["cover"]),
        locked_voice_provider="local_kokoro",
        locked_voice_id="af_heart",
        story_mood="suspense",
        settings_snapshot=settings.to_dict(),
    )
    platform = PlatformProfile(
        id="goodnovel",
        name="GoodNovel",
        search_template="Search {platform}: {code}",
        ending_template="Download {platform} and search code {code} to continue reading.",
    )
    stages: list[dict[str, object]] = []
    started = time.perf_counter()
    first_frame_seconds: float | None = None
    last_console_progress = -1.0
    last_console_status = ""

    def progress(status, value: float, label: str) -> None:
        nonlocal first_frame_seconds, last_console_progress, last_console_status
        event = {"status": status.value, "progress": round(float(value), 3), "label": label}
        stages.append(event)
        # Pipeline FFmpeg progress advances only after its machine-readable
        # stream has reported at least one video frame.  This is more truthful
        # than observing MP4 creation, which happens before encoding begins.
        if (
            first_frame_seconds is None
            and status.value == "rendering"
            and re.search(r"\b(?:[1-9]|[1-9]\d|100)%", str(label or ""))
        ):
            first_frame_seconds = round(time.perf_counter() - started, 3)
        if (
            status.value != last_console_status
            or float(value) >= last_console_progress + 0.02
            or float(value) >= 0.94
        ):
            _console_print(json.dumps({"scenario": name, **event}, ensure_ascii=False))
            last_console_progress = float(value)
            last_console_status = status.value

    runner = PipelineRunner(
        lambda: settings,
        ffmpeg_path=ffmpeg,
        text_provider_factory=lambda _config: OfflineTextProvider(),
        tts_provider_factory=lambda _config: OfflineTTSProvider(narration_seconds),
        usage_ledger=UsageLedger(scenario_root / "usage.json"),
        work_root=work_root,
    )
    job_dir = job_workspace_directory(job, work_root)
    sampler = ResourceSampler()
    sampler.start()
    result: dict[str, object] = {
        "name": name,
        "ok": False,
        "expected": {
            "duration_seconds": float(narration_seconds),
            "width": 1080,
            "height": 1920,
            "fps": int(output_fps),
            "cover_outro_enabled": bool(cover_outro_enabled),
            "minimum_ass_events": int(minimum_ass_events),
            "multi_source": bool(multi),
            "bgm": bool(bgm),
        },
        "stages": stages,
    }
    try:
        video_path = Path(runner(job, platform, progress)).resolve()
        audio_path = Path(job.narration_audio_file).resolve()
        manifest_path = job_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        duration_check = next(
            (
                item
                for item in (manifest.get("quality_control", {}).get("checks") or [])
                if isinstance(item, dict) and item.get("name") == "duration_seconds"
            ),
            {},
        )
        intended_duration = float(
            ((duration_check.get("expected") or {}).get("target"))
            or narration_seconds
        )
        video_probe = verify_video(
            ffprobe,
            video_path,
            expected_fps=int(output_fps),
            expected_duration_seconds=intended_duration,
        )
        audio_probe = verify_mp3(ffprobe, audio_path)
        if float(audio_probe.get("duration") or 0.0) + 0.75 < float(narration_seconds):
            raise AcceptanceError(
                "Narration MP3 is shorter than the requested acceptance workload: "
                f"requested {narration_seconds:.3f}s, got "
                f"{float(audio_probe.get('duration') or 0.0):.3f}s"
            )

        selected_videos = [Path(item) for item in manifest["media"]["videos"]]
        distinct_videos = {str(item.resolve()).casefold() for item in selected_videos}
        if multi and len(distinct_videos) < 4:
            raise AcceptanceError(
                f"Multi-source scenario used {len(distinct_videos)} distinct clips; expected at least 4."
            )
        if not multi and len(distinct_videos) != 1:
            raise AcceptanceError("Single-source scenario did not remain on one source clip.")
        if str(manifest["media"].get("bgm_mode")) != ("auto" if bgm else "none"):
            raise AcceptanceError("Manifest BGM mode does not match the scenario.")
        actual_encoder = str(manifest["media"].get("encoder") or "")
        if actual_encoder != encoder:
            raise AcceptanceError(
                "Acceptance did not exercise the requested encoder: "
                f"expected {encoder}, got {actual_encoder or '<missing>'}."
            )
        actual_command = _actual_render_command_encoder(
            job_dir, manifest.get("render_attempts")
        )
        command_encoder = str(actual_command["encoder"])
        if command_encoder != encoder:
            raise AcceptanceError(
                "The real FFmpeg command did not use the requested encoder: "
                f"expected {encoder}, got {command_encoder or '<missing>'} "
                f"in {actual_command['filename']}."
            )
        if command_encoder != actual_encoder:
            raise AcceptanceError(
                "Manifest encoder disagrees with the independently parsed "
                f"real FFmpeg command: {actual_encoder!r} != {command_encoder!r}."
            )
        if not bgm and manifest["media"].get("music"):
            raise AcceptanceError("No-BGM scenario unexpectedly selected a music track.")
        ending_card = manifest["media"].get("ending_card") or {}
        if bool(ending_card.get("cover_outro_enabled")) != bool(cover_outro_enabled):
            raise AcceptanceError("Manifest closing-cover state does not match the scenario.")

        subtitle_path = job_dir / ".work" / "subtitles.ass"
        subtitle_text = subtitle_path.read_text(encoding="utf-8-sig")
        ass_event_count = sum(
            1 for line in subtitle_text.splitlines() if line.startswith("Dialogue:")
        )
        if ass_event_count < int(minimum_ass_events) or "B73165" not in subtitle_text:
            raise AcceptanceError(
                "ASS subtitle workload is incomplete: "
                f"expected at least {minimum_ass_events} events and the search code, "
                f"got {ass_event_count} events."
            )
        command_text = str(actual_command["command_text"])
        if "subtitles.ass" not in command_text.replace("\\\\", "\\"):
            raise AcceptanceError("The real FFmpeg render command did not burn the ASS subtitles.")
        if first_frame_seconds is None:
            raise AcceptanceError(
                "The renderer completed without a truthful first-video-frame progress event."
            )

        result.update(
            {
                "ok": True,
                "video": str(video_path),
                "video_bytes": video_path.stat().st_size,
                "video_probe": video_probe,
                "mp3": str(audio_path),
                "mp3_bytes": audio_path.stat().st_size,
                "mp3_probe": audio_probe,
                "distinct_source_clips": len(distinct_videos),
                "bgm_mode": manifest["media"]["bgm_mode"],
                "encoder": actual_encoder,
                "actual_command_encoder": command_encoder,
                "actual_render_command": str(actual_command["path"]),
                "safe_serial_render": bool(manifest["media"].get("safe_serial_render")),
                "ass_event_count": ass_event_count,
                "subtitle_burn_command_verified": True,
            }
        )
    except Exception as error:  # noqa: BLE001 - acceptance must persist every failure
        error_log = job_dir / "render-error.log"
        result.update(
            {
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(limit=20),
                "render_error_log": str(error_log) if error_log.is_file() else "",
                "render_error_tail": (
                    error_log.read_text(encoding="utf-8", errors="replace")[-6000:]
                    if error_log.is_file()
                    else ""
                ),
            }
        )
    finally:
        sampler.stop()
        result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        result["first_frame_seconds"] = first_frame_seconds
        result["first_frame_measurement"] = "pipeline_ffmpeg_frame_progress"
        result["resources"] = sampler.report()
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline real-render acceptance test for StoryForge."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--quick",
        action="store_true",
        help="Run short packaging diagnostics only (default; never approves a stable release).",
    )
    mode.add_argument(
        "--stress",
        action="store_true",
        help="Run the employee-machine release gate with real long renders.",
    )
    parser.add_argument(
        "--stress-seconds",
        type=float,
        default=RELEASE_DEFAULT_STRESS_SECONDS,
        help=(
            "Duration of the 60 FPS sustained render. The stable-release gate "
            f"requires at least {RELEASE_DEFAULT_STRESS_SECONDS:.0f} seconds."
        ),
    )
    parser.add_argument("--root", type=Path, default=_default_root())
    parser.add_argument("--app-root", type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--ffprobe", type=Path)
    parser.add_argument(
        "--package-artifact",
        type=Path,
        help="ZIP or executable being approved; its SHA-256 is written to the report.",
    )
    parser.add_argument(
        "--encoder",
        choices=("libx264", "auto", "h264_nvenc", "h264_qsv", "h264_amf"),
        default="libx264",
        help="libx264 is the deterministic cross-machine acceptance default.",
    )
    parser.add_argument("--json-report", type=Path)
    return parser.parse_args(argv)


def _write_json_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _resource_rollup(results: list[dict[str, object]]) -> dict[str, object]:
    resource_reports = [
        item.get("resources")
        for item in results
        if isinstance(item.get("resources"), dict)
    ]

    def maximum(key: str) -> int | float | None:
        values = [
            value
            for report in resource_reports
            if isinstance((value := report.get(key)), (int, float))
        ]
        return max(values) if values else None

    def minimum(key: str) -> int | float | None:
        values = [
            value
            for report in resource_reports
            if isinstance((value := report.get(key)), (int, float))
        ]
        return min(values) if values else None

    return {
        "minimum_system_available_physical_bytes": minimum(
            "minimum_system_available_physical_bytes"
        ),
        "peak_system_commit_used_bytes": maximum("peak_system_commit_used_bytes"),
        "peak_system_commit_ratio": maximum("peak_system_commit_ratio"),
        "peak_acceptance_process_working_set_bytes": maximum(
            "peak_process_working_set_bytes"
        ),
        "peak_acceptance_process_private_bytes": maximum(
            "peak_process_private_bytes"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not math.isfinite(float(args.stress_seconds)) or args.stress_seconds <= 0:
        raise SystemExit("--stress-seconds must be a positive finite number")
    mode = "release_gate" if args.stress else "quick_diagnostic"
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    run_root = args.root.expanduser().resolve() / run_id
    report_path = (
        args.json_report.expanduser().resolve()
        if args.json_report
        else run_root / "acceptance-report.json"
    )
    started = time.perf_counter()
    report: dict[str, object] = {
        "schema_version": 3,
        "ok": False,
        "mode": mode,
        "acceptance_level": (
            "stable_release_gate" if args.stress else "diagnostic_only"
        ),
        "stable_release_eligible": False,
        "quick_is_not_stability_proof": not args.stress,
        "warning": (
            "QUICK ONLY: a pass verifies packaging and short pipeline wiring; "
            "it is not evidence that an employee machine is stable."
            if not args.stress
            else ""
        ),
        "run_root": str(run_root),
        "report": str(report_path),
        "storyforge_version": __version__,
        "code_under_test": (
            "frozen_executable_pipeline_runner"
            if bool(getattr(sys, "frozen", False))
            else "source_tree_pipeline_runner"
        ),
        "package_artifact_hash_is_identity_only": not bool(
            getattr(sys, "frozen", False)
        ),
        "runtime": _runtime_identity(),
        "source_revision": _source_revision(),
        "package": {},
        "host": {
            "platform": runtime_platform.platform(),
            "python": runtime_platform.python_version(),
            "machine": runtime_platform.machine(),
            "processor": runtime_platform.processor(),
            "logical_cpu_count": os.cpu_count(),
        },
        "encoder": args.encoder,
        "scenarios": [],
    }
    exit_code = 1
    try:
        run_root.mkdir(parents=True, exist_ok=False)
        report["package"] = _package_identity(args)
        package_artifact_bound = _explicit_package_artifact_is_bound(
            args, report["package"]
        )
        report["package_artifact_bound"] = package_artifact_bound
        ffmpeg, ffprobe = resolve_tools(args)
        report["tools"] = {
            "ffmpeg": str(ffmpeg),
            "ffmpeg_version": _ffmpeg_version(ffmpeg),
            "ffmpeg_sha256": _sha256_file(ffmpeg),
            "ffprobe": str(ffprobe),
            "ffprobe_sha256": _sha256_file(ffprobe),
        }

        if args.stress:
            sentence_count = RELEASE_MINIMUM_ASS_EVENTS + 20
            longest_seconds = max(RELEASE_EXACT_SECONDS, float(args.stress_seconds))
            scenario_specs = (
                {
                    "name": "exact-311s-30fps-outro-on",
                    "narration_seconds": RELEASE_EXACT_SECONDS,
                    "output_fps": 30,
                    "cover_outro_enabled": True,
                    "minimum_ass_events": RELEASE_MINIMUM_ASS_EVENTS,
                    "multi": True,
                    "bgm": True,
                },
                {
                    "name": "sustained-60fps-outro-off",
                    "narration_seconds": float(args.stress_seconds),
                    "output_fps": 60,
                    "cover_outro_enabled": False,
                    "minimum_ass_events": RELEASE_MINIMUM_ASS_EVENTS,
                    "multi": False,
                    "bgm": False,
                },
            )
        else:
            sentence_count = 8
            longest_seconds = 5.0
            scenario_specs = (
                {
                    "name": "quick-single-60fps-outro-on",
                    "narration_seconds": 5.0,
                    "output_fps": 60,
                    "cover_outro_enabled": True,
                    "minimum_ass_events": 1,
                    "multi": False,
                    "bgm": True,
                },
                {
                    "name": "quick-multi-30fps-outro-off",
                    "narration_seconds": 5.0,
                    "output_fps": 30,
                    "cover_outro_enabled": False,
                    "minimum_ass_events": 1,
                    "multi": True,
                    "bgm": False,
                },
            )

        inputs = prepare_inputs(
            ffmpeg,
            run_root,
            narration_seconds=longest_seconds,
            sentence_count=sentence_count,
        )
        results: list[dict[str, object]] = []
        for index, spec in enumerate(scenario_specs):
            result = render_scenario(
                root=run_root,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                inputs=inputs,
                encoder=args.encoder,
                **spec,
            )
            results.append(result)
            report["scenarios"] = results
            report["resources"] = _resource_rollup(results)
            report["elapsed_seconds"] = round(time.perf_counter() - started, 2)
            _write_json_report(report_path, report)
            if not result.get("ok"):
                for skipped in scenario_specs[index + 1 :]:
                    results.append(
                        {
                            "name": skipped["name"],
                            "ok": False,
                            "skipped": True,
                            "reason": "previous_release_scenario_failed",
                            "expected": skipped,
                        }
                    )
                break

        all_scenarios_passed = (
            len(results) == len(scenario_specs)
            and all(bool(item.get("ok")) for item in results)
        )
        full_duration_covered = (
            not args.stress
            or float(args.stress_seconds) >= RELEASE_DEFAULT_STRESS_SECONDS
        )
        report["stable_release_eligible"] = bool(
            args.stress
            and all_scenarios_passed
            and full_duration_covered
            and package_artifact_bound
        )
        # A quick diagnostic may pass without a package. A release-gate run is
        # successful only when every gate, including duration and exact
        # artifact binding, is satisfied.
        report["ok"] = bool(
            all_scenarios_passed
            and (not args.stress or report["stable_release_eligible"])
        )
        report["release_gate"] = {
            "exact_311_05_seconds_covered": bool(args.stress),
            "ten_minute_60fps_covered": bool(
                args.stress
                and float(args.stress_seconds) >= RELEASE_DEFAULT_STRESS_SECONDS
            ),
            "fps_30_and_60_covered": bool(args.stress),
            "cover_outro_on_and_off_covered": bool(args.stress),
            "ass_300_plus_covered": bool(args.stress),
            "explicit_package_artifact_bound": package_artifact_bound,
            "frozen_executable_pipeline_executed": bool(
                getattr(sys, "frozen", False)
            ),
            "running_executable_matches_package": bool(
                isinstance(report.get("package"), dict)
                and report["package"].get("runtime_entrypoint_matches") is True
            ),
        }
        report["verdict"] = (
            "stable_release_gate_passed"
            if report["stable_release_eligible"]
            else (
                "quick_diagnostic_passed_not_stability_proof"
                if all_scenarios_passed and not args.stress
                else (
                    "sustained_test_passed_but_ten_minute_gate_not_covered"
                    if all_scenarios_passed and args.stress and not full_duration_covered
                    else (
                        "release_gate_missing_package_artifact"
                        if all_scenarios_passed
                        and args.stress
                        and not package_artifact_bound
                        else "acceptance_failed"
                    )
                )
            )
        )
        exit_code = 0 if report["ok"] else 1
    except BaseException as error:  # report tool/input failures and Ctrl+C too
        report["error"] = f"{type(error).__name__}: {error}"
        report["verdict"] = "acceptance_interrupted" if isinstance(error, KeyboardInterrupt) else "acceptance_failed"
        exit_code = 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        report["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        try:
            _write_json_report(report_path, report)
        except OSError as report_error:
            _console_print(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"Could not persist acceptance report: {report_error}",
                        "intended_report": str(report_path),
                        "original": report,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 1
    stream = sys.stdout if report.get("ok") else sys.stderr
    _console_print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "verdict": report.get("verdict"),
                "stable_release_eligible": report.get("stable_release_eligible"),
                "storyforge_version": report.get("storyforge_version"),
                "elapsed_seconds": report.get("elapsed_seconds"),
                "report": str(report_path),
            },
            ensure_ascii=False,
        ),
        file=stream,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
