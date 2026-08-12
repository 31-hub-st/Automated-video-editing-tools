param(
    [string]$ExecutablePath = (Join-Path $PSScriptRoot "StoryForge Studio.exe"),
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [string]$TaskName = "StoryForge Hub"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Get-SafeFixedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not [System.IO.Path]::IsPathRooted($Value)) {
        throw "$Label must be an absolute local path."
    }
    $full = [System.IO.Path]::GetFullPath($Value)
    if ($full -match '[^\x00-\x7F]') {
        throw "$Label must be ASCII-only, for example D:\StoryForgeHub\Data."
    }
    if ($full -notmatch '^[A-Za-z]:\\(?:[A-Za-z0-9_. -]+\\)*[A-Za-z0-9_. -]+$') {
        throw "$Label contains unsupported characters: $full"
    }
    $volumeRoot = [System.IO.Path]::GetPathRoot($full)
    if ($full.TrimEnd('\') -eq $volumeRoot.TrimEnd('\')) {
        throw "$Label cannot be a drive root."
    }
    if (-not (Test-Path -LiteralPath $volumeRoot -PathType Container)) {
        throw "$Label drive is not available: $volumeRoot"
    }
    return $full.TrimEnd('\')
}

$resolvedExecutable = (Resolve-Path -LiteralPath $ExecutablePath).Path
if ([IO.Path]::GetFileName($resolvedExecutable) -ne "StoryForge Studio.exe") {
    throw "ExecutablePath must point to StoryForge Studio.exe."
}

$workingDirectory = Get-SafeFixedPath -Value (Split-Path -Parent $resolvedExecutable) -Label "Executable directory"
$resolvedDataRoot = Get-SafeFixedPath -Value $DataRoot -Label "DataRoot"
if (-not (Test-Path -LiteralPath $resolvedDataRoot -PathType Container)) {
    throw "DataRoot directory does not exist: $resolvedDataRoot"
}
if ($resolvedDataRoot -eq $workingDirectory) {
    throw "DataRoot must be a dedicated directory, not the executable directory."
}

$existingTask = @(
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
)
if ($existingTask.Count -gt 0) {
    throw "Scheduled task '$TaskName' already exists. This script is only for first-time Hub enablement."
}
$existingListeners = @(
    Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
)
if ($existingListeners.Count -gt 0) {
    throw "TCP port $Port already has a listener. Refusing first-time Hub enablement."
}

$launcherPath = Join-Path $PSScriptRoot "Start-StoryForge-Hub.ps1"
$launcherLines = @(
    '$ErrorActionPreference = ''Stop''',
    '$env:STORYFORGE_DEPLOYMENT_ROLE = ''Hub''',
    "`$env:STORYFORGE_DATA_DIR = '$resolvedDataRoot'",
    'Remove-Item Env:STORYFORGE_FROZEN_HUB_DATA_ROOT -ErrorAction SilentlyContinue',
    'Remove-Item Env:STORYFORGE_PORTABLE_MODE -ErrorAction SilentlyContinue',
    "& '$resolvedExecutable' --web --web-host 0.0.0.0 --web-port $Port",
    'exit $LASTEXITCODE'
)
[System.IO.File]::WriteAllLines($launcherPath, $launcherLines, [System.Text.Encoding]::ASCII)

$powerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcherPath`""
$action = New-ScheduledTaskAction `
    -Execute $powerShell `
    -Argument $arguments `
    -WorkingDirectory $workingDirectory
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal `
    -UserId ($env:USERDOMAIN + "\" + $env:USERNAME) `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$taskCreated = $false
try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "StoryForge browser Hub on TCP $Port. Starts at Windows logon and restarts after failures." |
        Out-Null
    $taskCreated = $true

    Start-ScheduledTask -TaskName $TaskName

    $healthUrl = "http://127.0.0.1:$Port/web/api/health"
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt += 1) {
        Start-Sleep -Seconds 1
        try {
            $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
            if ($response.ok -and $response.data.service -eq "storyforge-web") {
                $ready = $true
                break
            }
        }
        catch {
            # The frozen executable can need several seconds to unpack on first run.
        }
    }

    if (-not $ready) {
        throw "The newly registered task did not make StoryForge Hub ready on port $Port."
    }

    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop
    )
    if ($listeners.Count -ne 1) {
        throw "Expected exactly one StoryForge Hub listener on port $Port."
    }
    $listener = $listeners[0]
    $listenerProcess = Get-CimInstance `
        -ClassName Win32_Process `
        -Filter ("ProcessId = {0}" -f [uint32]$listener.OwningProcess) `
        -ErrorAction Stop
    if ($null -eq $listenerProcess) {
        throw "The StoryForge Hub listener process could not be identified."
    }
    $actualExecutable = [System.IO.Path]::GetFullPath(
        [string]$listenerProcess.ExecutablePath
    )
    $expectedCommandLine = "`"$resolvedExecutable`" --web --web-host 0.0.0.0 --web-port $Port"
    if (
        $actualExecutable -ne [System.IO.Path]::GetFullPath($resolvedExecutable) -or
        [string]$listenerProcess.CommandLine -ne $expectedCommandLine
    ) {
        throw "Port $Port is not owned by the exact newly enabled StoryForge Hub process."
    }
}
catch {
    $failure = $_
    if ($taskCreated) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    }
    throw $failure
}

Write-Host "StoryForge Hub is running: http://127.0.0.1:$Port/"
Write-Host "Scheduled task: $TaskName"
