[CmdletBinding()]
param(
    [string]$TaskName = 'StoryForge Local Worker'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host 'StoryForge local worker is not registered.'
    exit 0
}
if ($task.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host 'StoryForge local worker was stopped and removed from current-user logon.' -ForegroundColor Green
