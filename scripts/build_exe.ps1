[CmdletBinding()]
param(
    [switch]$WithLocalAI,
    [switch]$OneFile,
    [switch]$Standalone,
    [switch]$SkipDependencyInstall,
    [switch]$ReuseBuildCache,
    [string]$OutputDirectory = '',
    [string]$WorkDirectory = '',
    [string]$PythonExe = '',
    [string]$HubEndpoint = '',
    [string]$HubSiteName = 'StoryForge Hub',
    [switch]$ReleaseBuild,
    [switch]$RequireStableAcceptance,
    [double]$StableStressSeconds = 600,
    [ValidateSet('libx264', 'h264_nvenc', 'h264_qsv', 'h264_amf')]
    [string]$StableAcceptanceEncoder = 'libx264',
    [string]$StableAcceptanceReport = '',
    [string]$StableFfprobe = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$releasePackageSmokeReport = ''
if ($ReleaseBuild -and -not $RequireStableAcceptance) {
    throw '-ReleaseBuild requires the explicit -RequireStableAcceptance switch. Ordinary local builds should omit both switches.'
}
if ($ReleaseBuild -and $StableStressSeconds -lt 600) {
    throw '-ReleaseBuild requires -StableStressSeconds of at least 600.'
}
$specPath = Join-Path $projectRoot 'StoryForge.spec'
$buildVenv = Join-Path $projectRoot '.build-venv'
$venvPython = Join-Path $buildVenv 'Scripts\python.exe'
$projectPathNeedsAsciiDefaults = $projectRoot -match '[^\x00-\x7F]'
$asciiDefaultBuildBase = $null
if ($projectPathNeedsAsciiDefaults) {
    $projectVolumeRoot = [System.IO.Path]::GetPathRoot($projectRoot)
    if (-not $projectVolumeRoot) {
        throw "Could not resolve the project volume root for ASCII build defaults: $projectRoot"
    }
    $asciiDefaultBuildBase = Join-Path $projectVolumeRoot 'StoryForgeBuildTemp'
}
if ($WorkDirectory) {
    if ([System.IO.Path]::IsPathRooted($WorkDirectory)) {
        $buildRoot = [System.IO.Path]::GetFullPath($WorkDirectory)
    }
    else {
        $buildRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $WorkDirectory))
    }
}
elseif ($projectPathNeedsAsciiDefaults) {
    $buildRoot = Join-Path $asciiDefaultBuildBase 'work'
}
else {
    $buildRoot = Join-Path $projectRoot 'build'
}
$workPath = Join-Path $buildRoot 'pyinstaller'
[System.IO.Directory]::CreateDirectory($buildRoot) | Out-Null

function Resolve-BasePython {
    param([string]$Requested)

    if ($Requested) {
        $requestedCommand = Get-Command -Name $Requested -ErrorAction Stop
        return [pscustomobject]@{
            Path = $requestedCommand.Source
            Prefix = @()
        }
    }

    $python = Get-Command -Name 'python' -ErrorAction SilentlyContinue
    if ($python) {
        return [pscustomobject]@{
            Path = $python.Source
            Prefix = @()
        }
    }

    $launcher = Get-Command -Name 'py' -ErrorAction SilentlyContinue
    if ($launcher) {
        return [pscustomobject]@{
            Path = $launcher.Source
            Prefix = @('-3')
        }
    }

    throw 'Python 3 was not found. Packaging requires Python 3.11+.'
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(Mandatory = $true)]
        [string[]]$CommandArguments
    )

    & $Command @CommandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $Command (exit code $LASTEXITCODE)."
    }
}

function Assert-AsciiBuildPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PathValue,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if ($PathValue -match '[^\x00-\x7F]') {
        throw "$Label must use an ASCII-only path for the employee portable build. Use a path such as D:\StoryForgeBuildTemp. Current path: $PathValue"
    }
}

$basePython = Resolve-BasePython -Requested $PythonExe
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Host "Creating an isolated build environment: $buildVenv"
    & $basePython.Path @($basePython.Prefix) -m venv $buildVenv
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the build environment (exit code $LASTEXITCODE)."
    }
}

