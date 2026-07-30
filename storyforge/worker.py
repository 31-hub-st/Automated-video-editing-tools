from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from urllib.request import urlopen

from . import __version__
from .system import system_snapshot


LOCAL_WORKER_DEFAULT_PORT = 18765
LOCAL_WORKER_DISCOVERY_PORTS = tuple(range(18765, 18771))
LOCAL_WORKER_SESSION_SECONDS = 30 * 60
# This is the browser-to-local-worker contract, not the Hub RPC protocol.
# Both sides publish the oldest peer they still understand so rolling upgrades
# fail early with an actionable message instead of failing half way through a
# render request.
LOCAL_WORKER_PROTOCOL_VERSION = 2
LOCAL_WORKER_MIN_BROWSER_PROTOCOL_VERSION = 2
LOCAL_WORKER_TASK_NAME = "StoryForge Local Worker"


_TERMINAL_WORKER_JOB_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)


LOCAL_WORKER_RPC_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "queue_production_draft": ("drafts.create", "drafts.manage_all", "hub.manage"),
    "generate_voice_candidates": ("voice.preview", "hub.manage"),
    "set_local_tts_provider": ("voice.preview", "hub.manage"),
    "generate_intro_card_copy": ("text.assist", "hub.manage"),
    "start_queue": ("drafts.create", "drafts.manage_all", "hub.manage"),
    "cancel_queue": ("drafts.create", "drafts.manage_all", "hub.manage"),
    "get_jobs": ("drafts.create", "records.view_own", "records.view_all", "hub.manage"),
    "get_queue_connection": ("drafts.create", "records.view_own", "records.view_all", "hub.manage"),
    "get_archived_jobs": ("records.view_own", "records.view_all", "hub.manage"),
    "retry_failed": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "archive_job": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "restore_job": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "archive_batch": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "restore_batch": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "archive_finished_jobs": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    # The queue belongs to this workstation process, so a producer may tidy
    # its finished cards without receiving global Hub queue administration.
    "clear_finished_jobs": ("drafts.create", "drafts.manage_all", "hub.manage"),
    "get_record_artifacts": ("records.view_own", "records.view_all", "hub.manage"),
    "cancel_production_records": ("jobs.retry_own", "jobs.retry_all", "hub.manage"),
    "open_output_folder": ("drafts.create", "records.view_own", "records.view_all", "hub.manage"),
    "choose_folder": ("drafts.create", "hub.manage"),
    "worker_profile": ("drafts.create", "hub.manage"),
    "worker_runtime_snapshot": ("drafts.create", "hub.manage"),
    "worker_self_check": ("drafts.create", "hub.manage"),
    "worker_set_folders": ("drafts.create", "hub.manage"),
}


