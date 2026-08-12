from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHINESE_ENTRY = ROOT / "一键恢复StoryForge-Hub.cmd"
ASCII_ENTRY = ROOT / "Restore-StoryForge-Hub.cmd"
RECOVERY_SCRIPT = ROOT / "scripts" / "restore_storyforge_hub_new_machine.ps1"
QUICK_GUIDE = ROOT / "docs" / "ONE_CLICK_HUB_RECOVERY.md"
RECOVERY_GUIDE = ROOT / "docs" / "NEW_MACHINE_RECOVERY.md"


class OneClickHubRecoveryContractTests(unittest.TestCase):
    def test_root_launchers_are_ascii_safe_and_delegate_to_the_ps51_script(self) -> None:
        chinese = CHINESE_ENTRY.read_bytes()
        stable = ASCII_ENTRY.read_bytes()

        chinese.decode("ascii")
        stable.decode("ascii")
        self.assertIn(b"Restore-StoryForge-Hub.cmd", chinese)
        self.assertIn(
            b"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
            stable,
        )
        self.assertIn(b"-ExecutionPolicy Bypass", stable)
        self.assertIn(b"restore_storyforge_hub_new_machine.ps1", stable)

    def test_recovery_script_has_the_strict_fresh_host_and_github_contract(self) -> None:
        raw = RECOVERY_SCRIPT.read_bytes()
        self.assertTrue(
            raw.startswith(b"\xef\xbb\xbf"),
            "Windows PowerShell 5.1 needs a UTF-8 BOM for Chinese output",
        )
        script = raw.decode("utf-8-sig")

        required = (
            "-Verb RunAs",
            "GitHub.cli",
            "$env:Path",
            "| Out-Host",
            "https://cli.github.com/",
            "安装后重新双击",
            "--accept-source-agreements",
            "--accept-package-agreements",
            "auth login",
            "--web",
            "$env:GH_HOST = 'github.com'",
            "31-hub-st/Automated-video-editing-tools",
            "isPrivate",
            "Get-ScheduledTask",
            "StoryForge Hub",
            "Get-ChildItem -LiteralPath $InstallRoot -Force",
            "Get-ChildItem -LiteralPath $DataRoot -Force",
            "[System.IO.FileAttributes]::ReparsePoint",
            "Get-NetTCPConnection",
            "Get-CimInstance",
            "Win32_Process",
            "D:\\StoryForgeHub\\Data",
            "bootstrap_storyforge.ps1",
            "-Role",
            "Hub",
            "-RestoreHubData",
            "-ReplaceExistingData",
            "verify_storyforge_deployment.ps1",
            "Start-Transcript",
            "$bootstrapStarted = $true",
            "$bootstrapCompleted = $true",
            "$verifyCompleted = $true",
            "不要再次运行一键恢复",
            "Get-NetIPAddress",
            "DPAPI",
            "固定局域网 IP",
            "10.0.0.225",
            "恢复成功",
            "Start-Process -FilePath $localUrl",
            "Read-Host",
        )
        for item in required:
            with self.subTest(item=item):
                self.assertIn(item, script)

        self.assertEqual(
            script.count("Assert-FreshHubPreflight"),
            3,
            "the function plus pre-auth and post-auth calls must all exist",
        )
        self.assertLess(
            script.rindex("Assert-FreshHubPreflight"),
            script.index("$bootstrapArguments = @("),
        )

        bootstrap_call = script[script.index("bootstrap_storyforge.ps1") :]
        for item in (
            "-Role",
            "Hub",
            "-Repo",
            "-InstallRoot",
            "-DataRoot",
            "-TaskName",
            "-RestoreHubData",
            "-ReplaceExistingData",
            "-FreshReplacementHost",
            "-Port",
        ):
            self.assertIn(item, bootstrap_call)

        verify_call = script[script.index("$verifyArguments = @(") :]
        self.assertIn("'-TaskName', $TaskName", verify_call)

    def test_recovery_script_parses_in_windows_powershell(self) -> None:
        command = (
            "$tokens=$null; $errors=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile("
            "$env:STORYFORGE_RECOVERY_SCRIPT,[ref]$tokens,[ref]$errors) | Out-Null; "
            "if($errors.Count){$errors | ForEach-Object { $_.Message }; exit 1}"
        )
        environment = os.environ.copy()
        environment["STORYFORGE_RECOVERY_SCRIPT"] = str(RECOVERY_SCRIPT)
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            check=False,
        )
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, output)

    def test_chinese_guides_document_the_one_click_safety_boundary(self) -> None:
        quick = QUICK_GUIDE.read_text(encoding="utf-8")
        recovery = RECOVERY_GUIDE.read_text(encoding="utf-8")
        for text in (quick, recovery):
            self.assertIn("一键恢复StoryForge-Hub.cmd", text)
            self.assertIn("仅限全新 Hub", text)
            self.assertIn("不会合并", text)
            self.assertIn("DPAPI", text)
            self.assertIn("hub-state-latest", text)
            self.assertIn("仅下载最新程序不能恢复业务数据", text)

        required_flow = (
            "下载仓库 ZIP",
            "解压",
            "双击",
            "浏览器授权",
            "等待恢复成功",
        )
        positions = []
        for item in required_flow:
            with self.subTest(flow_step=item):
                self.assertIn(item, quick)
                positions.append(quick.index(item))
        self.assertEqual(positions, sorted(positions))

    def test_permission_failure_does_not_echo_github_stderr(self) -> None:
        script = RECOVERY_SCRIPT.read_text(encoding="utf-8-sig")
        permission_block = script[
            script.index("$repoJsonPath =") : script.index("function Invoke-CheckedPowerShellScript")
        ]
        self.assertIn("2> $null", permission_block)
        self.assertNotIn("$repoOutput", permission_block)


if __name__ == "__main__":
    unittest.main()
