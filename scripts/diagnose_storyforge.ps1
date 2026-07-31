[CmdletBinding()]
param(
    [string]$ExePath = '',
    [int]$WaitSeconds = 90
)

if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $exeCandidates = @(
        (Join-Path $PSScriptRoot 'StoryForge Studio.exe'),
        (Join-Path (Split-Path -Parent $PSScriptRoot) 'StoryForge Studio.exe')
    )
    $ExePath = $exeCandidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
}

$ErrorActionPreference = 'Continue'
$resolvedExe = [System.IO.Path]::GetFullPath($ExePath)
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop) {
    $desktop = if ($ExePath -and (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
        Split-Path -Parent (Resolve-Path -LiteralPath $ExePath).Path
    } else {
        Split-Path -Parent $PSScriptRoot
    }
}
$reportPath = Join-Path $desktop "StoryForge-Diagnostics-$stamp.txt"
$lines = [System.Collections.Generic.List[string]]::new()

function Add-ReportLine {
    param([string]$Value = '')
    $lines.Add($Value)
}

Add-ReportLine 'StoryForge Studio startup diagnostics'
Add-ReportLine ("Generated: {0:o}" -f (Get-Date))
Add-ReportLine ("Computer: {0}" -f $env:COMPUTERNAME)
Add-ReportLine ("Windows: {0}" -f [Environment]::OSVersion.VersionString)
Add-ReportLine ("Architecture: {0}" -f $env:PROCESSOR_ARCHITECTURE)
Add-ReportLine ("EXE: {0}" -f $resolvedExe)

if (-not (Test-Path -LiteralPath $resolvedExe -PathType Leaf)) {
    Add-ReportLine 'RESULT: StoryForge Studio.exe was not found. Keep the entire release folder together.'
    $lines | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Start-Process notepad.exe -ArgumentList ('"' + $reportPath + '"') | Out-Null
    exit 2
}

$exe = Get-Item -LiteralPath $resolvedExe
$signature = Get-AuthenticodeSignature -LiteralPath $resolvedExe
$hash = Get-FileHash -LiteralPath $resolvedExe -Algorithm SHA256
Add-ReportLine ("EXE size: {0:N0} bytes" -f $exe.Length)
Add-ReportLine ("EXE modified: {0:o}" -f $exe.LastWriteTime)
Add-ReportLine ("EXE SHA256: {0}" -f $hash.Hash)
Add-ReportLine ("Digital signature: {0}" -f $signature.Status)

$webViewVersion = ''
foreach ($key in @(
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F1E7E2D4-BDB4-4A2E-B6A0-9F3E9172A5F7}',
    'HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F1E7E2D4-BDB4-4A2E-B6A0-9F3E9172A5F7}',
    'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F1E7E2D4-BDB4-4A2E-B6A0-9F3E9172A5F7}'
)) {
    if (Test-Path -LiteralPath $key) {
        $webViewVersion = [string](Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue).pv
        if ($webViewVersion) { break }
    }
}
Add-ReportLine ("WebView2 Runtime: {0}" -f $(if ($webViewVersion) { $webViewVersion } else { 'not detected in registry' }))

$startedAt = Get-Date
$process = Start-Process -FilePath $resolvedExe -WorkingDirectory $exe.DirectoryName -PassThru
Add-ReportLine ("Started process PID: {0}" -f $process.Id)
$deadline = (Get-Date).AddSeconds([Math]::Max(10, $WaitSeconds))
$windowVisible = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    $process.Refresh()
    if ($process.HasExited) { break }
    if ($process.MainWindowHandle -ne 0) {
        $windowVisible = $true
        break
    }
}
$process.Refresh()
if ($windowVisible) {
    Add-ReportLine 'RESULT: StoryForge displayed its desktop window successfully.'
}
elseif ($process.HasExited) {
    Add-ReportLine ("RESULT: StoryForge exited before displaying a window. Exit code: {0}." -f $process.ExitCode)
}
else {
    Add-ReportLine ("RESULT: No window was detected after {0} seconds, but the process is still running. Security software or WebView2 may be delaying startup." -f $WaitSeconds)
}