def discover_local_production_worker(
    *, timeout_seconds: float = 0.25
) -> dict[str, Any] | None:
    """Return the first live production worker on the fixed loopback range."""

    for port in LOCAL_WORKER_DISCOVERY_PORTS:
        try:
            with urlopen(
                f"http://127.0.0.1:{port}/worker/api/health",
                timeout=max(0.05, float(timeout_seconds)),
            ) as response:
                payload = json.loads(response.read(128 * 1024).decode("utf-8"))
            data = dict(payload.get("data") or {})
            if (
                bool(payload.get("ok"))
                and data.get("service") == "storyforge-local-worker"
                and data.get("worker_role") == "production-workstation"
            ):
                data["endpoint"] = f"http://127.0.0.1:{port}"
                return data
        except (OSError, TimeoutError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            continue
    return None


def ensure_local_worker_autostart(
    *,
    executable_path: str | Path | None = None,
    command_runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Register and start the current user's packaged background worker.

    The scheduled task contains only the executable path and ``--local-worker``
    launch mode.  Hub credentials remain in ``SettingsRepository`` where the
    device token is protected with Windows DPAPI; a member password is never
    passed to PowerShell, Task Scheduler, or a child process.

    Source checkouts deliberately do not install a Windows login task.  This
    keeps development/test runs from modifying the operator's machine and also
    prevents a task from depending on a disposable Python environment.
    """

    if os.name != "nt":
        return {
            "state": "not_supported",
            "automatic": False,
            "message": "后台制作服务会在 Windows 安装版中自动启用。",
        }
    if executable_path is None and not bool(getattr(sys, "frozen", False)):
        return {
            "state": "development",
            "automatic": False,
            "message": "源码运行模式不会修改 Windows 登录任务。",
        }

    executable = Path(executable_path or sys.executable).resolve(strict=True)
    release_script = executable.parent / "admin-tools" / "enable_storyforge_worker.ps1"
    legacy_release_script = executable.parent / "enable_storyforge_worker.ps1"
    source_script = Path(__file__).resolve().parents[1] / "scripts" / "enable_storyforge_worker.ps1"
    script = next(
        (
            candidate
            for candidate in (release_script, legacy_release_script, source_script)
            if candidate.is_file()
        ),
        release_script,
    )
    if not script.is_file():
        raise RuntimeError(
            "没有找到后台制作服务组件。请保留完整 StoryForge 安装文件夹后重试。"
        )

    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-ExecutablePath",
        str(executable),
        "-TaskName",
        LOCAL_WORKER_TASK_NAME,
        "-RequiredProtocolVersion",
        str(LOCAL_WORKER_PROTOCOL_VERSION),
        "-NoHealthWait",
        "-Quiet",
    ]
    try:
        completed = command_runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            shell=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            "Windows 没能自动启用后台制作服务；当前桌面仍可继续使用。"
        ) from error
    if int(getattr(completed, "returncode", 1)) != 0:
        detail = str(
            getattr(completed, "stderr", "")
            or getattr(completed, "stdout", "")
            or "Windows 登录任务创建失败"
        ).strip()
        raise RuntimeError(
            "Windows 没能自动启用后台制作服务；当前桌面仍可继续使用。"
            + (f" 原因：{detail[:240]}" if detail else "")
        )
    return {
        "state": "enabled",
        "automatic": True,
        "task_name": LOCAL_WORKER_TASK_NAME,
        "message": "后台制作服务已自动启用，关闭桌面后网页仍可在本机制作。",
    }


def pause_local_worker_autostart_for_desktop(
    *, command_runner: Any = subprocess.run
) -> dict[str, Any]:
    """Yield an existing scheduled worker before the full desktop starts.

    An idle worker yields so the full desktop can own the queue. A busy worker
    stays alive and exposes its own loopback UI instead, so opening StoryForge
    never interrupts FFmpeg/TTS or starts a second queue. After a successful
    idle handoff, the login-task wrapper waits for the desktop worker to exit
    before taking over again.
    """

    if os.name != "nt" or not bool(getattr(sys, "frozen", False)):
        return {"state": "not_required", "paused": False}

    # Never kill a production worker that owns unfinished work.  Stopping its
    # scheduled task while FFmpeg/TTS is active interrupts the current render;
    # stopping it while jobs are queued can also let the desktop restore the
    # same durable queue in a second process.  Older workers do not publish
    # these fields, so fail closed until they have been updated rather than
    # guessing that the queue is idle.
    running_before_stop = discover_local_production_worker()
    if running_before_stop is not None:
        rendering_busy = running_before_stop.get("rendering_busy")
        queue_busy = running_before_stop.get("queue_busy")
        if not isinstance(rendering_busy, bool) or not isinstance(queue_busy, bool):
            return {
                "state": "worker_state_unknown",
                "paused": False,
                "worker_running": True,
                "rendering_busy": False,
                "queue_busy": True,
                "message": (
                    "后台制作服务仍在运行，但当前版本无法确认队列是否空闲。"
                    "为避免中断任务，本次不会停止后台服务；请等待任务完成或先更新 StoryForge。"
                ),
                "technical": str(
                    running_before_stop.get("endpoint") or "worker state unavailable"
                ),
            }
        if rendering_busy or queue_busy:
            return {
                "state": "busy",
                "paused": False,
                "worker_running": True,
                "rendering_busy": rendering_busy,
                "queue_busy": queue_busy,
                "endpoint": str(running_before_stop.get("endpoint") or ""),
                "message": (
                    "这台电脑正在后台制作视频或队列中仍有任务。"
                    "后台任务会继续运行，StoryForge 将直接打开同一制作队列的查看页面。"
                ),
                "technical": str(
                    running_before_stop.get("endpoint") or "local worker busy"
                ),
            }
    script = (
        "$task=Get-ScheduledTask -TaskName 'StoryForge Local Worker' "
        "-ErrorAction SilentlyContinue;"
        "if($null -ne $task -and $task.State -eq 'Running'){"
        "Stop-ScheduledTask -TaskName 'StoryForge Local Worker';"
        "for($i=0;$i -lt 50;$i++){"
        "$task=Get-ScheduledTask -TaskName 'StoryForge Local Worker' "
        "-ErrorAction SilentlyContinue;"
        "if($null -eq $task -or $task.State -ne 'Running'){break};"
        "Start-Sleep -Milliseconds 100}}"
    )
    try:
        completed = command_runner(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "state": "warning",
            "paused": False,
            "worker_running": bool(discover_local_production_worker()),
            "message": "后台制作服务暂未切换到桌面窗口。",
            "technical": (str(error) or type(error).__name__)[:300],
        }
    if int(getattr(completed, "returncode", 1)) != 0:
        return {
            "state": "warning",
            "paused": False,
            "worker_running": bool(discover_local_production_worker()),
            "message": "后台制作服务暂未切换到桌面窗口。",
            "technical": str(
                getattr(completed, "stderr", "")
                or getattr(completed, "stdout", "")
                or "Windows task stop failed"
            )[:300],
        }
    deadline = time.monotonic() + 6.0
    running = discover_local_production_worker()
    while running is not None and time.monotonic() < deadline:
        time.sleep(0.1)
        running = discover_local_production_worker()
    if running is not None:
        return {
            "state": "warning",
            "paused": False,
            "worker_running": True,
            "message": "后台制作服务仍在退出，桌面没有启动第二套制作队列。",
            "technical": str(running.get("endpoint") or "local worker busy"),
        }
    return {"state": "paused", "paused": True, "worker_running": False}


def _origin(value: Any) -> str:
    parsed = urlsplit(str(value or "").strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("browser origin must be an HTTP(S) origin")
    return f"{parsed.scheme}://{parsed.netloc}".casefold()


def _folder_path(value: Any, label: str) -> Path:
    raw = str(value or "").strip()
    normalized = raw.replace("/", "\\")
    if (
        not raw
        or "\x00" in raw
        or normalized.startswith("\\\\")
        or normalized.startswith("\\??\\")
        or any(character in raw for character in ("*", "?"))
    ):
        raise ValueError(f"{label} must be a local absolute folder")
    path = Path(raw).expanduser()
    if not path.is_absolute() or path.parent == path:
        raise ValueError(f"{label} must be a non-root absolute folder")
    drive, tail = os.path.splitdrive(str(path))
    if os.name == "nt" and (not re.fullmatch(r"[A-Za-z]:", drive) or ":" in tail):
        raise ValueError(f"{label} cannot use a device path or alternate stream")
    return path.resolve(strict=False)


class LocalWorkerProfileStore:
    """Private, workstation-only media roots.

    These paths never enter the Hub draft.  They are deliberately kept beside
    this StoryForge installation's settings and can be changed by the employee
    from either the desktop app or an authenticated Hub browser.
    """

    KEYS = ("video_folder", "music_folder", "output_folder")

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / "local-worker.json"
        self._lock = threading.RLock()

    def _defaults(self) -> dict[str, str]:
        root = (self.data_dir / "worker-workspace").resolve()
        values = {
            "video_folder": str((root / "videos").resolve()),
            "music_folder": str((root / "music").resolve()),
            "output_folder": str((root / "output").resolve()),
        }
        for value in values.values():
            Path(value).mkdir(parents=True, exist_ok=True)
        return values

    def load(self) -> dict[str, str]:
        with self._lock:
            values = self._defaults()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                raw = {}
            if isinstance(raw, Mapping):
                for key in self.KEYS:
                    candidate = str(raw.get(key) or "").strip()
                    if not candidate:
                        continue
                    try:
                        values[key] = str(_folder_path(candidate, key))
                    except ValueError:
                        continue
            return values

    def resolve_output_target(self, value: Any = "") -> str:
        """Resolve a browser-requested output folder inside this workstation.

        Hub records may contain historical paths written by another computer.
        The local worker must therefore treat the requested path as untrusted,
        while still allowing an employee to open the exact batch directory
        beneath this machine's configured output root.
        """

        root = Path(self.load()["output_folder"]).resolve(strict=True)
        raw = str(value or "").strip()
        candidate = Path(raw).expanduser() if raw else root
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("输出文件夹不存在或无法访问。") from error
        if not resolved.is_dir():
            raise ValueError("输出路径不是文件夹。")
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise PermissionError(
                "该批次不属于当前电脑的输出目录，请到制作该批次的电脑打开。"
            ) from error
        return str(resolved)

    def save(self, value: Mapping[str, Any]) -> dict[str, str]:
        if not isinstance(value, Mapping):
            raise ValueError("worker folders must be an object")
        current = self.load()
        for key in self.KEYS:
            raw = str(value.get(key) or current[key]).strip()
            folder = _folder_path(raw, key)
            folder.mkdir(parents=True, exist_ok=True)
            if not folder.is_dir():
                raise ValueError(f"{key} is not a folder")
            current[key] = str(folder)
        body = json.dumps(current, ensure_ascii=False, indent=2) + "\n"
        temporary = self.path.with_suffix(".json.tmp")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(body, encoding="utf-8")
            os.replace(temporary, self.path)
        return dict(current)


@dataclass(slots=True)
class _WorkerSession:
    token: str
    actor_user_id: str
    device_id: str
    browser_origin: str
    browser_protocol_version: int
    minimum_worker_protocol_version: int
    negotiated_protocol_version: int
    permissions: frozenset[str]
    expires_at: float


@dataclass(slots=True)
class _WorkerMedia:
    id: str
    path: Path
    expires_at: float


class LocalWorkerGateway:
    """Small localhost contract used by a Hub-hosted browser page."""

    def __init__(self, api: Any) -> None:
        self.api = api
        self.profile = LocalWorkerProfileStore(api._repository.data_dir)
        self.nonce = secrets.token_urlsafe(32)
        self.started_at_unix = time.time()
        self._sessions: dict[str, _WorkerSession] = {}
        self._media: dict[str, _WorkerMedia] = {}
        self._lock = threading.RLock()

    @property
    def configured_device_id(self) -> str:
        return str(self.api._state.settings.hub.device_id or "")

    def _queue_activity(self) -> dict[str, Any]:
        """Return the small, non-secret queue state used for safe handoff."""

        queue = getattr(self.api, "_queue", None)
        if queue is None:
            return {
                "rendering_busy": False,
                "queue_busy": False,
                "unfinished_jobs": 0,
            }
        try:
            rendering_busy = bool(queue.is_rendering_busy())
            jobs = list(queue.list_jobs())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            # An unreadable queue is not safe to stop.  Publish a conservative
            # result so desktop startup leaves the existing worker untouched.
            return {
                "rendering_busy": True,
                "queue_busy": True,
                "unfinished_jobs": 0,
            }

        unfinished = 0
        for item in jobs:
            raw_status = (
                item.get("status")
                if isinstance(item, Mapping)
                else getattr(item, "status", "")
            )
            status = str(getattr(raw_status, "value", raw_status) or "").casefold()
            if status not in _TERMINAL_WORKER_JOB_STATUSES:
                unfinished += 1
        return {
            "rendering_busy": rendering_busy,
            "queue_busy": bool(rendering_busy or unfinished),
            "unfinished_jobs": unfinished,
        }

    def health(self) -> dict[str, Any]:
        hub_client = getattr(self.api, "_hub_client", None)
        hub_server = getattr(self.api, "_hub_server", None)
        runtime_mode = str(getattr(self.api, "_runtime_hub_mode", ""))
        hub_connected = bool(
            (runtime_mode == "client" and hub_client is not None)
            or (
                runtime_mode == "host"
                and hub_server is not None
                and bool(getattr(hub_server, "is_running", False))
            )
        )
        # A Hub host shares library/AI/records. It is deliberately not exposed
        # to a browser as an employee rendering workstation in this protocol.
        ready = bool(
            runtime_mode == "client"
            and hub_connected
            and self.configured_device_id
        )
        worker_role = (
            "production-workstation"
            if runtime_mode == "client"
            else "hub-only"
            if runtime_mode == "host"
            else "standalone"
        )
        return {
            "service": "storyforge-local-worker",
            "version": __version__,
            "protocol_version": LOCAL_WORKER_PROTOCOL_VERSION,
            "minimum_browser_protocol_version": LOCAL_WORKER_MIN_BROWSER_PROTOCOL_VERSION,
            "started_at_unix": self.started_at_unix,
            "ready": ready,
            "hub_connected": hub_connected,
            "worker_role": worker_role,
            "worker_nonce": self.nonce,
            "device_id": self.configured_device_id if runtime_mode == "client" else "",
            "device_name": str(self.api._state.settings.hub.device_name or ""),
            **self._queue_activity(),
            "capabilities": [
                "local-video",
                "local-music",
                "local-tts",
                "local-ffmpeg",
                "local-output",
            ],
        }

    @staticmethod
    def _folder_check(key: str, label: str, path: str, *, writable: bool) -> dict[str, Any]:
        folder = Path(path)
        exists = folder.is_dir()
        readable = bool(exists and os.access(folder, os.R_OK))
        write_ready = bool(exists and os.access(folder, os.W_OK)) if writable else None
        probe_error = ""
        if writable and write_ready:
            probe = folder / f".storyforge-write-check-{secrets.token_hex(6)}.tmp"
            try:
                probe.write_bytes(b"ok")
                probe.unlink()
            except OSError as error:
                write_ready = False
                probe_error = type(error).__name__
                try:
                    probe.unlink(missing_ok=True)
                except OSError:
                    pass
        ready = readable and (write_ready is not False)
        if not exists:
            summary = f"{label}不存在"
            fix = "在制作台重新选择这个文件夹。"
        elif not readable:
            summary = f"当前账号无法读取{label}"
            fix = "检查文件夹权限，或重新选择当前账号可以访问的文件夹。"
        elif writable and not write_ready:
            summary = "输出文件夹无法写入"
            fix = "关闭占用该目录的程序，或改用当前账号有写入权限的文件夹。"
        else:
            summary = f"{label}可用"
            fix = ""
        technical: dict[str, Any] = {
            "configured": bool(str(path).strip()),
            "exists": exists,
            "readable": readable,
        }
        if writable:
            technical["writable"] = bool(write_ready)
        if probe_error:
            technical["probe_error"] = probe_error
        return {
            "key": key,
            "label": label,
            "status": "ok" if ready else "error",
            "summary": summary,
            "fix": fix,
            "technical": technical,
        }

    def _hub_check(self) -> dict[str, Any]:
        runtime_mode = str(getattr(self.api, "_runtime_hub_mode", ""))
        connected = False
        detail = runtime_mode or "unknown"
        if runtime_mode == "host":
            server = getattr(self.api, "_hub_server", None)
            connected = bool(server is not None and getattr(server, "is_running", False))
            detail = "host-running" if connected else "host-stopped"
        elif runtime_mode == "client":
            client = getattr(self.api, "_hub_client", None)
            if client is not None:
                try:
                    response = client.health()
                    connected = bool(response.get("ok")) if isinstance(response, Mapping) else True
                    detail = "client-reachable" if connected else "client-unhealthy"
                except Exception as error:  # noqa: BLE001 - convert network failures into diagnostics
                    detail = type(error).__name__
        else:
            # A local desktop installation can render without a Hub. The
            # browser worker, however, is only considered connected in host or
            # client mode, so make that distinction explicit in diagnostics.
            connected = runtime_mode in {"local", "embedded"}
        return {
            "key": "hub",
            "label": "主电脑连接",
            "status": "ok" if connected else "error",
            "summary": "已连接主电脑" if connected else "无法连接主电脑",
            "fix": "" if connected else "确认主电脑 StoryForge 正在运行，并检查局域网和主电脑地址。",
            "technical": {"runtime_mode": runtime_mode, "state": detail},
        }

    def runtime_snapshot(self) -> dict[str, Any]:
        """Return a non-secret capability projection for this workstation.

        A Hub-hosted browser renders shared library and account state from the
        Hub, while speech synthesis and video rendering run on the employee
        computer. Returning a deliberately small projection prevents the
        browser from mistaking the Hub computer's TTS/FFmpeg runtime for the
        local worker's. Endpoints, commands, API keys and executable paths stay
        private.
        """

        settings = getattr(getattr(self.api, "_state", None), "settings", None)
        providers = getattr(settings, "providers", None)
        local_system = system_snapshot()
        provider = str(getattr(providers, "tts_provider", "") or "local_kokoro")
        provider = provider.strip().casefold().replace("-", "_")
        edge_ready = bool(local_system.get("edge_tts_runtime_ready"))
        embedded_ready = bool(local_system.get("embedded_kokoro_ready"))
        kokoro_configured = bool(
            str(getattr(providers, "kokoro_endpoint", "") or "").strip()
            or str(getattr(providers, "kokoro_command", "") or "").strip()
        )
        api_key_configured = bool(
            str(getattr(providers, "tts_api_key", "") or "").strip()
        )
        endpoint_configured = bool(
            str(getattr(providers, "tts_endpoint", "") or "").strip()
        )
        if provider in {
            "edge",
            "edge_tts",
            "microsoft_edge",
            "microsoft_edge_tts",
        }:
            ready = edge_ready
        elif provider in {
            "local",
            "kokoro",
            "local_kokoro",
            "kokoro_local",
            "kokoro_http",
            "kokoro_cli",
        }:
            ready = embedded_ready or kokoro_configured
        else:
            ready = api_key_configured
        encoders = [
            str(item)
            for item in list(local_system.get("encoders") or [])
            if str(item) in {"h264_nvenc", "h264_qsv", "h264_amf", "libx264"}
        ]
        recommended_encoder = str(local_system.get("recommended_encoder") or "")
        if recommended_encoder not in encoders:
            recommended_encoder = encoders[0] if encoders else ""
        ffmpeg_ready = bool(local_system.get("ffmpeg_ready"))
        provider_label = {
            "edge": "Edge TTS",
            "edge_tts": "Edge TTS",
            "microsoft_edge": "Edge TTS",
            "microsoft_edge_tts": "Edge TTS",
            "local": "Kokoro 本地配音",
            "kokoro": "Kokoro 本地配音",
            "local_kokoro": "Kokoro 本地配音",
            "kokoro_local": "Kokoro 本地配音",
            "kokoro_http": "Kokoro 本地配音",
            "kokoro_cli": "Kokoro 本地配音",
            "deepgram": "Deepgram Aura",
            "deepgram_aura": "Deepgram Aura",
            "aura": "Deepgram Aura",
            "aura_2": "Deepgram Aura",
        }.get(provider, provider or "配音服务")
        if embedded_ready:
            tts_mode = "embedded"
        elif str(getattr(providers, "kokoro_endpoint", "") or "").strip():
            tts_mode = "http"
        elif str(getattr(providers, "kokoro_command", "") or "").strip():
            tts_mode = "command"
        elif provider in {"edge", "edge_tts", "microsoft_edge", "microsoft_edge_tts"}:
            tts_mode = "online-client"
        elif api_key_configured:
            tts_mode = "cloud"
        else:
            tts_mode = "unavailable"
        return {
            "app_version": __version__,
            "worker_protocol_version": LOCAL_WORKER_PROTOCOL_VERSION,
            "minimum_browser_protocol_version": LOCAL_WORKER_MIN_BROWSER_PROTOCOL_VERSION,
            "ffmpeg_ready": ffmpeg_ready,
            "ffmpeg_label": "FFmpeg" if ffmpeg_ready else "未检测到 FFmpeg",
            "encoders": encoders,
            "recommended_encoder": recommended_encoder,
            "tts_provider": provider,
            "tts_label": provider_label,
            "tts_mode": tts_mode,
            "tts_ready": ready,
            "edge_tts_runtime_ready": edge_ready,
            "embedded_kokoro_ready": embedded_ready,
            "kokoro_configured": kokoro_configured,
            "tts_endpoint_configured": endpoint_configured,
            "tts_api_key_configured": api_key_configured,
        }

    def self_check(self) -> dict[str, Any]:
        """Return one safe, actionable readiness report for this workstation."""

        runtime = self.runtime_snapshot()
        folders = self.profile.load()
        checks: list[dict[str, Any]] = []

        checks.append(
            {
                "key": "worker",
                "label": "本机制作服务",
                "status": "ok",
                "summary": f"Worker {__version__}，协议 {LOCAL_WORKER_PROTOCOL_VERSION}",
                "fix": "",
                "technical": {
                    "app_version": __version__,
                    "protocol_version": LOCAL_WORKER_PROTOCOL_VERSION,
                    "minimum_browser_protocol_version": LOCAL_WORKER_MIN_BROWSER_PROTOCOL_VERSION,
                },
            }
        )
        ffmpeg_ready = bool(runtime["ffmpeg_ready"])
        checks.append(
            {
                "key": "ffmpeg",
                "label": "FFmpeg",
                "status": "ok" if ffmpeg_ready else "error",
                "summary": "FFmpeg 可用" if ffmpeg_ready else "未检测到 FFmpeg",
                "fix": "" if ffmpeg_ready else (
                    "请重新解压完整 StoryForge 发布文件夹，不要只复制 EXE；"
                    "仍未恢复时再运行安装目录中的诊断工具。"
                ),
                "technical": {"ready": ffmpeg_ready},
            }
        )
        encoders = list(runtime.get("encoders") or [])
        checks.append(
            {
                "key": "encoder",
                "label": "H.264 编码器",
                "status": "ok" if encoders else "error",
                "summary": " / ".join(encoders) if encoders else "没有可用的 H.264 编码器",
                "fix": "" if encoders else (
                    "请重新解压完整发布文件夹；若 FFmpeg 已存在，再更新显卡驱动后重新自检。"
                ),
                "technical": {
                    "encoders": encoders,
                    "recommended": str(runtime.get("recommended_encoder") or ""),
                },
            }
        )
        tts_ready = bool(runtime["tts_ready"])
        provider = str(runtime["tts_provider"])
        provider_label = str(runtime.get("tts_label") or provider or "配音服务")
        if tts_ready and provider in {
            "edge",
            "edge_tts",
            "microsoft_edge",
            "microsoft_edge_tts",
        }:
            tts_summary = "Edge TTS 组件可用（生成配音时需要联网）"
        elif tts_ready and str(runtime.get("tts_mode") or "") in {"http", "command"}:
            tts_summary = f"{provider_label}已配置（首次试听时验证服务）"
        elif tts_ready:
            tts_summary = f"{provider_label}可用"
        else:
            tts_summary = f"{provider_label}尚未就绪"
        if tts_ready:
            tts_fix = ""
        elif provider in {
            "local",
            "kokoro",
            "local_kokoro",
            "kokoro_local",
            "kokoro_http",
            "kokoro_cli",
        }:
            tts_fix = (
                "当前电脑未检测到 Kokoro 组件或服务。可先切换到 Edge TTS，"
                "或安装完整版并重新自检。"
            )
        elif provider in {
            "edge",
            "edge_tts",
            "microsoft_edge",
            "microsoft_edge_tts",
        }:
            tts_fix = "当前安装缺少 Edge TTS 组件，请重新解压完整发布文件夹后再试。"
        else:
            tts_fix = "请在当前制作电脑配置该配音服务的 API Key，再重新自检。"
        checks.append(
            {
                "key": "tts",
                "label": "配音服务",
                "status": "ok" if tts_ready else "error",
                "summary": tts_summary,
                "fix": tts_fix,
                "technical": {
                    "provider": runtime["tts_provider"],
                    "edge_tts_ready": runtime["edge_tts_runtime_ready"],
                    "embedded_kokoro_ready": runtime["embedded_kokoro_ready"],
                    "kokoro_configured": runtime["kokoro_configured"],
                },
            }
        )
        checks.extend(
            [
                self._folder_check("video_folder", "视频素材文件夹", folders["video_folder"], writable=False),
                self._folder_check("music_folder", "音乐文件夹", folders["music_folder"], writable=False),
                self._folder_check("output_folder", "输出文件夹", folders["output_folder"], writable=True),
            ]
        )
        try:
            disk = shutil.disk_usage(folders["output_folder"])
            free_gb = round(disk.free / (1024**3), 1)
            disk_status = "error" if free_gb < 2 else "warning" if free_gb < 10 else "ok"
            checks.append(
                {
                    "key": "disk",
                    "label": "输出磁盘空间",
                    "status": disk_status,
                    "summary": f"剩余 {free_gb:.1f} GB",
                    "fix": "清理输出磁盘，建议至少保留 10 GB。" if disk_status != "ok" else "",
                    "technical": {"free_bytes": int(disk.free), "total_bytes": int(disk.total)},
                }
            )
        except OSError as error:
            checks.append(
                {
                    "key": "disk",
                    "label": "输出磁盘空间",
                    "status": "error",
                    "summary": "无法读取输出磁盘空间",
                    "fix": "重新选择可访问的输出文件夹。",
                    "technical": {"probe_error": type(error).__name__},
                }
            )
        checks.append(self._hub_check())
        has_error = any(item["status"] == "error" for item in checks)
        has_warning = any(item["status"] == "warning" for item in checks)
        return {
            "ready": not has_error,
            "status": "error" if has_error else "warning" if has_warning else "ready",
            "summary": (
                "存在需要处理的问题"
                if has_error
                else "可以制作，但有一项需要留意"
                if has_warning
                else "当前制作电脑可以正常工作"
            ),
            "checked_at_unix": time.time(),
            "checks": checks,
            "runtime": runtime,
        }

    @staticmethod
    def _protocol_version(value: Any, label: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{label} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label} must be an integer") from error
        if not 1 <= parsed <= 100:
            raise ValueError(f"{label} is unsupported")
        return parsed

    def connect(
        self,
        ticket: Any,
        browser_origin: Any,
        *,
        browser_protocol_version: Any,
        minimum_worker_protocol_version: Any,
    ) -> dict[str, Any]:
        origin = _origin(browser_origin)
        # Validate both advertised compatibility ranges before redeeming the
        # one-use Hub ticket.  This prevents a stale browser from consuming a
        # valid ticket and then failing only after it has begun issuing V0.3
        # RPC payloads to a V0.4 worker.
        browser_protocol = self._protocol_version(
            browser_protocol_version, "browser_protocol_version"
        )
        minimum_worker_protocol = self._protocol_version(
            minimum_worker_protocol_version, "minimum_worker_protocol_version"
        )
        if browser_protocol < LOCAL_WORKER_MIN_BROWSER_PROTOCOL_VERSION:
            raise ValueError(
                "browser protocol is too old for this local worker; update the Hub page"
            )
        if LOCAL_WORKER_PROTOCOL_VERSION < minimum_worker_protocol:
            raise ValueError(
                "local worker protocol is too old for this browser; update StoryForge on this computer"
            )
        negotiated_protocol = min(
            browser_protocol, LOCAL_WORKER_PROTOCOL_VERSION
        )
        client = getattr(self.api, "_hub_client", None)
        runtime_mode = str(getattr(self.api, "_runtime_hub_mode", ""))
        if runtime_mode == "client" and client is not None:
            redeemed = client.redeem_local_worker_ticket(
                str(ticket or ""),
                worker_nonce=self.nonce,
                browser_origin=origin,
            )
        elif runtime_mode == "host":
            raise PermissionError(
                "当前是 Hub 主电脑，只保存资料、AI 和制作记录；请在员工制作电脑启用本机制作服务。"
            )
        else:
            raise PermissionError("当前电脑尚未连接 Hub。请先登录员工账号并绑定这台电脑。")
        if str(redeemed.get("device_id") or "") != self.configured_device_id:
            raise PermissionError("主电脑授权的不是当前制作电脑，请退出网页账号后重新登录。")
        expires_at = time.time() + LOCAL_WORKER_SESSION_SECONDS
        try:
            parsed_expiry = datetime.fromisoformat(str(redeemed.get("expires_at") or ""))
            expires_at = min(expires_at, parsed_expiry.timestamp())
        except (TypeError, ValueError):
            pass
        session = _WorkerSession(
            token=secrets.token_urlsafe(40),
            actor_user_id=str(redeemed.get("actor_user_id") or ""),
            device_id=self.configured_device_id,
            browser_origin=origin,
            browser_protocol_version=browser_protocol,
            minimum_worker_protocol_version=minimum_worker_protocol,
            negotiated_protocol_version=negotiated_protocol,
            permissions=frozenset(str(item) for item in redeemed.get("permissions") or []),
            expires_at=expires_at,
        )
        if not session.actor_user_id:
            raise PermissionError("主电脑没有返回有效员工账号，请管理员检查该账号是否已启用。")
        with self._lock:
            self._prune_locked()
            self._sessions[session.token] = session
        diagnostics = self.self_check()
        return {
            "session_token": session.token,
            "expires_at": datetime.fromtimestamp(expires_at).astimezone().isoformat(),
            "device_id": session.device_id,
            "device_name": str(redeemed.get("device_name") or ""),
            "worker_protocol_version": LOCAL_WORKER_PROTOCOL_VERSION,
            "browser_protocol_version": browser_protocol,
            "negotiated_protocol_version": negotiated_protocol,
            "folders": self.profile.load(),
            "capabilities": self.health()["capabilities"],
            "runtime": diagnostics["runtime"],
            "self_check": diagnostics,
        }

    def _prune_locked(self) -> None:
        now = time.time()
        self._sessions = {
            key: value for key, value in self._sessions.items() if value.expires_at > now
        }
        self._media = {
            key: value for key, value in self._media.items() if value.expires_at > now
        }

    def session(self, token: Any, browser_origin: Any) -> _WorkerSession:
        clean_token = str(token or "")
        origin = _origin(browser_origin)
        with self._lock:
            self._prune_locked()
            value = self._sessions.get(clean_token)
        if value is None or not secrets.compare_digest(value.browser_origin, origin):
            raise PermissionError("本机制作服务连接已过期，网页会自动重新连接；请稍后重试。")
        return value

    @staticmethod
    def _authorized(session: _WorkerSession, method: str) -> None:
        required = LOCAL_WORKER_RPC_PERMISSIONS.get(method)
        if required is None:
            raise PermissionError("当前版本的本机制作服务不支持这项操作，请更新员工电脑。")
        if required and not set(required).intersection(session.permissions):
            raise PermissionError("当前员工账号没有这项制作权限，请管理员检查账号是否已启用。")

    def _register_media(self, path: Path, expires_at: float) -> str:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("media file does not exist")
        with self._lock:
            self._prune_locked()
            for item in self._media.values():
                if item.path == resolved and item.expires_at > time.time():
                    return item.id
            ref = secrets.token_urlsafe(32)
            self._media[ref] = _WorkerMedia(ref, resolved, expires_at)
            return ref

    def media(self, reference: Any) -> Path:
        ref = str(reference or "")
        with self._lock:
            self._prune_locked()
            item = self._media.get(ref)
        if item is None:
            raise FileNotFoundError("本机试听或成片链接已过期，请刷新页面后重新打开。")
        return item.path.resolve(strict=True)

    def _browser_value(self, value: Any, expires_at: float) -> Any:
        if isinstance(value, dict):
            result = {
                str(key): self._browser_value(item, expires_at)
                for key, item in value.items()
            }
            audio_path = str(result.get("audio_path") or "")
            if audio_path:
                try:
                    ref = self._register_media(Path(audio_path), expires_at)
                    result["audio_uri"] = f"/worker/api/media?ref={ref}"
                except (OSError, RuntimeError, ValueError):
                    pass
            output_file = str(result.get("output_file") or "")
            if output_file:
                try:
                    ref = self._register_media(Path(output_file), expires_at)
                    result["output_uri"] = f"/worker/api/media?ref={ref}"
                except (OSError, RuntimeError, ValueError):
                    pass
            return result
        if isinstance(value, (list, tuple)):
            return [self._browser_value(item, expires_at) for item in value]
        return value

    def rpc(
        self,
        session_token: Any,
        browser_origin: Any,
        method: Any,
        args: Any,
    ) -> dict[str, Any]:
        session = self.session(session_token, browser_origin)
        clean_method = str(method or "").strip()
        if not isinstance(args, list) or len(args) > 12:
            raise ValueError("本机制作请求格式不正确，请刷新网页后重试。")
        self._authorized(session, clean_method)

        if clean_method == "worker_profile":
            result: Any = {
                "folders": self.profile.load(),
                "health": self.health(),
                "runtime": self.runtime_snapshot(),
            }
        elif clean_method == "worker_runtime_snapshot":
            result = self.runtime_snapshot()
        elif clean_method == "worker_self_check":
            result = self.self_check()
        elif clean_method == "worker_set_folders":
            result = {"folders": self.profile.save(dict(args[0] if args else {}))}
        elif clean_method == "choose_folder":
            key = str(args[0] if args else "")
            if key not in self.profile.KEYS:
                raise ValueError("无法识别要选择的本机文件夹。")
            response = self.api.choose_folder(key)
            if not bool(response.get("ok")):
                return response
            selected = str(response.get("data") or "")
            if selected:
                current = self.profile.load()
                current[key] = selected
                self.profile.save(current)
            result = selected
        else:
            prepared = list(args)
            if clean_method == "queue_production_draft":
                payload = dict(prepared[0] if prepared else {})
                supplied = {
                    key: payload.get(key)
                    for key in self.profile.KEYS
                    if str(payload.get(key) or "").strip()
                }
                folders = self.profile.save(supplied) if supplied else self.profile.load()
                payload.update(folders)
                prepared = [payload]
            elif clean_method in {"retry_failed", "restore_job"}:
                folders = self.profile.load()
                prepared = [prepared[0], folders]
            elif clean_method == "open_output_folder":
                # Shared records can request a precise batch directory, but it
                # must remain inside this workstation's private output root.
                prepared = [
                    self.profile.resolve_output_target(
                        prepared[0] if prepared else ""
                    )
                ]

            target = getattr(self.api, clean_method, None)
            if not callable(target):
                raise PermissionError("员工电脑版本过旧，缺少这项制作功能，请更新后重新启用本机制作服务。")
            actor_scope = getattr(self.api, "_web_actor_scope", None)
            scope = actor_scope(session.actor_user_id) if callable(actor_scope) else nullcontext()
            with scope:
                response = target(*prepared)
            if not isinstance(response, dict) or "ok" not in response:
                raise RuntimeError("本机制作服务返回异常，请重新自检；若仍失败请把技术详情交给管理员。")
            return self._browser_value(response, session.expires_at)

        return {"ok": True, "data": self._browser_value(result, session.expires_at)}


__all__ = [
    "LOCAL_WORKER_DEFAULT_PORT",
    "LOCAL_WORKER_DISCOVERY_PORTS",
    "LOCAL_WORKER_MIN_BROWSER_PROTOCOL_VERSION",
    "LOCAL_WORKER_PROTOCOL_VERSION",
    "LOCAL_WORKER_RPC_PERMISSIONS",
    "LOCAL_WORKER_TASK_NAME",
    "LocalWorkerGateway",
    "LocalWorkerProfileStore",
    "discover_local_production_worker",
    "ensure_local_worker_autostart",
    "pause_local_worker_autostart_for_desktop",
]
