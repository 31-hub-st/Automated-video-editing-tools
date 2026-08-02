from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import package_smoke, run_test_gate
from scripts.build_update_package import write_release_validation
from storyforge import __version__


ROOT = Path(__file__).resolve().parents[1]


class ReleaseBuildContractTests(unittest.TestCase):
    def test_release_build_requires_explicit_stable_gate_and_package_smoke(self) -> None:
        script = (ROOT / "scripts" / "build_exe.ps1").read_text(encoding="utf-8")

        self.assertIn("[switch]$ReleaseBuild", script)
        self.assertIn("$ReleaseBuild -and -not $RequireStableAcceptance", script)
        self.assertIn("-ReleaseBuild requires the explicit", script)
        self.assertIn("scripts\\package_smoke.py", script)
        self.assertIn("'--require-stable-acceptance'", script)
        self.assertLess(
            script.index("write_release_validation"),
            script.index("scripts\\package_smoke.py"),
        )

    def test_workflows_keep_fast_nightly_and_release_gates_separate(self) -> None:
        workflows = ROOT / ".github" / "workflows"
        fast = (workflows / "fast-pr.yml").read_text(encoding="utf-8")
        nightly = (workflows / "nightly-full.yml").read_text(encoding="utf-8")
        release = (workflows / "release-stable-gate.yml").read_text(encoding="utf-8")

        self.assertIn("pull_request:", fast)
        self.assertIn("windows-latest", fast)
        self.assertIn("tests.test_release_gate", fast)
        self.assertIn("actions/upload-artifact", fast)
        self.assertIn("schedule:", nightly)
        self.assertIn("discover -s tests", nightly)
        self.assertIn("actions/upload-artifact", nightly)
        self.assertIn("workflow_dispatch:", release)
        self.assertIn("tags:", release)
        self.assertIn("-ReleaseBuild", release)
        self.assertIn("-RequireStableAcceptance", release)
        self.assertIn("package-smoke.json", release)
        self.assertIn("actions/upload-artifact", release)
        self.assertNotIn("secrets.", fast + nightly + release)


