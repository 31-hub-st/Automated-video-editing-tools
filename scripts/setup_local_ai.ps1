param(
    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$requirements = Join-Path $projectRoot 'requirements-ai.txt'

if (-not $PythonExe) {
    foreach ($candidate in @(
        (Join-Path $projectRoot '.build-venv\Scripts\python.exe'),
        (Join-Path $projectRoot '.venv\Scripts\python.exe')
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $PythonExe = $candidate
            break
        }
    }
}
if (-not $PythonExe) {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if (-not $command) {
        throw 'Python 3.11 or 3.12 was not found.'
    }
    $PythonExe = $command.Source
}

Write-Host 'Installing the optional local Kokoro voice engine...'
& $PythonExe -c "import sys; assert (3, 11) <= sys.version_info < (3, 13), 'Kokoro requires Python 3.11 or 3.12'"
if ($LASTEXITCODE -ne 0) {
    throw 'Local Kokoro requires Python 3.11 or 3.12.'
}
& $PythonExe -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) {
    throw "Installing local Kokoro failed with exit code $LASTEXITCODE."
}

Write-Host ''
Write-Host 'Local AI dependencies are installed.'
Write-Host "Python runtime: $PythonExe"
Write-Host 'The first narration may download Kokoro model files and will take longer.'