$versionAssertion = if ($WithLocalAI) {
    "import sys; assert (3, 11) <= sys.version_info < (3, 13), 'Bundled Kokoro requires Python 3.11 or 3.12'"
}
else {
    "import sys; assert sys.version_info >= (3, 11), 'StoryForge requires Python 3.11+'"
}
Invoke-Checked -Command $venvPython -CommandArguments @('-c', $versionAssertion)
$expectedAppVersion = (& $venvPython -c 'import sys; sys.path.insert(0, sys.argv[1]); from storyforge import __version__; print(__version__)' $projectRoot).Trim()
if ($LASTEXITCODE -ne 0 -or -not $expectedAppVersion) {
    throw 'Could not read the StoryForge application version from the source tree.'
}
if ($SkipDependencyInstall) {
    Write-Host 'Using the existing verified build environment (dependency download skipped)...'
    Invoke-Checked -Command $venvPython -CommandArguments @('-m', 'pip', 'check')
    $coreModules = 'imageio_ffmpeg,webview,PyInstaller,edge_tts,clr,clr_loader'
    $requiredModules = if ($WithLocalAI) {
        "$coreModules,kokoro,misaki,pyopenjtalk,fugashi,jaconv,mojimoji,phonemizer,espeakng_loader,torch,transformers,spacy,numpy,soundfile,en_core_web_sm"
    }
    else {
        $coreModules
    }
    $moduleCheck = "import importlib.util as i; names='$requiredModules'.split(','); missing=[n for n in names if i.find_spec(n) is None]; assert not missing, f'Missing build modules: {missing}'"
    Invoke-Checked -Command $venvPython -CommandArguments @('-c', $moduleCheck)
}
else {
    Write-Host 'Installing/checking packaging dependencies...'
    Invoke-Checked -Command $venvPython -CommandArguments @(
        '-m', 'pip', 'install', '--disable-pip-version-check', '-r',
        (Join-Path $projectRoot 'requirements-dev.txt')
    )

    if ($WithLocalAI) {
        Write-Host 'Installing local Kokoro dependencies; this can take a while...'
        Invoke-Checked -Command $venvPython -CommandArguments @(
            '-m', 'pip', 'install', '--disable-pip-version-check', '-r',
            (Join-Path $projectRoot 'requirements-ai.txt')
        )
    }
}

if ($WithLocalAI) {
    $env:STORYFORGE_BUNDLE_LOCAL_AI = '1'
}
else {
    $env:STORYFORGE_BUNDLE_LOCAL_AI = '0'
}
$env:STORYFORGE_BUILD_MODE = if ($OneFile) { 'onefile' } else { 'onedir' }

if ($OutputDirectory) {
    if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
        $distPath = [System.IO.Path]::GetFullPath($OutputDirectory)
    }
    else {
        $distPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
    }
}
elseif ($projectPathNeedsAsciiDefaults) {
    $distPath = Join-Path $asciiDefaultBuildBase 'dist'
}
else {
    $distPath = Join-Path $projectRoot 'dist'
}
[System.IO.Directory]::CreateDirectory($distPath) | Out-Null
Assert-AsciiBuildPath -PathValue $distPath -Label 'OutputDirectory'
Assert-AsciiBuildPath -PathValue $buildRoot -Label 'WorkDirectory'

Write-Host "Packaging StoryForge Studio to: $distPath"
Push-Location -LiteralPath $projectRoot
try {
    $pyInstallerArguments = @('-m', 'PyInstaller', '--noconfirm')
    if (-not $ReuseBuildCache) {
        $pyInstallerArguments += '--clean'
    }
    $pyInstallerArguments += @(
        '--distpath', $distPath, '--workpath', $workPath, $specPath
    )
    Invoke-Checked -Command $venvPython -CommandArguments $pyInstallerArguments
}
finally {
    Pop-Location
    Remove-Item Env:STORYFORGE_BUNDLE_LOCAL_AI -ErrorAction SilentlyContinue
    Remove-Item Env:STORYFORGE_BUILD_MODE -ErrorAction SilentlyContinue
}

$bundleRoot = if ($OneFile) {
    $distPath
}
else {
    Join-Path $distPath 'StoryForge Studio'
}
$exePath = Join-Path $bundleRoot 'StoryForge Studio.exe'
if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
    throw "PyInstaller finished but the expected executable was not found: $exePath"
}

