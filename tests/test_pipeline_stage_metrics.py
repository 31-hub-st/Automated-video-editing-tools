from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storyforge.jobs import JobQueue
from storyforge.models import JobStatus, PlatformProfile, RenderJob
from storyforge.pipeline import PipelineRunner, _write_pipeline_checkpoint
from storyforge.providers.text import TextResult


class PipelineStageMetricsTests(unittest.TestCase):
    def _job(self) -> RenderJob:
        return RenderJob(
            batch_id="batch-1",
            platform_id="platform-1",
            source_file="story.txt",
            title="Story",
            code="CODE1",
            video_folder="videos",
            music_folder="music",
            output_folder="output",
        )

    def test_stage_history_is_canonical_serial_and_bounded(self) -> None:
        queue = JobQueue()
        job = self._job()
        timestamps = [
            "2026-08-02T00:00:00+00:00",
            "2026-08-02T00:00:03+00:00",
            "2026-08-02T00:00:08+00:00",
            "2026-08-02T00:00:13+00:00",
            "2026-08-02T00:00:20+00:00",
            "2026-08-02T00:00:22+00:00",
            "2026-08-02T00:00:23+00:00",
            "2026-08-02T00:00:24+00:00",
        ]
        job.pipeline_stage_started_at = "2026-08-02T00:00:00+00:00"
        with patch("storyforge.jobs.utc_now", side_effect=timestamps):
            queue._update(job, JobStatus.PREFLIGHT, 0.04, "分析小说")
            queue._update(job, JobStatus.NARRATING, 0.27, "生成配音")
            queue._update(job, JobStatus.COMPOSING, 0.53, "编排字幕与素材")
            queue._update(job, JobStatus.RENDERING, 0.68, "整理视频素材")
            queue._update(job, JobStatus.RENDERING, 0.80, "渲染视频")
            queue._update(job, JobStatus.RENDERING, 0.94, "快速检查成片")
            queue._update(job, JobStatus.RENDERING, 0.96, "发布完整视频")
            queue._update(job, JobStatus.COMPLETED, 1.0, "已完成")

        self.assertEqual(
            [item["stage"] for item in job.pipeline_stage_history],
            [
                "text",
                "tts",
                "subtitles",
                "asset_preflight",
                "render",
                "qa",
                "publish",
            ],
        )
        self.assertEqual(job.pipeline_stage, "completed")
        self.assertEqual(job.pipeline_stage_started_at, "")
        self.assertTrue(
            all(item["duration_seconds"] >= 0 for item in job.pipeline_stage_history)
        )
        self.assertEqual(job.pipeline_stage_history[-1]["result"], "completed")

    def test_snapshot_round_trip_keeps_stage_telemetry(self) -> None:
        job = self._job()
        job.pipeline_stage = "render"
        job.pipeline_stage_history = [
            {"stage": "tts", "duration_seconds": 12.5, "result": "completed"}
        ]
        rebuilt = RenderJob.from_dict(job.to_dict())
        self.assertEqual(rebuilt.pipeline_stage, "render")
        self.assertEqual(rebuilt.pipeline_stage_history, job.pipeline_stage_history)

    def test_diagnostic_checkpoint_is_bounded_and_contains_no_business_copy(self) -> None:
        job = self._job()
        job.title = "Private Story Title"
        job.code = "PRIVATE-CODE"
        job.source_file = r"C:\private\story.txt"
        job.recipe_hash = "recipe-hash"
        job.content_fingerprint = "source-hash"
        with tempfile.TemporaryDirectory() as directory:
            job_dir = Path(directory)
            for index in range(40):
                _write_pipeline_checkpoint(
                    job_dir,
                    job,
                    stage=("text" if index % 2 == 0 else "tts"),
                    state=("started" if index % 2 == 0 else "completed"),
                    facts={"index": index},
                )

            checkpoint_path = job_dir / "checkpoint.json"
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["job_id"], job.id)
            self.assertEqual(payload["recipe_hash"], "recipe-hash")
            self.assertEqual(len(payload["history"]), 32)
            serialized = checkpoint_path.read_text(encoding="utf-8")
            self.assertNotIn(job.title, serialized)
            self.assertNotIn(job.code, serialized)
            self.assertNotIn(job.source_file, serialized)

    def test_diagnostic_checkpoint_is_fail_open_and_repairs_corrupt_json(self) -> None:
        job = self._job()
        with tempfile.TemporaryDirectory() as directory:
            job_dir = Path(directory)
            checkpoint_path = job_dir / "checkpoint.json"
            checkpoint_path.write_text("{broken", encoding="utf-8")
            _write_pipeline_checkpoint(
                job_dir, job, stage="text", state="completed"
            )
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["current_stage"], "text")
            self.assertEqual(payload["state"], "completed")

            with patch(
                "storyforge.pipeline._write_json_atomic",
                side_effect=OSError("read only"),
            ):
                _write_pipeline_checkpoint(
                    job_dir, job, stage="tts", state="started"
                )

    def test_terminal_checkpoint_keeps_latest_explicit_pipeline_boundary(self) -> None:
        job = self._job()
        job.pipeline_stage = "subtitles"
        with tempfile.TemporaryDirectory() as directory:
            job_dir = Path(directory)
            _write_pipeline_checkpoint(
                job_dir,
                job,
                stage="asset_preflight",
                state="started",
            )

            _write_pipeline_checkpoint(
                job_dir,
                job,
                stage=job.pipeline_stage,
                state="failed",
            )

            payload = json.loads(
                (job_dir / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["current_stage"], "asset_preflight")
            self.assertEqual(payload["state"], "failed")
            self.assertEqual(payload["history"][-1]["stage"], "asset_preflight")
            self.assertEqual(payload["history"][-1]["state"], "failed")

    def test_prepared_text_atomic_save_round_trips_through_existing_loader(self) -> None:
        job = self._job()
        job.novel_id = "novel-1"
        platform = PlatformProfile(id="platform-1", name="NovelBox")
        prepared = TextResult(
            polished_text="A polished story.",
            hook="A sharp hook.",
            ending_cta="Search CODE1 to continue.",
            mood="suspense",
            provider="local",
            model="test",
            retention_ratio=0.9,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prepared.json"
            PipelineRunner._save_prepared_text(
                path,
                recipe_hash="recipe-1",
                source_sha256="source-1",
                text_result=prepared,
                job=job,
                platform=platform,
            )

            loaded = PipelineRunner._load_prepared_text(path, "recipe-1")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.to_dict(), prepared.to_dict())
            self.assertIsNone(
                PipelineRunner._load_prepared_text(path, "different-recipe")
            )


if __name__ == "__main__":
    unittest.main()
