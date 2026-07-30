param(
    [switch]$Debug,
    [switch]$Web,
    [string]$WebHost = '0.0.0.0',
    [ValidateRange(1, 65535)]
    [int]$WebPort = 8765,
    [switch]$SkipInstall,
    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$requirementsPath = Join-Path $projectRoot 'requirements.txt'
$entryPoint = Join-Path $projectRoot 'run.py'

function Resolve-PythonCommand {
    param([string]$Requested)

    if ($Requested) {
        $requestedCommand = Get-Command -Name $Requested -ErrorAction Stop
        return [pscustomobject]@{
            Path = $requestedCommand.Source
            Prefix = @()
        }
    }

    # On the development/Hub computer the verified build environment also
    # contains the optional Kokoro and multilingual TTS runtimes. Prefer it
    # over both a normal .venv and bare system Python so adding a lightweight
    # development environment cannot silently disable the local voices.
    $buildPython = Join-Path $projectRoot '.build-venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $buildPython -PathType Leaf) {
        return [pscustomobject]@{
            Path = $buildPython
            Prefix = @()
        }
    }

    $projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
        return [pscustomobject]@{
            Path = $projectPython
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

    throw 'Python 3 was not found. Install Python 3.11+ and enable Add Python to PATH.'
}

function Invoke-Python {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Python,
        [Parameter(Mandatory = $true)]
        [string[]]$CommandArguments
    )

    & $Python.Path @($Python.Prefix) @CommandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE."
    }
}

$pythonCommand = Resolve-PythonCommand -Requested $PythonExe
Invoke-Python -Python $pythonCommand -CommandArguments @(
    '-c',
    "import sys; assert sys.version_info >= (3, 11), 'StoryForge requires Python 3.11+'; print('Python', sys.version.split()[0])"
)

if (-not $SkipInstall) {
    Write-Host 'Installing/checking StoryForge runtime dependencies...'
    Invoke-Python -Python $pythonCommand -CommandArguments @(
        '-m', 'pip', 'install', '--disable-pip-version-check', '-r', $requirementsPath
    )
}

$runArguments = @($entryPoint)
if ($Debug) {
    $runArguments += '--debug'
}
if ($Web) {
    $runArguments += @('--web', '--web-host', $WebHost, '--web-port', [string]$WebPort)
}

if ($Web) {
    Write-Host "Starting StoryForge browser service from: $projectRoot"
    Write-Host "Python runtime: $($pythonCommand.Path)"
    Write-Host "Browser address: http://127.0.0.1:$WebPort/"
}
else {
    Write-Host "Starting StoryForge Studio from: $projectRoot"
}
Push-Location -LiteralPath $projectRoot
try {
    & $pythonCommand.Path @($pythonCommand.Prefix) @runArguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

if ($exitCode -ne 0) {
    throw "StoryForge Studio exited with code $exitCode."
}
