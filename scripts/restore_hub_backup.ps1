[CmdletBinding()]
param(
    [string]$SnapshotPath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$toolDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$bundleDirectory = Split-Path -Parent $toolDirectory
$storyForgeExe = Join-Path $bundleDirectory 'StoryForge Studio.exe'

if (-not (Test-Path -LiteralPath $storyForgeExe -PathType Leaf)) {
    throw "StoryForge executable was not found: $storyForgeExe"
}

if (-not $SnapshotPath) {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = 'Select a StoryForge Hub backup to restore'
    $dialog.Filter = 'StoryForge Hub backup (*.sfbak)|*.sfbak|Legacy StoryForge backup (*.zip)|*.zip|All supported backups (*.sfbak;*.zip)|*.sfbak;*.zip'
    $managedBackupDirectory = Join-Path $bundleDirectory 'StoryForgeData\hub-backups'
    if (Test-Path -LiteralPath $managedBackupDirectory -PathType Container) {
        $dialog.InitialDirectory = $managedBackupDirectory
    }
    $dialog.Multiselect = $false
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        Write-Host 'Restore cancelled.'
        exit 0
    }
    $SnapshotPath = $dialog.FileName
}

$resolvedSnapshot = [System.IO.Path]::GetFullPath($SnapshotPath)
if (-not (Test-Path -LiteralPath $resolvedSnapshot -PathType Leaf)) {
    throw "Backup file was not found: $resolvedSnapshot"
}

$confirmation = Read-Host 'Close every StoryForge window and background worker. Type RESTORE to replace the Hub library with this backup'
if ($confirmation -cne 'RESTORE') {
    Write-Host 'Confirmation did not match. No data was changed.'
    exit 0
}

& $storyForgeExe --restore-hub-backup $resolvedSnapshot
if ($LASTEXITCODE -ne 0) {
    throw "Restore failed (exit code $LASTEXITCODE). Existing data was not replaced. Check StoryForgeData\logs."
}

Write-Host 'Restore completed. A pre-restore safety backup was created automatically.'
Read-Host 'Press Enter to close'
