from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run
from storyforge import main as storyforge_main


class StartupDiagnosticsTests(unittest.TestCase):
    def test_startup_failure_writes_timestamped_and_latest_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {"LOCALAPPDATA": temporary},
                clear=False,
            ):
                try:
                    raise RuntimeError("catalog schema 12 is newer than supported")
                except RuntimeError as error:
                    path = run._write_startup_failure(error)

            self.assertTrue(path.is_file())
            self.assertEqual(path.parent, Path(temporary) / "StoryForgeStudio" / "logs")
            content = path.read_text(encoding="utf-8")
            self.assertIn("RuntimeError", content)
            self.assertIn("catalog schema 12", content)
            self.assertIn("traceback:", content)
            self.assertEqual(
                content,
                (path.parent / "startup-error-latest.log").read_text(encoding="utf-8"),
            )

    def test_local_worker_mode_starts_only_loopback_worker_without_ui(self) -> None:
        calls: list[tuple[Path, bool, bool]] = []
        gateway = SimpleNamespace(
            health=lambda: {
                "service": "storyforge-local-worker",
                "ready": True,
                "device_id": "device-1",
            },
            runtime_snapshot=lambda: {
                "ffmpeg_ready": True,
                "ffmpeg_label": "FFmpeg",
                "encoders": ["libx264"],
                "recommended_encoder": "libx264",
            },
        )
        server = SimpleNamespace(
            base_url="http://127.0.0.1:18765",
            worker_gateway=gateway,
        )

        class FakeApi:
            _runtime_hub_mode = "client"
            _state = SimpleNamespace(
                settings=SimpleNamespace(
                    hub=SimpleNamespace(
                        access_token="device-token",
                        device_id="device-1",
                    )
                )
            )

            def _ensure_local_worker_server(
                self, ui_root: Path, *, serve_ui: bool, use_port_override: bool
            ):
                calls.append((ui_root, serve_ui, use_port_override))
                return server

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(storyforge_main, "_wait_for_stop") as wait,
            redirect_stdout(io.StringIO()),
        ):
            status = storyforge_main._run_local_worker_service(FakeApi(), Path(temporary))

        wait.assert_called_once_with()
        self.assertEqual(calls, [(Path(temporary).resolve(), True, False)])
        self.assertEqual(status["url"], "http://127.0.0.1:18765")
        self.assertTrue(status["runtime"]["ffmpeg_ready"])

    def test_existing_busy_worker_opens_its_ui_without_creating_second_api(self) -> None:
        busy = {
            "state": "busy",
            "worker_running": True,
            "rendering_busy": True,
            "queue_busy": True,
            "endpoint": "http://127.0.0.1:18765",
        }
        with (
            patch(
                "storyforge.worker.pause_local_worker_autostart_for_desktop",
                return_value=busy,
            ),
            patch(
                "storyforge.api.StoryForgeApi",
                side_effect=AssertionError("must not create a second API"),
            ) as api_constructor,
            patch.object(
                storyforge_main,
                "_open_existing_worker_window",
                return_value=0,
            ) as open_window,
        ):
            result = storyforge_main.main([])

        self.assertEqual(result, 0)
        open_window.assert_called_once_with(
            "http://127.0.0.1:18765", debug=False
        )
        api_constructor.assert_not_called()

    def test_existing_worker_window_is_view_only_process_lifetime(self) -> None:
        created: list[tuple[tuple[object, ...], dict[str, object]]] = []
        started: list[dict[str, object]] = []
        fake_webview = SimpleNamespace(
            create_window=lambda *args, **kwargs: created.append((args, kwargs)),
            start=lambda **kwargs: started.append(kwargs),
        )

        with patch.dict("sys.modules", {"webview": fake_webview}):
            result = storyforge_main._open_existing_worker_window(
                "http://127.0.0.1:18766", debug=True
            )

        self.assertEqual(result, 0)
        self.assertEqual(created[0][0][1], "http://127.0.0.1:18766/")
        self.assertNotIn("js_api", created[0][1])
        self.assertTrue(started[0]["debug"])

    def test_existing_worker_window_rejects_non_loopback_url(self) -> None:
        for value in (
            "https://127.0.0.1:18765",
            "http://10.0.0.225:18765",
            "http://127.0.0.1:9999",
            "http://127.0.0.1:18765/app.js",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                storyforge_main._validated_local_worker_ui_url(value)

    def test_local_worker_argument_is_exclusive_with_hub_web_mode(self) -> None:
        parser = storyforge_main.build_parser()
        self.assertTrue(parser.parse_args(["--local-worker"]).local_worker)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--local-worker", "--web"])

    def test_busy_background_worker_exits_desktop_with_clear_message(self) -> None:
        message = (
            "这台电脑正在后台制作视频。"
            "后台任务会继续运行，StoryForge 桌面窗口暂不启动。"
        )
        with self.assertRaisesRegex(SystemExit, "后台任务会继续运行"):
            storyforge_main._enforce_safe_worker_handoff(
                {
                    "state": "busy",
                    "worker_running": True,
                    "rendering_busy": True,
                    "queue_busy": True,
                    "message": message,
                }
            )

    def test_idle_background_worker_allows_desktop_startup(self) -> None:
        storyforge_main._enforce_safe_worker_handoff(
            {"state": "paused", "worker_running": False}
        )

    def test_release_build_copies_current_user_worker_service_scripts(self) -> None:
        project = Path(__file__).resolve().parents[1]
        enable = (project / "scripts" / "enable_storyforge_worker.ps1").read_text(
            encoding="utf-8"
        )
        disable = (project / "scripts" / "disable_storyforge_worker.ps1").read_text(
            encoding="utf-8"
        )
        diagnose = (project / "scripts" / "diagnose_storyforge.ps1").read_text(
            encoding="utf-8-sig"
        )
        build = (project / "scripts" / "build_exe.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("--local-worker", enable)
        self.assertIn("Split-Path -Parent $PSScriptRoot", enable)
        self.assertIn("admin-tools", enable)
        self.assertIn("Split-Path -Parent $PSScriptRoot", diagnose)
        self.assertIn("-AtLogOn -User $identity", enable)
        self.assertIn("-RunLevel Limited", enable)
        self.assertNotIn("RunLevel Highest", enable)
        self.assertIn("Unregister-ScheduledTask", disable)
        self.assertIn("imageio_ffmpeg,webview,PyInstaller,edge_tts", build)
        self.assertIn("--kokoro-self-test", build)
        self.assertIn("BUILD_KOKORO_VALIDATION.json", build)
        self.assertIn("os.environ['STORYFORGE_DATA_DIR']", build)
        self.assertIn("storyforge-connection.json", build)
        self.assertIn("write_connection_profile", build)
        self.assertIn("HubEndpoint", build)
        self.assertIn("Standalone", build)
        self.assertIn("Employee-ready builds require -HubEndpoint", build)
        self.assertIn("admin-tools", build)
        for name in (
            "diagnose_storyforge.ps1",
            "diagnose_storyforge.cmd",
            "enable_storyforge_worker.ps1",
            "disable_storyforge_worker.ps1",
            "enable_storyforge_worker.cmd",
            "disable_storyforge_worker.cmd",
            "EMPLOYEE_QUICK_START.md",
        ):
            self.assertIn(name, build)

    def test_windows_release_scripts_have_powershell_51_safe_encoding(self) -> None:
        project = Path(__file__).resolve().parents[1]
        utf8_bom_scripts = {
            "diagnose_storyforge.ps1",
            "enable_storyforge_worker.ps1",
        }
        for name in (
            "build_exe.ps1",
            "diagnose_storyforge.ps1",
            "diagnose_storyforge.cmd",
            "enable_storyforge_worker.ps1",
            "enable_storyforge_worker.cmd",
            "disable_storyforge_worker.ps1",
            "disable_storyforge_worker.cmd",
            "run_dev.ps1",
        ):
            content = (project / "scripts" / name).read_bytes()
            if name in utf8_bom_scripts:
                self.assertTrue(content.startswith(b"\xef\xbb\xbf"), name)
                content.decode("utf-8-sig")
            else:
                self.assertTrue(content.isascii(), name)

    def test_source_launcher_prefers_full_build_runtime_over_system_python(self) -> None:
        project = Path(__file__).resolve().parents[1]
        launcher = (project / "scripts" / "run_dev.ps1").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            launcher.index(".build-venv\\Scripts\\python.exe"),
            launcher.index("Get-Command -Name 'python'"),
        )


if __name__ == "__main__":
    unittest.main()
