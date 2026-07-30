param(
    [string]$ExecutablePath = (Join-Path $PSScriptRoot "StoryForge Studio.exe"),
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [string]$TaskName = "StoryForge Hub"
)

$ErrorActionPreference = "Stop"

$resolvedExecutable = (Resolve-Path -LiteralPath $ExecutablePath).Path
if ([IO.Path]::GetFileName($resolvedExecutable) -ne "StoryForge Studio.exe") {
    throw "ExecutablePath must point to StoryForge Studio.exe."
}

$workingDirectory = Split-Path -Parent $resolvedExecutable
$arguments = "--web --web-host 0.0.0.0 --web-port $Port"
$action = New-ScheduledTaskAction `
    -Execute $resolvedExecutable `
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

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "StoryForge browser Hub on TCP $Port. Starts at Windows logon and restarts after failures." `
    -Force | Out-Null

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
    throw "The scheduled task was installed, but StoryForge Hub did not become ready on port $Port."
}

Write-Host "StoryForge Hub is running: http://127.0.0.1:$Port/"
Write-Host "Scheduled task: $TaskName"
