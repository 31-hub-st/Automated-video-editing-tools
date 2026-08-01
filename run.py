from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path


_UPDATE_INSTALLING_GRACE_SECONDS = 10 * 60


def _early_storyforge_data_root() -> Path | None:
    """Resolve the employee data root without importing mutable app modules."""

    configured = str(os.environ.get("STORYFORGE_DATA_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    if bool(getattr(sys, "frozen", False)):
        return Path(sys.executable).resolve(strict=False).parent / "StoryForgeData"
    return None


def _update_install_mutex_owned(install_root: Path) -> bool:
    """Return whether the external updater owns this installation's mutex."""

    if os.name != "nt":
        return False
    identity = hashlib.sha256(
        str(install_root.resolve(strict=False)).lower().encode("utf-8")
    ).hexdigest().upper()
    name = f"Local\\StoryForgeUpdate-{identity}"
    synchronize = 0x00100000
    mutex_modify_state = 0x00000001
    wait_object_0 = 0x00000000
    wait_abandoned = 0x00000080
    wait_timeout = 0x00000102
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        # The fresh marker remains the fail-closed signal if a restricted
        # Windows environment refuses the optional named-mutex inspection.
        return False
    kernel32.OpenMutexW.argtypes = (
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_wchar_p,
    )
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
    kernel32.ReleaseMutex.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenMutexW(synchronize | mutex_modify_state, 0, name)
    if not handle:
        return False
    try:
        outcome = int(kernel32.WaitForSingleObject(handle, 0))
        if outcome == wait_timeout:
            return True
        if outcome in {wait_object_0, wait_abandoned}:
            kernel32.ReleaseMutex(handle)
        return False
    finally:
        kernel32.CloseHandle(handle)


def _active_update_installation(
    argv: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object] | None:
    """Inspect the shared marker before importing any updateable module."""

    data_root = _early_storyforge_data_root()
    if data_root is None:
        return None
    marker_path = data_root / "updates" / "pending-update.json"
    marker: dict[str, object] = {}
    try:
        loaded = json.loads(marker_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            marker = loaded
    except (OSError, json.JSONDecodeError, ValueError):
        marker = {}

    install_root_value = str(marker.get("install_root") or "").strip()
    install_root = (
        Path(install_root_value).resolve(strict=False)
        if install_root_value
        else Path(sys.executable).resolve(strict=False).parent
    )
    mutex_owned = _update_install_mutex_owned(install_root)
    installing = bool(marker.get("installing"))
    if installing:
        raw_time = str(marker.get("installing_at") or "").strip()
        try:
            installed_at = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if installed_at.tzinfo is None:
                installed_at = installed_at.replace(tzinfo=UTC)
            marker_is_fresh = (
                datetime.now(UTC) - installed_at.astimezone(UTC)
            ).total_seconds() <= _UPDATE_INSTALLING_GRACE_SECONDS
        except (TypeError, ValueError):
            marker_is_fresh = False
    else:
        marker_is_fresh = False
    if not mutex_owned and not marker_is_fresh:
        return None

    # The installer launches exactly one updated desktop for its health check.
    # Its unguessable inherited token is the only process allowed through while
    # live files remain protected by the installation mutex.
    phase = str(marker.get("installing_phase") or "").strip().casefold()
    installation_id = str(marker.get("installation_id") or "").strip()
    if not phase and str(marker.get("apply_work_root") or "").strip():
        # Releases older than this guard wrote ``installing=true`` only after
        # every live file had been copied, immediately before launching the
        # updated desktop health check. Preserve that one-time upgrade path;
        # new installers always publish an explicit phase before copying.
        return None
    health_token = str(
        os.environ.get("STORYFORGE_UPDATE_HEALTH_TOKEN") or ""
    ).strip()
    arguments = {str(item).strip().casefold() for item in (argv or ())}
    if phase in {"health_check", "rollback_health"} and "--local-worker" in arguments:
        # The scheduled local worker does not inherit the installer's private
        # desktop health token. It is allowed only after live-file copying has
        # finished (or after rollback has restored the old release).
        return None
    if (
        phase == "health_check"
        and installation_id
        and health_token
        and health_token == installation_id
    ):
        return None
    return {
        "marker_path": str(marker_path),
        "phase": phase or "installing",
        "mutex_owned": mutex_owned,
    }


def _enforce_update_installation_idle(
    argv: list[str] | tuple[str, ...] | None = None,
) -> None:
    active = _active_update_installation(argv)
    if active is None:
        return
    raise SystemExit(
        "StoryForge 正在完成软件更新，暂时不会启动第二个程序。"
        "请等待约一分钟后重新打开；现有文件和制作记录不会丢失。"
    )


def _startup_log_directory() -> Path:
    configured = str(os.environ.get("STORYFORGE_DATA_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve(strict=False) / "logs"
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "StoryForgeStudio" / "logs"


def _write_startup_failure(error: BaseException) -> Path:
    """Persist failures that happen before the desktop window exists."""

    root = _startup_log_directory()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    lines = [
        "StoryForge Studio startup failure",
        f"time_utc: {datetime.now(UTC).isoformat()}",
        f"executable: {sys.executable}",
        f"frozen: {bool(getattr(sys, 'frozen', False))}",
        f"python: {sys.version}",
        f"platform: {platform.platform()}",
        f"working_directory: {Path.cwd()}",
        f"arguments: {sys.argv!r}",
        f"error_type: {type(error).__name__}",
        f"error: {str(error) or type(error).__name__}",
        "",
        "traceback:",
        "".join(traceback.format_exception(type(error), error, error.__traceback__)),
    ]
    content = "\n".join(lines).rstrip() + "\n"
    candidates = [root]
    local = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    fallback = (
        Path(local) if local else Path.home() / "AppData" / "Local"
    ) / "StoryForgeStudio" / "logs"
    if fallback.resolve(strict=False) != root.resolve(strict=False):
        candidates.append(fallback)
    last_error: OSError | None = None
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            path = candidate / f"startup-error-{stamp}.log"
            path.write_text(content, encoding="utf-8", newline="\n")
            (candidate / "startup-error-latest.log").write_text(
                content, encoding="utf-8", newline="\n"
            )
            return path
        except OSError as write_error:
            last_error = write_error
    assert last_error is not None
    raise last_error


def _show_startup_failure(error: BaseException, log_path: Path) -> None:
    message = (
        "StoryForge 启动失败。\n\n"
        f"{type(error).__name__}: {str(error) or type(error).__name__}\n\n"
        f"诊断日志已保存到：\n{log_path}\n\n"
        "把这个日志文件发给管理员即可定位问题。"
    )
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                message,
                "StoryForge Studio - 启动失败",
                0x10 | 0x10000,
            )
            return
        except BaseException:
            pass
    print(message, file=sys.stderr, flush=True)


def _run() -> int:
    # The updater can replace Python/native modules in place. Refuse ordinary
    # launches before importing even ``storyforge.portable`` while its marker
    # or named mutex says the installation is inside that copy window.
    _enforce_update_installation_idle(sys.argv[1:])

    # This must run before importing the application, pywebview, TTS or any
    # module which asks Python/Windows for a cache or temporary directory.
    from storyforge.portable import configure_runtime_environment

    data_root = configure_runtime_environment(sys.argv[1:])
    # Optional language/voice packs live outside the immutable application
    # bundle. Activate only hash-verified releases before either the desktop or
    # disposable Kokoro child imports its runtime dependencies. No component
    # installer script is executed here.
    try:
        from storyforge.component_updater import (
            ComponentPackageError,
            activate_component_runtime_from_environment,
        )

        activate_component_runtime_from_environment(data_root)
    except (ComponentPackageError, OSError):
        # A damaged optional pack must not prevent StoryForge from starting.
        # The component status/API reports the unusable pack and permits a
        # verified reinstall or rollback.
        pass
    if "--storyforge-kokoro-child" in sys.argv:
        child_index = sys.argv.index("--storyforge-kokoro-child")
        child_args = sys.argv[child_index + 1 :]
        from storyforge.kokoro_child import main as kokoro_child_main

        return int(kokoro_child_main(child_args))
    if "--storyforge-stability-acceptance" in sys.argv:
        acceptance_index = sys.argv.index("--storyforge-stability-acceptance")
        acceptance_args = sys.argv[acceptance_index + 1 :]
        # Keep this a static import so PyInstaller includes the exact release
        # gate in the frozen application.  Running the gate through the EXE
        # proves the packaged PipelineRunner, media graph and dependencies,
        # rather than a source checkout that merely happens to sit beside it.
        from scripts.stability_render_acceptance import main as acceptance_main

        return int(acceptance_main(acceptance_args))
    if bool(getattr(sys, "frozen", False)) and os.name == "nt":
        # The desktop shell is a form UI and does not benefit from GPU
        # compositing. Keeping WebView2 off the GPU avoids competing with
        # Kokoro/FFmpeg and prevents a display-driver reset from turning the
        # StoryForge window black while the background render keeps running.
        browser_arguments = str(
            os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS") or ""
        ).strip()
        if "--disable-gpu" not in browser_arguments.split():
            os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
                f"{browser_arguments} --disable-gpu".strip()
            )
        if data_root is not None:
            from storyforge.windows_bootstrap import unblock_frozen_installation

            unblock_frozen_installation(data_root)
    from storyforge.main import main

    return int(main() or 0)


if __name__ == "__main__":
    try:
        raise SystemExit(_run())
    except SystemExit as error:
        if error.code in (None, 0) or isinstance(error.code, int):
            raise
        # ``SystemExit('message')`` is an application startup failure.  A
        # windowed PyInstaller executable otherwise discards that message.
        log = _write_startup_failure(error)
        _show_startup_failure(error, log)
        raise SystemExit(1) from error
    except BaseException as error:
        log = _write_startup_failure(error)
        _show_startup_failure(error, log)
        raise SystemExit(1) from error
