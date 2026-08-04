from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from storyforge.api import StoryForgeApi
from storyforge.catalog import CatalogConflictError, CatalogRepository
from storyforge.config import SettingsRepository
from storyforge.hub import HubRemoteError
from storyforge.jobs import JobQueue
from storyforge.models import JobStatus, PlatformProfile, RenderJob


class ProductionWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        # Workflow tests validate durable records, leases, retries and shutdown
        # ordering.  They must not inherit the host workstation's current disk
        # pressure; resource admission has a dedicated governance test module.
        admission_patch = patch(
            "storyforge.worker.default_heavy_job_admission",
            return_value={"allowed": True, "reason": "", "message": ""},
        )
        admission_patch.start()
        self.addCleanup(admission_patch.stop)

    def _wait_for(self, api: StoryForgeApi, expected: set[str]) -> list[dict]:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            jobs = api.get_jobs()["data"]
            if jobs and {item["status"] for item in jobs}.issubset(expected):
                return jobs
            time.sleep(0.02)
        self.fail(f"queue did not reach {expected}: {api.get_jobs()['data']}")

    def _durable_retry_job(
        self,
        root: Path,
        *,
        status: JobStatus = JobStatus.FAILED,
        job_kind: str = "full",
    ) -> tuple[StoryForgeApi, CatalogRepository, JobQueue, RenderJob, dict]:
        queue = JobQueue(lambda *_args: str(root / "done.mp4"))
        catalog = CatalogRepository(root / "catalog.sqlite3")
        api = StoryForgeApi(
            repository=SettingsRepository(root / "state"),
            queue=queue,
            catalog=catalog,
        )
        platform_value = api.save_platform(
            {"id": "goodnovel", "name": "GoodNovel"}
        )["data"]
        platform = PlatformProfile(
            id=str(platform_value["id"]), name=str(platform_value["name"])
        )
        novel = api.import_novel_text(
            {
                "title": "Retry lease story",
                "text": "A durable retry must own its production record.",
                "language": "en",
            }
        )["data"]["novel"]
        api.save_novel_binding(
            {"novel_id": novel["id"], "platform_id": platform.id}
        )
        promo = api.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": platform.id,
                "code": "RETRY01",
            }
        )["data"]["promo_code"]
        episode_id = str(novel["episodes"][0]["id"])
        draft = api.save_production_draft(
            {
                "novel_id": novel["id"],
                "platform_id": platform.id,
                "promo_code_id": promo["id"],
                "episode_ids": [episode_id],
                "target_video_count": 1,
                "video_folder": str(root),
                "music_folder": str(root),
                "output_folder": str(root),
            }
        )["data"]["draft"]
        record = catalog.save_production_record(
            {
                "draft_id": draft["id"],
                "job_id": "retry-job",
                "device_id": api._current_device_id(),
                "status": status.value,
            }
        )
        job = RenderJob(
            id="retry-job",
            batch_id=str(record["batch_id"]),
            platform_id=platform.id,
            source_file=__file__,
            title=str(novel["title"]),
            code="RETRY01",
            video_folder=str(root),
            music_folder=str(root),
            output_folder=str(root),
            novel_id=str(novel["id"]),
            episode_id=episode_id,
            production_record_id=str(record["id"]),
            production_draft_id=str(draft["id"]),
            status=status,
            job_kind=job_kind,
        )
        queue.enqueue_jobs([job], platform)
        return api, catalog, queue, job, record

    def _queue_shutdown_job(
        self,
        root: Path,
        processor,
    ) -> tuple[StoryForgeApi, CatalogRepository, JobQueue, str]:
        video = root / "video"
        music = root / "music"
        output = root / "output"
        video.mkdir()
        music.mkdir()
        queue = JobQueue(processor)
        catalog = CatalogRepository(root / "catalog.sqlite3")
        api = StoryForgeApi(
            repository=SettingsRepository(root / "state"),
            queue=queue,
            catalog=catalog,
        )
        api._state.settings.providers.kokoro_endpoint = "http://127.0.0.1:8880"
        platform = api.save_platform(
            {"id": "goodnovel", "name": "GoodNovel"}
        )["data"]
        novel = api.import_novel_text(
            {
                "title": "Shutdown Safety",
                "text": "The worker must stop before another computer can claim this story.",
                "language": "en",
            }
        )["data"]["novel"]
        api.save_novel_binding(
            {"novel_id": novel["id"], "platform_id": platform["id"]}
        )
        promo = api.add_promo_code(
            {
                "novel_id": novel["id"],
                "platform_id": platform["id"],
                "code": "SAFE001",
            }
        )["data"]["promo_code"]
        api.lock_novel_voice(
            novel["id"],
            {
                "provider": "local_kokoro",
                "voice_id": "af_heart",
                "label": "American suspense",
            },
        )
        draft = api.save_production_draft(
            {
                "novel_id": novel["id"],
                "platform_id": platform["id"],
                "promo_code_id": promo["id"],
                "episode_ids": [novel["episodes"][0]["id"]],
                "variant_count": 1,
                "video_folder": str(video),
                "music_folder": str(music),
                "output_folder": str(output),
            }
        )["data"]["draft"]
        queued = api.queue_production_draft({"draft_id": draft["id"]})
        self.assertTrue(queued["ok"], queued)
        record_id = str(queue.list_jobs()[0]["production_record_id"])
        return api, catalog, queue, record_id

    def test_shutdown_releases_lease_only_after_worker_is_confirmed_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entered = threading.Event()
            release_processor = threading.Event()

            def processor(_job, _platform, _progress):
                entered.set()
                self.assertTrue(release_processor.wait(4))
                return str(root / "output" / "late.mp4")

            api, catalog, queue, record_id = self._queue_shutdown_job(root, processor)
            self.assertTrue(entered.wait(2))
            release_observations: list[tuple[str, bool]] = []
            original_release = catalog.release_record_lease

            def observed_release(
                target_record_id: str, device_id: str, **kwargs
            ):
                worker = queue._worker
                release_observations.append(
                    (target_record_id, bool(worker and worker.is_alive()))
                )
                return original_release(target_record_id, device_id, **kwargs)

            with patch.object(
                catalog, "release_record_lease", side_effect=observed_release
            ):
                shutdown_thread = threading.Thread(target=api._shutdown, daemon=True)
                shutdown_thread.start()
                deadline = time.monotonic() + 2
                while (
                    queue.get_job(queue.list_jobs()[0]["id"]).status
                    != JobStatus.CANCELLED
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertEqual(
                    queue.get_job(queue.list_jobs()[0]["id"]).status,
                    JobStatus.CANCELLED,
                )
                self.assertEqual(release_observations, [])
                # Cancellation happens first; the processor is allowed to
                # unwind, and only the subsequent worker join permits release.
                release_processor.set()
                shutdown_thread.join(timeout=4)
                self.assertFalse(shutdown_thread.is_alive())

            self.assertTrue(release_observations)
            self.assertTrue(all(not worker_alive for _, worker_alive in release_observations))
            record = catalog.get_record(record_id)
            self.assertEqual(record["status"], "interrupted")
            self.assertFalse(record["lease_owner_device"])

    def test_shutdown_timeout_keeps_lease_until_worker_later_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entered = threading.Event()
            release_processor = threading.Event()

            def processor(_job, _platform, _progress):
                entered.set()
                self.assertTrue(release_processor.wait(4))
                return str(root / "output" / "late.mp4")

            api, catalog, queue, record_id = self._queue_shutdown_job(root, processor)
            self.assertTrue(entered.wait(2))
            with patch("storyforge.api.QUEUE_SHUTDOWN_TIMEOUT_SECONDS", 0.05):
                api._shutdown()

            held = catalog.get_record(record_id)
            self.assertTrue(held["lease_owner_device"])
            self.assertIn(held["status"], {"queued", "preflight", "running"})

            release_processor.set()
            worker = queue._worker
            if worker is not None:
                worker.join(timeout=3)
            self.assertFalse(worker and worker.is_alive())
            api._shutdown()
            released = catalog.get_record(record_id)
            self.assertEqual(released["status"], "interrupted")
            self.assertFalse(released["lease_owner_device"])

    def test_shutdown_catalog_error_keeps_lease_and_still_stops_services(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entered = threading.Event()
            release_processor = threading.Event()

            def processor(_job, _platform, _progress):
                entered.set()
                self.assertTrue(release_processor.wait(4))
                return str(root / "output" / "late.mp4")

            api, catalog, queue, record_id = self._queue_shutdown_job(root, processor)
            self.assertTrue(entered.wait(2))
            hub_server = Mock()
            client_web_server = Mock()
            api._hub_server = hub_server
            api._client_web_server = client_web_server

            def release_after_cancel() -> None:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    item = queue.get_job(queue.list_jobs()[0]["id"])
                    if item is not None and item.status == JobStatus.CANCELLED:
                        release_processor.set()
                        return
                    time.sleep(0.01)

            release_thread = threading.Thread(target=release_after_cancel, daemon=True)
            release_thread.start()
            with patch.object(
                api,
                "_sync_one_job_record",
                side_effect=sqlite3.OperationalError("synthetic catalog outage"),
            ):
                api._shutdown()
            release_thread.join(timeout=2)

            self.assertFalse(release_thread.is_alive())
            hub_server.stop.assert_called_once_with()
            client_web_server.stop.assert_called_once_with()
            self.assertIsNone(api._client_web_server)
            held = catalog.get_record(record_id)
            self.assertTrue(held["lease_owner_device"])

    def test_library_draft_runs_full_jobs_without_sample_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "video"
            music = root / "music"
            output = root / "output"
            video.mkdir()
            music.mkdir()
            processed: list[tuple[int, str]] = []

            def processor(job, _platform, _progress):
                processed.append((job.variant_index, job.job_kind))
                # A failed full video must not stop the remaining batch.
                if job.variant_index == 1:
                    raise RuntimeError("forced render failure")
                target = output / f"E{job.episode_number:03d}-V{job.variant_index:02d}.mp4"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"video")
                return str(target)

            repository = SettingsRepository(root / "state")
            catalog = CatalogRepository(root / "catalog.sqlite3")
            api = StoryForgeApi(
                repository=repository,
                queue=JobQueue(processor),
                catalog=catalog,
            )
            api._state.settings.providers.kokoro_endpoint = "http://127.0.0.1:8880"
            platform = api.save_platform(
                {"id": "goodnovel", "name": "GoodNovel"}
            )["data"]
            novel = api.import_novel_text(
                {
                    "title": "The Midnight Call",
                    "text": "The phone rang at ten. A stranger whispered my husband's name.",
                    "language": "en",
                }
            )["data"]["novel"]
            api.save_novel_binding(
                {"novel_id": novel["id"], "platform_id": platform["id"]}
            )
            promo = api.add_promo_code(
                {
                    "novel_id": novel["id"],
                    "platform_id": platform["id"],
                    "code": "B73165",
                }
            )["data"]["promo_code"]
            api.lock_novel_voice(
                novel["id"],
                {
                    "provider": "local_kokoro",
                    "voice_id": "af_heart",
                    "label": "American suspense",
                },
            )
            draft = api.save_production_draft(
                {
                    "novel_id": novel["id"],
                    "platform_id": platform["id"],
                    "promo_code_id": promo["id"],
                    "episode_ids": [novel["episodes"][0]["id"]],
                    "variant_count": 2,
                    "video_folder": str(video),
                    "music_folder": str(music),
                    "output_folder": str(output),
                }
            )["data"]["draft"]

            queued = api.queue_production_draft({"draft_id": draft["id"]})
            self.assertTrue(queued["ok"], queued)
            self.assertEqual(queued["data"]["total_videos"], 2)
            self.assertFalse(queued["data"]["preview_required"])
            self.assertEqual(queued["data"]["preview_job_id"], "")
            self.assertEqual(
                {item["job_kind"] for item in queued["data"]["jobs"]}, {"full"}
            )
            self.assertTrue(
                {item["status"] for item in queued["data"]["jobs"]}.isdisjoint(
                    {"awaiting_approval", "waiting_preview"}
                )
            )

            jobs = self._wait_for(api, {"failed", "completed"})
            self.assertEqual({item["job_kind"] for item in jobs}, {"full"})
            self.assertEqual({item["status"] for item in jobs}, {"failed", "completed"})
            self.assertTrue(all(not item["preview_file"] for item in jobs))
            self.assertEqual(processed, [(1, "full"), (2, "full")])
            records = [
                item
                for item in catalog.list_records(limit=20)["items"]
                if not (item.get("metadata") or {}).get("lease_gate")
            ]
            self.assertEqual(len(records), 2)
            self.assertEqual(
                {item["status"] for item in records}, {"failed", "completed"}
            )
            self.assertTrue(
                all(
                    (item.get("metadata") or {}).get("preview_required") is False
                    for item in records
                )
            )
            self.assertTrue(
                all(
                    isinstance((item.get("metadata") or {}).get("job_snapshot"), dict)
                    for item in records
                ),
                "small batches must also retain restart snapshots",
            )
            completed_record = next(
                item for item in records if item["status"] == "completed"
            )
            self.assertTrue(Path(completed_record["output_path"]).is_file())
            gate = next(
                item
                for item in catalog.list_records(limit=20)["items"]
                if (item.get("metadata") or {}).get("lease_gate")
            )
            self.assertTrue(all(not item["lease_owner_device"] for item in records))
            gate = catalog.get_record(gate["id"])
            self.assertEqual(gate["status"], "skipped")
            self.assertFalse(gate["lease_owner_device"])

            first_batch_id = queued["data"]["jobs"][0]["batch_id"]
            self.assertNotEqual(first_batch_id, draft["id"])
            first_group = catalog.list_record_groups(batch_id=first_batch_id)
            self.assertEqual(first_group["total_records"], 2)

            # Reusing a saved draft creates a new production dossier.  The
            # draft is configuration; it is not the durable batch identity.
            with patch.object(api._queue, "start"):
                queued_again = api.queue_production_draft({"draft_id": draft["id"]})
            self.assertTrue(queued_again["ok"], queued_again)
            second_batch_id = queued_again["data"]["jobs"][0]["batch_id"]
            self.assertNotEqual(second_batch_id, first_batch_id)
            self.assertEqual(
                catalog.list_record_groups(batch_id=second_batch_id)["total_records"],
                2,
            )
            api.cancel_queue()

    def test_large_batch_returns_window_while_all_tasks_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "video"
            music = root / "music"
            output = root / "output"
            video.mkdir()
            music.mkdir()
            queue = JobQueue(lambda *_args: "")
            catalog = CatalogRepository(root / "catalog.sqlite3")
            api = StoryForgeApi(
                repository=SettingsRepository(root / "state"),
                queue=queue,
                catalog=catalog,
            )
            api._state.settings.providers.kokoro_endpoint = "http://127.0.0.1:8880"
            platform = api.save_platform(
                {"id": "goodnovel", "name": "GoodNovel"}
            )["data"]
            novel = api.import_novel_text(
                {
                    "title": "A Large Story",
                    "text": "At midnight, she learned the secret that changed everything.",
                    "language": "en",
                }
            )["data"]["novel"]
            api.save_novel_binding(
                {"novel_id": novel["id"], "platform_id": platform["id"]}
            )
            promo = api.add_promo_code(
                {
                    "novel_id": novel["id"],
                    "platform_id": platform["id"],
                    "code": "BIGQUEUE",
                }
            )["data"]["promo_code"]
            api.lock_novel_voice(
                novel["id"],
                {"provider": "local_kokoro", "voice_id": "af_heart"},
            )
            draft = api.save_production_draft(
                {
                    "novel_id": novel["id"],
                    "platform_id": platform["id"],
                    "promo_code_id": promo["id"],
                    "episode_ids": [novel["episodes"][0]["id"]],
                    "target_video_count": 129,
                    "video_folder": str(video),
                    "music_folder": str(music),
                    "output_folder": str(output),
                }
            )["data"]["draft"]

            with patch.object(queue, "start"):
                queued = api.queue_production_draft({"draft_id": draft["id"]})

            self.assertTrue(queued["ok"], queued)
            payload = queued["data"]
            self.assertEqual(payload["total_videos"], 129)
            self.assertTrue(payload["jobs_truncated"])
            self.assertEqual(payload["returned_jobs"], 4)
            self.assertEqual(len(payload["jobs"]), 4)
            self.assertEqual(len(queue.list_jobs()), 4)
            self.assertEqual(
                {item["batch_total_count"] for item in payload["jobs"]}, {129}
            )
            self.assertEqual(
                [item["batch_ordinal"] for item in payload["jobs"]], [1, 2, 3, 4]
            )
            records = [
                item
                for item in catalog.list_records(limit=500)["items"]
                if not bool((item.get("metadata") or {}).get("lease_gate"))
            ]
            self.assertEqual(len(records), 129)
            durable_batch_ids = {str(item["batch_id"]) for item in records}
            self.assertEqual(len(durable_batch_ids), 1)
            durable_batch_id = durable_batch_ids.pop()
            self.assertNotEqual(durable_batch_id, draft["id"])
            self.assertTrue(
                all(item["batch_summary"]["total"] == 129 for item in payload["jobs"])
            )
            self.assertTrue(
                all(item["batch_summary"]["unfinished"] == 129 for item in payload["jobs"])
            )
            self.assertEqual(
                {str(item["batch_id"]) for item in payload["jobs"]},
                {durable_batch_id},
            )
            self.assertEqual(
                catalog.list_record_groups(batch_id=durable_batch_id)["total_records"],
                129,
            )
            self.assertEqual(
                sum(
                    1
                    for item in records
                    if isinstance((item.get("metadata") or {}).get("job_snapshot"), dict)
                ),
                129,
            )
            self.assertTrue(
                all(
                    str((item.get("metadata") or {})["job_snapshot"]["batch_id"])
                    == durable_batch_id
                    and str(
                        (item.get("metadata") or {})["job_snapshot"][
                            "production_record_id"
                        ]
                    )
                    == str(item["id"])
                    for item in records
                )
            )
            gate = next(
                item
                for item in catalog.list_records(limit=500)["items"]
                if bool((item.get("metadata") or {}).get("lease_gate"))
            )
            self.assertEqual(
                gate["metadata"]["durable_batch_id"], durable_batch_id
            )
            for job in [queue.get_job(item["id"]) for item in payload["jobs"]]:
                self.assertIsNotNone(job)
                job.status = JobStatus.COMPLETED
                api._sync_one_job_record(job)
            api._release_finished_draft_gates()
            held_gate = catalog.get_record(gate["id"])
            self.assertTrue(held_gate["lease_owner_device"])
            self.assertEqual(held_gate["status"], "queued")
            api.cancel_queue()
            released_gate = catalog.get_record(gate["id"])
            self.assertEqual(released_gate["status"], "skipped")
            self.assertFalse(released_gate["lease_owner_device"])

    def test_best_effort_sync_cannot_publish_terminal_before_terminal_callback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            queue = JobQueue(lambda *_args: "")
            catalog = CatalogRepository(root / "catalog.sqlite3")
            api = StoryForgeApi(
                repository=SettingsRepository(root / "state"),
                queue=queue,
                catalog=catalog,
            )
            platform_value = api.save_platform(
                {"id": "goodnovel", "name": "GoodNovel"}
            )["data"]
            platform = PlatformProfile(
                id=str(platform_value["id"]), name=str(platform_value["name"])
            )
            novel = api.import_novel_text(
                {
                    "title": "Terminal race",
                    "text": "The terminal callback must remain the only final writer.",
                    "language": "en",
                }
            )["data"]["novel"]
            binding = api.save_novel_binding(
                {"novel_id": novel["id"], "platform_id": platform.id}
            )["data"]
            promo = api.add_promo_code(
                {
                    "novel_id": novel["id"],
                    "platform_id": platform.id,
                    "code": "RACE001",
                }
            )["data"]["promo_code"]
            draft = api.save_production_draft(
                {
                    "novel_id": novel["id"],
                    "platform_id": platform.id,
                    "promo_code_id": promo["id"],
                    "episode_ids": [novel["episodes"][0]["id"]],
                    "target_video_count": 1,
                    "video_folder": str(root),
                    "music_folder": str(root),
                    "output_folder": str(root),
                }
            )["data"]["draft"]
            record = catalog.save_production_record(
                {
                    "draft_id": draft["id"],
                    "job_id": "terminal-race-job",
                    "device_id": api._current_device_id(),
                    "status": "queued",
                }
            )
            job = RenderJob(
                id="terminal-race-job",
                batch_id=str(record["batch_id"]),
                platform_id=platform.id,
                source_file=__file__,
                title="Terminal race",
                code="RACE001",
                video_folder=str(root),
                music_folder=str(root),
                output_folder=str(root),
                production_record_id=str(record["id"]),
                production_draft_id=str(draft["id"]),
                status=JobStatus.COMPLETED,
                progress=1.0,
            )
            queue.enqueue_jobs([job], platform)
            api._claim_record_lease(str(record["id"]))
            generation = api._lease_generation_for(str(record["id"]))

            # UI/action projections are best-effort non-terminal snapshots.
            # The Worker terminal callback alone owns final persistence and
            # lease release, otherwise its later callback loses the fencing
            # generation and Hub returns HTTP 409.
            api._sync_job_records()

            self.assertEqual(
                api._lease_generation_for(str(record["id"])), generation
            )
            self.assertEqual(catalog.get_record(str(record["id"]))["status"], "queued")
            api._sync_terminal_job_record(job)
            self.assertEqual(
                catalog.get_record(str(record["id"]))["status"], "completed"
            )
            self.assertEqual(api._lease_generation_for(str(record["id"])), 0)

    def test_claim_record_lease_rejects_missing_fencing_generation(self) -> None:
        api = StoryForgeApi.__new__(StoryForgeApi)
        api._lease_lock = threading.RLock()
        api._leased_records = set()
        api._lease_generations = {}
        api._lease_health = {}
        api._superseded_lease_records = set()
        api._current_device_id = lambda: "worker-a"
        api._catalog = Mock()
        api._catalog.claim_record_lease.return_value = {"record": {}}
        api._ensure_lease_heartbeat = Mock()

        with self.assertRaisesRegex(RuntimeError, "lease generation"):
            api._claim_record_lease("record-without-generation")

        self.assertNotIn("record-without-generation", api._leased_records)
        self.assertNotIn("record-without-generation", api._lease_generations)
        self.assertNotIn("record-without-generation", api._lease_health)
        api._ensure_lease_heartbeat.assert_not_called()

    def test_retry_failed_rejects_historical_preview_without_requeueing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            processed: list[str] = []

            def processor(job, _platform, _progress):
                processed.append(job.id)
                return str(root / "unexpected.mp4")

            queue = JobQueue(processor)
            preview = RenderJob(
                id="historical-preview",
                batch_id="legacy-draft",
                platform_id="goodnovel",
                source_file=str(root / "story.txt"),
                title="Legacy story",
                code="B73165",
                video_folder=str(root),
                music_folder=str(root),
                output_folder=str(root),
                status=JobStatus.FAILED,
                job_kind="preview",
                message="old preview failed",
            )
            queue.enqueue_jobs(
                [preview],
                PlatformProfile(id="goodnovel", name="GoodNovel"),
            )
            api = StoryForgeApi(
                repository=SettingsRepository(root / "state"),
                queue=queue,
                catalog=CatalogRepository(root / "catalog.sqlite3"),
            )
            try:
                result = api.retry_failed(preview.id)

                self.assertFalse(result["ok"])
                self.assertIn("历史预览任务不再重试", result["error"])
                self.assertEqual(preview.status, JobStatus.FAILED)
                self.assertEqual(preview.job_kind, "preview")
                self.assertEqual(preview.message, "old preview failed")
                self.assertEqual(processed, [])
            finally:
                api._shutdown()

    def test_retry_failed_atomically_claims_durable_record_before_requeue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            api, catalog, queue, job, record = self._durable_retry_job(Path(temp))
            try:
                with (
                    patch.object(queue, "start"),
                    patch.object(api, "_validate_provider_readiness"),
                ):
                    result = api.retry_failed(job.id)

                self.assertTrue(result["ok"], result)
                retried = catalog.get_record(str(record["id"]))
                generation = api._lease_generation_for(str(record["id"]))
                self.assertEqual(job.status, JobStatus.QUEUED)
                self.assertEqual(retried["status"], "queued")
                self.assertEqual(retried["current_attempt"], 2)
                self.assertEqual(
                    retried["lease_owner_device"], api._current_device_id()
                )
                self.assertGreater(generation, 0)
                self.assertEqual(retried["lease_generation"], generation)
                api._sync_one_job_record(job)
            finally:
                api._shutdown()

    def test_retry_claim_conflict_does_not_modify_local_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            api, catalog, queue, job, record = self._durable_retry_job(Path(temp))
            try:
                with catalog._write_connection() as connection:
                    connection.execute(
                        """
                        UPDATE production_records
                        SET lease_owner_device = 'other-process',
                            lease_generation = lease_generation + 1,
                            lease_expires_at = '2999-01-01T00:00:00+00:00',
                            heartbeat_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (record["id"],),
                    )
                with (
                    patch.object(queue, "start") as start,
                    patch.object(api, "_validate_provider_readiness"),
                ):
                    result = api.retry_failed(job.id)

                self.assertFalse(result["ok"])
                self.assertEqual(job.status, JobStatus.FAILED)
                self.assertEqual(catalog.get_record(str(record["id"]))["status"], "failed")
                self.assertEqual(api._lease_generation_for(str(record["id"])), 0)
                start.assert_not_called()
            finally:
                api._shutdown()

    def test_terminal_preview_regeneration_reopens_and_claims_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            api, catalog, queue, job, record = self._durable_retry_job(
                Path(temp), job_kind="preview"
            )
            try:
                with (
                    patch.object(queue, "start"),
                    patch.object(api, "_validate_provider_readiness"),
                ):
                    result = api.regenerate_preview(job.id)

                self.assertTrue(result["ok"], result)
                retried = catalog.get_record(str(record["id"]))
                generation = api._lease_generation_for(str(record["id"]))
                self.assertEqual(job.status, JobStatus.QUEUED)
                self.assertEqual(retried["status"], "queued")
                self.assertEqual(retried["current_attempt"], 2)
                self.assertGreater(generation, 0)
                self.assertEqual(retried["lease_generation"], generation)
                api._sync_one_job_record(job)
            finally:
                api._shutdown()

    def test_awaiting_preview_regeneration_reuses_current_attempt_and_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            api, catalog, queue, job, record = self._durable_retry_job(
                Path(temp),
                status=JobStatus.AWAITING_APPROVAL,
                job_kind="preview",
            )
            try:
                api._claim_record_lease(str(record["id"]))
                generation = api._lease_generation_for(str(record["id"]))
                with (
                    patch.object(queue, "start"),
                    patch.object(api, "_validate_provider_readiness"),
                ):
                    result = api.regenerate_preview(job.id)

                self.assertTrue(result["ok"], result)
                current = catalog.get_record(str(record["id"]))
                self.assertEqual(current["current_attempt"], 1)
                self.assertEqual(current["lease_generation"], generation)
                self.assertEqual(
                    api._lease_generation_for(str(record["id"])), generation
                )
            finally:
                api._shutdown()

    def test_retry_cancelled_active_job_does_not_reopen_durable_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entered = threading.Event()
            release = threading.Event()

            def processor(_job, _platform, _progress):
                entered.set()
                self.assertTrue(release.wait(2))
                return str(root / "late.mp4")

            queue = JobQueue(processor)
            job = RenderJob(
                id="cancelled-active",
                batch_id="batch-1",
                platform_id="goodnovel",
                source_file=str(root / "story.txt"),
                title="Story",
                code="B73165",
                video_folder=str(root),
                music_folder=str(root),
                output_folder=str(root),
                production_record_id="record-1",
            )
            queue.enqueue_jobs(
                [job], PlatformProfile(id="goodnovel", name="GoodNovel")
            )
            api = StoryForgeApi(
                repository=SettingsRepository(root / "state"),
                queue=queue,
                catalog=CatalogRepository(root / "catalog.sqlite3"),
            )
            try:
                queue.start()
                self.assertTrue(entered.wait(2))
                queue.cancel_jobs({job.id})

                with (
                    patch.object(api, "_require_job_record_access", return_value={}),
                    patch.object(api._catalog, "begin_record_retry") as begin_retry,
                ):
                    result = api.retry_failed(job.id)

                self.assertFalse(result["ok"])
                self.assertIn("仍在停止", result["error"])
                begin_retry.assert_not_called()
                self.assertEqual(job.status, JobStatus.CANCELLED)
            finally:
                release.set()
                worker = queue._worker
                if worker is not None:
                    worker.join(timeout=2)
                api._shutdown()

    def test_restart_recovers_queued_snapshot_and_marks_running_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("video", "music", "output"):
                (root / name).mkdir()
            processed: list[str] = []
            queue = JobQueue(
                lambda job, _platform, _progress: processed.append(job.id)
                or str(root / "output" / f"{job.id}.mp4")
            )
            repository = SettingsRepository(root / "state")
            catalog = CatalogRepository(root / "catalog.sqlite3")
            api = StoryForgeApi(repository=repository, queue=queue, catalog=catalog)
            self.addCleanup(api._shutdown)
            platform = api.save_platform(
                {"id": "goodnovel", "name": "GoodNovel"}
            )["data"]
            novel = api.import_novel_text(
                {
                    "title": "Restart Story",
                    "text": "A secret survives the restart.",
                    "language": "en",
                }
            )["data"]["novel"]
            api.save_novel_binding(
                {"novel_id": novel["id"], "platform_id": platform["id"]}
            )
            promo = api.add_promo_code(
                {
                    "novel_id": novel["id"],
                    "platform_id": platform["id"],
                    "code": "R10001",
                }
            )["data"]["promo_code"]
            draft = api.save_production_draft(
                {
                    "novel_id": novel["id"],
                    "platform_id": platform["id"],
                    "promo_code_id": promo["id"],
                    "episode_ids": [novel["episodes"][0]["id"]],
                    "variant_count": 2,
                    "video_folder": str(root / "video"),
                    "music_folder": str(root / "music"),
                    "output_folder": str(root / "output"),
                }
            )["data"]["draft"]

            def snapshot(job_id: str, variant: int, status: JobStatus) -> dict:
                return RenderJob(
                    id=job_id,
                    batch_id=draft["id"],
                    platform_id=platform["id"],
                    source_file=__file__,
                    title="Restart Story",
                    code="R10001",
                    video_folder=str(root / "video"),
                    music_folder=str(root / "music"),
                    output_folder=str(root / "output"),
                    status=status,
                    novel_id=novel["id"],
                    episode_id=novel["episodes"][0]["id"],
                    production_draft_id=draft["id"],
                    variant_index=variant,
                    variant_count=2,
                ).to_dict()

            queued = catalog.save_production_record(
                {
                    "draft_id": draft["id"],
                    "job_id": "recover-queued",
                    "variant_index": 1,
                    "device_id": api._current_device_id(),
                    "status": "queued",
                    "metadata": {
                        "production_run_id": "restart-run",
                        "job_snapshot": snapshot(
                            "recover-queued", 1, JobStatus.QUEUED
                        ),
                    },
                }
            )
            running = catalog.save_production_record(
                {
                    "draft_id": draft["id"],
                    "job_id": "recover-running",
                    "variant_index": 2,
                    "device_id": api._current_device_id(),
                    "status": "running",
                    "metadata": {
                        "production_run_id": "restart-run",
                        "job_snapshot": snapshot(
                            "recover-running", 2, JobStatus.RENDERING
                        ),
                    },
                }
            )

            api._reconcile_interrupted_records()
            worker = queue._worker
            self.assertIsNotNone(worker)
            assert worker is not None
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            api.get_jobs()

            self.assertEqual(processed, ["recover-queued"])
            self.assertEqual(catalog.get_record(queued["id"])["status"], "completed")
            interrupted = catalog.get_record(running["id"])
            self.assertEqual(interrupted["status"], "interrupted")
            self.assertTrue(interrupted["metadata"]["recovery_available"])

    def test_lease_confirmation_distinguishes_outage_from_other_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            api = StoryForgeApi(
                repository=SettingsRepository(root / "state"),
                queue=JobQueue(lambda *_args: "done.mp4"),
                catalog=CatalogRepository(root / "catalog.sqlite3"),
            )
            try:
                api._lease_generations["record-1"] = 1
                with (
                    patch.object(
                        api._catalog,
                        "claim_record_lease",
                        side_effect=OSError("Hub offline"),
                    ),
                    patch.object(
                        api._catalog,
                        "get_record",
                        side_effect=OSError("Hub offline"),
                    ),
                ):
                    self.assertFalse(
                        api._recover_or_confirm_record_lease_loss(
                            "record-1", "worker-a"
                        )
                    )
                with (
                    patch.object(
                        api._catalog,
                        "claim_record_lease",
                        side_effect=RuntimeError("lease conflict"),
                    ),
                    patch.object(
                        api._catalog,
                        "get_record",
                        return_value={
                            "status": "running",
                            "lease_owner_device": "worker-b",
                        },
                    ),
                ):
                    self.assertTrue(
                        api._recover_or_confirm_record_lease_loss(
                            "record-1", "worker-a"
                        )
                    )
            finally:
                api._shutdown()

    def test_successful_lease_reclaim_preserves_the_renewed_deadline(self) -> None:
        class RecoverableCatalog:
            def heartbeat_record_lease(self, *_args, **_kwargs):
                raise OSError("heartbeat route unavailable")

            def claim_record_lease(self, *_args, **_kwargs):
                return {"record": {"lease_generation": 1}}

            def get_record(self, *_args, **_kwargs):
                raise AssertionError("a successful re-claim must not query the record")

        api = StoryForgeApi.__new__(StoryForgeApi)
        api._lease_lock = threading.RLock()
        api._leased_records = {"record-1"}
        api._lease_generations = {"record-1": 1}
        original_deadline = time.monotonic() + 0.06
        api._lease_health = {
            "record-1": {
                "failures": 0,
                "next_attempt": 0.0,
                "deadline": original_deadline,
                "last_error": "",
                "state": "healthy",
            }
        }
        api._lease_stop = threading.Event()
        api._catalog = RecoverableCatalog()
        api._current_device_id = lambda: "worker-a"
        lost: list[str] = []

        def stop_for_lost_lease(record_id: str, _detail: str = "") -> None:
            lost.append(record_id)
            api._lease_stop.set()

        api._stop_jobs_for_lost_lease = stop_for_lost_lease
        with patch(
            "storyforge.api.RECORD_LEASE_HEARTBEAT_SECONDS", 0.02
        ):
            worker = threading.Thread(target=api._lease_heartbeat_loop, daemon=True)
            worker.start()
            time.sleep(0.14)
            api._lease_stop.set()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(lost, [])
        self.assertGreater(
            float(api._lease_health["record-1"]["deadline"]),
            original_deadline + 60.0,
        )

    def test_lost_lease_terminal_conflict_does_not_block_younger_batch(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        processed: list[str] = []
        queue = JobQueue(
            lambda job, _platform, _progress: processed.append(job.id)
            or f"{job.id}.mp4"
        )
        lost = RenderJob(
            id="lost-job",
            batch_id="lost-batch",
            platform_id=platform.id,
            source_file=__file__,
            title="Lost lease",
            code="LOST001",
            video_folder=".",
            music_folder=".",
            output_folder=".",
            production_record_id="lost-record",
        )
        younger = RenderJob(
            id="younger-job",
            batch_id="younger-batch",
            platform_id=platform.id,
            source_file=__file__,
            title="Younger batch",
            code="NEXT001",
            video_folder=".",
            music_folder=".",
            output_folder=".",
        )

        api = StoryForgeApi.__new__(StoryForgeApi)
        api._queue = queue
        api._lease_lock = threading.RLock()
        api._leased_records = {lost.production_record_id}
        api._lease_health = {lost.production_record_id: {"state": "healthy"}}
        api._superseded_lease_records = set()
        api._draft_gate_leases = {}
        api._shutdown_in_progress = threading.Event()
        api._job_materials = {}
        api._job_media_selection = {}
        api._recorded_media_jobs = set()
        api._recorded_artifacts = set()
        api._current_device_id = lambda: "worker-a"
        api._release_finished_draft_gates = lambda: None
        api._catalog = Mock()
        api._catalog.save_production_record.side_effect = CatalogConflictError(
            "production record lease belongs to another device"
        )
        queue.set_terminal_callback(api._sync_terminal_job_record)
        queue.enqueue_jobs([lost, younger], platform)

        api._stop_jobs_for_lost_lease(lost.production_record_id)
        queue.start()
        worker = queue._worker
        self.assertIsNotNone(worker)
        assert worker is not None
        worker.join(timeout=3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(processed, [younger.id])
        self.assertEqual(queue.stream_status()["state"], "connected")
        self.assertNotIn(
            lost.production_record_id,
            api._superseded_lease_records,
        )

        remote_lost = RenderJob(
            id="remote-lost-job",
            batch_id="remote-lost-batch",
            platform_id=platform.id,
            source_file=__file__,
            title="Remote lost lease",
            code="LOST002",
            video_folder=".",
            music_folder=".",
            output_folder=".",
            production_record_id="remote-lost-record",
            status=JobStatus.CANCELLED,
        )
        api._catalog.save_production_record.side_effect = HubRemoteError(
            409,
            "catalog_conflict",
            "production record lease belongs to another device",
        )
        api._superseded_lease_records.add(remote_lost.production_record_id)
        api._sync_terminal_job_record(remote_lost)
        self.assertNotIn(
            remote_lost.production_record_id,
            api._superseded_lease_records,
        )

        unmarked = RenderJob.from_dict(
            {
                **remote_lost.to_dict(),
                "id": "unmarked-job",
                "production_record_id": "unmarked-record",
            }
        )
        with self.assertRaises(HubRemoteError):
            api._sync_terminal_job_record(unmarked)

    def test_discarded_stream_page_releases_each_claimed_record_lease_once(self) -> None:
        api = StoryForgeApi.__new__(StoryForgeApi)
        api._release_record_lease = Mock()
        required = {
            "batch_id": "batch-1",
            "platform_id": "platform-1",
            "source_file": __file__,
            "title": "Late stream page",
            "code": "LATE001",
            "video_folder": ".",
            "music_folder": ".",
            "output_folder": ".",
        }
        jobs = [
            RenderJob(
                id="late-1",
                production_record_id="record-1",
                **required,
            ),
            RenderJob(
                id="late-1-duplicate",
                production_record_id="record-1",
                **required,
            ),
            RenderJob(
                id="late-2",
                production_record_id="record-2",
                **required,
            ),
            RenderJob(id="late-without-record", **required),
        ]

        api._discard_loaded_jobs(jobs)

        self.assertEqual(
            api._release_record_lease.call_args_list,
            [call("record-1"), call("record-2")],
        )

    def test_terminal_conflict_accepts_matching_authoritative_terminal_record(self) -> None:
        job = RenderJob(
            id="finished-job",
            batch_id="batch-1",
            platform_id="platform-1",
            source_file=__file__,
            title="Finished elsewhere",
            code="DONE001",
            video_folder=".",
            music_folder=".",
            output_folder=".",
            production_record_id="record-1",
            status=JobStatus.FAILED,
            message="the response was lost after the terminal write",
        )
        api = StoryForgeApi.__new__(StoryForgeApi)
        api._lease_lock = threading.RLock()
        api._leased_records = {job.production_record_id}
        api._lease_generations = {job.production_record_id: 7}
        api._lease_health = {job.production_record_id: {"state": "healthy"}}
        api._superseded_lease_records = set()
        api._draft_gate_leases = {}
        api._shutdown_in_progress = threading.Event()
        api._job_materials = {}
        api._job_media_selection = {}
        api._recorded_media_jobs = set()
        api._recorded_artifacts = set()
        api._current_device_id = lambda: "worker-a"
        api._release_finished_draft_gates = Mock()
        api._catalog = Mock()

        for error in (
            CatalogConflictError("production record lease belongs to another device"),
            HubRemoteError(
                409,
                "catalog_conflict",
                "production record lease belongs to another device",
            ),
        ):
            with self.subTest(error=type(error).__name__):
                api._leased_records = {job.production_record_id}
                api._lease_generations = {job.production_record_id: 7}
                api._lease_health = {
                    job.production_record_id: {"state": "healthy"}
                }
                api._catalog.reset_mock()
                api._catalog.save_production_record.side_effect = error
                api._catalog.get_record.return_value = {
                    "id": job.production_record_id,
                    "job_id": job.id,
                    "device_id": "worker-a",
                    "lease_generation": 7,
                    "status": "failed",
                }
                api._release_finished_draft_gates.reset_mock()

                api._sync_terminal_job_record(job)

                api._catalog.get_record.assert_called_once_with(
                    job.production_record_id
                )
                self.assertNotIn(job.production_record_id, api._leased_records)
                self.assertNotIn(job.production_record_id, api._lease_generations)
                self.assertNotIn(job.production_record_id, api._lease_health)
                api._release_finished_draft_gates.assert_called_once_with()

    def test_terminal_conflict_rejects_nonmatching_authoritative_record(self) -> None:
        job = RenderJob(
            id="finished-job",
            batch_id="batch-1",
            platform_id="platform-1",
            source_file=__file__,
            title="Do not acknowledge the wrong record",
            code="SAFE001",
            video_folder=".",
            music_folder=".",
            output_folder=".",
            production_record_id="record-1",
            status=JobStatus.FAILED,
        )
        api = StoryForgeApi.__new__(StoryForgeApi)
        api._lease_lock = threading.RLock()
        api._superseded_lease_records = set()
        api._draft_gate_leases = {}
        api._shutdown_in_progress = threading.Event()
        api._job_materials = {}
        api._job_media_selection = {}
        api._recorded_media_jobs = set()
        api._recorded_artifacts = set()
        api._current_device_id = lambda: "worker-a"
        api._release_finished_draft_gates = Mock()
        api._catalog = Mock()

        cases = {
            "wrong_record": {
                "id": "record-2",
                "job_id": job.id,
                "device_id": "worker-a",
                "lease_generation": 7,
                "status": "failed",
            },
            "wrong_job": {
                "id": job.production_record_id,
                "job_id": "other-job",
                "device_id": "worker-a",
                "lease_generation": 7,
                "status": "failed",
            },
            "wrong_device": {
                "id": job.production_record_id,
                "job_id": job.id,
                "device_id": "worker-b",
                "lease_generation": 7,
                "status": "failed",
            },
            "wrong_generation": {
                "id": job.production_record_id,
                "job_id": job.id,
                "device_id": "worker-a",
                "lease_generation": 8,
                "status": "failed",
            },
            "nonterminal": {
                "id": job.production_record_id,
                "job_id": job.id,
                "device_id": "worker-a",
                "lease_generation": 7,
                "status": "running",
            },
        }
        for label, authoritative in cases.items():
            with self.subTest(case=label):
                api._leased_records = {job.production_record_id}
                api._lease_generations = {job.production_record_id: 7}
                api._lease_health = {
                    job.production_record_id: {"state": "healthy"}
                }
                api._catalog.reset_mock()
                error = HubRemoteError(409, "catalog_conflict", "lease conflict")
                api._catalog.save_production_record.side_effect = error
                api._catalog.get_record.return_value = authoritative

                with self.assertRaises(HubRemoteError):
                    api._sync_terminal_job_record(job)

                self.assertIn(job.production_record_id, api._leased_records)
                self.assertEqual(
                    api._lease_generations[job.production_record_id], 7
                )

    def test_voice_candidates_are_rejected_while_local_render_is_busy(self) -> None:
        api = StoryForgeApi.__new__(StoryForgeApi)
        api._queue = Mock()
        api._queue.is_rendering_busy.return_value = True
        api._voice_preview_lock = threading.Lock()
        api._library = Mock()
        api._require_shared_catalog_online = lambda: None

        response = api.generate_voice_candidates("novel-1", "suspense", 240)

        self.assertFalse(response["ok"])
        self.assertIn("正在制作视频", response["error"])
        api._library.generate_voice_candidates.assert_not_called()

    def test_voice_candidate_requests_are_serialized(self) -> None:
        api = StoryForgeApi.__new__(StoryForgeApi)
        api._queue = Mock()
        api._queue.is_rendering_busy.return_value = False
        api._voice_preview_lock = threading.Lock()
        api._voice_preview_lock.acquire()
        api._library = Mock()
        api._require_shared_catalog_online = lambda: None

        try:
            response = api.generate_voice_candidates("novel-1", "suspense", 240)
        finally:
            api._voice_preview_lock.release()

        self.assertFalse(response["ok"])
        self.assertIn("候选配音正在生成", response["error"])
        api._library.generate_voice_candidates.assert_not_called()


if __name__ == "__main__":
    unittest.main()
