from __future__ import annotations

import argparse
import json
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
        self.assertIn("Invoke-Checked -Command $exePath", script)
        self.assertIn("$stableReport.stable_release_eligible", script)
        self.assertIn("Get-FileHash -LiteralPath $exePath", script)
        self.assertIn("$scenario.actual_command_encoder", script)

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