# Windows can propagate Mark-of-the-Web from a downloaded ZIP to the bundled
# Python.NET assemblies. .NET Framework then refuses to load Python.Runtime.dll
# on an employee computer even though the same build starts on the build host.
# Keep the narrowly scoped .NET switch beside the frozen executable so verified
# portable ZIPs remain loadable after they are copied or extracted elsewhere.
$dotNetConfigPath = Join-Path $bundleRoot 'StoryForge Studio.exe.config'
$dotNetConfig = @'
<?xml version="1.0" encoding="utf-8" ?>
<configuration>
  <runtime>
    <loadFromRemoteSources enabled="true" />
  </runtime>
</configuration>
'@
[IO.File]::WriteAllText(
    $dotNetConfigPath,
    $dotNetConfig + [Environment]::NewLine,
    (New-Object Text.UTF8Encoding($false))
)

# Employee packages carry only the public Hub location beside the EXE. The
# account password is entered in the normal login screen and the device token
# is issued later by Hub and protected with Windows DPAPI inside StoryForgeData.
if (-not $HubEndpoint) {
    $HubEndpoint = [string]$env:STORYFORGE_HUB_ENDPOINT
}
if ($Standalone -and $HubEndpoint) {
    throw 'Use either -Standalone or -HubEndpoint, not both.'
}
if (-not $Standalone -and -not $HubEndpoint) {
    throw 'Employee-ready builds require -HubEndpoint. Use -Standalone only for an intentionally independent package.'
}
if ($HubEndpoint) {
    $connectionProfilePath = Join-Path $bundleRoot 'storyforge-connection.json'
    $connectionProfileScript = @'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from storyforge.connection_profile import write_connection_profile
write_connection_profile(Path(sys.argv[2]), sys.argv[3], site_name=sys.argv[4])
'@
    Invoke-Checked -Command $venvPython -CommandArguments @(
        '-c', $connectionProfileScript, $projectRoot, $connectionProfilePath,
        $HubEndpoint, $HubSiteName
    )
    Write-Host "Bundled automatic Hub connection: $HubEndpoint"
}
else {
    Write-Host 'Building an explicitly standalone package without a Hub connection profile.'
}

if ($WithLocalAI) {
    # The Python runtime is frozen into the executable, while Kokoro's model and
    # voice tensors intentionally remain beside it. Keeping these large assets
    # external makes future model/voice updates possible without rebuilding the
    # executable and guarantees that a copied release works fully offline.
    $kokoroSource = Join-Path $projectRoot 'local-ai\kokoro'
    $kokoroModel = Join-Path $kokoroSource 'kokoro-v1_0.pth'
    $kokoroConfig = Join-Path $kokoroSource 'config.json'
    $kokoroVoices = Join-Path $kokoroSource 'voices'
    if (
        -not (Test-Path -LiteralPath $kokoroModel -PathType Leaf) -or
        -not (Test-Path -LiteralPath $kokoroConfig -PathType Leaf) -or
        -not (Test-Path -LiteralPath $kokoroVoices -PathType Container)
    ) {
        throw "Local Kokoro assets are incomplete. Run scripts\setup_local_ai.ps1 before packaging."
    }

    $localAiTarget = Join-Path $bundleRoot 'local-ai'
    [System.IO.Directory]::CreateDirectory($localAiTarget) | Out-Null
    $kokoroTarget = Join-Path $localAiTarget 'kokoro'
    # Merge with an existing target instead of deleting it. This keeps the
    # packaging command recoverable if a prior build was interrupted.
    Copy-Item -LiteralPath $kokoroSource -Destination $localAiTarget -Recurse -Force

    $copiedModel = Join-Path $kokoroTarget 'kokoro-v1_0.pth'
    if ((Get-Item -LiteralPath $copiedModel).Length -ne (Get-Item -LiteralPath $kokoroModel).Length) {
        throw 'The copied Kokoro model failed the size verification.'
    }
    Write-Host "Bundled offline Kokoro assets: $kokoroTarget"
}

