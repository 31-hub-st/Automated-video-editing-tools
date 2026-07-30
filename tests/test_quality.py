from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from storyforge.services.quality import QualityExpectation, run_fast_quality_check


class FastQualityCheckTests(unittest.TestCase):
    def test_ffprobe_accepts_playable_vertical_h264_with_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "episode.mp4"
            output.write_bytes(b"x" * 8192)
            payload = {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1080,
                        "height": 1920,
                        "avg_frame_rate": "60000/1001",
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {
                    "duration": "60.04",
                    "size": str(output.stat().st_size),
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                },
            }
            commands: list[list[str]] = []

            def runner(command, **_kwargs):
                commands.append(command)
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(payload),
                    stderr="",
                )

            report = run_fast_quality_check(
                output,
                QualityExpectation(
                    width=1080,
                    height=1920,
                    duration_seconds=60.0,
                    fps=60,
                    checklist={"promo_code_snapshot": ("B123", "B123")},
                ),
                ffprobe_path=Path("fake-ffprobe.exe"),
                runner=runner,
            )

        self.assertTrue(report.passed)
        self.assertEqual(report.backend, "ffprobe")
        self.assertEqual(report.media["video_codec"], "h264")
        self.assertAlmostEqual(report.media["fps"], 59.94, places=2)
        self.assertTrue(next(check for check in report.checks if check.name == "fps").passed)
        self.assertIn("-show_streams", commands[0])
        self.assertIn("-show_format", commands[0])

    def test_reports_every_fast_failure_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "bad.mp4"
            output.write_bytes(b"tiny")
            payload = {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": 1920,
                        "height": 1080,
                    }
                ],
                "format": {"duration": "20", "size": "4"},
            }

            def runner(command, **_kwargs):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(payload),
                    stderr="",
                )

            report = run_fast_quality_check(
                output,
                QualityExpectation(
                    width=1080,
                    height=1920,
                    duration_seconds=30.0,
                    minimum_size_bytes=1024,
                    checklist={"promo_code_snapshot": ("B123", "WRONG")},
                ),
                ffprobe_path=Path("fake-ffprobe.exe"),
                runner=runner,
            )

        failed = {check.name for check in report.checks if not check.passed}
        self.assertFalse(report.passed)
        self.assertTrue(
            {
                "file_size",
                "audio_stream",
                "width",
                "height",
                "video_codec",
                "duration_seconds",
                "checklist.promo_code_snapshot",
            }.issubset(failed)
        )

    def test_packaged_ffmpeg_fallback_decodes_only_opening_fraction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "episode.mp4"
            output.write_bytes(b"x" * 4096)
            commands: list[list[str]] = []
            probe_output = (
                "Duration: 00:00:30.00, start: 0.000000, bitrate: 1200 kb/s\n"
                "Stream #0:0: Video: h264 (High), yuv420p, 1080x1920, 30 fps\n"
                "Stream #0:1: Audio: aac (LC), 48000 Hz, stereo\n"
            )

            def runner(command, **_kwargs):
                commands.append(command)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr=probe_output)

            with mock.patch(
                "storyforge.services.quality.resolve_ffprobe", return_value=None
            ):
                report = run_fast_quality_check(
                    output,
                    QualityExpectation(
                        width=1080,
                        height=1920,
                        duration_seconds=30.0,
                        fps=30,
                    ),
                    ffmpeg_path=Path("bundled-ffmpeg.exe"),
                    runner=runner,
                )

        self.assertTrue(report.passed)
        self.assertEqual(report.backend, "ffmpeg-fallback")
        self.assertEqual(report.media["fps"], 30.0)
        self.assertEqual(commands[0][commands[0].index("-t") + 1], "0.25")
        self.assertEqual(commands[0][-3:], ["-f", "null", "-"])

    def test_probe_process_failure_becomes_a_failed_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "episode.mp4"
            output.write_bytes(b"x" * 4096)

            def runner(command, **_kwargs):
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr="invalid data found",
                )

            report = run_fast_quality_check(
                output,
                QualityExpectation(
                    width=1080,
                    height=1920,
                    duration_seconds=30.0,
                ),
                ffprobe_path=Path("fake-ffprobe.exe"),
                runner=runner,
            )

        self.assertFalse(report.passed)
        self.assertTrue(report.errors)
        self.assertIn("invalid data found", report.errors[0])
        self.assertFalse(next(check for check in report.checks if check.name == "media_probe").passed)


if __name__ == "__main__":
    unittest.main()
