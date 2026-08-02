from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from storyforge.jobs import JobQueue
from storyforge.models import JobStatus, PlatformProfile, RenderJob


def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def _jobs(platform: PlatformProfile, *identities: str) -> list[RenderJob]:
    return [
        RenderJob(
            id=identity,
            batch_id="batch-governance",
            platform_id=platform.id,
            source_file=__file__,
            title=f"Story {identity}",
            code=identity.upper(),
            video_folder=".",
            music_folder=".",
            output_folder=".",
        )
        for identity in identities
    ]


class JobQueueGovernanceTests(unittest.TestCase):
    def test_pause_finishes_current_job_without_claiming_the_next(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def processor(job, _platform, _progress):
            calls.append(job.id)
            if job.id == "first":
                entered.set()
                self.assertTrue(release.wait(2))
            return f"{job.id}.mp4"

        queue = JobQueue(processor)
        self.addCleanup(queue.stop_and_wait, 1.0)
        queue.enqueue_jobs(_jobs(platform, "first", "second"), platform)
        queue.start()
        self.assertTrue(entered.wait(2))

        queue.pause_new_work()
        release.set()
        _wait_until(lambda: queue.list_jobs()[0]["status"] == "completed")
        time.sleep(0.05)

        self.assertEqual(calls, ["first"])
        self.assertEqual(queue.list_jobs()[1]["status"], "queued")
        self.assertEqual(queue.governance_status()["state"], "paused")

        queue.resume()
        _wait_until(lambda: queue.list_jobs()[1]["status"] == "completed")
        self.assertEqual(calls, ["first", "second"])

    def test_drain_stops_worker_after_current_job_and_preserves_queue(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def processor(job, _platform, _progress):
            calls.append(job.id)
            if job.id == "first":
                entered.set()
                self.assertTrue(release.wait(2))
            return f"{job.id}.mp4"

        queue = JobQueue(processor)
        self.addCleanup(queue.stop_and_wait, 1.0)
        queue.enqueue_jobs(_jobs(platform, "first", "second"), platform)
        queue.start()
        self.assertTrue(entered.wait(2))

        queue.drain()
        release.set()
        _wait_until(lambda: queue.governance_status()["drained"])

        self.assertEqual(calls, ["first"])
        self.assertEqual(queue.list_jobs()[1]["status"], "queued")
        self.assertEqual(queue.governance_status()["state"], "draining")

        queue.resume()
        _wait_until(lambda: queue.list_jobs()[1]["status"] == "completed")
        self.assertEqual(calls, ["first", "second"])

    def test_resource_gate_degrades_without_starting_or_cancelling_work(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        allowed = False
        calls: list[str] = []

        def admission(_job):
            return {
                "allowed": allowed,
                "reason": "low_memory" if not allowed else "",
            }

        queue = JobQueue(
            lambda job, _platform, _progress: calls.append(job.id)
            or f"{job.id}.mp4"
        )
        self.addCleanup(queue.stop_and_wait, 1.0)
        queue.set_admission_check(admission)
        queue.enqueue_jobs(_jobs(platform, "only"), platform)
        queue.start()

        _wait_until(lambda: queue.governance_status()["state"] == "degraded")
        self.assertEqual(calls, [])
        self.assertEqual(queue.list_jobs()[0]["status"], "queued")

        allowed = True
        queue.notify_resources_changed()
        _wait_until(lambda: queue.list_jobs()[0]["status"] == "completed")
        self.assertEqual(calls, ["only"])

    def test_resource_drop_never_interrupts_current_heavy_job(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        allowed = True
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def processor(job, _platform, _progress):
            calls.append(job.id)
            if job.id == "first":
                entered.set()
                self.assertTrue(release.wait(2))
            return f"{job.id}.mp4"

        queue = JobQueue(processor)
        self.addCleanup(queue.stop_and_wait, 1.0)
        queue.set_admission_check(
            lambda _job: {
                "allowed": allowed,
                "reason": "low_memory" if not allowed else "",
            }
        )
        queue.enqueue_jobs(_jobs(platform, "first", "second"), platform)
        queue.start()
        self.assertTrue(entered.wait(2))

        allowed = False
        release.set()
        _wait_until(lambda: queue.list_jobs()[0]["status"] == "completed")
        _wait_until(lambda: queue.governance_status()["state"] == "degraded")

        self.assertEqual(calls, ["first"])
        self.assertEqual(queue.list_jobs()[1]["status"], "queued")
        allowed = True
        queue.notify_resources_changed()
        _wait_until(lambda: queue.list_jobs()[1]["status"] == "completed")

    def test_recovered_start_installs_default_resource_gate_before_gateway(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        calls: list[str] = []
        queue = JobQueue(
            lambda job, _platform, _progress: calls.append(job.id)
            or f"{job.id}.mp4"
        )
        self.addCleanup(queue.stop_and_wait, 1.0)
        queue.enqueue_jobs(_jobs(platform, "recovered"), platform)

        with patch(
            "storyforge.worker.default_heavy_job_admission",
            return_value={"allowed": False, "reason": "low_memory"},
        ):
            queue.start()
            _wait_until(lambda: queue.governance_status()["state"] == "degraded")

        self.assertEqual(calls, [])
        self.assertEqual(queue.list_jobs()[0]["status"], "queued")
        queue.set_admission_check(lambda _job: True)
        _wait_until(lambda: queue.list_jobs()[0]["status"] == "completed")

    def test_admission_probe_failure_reports_fault_and_keeps_job_queued(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        calls: list[str] = []
        queue = JobQueue(
            lambda job, _platform, _progress: calls.append(job.id)
            or f"{job.id}.mp4"
        )
        self.addCleanup(queue.stop_and_wait, 1.0)

        def broken_probe(_job):
            raise OSError("private path must not reach health")

        queue.set_admission_check(broken_probe)
        queue.enqueue_jobs(_jobs(platform, "waiting"), platform)
        queue.start()
        _wait_until(lambda: queue.governance_status()["state"] == "fault")

        snapshot = queue.governance_status()
        self.assertEqual(snapshot["reason"], "oserror")
        self.assertEqual(calls, [])
        self.assertEqual(queue.list_jobs()[0]["status"], "queued")

        queue.set_admission_check(lambda _job: True)
        _wait_until(lambda: queue.list_jobs()[0]["status"] == "completed")

    def test_fatal_admission_reason_is_visible_on_worker_and_waiting_task(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        allowed = False
        calls: list[str] = []

        def admission(_job):
            if allowed:
                return {"allowed": True, "reason": ""}
            return {
                "allowed": False,
                "fault": True,
                "reason": "output_volume_unavailable",
                "message": "输出磁盘不可用，请重新连接移动硬盘或更换输出文件夹。",
            }

        queue = JobQueue(
            lambda job, _platform, _progress: calls.append(job.id)
            or f"{job.id}.mp4"
        )
        self.addCleanup(queue.stop_and_wait, 1.0)
        queue.set_admission_check(admission)
        queue.enqueue_jobs(_jobs(platform, "waiting"), platform)
        queue.start()

        _wait_until(lambda: queue.governance_status()["state"] == "fault")
        snapshot = queue.governance_status()
        waiting = queue.list_jobs()[0]
        self.assertEqual(snapshot["reason"], "output_volume_unavailable")
        self.assertIn("输出磁盘不可用", snapshot["message"])
        self.assertEqual(waiting["status"], "queued")
        self.assertIn("输出磁盘不可用", waiting["stage_label"])
        self.assertIn("更换输出文件夹", waiting["message"])
        self.assertEqual(calls, [])

        allowed = True
        queue.notify_resources_changed()
        _wait_until(lambda: queue.list_jobs()[0]["status"] == "completed")
        self.assertEqual(calls, ["waiting"])

    def test_consecutive_retryable_failures_enter_bounded_cooling(self) -> None:
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        calls: list[str] = []

        class RetryableFailure(RuntimeError):
            retryable = True

        def processor(job, _platform, _progress):
            calls.append(job.id)
            if len(calls) <= 2:
                raise RetryableFailure("temporary provider outage")
            return f"{job.id}.mp4"

        queue = JobQueue(
            processor,
            retryable_failure_threshold=2,
            cooldown_seconds=0.25,
        )
        self.addCleanup(queue.stop_and_wait, 1.0)
        queue.enqueue_jobs(_jobs(platform, "first", "second", "third"), platform)

        with patch("storyforge.jobs._persist_job_failure", return_value=""):
            queue.start()
            _wait_until(lambda: queue.governance_status()["state"] == "cooling")
            snapshot = queue.governance_status()
            self.assertGreater(snapshot["retry_in_seconds"], 0.0)
            self.assertGreater(snapshot["cooling_until_unix"], time.time())
            self.assertEqual(calls, ["first", "second"])
            time.sleep(0.05)
            self.assertEqual(calls, ["first", "second"])
            _wait_until(
                lambda: queue.list_jobs()[2]["status"]
                == JobStatus.COMPLETED.value
            )

        self.assertEqual(calls, ["first", "second", "third"])
        self.assertEqual(queue.governance_status()["state"], "ready")


if __name__ == "__main__":
    unittest.main()