class PackageSmokeTests(unittest.TestCase):
    @staticmethod
    def _make_package(root: Path) -> tuple[Path, Path]:
        entrypoint = root / "StoryForge Studio.exe"
        entrypoint.write_bytes(b"MZ-frozen-storyforge")
        ui_root = root / "_internal" / "ui"
        ui_root.mkdir(parents=True)
        for name in package_smoke.UI_FILES:
            (ui_root / name).write_text(f"asset:{name}", encoding="utf-8")
        (root / "BUILD_STARTUP_VALIDATION.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "status": "passed",
                    "frozen": True,
                    "app_version": __version__,
                }
            ),
            encoding="utf-8",
        )
        write_release_validation(
            root,
            entrypoint=entrypoint.name,
            requested_version=__version__,
            with_local_ai=False,
        )
        return entrypoint, ui_root

    def test_metadata_smoke_records_explicit_runtime_and_ffmpeg_skip_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _entrypoint, ui_root = self._make_package(root)

            report = package_smoke.run_package_smoke(
                package_root=root,
                expected_version=__version__,
                require_stable_acceptance=False,
                skip_runtime_reason="unit test does not execute a Windows PE file",
                timeout_seconds=1,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["runtime"]["status"], "skipped")
        self.assertEqual(report["runtime"]["ffmpeg"]["status"], "skipped")
        self.assertIn("unit test", report["runtime"]["ffmpeg"]["reason"])
        self.assertEqual(report["runtime"]["ui"]["root"], str(ui_root.resolve()))

    def test_fresh_runtime_payload_proves_version_imports_ui_and_bundled_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _entrypoint, ui_root = self._make_package(root)
            ffmpeg = root / "_internal" / "imageio_ffmpeg" / "ffmpeg.exe"
            ffmpeg.parent.mkdir(parents=True)
            ffmpeg.write_bytes(b"ffmpeg")
            payload = {
                "ok": True,
                "status": "passed",
                "frozen": True,
                "app_version": __version__,
                "pythonnet_bridge_loaded": True,
                "webview_version": "test",
                "ui_root": str(ui_root),
                "ffmpeg_path": str(ffmpeg),
                "worker_ready": True,
                "worker_url": "http://127.0.0.1:1",
            }

            result = package_smoke._validate_startup_payload(
                payload,
                package_root=root,
                expected_version=__version__,
            )

        self.assertEqual(result["version"], __version__)
        self.assertEqual(result["imports"]["status"], "passed")
        self.assertEqual(result["ui"]["status"], "passed")
        self.assertEqual(result["ffmpeg"]["status"], "passed")

    def test_runtime_payload_rejects_ffmpeg_outside_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "package"
            root.mkdir()
            _entrypoint, ui_root = self._make_package(root)
            external_ffmpeg = base / "ffmpeg.exe"
            external_ffmpeg.write_bytes(b"external")
            payload = {
                "ok": True,
                "status": "passed",
                "frozen": True,
                "app_version": __version__,
                "pythonnet_bridge_loaded": True,
                "ui_root": str(ui_root),
                "ffmpeg_path": str(external_ffmpeg),
            }

            with self.assertRaisesRegex(
                package_smoke.PackageSmokeError, "outside the package"
            ):
                package_smoke._validate_startup_payload(
                    payload,
                    package_root=root,
                    expected_version=__version__,
                )

    def test_stable_acceptance_is_bound_to_the_exact_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entrypoint = root / "StoryForge Studio.exe"
            entrypoint.write_bytes(b"MZ-release-candidate")
            report_path = root / package_smoke.STABLE_ACCEPTANCE_REPORT
            report_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "stable_release_eligible": True,
                        "storyforge_version": __version__,
                        "code_under_test": "frozen_executable_pipeline_runner",
                        "package_artifact_bound": True,
                        "release_gate": {
                            "frozen_executable_pipeline_executed": True,
                        },
                        "package": {
                            "sha256": package_smoke.file_sha256(entrypoint),
                            "bytes": entrypoint.stat().st_size,
                        },
                        "verdict": "stable_release_eligible",
                        "scenarios": [{"ok": True}],
                    }
                ),
                encoding="utf-8",
            )

            result = package_smoke._validate_stable_acceptance(
                report_path,
                entrypoint=entrypoint,
                expected_version=__version__,
            )
            entrypoint.write_bytes(b"MZ-different-release-candidate")

            with self.assertRaisesRegex(
                package_smoke.PackageSmokeError, "SHA-256 does not match"
            ):
                package_smoke._validate_stable_acceptance(
                    report_path,
                    entrypoint=entrypoint,
                    expected_version=__version__,
                )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["scenario_count"], 1)

    @patch("scripts.package_smoke.subprocess.run")
    def test_bundled_ffmpeg_probe_executes_and_records_version(self, run) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ffmpeg = root / "ffmpeg.exe"
            ffmpeg.write_bytes(b"ffmpeg")
            run.return_value.returncode = 0
            run.return_value.stdout = "ffmpeg version 7.1-test\n"
            run.return_value.stderr = ""

            result = package_smoke._probe_ffmpeg(
                ffmpeg,
                package_root=root,
                timeout_seconds=180,
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["probe_exit_code"], 0)
        self.assertEqual(result["version_line"], "ffmpeg version 7.1-test")
        run.assert_called_once_with(
            [str(ffmpeg), "-hide_banner", "-version"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )


class TestSummaryTests(unittest.TestCase):
    def test_test_gate_reconfigures_parent_console_for_utf8_diagnostics(self) -> None:
        stdout = Mock()
        stderr = Mock()

        with patch.object(run_test_gate.sys, "stdout", stdout), patch.object(
            run_test_gate.sys, "stderr", stderr
        ):
            run_test_gate._configure_console_encoding()

        stdout.reconfigure.assert_called_once_with(
            encoding="utf-8", errors="replace"
        )
        stderr.reconfigure.assert_called_once_with(
            encoding="utf-8", errors="replace"
        )

    def test_test_gate_forces_utf8_across_windows_console_boundary(self) -> None:
        environment = run_test_gate._subprocess_environment({"PATH": "test"})

        self.assertEqual(environment["PATH"], "test")
        self.assertEqual(environment["PYTHONUTF8"], "1")
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")

    def test_unittest_summary_extracts_counts(self) -> None:
        parsed = run_test_gate.parse_unittest_output(
            "Ran 42 tests in 1.250s\n\nOK (skipped=2)\n",
            exit_code=0,
        )

        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["tests_run"], 42)
        self.assertEqual(parsed["skipped"], 2)
        self.assertEqual(parsed["failures"], 0)
        self.assertEqual(parsed["errors"], 0)

    def test_unittest_summary_preserves_failures_and_errors(self) -> None:
        parsed = run_test_gate.parse_unittest_output(
            "Ran 5 tests in 0.100s\n\nFAILED (failures=1, errors=2)\n",
            exit_code=1,
        )

        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["failures"], 1)
        self.assertEqual(parsed["errors"], 2)


if __name__ == "__main__":
    unittest.main()