$adminToolsTarget = Join-Path $bundleRoot 'admin-tools'
[System.IO.Directory]::CreateDirectory($adminToolsTarget) | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\diagnose_storyforge.ps1') `
    -Destination (Join-Path $adminToolsTarget 'diagnose_storyforge.ps1') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\diagnose_storyforge.cmd') `
    -Destination (Join-Path $adminToolsTarget 'diagnose_storyforge.cmd') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\enable_storyforge_worker.ps1') `
    -Destination (Join-Path $adminToolsTarget 'enable_storyforge_worker.ps1') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\disable_storyforge_worker.ps1') `
    -Destination (Join-Path $adminToolsTarget 'disable_storyforge_worker.ps1') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\restore_hub_backup.ps1') `
    -Destination (Join-Path $adminToolsTarget 'restore_hub_backup.ps1') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\restore_hub_backup.cmd') `
    -Destination (Join-Path $adminToolsTarget 'restore_hub_backup.cmd') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\enable_storyforge_worker.cmd') `
    -Destination (Join-Path $adminToolsTarget 'enable_storyforge_worker.cmd') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\disable_storyforge_worker.cmd') `
    -Destination (Join-Path $adminToolsTarget 'disable_storyforge_worker.cmd') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\publish_hub_snapshot.ps1') `
    -Destination (Join-Path $adminToolsTarget 'publish_hub_snapshot.ps1') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\verify_storyforge_deployment.ps1') `
    -Destination (Join-Path $adminToolsTarget 'verify_storyforge_deployment.ps1') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\repair_storyforge_hub_launcher.ps1') `
    -Destination (Join-Path $adminToolsTarget 'repair_storyforge_hub_launcher.ps1') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'docs\EMPLOYEE_QUICK_START.md') `
    -Destination (Join-Path $bundleRoot 'QUICK_START.md') -Force

# Build success is not proof that a windowed executable can start. Create a
# catalog with the current source schema, then make the frozen executable load
# it, import Python.NET/WinForms/WebView2, locate FFmpeg and start its localhost
# production worker.  This would have rejected the old schema-10 executable
# before it was delivered against a schema-11 database.
$smokeRoot = Join-Path $buildRoot ("startup-smoke\" + [guid]::NewGuid().ToString('N'))
$smokeData = Join-Path $smokeRoot 'data'
$smokeOutput = Join-Path $smokeRoot 'result'
[System.IO.Directory]::CreateDirectory($smokeData) | Out-Null
[System.IO.Directory]::CreateDirectory($smokeOutput) | Out-Null
$previousDataDir = [Environment]::GetEnvironmentVariable('STORYFORGE_DATA_DIR', 'Process')
$previousDeploymentRole = [Environment]::GetEnvironmentVariable('STORYFORGE_DEPLOYMENT_ROLE', 'Process')
$previousFrozenHubDataRoot = [Environment]::GetEnvironmentVariable('STORYFORGE_FROZEN_HUB_DATA_ROOT', 'Process')
$previousPortableMode = [Environment]::GetEnvironmentVariable('STORYFORGE_PORTABLE_MODE', 'Process')
try {
    $env:STORYFORGE_DATA_DIR = $smokeData
    # Build gates validate an employee/standalone frozen package, not the
    # identity of the shell that invoked the build. Never let a Hub launcher
    # environment authorize these child processes or suppress portable setup.
    Remove-Item Env:STORYFORGE_DEPLOYMENT_ROLE -ErrorAction SilentlyContinue
    Remove-Item Env:STORYFORGE_FROZEN_HUB_DATA_ROOT -ErrorAction SilentlyContinue
    Remove-Item Env:STORYFORGE_PORTABLE_MODE -ErrorAction SilentlyContinue
    $fixtureScript = @'
import os
from pathlib import Path
from storyforge.catalog import CatalogRepository, SCHEMA_VERSION

# PowerShell forwards this here-string to ``python -c`` through the Windows
# command-line parser.  Double quotes inside the argument can be consumed by
# that parser, so keep Python string literals single-quoted.
root = Path(os.environ['STORYFORGE_DATA_DIR'])
summary = CatalogRepository(root / 'storyforge-catalog.sqlite3').bootstrap_summary()
assert summary['schema_version'] == SCHEMA_VERSION, summary
'@
    Invoke-Checked -Command $venvPython -CommandArguments @('-c', $fixtureScript)

    Write-Host 'Running frozen startup/database/UI/worker smoke test...'
    $quotedSmokeOutput = '"' + $smokeOutput + '"'
    $smokeProcess = Start-Process -FilePath $exePath `
        -ArgumentList @('--startup-self-test', $quotedSmokeOutput) `
        -WorkingDirectory $bundleRoot -PassThru
    if (-not $smokeProcess.WaitForExit(180000)) {
        Stop-Process -Id $smokeProcess.Id -Force -ErrorAction SilentlyContinue
        throw 'The frozen startup smoke test timed out after 180 seconds.'
    }
    $smokeProcess.Refresh()
    $smokeResultPath = Join-Path $smokeOutput 'startup-self-test.json'
    if ($smokeProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $smokeResultPath)) {
        throw "The frozen startup smoke test failed (exit code $($smokeProcess.ExitCode)). Result: $smokeResultPath"
    }
    $smokeResult = Get-Content -LiteralPath $smokeResultPath -Raw | ConvertFrom-Json
    if (-not $smokeResult.ok) {
        throw "The frozen startup smoke test reported failure: $($smokeResult.error)"
    }
    if ([string]$smokeResult.app_version -ne $expectedAppVersion) {
        throw "The frozen application version is $($smokeResult.app_version), expected $expectedAppVersion."
    }
    Copy-Item -LiteralPath $smokeResultPath `
        -Destination (Join-Path $bundleRoot 'BUILD_STARTUP_VALIDATION.json') -Force

    if ($WithLocalAI) {
        # A successful PyInstaller build can still contain an incomplete
        # Kokoro runtime or a model/voice asset that cannot be loaded on a
        # clean workstation.  Exercise the frozen executable itself so a
        # broken "full" build never reaches an employee computer.
        $kokoroOutput = Join-Path $smokeRoot 'kokoro-result'
        [System.IO.Directory]::CreateDirectory($kokoroOutput) | Out-Null
        Write-Host 'Running frozen embedded-Kokoro synthesis smoke test...'
        $quotedKokoroOutput = '"' + $kokoroOutput + '"'
        $kokoroProcess = Start-Process -FilePath $exePath `
            -ArgumentList @('--kokoro-self-test', $quotedKokoroOutput) `
            -WorkingDirectory $bundleRoot -PassThru
        if (-not $kokoroProcess.WaitForExit(240000)) {
            Stop-Process -Id $kokoroProcess.Id -Force -ErrorAction SilentlyContinue
            throw 'The frozen Kokoro smoke test timed out after 240 seconds.'
        }
        $kokoroProcess.Refresh()
        $kokoroResultPath = Join-Path $kokoroOutput 'kokoro-self-test.json'
        if ($kokoroProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $kokoroResultPath)) {
            throw "The frozen Kokoro smoke test failed (exit code $($kokoroProcess.ExitCode)). Result: $kokoroResultPath"
        }
        $kokoroResult = Get-Content -LiteralPath $kokoroResultPath -Raw | ConvertFrom-Json
        if (-not $kokoroResult.ok) {
            throw "The frozen Kokoro smoke test reported failure: $($kokoroResult.error)"
        }
        if ([string]$kokoroResult.app_version -ne $expectedAppVersion) {
            throw "The frozen Kokoro application version is $($kokoroResult.app_version), expected $expectedAppVersion."
        }
        Copy-Item -LiteralPath $kokoroResultPath `
            -Destination (Join-Path $bundleRoot 'BUILD_KOKORO_VALIDATION.json') -Force
    }

    if ($RequireStableAcceptance) {
        if ($StableStressSeconds -lt 600) {
            throw '-RequireStableAcceptance requires -StableStressSeconds of at least 600.'
        }
        $acceptanceReportPath = if ($StableAcceptanceReport) {
            if ([System.IO.Path]::IsPathRooted($StableAcceptanceReport)) {
                [System.IO.Path]::GetFullPath($StableAcceptanceReport)
            }
            else {
                [System.IO.Path]::GetFullPath((Join-Path $projectRoot $StableAcceptanceReport))
            }
        }
        else {
            Join-Path $bundleRoot 'BUILD_STABILITY_ACCEPTANCE.json'
        }
        [System.IO.Directory]::CreateDirectory(
            [System.IO.Path]::GetDirectoryName($acceptanceReportPath)
        ) | Out-Null
        $acceptanceRoot = Join-Path $buildRoot 'stability-acceptance'
        $stressSecondsText = [string]::Format(
            [Globalization.CultureInfo]::InvariantCulture,
            '{0}',
            $StableStressSeconds
        )
        $acceptanceArguments = @(
            '--storyforge-stability-acceptance',
            '--stress',
            '--stress-seconds', $stressSecondsText,
            '--app-root', $bundleRoot,
            '--package-artifact', $exePath,
            '--encoder', $StableAcceptanceEncoder,
            '--root', $acceptanceRoot,
            '--json-report', $acceptanceReportPath
        )
        if ($StableFfprobe) {
            $acceptanceArguments += @('--ffprobe', $StableFfprobe)
        }
        Write-Host 'Running the frozen package-bound 10-minute stable release gate...'
        # ``StoryForge Studio.exe`` is a windowed executable. Windows
        # PowerShell does not reliably wait when it is invoked with ``&`` and
        # can therefore evaluate a stale LASTEXITCODE while the real
        # acceptance render is still running in the background. Start the
        # process explicitly, keep it hidden, and wait for its authoritative
        # exit code and JSON report.
        $quotedAcceptanceArguments = @(
            $acceptanceArguments | ForEach-Object {
                $argument = [string]$_
                if ($argument -match '[\s"]') {
                    '"' + $argument.Replace('"', '\"') + '"'
                }
                else {
                    $argument
                }
            }
        )
        $acceptanceProcess = Start-Process -FilePath $exePath `
            -ArgumentList $quotedAcceptanceArguments `
            -WorkingDirectory $bundleRoot -WindowStyle Hidden -PassThru
        $acceptanceTimeoutSeconds = [Math]::Max(
            900,
            [Math]::Ceiling($StableStressSeconds * 4)
        )
        $acceptanceTimeoutMilliseconds = [int]($acceptanceTimeoutSeconds * 1000)
        if (-not $acceptanceProcess.WaitForExit($acceptanceTimeoutMilliseconds)) {
            Stop-Process -Id $acceptanceProcess.Id -Force -ErrorAction SilentlyContinue
            throw "The frozen stable acceptance timed out after $acceptanceTimeoutSeconds seconds."
        }
        $acceptanceProcess.Refresh()
        if ($acceptanceProcess.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $acceptanceReportPath)) {
            throw "The frozen stable acceptance failed (exit code $($acceptanceProcess.ExitCode)). Report: $acceptanceReportPath"
        }

        # The stable report intentionally contains the full progress timeline and
        # can exceed what Windows PowerShell 5's ConvertFrom-Json handles
        # reliably. Validate the authoritative report with the build Python
        # runtime instead of loading the whole document into PowerShell.
        $stableReportValidationScript = @'
import hashlib
import json
import os
import sys
from pathlib import Path

report_path = Path(sys.argv[1]).resolve(strict=True)
expected_version = sys.argv[2]
expected_executable = Path(sys.argv[3]).resolve(strict=True)
expected_encoder = sys.argv[4]
report = json.loads(report_path.read_text(encoding='utf-8'))

def require(condition, message):
    if not condition:
        raise SystemExit(message)

require(report.get('ok') is True and report.get('stable_release_eligible') is True,
        f'Stable acceptance did not approve this package: {report_path}')
actual_version = report.get('storyforge_version')
require(str(actual_version) == expected_version,
        f'Stable acceptance version is {actual_version}, expected {expected_version}.')
package = report.get('package') or {}
release_gate = report.get('release_gate') or {}
require(report.get('package_artifact_bound') is True and package.get('kind') == 'explicit_artifact',
        'Stable acceptance is not bound to an explicit package artifact.')
require(report.get('code_under_test') == 'frozen_executable_pipeline_runner'
        and release_gate.get('frozen_executable_pipeline_executed') is True,
        'Stable acceptance did not execute the frozen StoryForge pipeline.')
require(package.get('runtime_entrypoint_matches') is True,
        'Stable acceptance executable does not match the running frozen entrypoint.')
reported_path = Path(str(package.get('path') or '')).resolve(strict=True)
require(os.path.normcase(str(reported_path)) == os.path.normcase(str(expected_executable)),
        f'Stable acceptance belongs to a different package: {reported_path}')
digest = hashlib.sha256(expected_executable.read_bytes()).hexdigest()
require(package.get('sha256') == digest,
        'Stable acceptance package SHA-256 does not match the executable being released.')
require(int(package.get('bytes') or -1) == expected_executable.stat().st_size,
        'Stable acceptance package size does not match the executable being released.')
scenarios = report.get('scenarios') or []
require(bool(scenarios), 'Stable acceptance report contains no scenarios.')
for scenario in scenarios:
    scenario_name = scenario.get('name')
    require(scenario.get('ok') is True and scenario.get('actual_command_encoder') == expected_encoder,
            f'Stable scenario did not prove encoder {expected_encoder} from its real FFmpeg command: {scenario_name}')
'@
        Invoke-Checked -Command $venvPython -CommandArguments @(
            '-c', $stableReportValidationScript, $acceptanceReportPath,
            $expectedAppVersion, $exePath, $StableAcceptanceEncoder
        )
        $bundledAcceptanceReport = Join-Path $bundleRoot 'BUILD_STABILITY_ACCEPTANCE.json'
        if (-not [string]::Equals($acceptanceReportPath, $bundledAcceptanceReport, [StringComparison]::OrdinalIgnoreCase)) {
            Copy-Item -LiteralPath $acceptanceReportPath -Destination $bundledAcceptanceReport -Force
        }
    }

    # Bind the exact frozen directory to the self-tests above. The release
    # attestation hashes the entrypoint and every bundle file except itself, so
    # a stale validation JSON cannot be copied beside a different or partially
    # modified executable and then published as a trusted employee update.
    $releaseValidationScript = @'
import sys
sys.path.insert(0, sys.argv[1])
from scripts.build_update_package import write_release_validation
write_release_validation(
    sys.argv[2],
    entrypoint=sys.argv[3],
    requested_version=sys.argv[4],
    with_local_ai=sys.argv[5] == '1',
)
'@
    Write-Host 'Hashing and attesting the verified frozen release directory...'
    Invoke-Checked -Command $venvPython -CommandArguments @(
        '-c', $releaseValidationScript, $projectRoot, $bundleRoot,
        'StoryForge Studio.exe', $expectedAppVersion,
        $(if ($WithLocalAI) { '1' } else { '0' })
    )

    if ($ReleaseBuild) {
        $releaseGateRoot = Join-Path $buildRoot 'release-gate'
        [System.IO.Directory]::CreateDirectory($releaseGateRoot) | Out-Null
        $releasePackageSmokeReport = Join-Path $releaseGateRoot 'package-smoke.json'
        Write-Host 'Running the independent frozen package smoke gate...'
        Invoke-Checked -Command $venvPython -CommandArguments @(
            (Join-Path $projectRoot 'scripts\package_smoke.py'),
            '--package-root', $bundleRoot,
            '--expected-version', $expectedAppVersion,
            '--report', $releasePackageSmokeReport,
            '--require-stable-acceptance'
        )
    }
}
finally {
    if ($null -eq $previousDataDir) {
        Remove-Item Env:STORYFORGE_DATA_DIR -ErrorAction SilentlyContinue
    }
    else {
        $env:STORYFORGE_DATA_DIR = $previousDataDir
    }
    if ($null -eq $previousDeploymentRole) {
        Remove-Item Env:STORYFORGE_DEPLOYMENT_ROLE -ErrorAction SilentlyContinue
    }
    else {
        $env:STORYFORGE_DEPLOYMENT_ROLE = $previousDeploymentRole
    }
    if ($null -eq $previousFrozenHubDataRoot) {
        Remove-Item Env:STORYFORGE_FROZEN_HUB_DATA_ROOT -ErrorAction SilentlyContinue
    }
    else {
        $env:STORYFORGE_FROZEN_HUB_DATA_ROOT = $previousFrozenHubDataRoot
    }
    if ($null -eq $previousPortableMode) {
        Remove-Item Env:STORYFORGE_PORTABLE_MODE -ErrorAction SilentlyContinue
    }
    else {
        $env:STORYFORGE_PORTABLE_MODE = $previousPortableMode
    }
}

$sizeMb = [Math]::Round((Get-Item -LiteralPath $exePath).Length / 1MB, 1)
Write-Host ''
Write-Host "Build complete: StoryForge v$expectedAppVersion - $exePath ($sizeMb MB)"
Write-Host "Startup validation: $(Join-Path $bundleRoot 'BUILD_STARTUP_VALIDATION.json')"
Write-Host "Release attestation: $(Join-Path $bundleRoot 'BUILD_RELEASE_VALIDATION.json')"
if ($releasePackageSmokeReport) {
    Write-Host "Release package smoke: $releasePackageSmokeReport"
}
if (-not $WithLocalAI) {
    Write-Host 'This is the lightweight build. Use a Kokoro HTTP service or rebuild with -WithLocalAI.'
}
