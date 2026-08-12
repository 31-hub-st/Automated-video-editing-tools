from __future__ import annotations

import tempfile
import json
import socket
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen
from unittest.mock import patch

from storyforge.worker import (
    LOCAL_WORKER_MIN_BROWSER_PROTOCOL_VERSION,
    LOCAL_WORKER_PROTOCOL_VERSION,
    LOCAL_WORKER_RPC_PERMISSIONS,
    LocalWorkerGateway,
    LocalWorkerProfileStore,
    ProductionWorkerMutex,
    discover_local_production_worker,
    ensure_local_worker_autostart,
    local_worker_release_state,
    pause_local_worker_autostart_for_desktop,
    _disk_status,
    _resource_status,
)
from storyforge.web import ClientLocalWebServer
from storyforge.api import StoryForgeApi
from storyforge.config import AppSettings, SettingsRepository
from storyforge.jobs import JobQueue
from storyforge.models import JobStatus, PlatformProfile, RenderJob


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class LocalWorkerProfileTests(unittest.TestCase):
    def test_discovery_hides_unready_worker_except_for_lifecycle_handoff(self) -> None:
        payload = {
            "ok": True,
            "data": {
                "service": "storyforge-local-worker",
                "worker_role": "production-workstation",
                "ready": False,
                "rendering_busy": False,
                "queue_busy": False,
            },
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit: int) -> bytes:
                return json.dumps(payload).encode("utf-8")

        with patch("storyforge.worker.urlopen", return_value=Response()):
            self.assertIsNone(discover_local_production_worker())
            lifecycle_worker = discover_local_production_worker(
                include_unready=True
            )

        self.assertIsNotNone(lifecycle_worker)
        assert lifecycle_worker is not None
        self.assertFalse(lifecycle_worker["ready"])
        self.assertEqual(
            lifecycle_worker["endpoint"], "http://127.0.0.1:18765"
        )

    def test_worker_release_state_requires_matching_version_and_protocol(self) -> None:
        self.assertEqual(LOCAL_WORKER_PROTOCOL_VERSION, 3)
        self.assertEqual(LOCAL_WORKER_MIN_BROWSER_PROTOCOL_VERSION, 3)
        current = {
            "version": "0.4.1",
            "protocol_version": LOCAL_WORKER_PROTOCOL_VERSION,
            "minimum_browser_protocol_version": LOCAL_WORKER_PROTOCOL_VERSION,
        }
        self.assertEqual(
            local_worker_release_state(current, expected_version="0.4.1"),
            "current",
        )
        self.assertEqual(
            local_worker_release_state(current, expected_version="0.4.2"),
            "stale",
        )
        self.assertEqual(local_worker_release_state({}), "unknown")

    def test_worker_mutex_identity_does_not_split_across_data_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = ProductionWorkerMutex(temporary)
            second = ProductionWorkerMutex(Path(temporary) / "portable" / "StoryForgeData")
            self.assertEqual(first.name, second.name)
            self.assertEqual(first.name, "Global\\StoryForgeProductionWorker")
            self.assertIn(
                "Local\\StoryForgeProductionWorker", first.compatibility_names
            )
            # Both new processes also probe the legacy AppData-derived alias,
            # so an already-running 0.4.2 Worker cannot coexist with a newly
            # migrated portable Worker.
            self.assertTrue(
                set(first.compatibility_names).intersection(
                    second.compatibility_names
                )
            )
            if sys.platform != "win32":
                self.assertTrue(first.acquire())
                first.release()

    def test_packaged_login_task_never_contains_account_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "StoryForge Studio.exe"
            executable.write_bytes(b"MZ")
            admin_tools = root / "admin-tools"
            admin_tools.mkdir()
            installer = admin_tools / "enable_storyforge_worker.ps1"
            installer.write_text(
                "# worker setup", encoding="utf-8"
            )
            calls: list[tuple[list[str], dict]] = []

            def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
                calls.append((list(command), dict(kwargs)))
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("storyforge.worker.os.name", "nt"):
                result = ensure_local_worker_autostart(
                    executable_path=executable,
                    command_runner=runner,
                )

            self.assertEqual(result["state"], "enabled")
            self.assertEqual(len(calls), 1)
            command_text = " ".join(calls[0][0])
            self.assertIn(str(installer), command_text)
            self.assertIn("-NoHealthWait", command_text)
            self.assertIn("-Quiet", command_text)
            self.assertNotIn("password", command_text.casefold())
            self.assertNotIn("access_token", command_text.casefold())
            self.assertFalse(bool(calls[0][1]["shell"]))

    def test_desktop_pause_is_best_effort_and_idempotent(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(list(command))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("storyforge.worker.os.name", "nt"),
            patch("storyforge.worker.sys.frozen", True, create=True),
            patch(
                "storyforge.worker.discover_local_production_worker",
                return_value=None,
            ),
        ):
            first = pause_local_worker_autostart_for_desktop(
                command_runner=runner
            )
            second = pause_local_worker_autostart_for_desktop(
                command_runner=runner
            )
        self.assertTrue(first["paused"])
        self.assertTrue(second["paused"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("Stop-ScheduledTask" in " ".join(call) for call in calls))

    def test_desktop_pause_leaves_busy_worker_running(self) -> None:
        for rendering_busy, queue_busy in ((True, True), (False, True)):
            with self.subTest(
                rendering_busy=rendering_busy, queue_busy=queue_busy
            ):
                calls: list[list[str]] = []

                def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
                    calls.append(list(command))
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                busy = {
                    "endpoint": "http://127.0.0.1:18765",
                    "rendering_busy": rendering_busy,
                    "queue_busy": queue_busy,
                }
                with (
                    patch("storyforge.worker.os.name", "nt"),
                    patch("storyforge.worker.sys.frozen", True, create=True),
                    patch(
                        "storyforge.worker.discover_local_production_worker",
                        return_value=busy,
                    ),
                ):
                    result = pause_local_worker_autostart_for_desktop(
                        command_runner=runner
                    )

                self.assertEqual(result["state"], "busy")
                self.assertTrue(result["worker_running"])
                self.assertEqual(result["rendering_busy"], rendering_busy)
                self.assertEqual(result["endpoint"], busy["endpoint"])
                self.assertEqual(calls, [])
                self.assertIn("后台任务会继续运行", result["message"])

    def test_desktop_pause_stops_idle_worker_before_starting_ui(self) -> None:
        calls: list[list[str]] = []
        idle = {
            "endpoint": "http://127.0.0.1:18765",
            "rendering_busy": False,
            "queue_busy": False,
        }

        def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(list(command))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("storyforge.worker.os.name", "nt"),
            patch("storyforge.worker.sys.frozen", True, create=True),
            patch(
                "storyforge.worker.discover_local_production_worker",
                side_effect=[idle, None],
            ),
        ):
            result = pause_local_worker_autostart_for_desktop(
                command_runner=runner
            )

        self.assertEqual(result["state"], "paused")
        self.assertTrue(result["paused"])
        self.assertFalse(result["worker_running"])
        self.assertEqual(len(calls), 1)
        self.assertIn("Stop-ScheduledTask", " ".join(calls[0]))

    def test_desktop_pause_refuses_unknown_legacy_worker_state(self) -> None:
        with (
            patch("storyforge.worker.os.name", "nt"),
            patch("storyforge.worker.sys.frozen", True, create=True),
            patch(
                "storyforge.worker.discover_local_production_worker",
                return_value={"endpoint": "http://127.0.0.1:18765"},
            ),
        ):
            result = pause_local_worker_autostart_for_desktop(
                command_runner=lambda *_args, **_kwargs: self.fail(
                    "legacy worker must not be stopped blindly"
                )
            )

        self.assertEqual(result["state"], "worker_state_unknown")
        self.assertTrue(result["worker_running"])
        self.assertTrue(result["queue_busy"])

    def test_profile_is_private_persistent_and_uses_workstation_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = LocalWorkerProfileStore(root)
            defaults = store.load()
            self.assertEqual(set(defaults), set(store.KEYS))
            self.assertTrue(all(Path(value).is_dir() for value in defaults.values()))

            video = root / "employee-video"
            music = root / "employee-music"
            output = root / "employee-output"
            saved = store.save(
                {
                    "video_folder": str(video),
                    "music_folder": str(music),
                    "output_folder": str(output),
                }
            )
            self.assertEqual(LocalWorkerProfileStore(root).load(), saved)
            self.assertTrue(all(Path(value).is_dir() for value in saved.values()))
            self.assertNotIn("hub", store.path.name.casefold())

    def test_profile_rejects_root_unc_and_wildcard_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LocalWorkerProfileStore(temporary)
            for invalid in (str(Path(temporary).anchor), r"\\server\share", r"C:\bad\*"):
                with self.subTest(value=invalid), self.assertRaises(ValueError):
                    store.save({"video_folder": invalid})

    def test_folder_choice_persists_the_workstation_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "employee-videos"
            repository = SettingsRepository(root / "app-data")
            api = StoryForgeApi(repository=repository)
            api._window = SimpleNamespace(
                create_file_dialog=lambda _kind: [str(selected)]
            )
            fake_webview = SimpleNamespace(
                FileDialog=SimpleNamespace(FOLDER="folder")
            )
            try:
                with patch.dict(sys.modules, {"webview": fake_webview}):
                    result = api.choose_folder("video_folder")
                self.assertTrue(result["ok"], result)
                self.assertEqual(Path(result["data"]), selected)
                stored = LocalWorkerProfileStore(repository.data_dir).load()
                self.assertEqual(Path(stored["video_folder"]), selected)
            finally:
                api._shutdown()


