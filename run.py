from __future__ import annotations

import ctypes
import os
import platform
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path


def _startup_log_directory() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "StoryForgeStudio" / "logs"


def _write_startup_failure(error: BaseException) -> Path:
    """Persist failures that happen before the desktop window exists."""

    root = _startup_log_directory()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = root / f"startup-error-{stamp}.log"
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
    path.write_text(content, encoding="utf-8", newline="\n")
    (root / "startup-error-latest.log").write_text(
        content, encoding="utf-8", newline="\n"
    )
    return path


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
