from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run
from storyforge import __version__
from storyforge import main as storyforge_main
from storyforge import system as storyforge_system


class EncoderRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        storyforge_system._runtime_encoder_works.cache_clear()
        storyforge_system.available_encoders.cache_clear()
        with storyforge_system._DISABLED_ENCODERS_LOCK:
            storyforge_system._DISABLED_ENCODERS.clear()

    def test_hardware_probe_uses_delivery_resolution_and_frame_rate(self) -> None:
        observed: list[str] = []

        def run(command, **_kwargs):
            observed.extend(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("storyforge.system.run_cancellable_process", side_effect=run):
            self.assertTrue(
                storyforge_system._runtime_encoder_works(
                    "fake-ffmpeg.exe",
                    "h264_nvenc",
                )
            )

        self.assertIn("color=c=black:s=1080x1920:r=60:d=0.25", observed)
        self.assertEqual(observed[observed.index("-frames:v") + 1], "15")

    def test_failed_hardware_encoder_is_disabled_for_the_session(self) -> None:
        executable = Path("fake-ffmpeg.exe").resolve()
        storyforge_system.mark_encoder_unavailable(executable, "h264_nvenc")

        def run(command, **_kwargs):
            self.assertIn("-encoders", command)
            return SimpleNamespace(
                returncode=0,
                stdout="V..... h264_nvenc\nV..... libx264\n",
                stderr="",
            )

        with patch("storyforge.system.run_cancellable_process", side_effect=run):
            encoders = storyforge_system.available_encoders(executable)

        self.assertEqual(encoders, ["libx264"])

    def test_disabling_cached_hardware_encoder_reselects_cpu_fallback(self) -> None:
        executable = Path("fake-ffmpeg.exe").resolve()

        def run(command, **_kwargs):
            self.assertIn("-encoders", command)
            return SimpleNamespace(
                returncode=0,
                stdout="V..... h264_nvenc\nV..... libx264\n",
                stderr="",
            )

        with (
            patch("storyforge.system.run_cancellable_process", side_effect=run),
            patch(
                "storyforge.system._runtime_encoder_works",
                return_value=True,
            ) as probe,
        ):
            self.assertEqual(
                storyforge_system.available_encoders(executable),
                ["h264_nvenc", "libx264"],
            )
            storyforge_system.mark_encoder_unavailable(
                executable, "h264_nvenc"
            )
            self.assertEqual(
                storyforge_system.available_encoders(executable),
                ["libx264"],
            )

        probe.assert_called_once_with(str(executable), "h264_nvenc")

    def test_hardware_probe_stops_after_first_working_backend(self) -> None:
        executable = Path("fake-ffmpeg.exe").resolve()

        def run(command, **_kwargs):
            self.assertIn("-encoders", command)
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "V..... h264_nvenc\n"
                    "V..... h264_qsv\n"
                    "V..... h264_amf\n"
                    "V..... libx264\n"
                ),
                stderr="",
            )

        with (
            patch("storyforge.system.run_cancellable_process", side_effect=run),
            patch(
                "storyforge.system._runtime_encoder_works",
                return_value=True,
            ) as probe,
        ):
            encoders = storyforge_system.available_encoders(executable)

        self.assertEqual(encoders, ["h264_nvenc", "libx264"])
        probe.assert_called_once_with(str(executable), "h264_nvenc")

    def test_hardware_probe_falls_through_to_next_backend(self) -> None:
        executable = Path("fake-ffmpeg.exe").resolve()

        def run(command, **_kwargs):
            self.assertIn("-encoders", command)
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "V..... h264_nvenc\n"
                    "V..... h264_qsv\n"
                    "V..... h264_amf\n"
                    "V..... libx264\n"
                ),
                stderr="",
            )

        with (
            patch("storyforge.system.run_cancellable_process", side_effect=run),
            patch(
                "storyforge.system._runtime_encoder_works",
                side_effect=(False, True),
            ) as probe,
        ):
            encoders = storyforge_system.available_encoders(executable)

        self.assertEqual(encoders, ["h264_qsv", "libx264"])
        self.assertEqual(
            probe.call_args_list,
            [
                unittest.mock.call(str(executable), "h264_nvenc"),
                unittest.mock.call(str(executable), "h264_qsv"),
            ],
        )

    def test_concurrent_first_calls_share_one_hardware_probe(self) -> None:
        executable = Path("fake-ffmpeg.exe").resolve()
        callers = 6
        barrier = threading.Barrier(callers)
        listing_calls = 0
        listing_lock = threading.Lock()

        def run(command, **_kwargs):
            nonlocal listing_calls
            self.assertIn("-encoders", command)
            with listing_lock:
                listing_calls += 1
            # Keep the first cache miss open long enough for every caller to
            # reach the outer probe lock.
            time.sleep(0.05)
            return SimpleNamespace(
                returncode=0,
                stdout="V..... h264_nvenc\nV..... libx264\n",
                stderr="",
            )

        def discover() -> list[str]:
            barrier.wait(timeout=2.0)
            return storyforge_system.available_encoders(executable)

        with (
            patch("storyforge.system.run_cancellable_process", side_effect=run),
            patch(
                "storyforge.system._runtime_encoder_works",
                return_value=True,
            ) as probe,
            ThreadPoolExecutor(max_workers=callers) as executor,
        ):
            results = list(executor.map(lambda _index: discover(), range(callers)))

        self.assertEqual(results, [["h264_nvenc", "libx264"]] * callers)
        self.assertEqual(listing_calls, 1)
        probe.assert_called_once_with(str(executable), "h264_nvenc")