class _HubClientStub:
    def __init__(self, actor_user_id: str, device_id: str) -> None:
        self.actor_user_id = actor_user_id
        self.device_id = device_id
        self.calls: list[tuple[str, str, str]] = []

    def redeem_local_worker_ticket(
        self, ticket: str, *, worker_nonce: str, browser_origin: str
    ) -> dict:
        self.calls.append((ticket, worker_nonce, browser_origin))
        if ticket != "valid-ticket":
            raise PermissionError("invalid ticket")
        return {
            "actor_user_id": self.actor_user_id,
            "device_id": self.device_id,
            "device_name": "Employee PC",
            "browser_origin": browser_origin,
            "permissions": ["drafts.create", "records.view_own", "jobs.retry_own"],
        }

    def health(self) -> dict:
        return {"ok": True, "service": "storyforge-hub", "protocol_version": 1}


class _ApiStub:
    def __init__(self, root: Path) -> None:
        self._repository = SimpleNamespace(data_dir=root)
        self._runtime_hub_mode = "client"
        hub = SimpleNamespace(device_id="device-1", device_name="Employee PC")
        providers = SimpleNamespace(
            tts_provider="edge_tts",
            tts_api_key="employee-secret",
            tts_endpoint="https://voice.example.invalid/v1",
            kokoro_endpoint="",
            kokoro_command="",
        )
        self._state = SimpleNamespace(
            settings=SimpleNamespace(hub=hub, providers=providers)
        )
        self._hub_client = _HubClientStub("user-1", "device-1")
        self.queued: list[dict] = []
        self.actors: list[str] = []
        self.opened_output_folders: list[str] = []
        self._queue = SimpleNamespace(
            is_rendering_busy=lambda: False,
            list_jobs=lambda: [],
        )

    @contextmanager
    def _web_actor_scope(self, actor_user_id: str):
        self.actors.append(actor_user_id)
        yield

    def queue_production_draft(self, value: dict) -> dict:
        self.queued.append(dict(value))
        return {"ok": True, "data": {"jobs": [], "draft": {"id": value["draft_id"]}}}

    def get_jobs(self) -> dict:
        return {"ok": True, "data": []}

    def get_queue_connection(self) -> dict:
        return {
            "ok": True,
            "data": {"state": "connected", "reconnecting": False},
        }

    def choose_folder(self, _kind: str) -> dict:
        return {"ok": False, "error": "desktop window unavailable"}

    def open_output_folder(self, path: str) -> dict:
        self.opened_output_folders.append(str(path))
        return {"ok": True, "data": {"path": str(path)}}


class LocalWorkerGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        memory_patch = patch(
            "storyforge.worker._memory_status",
            return_value={
                "observable": True,
                "total_bytes": 16 * 1024**3,
                "available_bytes": 8 * 1024**3,
                "low": False,
            },
        )
        disk_patch = patch(
            "storyforge.worker._disk_status",
            return_value={
                "observable": True,
                "total_bytes": 200 * 1024**3,
                "free_bytes": 100 * 1024**3,
                "low_space": False,
            },
        )
        memory_patch.start()
        disk_patch.start()
        self.addCleanup(memory_patch.stop)
        self.addCleanup(disk_patch.stop)
        self.api = _ApiStub(self.root)
        self.gateway = LocalWorkerGateway(self.api)

    def test_connect_and_queue_use_only_workstation_folders(self) -> None:
        health = self.gateway.health()
        self.assertTrue(health["ready"])
        self.assertFalse(health["rendering_busy"])
        self.assertFalse(health["queue_busy"])
        self.assertEqual(health["unfinished_jobs"], 0)
        self.assertEqual(health["health_state"], "ready")
        self.assertTrue(health["queue"]["observable"])
        self.assertTrue(health["storage"]["observable"])
        self.assertTrue(health["connectivity"]["hub_connected"])
        self.assertNotIn("folders", health)
        self.assertEqual(health["protocol_version"], LOCAL_WORKER_PROTOCOL_VERSION)
        self.assertEqual(
            health["minimum_browser_protocol_version"],
            LOCAL_WORKER_MIN_BROWSER_PROTOCOL_VERSION,
        )
        connected = self.gateway.connect(
            "valid-ticket",
            "http://10.0.0.225:8765",
            browser_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION,
            minimum_worker_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION,
        )
        folders = connected["folders"]
        runtime = connected["runtime"]
        self.assertIsInstance(connected["self_check"]["ready"], bool)
        self.assertEqual(runtime["tts_provider"], "edge_tts")
        self.assertEqual(
            connected["negotiated_protocol_version"],
            LOCAL_WORKER_PROTOCOL_VERSION,
        )
        self.assertIsInstance(runtime["ffmpeg_ready"], bool)
        self.assertEqual(runtime["ffmpeg_label"], "FFmpeg")
        self.assertIsInstance(runtime["encoders"], list)
        self.assertIn(runtime["recommended_encoder"], [*runtime["encoders"], ""])
        self.assertIsInstance(runtime["edge_tts_runtime_ready"], bool)
        self.assertIsInstance(runtime["embedded_kokoro_ready"], bool)
        self.assertTrue(runtime["tts_api_key_configured"])
        self.assertTrue(runtime["tts_endpoint_configured"])
        self.assertNotIn("employee-secret", json.dumps(runtime))
        self.assertNotIn("voice.example.invalid", json.dumps(runtime))
        result = self.gateway.rpc(
            connected["session_token"],
            "http://10.0.0.225:8765",
            "queue_production_draft",
            [{"draft_id": "draft-1"}],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.actors, ["user-1"])
        self.assertEqual(
            {key: self.api.queued[0][key] for key in folders}, folders
        )
        self.assertNotIn("10.0.0.225", " ".join(folders.values()))

        snapshot = self.gateway.rpc(
            connected["session_token"],
            "http://10.0.0.225:8765",
            "worker_runtime_snapshot",
            [],
        )
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["data"]["tts_provider"], "edge_tts")

        self_check = self.gateway.rpc(
            connected["session_token"],
            "http://10.0.0.225:8765",
            "worker_self_check",
            [],
        )
        self.assertTrue(self_check["ok"])
        self.assertIsInstance(self_check["data"]["ready"], bool)
        self.assertEqual(
            {item["key"] for item in self_check["data"]["checks"]},
            {
                "worker",
                "ffmpeg",
                "encoder",
                "tts",
                "video_folder",
                "music_folder",
                "output_folder",
                "disk",
                "hub",
            },
        )

    def test_health_reports_unfinished_queue_for_safe_desktop_handoff(self) -> None:
        self.api._queue = SimpleNamespace(
            is_rendering_busy=lambda: False,
            list_jobs=lambda: [
                {"status": "completed"},
                {"status": "queued"},
                {"status": "failed"},
            ],
        )

        health = self.gateway.health()

        self.assertFalse(health["rendering_busy"])
        self.assertTrue(health["queue_busy"])
        self.assertEqual(health["unfinished_jobs"], 1)
        self.assertEqual(health["health_state"], "busy")
        self.assertEqual(health["queue"]["status"], "queued")

    def test_health_reports_stalled_progress_without_exposing_story_or_paths(self) -> None:
        secret_title = "Secret Novel Title"
        secret_path = r"D:\employee-private\secret-title.txt"
        self.api._queue = SimpleNamespace(
            is_rendering_busy=lambda: True,
            list_jobs=lambda: [
                {
                    "id": "job-1",
                    "status": "rendering",
                    "progress": 0.42,
                    "stage_label": "正在生成字幕",
                    "title": secret_title,
                    "source_path": secret_path,
                    "error_message": "private failure text",
                }
            ],
        )
        base = self.gateway.started_at_unix
        with patch("storyforge.worker.time.time", return_value=base + 1):
            first = self.gateway.health()
        with patch("storyforge.worker.time.time", return_value=base + 602):
            stale = self.gateway.health()

        self.assertFalse(first["queue"]["progress_stale"])
        self.assertTrue(stale["queue"]["progress_stale"])
        self.assertEqual(stale["health_state"], "degraded")
        self.assertEqual(stale["queue"]["progress"], 0.42)
        self.assertEqual(stale["queue"]["stage"], "正在生成字幕")
        serialized = json.dumps(stale, ensure_ascii=False)
        self.assertNotIn(secret_title, serialized)
        self.assertNotIn(secret_path, serialized)
        self.assertNotIn("private failure text", serialized)

    def test_health_treats_unreadable_queue_as_degraded_and_busy(self) -> None:
        def fail() -> list[dict]:
            raise RuntimeError("queue database unavailable")

        self.api._queue = SimpleNamespace(
            is_rendering_busy=lambda: False,
            list_jobs=fail,
        )

        health = self.gateway.health()

        self.assertEqual(health["health_state"], "degraded")
        self.assertTrue(health["queue_busy"])
        self.assertFalse(health["queue"]["observable"])

    def test_health_uses_unified_queue_governance_state_compatibly(self) -> None:
        legacy_states = {
            "paused": "busy",
            "draining": "busy",
            "cooling": "busy",
            "fault": "degraded",
        }
        for state in ("paused", "draining", "cooling", "fault"):
            with self.subTest(state=state):
                self.api._queue = SimpleNamespace(
                    is_rendering_busy=lambda: state == "draining",
                    list_jobs=lambda: (
                        [{"status": "rendering"}]
                        if state == "draining"
                        else [{"status": "queued"}]
                    ),
                    governance_status=lambda: {
                        "state": state,
                        "accepting_new_work": False,
                        "paused": state == "paused",
                        "draining": state == "draining",
                        "drained": False,
                        "cooling": state == "cooling",
                        "degraded": False,
                        "fault": state == "fault",
                        "retry_in_seconds": 12.0 if state == "cooling" else 0.0,
                        "reason": state,
                        "heavy_task_limit": 1,
                        "active_heavy_tasks": int(state == "draining"),
                    },
                )

                health = self.gateway.health()

                self.assertEqual(health["state"], state)
                self.assertEqual(health["health_state"], legacy_states[state])
                self.assertEqual(health["governance"]["state"], state)
                self.assertEqual(health["governance"]["heavy_task_limit"], 1)

    def test_low_memory_marks_worker_degraded_without_hiding_active_work(self) -> None:
        self.api._queue = SimpleNamespace(
            is_rendering_busy=lambda: True,
            list_jobs=lambda: [
                {"id": "job-1", "status": "rendering", "progress": 0.5}
            ],
        )
        low_memory = {
            "observable": True,
            "total_bytes": 16 * 1024**3,
            "available_bytes": 512 * 1024**2,
            "low": True,
        }

        with patch("storyforge.worker._memory_status", return_value=low_memory):
            health = self.gateway.health()

        self.assertEqual(health["state"], "degraded")
        self.assertEqual(health["health_state"], "degraded")
        self.assertTrue(health["rendering_busy"])
        self.assertFalse(health["resources"]["admission_allowed"])
        self.assertEqual(health["resources"]["reason"], "low_memory")

    def test_disk_status_uses_backing_volume_before_data_folder_exists(self) -> None:
        missing_data_dir = self.root / "first-run" / "StoryForgeStudio"

        status = _disk_status(missing_data_dir)

        self.assertTrue(status["observable"])
        self.assertGreater(status["total_bytes"], 0)
        self.assertFalse(missing_data_dir.exists())

    def test_low_output_disk_marks_worker_degraded_before_new_work(self) -> None:
        healthy_memory = {
            "observable": True,
            "total_bytes": 16 * 1024**3,
            "available_bytes": 8 * 1024**3,
            "low": False,
        }
        healthy_disk = {
            "observable": True,
            "total_bytes": 100 * 1024**3,
            "free_bytes": 50 * 1024**3,
            "low_space": False,
        }
        low_output_disk = {
            "observable": True,
            "total_bytes": 100 * 1024**3,
            "free_bytes": 2 * 1024**3,
            "low_space": True,
        }

        with (
            patch("storyforge.worker._memory_status", return_value=healthy_memory),
            patch(
                "storyforge.worker._disk_status",
                side_effect=[healthy_disk, low_output_disk],
            ),
        ):
            health = self.gateway.health()

        self.assertEqual(health["state"], "degraded")
        self.assertFalse(health["accepting_new_work"])
        self.assertEqual(health["resources"]["reason"], "low_output_disk")

    def test_resource_gate_creates_missing_output_folder_on_available_disk(self) -> None:
        output = self.root / "ordinary-output" / "nested"
        healthy_memory = {
            "observable": True,
            "total_bytes": 16 * 1024**3,
            "available_bytes": 8 * 1024**3,
            "low": False,
        }
        healthy_disk = {
            "observable": True,
            "total_bytes": 100 * 1024**3,
            "free_bytes": 50 * 1024**3,
            "low_space": False,
        }

        with (
            patch("storyforge.worker._memory_status", return_value=healthy_memory),
            patch("storyforge.worker._disk_status", return_value=healthy_disk),
        ):
            resources = _resource_status(self.root, output)

        self.assertTrue(output.is_dir())
        self.assertTrue(resources["admission_allowed"])
        self.assertFalse(resources["fault"])

    def test_missing_output_volume_is_a_fault_instead_of_endless_degraded_wait(self) -> None:
        healthy_memory = {
            "observable": True,
            "total_bytes": 16 * 1024**3,
            "available_bytes": 8 * 1024**3,
            "low": False,
        }
        healthy_disk = {
            "observable": True,
            "total_bytes": 100 * 1024**3,
            "free_bytes": 50 * 1024**3,
            "low_space": False,
        }

        with (
            patch("storyforge.worker._memory_status", return_value=healthy_memory),
            patch("storyforge.worker._disk_status", return_value=healthy_disk),
            patch("storyforge.worker._nearest_existing_directory", return_value=None),
        ):
            resources = _resource_status(self.root, self.root / "detached" / "output")

        self.assertFalse(resources["admission_allowed"])
        self.assertTrue(resources["fault"])
        self.assertEqual(resources["reason"], "output_volume_unavailable")
        self.assertIn("输出", resources["message"])

    def test_session_is_origin_bound_and_rpc_allowlisted(self) -> None:
        connected = self.gateway.connect(
            "valid-ticket",
            "http://10.0.0.225:8765",
            browser_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION,
            minimum_worker_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION,
        )
        with self.assertRaises(PermissionError):
            self.gateway.rpc(
                connected["session_token"],
                "http://evil.invalid",
                "get_jobs",
                [],
            )
        with self.assertRaises(PermissionError):
            self.gateway.rpc(
                connected["session_token"],
                "http://10.0.0.225:8765",
                "save_settings",
                [{}],
            )

    def test_connect_rejects_missing_or_incompatible_browser_protocol_before_ticket(self) -> None:
        origin = "http://10.0.0.225:8765"
        with self.assertRaisesRegex(ValueError, "browser_protocol_version"):
            self.gateway.connect(
                "valid-ticket",
                origin,
                browser_protocol_version=None,
                minimum_worker_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION,
            )
        with self.assertRaisesRegex(ValueError, "browser protocol is too old"):
            self.gateway.connect(
                "valid-ticket",
                origin,
                browser_protocol_version=LOCAL_WORKER_MIN_BROWSER_PROTOCOL_VERSION - 1,
                minimum_worker_protocol_version=1,
            )
        with self.assertRaisesRegex(ValueError, "local worker protocol is too old"):
            self.gateway.connect(
                "valid-ticket",
                origin,
                browser_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION + 1,
                minimum_worker_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION + 1,
            )
        self.assertEqual(self.api._hub_client.calls, [])

    def test_selected_record_cancel_and_local_tts_switch_are_worker_methods(self) -> None:
        self.assertIn("cancel_production_records", LOCAL_WORKER_RPC_PERMISSIONS)
        self.assertIn("set_local_tts_provider", LOCAL_WORKER_RPC_PERMISSIONS)
        self.assertIn("worker_self_check", LOCAL_WORKER_RPC_PERMISSIONS)
        self.assertIn("get_queue_connection", LOCAL_WORKER_RPC_PERMISSIONS)

    def test_open_output_folder_preserves_exact_local_batch_directory(self) -> None:
        connected = self.gateway.connect(
            "valid-ticket",
            "http://10.0.0.225:8765",
            browser_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION,
            minimum_worker_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION,
        )
        output_root = Path(connected["folders"]["output_folder"])
        batch_folder = output_root / "20260728_B001_GoodNovel_B41671"
        batch_folder.mkdir(parents=True)

        result = self.gateway.rpc(
            connected["session_token"],
            "http://10.0.0.225:8765",
            "open_output_folder",
            [str(batch_folder)],
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            self.api.opened_output_folders,
            [str(batch_folder.resolve(strict=True))],
        )

    def test_open_output_folder_rejects_paths_outside_local_output_root(self) -> None:
        connected = self.gateway.connect(
            "valid-ticket",
            "http://10.0.0.225:8765",
            browser_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION,
            minimum_worker_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION,
        )
        output_root = Path(connected["folders"]["output_folder"])
        outside = self.root / "worker-workspace" / "output-evil" / "batch"
        outside.mkdir(parents=True)
        self.assertNotEqual(outside.parent, output_root)

        with self.assertRaises(PermissionError):
            self.gateway.rpc(
                connected["session_token"],
                "http://10.0.0.225:8765",
                "open_output_folder",
                [str(outside)],
            )
        self.assertEqual(self.api.opened_output_folders, [])

    def test_open_output_folder_keeps_root_fallback_for_legacy_calls(self) -> None:
        connected = self.gateway.connect(
            "valid-ticket",
            "http://10.0.0.225:8765",
            browser_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION,
            minimum_worker_protocol_version=LOCAL_WORKER_PROTOCOL_VERSION,
        )
        output_root = Path(connected["folders"]["output_folder"])

        result = self.gateway.rpc(
            connected["session_token"],
            "http://10.0.0.225:8765",
            "open_output_folder",
            [],
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            self.api.opened_output_folders,
            [str(output_root.resolve(strict=True))],
        )

    def test_runtime_snapshot_exposes_only_safe_local_render_capabilities(self) -> None:
        with patch(
            "storyforge.worker.system_snapshot",
            return_value={
                "ffmpeg_ready": True,
                "ffmpeg_path": r"C:\private\employee-secret\ffmpeg.exe",
                "encoders": ["h264_nvenc", "libx264", "unexpected_encoder"],
                "recommended_encoder": "h264_nvenc",
            },
        ):
            runtime = self.gateway.runtime_snapshot()

        self.assertTrue(runtime["ffmpeg_ready"])
        self.assertEqual(runtime["ffmpeg_label"], "FFmpeg")
        self.assertEqual(runtime["encoders"], ["h264_nvenc", "libx264"])
        self.assertEqual(runtime["recommended_encoder"], "h264_nvenc")
        self.assertEqual(
            runtime["worker_protocol_version"], LOCAL_WORKER_PROTOCOL_VERSION
        )
        self.assertNotIn("ffmpeg_path", runtime)
        serialized = json.dumps(runtime)
        self.assertNotIn("employee-secret", serialized)
        self.assertNotIn(r"C:\private", serialized)

    def test_kokoro_self_check_distinguishes_missing_and_external_service(self) -> None:
        providers = self.api._state.settings.providers
        providers.tts_provider = "local_kokoro"
        providers.kokoro_endpoint = ""
        providers.kokoro_command = ""
        system = {
            "ffmpeg_ready": True,
            "encoders": ["libx264"],
            "recommended_encoder": "libx264",
            "edge_tts_runtime_ready": True,
            "embedded_kokoro_ready": False,
        }
        with patch("storyforge.worker.system_snapshot", return_value=system):
            missing = self.gateway.self_check()
        tts = next(item for item in missing["checks"] if item["key"] == "tts")
        self.assertEqual(tts["status"], "error")
        self.assertIn("Kokoro", tts["summary"])
        self.assertIn("切换到 Edge TTS", tts["fix"])

        providers.kokoro_endpoint = "http://127.0.0.1:8880"
        with patch("storyforge.worker.system_snapshot", return_value=system):
            external = self.gateway.self_check()
        tts = next(item for item in external["checks"] if item["key"] == "tts")
        self.assertEqual(tts["status"], "ok")
        self.assertIn("首次试听时验证", tts["summary"])
        self.assertEqual(external["runtime"]["tts_mode"], "http")

    def test_self_check_reports_missing_media_folder_with_employee_fix(self) -> None:
        folders = self.gateway.profile.load()
        missing_music = self.root / "employee-music"
        folders["music_folder"] = str(missing_music)
        self.gateway.profile.save(folders)
        missing_music.rmdir()
        with patch(
            "storyforge.worker.system_snapshot",
            return_value={
                "ffmpeg_ready": True,
                "encoders": ["libx264"],
                "recommended_encoder": "libx264",
                "edge_tts_runtime_ready": True,
                "embedded_kokoro_ready": False,
            },
        ):
            report = self.gateway.self_check()

        music = next(item for item in report["checks"] if item["key"] == "music_folder")
        self.assertEqual(music["status"], "error")
        self.assertIn("不存在", music["summary"])
        self.assertIn("重新选择", music["fix"])
        self.assertFalse(report["ready"])

    def test_all_local_queue_methods_remain_available_through_worker_gateway(self) -> None:
        local_queue_methods = {
            "get_jobs",
            "get_archived_jobs",
            "archive_job",
            "restore_job",
            "archive_batch",
            "restore_batch",
            "archive_finished_jobs",
            "clear_finished_jobs",
        }
        self.assertTrue(local_queue_methods.issubset(LOCAL_WORKER_RPC_PERMISSIONS))

    def test_loopback_http_gateway_supports_cross_origin_hub_browser(self) -> None:
        ui = self.root / "ui"
        ui.mkdir()
        for name, body in (
            ("index.html", "<!doctype html>"),
            ("app.js", ""),
            ("styles.css", ""),
            ("studio-theme.css", ""),
        ):
            (ui / name).write_text(body, encoding="utf-8")
        server = ClientLocalWebServer(
            self.api,
            ui_root=ui,
            upload_root=self.root / "uploads",
            port=0,
        ).start()
        self.addCleanup(server.stop)
        origin = "http://10.0.0.225:8765"

        with urlopen(server.base_url + "/worker/api/health", timeout=3) as response:
            health = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
        self.assertTrue(health["data"]["ready"])

        connect_body = json.dumps(
            {
                "ticket": "valid-ticket",
                "browser_protocol_version": LOCAL_WORKER_PROTOCOL_VERSION,
                "minimum_worker_protocol_version": LOCAL_WORKER_PROTOCOL_VERSION,
            }
        ).encode("utf-8")
        with urlopen(
            Request(
                server.base_url + "/worker/api/connect",
                data=connect_body,
                method="POST",
                headers={"Content-Type": "application/json", "Origin": origin},
            ),
            timeout=3,
        ) as response:
            connected = json.loads(response.read().decode("utf-8"))["data"]
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], origin)
        self.assertEqual(connected["runtime"]["tts_provider"], "edge_tts")
        self.assertEqual(
            connected["negotiated_protocol_version"],
            LOCAL_WORKER_PROTOCOL_VERSION,
        )
        self.assertIsInstance(connected["runtime"]["ffmpeg_ready"], bool)
        self.assertIn(
            connected["runtime"]["ffmpeg_label"], {"FFmpeg", "未检测到 FFmpeg"}
        )
        self.assertNotIn("ffmpeg_path", connected["runtime"])
        self.assertNotIn("employee-secret", json.dumps(connected["runtime"]))

        rpc_body = json.dumps({"method": "get_jobs", "args": []}).encode("utf-8")
        with urlopen(
            Request(
                server.base_url + "/worker/api/rpc",
                data=rpc_body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Origin": origin,
                    "X-StoryForge-Worker-Session": connected["session_token"],
                },
            ),
            timeout=3,
        ) as response:
            result = json.loads(response.read().decode("utf-8"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], [])

    def test_loopback_worker_port_is_owned_by_only_one_process(self) -> None:
        ui = self.root / "exclusive-ui"
        ui.mkdir()
        for name, body in (
            ("index.html", "<!doctype html>"),
            ("app.js", ""),
            ("styles.css", ""),
            ("studio-theme.css", ""),
        ):
            (ui / name).write_text(body, encoding="utf-8")
        port = _free_port()
        first = ClientLocalWebServer(
            self.api,
            ui_root=ui,
            upload_root=self.root / "exclusive-uploads-a",
            port=port,
        ).start()
        self.addCleanup(first.stop)
        second = ClientLocalWebServer(
            self.api,
            ui_root=ui,
            upload_root=self.root / "exclusive-uploads-b",
            port=port,
        )
        try:
            with self.assertRaises(OSError):
                second.start()
        finally:
            second.stop()


