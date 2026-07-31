from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit


_PROCESS_WORKER_MUTEX: object | None = None


def resource_path(relative: str) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return (bundle_root / relative).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="StoryForge Studio")
    parser.add_argument("--debug", action="store_true", help="Enable the webview debug tools")
    parser.add_argument(
        "--kokoro-self-test",
        metavar="OUTPUT_DIRECTORY",
        help="Synthesize one packaged Kokoro WAV, write a JSON result, and exit",
    )
    parser.add_argument(
        "--startup-self-test",
        metavar="OUTPUT_DIRECTORY",
        help=(
            "Initialize the packaged database, UI, FFmpeg, WebView2 bridge and "
            "localhost worker; write a JSON result and exit"
        ),
    )
    runtime = parser.add_mutually_exclusive_group()
    runtime.add_argument(
        "--web",
        "--web-only",
        dest="web_only",
        action="store_true",
        help="Run the configured Hub host and browser UI without a desktop window",
    )
    runtime.add_argument(
        "--local-worker",
        action="store_true",
        help=(
            "Run only this enrolled employee computer's loopback media worker "
            "without opening the desktop window"
        ),
    )
    parser.add_argument(
        "--web-host",
        metavar="ADDRESS",
        help="Temporarily override the configured Hub listen address for this run",
    )
    parser.add_argument(
        "--web-port",
        metavar="PORT",
        type=int,
        help="Temporarily override the configured Hub/web port for this run",
    )
    return parser


