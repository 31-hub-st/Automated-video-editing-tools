from __future__ import annotations

import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from storyforge.api import StoryForgeApi
from storyforge.config import SettingsRepository
from storyforge.hub import HubConnectionError
from storyforge.models import AppSettings, JobStatus, PlatformProfile, RenderJob


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ClientLocalWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.host_api: StoryForgeApi | None = None
        self.client_api: StoryForgeApi | None = None
        self.ui = self.root / "ui"
        self.ui.mkdir()
        (self.ui / "index.html").write_text(
            "<!doctype html><title>StoryForge local</title>", encoding="utf-8"
        )
        (self.ui / "app.js").write_text("window.ready=true;", encoding="utf-8")
        (self.ui / "styles.css").write_text("body{}", encoding="utf-8")

        host_repository = SettingsRepository(self.root / "host")
        host_settings = AppSettings()
        host_settings.hub.mode = "host"
        host_settings.hub.listen_host = "127.0.0.1"
        host_settings.hub.listen_port = _free_port()
        host_repository.save(
            host_settings,
            [PlatformProfile(id="goodnovel", name="GoodNovel")],
            [],
        )
        self.host_api = StoryForgeApi(repository=host_repository)
        self.member = self.host_api.save_software_user(
            {
                "username": "local-renderer",
                "display_name": "Local Renderer",
                "role": "producer",
                "active": True,
                "initial_password": "Lr1!2026",
            }
        )["data"]
        self.web_admin = self.host_api.save_software_user(
            {
                "username": "web-owner",
                "display_name": "Web Owner",
                "role": "admin",
                "active": True,
                "initial_password": "Wo1!2026",
            }
        )["data"]

        client_repository = SettingsRepository(self.root / "client")
        client_repository.save(AppSettings(), [], [])
        self.client_api = StoryForgeApi(
            repository=client_repository, hub_listen_port=0
        )
        connected = self.client_api.connect_hub_with_password(
            self.host_api._hub_server.base_url,  # type: ignore[union-attr]
            "local-renderer",
            "Lr1!2026",
            "Render PC",
        )
        self.assertTrue(connected["ok"], connected)
        allowed_root = self.root / "client-media"
        for name in ("videos", "music", "output"):
            (allowed_root / name).mkdir(parents=True, exist_ok=True)
        self.allowed_root = allowed_root
        self.client_api._state.update_settings(
            {"hub": {"web_allowed_roots": [str(allowed_root)]}}
        )
        self.client_api._queue.set_processor(
            lambda job, _platform, _progress: str(
                Path(job.output_folder) / f"{job.id}.mp4"
            )
        )
        status = self.client_api._enable_web_access(self.ui)
        self.assertEqual(status["mode"], "client_local")
        self.assertTrue(status["loopback_only"])
        self.assertTrue(status["local_url"].startswith("http://127.0.0.1:"))
        self.client_url = status["local_url"]

    def tearDown(self) -> None:
        if self.client_api is not None:
            self.client_api._shutdown()
        if self.host_api is not None:
            self.host_api._shutdown()
        self.temporary.cleanup()

    @staticmethod
    def _json(response) -> dict:
        return json.loads(response.read().decode("utf-8"))

    def _request(
        self,
        base_url: str,
        path: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ):
        request_headers = dict(headers or {})
        if method in {"POST", "DELETE"}:
            request_headers.setdefault("Origin", base_url)
        return urlopen(
            Request(
                base_url + path,
                method=method,
                data=data,
                headers=request_headers,
            ),
            timeout=5,
        )

    def _local_session(self) -> tuple[str, str, dict]:
        with self._request(self.client_url, "/web/api/session") as response:
            payload = self._json(response)
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        self.assertTrue(payload["ok"], payload)
        return cookie, payload["data"]["csrf_token"], payload["data"]

    def _rpc(
        self,
        base_url: str,
        cookie: str,
        csrf: str,
        method: str,
        args: list | None = None,
    ):
        return self._request(
            base_url,
            "/web/api/rpc",
            method="POST",
            data=json.dumps({"method": method, "args": args or []}).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "X-StoryForge-CSRF": csrf,
            },
        )

    def _owned_draft(self, *, title: str = "A Local Secret") -> dict:
        assert self.host_api is not None
        story_body = (
            f"Chapter 1\n{title}: The message on her husband's phone changed everything."
        )
        novel = self.host_api._catalog.import_novel(
            {
                "title": title,
                "language": "en",
                "body": story_body,
            },
            actor_user_id=self.member["id"],
        )["novel"]
        episodes = novel["current_revision"].get("episodes") or []
        if episodes:
            episode_id = episodes[0]["id"]
        else:
            episode = self.host_api._catalog.save_episode(
                {
                    "revision_id": novel["current_revision"]["id"],
                    "ordinal": 1,
                    "title": "Chapter 1",
                    "source_map": [
                        {
                            "start": 0,
                            "end": len(story_body),
                        }
                    ],
                    "estimated_duration_seconds": 8,
                },
                actor_user_id=self.member["id"],
            )
            episode_id = episode["id"]
        binding = self.host_api._catalog.save_novel_binding(
            {"novel_id": novel["id"], "platform_id": "goodnovel"},
            actor_user_id=self.member["id"],
        )
        code = self.host_api._catalog.add_promo_code(
            {"binding_id": binding["id"], "code": "B39760"},
            actor_user_id=self.member["id"],
        )
        other_worker = self.root / "other-worker"
        return self.host_api._catalog.save_draft(
            {
                "novel_id": novel["id"],
                "binding_id": binding["id"],
                "promo_code_id": code["id"],
                "episode_ids": [episode_id],
                "creative_line_count": 1,
                "created_by_user_id": self.member["id"],
                "metadata": {
                    "video_folder": str(other_worker / "videos"),
                    "music_folder": str(other_worker / "music"),
                    "output_folder": str(other_worker / "output"),
                },
            },
            actor_user_id=self.member["id"],
        )

    def test_worker_cors_allows_chromium_private_network_preflight(self) -> None:
        hub_origin = "http://10.0.0.225:8765"
        with self._request(
            self.client_url,
            "/worker/api/health",
            method="OPTIONS",
            headers={
                "Origin": hub_origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Private-Network": "true",
            },
        ) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(
                response.headers.get("Access-Control-Allow-Origin"), hub_origin
            )
            self.assertEqual(
                response.headers.get("Access-Control-Allow-Private-Network"),
                "true",
            )

    def test_device_session_is_automatic_secret_free_and_revocable(self) -> None:
        assert self.host_api is not None
        cookie, csrf, session = self._local_session()
        serialized = json.dumps(session)
        self.assertEqual(session["user"]["username"], "local-renderer")
        self.assertTrue(session["capabilities"]["client_local"])
        self.assertFalse(session["capabilities"]["password_change"])
        self.assertFalse(session["must_set_password"])
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("sfh_", serialized)

        with self.assertRaises(HTTPError) as password_change:
            self._request(
                self.client_url,
                "/web/api/session/password",
                method="POST",
                data=json.dumps(
                    {"current_password": "x", "new_password": "Ch1!2026"}
                ).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Cookie": cookie,
                    "X-StoryForge-CSRF": csrf,
                },
            )
        self.assertEqual(password_change.exception.code, 403)
        password_change.exception.close()

        device_id = self.client_api._state.settings.hub.device_id  # type: ignore[union-attr]
        disabled = self.host_api.set_managed_device_active(
            device_id, False, revoke_tokens=False
        )
        self.assertTrue(disabled["ok"], disabled)
        with self.assertRaises(HTTPError) as inactive_device:
            self._rpc(self.client_url, cookie, csrf, "get_bootstrap")
        self.assertEqual(inactive_device.exception.code, 401)
        inactive_device.exception.close()
        worker = self.client_api._client_web_server.worker_gateway  # type: ignore[union-attr]
        self.assertFalse(worker.health()["ready"])
        reenabled = self.host_api.set_managed_device_active(
            device_id, True, revoke_tokens=False
        )
        self.assertTrue(reenabled["ok"], reenabled)
        recovered = self.client_api.connect_hub_with_password(  # type: ignore[union-attr]
            self.host_api._hub_server.base_url,  # type: ignore[union-attr]
            "local-renderer",
            "Lr1!2026",
            "Render PC",
        )
        self.assertTrue(recovered["ok"], recovered)
        self.assertTrue(worker.health()["ready"])
        cookie, csrf, _session = self._local_session()

        tokens = [
            item
            for item in self.host_api.list_hub_user_tokens(self.member["id"])["data"]["items"]
            if not item.get("revoked_at")
        ]
        self.assertEqual(len(tokens), 1)
        self.assertTrue(self.host_api.revoke_hub_user_token(tokens[0]["id"])["ok"])
        with self.assertRaises(HTTPError) as revoked:
            self._rpc(self.client_url, cookie, csrf, "get_bootstrap")
        self.assertEqual(revoked.exception.code, 401)
        revoked.exception.close()
        self.assertFalse(worker.health()["ready"])

        recovered = self.client_api.connect_hub_with_password(  # type: ignore[union-attr]
            self.host_api._hub_server.base_url,  # type: ignore[union-attr]
            "local-renderer",
            "Lr1!2026",
            "Render PC",
        )
        self.assertTrue(recovered["ok"], recovered)
        self.assertTrue(worker.health()["ready"])
        _cookie, _csrf, recovered_session = self._local_session()
        self.assertEqual(recovered_session["user"]["username"], "local-renderer")

    def test_temporary_hub_outage_does_not_disconnect_verified_worker(self) -> None:
        assert self.client_api is not None
        worker = self.client_api._client_web_server.worker_gateway
        client = self.client_api._hub_client_snapshot()
        self.assertIsNotNone(client)
        assert client is not None

        with patch.object(
            client,
            "verify_identity",
            side_effect=HubConnectionError("temporary LAN outage"),
        ):
            status = self.client_api.get_hub_status()

        self.assertTrue(status["ok"], status)
        self.assertIs(self.client_api._hub_client_snapshot(), client)
        self.assertTrue(worker.health()["ready"])

    def test_client_media_rpc_uses_only_local_queue_and_safe_local_paths(self) -> None:
        assert self.client_api is not None
        assert self.host_api is not None
        cookie, csrf, _session = self._local_session()
        draft = self._owned_draft()
        captured: dict = {}

        def build_jobs(value: dict):
            captured.update(value)
            job = RenderJob(
                batch_id=draft["id"],
                platform_id="goodnovel",
                source_file=str(self.root / "episode.txt"),
                title="A Local Secret",
                code="B39760",
                video_folder=value["video_folder"],
                music_folder=value["music_folder"],
                output_folder=value["output_folder"],
                production_draft_id=draft["id"],
                novel_id=draft["novel_id"],
                episode_id=draft["episode_ids"][0],
                job_kind="full",
            )
            return draft, "goodnovel", 1, iter((job,))

        local_paths = {
            "video_folder": str(self.allowed_root / "videos"),
            "music_folder": str(self.allowed_root / "music"),
            "output_folder": str(self.allowed_root / "output"),
        }
        self.client_api.generate_voice_candidates = lambda *_args: {
            "ok": True,
            "data": {"candidates": [{"voice_id": "af_heart"}]},
        }
        with self._rpc(
            self.client_url,
            cookie,
            csrf,
            "generate_voice_candidates",
            [draft["novel_id"], "suspense"],
        ) as response:
            self.assertTrue(self._json(response)["ok"])

        with self._rpc(
            self.client_url,
            cookie,
            csrf,
            "get_local_runtime_snapshot",
            [],
        ) as response:
            runtime = self._json(response)
        self.assertTrue(runtime["ok"], runtime)
        self.assertIn("embedded_kokoro_ready", runtime["data"]["system"])

        with self._rpc(
            self.client_url,
            cookie,
            csrf,
            "get_local_self_check",
            [],
        ) as response:
            self_check = self._json(response)
        self.assertTrue(self_check["ok"], self_check)
        self.assertIn(
            "encoder", {item["key"] for item in self_check["data"]["checks"]}
        )

        with self._rpc(
            self.client_url,
            cookie,
            csrf,
            "set_local_tts_provider",
            ["edge_tts"],
        ) as response:
            switched = self._json(response)
        self.assertTrue(switched["ok"], switched)
        self.assertEqual(
            self.client_api._state.settings.providers.tts_provider, "edge_tts"
        )

        with patch.object(
            self.client_api._library, "build_render_job_plan", side_effect=build_jobs
        ), patch.object(self.client_api, "_validate_provider_readiness"):
            with self._rpc(
                self.client_url,
                cookie,
                csrf,
                "queue_production_draft",
                [{"draft_id": draft["id"], **local_paths}],
            ) as response:
                queued = self._json(response)
            self.assertTrue(queued["ok"], queued)
            self.assertEqual(queued["data"]["jobs"][0]["job_kind"], "full")

            deadline = time.time() + 2
            while not self.client_api._queue.list_jobs() and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(self.client_api._queue.list_jobs()), 1)
            self.assertEqual(self.host_api._queue.list_jobs(), [])
            for key, expected in local_paths.items():
                self.assertEqual(captured[key], expected)

            job = self.client_api._queue.get_job(queued["data"]["jobs"][0]["id"])
            self.assertIsNotNone(job)
            assert job is not None
            job.status = JobStatus.FAILED
            self.host_api._catalog.save_production_record(
                {"id": job.production_record_id, "status": "failed", "progress": job.progress},
                actor_user_id=self.member["id"],
            )
            with self._rpc(
                self.client_url, cookie, csrf, "retry_failed", [job.id]
            ) as response:
                retried = self._json(response)
                self.assertTrue(retried["ok"], retried)

        defaults = self.client_api._client_web_server.application._validate_draft_folders(
            {}, existing=draft
        )
        self.assertNotIn("other-worker", json.dumps(defaults))
        self.assertTrue(
            all(
                str(Path(value)).startswith(str(self.client_api._repository.data_dir))
                for value in defaults.values()
            )
        )

        second = self._owned_draft(title="Another Local Secret")
        outside = self.root / "unapproved"
        outside.mkdir()
        with self.assertRaises(HTTPError) as unsafe:
            self._rpc(
                self.client_url,
                cookie,
                csrf,
                "queue_production_draft",
                [
                    {
                        "draft_id": second["id"],
                        "video_folder": str(outside),
                        "music_folder": local_paths["music_folder"],
                        "output_folder": local_paths["output_folder"],
                    }
                ],
            )
        self.assertEqual(unsafe.exception.code, 400)
        unsafe.exception.close()

    def test_archived_job_restore_cannot_reuse_another_worker_folder(self) -> None:
        assert self.client_api is not None
        assert self.host_api is not None
        cookie, csrf, _session = self._local_session()
        draft = self._owned_draft(title="Archived Local Secret")
        job_id = "archived-local-render"
        record = self.host_api._catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": job_id,
                "status": "failed",
                "device_id": self.client_api._state.settings.hub.device_id,
                "created_by_user_id": self.member["id"],
            },
            actor_user_id=self.member["id"],
        )
        stale_root = self.root / "stale-other-worker"
        archived_job = RenderJob(
            id=job_id,
            batch_id=draft["id"],
            platform_id="goodnovel",
            source_file=str(stale_root / "episode.txt"),
            title="Archived Local Secret",
            code="B39760",
            video_folder=str(stale_root / "videos"),
            music_folder=str(stale_root / "music"),
            output_folder=str(stale_root / "output"),
            status=JobStatus.FAILED,
            production_record_id=record["id"],
            production_draft_id=draft["id"],
            novel_id=draft["novel_id"],
            episode_id=draft["episode_ids"][0],
            job_kind="full",
        )
        self.host_api._catalog.archive_job_snapshot(
            job_id,
            archived_job.to_dict(),
            actor_user_id=self.member["id"],
        )

        with self._rpc(
            self.client_url, cookie, csrf, "restore_job", [job_id]
        ) as response:
            restored = self._json(response)
        self.assertTrue(restored["ok"], restored)
        restored_job = self.client_api._queue.get_job(job_id)
        self.assertIsNotNone(restored_job)
        assert restored_job is not None
        for key in ("video_folder", "music_folder", "output_folder"):
            value = str(getattr(restored_job, key))
            self.assertNotIn("stale-other-worker", value)
            self.assertTrue(value.startswith(str(self.client_api._repository.data_dir)))

        with patch.object(self.client_api, "_validate_provider_readiness"):
            with self._rpc(
                self.client_url, cookie, csrf, "retry_failed", [job_id]
            ) as response:
                retried = self._json(response)
        self.assertTrue(retried["ok"], retried)
        for key in ("video_folder", "music_folder", "output_folder"):
            self.assertNotIn(
                "stale-other-worker", str(getattr(restored_job, key))
            )

        foreign_id = "archived-foreign-render"
        foreign_record = self.host_api._catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": foreign_id,
                "status": "failed",
                "device_id": "another-workstation",
                "created_by_user_id": self.member["id"],
            },
            actor_user_id=self.member["id"],
        )
        foreign_job = RenderJob.from_dict(
            {
                **archived_job.to_dict(),
                "id": foreign_id,
                "production_record_id": foreign_record["id"],
            }
        )
        self.host_api._catalog.archive_job_snapshot(
            foreign_id,
            foreign_job.to_dict(),
            actor_user_id=self.member["id"],
        )
        with self.assertRaises(HTTPError) as foreign_restore:
            self._rpc(
                self.client_url, cookie, csrf, "restore_job", [foreign_id]
            )
        self.assertEqual(foreign_restore.exception.code, 403)
        foreign_restore.exception.close()
        self.assertIsNone(self.client_api._queue.get_job(foreign_id))

    def test_local_listener_rejects_untrusted_host_origin_and_session_flood(self) -> None:
        hostile = Request(
            self.client_url + "/web/api/session",
            headers={"Host": "attacker.example"},
        )
        with self.assertRaises(HTTPError) as bad_host:
            urlopen(hostile, timeout=5)
        self.assertEqual(bad_host.exception.code, 403)
        bad_host.exception.close()

        cookie, csrf, _session = self._local_session()
        with self.assertRaises(HTTPError) as bad_origin:
            self._request(
                self.client_url,
                "/web/api/rpc",
                method="POST",
                data=json.dumps({"method": "get_bootstrap", "args": []}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Cookie": cookie,
                    "X-StoryForge-CSRF": csrf,
                    "Origin": "https://attacker.example",
                },
            )
        self.assertEqual(bad_origin.exception.code, 403)
        bad_origin.exception.close()

        with patch("storyforge.web.MAX_SESSIONS_PER_IP", 1):
            with self.assertRaises(HTTPError) as capacity:
                self._request(self.client_url, "/web/api/session")
        self.assertEqual(capacity.exception.code, 429)
        capacity.exception.close()

    def test_client_skips_local_text_key_but_keeps_local_tts_preflight(self) -> None:
        assert self.client_api is not None
        providers = self.client_api._state.settings.providers
        providers.text_provider = "cloudflare"
        providers.text_endpoint = ""
        providers.text_api_key = ""
        providers.tts_provider = "deepgram"
        providers.tts_api_key = ""
        with self.assertRaisesRegex(ValueError, "Deepgram"):
            self.client_api._validate_provider_readiness()
        providers.tts_api_key = "worker-local-tts-key"
        self.client_api._validate_provider_readiness()

    def test_client_local_employee_can_update_only_its_own_installation(self) -> None:
        assert self.client_api is not None
        cookie, csrf, session = self._local_session()
        self.assertIn("updates.manage_own", session["permissions"])
        status = {
            "state": "downloaded",
            "current_version": "0.4.0-rc4",
            "available_version": "0.4.1",
            "downloaded": True,
        }
        for method, target in (
            ("check_for_updates", "check_now"),
            ("download_update", "download"),
            ("schedule_update_on_restart", "schedule_on_restart"),
            ("cancel_scheduled_update", "cancel_schedule"),
        ):
            with self.subTest(method=method), patch.object(
                self.client_api._update_manager, target, return_value=dict(status)
            ):
                with self._rpc(
                    self.client_url, cookie, csrf, method, []
                ) as response:
                    result = self._json(response)
                self.assertTrue(result["ok"], result)

        with patch.object(self.client_api._update_manager, "start"), patch.object(
            self.client_api._update_manager, "wake"
        ):
            with self._rpc(
                self.client_url,
                cookie,
                csrf,
                "save_local_update_preferences",
                [
                    {
                        "auto_update_enabled": True,
                        "auto_download_updates": False,
                        "update_check_minutes": 5,
                    }
                ],
            ) as response:
                saved = self._json(response)
        self.assertTrue(saved["ok"], saved)
        hub = self.client_api._state.settings.hub
        self.assertTrue(hub.auto_update_enabled)
        self.assertFalse(hub.auto_download_updates)
        self.assertEqual(hub.update_check_minutes, 5)

        for method, args in (
            ("publish_update", ["release.zip", "9.9.9", "Denied"]),
            ("save_settings", [{"hub": {"mode": "host"}}]),
        ):
            with self.subTest(method=method), self.assertRaises(HTTPError) as denied:
                self._rpc(self.client_url, cookie, csrf, method, args)
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()

    def test_host_browser_still_rejects_media_rpc(self) -> None:
        assert self.host_api is not None
        host_web = self.host_api._enable_web_access(self.ui)
        login = json.dumps(
            {
                "username": "web-owner",
                "password": "Wo1!2026",
                "remember": False,
            }
        ).encode()
        with self._request(
            host_web["local_url"],
            "/web/api/session/login",
            method="POST",
            data=login,
            headers={"Content-Type": "application/json"},
        ) as response:
            payload = self._json(response)
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        csrf = payload["data"]["csrf_token"]
        local_queue_calls = {
            "get_jobs": [],
            "get_archived_jobs": [],
            "archive_job": ["local-job"],
            "restore_job": ["local-job"],
            "archive_finished_jobs": [],
            "clear_finished_jobs": [],
        }
        for method, arguments in local_queue_calls.items():
            with self.subTest(method=method), self.assertRaises(HTTPError) as denied:
                self._rpc(
                    host_web["local_url"], cookie, csrf, method, arguments
                )
            self.assertEqual(denied.exception.code, 403)
            error = self._json(denied.exception)
            self.assertIn("请在制作电脑客户端执行", error["error"])
            denied.exception.close()

        # The durable Hub ledger remains a shared interface; only the host's
        # process-local queue is unavailable to a Hub-hosted browser.
        with self._rpc(
            host_web["local_url"],
            cookie,
            csrf,
            "get_production_record_groups",
            [],
        ) as response:
            shared_records = self._json(response)
        self.assertTrue(shared_records["ok"], shared_records)

        with self.assertRaises(HTTPError) as denied:
            self._rpc(
                host_web["local_url"],
                cookie,
                csrf,
                "generate_voice_candidates",
                ["novel", "suspense"],
            )
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()

        with self.assertRaises(HTTPError) as local_setting_denied:
            self._rpc(
                host_web["local_url"],
                cookie,
                csrf,
                "set_local_tts_provider",
                ["edge_tts"],
            )
        self.assertEqual(local_setting_denied.exception.code, 403)
        local_setting_denied.exception.close()

    def test_client_local_producer_can_read_and_clear_its_local_queue(self) -> None:
        cookie, csrf, _session = self._local_session()
        for method in (
            "get_jobs",
            "get_archived_jobs",
            "archive_finished_jobs",
            "clear_finished_jobs",
        ):
            with self.subTest(method=method), self._rpc(
                self.client_url, cookie, csrf, method
            ) as response:
                result = self._json(response)
            self.assertTrue(result["ok"], result)

    def test_client_local_web_scopes_production_presets_to_logged_in_employee(self) -> None:
        assert self.host_api is not None
        owner = self.host_api._catalog._web_user_by_username("storyforge-owner")
        self.assertIsNotNone(owner)
        admin_personal = self.host_api._catalog.save_production_preset(
            {
                "name": "团队网页方案",
                "recipe": {"production_settings": {"output_fps": 60}},
            },
            actor_user_id=owner["id"],
        )
        cookie, csrf, _session = self._local_session()
        with self._rpc(
            self.client_url, cookie, csrf, "get_production_presets"
        ) as response:
            listing = self._json(response)
        self.assertTrue(listing["ok"], listing)
        self.assertNotIn(
            admin_personal["id"],
            {item["id"] for item in listing["data"]["items"]},
        )

        with self._rpc(
            self.client_url,
            cookie,
            csrf,
            "save_production_preset",
            [
                {
                    "name": "我的网页方案",
                    "recipe": {"production_settings": {"output_fps": 60}},
                }
            ],
        ) as response:
            created = self._json(response)
        self.assertTrue(created["ok"], created)
        self.assertEqual(created["data"]["owner_user_id"], self.member["id"])
        self.assertTrue(created["data"]["owned_by_current_user"])


if __name__ == "__main__":
    unittest.main()
