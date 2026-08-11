from __future__ import annotations

import unittest
from pathlib import Path


class BootstrapContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = Path(__file__).resolve().parents[1]
        cls.bootstrap = (
            cls.project / "scripts" / "bootstrap_storyforge.ps1"
        ).read_text(encoding="utf-8")
        cls.publish = (
            cls.project / "scripts" / "publish_hub_snapshot.ps1"
        ).read_text(encoding="utf-8")
        cls.verify = (
            cls.project / "scripts" / "verify_storyforge_deployment.ps1"
        ).read_text(encoding="utf-8")

    def test_scripts_are_powershell_51_safe_ascii(self) -> None:
        for name in (
            "bootstrap_storyforge.ps1",
            "publish_hub_snapshot.ps1",
            "verify_storyforge_deployment.ps1",
        ):
            content = (self.project / "scripts" / name).read_bytes()
            self.assertTrue(content.isascii(), name)

    def test_bootstrap_uses_stable_release_and_all_package_checks(self) -> None:
        self.assertIn("repos/$Repo/releases/latest", self.bootstrap)
        self.assertIn("Assert-GitHubAsset", self.bootstrap)
        self.assertIn("Assert-SidecarFile", self.bootstrap)
        self.assertIn("Read-VerifiedInternalManifest", self.bootstrap)
        self.assertIn("storyforge-update.json", self.bootstrap)
        self.assertIn("Expand-Archive", self.bootstrap)

    def test_existing_same_version_app_is_reused_only_after_full_tree_match(self) -> None:
        self.assertIn("function Get-DirectoryFileManifest", self.bootstrap)
        self.assertIn("function Assert-DirectoryTreesMatch", self.bootstrap)
        self.assertIn("Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256", self.bootstrap)
        self.assertIn("Existing application directory does not exactly match", self.bootstrap)
        self.assertLess(
            self.bootstrap.index("Expand-Archive"),
            self.bootstrap.index("if (Test-Path -LiteralPath $appDirectory)"),
        )
        self.assertNotIn("$installedManifest.version -ne $version", self.bootstrap)

    def test_fixed_path_contract_accepts_safe_nested_ascii_paths(self) -> None:
        expected_pattern = (
            r"^[A-Za-z]:\\(?:[A-Za-z0-9_. -]+\\)*[A-Za-z0-9_. -]+$"
        )
        self.assertIn(expected_pattern, self.bootstrap)
        self.assertIn(expected_pattern, self.publish)
        self.assertIn(expected_pattern, self.verify)

    def test_hub_restore_is_explicit_authoritative_and_offline(self) -> None:
        self.assertIn("hub-state-latest", self.bootstrap)
        self.assertIn("StoryForge-Hub-Latest.sfbak", self.bootstrap)
        self.assertIn("-RestoreHubData -ReplaceExistingData", self.bootstrap)
        self.assertIn("--startup-self-test", self.bootstrap)
        self.assertIn("--restore-hub-backup", self.bootstrap)
        self.assertIn("$restoreResult.restored", self.bootstrap)
        self.assertIn("$restoreResult.requires_restart", self.bootstrap)
        self.assertIn("[string]$selfTest.status -ne 'passed'", self.bootstrap)
        self.assertIn("storyforge-catalog.sqlite3", self.bootstrap)
        self.assertLess(
            self.bootstrap.index("--restore-hub-backup"),
            self.bootstrap.index("Register-ScheduledTask"),
        )

    def test_hub_service_is_private_and_health_checked(self) -> None:
        self.assertIn("-Profile Private", self.bootstrap)
        self.assertIn("-RemoteAddress LocalSubnet", self.bootstrap)
        self.assertIn("/web/api/health", self.bootstrap)
        self.assertIn("Start-ScheduledTask", self.bootstrap)
        self.assertIn("Get-NetTCPConnection", self.bootstrap)
        self.assertIn("Win32_Process", self.bootstrap)
        self.assertIn("$health.data.version -eq $version", self.bootstrap)
        self.assertIn("$health.data.backup.available", self.bootstrap)
        self.assertIn("$health.data.backup.running", self.bootstrap)
        self.assertIn("$listenerProcess.ExecutablePath", self.bootstrap)
        self.assertIn("New-TimeSpan -Minutes 2", self.bootstrap)

    def test_hub_preflight_refuses_existing_task_and_firewall(self) -> None:
        self.assertIn("Scheduled task '$TaskName' already exists", self.bootstrap)
        self.assertIn("Firewall rule '$ruleName' already exists", self.bootstrap)
        register_line = next(
            line for line in self.bootstrap.splitlines()
            if "Register-ScheduledTask -TaskName $TaskName" in line
        )
        self.assertNotIn("-Force", register_line)
        self.assertNotIn("Set-NetFirewallRule", self.bootstrap)
        self.assertIn("$hubTaskCreated = $true", self.bootstrap)
        self.assertIn("$hubFirewallRuleCreated = $true", self.bootstrap)
        self.assertIn("Unregister-ScheduledTask", self.bootstrap)
        self.assertIn("Remove-NetFirewallRule", self.bootstrap)

    def test_pointer_is_published_only_after_role_validation(self) -> None:
        pointer_call = "Write-DeploymentPointer -Destination $pointerPath -Value $pointer"
        self.assertEqual(self.bootstrap.count(pointer_call), 2)
        health_index = self.bootstrap.index("$listenerProcess.ExecutablePath")
        hub_pointer_index = self.bootstrap.index(pointer_call, health_index)
        employee_branch_index = self.bootstrap.index(
            "# Employee package validation has completed before publishing the pointer."
        )
        employee_pointer_index = self.bootstrap.index(pointer_call, hub_pointer_index + 1)
        self.assertLess(health_index, hub_pointer_index)
        self.assertLess(employee_branch_index, employee_pointer_index)

    def test_offline_check_does_not_block_every_desktop_process(self) -> None:
        offline_function = self.bootstrap[
            self.bootstrap.index("function Assert-HubIsOffline") :
            self.bootstrap.index("if (-not [Environment]::Is64BitOperatingSystem")
        ]
        self.assertNotIn("$name -ieq 'StoryForge Studio.exe'", offline_function)
        self.assertIn("--web", offline_function)
        self.assertIn(r"Start-StoryForge-Hub\.ps1", offline_function)

    def test_employee_mode_rejects_hub_data_options(self) -> None:
        self.assertIn(
            "Employee mode installs only the program; Hub data options are not allowed.",
            self.bootstrap,
        )

    def test_publisher_keeps_one_private_latest_snapshot_pair(self) -> None:
        self.assertIn("--create-hub-backup", self.publish)
        self.assertIn("Refusing to publish Hub business data to a public repository", self.publish)
        self.assertIn("'release', 'upload', $Tag, '--repo', $Repo, '--clobber'", self.publish)
        self.assertIn("$assets.Count -ne 2", self.publish)
        self.assertIn("repos/$Repo/releases/assets/$($asset.id)", self.publish)

    def test_publisher_supports_source_cli_without_current_pointer(self) -> None:
        self.assertIn("[string]$StoryForgeExe", self.publish)
        self.assertIn("[string]$PythonExe", self.publish)
        self.assertIn("'-m', 'storyforge.main', '--create-hub-backup'", self.publish)
        self.assertIn("@('3.12', '3.11')", self.publish)
        self.assertNotIn("StoryForge deployment pointer is missing", self.publish)
        self.assertNotIn("Join-Path $HubRoot 'current.json'", self.publish)
        self.assertNotIn("$pointerVersion", self.publish)

    def test_handoff_document_covers_data_boundaries(self) -> None:
        agents = (self.project / "AGENTS.md").read_text(encoding="utf-8")
        recovery = (
            self.project / "docs" / "NEW_MACHINE_RECOVERY.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Never edit, merge, replace, delete", agents)
        self.assertIn("API Key", recovery)
        self.assertIn("员工电脑的视频素材", recovery)
        self.assertIn("-ReplaceExistingData", recovery)


if __name__ == "__main__":
    unittest.main()
