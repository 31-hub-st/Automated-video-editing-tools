from __future__ import annotations

import unittest
from unittest.mock import patch

from storyforge.jobs import JobQueue
from storyforge.models import JobStatus, RenderJob


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


if __name__ == "__main__":
    unittest.main()
