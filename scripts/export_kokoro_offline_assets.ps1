param(
    [string]$Destination = "",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Destination) {
    $Destination = Join-Path $projectRoot "local-ai\kokoro"
}
if (-not $PythonExe) {
    $PythonExe = Join-Path $projectRoot ".build-venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python runtime not found: $PythonExe"
}

$destinationRoot = [System.IO.Path]::GetFullPath($Destination)
$voiceRoot = Join-Path $destinationRoot "voices"
New-Item -ItemType Directory -Path $voiceRoot -Force | Out-Null

$pythonCode = @'
import json
import time
from huggingface_hub import hf_hub_download

repo = "hexgrad/Kokoro-82M"
files = [
    "config.json",
    "kokoro-v1_0.pth",
    "voices/af_heart.pt",
    "voices/af_bella.pt",
    "voices/af_nicole.pt",
    "voices/af_sarah.pt",
    "voices/bf_alice.pt",
    "voices/bf_emma.pt",
    "voices/bf_isabella.pt",
    "voices/bf_lily.pt",
    "voices/ef_dora.pt",
    "voices/ff_siwis.pt",
    "voices/hf_alpha.pt",
    "voices/hf_beta.pt",
    "voices/if_sara.pt",
    "voices/jf_alpha.pt",
    "voices/jf_gongitsune.pt",
    "voices/jf_tebukuro.pt",
    "voices/jf_nezumi.pt",
    "voices/pf_dora.pt",
    "voices/zf_xiaobei.pt",
    "voices/zf_xiaoni.pt",
    "voices/zf_xiaoxiao.pt",
    "voices/zf_xiaoyi.pt",
]
downloaded = {}
for name in files:
    last_error = None
    for attempt in range(1, 6):
        try:
            downloaded[name] = hf_hub_download(repo_id=repo, filename=name)
            last_error = None
            break
        except Exception as error:
            last_error = error
            if attempt < 5:
                time.sleep(min(8, attempt * 2))
    if last_error is not None:
        raise RuntimeError(f"Could not download {name} after 5 attempts: {last_error}")
print(json.dumps(downloaded))
'@

$previousExportCode = [Environment]::GetEnvironmentVariable(
    "STORYFORGE_EXPORT_KOKORO_CODE",
    [EnvironmentVariableTarget]::Process
)
try {
    # Windows PowerShell can strip the quote characters from a multiline
    # argument passed directly to ``python -c``.  Put the script in the child
    # process environment and keep the command-line expression quote-safe.
    $env:STORYFORGE_EXPORT_KOKORO_CODE = $pythonCode
    $json = & $PythonExe -c "import os; exec(os.environ['STORYFORGE_EXPORT_KOKORO_CODE'])"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not prepare Kokoro model files. Check network access and retry."
    }
}
finally {
    if ($null -eq $previousExportCode) {
        Remove-Item Env:STORYFORGE_EXPORT_KOKORO_CODE -ErrorAction SilentlyContinue
    }
    else {
        $env:STORYFORGE_EXPORT_KOKORO_CODE = $previousExportCode
    }
}
$sources = $json | ConvertFrom-Json
foreach ($relative in @(
    "config.json",
    "kokoro-v1_0.pth",
    "voices/af_heart.pt",
    "voices/af_bella.pt",
    "voices/af_nicole.pt",
    "voices/af_sarah.pt",
    "voices/bf_alice.pt",
    "voices/bf_emma.pt",
    "voices/bf_isabella.pt",
    "voices/bf_lily.pt",
    "voices/ef_dora.pt",
    "voices/ff_siwis.pt",
    "voices/hf_alpha.pt",
    "voices/hf_beta.pt",
    "voices/if_sara.pt",
    "voices/jf_alpha.pt",
    "voices/jf_gongitsune.pt",
    "voices/jf_tebukuro.pt",
    "voices/jf_nezumi.pt",
    "voices/pf_dora.pt",
    "voices/zf_xiaobei.pt",
    "voices/zf_xiaoni.pt",
    "voices/zf_xiaoxiao.pt",
    "voices/zf_xiaoyi.pt"
)) {
    $source = [string]$sources.$relative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Downloaded Kokoro asset is missing: $relative"
    }
    $target = Join-Path $destinationRoot $relative
    $targetParent = Split-Path -Parent $target
    New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $target -Force
}

$required = Get-ChildItem -LiteralPath $destinationRoot -Recurse -File
$totalBytes = ($required | Measure-Object -Property Length -Sum).Sum
[pscustomobject]@{
    Destination = $destinationRoot
    Files = $required.Count
    Bytes = $totalBytes
}