def _wait_for_stop(stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is not None:
            signal.signal(signal_value, request_stop)
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass


def _run_local_worker_service(api: object, ui_root: Path) -> dict[str, object]:
    """Keep an enrolled employee workstation available to a Hub web page."""

    if str(getattr(api, "_runtime_hub_mode", "")) != "client":
        raise RuntimeError(
            "本机制作服务只用于已连接 Hub 的员工电脑；Hub 主机请使用网页后台服务。"
        )
    state = getattr(api, "_state", None)
    settings = getattr(state, "settings", None)
    hub = getattr(settings, "hub", None)
    if not str(getattr(hub, "access_token", "") or "").strip() or not str(
        getattr(hub, "device_id", "") or ""
    ).strip():
        raise RuntimeError("请先在 StoryForge 中登录员工账号并完成这台电脑的绑定。")

    stop = threading.Event()
    attach_exit = getattr(api, "_attach_process_exit_callback", None)
    if callable(attach_exit):
        attach_exit(stop.set)
    try:
        server = api._ensure_local_worker_server(
            Path(ui_root).resolve(),
            serve_ui=True,
            use_port_override=False,
        )
        health = dict(server.worker_gateway.health())
        if health.get("ready") is False:
            raise RuntimeError(
                "本机保存的 Hub 登录已失效。请打开 StoryForge，使用员工账号和密码重新登录；"
                "登录成功后会自动重新绑定这台制作电脑。"
            )
        runtime = dict(server.worker_gateway.runtime_snapshot())
        print(
            "StoryForge Local Worker: "
            f"{server.base_url} | {runtime.get('ffmpeg_label', 'FFmpeg')} | "
            f"ready={bool(health.get('ready'))}",
            flush=True,
        )

        # A verified package may have been scheduled before Windows restarted
        # the long-running Worker.  Do not leave that package waiting forever:
        # once the loopback server is healthy and rendering is idle, let the
        # Worker own the normal shutdown/apply handoff.
        update_manager = getattr(api, "_update_manager", None)
        if update_manager is not None:
            try:
                update_status = dict(update_manager.status())
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                update_status = {}
            if bool(update_status.get("apply_on_restart")) and not bool(
                update_status.get("rendering_busy")
            ):
                stop.set()

        _wait_for_stop(stop)
        return {"url": server.base_url, "health": health, "runtime": runtime}
    finally:
        if callable(attach_exit):
            attach_exit(None)


def _enforce_safe_worker_handoff(status: dict[str, object]) -> None:
    """Abort desktop startup while the background queue still owns work."""

    if not bool(status.get("worker_running")):
        return
    message = str(status.get("message") or "").strip()
    if not message:
        message = (
            "后台制作服务仍在退出，为避免同一任务被两套队列重复处理，"
            "请等待几秒后重新打开 StoryForge。"
        )
    raise SystemExit(message)


def _validated_local_worker_ui_url(value: object) -> str:
    """Accept only a discovered StoryForge loopback endpoint as window URL."""

    from .worker import LOCAL_WORKER_DISCOVERY_PORTS

    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("本机制作页面地址无效。") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port not in LOCAL_WORKER_DISCOVERY_PORTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("本机制作页面地址无效。")
    return f"http://127.0.0.1:{port}/"


def _open_existing_worker_window(endpoint: object, *, debug: bool = False) -> int:
    """Show the live background worker UI without owning its process lifetime."""

    url = _validated_local_worker_ui_url(endpoint)
    try:
        import webview
    except ImportError as error:
        raise SystemExit(
            "后台任务仍在继续，但当前安装缺少桌面窗口组件。"
            f"请在浏览器打开 {url} 查看制作进度。"
        ) from error

    webview.create_window(
        "StoryForge Studio",
        url,
        width=1480,
        height=920,
        min_size=(1120, 720),
        background_color="#E9EEF7",
        text_select=True,
    )
    from .portable import configured_webview_storage_path

    webview.start(
        debug=debug,
        gui="edgechromium" if os.name == "nt" else None,
        storage_path=configured_webview_storage_path(),
    )
    # The scheduled Worker owns both the API and render queue. Closing this
    # viewer must not call its shutdown route or interrupt its current job.
    return 0


def main(argv: list[str] | None = None) -> int:
    global _PROCESS_WORKER_MUTEX
    args = build_parser().parse_args(argv)
    if args.kokoro_self_test:
        from .diagnostics import run_kokoro_self_test

        return run_kokoro_self_test(args.kokoro_self_test)
    if args.startup_self_test:
        from .diagnostics import run_startup_self_test

        return run_startup_self_test(
            args.startup_self_test,
            ui_root=resource_path("ui"),
        )
    if not args.web_only:
        # The long-running local Worker is the sole owner of this computer's
        # render queue.  The desktop executable is only a viewer/client when
        # that Worker already exists; it must never stop an idle Worker and
        # create a second queue owner merely because a window was opened.
        from .worker import (
            discover_local_production_worker,
            pause_local_worker_autostart_for_desktop,
        )

        # Lifecycle discovery includes an unhealthy listener so the desktop
        # can retire it safely. Normal worker discovery deliberately hides
        # ready=false endpoints from production callers.
        existing_worker = discover_local_production_worker(include_unready=True)
        # A running Worker remains authoritative even when it belongs to an
        # older release.  Legacy workers do not expose an atomic
        # drain-and-shutdown operation, so killing one after an external
        # "idle" check could interrupt a task submitted in the meantime.  The
        # update installer replaces the login Worker while the application is
        # closed; a manually copied repair build takes effect after the old
        # Worker is closed or Windows is restarted.
        if existing_worker and existing_worker.get("endpoint"):
            if args.local_worker:
                # Scheduled/manual background launches are viewless. The live
                # owner keeps its queue; this process exits without importing
                # the API or binding a second discovery port.
                return 0
            if existing_worker.get("ready") is False:
                # The loopback browser intentionally has no password-login
                # route. Yield an idle invalid Worker, then continue into the
                # desktop bridge whose login form can re-enrol the stable
                # installation using only account + password.
                handoff = pause_local_worker_autostart_for_desktop()
                if not bool(handoff.get("paused")):
                    _enforce_safe_worker_handoff(handoff)
                    raise SystemExit(
                        "本机后台制作服务的 Hub 登录已失效，但暂时无法安全关闭。"
                        "请等待当前任务结束后重新打开 StoryForge，再使用账号和密码登录。"
                    )
            else:
                return _open_existing_worker_window(
                    existing_worker["endpoint"], debug=args.debug
                )

    # Portable initialization may have deferred the legacy AppData copy while
    # an older Worker was alive. If that owner stopped between the early probe
    # and the loopback discovery above, finish migration now rather than
    # constructing a queue against an empty StoryForgeData directory.
    from .portable import ensure_deferred_migration_complete

    try:
        ensure_deferred_migration_complete()
    except RuntimeError as error:
        raise SystemExit(str(error)) from error

    # Port discovery has a check-then-bind race. A scheduled Worker and the
    # desktop can both see no listener and otherwise start two queues on
    # adjacent ports. Acquire the per-data-directory mutex before constructing
    # StoryForgeApi; the loser waits for and opens the sole owner.
    from .config import default_data_dir
    from .worker import ProductionWorkerMutex, wait_for_local_production_worker

    ownership = ProductionWorkerMutex(default_data_dir())
    if not ownership.acquire():
        existing_worker = wait_for_local_production_worker(timeout_seconds=25.0)
        if (
            not args.local_worker
            and not args.web_only
            and existing_worker
            and existing_worker.get("endpoint")
        ):
            return _open_existing_worker_window(
                existing_worker["endpoint"], debug=args.debug
            )
        raise SystemExit(
            "StoryForge 本机制作服务正在启动，请稍候几秒后重新打开；已有任务不会中断。"
        )
    _PROCESS_WORKER_MUTEX = ownership

    from .api import StoryForgeApi
    from .pipeline import PipelineRunner

    api = StoryForgeApi(
        hub_listen_host=args.web_host,
        hub_listen_port=args.web_port,
    )
    api._queue.set_processor(
        PipelineRunner(
            lambda: api._state.settings,
            text_provider_factory=api._runtime_text_provider_factory,
            work_root=api._repository.data_dir / "render-work",
            heavy_resource_lock=api._heavy_resource_lock,
        )
    )
    page = resource_path("ui/index.html")
    api._desktop_ui_root = page.parent
    if not page.is_file():
        raise SystemExit(f"界面文件不存在：{page}")

    if args.local_worker:
        try:
            _run_local_worker_service(api, page.parent)
        except RuntimeError as error:
            raise SystemExit(str(error)) from error
        finally:
            api._shutdown()
        return 0

    try:
        web_status = api._enable_web_access(page.parent)
    except RuntimeError as error:
        if args.web_only:
            api._shutdown()
            raise SystemExit(str(error)) from error
        web_status = None

    if web_status is not None and api._runtime_hub_mode == "client":
        api._worker_autostart_after_login()

    if args.web_only:
        assert web_status is not None
        print(f"StoryForge Web: {web_status['url']}", flush=True)
        try:
            _wait_for_stop()
        finally:
            api._shutdown()
        return 0

    try:
        import webview
    except ImportError as error:
        api._shutdown()
        raise SystemExit(
            "缺少 pywebview。请先运行：python -m pip install -r requirements.txt"
        ) from error

    from .desktop_bridge import StoryForgeDesktopBridge

    window = webview.create_window(
        "StoryForge Studio",
        page.as_uri(),
        js_api=StoryForgeDesktopBridge(api),
        width=1480,
        height=920,
        min_size=(1120, 720),
        background_color="#E9EEF7",
        text_select=True,
    )
    api._attach_window(window)
    try:
        from .portable import configured_webview_storage_path

        webview.start(
            debug=args.debug,
            gui="edgechromium" if os.name == "nt" else None,
            storage_path=configured_webview_storage_path(),
        )
    finally:
        api._shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
