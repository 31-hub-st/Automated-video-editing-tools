from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import Mock
from pathlib import Path
from unittest.mock import patch

from scripts import stability_render_acceptance as acceptance
from storyforge.models import RenderJob


class StabilityAcceptanceTests(unittest.TestCase):
    @staticmethod
    def _attestation(root: Path, files: list[Path]) -> dict[str, object]:
        records = [
            (
                path.relative_to(root).as_posix(),
                path.stat().st_size,
                acceptance._sha256_file(path),
            )
            for path in files
        ]
        summary = acceptance._manifest_summary(records)
        return {
            "schema_version": 1,
            "ok": True,
            "frozen": True,
            "app_version": acceptance.__version__,
            "entrypoint": "StoryForge Studio.exe",
            "entrypoint_sha256": acceptance._sha256_file(
                root / "StoryForge Studio.exe"
            ),
            **summary,
        }

    def test_windowed_executable_console_failure_is_nonfatal(self) -> None:
        stream = Mock()
        with patch("builtins.print", side_effect=OSError(22, "Invalid argument")):
            acceptance._console_print("progress", file=stream)

    def test_packaged_ffmpeg_is_the_media_probe_when_ffprobe_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app_root = Path(temporary)
            ffmpeg = (
                app_root
                / "_internal"
                / "imageio_ffmpeg"
                / "binaries"
                / "ffmpeg-win-x86_64-v7.1.exe"
            )
            ffmpeg.parent.mkdir(parents=True)
            ffmpeg.write_bytes(b"packaged-ffmpeg")
            args = argparse.Namespace(app_root=app_root, ffmpeg=None, ffprobe=None)

            with patch.object(acceptance, "resolve_ffprobe", return_value=None):
                resolved_ffmpeg, media_probe = acceptance.resolve_tools(args)

        self.assertEqual(resolved_ffmpeg, ffmpeg.resolve())
        self.assertEqual(media_probe, resolved_ffmpeg)

    def test_ffmpeg_fallback_strictly_verifies_vertical_h264_with_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ffmpeg = root / "ffmpeg.exe"
            video = root / "story.mp4"
            ffmpeg.write_bytes(b"ffmpeg")
            video.write_bytes(b"0" * 4096)
            stderr = """
Input #0, mov,mp4, from 'story.mp4':
  Duration: 00:00:05.000, start: 0.000000, bitrate: 1200 kb/s
  Stream #0:0: Video: h264 (High), yuv420p, 1080x1920, 60 fps, 60 tbr
  Stream #0:1: Audio: aac (LC), 48000 Hz, stereo, fltp
Stream mapping:
  Stream #0:0 -> #0:0 (h264 (native) -> wrapped_avframe (native))
  Stream #0:1 -> #0:1 (aac (native) -> pcm_s16le (native))
"""

            with patch.object(
                acceptance,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", stderr),
            ) as run:
                result = acceptance.verify_video(
                    ffmpeg,
                    video,
                    ffmpeg=ffmpeg,
                    expected_fps=60,
                    expected_duration_seconds=5.0,
                )

        command = run.call_args.args[0]
        self.assertIn("0:v:0?", command)
        self.assertIn("0:a:0?", command)
        self.assertEqual(result["probe_backend"], "ffmpeg-fallback")
        self.assertEqual(result["codec"], "h264")
        self.assertEqual(result["audio_streams"], 1)

    def test_ffmpeg_fallback_accepts_a_pure_mp3_without_a_video_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ffmpeg = root / "ffmpeg.exe"
            narration = root / "narration.mp3"
            ffmpeg.write_bytes(b"ffmpeg")
            narration.write_bytes(b"0" * 4096)
            stderr = """
Input #0, mp3, from 'narration.mp3':
  Duration: 00:00:12.500, start: 0.025057, bitrate: 128 kb/s
  Stream #0:0: Audio: mp3, 48000 Hz, mono, fltp, 128 kb/s
Stream mapping:
  Stream #0:0 -> #0:0 (mp3 (mp3float) -> pcm_s16le (native))
"""

            with patch.object(
                acceptance,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", stderr),
            ) as run:
                result = acceptance.verify_mp3(
                    ffmpeg,
                    narration,
                    ffmpeg=ffmpeg,
                )

        command = run.call_args.args[0]
        self.assertIn("0:v:0?", command)
        self.assertIn("0:a:0?", command)
        self.assertEqual(result["probe_backend"], "ffmpeg-fallback")
        self.assertEqual(result["codec"], "mp3")
        self.assertEqual(result["audio_streams"], 1)

    def test_ffmpeg_fallback_rejects_mp3_container_with_a_video_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ffmpeg = root / "ffmpeg.exe"
            narration = root / "not-pure.mp3"
            ffmpeg.write_bytes(b"ffmpeg")
            narration.write_bytes(b"0" * 4096)
            stderr = """
Input #0, matroska, from 'not-pure.mp3':
  Duration: 00:00:12.500, start: 0.000000, bitrate: 256 kb/s
  Stream #0:0: Video: h264 (High), yuv420p, 1080x1920, 60 fps
  Stream #0:1: Audio: mp3, 48000 Hz, mono, fltp, 128 kb/s
Stream mapping:
"""

            with patch.object(
                acceptance,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", stderr),
            ):
                with self.assertRaisesRegex(
                    acceptance.AcceptanceError,
                    "standalone MP3",
                ):
                    acceptance.verify_mp3(
                        ffmpeg,
                        narration,
                        ffmpeg=ffmpeg,
                    )

    def test_standalone_ffprobe_remains_the_preferred_compatible_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ffmpeg = root / "ffmpeg.exe"
            ffprobe = root / "ffprobe.exe"
            video = root / "story.mp4"
            for tool in (ffmpeg, ffprobe):
                tool.write_bytes(b"tool")
            video.write_bytes(b"0" * 4096)
            payload = {
                "format": {"duration": "5.0"},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1080,
                        "height": 1920,
                        "avg_frame_rate": "60/1",
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
            }

            with patch.object(
                acceptance,
                "run_command",
                return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(payload), ""
                ),
            ) as run:
                result = acceptance.verify_video(
                    ffprobe,
                    video,
                    ffmpeg=ffmpeg,
                    expected_fps=60,
                    expected_duration_seconds=5.0,
                )

        command = run.call_args.args[0]
        self.assertIn("-show_streams", command)
        self.assertNotIn("0:v:0?", command)
        self.assertEqual(result["probe_backend"], "ffprobe")

    def test_regular_publish_contract_accepts_mp4_without_mp3_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            video = output / "publish" / "story.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"mp4")
            job = RenderJob(
                batch_id="batch",
                platform_id="goodnovel",
                source_file="B73165_Story.txt",
                title="Story",
                code="B73165",
                video_folder="videos",
                music_folder="music",
                output_folder=str(output),
            )
            manifest = {
                "job": {"narration_audio_file": ""},
                "result": {"narration_audio_file": ""},
                "media": {
                    "narration_audio": {"enabled": False, "output_file": ""}
                },
            }

            contract = acceptance.verify_regular_publish_contract(
                output_root=output,
                video_path=video,
                video_probe={"audio_streams": 1},
                job=job,
                manifest=manifest,
            )

        self.assertEqual(contract["mp4_count"], 1)
        self.assertEqual(contract["mp3_count"], 0)
        self.assertEqual(contract["embedded_audio_streams"], 1)

    def test_regular_publish_contract_rejects_legacy_mp3_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            video = output / "publish" / "story.mp4"
            audio = output / "publish" / "story.mp3"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"mp4")
            audio.write_bytes(b"mp3")
            job = RenderJob(
                batch_id="batch",
                platform_id="goodnovel",
                source_file="B73165_Story.txt",
                title="Story",
                code="B73165",
                video_folder="videos",
                music_folder="music",
                output_folder=str(output),
            )
            manifest = {
                "job": {"narration_audio_file": ""},
                "result": {"narration_audio_file": ""},
                "media": {
                    "narration_audio": {"enabled": False, "output_file": ""}
                },
            }

            with self.assertRaisesRegex(acceptance.AcceptanceError, "unexpected MP3"):
                acceptance.verify_regular_publish_contract(
                    output_root=output,
                    video_path=video,
                    video_probe={"audio_streams": 1},
                    job=job,
                    manifest=manifest,
                )

    def test_stable_build_switch_validates_report_and_exact_package_hash(self) -> None:
        script = (
            acceptance.SOURCE_ROOT / "scripts" / "build_exe.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("[switch]$RequireStableAcceptance", script)
        self.assertIn("'--storyforge-stability-acceptance'", script)
        self.assertIn("$acceptanceProcess = Start-Process -FilePath $exePath", script)
        self.assertIn("$acceptanceProcess.WaitForExit", script)
        self.assertIn("$acceptanceProcess.ExitCode", script)
        self.assertIn("$stableReportValidationScript", script)
        self.assertIn('report.get("stable_release_eligible")', script)
        self.assertIn("hashlib.sha256(expected_executable.read_bytes())", script)
        self.assertIn('scenario.get("actual_command_encoder")', script)

    def test_actual_encoder_comes_from_latest_real_render_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary)
            (job_dir / "render-command.txt").write_text(
                'ffmpeg -i source.mp4 -c:v h264_nvenc output.mp4',
                encoding="utf-8",
            )
            (job_dir / "render-command-fallback.txt").write_text(
                'ffmpeg -i source.mp4 -c:v libx264 output.mp4',
                encoding="utf-8",
            )

            actual = acceptance._actual_render_command_encoder(
                job_dir,
                [
                    {"stage": "ffmpeg_render", "succeeded": False},
                    {"stage": "ffmpeg_cpu_fallback", "succeeded": True},
                ],
            )

        self.assertEqual(actual["encoder"], "libx264")
        self.assertEqual(actual["filename"], "render-command-fallback.txt")

    def test_low_memory_final_command_encoder_wins_over_prepare_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary)
            (job_dir / "render-command-low-memory.txt").write_text(
                "ffmpeg -i clip.mp4 -c:v libx264 normalised.mp4\n"
                "ffmpeg -i normalised.mp4 -c:v h264_qsv final.mp4\n",
                encoding="utf-8",
            )

            actual = acceptance._actual_render_command_encoder(
                job_dir,
                [{"stage": "ffmpeg_serial_fallback", "succeeded": True}],
            )

        self.assertEqual(actual["encoder"], "h264_qsv")

    def test_serial_preparation_command_is_not_mistaken_for_final_render(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary)
            (job_dir / "render-command-low-memory.txt").write_text(
                "ffmpeg -i clip.mp4 -c:v libx264 normalised.mp4\n"
                "ffmpeg -i list.txt -c:v copy stitched.mp4\n",
                encoding="utf-8",
            )
            (job_dir / "render-command.txt").write_text(
                "ffmpeg -i stitched.mp4 -c:v h264_nvenc final.mp4\n",
                encoding="utf-8",
            )

            actual = acceptance._actual_render_command_encoder(
                job_dir,
                [{"stage": "ffmpeg_render", "succeeded": True}],
            )

        self.assertEqual(actual["encoder"], "h264_nvenc")
        self.assertEqual(actual["filename"], "render-command.txt")

    def test_arbitrary_nonempty_file_is_not_a_bound_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "StoryForge Studio.exe"
            artifact.write_bytes(b"MZ-real-package")
            args = argparse.Namespace(
                package_artifact=artifact,
                app_root=None,
            )
            package = acceptance._package_identity(args)

        self.assertFalse(
            acceptance._explicit_package_artifact_is_bound(args, package)
        )
        self.assertEqual(package["kind"], "explicit_artifact")
        self.assertTrue(package["sha256"])

    def test_frozen_runtime_must_match_entrypoint_inside_update_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            root.mkdir()
            executable = root / "StoryForge Studio.exe"
            executable.write_bytes(b"MZ-frozen-release")
            dependency = root / "runtime.dll"
            dependency.write_bytes(b"verified-runtime")
            validation = root / acceptance._RELEASE_VALIDATION_NAME
            validation.write_text(
                json.dumps(self._attestation(root, [executable, dependency])),
                encoding="utf-8",
            )
            artifact = Path(temporary) / "StoryForge.zip"
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr(
                    "storyforge-update.json",
                    json.dumps(
                        {
                            "schema_version": 1,
                            "version": acceptance.__version__,
                            "entrypoint": executable.name,
                        }
                    ),
                )
                archive.write(executable, executable.name)
                archive.write(dependency, dependency.name)
                archive.write(validation, validation.name)
            args = argparse.Namespace(package_artifact=artifact, app_root=None)
            with (
                patch.object(acceptance.sys, "frozen", True, create=True),
                patch.object(acceptance.sys, "executable", str(executable)),
            ):
                package = acceptance._package_identity(args)

        self.assertTrue(package["metadata_valid"])
        self.assertTrue(package["release_attestation_valid"])
        self.assertTrue(package["zip_bundle_manifest_matches"])
        self.assertTrue(package["runtime_bundle_manifest_matches"])
        self.assertTrue(package["runtime_entrypoint_matches"])
        self.assertTrue(acceptance._explicit_package_artifact_is_bound(args, package))

    def test_tampered_zip_dependency_cannot_bind_to_running_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "runtime"
            root.mkdir()
            executable = root / "StoryForge Studio.exe"
            executable.write_bytes(b"MZ-frozen-release")
            dependency = root / "runtime.dll"
            dependency.write_bytes(b"verified-runtime")
            validation = root / acceptance._RELEASE_VALIDATION_NAME
            validation.write_text(
                json.dumps(self._attestation(root, [executable, dependency])),
                encoding="utf-8",
            )
            artifact = Path(temporary) / "StoryForge-tampered.zip"
            with zipfile.ZipFile(artifact, "w") as archive:
                archive.writestr(
                    acceptance._UPDATE_METADATA_NAME,
                    json.dumps(
                        {
                            "schema_version": 1,
                            "version": acceptance.__version__,
                            "entrypoint": executable.name,
                        }
                    ),
                )
                archive.write(executable, executable.name)
                archive.writestr(dependency.name, b"tampered-runtime")
                archive.write(validation, validation.name)
            args = argparse.Namespace(package_artifact=artifact, app_root=None)
            with (
                patch.object(acceptance.sys, "frozen", True, create=True),
                patch.object(acceptance.sys, "executable", str(executable)),
            ):
                package = acceptance._package_identity(args)

        self.assertFalse(package["zip_bundle_manifest_matches"])
        self.assertFalse(package["runtime_entrypoint_matches"])
        self.assertFalse(acceptance._explicit_package_artifact_is_bound(args, package))

    def test_short_stress_run_returns_nonzero_even_when_scenarios_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "StoryForge Studio.exe"
            ffmpeg = root / "ffmpeg.exe"
            ffprobe = root / "ffprobe.exe"
            for path in (artifact, ffmpeg, ffprobe):
                path.write_bytes(b"nonempty")
            report_path = root / "acceptance.json"

            with (
                patch.object(
                    acceptance, "resolve_tools", return_value=(ffmpeg, ffprobe)
                ),
                patch.object(acceptance, "_ffmpeg_version", return_value="ffmpeg test"),
                patch.object(acceptance, "prepare_inputs", return_value={}),
                patch.object(
                    acceptance,
                    "render_scenario",
                    side_effect=lambda **spec: {
                        "name": spec["name"],
                        "ok": True,
                        "resources": {},
                    },
                ),
            ):
                exit_code = acceptance.main(
                    [
                        "--stress",
                        "--stress-seconds",
                        "10",
                        "--package-artifact",
                        str(artifact),
                        "--root",
                        str(root / "runs"),
                        "--json-report",
                        str(report_path),
                    ]
                )

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(report["ok"])
        self.assertFalse(report["stable_release_eligible"])
        self.assertFalse(report["package_artifact_bound"])
        self.assertEqual(
            report["verdict"],
            "sustained_test_passed_but_ten_minute_gate_not_covered",
        )


if __name__ == "__main__":
    unittest.main()
