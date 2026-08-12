from __future__ import annotations

import io
import json
import os
import shutil
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

    def test_release_build_copies_one_click_hub_recovery_payload_before_attestation(
        self,
    ) -> None:
        script = (ROOT / "scripts" / "build_exe.ps1").read_text(encoding="utf-8")
        attestation = script.index("write_release_validation")

        required_copy_contract = (
            "[char]0x4E00, [char]0x952E, [char]0x6062, [char]0x590D",
            "Join-Path $projectRoot $chineseRecoveryLauncherName",
            "Join-Path $bundleRoot $chineseRecoveryLauncherName",
            "Join-Path $projectRoot 'Restore-StoryForge-Hub.cmd'",
            "Join-Path $bundleRoot 'scripts'",
            "Join-Path $projectRoot 'scripts\\restore_storyforge_hub_new_machine.ps1'",
            "Join-Path $projectRoot 'scripts\\bootstrap_storyforge.ps1'",
            "Join-Path $recoveryScriptsTarget 'verify_storyforge_deployment.ps1'",
        )
        for fragment in required_copy_contract:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, script)
                self.assertLess(script.index(fragment), attestation)

    def test_stable_report_validator_survives_windows_powershell_quoting(self) -> None:
        script = (ROOT / "scripts" / "build_exe.ps1").read_text(encoding="utf-8")
        start = script.index("$stableReportValidationScript = @'")
        end = script.index("\n'@", start)
        validator = script[start:end]

        self.assertNotIn(
            '"',
            validator,
            "python -c code passed through Windows PowerShell must use single quotes",
        )

    def test_frozen_smokes_clear_hub_identity_and_restore_the_build_shell(self) -> None:
        script = (ROOT / "scripts" / "build_exe.ps1").read_text(encoding="utf-8")
        scope = script[script.index("$previousDataDir"):script.index("$sizeMb")]
        first_frozen_launch = scope.index("$smokeProcess = Start-Process")
        package_smoke_launch = scope.index("scripts\\package_smoke.py")

        previous_names = {
            "STORYFORGE_DEPLOYMENT_ROLE": "previousDeploymentRole",
            "STORYFORGE_FROZEN_HUB_DATA_ROOT": "previousFrozenHubDataRoot",
            "STORYFORGE_PORTABLE_MODE": "previousPortableMode",
        }
        for environment_name, previous_name in previous_names.items():
            capture = (
                f"${previous_name} = [Environment]::GetEnvironmentVariable("
                f"'{environment_name}', 'Process')"
            )
            clear = f"Remove-Item Env:{environment_name} -ErrorAction SilentlyContinue"
            restore = f"$env:{environment_name} = ${previous_name}"
            self.assertIn(capture, scope)
            self.assertLess(scope.index(clear), first_frozen_launch)
            self.assertGreater(scope.index(restore), package_smoke_launch)

        self.assertIn("$kokoroProcess = Start-Process", scope)
        self.assertIn("$acceptanceProcess = Start-Process", scope)

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
    def _copy_recovery_payload(root: Path) -> None:
        for relative in package_smoke.RECOVERY_PAYLOAD_FILES:
            source = ROOT / Path(relative)
            target = root / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

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
        PackageSmokeTests._copy_recovery_payload(root)
        write_release_validation(
            root,
            entrypoint=entrypoint.name,
            requested_version=__version__,
            with_local_ai=False,
        )
        return entrypoint, ui_root

    def test_packaged_ui_contract_requires_studio_theme(self) -> None:
        self.assertIn("studio-theme.css", package_smoke.UI_FILES)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ui_root = root / "_internal" / "ui"
            ui_root.mkdir(parents=True)
            for name in package_smoke.UI_FILES:
                if name != "studio-theme.css":
                    (ui_root / name).write_text(f"asset:{name}", encoding="utf-8")

            with self.assertRaises(package_smoke.PackageSmokeError):
                package_smoke._find_ui_root(root)

    def test_recovery_payload_requires_every_launcher_and_delegated_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._copy_recovery_payload(root)
            missing = root / "scripts" / "bootstrap_storyforge.ps1"
            missing.unlink()

            with self.assertRaisesRegex(
                package_smoke.PackageSmokeError,
                "bootstrap_storyforge.ps1",
            ):
                package_smoke._validate_recovery_payload(root)

    def test_recovery_payload_validates_encoding_and_delegation_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._copy_recovery_payload(root)

            files = package_smoke._validate_recovery_payload(root)

        self.assertEqual(
            [item["name"] for item in files],
            list(package_smoke.RECOVERY_PAYLOAD_FILES),
        )
        self.assertTrue(all(int(item["bytes"]) > 0 for item in files))

    def test_package_smoke_console_json_is_safe_for_legacy_windows_codepages(self) -> None:
        launcher = "\u4e00\u952e\u6062\u590dStoryForge-Hub.cmd"

        rendered = package_smoke._json_for_console({"name": launcher})

        rendered.encode("cp1252", errors="strict")
        self.assertEqual(json.loads(rendered), {"name": launcher})
        self.assertNotIn(launcher, rendered)

    def test_package_smoke_cli_survives_cp1252_stdout_with_chinese_launcher(self) -> None:
        launcher = "\u4e00\u952e\u6062\u590dStoryForge-Hub.cmd"
        payload = {"ok": True, "recovery": {"files": [{"name": launcher}]}}
        raw_console = io.BytesIO()
        console = io.TextIOWrapper(raw_console, encoding="cp1252", errors="strict")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = root / "package"
            package_root.mkdir()
            report_path = root / "package-smoke.json"
            with patch.object(package_smoke, "run_package_smoke", return_value=payload), patch.object(
                package_smoke.sys, "stdout", console
            ):
                exit_code = package_smoke.main(
                    [
                        "--package-root",
                        str(package_root),
                        "--report",
                        str(report_path),
                    ]
                )
            console.flush()
            report = json.loads(report_path.read_text(encoding="utf-8"))

        rendered = raw_console.getvalue().decode("cp1252")
        self.assertEqual(exit_code, 0)
        self.assertEqual(report, payload)
        self.assertEqual(json.loads(rendered), payload)
        self.assertNotIn(launcher, rendered)

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
        self.assertEqual(report["recovery"]["status"], "passed")

    def test_runtime_smoke_does_not_inherit_hub_or_portable_identity(self) -> None:
        captured_environment: dict[str, str] = {}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_package(root)
            fake_ffmpeg = root / "_internal" / "imageio_ffmpeg" / "ffmpeg.exe"

            def run_frozen(command, **kwargs):
                captured_environment.update(dict(kwargs["env"]))
                result_root = Path(command[2])
                (result_root / "startup-self-test.json").write_text(
                    json.dumps({"ok": True}), encoding="utf-8"
                )
                return Mock(returncode=0, stdout="", stderr="")

            validated_runtime = {
                "version": __version__,
                "imports": {"status": "passed"},
                "ui": {"status": "passed"},
                "ffmpeg": {"status": "passed", "path": str(fake_ffmpeg)},
                "worker": {"status": "passed"},
            }
            inherited = {
                "STORYFORGE_DEPLOYMENT_ROLE": "Hub",
                "STORYFORGE_FROZEN_HUB_DATA_ROOT": "D:/StoryForgeHub/Data",
                "STORYFORGE_PORTABLE_MODE": "1",
            }
            with (
                patch.dict(os.environ, inherited, clear=False),
                patch(
                    "scripts.package_smoke.subprocess.run",
                    side_effect=run_frozen,
                ),
                patch(
                    "scripts.package_smoke._validate_startup_payload",
                    return_value=validated_runtime,
                ),
                patch(
                    "scripts.package_smoke._probe_ffmpeg",
                    return_value={"status": "passed", "path": str(fake_ffmpeg)},
                ),
            ):
                report = package_smoke.run_package_smoke(
                    package_root=root,
                    expected_version=__version__,
                    require_stable_acceptance=False,
                    skip_runtime_reason="",
                    timeout_seconds=1,
                )
                self.assertEqual(
                    {name: os.environ.get(name) for name in inherited}, inherited
                )

        self.assertTrue(report["ok"])
        self.assertTrue(captured_environment.get("STORYFORGE_DATA_DIR", "").endswith("data"))
        for name in inherited:
            self.assertNotIn(name, captured_environment)

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
