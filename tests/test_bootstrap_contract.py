from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
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
        cls.enable_hub = (
            cls.project / "scripts" / "enable_storyforge_hub.ps1"
        ).read_text(encoding="utf-8")
        cls.ops_hub = (
            cls.project / "ops" / "Start-StoryForgeHub.ps1"
        ).read_text(encoding="utf-8")
        cls.repair_hub = (
            cls.project / "scripts" / "repair_storyforge_hub_launcher.ps1"
        ).read_text(encoding="utf-8")

    @staticmethod
    def _windows_powershell_environment(
        updates: dict[str, str] | None = None,
        *,
        inherited_environment: dict[str, str] | None = None,
    ) -> dict[str, str]:
        environment = dict(
            os.environ
            if inherited_environment is None
            else inherited_environment
        )
        if updates:
            environment.update(updates)
        # A Windows PowerShell 5.1 child started by pwsh can inherit PowerShell
        # 7 module paths first. Remove the variable entirely so 5.1 rebuilds
        # its own WindowsPowerShell-only module search path.
        for name in tuple(environment):
            if name.casefold() == "psmodulepath":
                del environment[name]
        return environment

    @staticmethod
    def _windows_powershell_executable(environment: dict[str, str]) -> str:
        system_root = next(
            (
                value
                for name, value in environment.items()
                if name.casefold() == "systemroot"
            ),
            "",
        )
        if not system_root:
            raise AssertionError("SystemRoot is required for Windows PowerShell tests.")
        executable = (
            Path(system_root)
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if not executable.is_file():
            raise AssertionError(f"Windows PowerShell 5.1 is missing: {executable}")
        return str(executable)

    @classmethod
    def _run_windows_powershell(
        cls,
        *arguments: str,
        environment_updates: dict[str, str] | None = None,
        inherited_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = cls._windows_powershell_environment(
            environment_updates,
            inherited_environment=inherited_environment,
        )
        return subprocess.run(
            [
                cls._windows_powershell_executable(environment),
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                *arguments,
            ],
            cwd=cls.project,
            env=environment,
            capture_output=True,
            check=False,
        )

    def test_windows_powershell_child_discards_inherited_pwsh_module_path(
        self,
    ) -> None:
        inherited = os.environ.copy()
        inherited["pSmOdUlEpAtH"] = (
            r"C:\Program Files\PowerShell\7\Modules;"
            r"C:\Program Files\WindowsPowerShell\Modules"
        )
        environment = self._windows_powershell_environment(
            inherited_environment=inherited
        )
        self.assertFalse(
            any(name.casefold() == "psmodulepath" for name in environment)
        )

        result = self._run_windows_powershell(
            "-Command",
            r"""
$ErrorActionPreference = 'Stop'
if (
    $PSVersionTable.PSEdition -ne 'Desktop' -or
    $PSVersionTable.PSVersion.Major -ne 5
) {
    throw 'The contract harness did not start Windows PowerShell 5.1.'
}
$command = Get-Command Get-FileHash -ErrorAction Stop
if ([string]$command.Source -ne 'Microsoft.PowerShell.Utility') {
    throw 'Windows PowerShell did not rebuild its Utility module path.'
}
[void](Get-FileHash -LiteralPath $PSHOME\powershell.exe -Algorithm SHA256)
""",
            inherited_environment=inherited,
        )
        output = (result.stdout + result.stderr).decode(
            "utf-8", errors="replace"
        )
        self.assertEqual(result.returncode, 0, output)

    @staticmethod
    def _write_verified_target_app(app: Path, version: str) -> Path:
        app.mkdir(parents=True)
        entrypoint = app / "StoryForge Studio.exe"
        entrypoint.write_bytes(b"verified-current-release")
        startup = app / "BUILD_STARTUP_VALIDATION.json"
        startup.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "ok": True,
                    "frozen": True,
                    "app_version": version,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        nested_asset = app / "assets" / "Static.bin"
        nested_asset.parent.mkdir()
        nested_asset.write_bytes(b"verified-nested-asset")
        admin_tool = app / "admin-tools" / "diagnose.ps1"
        admin_tool.parent.mkdir()
        admin_tool.write_bytes(b"Write-Output 'verified'\r\n")
        (app / "storyforge-update.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "version": version,
                    "entrypoint": "StoryForge Studio.exe",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        records: list[tuple[str, int, str]] = []
        for path in (startup, entrypoint, nested_asset, admin_tool):
            relative = path.relative_to(app).as_posix()
            payload = path.read_bytes()
            records.append(
                (relative, len(payload), hashlib.sha256(payload).hexdigest())
            )
        records.sort(key=lambda item: item[0].casefold())
        aggregate = hashlib.sha256()
        for relative, size, digest in records:
            aggregate.update(relative.encode("utf-8"))
            aggregate.update(b"\0")
            aggregate.update(str(size).encode("ascii"))
            aggregate.update(b"\0")
            aggregate.update(digest.encode("ascii"))
            aggregate.update(b"\n")
        release_validation = {
            "schema_version": 1,
            "ok": True,
            "frozen": True,
            "app_version": version,
            "entrypoint": "StoryForge Studio.exe",
            "entrypoint_sha256": hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
            "startup_validation_sha256": hashlib.sha256(startup.read_bytes()).hexdigest(),
            "kokoro_validation_sha256": "",
            "with_local_ai": False,
            "bundle_manifest_sha256": aggregate.hexdigest(),
            "bundle_file_count": len(records),
            "bundle_size_bytes": sum(record[1] for record in records),
            "bundle_files": [record[0] for record in records],
        }
        (app / "BUILD_RELEASE_VALIDATION.json").write_text(
            json.dumps(release_validation) + "\n",
            encoding="utf-8",
        )
        return entrypoint

    @staticmethod
    def _write_legacy_ops_wrapper(path: Path) -> None:
        path.write_bytes(
            """param(
    [Parameter(Mandatory = $true)]
    [string]$InstallPath,

    [Parameter(Mandatory = $true)]
    [string]$DataPath,

    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

function Test-StoryForgeHub {
    param([int]$HealthPort)

    try {
        $response = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$HealthPort/web/api/health" `
            -TimeoutSec 3
        return [bool]$response.ok -and `
            [string]$response.data.service -eq "storyforge-web"
    }
    catch {
        return $false
    }
}

if (Test-StoryForgeHub -HealthPort $Port) {
    exit 0
}

$executable = Join-Path $InstallPath "StoryForge Studio.exe"
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "StoryForge executable not found: $executable"
}
if (-not (Test-Path -LiteralPath $DataPath -PathType Container)) {
    throw "StoryForge data directory not found: $DataPath"
}

$env:STORYFORGE_DATA_DIR = $DataPath
Push-Location -LiteralPath $InstallPath
try {
    & $executable --web --web-host 0.0.0.0 --web-port $Port
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
""".replace("\n", "\r\n").encode("ascii")
        )

    def test_scripts_are_powershell_51_safe_ascii(self) -> None:
        for name in (
            "bootstrap_storyforge.ps1",
            "enable_storyforge_hub.ps1",
            "repair_storyforge_hub_launcher.ps1",
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

    def test_bootstrap_rejects_existing_reparse_point_roots_before_writes(self) -> None:
        self.assertIn("function Assert-ExistingOrdinaryDirectory", self.bootstrap)
        self.assertIn("[System.IO.FileAttributes]::ReparsePoint", self.bootstrap)
        install_check = (
            "Assert-ExistingOrdinaryDirectory -Path $InstallRoot -Label 'InstallRoot'"
        )
        data_check = (
            "Assert-ExistingOrdinaryDirectory -Path $DataRoot -Label 'DataRoot'"
        )
        self.assertIn(install_check, self.bootstrap)
        self.assertIn(data_check, self.bootstrap)
        first_write = self.bootstrap.index(
            "[System.IO.Directory]::CreateDirectory($InstallRoot)"
        )
        self.assertLess(self.bootstrap.index(install_check), first_write)
        self.assertLess(self.bootstrap.index(data_check), first_write)

    def test_fresh_replacement_intent_rechecks_state_around_slow_downloads(self) -> None:
        self.assertIn("[switch]$FreshReplacementHost", self.bootstrap)
        self.assertIn("function Get-FreshReplacementDataState", self.bootstrap)
        self.assertIn("function Assert-FreshReplacementDataState", self.bootstrap)
        self.assertIn("function Assert-FreshReplacementHostState", self.bootstrap)
        self.assertIn("Get-ScheduledTask", self.bootstrap)
        self.assertIn("Get-NetTCPConnection", self.bootstrap)
        self.assertIn("Win32_Process", self.bootstrap)
        self.assertIn("StoryForge Studio.exe", self.bootstrap)

        validation = "FreshReplacementHost requires Hub restore"
        self.assertIn(validation, self.bootstrap)
        self.assertLess(
            self.bootstrap.index(validation),
            self.bootstrap.index("$script:GhPath = Get-GhPath"),
        )

        guard_call = "Assert-FreshReplacementHostState `"
        guard_positions = []
        offset = 0
        while True:
            try:
                position = self.bootstrap.index(guard_call, offset)
            except ValueError:
                break
            guard_positions.append(position)
            offset = position + len(guard_call)
        self.assertGreaterEqual(len(guard_positions), 3)

        app_install = self.bootstrap.index("$appDirectory = Join-Path $InstallRoot")
        startup_self_test = self.bootstrap.index("& $entrypoint --startup-self-test")
        snapshot = self.bootstrap.index(
            "$freshInitializedDataState = @(", startup_self_test
        )
        hub_download = self.bootstrap.index("Downloading the authoritative latest Hub snapshot")
        restore = self.bootstrap.index("& $entrypoint --restore-hub-backup")
        self.assertTrue(
            any(app_install < item < startup_self_test for item in guard_positions),
            guard_positions,
        )
        self.assertTrue(startup_self_test < snapshot < hub_download)
        self.assertTrue(
            any(hub_download < item < restore for item in guard_positions),
            guard_positions,
        )
        restore_window = self.bootstrap[hub_download:restore]
        self.assertIn("-ExpectedDataState $freshInitializedDataState", restore_window)

    def test_fresh_intent_does_not_change_manual_replace_existing_semantics(self) -> None:
        self.assertIn(
            "$dataHasContent -and -not ($RestoreHubData -and $ReplaceExistingData)",
            self.bootstrap,
        )
        validation_start = self.bootstrap.index(
            "if (\n    $FreshReplacementHost -and"
        )
        validation_end = self.bootstrap.index("$dataHasContent = $false")
        validation = self.bootstrap[validation_start:validation_end]
        self.assertIn("FreshReplacementHost requires Hub restore", validation)
        self.assertNotIn("$dataHasContent", validation)

    def test_fresh_replacement_state_guard_detects_isolated_changes_and_activity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            install_root = root / "FreshHub"
            data_root = install_root / "Data"
            install_root.mkdir()
            data_root.mkdir()

            start = self.bootstrap.index(
                "function Assert-ExistingOrdinaryDirectory"
            )
            end = self.bootstrap.index("function Get-GhPath")
            function_file = root / "fresh-functions.ps1"
            function_file.write_text(
                self.bootstrap[start:end],
                encoding="ascii",
            )

            harness = root / "fresh-guard-harness.ps1"
            harness.write_text(
                r"""
$ErrorActionPreference = 'Stop'
. $env:STORYFORGE_FRESH_FUNCTIONS
$global:StoryForgeFreshMode = 'clean'
function Get-ScheduledTask {
    param(
        [string]$TaskName,
        [System.Management.Automation.ActionPreference]$ErrorAction
    )
    if ($global:StoryForgeFreshMode -eq 'task') {
        return [PSCustomObject]@{ TaskName = $TaskName }
    }
    return @()
}
function Get-NetTCPConnection {
    param(
        [string]$State,
        [int]$LocalPort,
        [System.Management.Automation.ActionPreference]$ErrorAction
    )
    if ($global:StoryForgeFreshMode -eq 'listener') {
        return [PSCustomObject]@{ OwningProcess = 4242 }
    }
    return @()
}
function Get-CimInstance {
    param(
        [string]$ClassName,
        [System.Management.Automation.ActionPreference]$ErrorAction
    )
    if ($global:StoryForgeFreshMode -eq 'process') {
        return [PSCustomObject]@{
            ProcessId = 4343
            Name = 'StoryForge Studio.exe'
            ExecutablePath = 'D:\StoryForgeHub\App-test\StoryForge Studio.exe'
            CommandLine = '"D:\StoryForgeHub\App-test\StoryForge Studio.exe" --web'
        }
    }
    return @()
}

[void](Assert-FreshReplacementHostState `
    -InstallRoot $env:STORYFORGE_TEST_INSTALL `
    -DataRoot $env:STORYFORGE_TEST_DATA `
    -TaskName 'StoryForge Hub' `
    -Port 8765 `
    -RequireEmptyData)

$generated = Join-Path $env:STORYFORGE_TEST_DATA 'generated.dat'
[System.IO.File]::WriteAllBytes($generated, [byte[]](1, 2, 3, 4))
$nonemptyRejected = $false
try {
    [void](Assert-FreshReplacementHostState `
        -InstallRoot $env:STORYFORGE_TEST_INSTALL `
        -DataRoot $env:STORYFORGE_TEST_DATA `
        -TaskName 'StoryForge Hub' `
        -Port 8765 `
        -RequireEmptyData)
}
catch { $nonemptyRejected = $true }
if (-not $nonemptyRejected) { throw 'Nonempty fresh DataRoot was accepted.' }
$expected = @(Get-FreshReplacementDataState -DataRoot $env:STORYFORGE_TEST_DATA)
if ($expected.Count -eq 0) { throw 'Expected generated state was empty.' }
[void](Assert-FreshReplacementHostState `
    -InstallRoot $env:STORYFORGE_TEST_INSTALL `
    -DataRoot $env:STORYFORGE_TEST_DATA `
    -TaskName 'StoryForge Hub' `
    -Port 8765 `
    -ExpectedDataState $expected)

[System.IO.File]::WriteAllBytes($generated, [byte[]](4, 3, 2, 1))
$changedRejected = $false
try {
    [void](Assert-FreshReplacementDataState `
        -DataRoot $env:STORYFORGE_TEST_DATA `
        -ExpectedDataState $expected)
}
catch { $changedRejected = $true }
if (-not $changedRejected) { throw 'Changed generated file was accepted.' }
[System.IO.File]::WriteAllBytes($generated, [byte[]](1, 2, 3, 4))

$unexpected = Join-Path $env:STORYFORGE_TEST_DATA 'unexpected.dat'
[System.IO.File]::WriteAllBytes($unexpected, [byte[]](9))
$addedRejected = $false
try {
    [void](Assert-FreshReplacementDataState `
        -DataRoot $env:STORYFORGE_TEST_DATA `
        -ExpectedDataState $expected)
}
catch { $addedRejected = $true }
if (-not $addedRejected) { throw 'Unexpected generated file was accepted.' }
Remove-Item -LiteralPath $unexpected -Force

foreach ($mode in @('task', 'listener', 'process')) {
    $global:StoryForgeFreshMode = $mode
    $rejected = $false
    try {
        [void](Assert-FreshReplacementHostState `
            -InstallRoot $env:STORYFORGE_TEST_INSTALL `
            -DataRoot $env:STORYFORGE_TEST_DATA `
            -TaskName 'StoryForge Hub' `
            -Port 8765 `
            -ExpectedDataState $expected)
    }
    catch { $rejected = $true }
    if (-not $rejected) { throw "Fresh guard accepted $mode activity." }
}
""".strip()
                + "\n",
                encoding="ascii",
            )
            result = self._run_windows_powershell(
                "-File",
                str(harness),
                environment_updates={
                    "STORYFORGE_FRESH_FUNCTIONS": str(function_file),
                    "STORYFORGE_TEST_INSTALL": str(install_root),
                    "STORYFORGE_TEST_DATA": str(data_root),
                },
            )
            output = (result.stdout + result.stderr).decode(
                "utf-8", errors="replace"
            )
            self.assertEqual(result.returncode, 0, output)

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
        self.assertIn(
            "'$env:STORYFORGE_DEPLOYMENT_ROLE = ''Hub'''",
            self.bootstrap,
        )

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

    def test_desktop_launchers_isolate_hub_and_employee_data_roots(self) -> None:
        launcher_function = self.bootstrap[
            self.bootstrap.index("function Write-DesktopLauncher") :
            self.bootstrap.index("function Assert-GitHubAsset")
        ]
        self.assertIn("[ValidateSet('Hub', 'Employee')]", launcher_function)
        self.assertIn("Hub desktop launcher requires DataRoot", launcher_function)
        self.assertIn('set `"STORYFORGE_DATA_DIR=$DataRoot`"', launcher_function)
        self.assertIn(
            'set `"STORYFORGE_DEPLOYMENT_ROLE=Hub`"',
            launcher_function,
        )
        self.assertIn('set `"STORYFORGE_DATA_DIR=`"', launcher_function)
        for inherited_name in (
            "STORYFORGE_DEPLOYMENT_ROLE",
            "STORYFORGE_FROZEN_HUB_DATA_ROOT",
            "STORYFORGE_PORTABLE_MODE",
        ):
            self.assertIn(
                f'set `"{inherited_name}=`"',
                launcher_function,
            )
        self.assertIn(
            "Write-DesktopLauncher -Destination $desktopLauncherPath -Entrypoint $entrypoint -Role 'Hub' -DataRoot $DataRoot",
            self.bootstrap,
        )
        self.assertIn(
            "Write-DesktopLauncher -Destination $desktopLauncherPath -Entrypoint $entrypoint -Role 'Employee'",
            self.bootstrap,
        )
        self.assertNotIn(
            "-Entrypoint $entrypoint -Role 'Employee' -DataRoot",
            self.bootstrap,
        )
        hub_branch = launcher_function[
            launcher_function.index("if ($Role -eq 'Hub')") :
            launcher_function.index("else")
        ]
        employee_branch = launcher_function[
            launcher_function.index("else") :
            launcher_function.index("$startCommand")
        ]
        self.assertNotIn("%*", hub_branch)
        self.assertIn("%*", employee_branch)

    def test_legacy_hub_enabler_requires_and_propagates_safe_data_root(self) -> None:
        self.assertIn(
            "[Parameter(Mandatory = $true)][string]$DataRoot",
            self.enable_hub,
        )
        self.assertIn("function Get-SafeFixedPath", self.enable_hub)
        self.assertIn("$resolvedDataRoot = Get-SafeFixedPath", self.enable_hub)
        self.assertIn("$env:STORYFORGE_DATA_DIR", self.enable_hub)
        self.assertIn(
            "'$env:STORYFORGE_DEPLOYMENT_ROLE = ''Hub'''",
            self.enable_hub,
        )
        self.assertIn("Start-StoryForge-Hub.ps1", self.enable_hub)
        self.assertIn("-File `\"$launcherPath`\"", self.enable_hub)
        self.assertNotIn("-Execute $resolvedExecutable", self.enable_hub)
        self.assertIn(
            "Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue",
            self.enable_hub,
        )
        self.assertIn(
            "Get-NetTCPConnection -State Listen -LocalPort $Port",
            self.enable_hub,
        )
        self.assertIn("$taskCreated = $true", self.enable_hub)
        self.assertIn("$listener.OwningProcess", self.enable_hub)
        self.assertIn("Win32_Process", self.enable_hub)
        self.assertIn("$listenerProcess.ExecutablePath", self.enable_hub)
        self.assertIn("$listenerProcess.CommandLine", self.enable_hub)
        self.assertIn("Stop-ScheduledTask -TaskName $TaskName", self.enable_hub)
        self.assertIn("Unregister-ScheduledTask -TaskName $TaskName", self.enable_hub)
        self.assertNotIn("-Force | Out-Null", self.enable_hub)
        self.assertLess(
            self.enable_hub.index("Get-ScheduledTask -TaskName $TaskName"),
            self.enable_hub.index("Register-ScheduledTask"),
        )
        self.assertLess(
            self.enable_hub.index("Get-NetTCPConnection -State Listen"),
            self.enable_hub.index("Register-ScheduledTask"),
        )

    def test_ops_hub_launcher_declares_the_fixed_hub_role(self) -> None:
        self.assertTrue(
            (self.project / "ops" / "Start-StoryForgeHub.ps1")
            .read_bytes()
            .isascii()
        )
        self.assertIn(
            '$env:STORYFORGE_DEPLOYMENT_ROLE = "Hub"',
            self.ops_hub,
        )
        self.assertIn(
            "Remove-Item Env:STORYFORGE_FROZEN_HUB_DATA_ROOT",
            self.ops_hub,
        )
        self.assertIn(
            "Remove-Item Env:STORYFORGE_PORTABLE_MODE",
            self.ops_hub,
        )
        self.assertLess(
            self.ops_hub.index("STORYFORGE_DEPLOYMENT_ROLE"),
            self.ops_hub.index("& $executable --web"),
        )

    def test_legacy_hub_enabler_rolls_back_only_its_fake_task_on_identity_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            release_root = root / "LegacyRelease"
            data_root = root / "ExistingData"
            release_root.mkdir()
            data_root.mkdir()
            executable = release_root / "StoryForge Studio.exe"
            executable.write_bytes(b"isolated-fixture")
            sentinel = data_root / "do-not-touch.bin"
            sentinel.write_bytes(b"preserve")
            isolated_enable_script = release_root / "enable_storyforge_hub.ps1"
            shutil.copyfile(
                self.project / "scripts" / "enable_storyforge_hub.ps1",
                isolated_enable_script,
            )

            environment_updates = {
                "STORYFORGE_ENABLE_SCRIPT": str(isolated_enable_script),
                "STORYFORGE_TEST_EXE": str(executable),
                "STORYFORGE_TEST_DATA_ROOT": str(data_root),
            }
            harness = r"""
$ErrorActionPreference = 'Stop'
$global:StoryForgeListenerCalls = 0
$global:StoryForgeRegistered = $false
$global:StoryForgeStarted = $false
$global:StoryForgeStopped = $false
$global:StoryForgeUnregistered = $false
function Get-ScheduledTask {
    param(
        [string]$TaskName,
        [System.Management.Automation.ActionPreference]$ErrorAction
    )
    return @()
}
function Get-NetTCPConnection {
    param(
        [string]$State,
        [int]$LocalPort,
        [System.Management.Automation.ActionPreference]$ErrorAction
    )
    $global:StoryForgeListenerCalls += 1
    if ($global:StoryForgeListenerCalls -eq 1) { return @() }
    return [PSCustomObject]@{ OwningProcess = 4242 }
}
function New-ScheduledTaskAction {
    param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    return [PSCustomObject]@{}
}
function New-ScheduledTaskTrigger {
    param([switch]$AtLogOn, [string]$User)
    return [PSCustomObject]@{}
}
function New-ScheduledTaskPrincipal {
    param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    return [PSCustomObject]@{}
}
function New-ScheduledTaskSettingsSet {
    param(
        [switch]$AllowStartIfOnBatteries,
        [switch]$DontStopIfGoingOnBatteries,
        [switch]$StartWhenAvailable,
        [string]$MultipleInstances,
        [int]$RestartCount,
        [TimeSpan]$RestartInterval,
        [TimeSpan]$ExecutionTimeLimit
    )
    return [PSCustomObject]@{}
}
function Register-ScheduledTask {
    param(
        [string]$TaskName,
        [object]$Action,
        [object]$Trigger,
        [object]$Principal,
        [object]$Settings,
        [string]$Description
    )
    $global:StoryForgeRegistered = $true
}
function Start-ScheduledTask {
    param([string]$TaskName)
    $global:StoryForgeStarted = $true
}
function Invoke-RestMethod {
    param([string]$Uri, [int]$TimeoutSec)
    return [PSCustomObject]@{
        ok = $true
        data = [PSCustomObject]@{ service = 'storyforge-web' }
    }
}
function Get-CimInstance {
    param(
        [string]$ClassName,
        [string]$Filter,
        [System.Management.Automation.ActionPreference]$ErrorAction
    )
    return [PSCustomObject]@{
        ExecutablePath = 'C:\Foreign\StoryForge Studio.exe'
        CommandLine = 'foreign process'
    }
}
function Stop-ScheduledTask {
    param(
        [string]$TaskName,
        [System.Management.Automation.ActionPreference]$ErrorAction
    )
    $global:StoryForgeStopped = $true
}
function Unregister-ScheduledTask {
    param(
        [string]$TaskName,
        [switch]$Confirm,
        [System.Management.Automation.ActionPreference]$ErrorAction
    )
    $global:StoryForgeUnregistered = $true
}
$caught = $false
try {
    & $env:STORYFORGE_ENABLE_SCRIPT `
        -ExecutablePath $env:STORYFORGE_TEST_EXE `
        -DataRoot $env:STORYFORGE_TEST_DATA_ROOT
}
catch {
    $caught = $_.Exception.Message -like '*not owned by the exact*'
}
if (
    -not $caught -or
    -not $global:StoryForgeRegistered -or
    -not $global:StoryForgeStarted -or
    -not $global:StoryForgeStopped -or
    -not $global:StoryForgeUnregistered
) {
    throw 'The isolated first-enable rollback contract was not observed.'
}
"""
            result = self._run_windows_powershell(
                "-Command",
                harness,
                environment_updates=environment_updates,
            )
            output = (result.stdout + result.stderr).decode(
                "utf-8", errors="replace"
            )
            self.assertEqual(result.returncode, 0, output)
            self.assertEqual(sentinel.read_bytes(), b"preserve")

    def test_existing_hub_launcher_repair_is_identity_bound_and_data_read_only(
        self,
    ) -> None:
        required = (
            "current.json",
            "storyforge-update.json",
            "storyforge-catalog.sqlite3",
            "settings.json",
            "App-$pointerVersion",
            "$settings.settings.hub.mode",
            "$task.Actions",
            "$task.Principal.UserId",
            "$task.Principal.LogonType",
            "$task.Principal.RunLevel",
            "$expectedTaskArguments",
            "$actionExecutableValue",
            "$actionWorkingDirectoryValue",
            "IsPathRooted($action.WorkingDirectory)",
            "$previousEntrypoint",
            "$previousVersion",
            "$previousAppDirectory",
            "$previousInternalManifest",
            "$oldLauncherPattern",
            "$desktopLauncherPath",
            "$TargetAppDirectory",
            "$legacyOpsLauncherPath",
            "$expectedLegacyTaskArguments",
            "BUILD_RELEASE_VALIDATION.json",
            "bundle_manifest_sha256",
            "Set-ScheduledTask",
            "$previousLegacyDesktopLauncherContent",
            "$previousRoleDesktopLauncherContent",
            "$newDesktopLauncherContent",
            "$newLauncherContent",
            "[System.IO.File]::Replace",
            "STORYFORGE_DEPLOYMENT_ROLE = ''Hub''",
            "STORYFORGE_FROZEN_HUB_DATA_ROOT",
            "STORYFORGE_PORTABLE_MODE",
        )
        for item in required:
            with self.subTest(item=item):
                self.assertIn(item, self.repair_hub)

        forbidden = (
            "Start-ScheduledTask",
            "Stop-ScheduledTask",
            "Register-ScheduledTask",
            "Unregister-ScheduledTask",
            "RestoreHubData",
            "ReplaceExistingData",
            "sqlite3.exe",
            "System.Data.SQLite",
            "Get-Content -LiteralPath $catalogPath",
        )
        for item in forbidden:
            with self.subTest(item=item):
                self.assertNotIn(item, self.repair_hub)

        self.assertIn(
            "The previous managed release manifest does not match",
            self.repair_hub,
        )
        self.assertIn("App-(?<version>", self.repair_hub)
        self.assertNotIn("$oldLauncherContent", self.repair_hub)
        self.assertGreaterEqual(
            self.repair_hub.count("[System.IO.File]::Replace"),
            2,
        )

        self.assertIn(
            "repair_storyforge_hub_launcher.ps1",
            (
                self.project / "scripts" / "build_exe.ps1"
            ).read_text(encoding="utf-8"),
        )

    def test_legacy_ops_task_migrates_to_modern_launchers_without_touching_data(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            hub_root = root / "ManagedHub"
            data_root = hub_root / "Data"
            previous_app = hub_root / "App-1.0.0"
            target_app = hub_root / "App-1.0.2"
            for directory in (data_root, previous_app):
                directory.mkdir(parents=True)
            previous_exe = previous_app / "StoryForge Studio.exe"
            previous_exe.write_bytes(b"real-legacy-layout-fixture")
            target_exe = self._write_verified_target_app(target_app, "1.0.2")
            settings = data_root / "settings.json"
            settings.write_text(
                json.dumps({"settings": {"hub": {"mode": "host"}}}),
                encoding="utf-8",
            )
            catalog = data_root / "storyforge-catalog.sqlite3"
            catalog.write_bytes(b"opaque-catalog-sentinel")
            legacy_wrapper = hub_root / "Start-StoryForgeHub.ps1"
            self._write_legacy_ops_wrapper(legacy_wrapper)

            protected = {
                path: path.read_bytes()
                for path in hub_root.rglob("*")
                if path.is_file()
            }
            environment_updates = {
                "STORYFORGE_REPAIR_SCRIPT": str(
                    self.project / "scripts" / "repair_storyforge_hub_launcher.ps1"
                ),
                "STORYFORGE_TEST_HUB_ROOT": str(hub_root),
                "STORYFORGE_TEST_DATA_ROOT": str(data_root),
                "STORYFORGE_TEST_TARGET_APP": str(target_app),
                "STORYFORGE_TEST_LEGACY_APP": str(previous_app),
                "STORYFORGE_TEST_LEGACY_WRAPPER": str(legacy_wrapper),
            }
            harness = r"""
$ErrorActionPreference = 'Stop'
$legacyArguments = "-NoProfile -ExecutionPolicy Bypass -File $env:STORYFORGE_TEST_LEGACY_WRAPPER -InstallPath $env:STORYFORGE_TEST_LEGACY_APP -DataPath $env:STORYFORGE_TEST_DATA_ROOT -Port 8765"
$global:StoryForgeSetCount = 0
$global:StoryForgeFakeTask = [PSCustomObject]@{
    Actions = @([PSCustomObject]@{
        Execute = 'powershell.exe'
        Arguments = $legacyArguments
        WorkingDirectory = ''
    })
    Principal = [PSCustomObject]@{
        UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        LogonType = 'Interactive'
        RunLevel = 'Limited'
    }
}
function Get-ScheduledTask {
    param(
        [string]$TaskName,
        [string]$TaskPath,
        [System.Management.Automation.ActionPreference]$ErrorAction
    )
    return $global:StoryForgeFakeTask
}
function New-ScheduledTaskAction {
    param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    return [PSCustomObject]@{
        Execute = $Execute
        Arguments = $Argument
        WorkingDirectory = $WorkingDirectory
    }
}
function Set-ScheduledTask {
    param(
        [string]$TaskName,
        [string]$TaskPath,
        [object[]]$Action,
        [System.Management.Automation.ActionPreference]$ErrorAction
    )
    $global:StoryForgeSetCount += 1
    $global:StoryForgeFakeTask.Actions = @($Action)
    return $global:StoryForgeFakeTask
}
& $env:STORYFORGE_REPAIR_SCRIPT `
    -HubRoot $env:STORYFORGE_TEST_HUB_ROOT `
    -DataRoot $env:STORYFORGE_TEST_DATA_ROOT `
    -TargetAppDirectory $env:STORYFORGE_TEST_TARGET_APP
$modernLauncher = Join-Path $env:STORYFORGE_TEST_HUB_ROOT 'Start-StoryForge-Hub.ps1'
$expectedPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$expectedArguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$modernLauncher`""
$finalAction = @($global:StoryForgeFakeTask.Actions)[0]
if (
    $global:StoryForgeSetCount -ne 1 -or
    [System.IO.Path]::GetFullPath([string]$finalAction.Execute) -ne [System.IO.Path]::GetFullPath($expectedPowerShell) -or
    [string]$finalAction.Arguments -ne $expectedArguments -or
    [System.IO.Path]::GetFullPath([string]$finalAction.WorkingDirectory).TrimEnd('\') -ne [System.IO.Path]::GetFullPath($env:STORYFORGE_TEST_HUB_ROOT).TrimEnd('\')
) {
    throw 'The isolated legacy task was not switched to the exact modern action.'
}
"""
            result = self._run_windows_powershell(
                "-Command",
                harness,
                environment_updates=environment_updates,
            )
            output = (result.stdout + result.stderr).decode(
                "utf-8", errors="replace"
            )
            self.assertEqual(result.returncode, 0, output)

            pointer = json.loads((hub_root / "current.json").read_text("utf-8"))
            self.assertEqual(pointer["version"], "1.0.2")
            self.assertEqual(pointer["app_directory"], str(target_app))
            self.assertEqual(pointer["entrypoint"], str(target_exe))
            task_launcher = (hub_root / "Start-StoryForge-Hub.ps1").read_text(
                encoding="ascii"
            )
            desktop_launcher = (hub_root / "Start-StoryForge.cmd").read_text(
                encoding="ascii"
            )
            for launcher in (task_launcher, desktop_launcher):
                self.assertIn("STORYFORGE_DEPLOYMENT_ROLE", launcher)
                self.assertIn(str(data_root), launcher)
                self.assertIn(str(target_exe), launcher)
                self.assertNotIn(str(previous_exe), launcher)
            self.assertNotIn("%*", desktop_launcher)
            for path, before in protected.items():
                with self.subTest(path=path):
                    self.assertEqual(path.read_bytes(), before)

    def test_legacy_ops_task_migration_rejects_ambiguity_without_writes(
        self,
    ) -> None:
        for case in (
            "malformed_action",
            "partial_modern_state",
            "tampered_target",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve(strict=True)
                hub_root = root / "ManagedHub"
                data_root = hub_root / "Data"
                previous_app = hub_root / "App-1.0.0"
                target_app = hub_root / "App-1.0.2"
                for directory in (data_root, previous_app):
                    directory.mkdir(parents=True)
                previous_exe = previous_app / "StoryForge Studio.exe"
                previous_exe.write_bytes(b"legacy")
                target_exe = self._write_verified_target_app(target_app, "1.0.2")
                if case == "tampered_target":
                    target_exe.write_bytes(b"changed-after-release-validation")
                settings = data_root / "settings.json"
                settings.write_text(
                    json.dumps({"settings": {"hub": {"mode": "host"}}}),
                    encoding="utf-8",
                )
                catalog = data_root / "storyforge-catalog.sqlite3"
                catalog.write_bytes(b"opaque-catalog-sentinel")
                legacy_wrapper = hub_root / "Start-StoryForgeHub.ps1"
                self._write_legacy_ops_wrapper(legacy_wrapper)
                partial_pointer = hub_root / "current.json"
                if case == "partial_modern_state":
                    partial_pointer.write_bytes(b"do-not-overwrite")
                before = {
                    path: path.read_bytes()
                    for path in hub_root.rglob("*")
                    if path.is_file()
                }
                environment_updates = {
                    "STORYFORGE_REPAIR_SCRIPT": str(
                        self.project
                        / "scripts"
                        / "repair_storyforge_hub_launcher.ps1"
                    ),
                    "STORYFORGE_TEST_HUB_ROOT": str(hub_root),
                    "STORYFORGE_TEST_DATA_ROOT": str(data_root),
                    "STORYFORGE_TEST_TARGET_APP": str(target_app),
                    "STORYFORGE_TEST_LEGACY_APP": str(previous_app),
                    "STORYFORGE_TEST_LEGACY_WRAPPER": str(legacy_wrapper),
                    "STORYFORGE_TEST_EXTRA_ARGUMENT": (
                        " -Unexpected" if case == "malformed_action" else ""
                    ),
                }
                harness = r"""
$ErrorActionPreference = 'Stop'
$legacyArguments = "-NoProfile -ExecutionPolicy Bypass -File $env:STORYFORGE_TEST_LEGACY_WRAPPER -InstallPath $env:STORYFORGE_TEST_LEGACY_APP -DataPath $env:STORYFORGE_TEST_DATA_ROOT -Port 8765$env:STORYFORGE_TEST_EXTRA_ARGUMENT"
$global:StoryForgeSetCount = 0
$global:StoryForgeFakeTask = [PSCustomObject]@{
    Actions = @([PSCustomObject]@{ Execute = 'powershell.exe'; Arguments = $legacyArguments; WorkingDirectory = '' })
    Principal = [PSCustomObject]@{ UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name; LogonType = 'Interactive'; RunLevel = 'Limited' }
}
function Get-ScheduledTask {
    param([string]$TaskName, [string]$TaskPath, [System.Management.Automation.ActionPreference]$ErrorAction)
    return $global:StoryForgeFakeTask
}
function New-ScheduledTaskAction {
    param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    return [PSCustomObject]@{ Execute = $Execute; Arguments = $Argument; WorkingDirectory = $WorkingDirectory }
}
function Set-ScheduledTask {
    param([string]$TaskName, [string]$TaskPath, [object[]]$Action, [System.Management.Automation.ActionPreference]$ErrorAction)
    $global:StoryForgeSetCount += 1
}
$caught = $false
try {
    & $env:STORYFORGE_REPAIR_SCRIPT -HubRoot $env:STORYFORGE_TEST_HUB_ROOT -DataRoot $env:STORYFORGE_TEST_DATA_ROOT -TargetAppDirectory $env:STORYFORGE_TEST_TARGET_APP
}
catch {
    $caught = $true
}
if (-not $caught -or $global:StoryForgeSetCount -ne 0) {
    throw 'Ambiguous legacy state did not fail closed before task mutation.'
}
"""
                result = self._run_windows_powershell(
                    "-Command",
                    harness,
                    environment_updates=environment_updates,
                )
                output = (result.stdout + result.stderr).decode(
                    "utf-8", errors="replace"
                )
                self.assertEqual(result.returncode, 0, output)
                after = {
                    path: path.read_bytes()
                    for path in hub_root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)

    def test_legacy_ops_task_migration_rolls_back_files_and_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            hub_root = root / "ManagedHub"
            data_root = hub_root / "Data"
            previous_app = hub_root / "App-1.0.0"
            target_app = hub_root / "App-1.0.2"
            for directory in (data_root, previous_app):
                directory.mkdir(parents=True)
            (previous_app / "StoryForge Studio.exe").write_bytes(b"legacy")
            self._write_verified_target_app(target_app, "1.0.2")
            (data_root / "settings.json").write_text(
                json.dumps({"settings": {"hub": {"mode": "host"}}}),
                encoding="utf-8",
            )
            (data_root / "storyforge-catalog.sqlite3").write_bytes(
                b"opaque-catalog-sentinel"
            )
            legacy_wrapper = hub_root / "Start-StoryForgeHub.ps1"
            self._write_legacy_ops_wrapper(legacy_wrapper)
            before = {
                path: path.read_bytes()
                for path in hub_root.rglob("*")
                if path.is_file()
            }
            environment_updates = {
                "STORYFORGE_REPAIR_SCRIPT": str(
                    self.project / "scripts" / "repair_storyforge_hub_launcher.ps1"
                ),
                "STORYFORGE_TEST_HUB_ROOT": str(hub_root),
                "STORYFORGE_TEST_DATA_ROOT": str(data_root),
                "STORYFORGE_TEST_TARGET_APP": str(target_app),
                "STORYFORGE_TEST_LEGACY_APP": str(previous_app),
                "STORYFORGE_TEST_LEGACY_WRAPPER": str(legacy_wrapper),
            }
            harness = r"""
$ErrorActionPreference = 'Stop'
$legacyArguments = "-NoProfile -ExecutionPolicy Bypass -File $env:STORYFORGE_TEST_LEGACY_WRAPPER -InstallPath $env:STORYFORGE_TEST_LEGACY_APP -DataPath $env:STORYFORGE_TEST_DATA_ROOT -Port 8765"
$legacyAction = [PSCustomObject]@{ Execute = 'powershell.exe'; Arguments = $legacyArguments; WorkingDirectory = '' }
$global:StoryForgeSetCount = 0
$global:StoryForgeFakeTask = [PSCustomObject]@{
    Actions = @($legacyAction)
    Principal = [PSCustomObject]@{ UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name; LogonType = 'Interactive'; RunLevel = 'Limited' }
}
function Get-ScheduledTask {
    param([string]$TaskName, [string]$TaskPath, [System.Management.Automation.ActionPreference]$ErrorAction)
    return $global:StoryForgeFakeTask
}
function New-ScheduledTaskAction {
    param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    return [PSCustomObject]@{ Execute = $Execute; Arguments = $Argument; WorkingDirectory = $WorkingDirectory }
}
function Set-ScheduledTask {
    param([string]$TaskName, [string]$TaskPath, [object[]]$Action, [System.Management.Automation.ActionPreference]$ErrorAction)
    $global:StoryForgeSetCount += 1
    if ($global:StoryForgeSetCount -eq 1) {
        $requested = @($Action)[0]
        $global:StoryForgeFakeTask.Actions = @([PSCustomObject]@{
            Execute = $requested.Execute
            Arguments = ([string]$requested.Arguments + ' -Unexpected')
            WorkingDirectory = $requested.WorkingDirectory
        })
    }
    else {
        $global:StoryForgeFakeTask.Actions = @($Action)
    }
    return $global:StoryForgeFakeTask
}
$caught = $false
try {
    & $env:STORYFORGE_REPAIR_SCRIPT -HubRoot $env:STORYFORGE_TEST_HUB_ROOT -DataRoot $env:STORYFORGE_TEST_DATA_ROOT -TargetAppDirectory $env:STORYFORGE_TEST_TARGET_APP
}
catch {
    $caught = $true
}
$finalAction = @($global:StoryForgeFakeTask.Actions)[0]
if (
    -not $caught -or
    $global:StoryForgeSetCount -ne 2 -or
    [string]$finalAction.Execute -ne 'powershell.exe' -or
    [string]$finalAction.Arguments -ne $legacyArguments -or
    -not [string]::IsNullOrWhiteSpace([string]$finalAction.WorkingDirectory)
) {
    throw 'The legacy task rollback contract was not observed.'
}
"""
            result = self._run_windows_powershell(
                "-Command",
                harness,
                environment_updates=environment_updates,
            )
            output = (result.stdout + result.stderr).decode(
                "utf-8", errors="replace"
            )
            self.assertEqual(result.returncode, 0, output)
            after = {
                path: path.read_bytes()
                for path in hub_root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_existing_hub_upgrade_docs_use_the_read_only_launcher_repair(self) -> None:
        deployment = (
            self.project / "docs" / "DEPLOYMENT_WINDOWS.md"
        ).read_text(encoding="utf-8")
        updates = (
            self.project / "docs" / "AUTO_UPDATE.md"
        ).read_text(encoding="utf-8")
        for document in (deployment, updates):
            self.assertIn("repair_storyforge_hub_launcher.ps1", document)
            self.assertIn("不会读取或修改 SQLite", document)
            self.assertIn("current.json", document)
            self.assertIn("Start-StoryForge-Hub.ps1", document)
            self.assertIn("Start-StoryForge.cmd", document)
            self.assertIn("Start-StoryForgeHub.ps1", document)
            self.assertIn("-TargetAppDirectory", document)
            self.assertIn("BUILD_RELEASE_VALIDATION.json", document)
            self.assertIn("enable_storyforge_hub.ps1", document)
            self.assertIn("首次启用", document)
        recovery = (
            self.project / "docs" / "NEW_MACHINE_RECOVERY.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Start-StoryForgeHub.ps1", recovery)
        self.assertIn("repair_storyforge_hub_launcher.ps1", recovery)
        self.assertIn("-TargetAppDirectory", recovery)
        self.assertIn("绝不能对它运行一键恢复或 bootstrap", recovery)
        self.assertIn("不读取或修改 SQLite", recovery)

    def test_existing_hub_repair_switches_a_verified_previous_wrapper_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            hub_root = root / "ManagedHub"
            data_root = hub_root / "Data"
            previous_app = hub_root / "App-1.0.1"
            current_app = hub_root / "App-1.0.2"
            for directory in (data_root, previous_app, current_app):
                directory.mkdir(parents=True, exist_ok=True)

            previous_exe = previous_app / "StoryForge Studio.exe"
            current_exe = current_app / "StoryForge Studio.exe"
            previous_exe.write_bytes(b"previous-release")
            current_exe.write_bytes(b"current-release")
            for app, version in ((previous_app, "1.0.1"), (current_app, "1.0.2")):
                (app / "storyforge-update.json").write_text(
                    json.dumps(
                        {"version": version, "entrypoint": "StoryForge Studio.exe"}
                    ),
                    encoding="utf-8",
                )

            pointer_path = hub_root / "current.json"
            pointer_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "version": "1.0.2",
                        "app_directory": str(current_app),
                        "entrypoint": str(current_exe),
                    }
                ),
                encoding="utf-8",
            )
            settings_path = data_root / "settings.json"
            settings_path.write_text(
                json.dumps({"settings": {"hub": {"mode": "host"}}}),
                encoding="utf-8",
            )
            catalog_path = data_root / "storyforge-catalog.sqlite3"
            catalog_path.write_bytes(b"isolated-fixture-not-sqlite")
            launcher_path = hub_root / "Start-StoryForge-Hub.ps1"
            launcher_path.write_bytes(
                "\r\n".join(
                    (
                        "$ErrorActionPreference = 'Stop'",
                        f"$env:STORYFORGE_DATA_DIR = '{data_root}'",
                        f"& '{previous_exe}' --web --web-host 0.0.0.0 --web-port 8765",
                        "exit $LASTEXITCODE",
                        "",
                    )
                ).encode("ascii")
            )
            desktop_launcher_path = hub_root / "Start-StoryForge.cmd"
            desktop_launcher_path.write_bytes(
                (
                    "@echo off\r\n"
                    f'start "" "{previous_exe}" %*\r\n'
                ).encode("ascii")
            )

            protected_before = {
                pointer_path: pointer_path.read_bytes(),
                settings_path: settings_path.read_bytes(),
                catalog_path: catalog_path.read_bytes(),
                previous_exe: previous_exe.read_bytes(),
                current_exe: current_exe.read_bytes(),
            }
            environment_updates = {
                "STORYFORGE_REPAIR_SCRIPT": str(
                    self.project / "scripts" / "repair_storyforge_hub_launcher.ps1"
                ),
                "STORYFORGE_TEST_HUB_ROOT": str(hub_root),
                "STORYFORGE_TEST_DATA_ROOT": str(data_root),
                "STORYFORGE_TEST_LAUNCHER": str(launcher_path),
            }
            harness = r"""
$ErrorActionPreference = 'Stop'
$powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$env:STORYFORGE_TEST_LAUNCHER`""
$expectedUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$global:StoryForgeFakeTask = [PSCustomObject]@{
    Actions = @([PSCustomObject]@{
        Execute = $powerShell
        Arguments = $arguments
        WorkingDirectory = $env:STORYFORGE_TEST_HUB_ROOT
    })
    Principal = [PSCustomObject]@{
        UserId = $expectedUser
        LogonType = 'Interactive'
        RunLevel = 'Limited'
    }
}
function Get-ScheduledTask {
    param(
        [string]$TaskName,
        [string]$TaskPath,
        [System.Management.Automation.ActionPreference]$ErrorAction
    )
    return $global:StoryForgeFakeTask
}
try {
    & $env:STORYFORGE_REPAIR_SCRIPT `
        -HubRoot $env:STORYFORGE_TEST_HUB_ROOT `
        -DataRoot $env:STORYFORGE_TEST_DATA_ROOT
}
catch {
    $actualAction = @($global:StoryForgeFakeTask.Actions)[0]
    $actualPrincipal = $global:StoryForgeFakeTask.Principal
    $diagnostic = @(
        "repair_error=$($_.Exception.Message)",
        "execute.expected=$powerShell",
        "execute.actual=$([string]$actualAction.Execute)",
        "arguments.expected=$arguments",
        "arguments.actual=$([string]$actualAction.Arguments)",
        "working_directory.expected=$env:STORYFORGE_TEST_HUB_ROOT",
        "working_directory.actual=$([string]$actualAction.WorkingDirectory)",
        "user.expected=$expectedUser",
        "user.actual=$([string]$actualPrincipal.UserId)",
        'logon_type.expected=Interactive',
        "logon_type.actual=$([string]$actualPrincipal.LogonType)",
        'run_level.expected=Limited',
        "run_level.actual=$([string]$actualPrincipal.RunLevel)"
    )
    throw ($diagnostic -join "`n")
}
"""
            result = self._run_windows_powershell(
                "-Command",
                harness,
                environment_updates=environment_updates,
            )
            output = (result.stdout + result.stderr).decode(
                "utf-8", errors="replace"
            )
            self.assertEqual(result.returncode, 0, output)

            repaired = launcher_path.read_text(encoding="ascii")
            self.assertIn("STORYFORGE_DEPLOYMENT_ROLE = 'Hub'", repaired)
            self.assertIn(str(current_exe), repaired)
            self.assertNotIn(str(previous_exe), repaired)
            repaired_desktop = desktop_launcher_path.read_text(encoding="ascii")
            self.assertIn("STORYFORGE_DEPLOYMENT_ROLE=Hub", repaired_desktop)
            self.assertIn(f"STORYFORGE_DATA_DIR={data_root}", repaired_desktop)
            self.assertIn(str(current_exe), repaired_desktop)
            self.assertNotIn(str(previous_exe), repaired_desktop)
            self.assertNotIn("%*", repaired_desktop)
            for path, before in protected_before.items():
                with self.subTest(path=path):
                    self.assertEqual(path.read_bytes(), before)

            # A previous repair version may already have switched the scheduled
            # task wrapper while leaving the desktop launcher behind. That
            # partial legacy state must be repairable without touching any
            # protected deployment or data file.
            desktop_launcher_path.write_bytes(
                (
                    "@echo off\r\n"
                    f'start "" "{previous_exe}" %*\r\n'
                ).encode("ascii")
            )
            second_result = self._run_windows_powershell(
                "-Command",
                harness,
                environment_updates=environment_updates,
            )
            second_output = (second_result.stdout + second_result.stderr).decode(
                "utf-8", errors="replace"
            )
            self.assertEqual(second_result.returncode, 0, second_output)
            repaired_desktop = desktop_launcher_path.read_text(encoding="ascii")
            self.assertIn(str(current_exe), repaired_desktop)
            self.assertNotIn(str(previous_exe), repaired_desktop)
            self.assertNotIn("%*", repaired_desktop)
            for path, before in protected_before.items():
                with self.subTest(path=path):
                    self.assertEqual(path.read_bytes(), before)

    def test_publisher_keeps_one_private_latest_snapshot_pair(self) -> None:
        self.assertIn("--create-hub-backup", self.publish)
        self.assertIn("Refusing to publish Hub business data to a public repository", self.publish)
        self.assertIn("$previousErrorActionPreference = $ErrorActionPreference", self.publish)
        self.assertIn("$existingExitCode = $LASTEXITCODE", self.publish)
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
