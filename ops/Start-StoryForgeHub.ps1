param(
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
