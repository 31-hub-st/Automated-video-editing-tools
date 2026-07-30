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
    [string]$HubSiteName = 'StoryForge Hub'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$specPath = Join-Path $projectRoot 'StoryForge.spec'
$buildVenv = Join-Path $projectRoot '.build-venv'
$venvPython = Join-Path $buildVenv 'Scripts\python.exe'
if ($WorkDirectory) {
    if ([System.IO.Path]::IsPathRooted($WorkDirectory)) {
        $buildRoot = [System.IO.Path]::GetFullPath($WorkDirectory)
    }
    else {
        $buildRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $WorkDirectory))
    }
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
    $coreModules = 'imageio_ffmpeg,webview,PyInstaller,edge_tts'
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
else {
    $distPath = Join-Path $projectRoot 'dist'
}
[System.IO.Directory]::CreateDirectory($distPath) | Out-Null

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

# Employee packages carry only the public Hub location beside the EXE. The
# account password is entered in the normal login screen and the device token
# is issued later by Hub and protected in AppData with Windows DPAPI.
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
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\enable_storyforge_worker.cmd') `
    -Destination (Join-Path $adminToolsTarget 'enable_storyforge_worker.cmd') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\disable_storyforge_worker.cmd') `
    -Destination (Join-Path $adminToolsTarget 'disable_storyforge_worker.cmd') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'docs\EMPLOYEE_QUICK_START.md') `
    -Destination (Join-Path $bundleRoot 'QUICK_START.md') -Force

# Build success is not proof that a windowed executable can start.  Create a
# catalog with the current source schema, then make the frozen executable load
# it, import the bundled WebView2 backend, locate FFmpeg and start its localhost
# production worker.  This would have rejected the old schema-10 executable
# before it was delivered against a schema-11 database.
$smokeRoot = Join-Path $buildRoot ("startup-smoke\" + [guid]::NewGuid().ToString('N'))
$smokeData = Join-Path $smokeRoot 'data'
$smokeOutput = Join-Path $smokeRoot 'result'
[System.IO.Directory]::CreateDirectory($smokeData) | Out-Null
[System.IO.Directory]::CreateDirectory($smokeOutput) | Out-Null
$previousDataDir = [Environment]::GetEnvironmentVariable('STORYFORGE_DATA_DIR', 'Process')
try {
    $env:STORYFORGE_DATA_DIR = $smokeData
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
}
finally {
    if ($null -eq $previousDataDir) {
        Remove-Item Env:STORYFORGE_DATA_DIR -ErrorAction SilentlyContinue
    }
    else {
        $env:STORYFORGE_DATA_DIR = $previousDataDir
    }
}

$sizeMb = [Math]::Round((Get-Item -LiteralPath $exePath).Length / 1MB, 1)
Write-Host ''
Write-Host "Build complete: StoryForge v$expectedAppVersion - $exePath ($sizeMb MB)"
Write-Host "Startup validation: $(Join-Path $bundleRoot 'BUILD_STARTUP_VALIDATION.json')"
if (-not $WithLocalAI) {
    Write-Host 'This is the lightweight build. Use a Kokoro HTTP service or rebuild with -WithLocalAI.'
}