class StartupDiagnosticsTests(unittest.TestCase):
    @staticmethod
    def _write_installing_marker(
        data_root: Path,
        *,
        phase: str,
        installation_id: str = "install-123",
        installed_at: datetime | None = None,
    ) -> Path:
        marker_path = data_root / "updates" / "pending-update.json"
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {
                    "installing": True,
                    "installing_at": (
                        installed_at or datetime.now(UTC)
                    ).isoformat(),
                    "installing_phase": phase,
                    "installation_id": installation_id,
                    "install_root": str(data_root.parent),
                }
            ),
            encoding="utf-8",
        )
        return marker_path

    def test_copy_window_blocks_before_importing_mutable_application_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "StoryForgeData"
            self._write_installing_marker(data_root, phase="copying")
            with (
                patch.dict(
                    os.environ,
                    {
                        "STORYFORGE_DATA_DIR": str(data_root),
                        "STORYFORGE_UPDATE_HEALTH_TOKEN": "",
                    },
                    clear=False,
                ),
                patch.object(run.sys, "argv", ["StoryForge Studio.exe"]),
                patch.object(
                    run, "_update_install_mutex_owned", return_value=False
                ),
                patch(
                    "storyforge.portable.configure_runtime_environment"
                ) as configure,
                self.assertRaisesRegex(SystemExit, "正在完成软件更新"),
            ):
                run._run()

            configure.assert_not_called()

    def test_installer_health_desktop_and_worker_are_the_only_bypasses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "StoryForgeData"
            self._write_installing_marker(
                data_root,
                phase="health_check",
                installation_id="health-secret",
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "STORYFORGE_DATA_DIR": str(data_root),
                        "STORYFORGE_UPDATE_HEALTH_TOKEN": "health-secret",
                    },
                    clear=False,
                ),
                patch.object(
                    run, "_update_install_mutex_owned", return_value=True
                ),
            ):
                self.assertIsNone(run._active_update_installation([]))

            with (
                patch.dict(
                    os.environ,
                    {
                        "STORYFORGE_DATA_DIR": str(data_root),
                        "STORYFORGE_UPDATE_HEALTH_TOKEN": "wrong-token",
                    },
                    clear=False,
                ),
                patch.object(
                    run, "_update_install_mutex_owned", return_value=True
                ),
            ):
                self.assertIsNotNone(run._active_update_installation([]))
                self.assertIsNone(
                    run._active_update_installation(["--local-worker"])
                )

    def test_stale_marker_stops_blocking_after_install_mutex_is_released(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "StoryForgeData"
            marker = self._write_installing_marker(
                data_root,
                phase="copying",
                installed_at=datetime.now(UTC) - timedelta(minutes=11),
            )
            with (
                patch.dict(
                    os.environ,
                    {"STORYFORGE_DATA_DIR": str(data_root)},
                    clear=False,
                ),
                patch.object(
                    run, "_update_install_mutex_owned", return_value=False
                ),
            ):
                self.assertIsNone(run._active_update_installation([]))
                marker.unlink()
                self.assertIsNone(run._active_update_installation([]))

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

    def test_startup_failure_log_falls_back_when_portable_root_is_unwritable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blocker = root / "not-a-directory"
            blocker.write_text("file", encoding="utf-8")
            fallback = root / "LocalAppData"
            with patch.dict(
                os.environ,
                {
                    "STORYFORGE_DATA_DIR": str(blocker),
                    "LOCALAPPDATA": str(fallback),
                },
                clear=False,
            ):
                path = run._write_startup_failure(
                    RuntimeError("move the complete folder to D or E")
                )

            self.assertEqual(
                path.parent, fallback / "StoryForgeStudio" / "logs"
            )
            self.assertIn(
                "move the complete folder",
                path.read_text(encoding="utf-8"),
            )

    def test_frozen_windows_bootstrap_disables_webview_gpu_and_unblocks_bundle(self) -> None:
        data_root = Path("D:/StoryForge/StoryForgeData")
        with (
            patch.object(run.sys, "frozen", True, create=True),
            patch.object(run.os, "name", "nt"),
            patch.dict(run.os.environ, {}, clear=True),
            patch(
                "storyforge.portable.configure_runtime_environment",
                return_value=data_root,
            ),
            patch(
                "storyforge.windows_bootstrap.unblock_frozen_installation"
            ) as unblock,
            patch("storyforge.main.main", return_value=0),
        ):
            result = run._run()
            browser_arguments = run.os.environ.get(
                "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", ""
            )

        self.assertEqual(result, 0)
        unblock.assert_called_once_with(data_root)
        self.assertIn("--disable-gpu", browser_arguments)

    def test_frozen_web_records_only_a_preconfigured_data_root_as_hub_authority(
        self,
    ) -> None:
        configured_root = Path("D:/StoryForgeHub/Data").resolve(strict=False)

        def configure_after_startup(_argv):
            run.os.environ["STORYFORGE_DATA_DIR"] = str(configured_root)
            return configured_root

        with (
            patch.object(run.sys, "frozen", True, create=True),
            patch.object(
                run.sys,
                "argv",
                ["StoryForge Studio.exe", "--web"],
            ),
            patch.dict(
                run.os.environ,
                {"STORYFORGE_FROZEN_HUB_DATA_ROOT": "forged-stale-value"},
                clear=True,
            ),
            patch(
                "storyforge.portable.configure_runtime_environment",
                side_effect=configure_after_startup,
            ),
            patch(
                "storyforge.component_updater.activate_component_runtime_from_environment"
            ),
            patch("storyforge.main.main", return_value=0),
        ):
            self.assertEqual(run._run(), 0)
            self.assertNotIn(
                "STORYFORGE_FROZEN_HUB_DATA_ROOT",
                run.os.environ,
            )

        with (
            patch.object(run.sys, "frozen", True, create=True),
            patch.object(
                run.sys,
                "argv",
                ["StoryForge Studio.exe", "--web"],
            ),
            patch.dict(
                run.os.environ,
                {
                    "STORYFORGE_DATA_DIR": str(configured_root),
                    "STORYFORGE_DEPLOYMENT_ROLE": "Hub",
                },
                clear=True,
            ),
            patch(
                "storyforge.portable.configure_runtime_environment",
                return_value=configured_root,
            ),
            patch(
                "storyforge.component_updater.activate_component_runtime_from_environment"
            ),
            patch("storyforge.main.main", return_value=0),
        ):
            self.assertEqual(run._run(), 0)
            self.assertEqual(
                run.os.environ.get("STORYFORGE_FROZEN_HUB_DATA_ROOT"),
                str(configured_root),
            )

    def test_frozen_data_root_without_hub_deployment_role_is_not_authorized(
        self,
    ) -> None:
        configured_root = Path("D:/Employee/StoryForgeData").resolve(strict=False)
        with (
            patch.object(run.sys, "frozen", True, create=True),
            patch.dict(
                run.os.environ,
                {
                    "STORYFORGE_DATA_DIR": str(configured_root),
                    "STORYFORGE_DEPLOYMENT_ROLE": "Employee",
                    "STORYFORGE_FROZEN_HUB_DATA_ROOT": "stale-forged-value",
                },
                clear=True,
            ),
        ):
            self.assertIsNone(run._record_frozen_hub_data_root_authorization())
            self.assertNotIn(
                "STORYFORGE_FROZEN_HUB_DATA_ROOT",
                run.os.environ,
            )

    def test_frozen_employee_child_cannot_promote_inherited_data_root_to_hub(
        self,
    ) -> None:
        previous_tempdir = tempfile.tempdir
        try:
            with tempfile.TemporaryDirectory() as temporary:
                install = Path(temporary) / "Employee"
                install.mkdir()
                executable = install / "StoryForge Studio.exe"
                executable.write_bytes(b"exe")
                (install / "storyforge-connection.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "endpoint": "http://10.0.0.225:8765",
                        }
                    ),
                    encoding="utf-8",
                )
                sidecar = install / "StoryForgeData"
                with (
                    patch.object(run.sys, "frozen", True, create=True),
                    patch.object(run.sys, "executable", str(executable)),
                    patch.object(run.sys, "argv", [str(executable)]),
                    patch.dict(
                        run.os.environ,
                        {
                            "STORYFORGE_DATA_DIR": str(sidecar),
                            "STORYFORGE_PORTABLE_MODE": "1",
                            "STORYFORGE_FROZEN_HUB_DATA_ROOT": "stale-forged-value",
                        },
                        clear=True,
                    ),
                    patch(
                        "storyforge.component_updater.activate_component_runtime_from_environment"
                    ),
                    patch(
                        "storyforge.windows_bootstrap.unblock_frozen_installation"
                    ),
                    patch("storyforge.main.main", return_value=0),
                ):
                    self.assertEqual(run._run(), 0)
                    self.assertNotIn(
                        "STORYFORGE_FROZEN_HUB_DATA_ROOT",
                        run.os.environ,
                    )
                    self.assertEqual(
                        run.os.environ.get("STORYFORGE_PORTABLE_MODE"),
                        "1",
                    )
        finally:
            tempfile.tempdir = previous_tempdir

    def test_hub_web_mode_does_not_acquire_production_worker_mutex(self) -> None:
        fake_api = unittest.mock.Mock()
        fake_api._queue = unittest.mock.Mock()
        fake_api._state.settings = SimpleNamespace()
        fake_api._repository.data_dir = Path("data")
        fake_api._heavy_resource_lock = threading.Lock()
        fake_api._runtime_hub_mode = "host"
        fake_api._enable_web_access.return_value = {
            "url": "http://127.0.0.1:8765"
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "storyforge.portable.ensure_deferred_migration_complete"
            ),
            patch("storyforge.worker.ProductionWorkerMutex") as mutex,
            patch("storyforge.api.StoryForgeApi", return_value=fake_api) as api_factory,
            patch("storyforge.pipeline.PipelineRunner"),
            patch.object(
                storyforge_main,
                "resource_path",
                return_value=Path(temporary) / "index.html",
            ),
            patch.object(storyforge_main, "_wait_for_stop"),
            patch.object(storyforge_main, "_PROCESS_WORKER_MUTEX", None),
            redirect_stdout(io.StringIO()),
        ):
            (Path(temporary) / "index.html").write_text("UI", encoding="utf-8")
            result = storyforge_main.main(["--web"])

        self.assertEqual(result, 0)
        mutex.assert_not_called()
        api_factory.assert_called_once_with(
            hub_listen_host=None,
            hub_listen_port=None,
            local_production_enabled=False,
        )
        fake_api._queue.set_processor.assert_not_called()
        fake_api._shutdown.assert_called_once_with()

    def test_local_worker_still_acquires_production_worker_mutex(self) -> None:
        ownership = unittest.mock.Mock()
        ownership.acquire.return_value = True
        fake_api = unittest.mock.Mock()
        fake_api._queue = unittest.mock.Mock()
        fake_api._state.settings = SimpleNamespace()
        fake_api._repository.data_dir = Path("data")
        fake_api._heavy_resource_lock = threading.Lock()
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "storyforge.worker.discover_local_production_worker",
                return_value=None,
            ),
            patch(
                "storyforge.portable.ensure_deferred_migration_complete"
            ),
            patch(
                "storyforge.worker.ProductionWorkerMutex",
                return_value=ownership,
            ) as mutex,
            patch("storyforge.config.default_data_dir", return_value=Path("data")),
            patch("storyforge.api.StoryForgeApi", return_value=fake_api),
            patch("storyforge.pipeline.PipelineRunner"),
            patch.object(
                storyforge_main,
                "resource_path",
                return_value=Path(temporary) / "index.html",
            ),
            patch.object(storyforge_main, "_run_local_worker_service"),
            patch.object(storyforge_main, "_PROCESS_WORKER_MUTEX", None),
        ):
            (Path(temporary) / "index.html").write_text("UI", encoding="utf-8")
            result = storyforge_main.main(["--local-worker"])

        self.assertEqual(result, 0)
        mutex.assert_called_once_with(Path("data"))
        ownership.acquire.assert_called_once_with()
        fake_api._shutdown.assert_called_once_with()

    def test_desktop_still_acquires_production_worker_mutex(self) -> None:
        ownership = unittest.mock.Mock()
        ownership.acquire.return_value = True
        fake_api = unittest.mock.Mock()
        fake_api._queue = unittest.mock.Mock()
        fake_api._state.settings = SimpleNamespace()
        fake_api._repository.data_dir = Path("data")
        fake_api._heavy_resource_lock = threading.Lock()
        fake_api._runtime_hub_mode = "local"
        fake_api._enable_web_access.return_value = None
        window = object()
        fake_webview = SimpleNamespace(
            create_window=unittest.mock.Mock(return_value=window),
            start=unittest.mock.Mock(),
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "storyforge.worker.discover_local_production_worker",
                return_value=None,
            ),
            patch(
                "storyforge.portable.ensure_deferred_migration_complete"
            ),
            patch(
                "storyforge.worker.ProductionWorkerMutex",
                return_value=ownership,
            ) as mutex,
            patch("storyforge.config.default_data_dir", return_value=Path("data")),
            patch("storyforge.api.StoryForgeApi", return_value=fake_api),
            patch("storyforge.pipeline.PipelineRunner"),
            patch("storyforge.desktop_bridge.StoryForgeDesktopBridge"),
            patch.object(
                storyforge_main,
                "resource_path",
                return_value=Path(temporary) / "index.html",
            ),
            patch.object(storyforge_main, "_PROCESS_WORKER_MUTEX", None),
            patch.dict("sys.modules", {"webview": fake_webview}),
        ):
            (Path(temporary) / "index.html").write_text("UI", encoding="utf-8")
            result = storyforge_main.main([])

        self.assertEqual(result, 0)
        mutex.assert_called_once_with(Path("data"))
        ownership.acquire.assert_called_once_with()
        fake_api._attach_window.assert_called_once_with(window)
        fake_api._shutdown.assert_called_once_with()

    def test_hub_desktop_stays_management_only_when_hub_and_worker_are_running(
        self,
    ) -> None:
        fake_api = unittest.mock.Mock()
        fake_api._queue = unittest.mock.Mock()
        fake_api._state.settings = SimpleNamespace()
        fake_api._repository.data_dir = Path("hub-data")
        fake_api._heavy_resource_lock = threading.Lock()
        fake_api._runtime_hub_mode = "host"
        fake_api._enable_web_access.side_effect = RuntimeError(
            "Hub port is already owned by the background service"
        )
        window = object()
        fake_webview = SimpleNamespace(
            create_window=unittest.mock.Mock(return_value=window),
            start=unittest.mock.Mock(),
        )
        existing_worker = {
            "ready": True,
            "worker_role": "production-workstation",
            "endpoint": "http://127.0.0.1:18765",
        }
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                storyforge_main.os.environ,
                {"STORYFORGE_DEPLOYMENT_ROLE": "Hub"},
                clear=True,
            ),
            patch(
                "storyforge.worker.discover_local_production_worker",
                return_value=existing_worker,
            ) as discover_worker,
            patch.object(
                storyforge_main, "_open_existing_worker_window"
            ) as open_worker,
            patch(
                "storyforge.portable.ensure_deferred_migration_complete"
            ),
            patch("storyforge.worker.ProductionWorkerMutex") as mutex,
            patch(
                "storyforge.api.StoryForgeApi", return_value=fake_api
            ) as api_factory,
            patch("storyforge.pipeline.PipelineRunner") as pipeline,
            patch("storyforge.desktop_bridge.StoryForgeDesktopBridge"),
            patch.object(
                storyforge_main,
                "resource_path",
                return_value=Path(temporary) / "index.html",
            ),
            patch.object(storyforge_main, "_PROCESS_WORKER_MUTEX", None),
            patch.dict("sys.modules", {"webview": fake_webview}),
        ):
            (Path(temporary) / "index.html").write_text("UI", encoding="utf-8")
            result = storyforge_main.main([])

        self.assertEqual(result, 0)
        discover_worker.assert_not_called()
        open_worker.assert_not_called()
        mutex.assert_not_called()
        api_factory.assert_called_once_with(
            hub_listen_host=None,
            hub_listen_port=None,
            local_production_enabled=False,
        )
        pipeline.assert_not_called()
        fake_api._queue.set_processor.assert_not_called()
        fake_api._attach_window.assert_called_once_with(window)
        fake_api._shutdown.assert_called_once_with()

    def test_frozen_stability_gate_dispatches_after_portable_setup(self) -> None:
        with (
            patch.object(
                run.sys,
                "argv",
                [
                    "StoryForge Studio.exe",
                    "--storyforge-stability-acceptance",
                    "--quick",
                    "--root",
                    "D:/acceptance",
                ],
            ),
            patch(
                "storyforge.portable.configure_runtime_environment",
                return_value=Path("D:/StoryForge/StoryForgeData"),
            ) as configure,
            patch(
                "scripts.stability_render_acceptance.main",
                return_value=7,
            ) as acceptance_main,
        ):
            result = run._run()

        self.assertEqual(result, 7)
        configure.assert_called_once()
        acceptance_main.assert_called_once_with(
            ["--quick", "--root", "D:/acceptance"]
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

        wait.assert_called_once()
        self.assertIsInstance(wait.call_args.args[0], threading.Event)
        self.assertFalse(wait.call_args.args[0].is_set())
        self.assertEqual(calls, [(Path(temporary).resolve(), True, False)])
        self.assertEqual(status["url"], "http://127.0.0.1:18765")
        self.assertTrue(status["runtime"]["ffmpeg_ready"])

    def test_local_worker_exits_when_saved_hub_login_is_invalid(self) -> None:
        gateway = SimpleNamespace(
            health=lambda: {
                "service": "storyforge-local-worker",
                "ready": False,
                "device_id": "device-1",
            },
            runtime_snapshot=lambda: self.fail(
                "an invalid worker must stop before reporting runtime"
            ),
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
                        access_token="revoked-device-token",
                        device_id="device-1",
                    )
                )
            )

            def _ensure_local_worker_server(self, *_args, **_kwargs):
                return server

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(storyforge_main, "_wait_for_stop") as wait,
            self.assertRaisesRegex(RuntimeError, "重新登录"),
        ):
            storyforge_main._run_local_worker_service(
                FakeApi(), Path(temporary)
            )

        wait.assert_not_called()

    def test_desktop_retires_idle_unready_worker_then_reaches_login_startup(self) -> None:
        worker = {
            "ready": False,
            "rendering_busy": False,
            "queue_busy": False,
            "endpoint": "http://127.0.0.1:18765",
        }
        with (
            patch(
                "storyforge.worker.discover_local_production_worker",
                return_value=worker,
            ),
            patch(
                "storyforge.worker.pause_local_worker_autostart_for_desktop",
                return_value={
                    "state": "paused",
                    "paused": True,
                    "worker_running": False,
                },
            ) as pause_worker,
            patch(
                "storyforge.portable.ensure_deferred_migration_complete",
                side_effect=RuntimeError("login-startup-reached"),
            ),
            patch.object(
                storyforge_main, "_open_existing_worker_window"
            ) as open_worker,
            self.assertRaisesRegex(SystemExit, "login-startup-reached"),
        ):
            storyforge_main.main([])

        pause_worker.assert_called_once_with()
        open_worker.assert_not_called()

    def test_desktop_does_not_interrupt_busy_unready_worker(self) -> None:
        worker = {
            "ready": False,
            "rendering_busy": True,
            "queue_busy": True,
            "endpoint": "http://127.0.0.1:18765",
        }
        with (
            patch(
                "storyforge.worker.discover_local_production_worker",
                return_value=worker,
            ),
            patch(
                "storyforge.worker.pause_local_worker_autostart_for_desktop",
                return_value={
                    "state": "busy",
                    "paused": False,
                    "worker_running": True,
                    "message": "后台任务仍在制作",
                },
            ),
            patch.object(
                storyforge_main, "_open_existing_worker_window"
            ) as open_worker,
            self.assertRaisesRegex(SystemExit, "后台任务仍在制作"),
        ):
            storyforge_main.main([])

        open_worker.assert_not_called()

    def test_local_worker_applies_a_pending_update_on_start_when_idle(self) -> None:
        callbacks: list[object] = []
        gateway = SimpleNamespace(
            health=lambda: {"ready": True},
            runtime_snapshot=lambda: {"ffmpeg_ready": True},
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
            _update_manager = SimpleNamespace(
                status=lambda: {
                    "apply_on_restart": True,
                    "rendering_busy": False,
                }
            )

            def _attach_process_exit_callback(self, callback):
                callbacks.append(callback)

            def _ensure_local_worker_server(self, *_args, **_kwargs):
                return server

        observed: list[bool] = []

        def wait(stop: threading.Event) -> None:
            observed.append(stop.is_set())

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(storyforge_main, "_wait_for_stop", side_effect=wait),
            redirect_stdout(io.StringIO()),
        ):
            storyforge_main._run_local_worker_service(
                FakeApi(), Path(temporary)
            )

        self.assertEqual(observed, [True])
        self.assertTrue(callable(callbacks[0]))
        self.assertIsNone(callbacks[-1])

    def test_existing_worker_always_opens_its_ui_without_creating_second_api(self) -> None:
        worker = {
            "rendering_busy": False,
            "queue_busy": False,
            "endpoint": "http://127.0.0.1:18765",
        }
        with (
            patch(
                "storyforge.worker.discover_local_production_worker",
                return_value=worker,
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

    def test_local_worker_process_exits_when_any_queue_owner_already_exists(self) -> None:
        worker = {
            "version": "0.4.2",
            "worker_role": "production-workstation",
            "endpoint": "http://127.0.0.1:18765",
        }
        with (
            patch(
                "storyforge.worker.discover_local_production_worker",
                return_value=worker,
            ),
            patch(
                "storyforge.api.StoryForgeApi",
                side_effect=AssertionError("must not create a second queue"),
            ) as api_constructor,
            patch.object(
                storyforge_main,
                "_open_existing_worker_window",
                side_effect=AssertionError("background worker is viewless"),
            ),
        ):
            result = storyforge_main.main(["--local-worker"])

        self.assertEqual(result, 0)
        api_constructor.assert_not_called()

    def test_stale_idle_worker_is_preserved_until_safe_installer_restart(self) -> None:
        stale = {
            "version": "0.4.0-rc1",
            "protocol_version": 2,
            "minimum_browser_protocol_version": 2,
            "rendering_busy": False,
            "queue_busy": False,
            "endpoint": "http://127.0.0.1:18765",
        }
        with (
            patch(
                "storyforge.worker.discover_local_production_worker",
                return_value=stale,
            ),
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
        spec = (project / "StoryForge.spec").read_text(encoding="utf-8")

        self.assertIn("--local-worker", enable)
        self.assertIn("Split-Path -Parent $PSScriptRoot", enable)
        self.assertIn("admin-tools", enable)
        self.assertIn("Split-Path -Parent $PSScriptRoot", diagnose)
        self.assertIn("-AtLogOn -User $identity", enable)
        self.assertIn("-RunLevel Limited", enable)
        self.assertNotIn("RunLevel Highest", enable)
        self.assertIn("Unregister-ScheduledTask", disable)
        self.assertIn("imageio_ffmpeg,webview,PyInstaller,edge_tts", build)
        self.assertIn("StoryForge Studio.exe.config", build)
        self.assertIn("loadFromRemoteSources enabled=\"true\"", build)
        self.assertIn("--kokoro-self-test", build)
        self.assertIn("BUILD_KOKORO_VALIDATION.json", build)
        self.assertIn('collect_data_files("unidic_lite"', spec)
        self.assertIn('copy_metadata("unidic-lite")', spec)
        self.assertIn("os.environ['STORYFORGE_DATA_DIR']", build)
        self.assertIn("storyforge-connection.json", build)
        self.assertIn("write_connection_profile", build)
        self.assertIn("HubEndpoint", build)
        self.assertIn("Standalone", build)
        self.assertIn("Employee-ready builds require -HubEndpoint", build)
        self.assertIn("Assert-AsciiBuildPath", build)
        self.assertIn("ASCII-only path", build)
        self.assertIn("[System.IO.Path]::GetPathRoot($projectRoot)", build)
        self.assertIn("Join-Path $projectVolumeRoot 'StoryForgeBuildTemp'", build)
        self.assertIn("Join-Path $asciiDefaultBuildBase 'dist'", build)
        self.assertIn("Join-Path $asciiDefaultBuildBase 'work'", build)
        self.assertIn("admin-tools", build)
        for name in (
            "diagnose_storyforge.ps1",
            "diagnose_storyforge.cmd",
            "enable_storyforge_worker.ps1",
            "disable_storyforge_worker.ps1",
            "enable_storyforge_worker.cmd",
            "disable_storyforge_worker.cmd",
            "restore_hub_backup.ps1",
            "restore_hub_backup.cmd",
            "publish_hub_snapshot.ps1",
            "verify_storyforge_deployment.ps1",
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
            "restore_hub_backup.ps1",
            "restore_hub_backup.cmd",
            "publish_hub_snapshot.ps1",
            "verify_storyforge_deployment.ps1",
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

    def test_offline_restore_command_uses_verified_backup_manager_and_exits(self) -> None:
        manager = unittest.mock.Mock()
        manager.restore_snapshot.return_value = {
            "restored": True,
            "requires_restart": True,
        }
        output = io.StringIO()
        with (
            patch("storyforge.backup.HubBackupManager", return_value=manager),
            patch("storyforge.config.default_data_dir", return_value=Path("data")),
            redirect_stdout(output),
        ):
            result = storyforge_main.main(
                ["--restore-hub-backup", "verified-backup.sfbak"]
            )

        self.assertEqual(result, 0)
        manager.restore_snapshot.assert_called_once_with("verified-backup.sfbak")
        self.assertTrue(json.loads(output.getvalue())["restored"])

    def test_offline_backup_command_uses_external_directory_and_json_output(self) -> None:
        manager = unittest.mock.Mock()
        manager.create_snapshot.return_value = {
            "valid": True,
            "path": "transfer/snapshot.sfbak",
        }
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            backup_dir = root / "transfer"
            with (
                patch(
                    "storyforge.backup.HubBackupManager",
                    return_value=manager,
                ) as factory,
                patch("storyforge.config.default_data_dir", return_value=data_dir),
                redirect_stdout(output),
            ):
                result = storyforge_main.main(
                    ["--create-hub-backup", str(backup_dir)]
                )

        self.assertEqual(result, 0)
        factory.assert_called_once_with(
            data_dir.resolve(),
            backup_dir=backup_dir.resolve(),
        )
        manager.create_snapshot.assert_called_once_with(
            "github_transfer",
            cleanup=False,
            deduplicate=False,
            metadata={
                "app_version": __version__,
                "purpose": "github_private_recovery",
            },
        )
        self.assertTrue(json.loads(output.getvalue())["valid"])

    def test_offline_backup_command_does_not_write_to_source_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "authoritative-data"
            backup_dir = root / "transfer"
            data_dir.mkdir()
            database = data_dir / "storyforge-catalog.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.executescript(
                    """
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );
                    INSERT INTO schema_migrations VALUES (10, '2026-08-12T00:00:00Z');
                    CREATE TABLE novels (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
                    INSERT INTO novels(title) VALUES ('Transfer test');
                    """
                )
                connection.commit()
            finally:
                connection.close()
            settings = data_dir / "settings.json"
            settings.write_text(
                json.dumps({"schema_version": 19, "settings": {}}),
                encoding="utf-8",
            )
            original_files = sorted(path.name for path in data_dir.iterdir())
            original_database = database.read_bytes()
            original_settings = settings.read_bytes()

            output = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {"STORYFORGE_DATA_DIR": str(data_dir)},
                ),
                redirect_stdout(output),
            ):
                result = storyforge_main.main(
                    ["--create-hub-backup", str(backup_dir)]
                )

            self.assertEqual(result, 0)
            self.assertTrue(json.loads(output.getvalue())["valid"])
            self.assertEqual(
                sorted(path.name for path in data_dir.iterdir()),
                original_files,
            )
            self.assertEqual(database.read_bytes(), original_database)
            self.assertEqual(settings.read_bytes(), original_settings)
            self.assertEqual(len(list(backup_dir.glob("*.sfbak"))), 1)

    def test_offline_backup_rejects_source_directory_and_conflicting_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            error = io.StringIO()
            with (
                patch("storyforge.config.default_data_dir", return_value=data_dir),
                redirect_stderr(error),
            ):
                result = storyforge_main.main(
                    ["--create-hub-backup", str(data_dir / "hub-backups")]
                )
            self.assertEqual(result, 1)
            self.assertFalse(json.loads(error.getvalue())["ok"])

        with (
            self.assertRaises(SystemExit),
            redirect_stderr(io.StringIO()),
        ):
            storyforge_main.build_parser().parse_args(
                [
                    "--create-hub-backup",
                    "transfer",
                    "--restore-hub-backup",
                    "snapshot.sfbak",
                ]
            )

    def test_offline_restore_picker_prefers_storyforge_backup_extension(self) -> None:
        project = Path(__file__).resolve().parents[1]
        script = (project / "scripts" / "restore_hub_backup.ps1").read_text(
            encoding="ascii"
        )

        self.assertIn("StoryForge Hub backup (*.sfbak)|*.sfbak", script)
        self.assertIn("Legacy StoryForge backup (*.zip)|*.zip", script)


if __name__ == "__main__":
    unittest.main()