Add-ReportLine ''
Add-ReportLine '--- 本机制作服务 / Local worker ---'
$workerFound = $false
foreach ($port in 18765..18770) {
    try {
        $worker = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$port/worker/api/health" `
            -TimeoutSec 2
        if ($worker.ok -and $worker.data.service -eq 'storyforge-local-worker') {
            $workerFound = $true
            $versionProperty = $worker.data.PSObject.Properties['version']
            $protocolProperty = $worker.data.PSObject.Properties['protocol_version']
            $roleProperty = $worker.data.PSObject.Properties['worker_role']
            $readyProperty = $worker.data.PSObject.Properties['ready']
            Add-ReportLine (
                "127.0.0.1:{0} | version={1} | protocol={2} | role={3} | ready={4}" -f `
                    $port, `
                    $(if ($versionProperty) { $versionProperty.Value } else { 'unknown' }), `
                    $(if ($protocolProperty) { $protocolProperty.Value } else { 'legacy/missing' }), `
                    $(if ($roleProperty) { $roleProperty.Value } else { 'legacy/missing' }), `
                    $(if ($readyProperty) { $readyProperty.Value } else { 'unknown' })
            )
        }
    }
    catch {
        # Most discovery ports are expected to be unused.
    }
}
if (-not $workerFound) {
    Add-ReportLine '未发现本机制作服务。员工电脑请先登录并绑定主电脑，再运行 enable_storyforge_worker.cmd。'
}

$portableDataRoot = Join-Path $exe.DirectoryName 'StoryForgeData'
$portableLogRoot = Join-Path $portableDataRoot 'logs'
$legacyLogRoot = Join-Path $env:LOCALAPPDATA 'StoryForgeStudio\logs'
$logRoot = $(if (Test-Path -LiteralPath $portableDataRoot -PathType Container) {
    $portableLogRoot
} else {
    $legacyLogRoot
})
$latestLog = Join-Path $logRoot 'startup-error-latest.log'
Add-ReportLine ''
Add-ReportLine ("StoryForge data directory: {0}" -f $(if (Test-Path -LiteralPath $portableDataRoot -PathType Container) { $portableDataRoot } else { 'legacy AppData layout' }))
Add-ReportLine ("Startup log directory: {0}" -f $logRoot)
if (Test-Path -LiteralPath $latestLog -PathType Leaf) {
    Add-ReportLine '--- startup-error-latest.log ---'
    Get-Content -LiteralPath $latestLog -ErrorAction SilentlyContinue | Select-Object -Last 200 | ForEach-Object {
        Add-ReportLine ([string]$_)
    }
}
else {
    Add-ReportLine 'No StoryForge startup error log was found.'
}

Add-ReportLine ''
Add-ReportLine '--- Windows application events after this launch ---'
$events = Get-WinEvent -FilterHashtable @{
    LogName = 'Application'
    StartTime = $startedAt.AddSeconds(-5)
} -ErrorAction SilentlyContinue | Where-Object {
    $_.Level -le 3 -or $_.ProviderName -match 'Application Error|Windows Error Reporting|SideBySide'
} | Select-Object -First 20
if ($events) {
    foreach ($event in $events) {
        Add-ReportLine ("[{0:o}] {1} / {2} / {3}" -f $event.TimeCreated, $event.ProviderName, $event.Id, $event.LevelDisplayName)
        Add-ReportLine (([string]$event.Message).Trim())
    }
}
else {
    Add-ReportLine 'No relevant Windows application errors were found.'
}

$lines | Set-Content -LiteralPath $reportPath -Encoding UTF8
Write-Host "Diagnostics report created: $reportPath"
if (-not $windowVisible) {
    Start-Process notepad.exe -ArgumentList ('"' + $reportPath + '"') | Out-Null
}
exit $(if ($windowVisible) { 0 } else { 1 })