class HostLocalWorkerIntegrationTests(unittest.TestCase):
    def test_hub_only_public_production_entries_cannot_touch_local_queue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = SettingsRepository(root / "host-public-boundary")
            settings = AppSettings()
            settings.hub.mode = "host"
            settings.hub.listen_host = "127.0.0.1"
            settings.hub.listen_port = _free_port()
            repository.save(settings, [], [])
            queue = JobQueue()
            api = StoryForgeApi(
                repository=repository,
                queue=queue,
                local_production_enabled=False,
            )
            try:
                with (
                    patch.object(queue, "enqueue_stream") as enqueue_stream,
                    patch.object(queue, "enqueue_jobs") as enqueue_jobs,
                    patch.object(queue, "enqueue_batch") as enqueue_batch,
                    patch.object(queue, "restore_archived") as restore_archived,
                    patch.object(
                        queue, "restore_archived_batch"
                    ) as restore_archived_batch,
                    patch.object(queue, "approve_preview") as approve_preview,
                    patch.object(
                        queue, "regenerate_preview"
                    ) as regenerate_preview,
                    patch.object(queue, "retry_failed") as retry_failed,
                    patch.object(queue, "start") as start,
                ):
                    calls = (
                        api.queue_production_draft({}),
                        api.queue_batch({}),
                        api.start_queue(),
                        api.restore_job("archived-job"),
                        api.restore_batch("archived-batch"),
                        api.approve_preview("preview-job"),
                        api.regenerate_preview("preview-job"),
                        api.retry_failed("failed-job"),
                    )

                for result in calls:
                    self.assertFalse(result["ok"], result)
                    self.assertIn("Hub-only", result["error"])
                for queue_call in (
                    enqueue_stream,
                    enqueue_jobs,
                    enqueue_batch,
                    restore_archived,
                    restore_archived_batch,
                    approve_preview,
                    regenerate_preview,
                    retry_failed,
                    start,
                ):
                    queue_call.assert_not_called()
            finally:
                api._shutdown()

    def test_hub_only_startup_does_not_recover_seeded_render_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = SettingsRepository(root / "host-recovery")
            settings = AppSettings()
            settings.hub.mode = "host"
            settings.hub.device_id = "hub-device"
            settings.hub.listen_host = "127.0.0.1"
            settings.hub.listen_port = _free_port()
            repository.save(
                settings,
                [PlatformProfile(id="goodnovel", name="GoodNovel")],
                [],
            )
            seed_api = StoryForgeApi(repository=repository)
            try:
                novel = seed_api.import_novel_text(
                    {
                        "title": "Queued on Hub",
                        "text": "Chapter 1\nThis task belongs to a worker.",
                    }
                )["data"]["novel"]
                binding = seed_api._catalog.save_novel_binding(
                    {"novel_id": novel["id"], "platform_id": "goodnovel"}
                )
                code = seed_api._catalog.add_promo_code(
                    {"binding_id": binding["id"], "code": "HUB001"}
                )
                draft = seed_api._catalog.save_draft(
                    {
                        "novel_id": novel["id"],
                        "binding_id": binding["id"],
                        "promo_code_id": code["id"],
                        "episode_ids": [novel["episodes"][0]["id"]],
                        "creative_line_count": 1,
                    }
                )
                job = RenderJob(
                    id="hub-must-not-render",
                    batch_id=draft["id"],
                    platform_id="goodnovel",
                    source_file=__file__,
                    title="Queued on Hub",
                    code="HUB001",
                    video_folder=str(root / "video"),
                    music_folder=str(root / "music"),
                    output_folder=str(root / "output"),
                    status=JobStatus.QUEUED,
                    novel_id=novel["id"],
                    episode_id=novel["episodes"][0]["id"],
                    production_draft_id=draft["id"],
                    variant_index=1,
                    variant_count=1,
                )
                record = seed_api._catalog.save_production_record(
                    {
                        "draft_id": draft["id"],
                        "job_id": job.id,
                        "variant_index": 1,
                        "device_id": "hub-device",
                        "status": "queued",
                        "metadata": {
                            "production_run_id": "hub-only-recovery",
                            "job_snapshot": job.to_dict(),
                        },
                    }
                )
            finally:
                seed_api._shutdown()

            queue = JobQueue()
            hub_only = StoryForgeApi(
                repository=repository,
                queue=queue,
                local_production_enabled=False,
            )
            try:
                self.assertEqual(queue.list_jobs(), [])
                self.assertEqual(
                    hub_only._catalog.get_record(record["id"])["status"],
                    "queued",
                )
            finally:
                hub_only._shutdown()

    def test_hub_host_web_does_not_start_a_local_render_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = SettingsRepository(root / "host")
            settings = AppSettings()
            settings.hub.mode = "host"
            settings.hub.listen_host = "127.0.0.1"
            settings.hub.listen_port = _free_port()
            repository.save(settings, [], [])
            api = StoryForgeApi(repository=repository)
            try:
                ui = root / "ui"
                ui.mkdir()
                for name, body in (
                    ("index.html", "<!doctype html>"),
                    ("app.js", ""),
                    ("styles.css", ""),
                    ("studio-theme.css", ""),
                ):
                    (ui / name).write_text(body, encoding="utf-8")
                with patch.object(api, "_ensure_local_worker_server") as start_worker:
                    web = api._enable_web_access(ui)

                self.assertEqual(web["local_url"], api._hub_server.base_url)
                self.assertIsNone(api._client_web_server)
                start_worker.assert_not_called()
            finally:
                api._shutdown()


class WorkerInstallScriptContractTests(unittest.TestCase):
    def test_worker_scripts_are_windows_utf8_and_explain_recovery(self) -> None:
        root = Path(__file__).resolve().parents[1]
        enable_path = root / "scripts" / "enable_storyforge_worker.ps1"
        diagnose_path = root / "scripts" / "diagnose_storyforge.ps1"
        self.assertTrue(enable_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertTrue(diagnose_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        enable = enable_path.read_text(encoding="utf-8-sig")
        diagnose = diagnose_path.read_text(encoding="utf-8-sig")
        self.assertIn("RequiredProtocolVersion", enable)
        self.assertIn("protocol_version", enable)
        self.assertIn("NoHealthWait", enable)
        self.assertIn("RunWorker", enable)
        self.assertIn("-WindowStyle Hidden", enable)
        self.assertIn("自动重连，不需要重复启用", enable)
        self.assertIn("Unregister-ScheduledTask", enable)
        self.assertIn("--- 本机制作服务 / Local worker ---", diagnose)
        self.assertIn("legacy/missing", diagnose)


if __name__ == "__main__":
    unittest.main()
