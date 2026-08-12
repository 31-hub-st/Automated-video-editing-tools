[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$HubRoot,
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [string]$TargetAppDirectory = '',
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [string]$TaskName = 'StoryForge Hub'
)

$ErrorActionPreference = 'Stop'
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
        throw "$Label must be ASCII-only."
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

function Assert-OrdinaryFileSystemItem {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][ValidateSet('Leaf', 'Container')]
        [string]$PathType
    )

    if (-not (Test-Path -LiteralPath $Path -PathType $PathType)) {
        throw "$Label is missing: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label cannot be a reparse point: $Path"
    }
    return $item
}

function ConvertTo-NormalizedText {
    param([Parameter(Mandatory = $true)][string]$Value)
    return ($Value -replace "`r`n", "`n").TrimEnd("`r", "`n")
}

function Resolve-TaskPrincipalSid {
    param([Parameter(Mandatory = $true)][string]$UserId)

    if ([string]::IsNullOrWhiteSpace($UserId)) {
        throw 'The scheduled task principal UserId is empty.'
    }
    try {
        if ($UserId -match '^S-\d-(?:\d+-)+\d+$') {
            return (New-Object Security.Principal.SecurityIdentifier($UserId)).Value
        }
        $account = New-Object Security.Principal.NTAccount($UserId)
        return $account.Translate([Security.Principal.SecurityIdentifier]).Value
    }
    catch {
        throw "The scheduled task principal UserId cannot be resolved to a SID: $UserId"
    }
}

function Assert-CurrentUserTaskPrincipal {
    param(
        [Parameter(Mandatory = $true)][object]$Principal,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $taskSid = Resolve-TaskPrincipalSid -UserId ([string]$Principal.UserId)
    if (
        $taskSid -cne $currentSid -or
        [string]$Principal.LogonType -ne 'Interactive' -or
        [string]$Principal.RunLevel -ne 'Limited'
    ) {
        throw "$Label is not the exact current-user SID Interactive/Limited identity."
    }
}

function Assert-ExistingHubDataIdentity {
    param([Parameter(Mandatory = $true)][string]$Root)

    [void](Assert-OrdinaryFileSystemItem -Path $Root -Label 'Existing Hub DataRoot' -PathType Container)
    # The catalog is deliberately never opened or hashed. Its ordinary-file
    # identity and nonzero metadata are the only checks this repair may make.
    $catalogPath = Join-Path $Root 'storyforge-catalog.sqlite3'
    $catalog = Assert-OrdinaryFileSystemItem -Path $catalogPath -Label 'Existing Hub catalog' -PathType Leaf
    if ([long]$catalog.Length -le 0) {
        throw "Existing Hub catalog is empty: $catalogPath"
    }
    $settingsPath = Join-Path $Root 'settings.json'
    [void](Assert-OrdinaryFileSystemItem -Path $settingsPath -Label 'Existing Hub settings' -PathType Leaf)
    try {
        $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw 'Existing Hub settings.json is invalid; refusing launcher repair.'
    }
    if ([string]$settings.settings.hub.mode -ne 'host') {
        throw 'DataRoot is not explicitly configured as a Hub host.'
    }
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-TextSha256 {
    param([Parameter(Mandatory = $true)][string]$Value)

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-VerifiedBundleRecords {
    param([Parameter(Mandatory = $true)][string]$Root)

    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $items = @(Get-ChildItem -LiteralPath $fullRoot -Recurse -Force -ErrorAction Stop)
    $records = New-Object System.Collections.Generic.List[object]
    foreach ($item in $items) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Target application contains a reparse point: $($item.FullName)"
        }
        $relative = $item.FullName.Substring($fullRoot.Length).TrimStart('\').Replace('\', '/')
        $parts = @($relative -split '/')
        if (
            ($parts.Count -gt 0 -and [string]$parts[0] -ieq 'StoryForgeData') -or
            @($parts | Where-Object { $_ -eq '.git' -or $_ -eq '__pycache__' }).Count -gt 0
        ) {
            throw "Target application contains excluded runtime or source data: $relative"
        }
        if ($item.PSIsContainer) {
            continue
        }
        if (
            $relative -ieq 'BUILD_RELEASE_VALIDATION.json' -or
            $relative -ieq 'storyforge-update.json'
        ) {
            continue
        }
        $records.Add([PSCustomObject]@{
            relative = $relative
            normalized = $relative.ToLowerInvariant()
            size = [long]$item.Length
            sha256 = Get-Sha256 -Path $item.FullName
        }) | Out-Null
    }
    $comparison = [System.Comparison[object]]{
        param($left, $right)
        return [System.StringComparer]::Ordinal.Compare(
            [string]$left.normalized,
            [string]$right.normalized
        )
    }
    $records.Sort($comparison)
    $ordered = @($records.ToArray())
    for ($index = 1; $index -lt $ordered.Count; $index += 1) {
        if ([string]$ordered[$index - 1].normalized -eq [string]$ordered[$index].normalized) {
            throw "Target application contains case-insensitive duplicate paths: $($ordered[$index].relative)"
        }
    }
    return $ordered
}

function Assert-VerifiedTargetRelease {
    param(
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$ExpectedHubRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedDataRoot
    )

    $targetRoot = Get-SafeFixedPath -Value $Target -Label 'TargetAppDirectory'
    $targetName = Split-Path -Leaf $targetRoot
    if ($targetName -notmatch '^App-(?<version>[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)$') {
        throw 'TargetAppDirectory must be an exact managed App-version directory.'
    }
    $targetVersion = [string]$Matches['version']
    if ($targetRoot -ne (Join-Path $ExpectedHubRoot "App-$targetVersion")) {
        throw 'TargetAppDirectory must be directly inside HubRoot.'
    }
    $targetPrefix = $targetRoot.TrimEnd('\') + '\'
    $dataPrefix = $ExpectedDataRoot.TrimEnd('\') + '\'
    if (
        $targetRoot.Equals($ExpectedDataRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        $targetRoot.StartsWith($dataPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
        $ExpectedDataRoot.StartsWith($targetPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    ) {
        throw 'TargetAppDirectory and DataRoot must not overlap.'
    }
    [void](Assert-OrdinaryFileSystemItem -Path $targetRoot -Label 'Verified target application directory' -PathType Container)

    $targetEntrypoint = Join-Path $targetRoot 'StoryForge Studio.exe'
    [void](Assert-OrdinaryFileSystemItem -Path $targetEntrypoint -Label 'Verified target executable' -PathType Leaf)
    $targetManifestPath = Join-Path $targetRoot 'storyforge-update.json'
    [void](Assert-OrdinaryFileSystemItem -Path $targetManifestPath -Label 'Target internal manifest' -PathType Leaf)
    try {
        $targetManifest = Get-Content -LiteralPath $targetManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw 'Target storyforge-update.json is invalid.'
    }
    if (
        [int]$targetManifest.schema_version -ne 1 -or
        [string]$targetManifest.version -ne $targetVersion -or
        [string]$targetManifest.entrypoint -ne 'StoryForge Studio.exe'
    ) {
        throw 'Target internal manifest does not match TargetAppDirectory.'
    }

    $releaseValidationPath = Join-Path $targetRoot 'BUILD_RELEASE_VALIDATION.json'
    [void](Assert-OrdinaryFileSystemItem -Path $releaseValidationPath -Label 'Target release validation' -PathType Leaf)
    try {
        $releaseValidation = Get-Content -LiteralPath $releaseValidationPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw 'Target BUILD_RELEASE_VALIDATION.json is invalid.'
    }
    if (
        [int]$releaseValidation.schema_version -ne 1 -or
        $releaseValidation.ok -ne $true -or
        $releaseValidation.frozen -ne $true -or
        [string]$releaseValidation.app_version -ne $targetVersion -or
        [string]$releaseValidation.entrypoint -ne 'StoryForge Studio.exe'
    ) {
        throw 'Target release validation does not match the frozen target release.'
    }
    $entrypointDigest = ([string]$releaseValidation.entrypoint_sha256).ToLowerInvariant()
    if (
        $entrypointDigest -notmatch '^[0-9a-f]{64}$' -or
        (Get-Sha256 -Path $targetEntrypoint) -ne $entrypointDigest
    ) {
        throw 'Target executable changed after frozen release validation.'
    }

    $startupValidationPath = Join-Path $targetRoot 'BUILD_STARTUP_VALIDATION.json'
    [void](Assert-OrdinaryFileSystemItem -Path $startupValidationPath -Label 'Target startup validation' -PathType Leaf)
    $startupDigest = ([string]$releaseValidation.startup_validation_sha256).ToLowerInvariant()
    if (
        $startupDigest -notmatch '^[0-9a-f]{64}$' -or
        (Get-Sha256 -Path $startupValidationPath) -ne $startupDigest
    ) {
        throw 'Target startup validation changed after release attestation.'
    }
    try {
        $startupValidation = Get-Content -LiteralPath $startupValidationPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw 'Target startup validation is invalid.'
    }
    if (
        $startupValidation.ok -ne $true -or
        $startupValidation.frozen -ne $true -or
        [string]$startupValidation.app_version -ne $targetVersion
    ) {
        throw 'Target startup validation is not a passed check for the target version.'
    }

    if ($releaseValidation.with_local_ai -isnot [bool]) {
        throw 'Target release validation has an invalid with_local_ai value.'
    }
    $kokoroValidationPath = Join-Path $targetRoot 'BUILD_KOKORO_VALIDATION.json'
    $kokoroDigest = [string]$releaseValidation.kokoro_validation_sha256
    if ([bool]$releaseValidation.with_local_ai) {
        [void](Assert-OrdinaryFileSystemItem -Path $kokoroValidationPath -Label 'Target Kokoro validation' -PathType Leaf)
        if (
            $kokoroDigest.ToLowerInvariant() -notmatch '^[0-9a-f]{64}$' -or
            (Get-Sha256 -Path $kokoroValidationPath) -ne $kokoroDigest.ToLowerInvariant()
        ) {
            throw 'Target Kokoro validation changed after release attestation.'
        }
        try {
            $kokoroValidation = Get-Content -LiteralPath $kokoroValidationPath -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        catch {
            throw 'Target Kokoro validation is invalid.'
        }
        if (
            $kokoroValidation.ok -ne $true -or
            $kokoroValidation.frozen -ne $true -or
            [string]$kokoroValidation.app_version -ne $targetVersion
        ) {
            throw 'Target Kokoro validation is not a passed check for the target version.'
        }
    }
    elseif ($kokoroDigest -or (Test-Path -LiteralPath $kokoroValidationPath)) {
        throw 'Lightweight target release contains stale Kokoro validation.'
    }

    $records = @(Get-VerifiedBundleRecords -Root $targetRoot)
    [long]$actualBundleSize = 0
    foreach ($record in $records) {
        $actualBundleSize += [long]$record.size
    }
    if (
        [int]$releaseValidation.bundle_file_count -ne $records.Count -or
        [long]$releaseValidation.bundle_size_bytes -ne $actualBundleSize
    ) {
        throw 'Target release file count or size does not match its release validation.'
    }
    $declaredFiles = @($releaseValidation.bundle_files)
    if ($declaredFiles.Count -ne $records.Count) {
        throw 'Target release file allow-list does not match the installed target.'
    }
    $manifestText = New-Object System.Text.StringBuilder
    for ($index = 0; $index -lt $records.Count; $index += 1) {
        if ([string]$declaredFiles[$index] -cne [string]$records[$index].relative) {
            throw 'Target release file allow-list does not match the installed target.'
        }
        [void]$manifestText.Append([string]$records[$index].relative)
        [void]$manifestText.Append([char]0)
        [void]$manifestText.Append([string]$records[$index].size)
        [void]$manifestText.Append([char]0)
        [void]$manifestText.Append([string]$records[$index].sha256)
        [void]$manifestText.Append("`n")
    }
    $bundleDigest = ([string]$releaseValidation.bundle_manifest_sha256).ToLowerInvariant()
    if (
        $bundleDigest -notmatch '^[0-9a-f]{64}$' -or
        (Get-TextSha256 -Value $manifestText.ToString()) -ne $bundleDigest
    ) {
        throw 'Target application tree does not match BUILD_RELEASE_VALIDATION.json.'
    }

    return [PSCustomObject]@{
        Version = $targetVersion
        AppDirectory = $targetRoot
        Entrypoint = $targetEntrypoint
    }
}

function Get-LegacyOpsLauncherContent {
    return @'
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
'@
}

function Assert-LegacyOpsTaskIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedHubRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedDataRoot,
        [Parameter(Mandatory = $true)][string]$LegacyLauncherPath,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskName,
        [Parameter(Mandatory = $true)][int]$ExpectedPort,
        [Parameter(Mandatory = $true)][string]$CanonicalPowerShell
    )

    [void](Assert-OrdinaryFileSystemItem -Path $LegacyLauncherPath -Label 'Legacy ops launcher' -PathType Leaf)
    $legacyLauncherContent = [System.IO.File]::ReadAllText(
        $LegacyLauncherPath,
        [System.Text.Encoding]::ASCII
    )
    if (
        (ConvertTo-NormalizedText -Value $legacyLauncherContent) -cne
        (ConvertTo-NormalizedText -Value (Get-LegacyOpsLauncherContent))
    ) {
        throw 'The legacy Start-StoryForgeHub.ps1 is not the exact supported ops wrapper.'
    }

    $tasks = @(Get-ScheduledTask -TaskName $ExpectedTaskName -TaskPath '\' -ErrorAction SilentlyContinue)
    if ($tasks.Count -ne 1) {
        throw "Exactly one existing root task named '$ExpectedTaskName' is required."
    }
    $task = $tasks[0]
    $actions = @($task.Actions)
    if ($actions.Count -ne 1) {
        throw 'The legacy Hub task must contain exactly one action.'
    }
    $action = $actions[0]
    $actionExecutable = [string]$action.Execute
    $executableMatches = $actionExecutable -ieq 'powershell.exe'
    if ([System.IO.Path]::IsPathRooted($actionExecutable)) {
        $executableMatches = (
            [System.IO.Path]::GetFullPath($actionExecutable) -eq
            [System.IO.Path]::GetFullPath($CanonicalPowerShell)
        )
    }
    if (-not $executableMatches -or -not [string]::IsNullOrWhiteSpace([string]$action.WorkingDirectory)) {
        throw 'The legacy task executable or empty WorkingDirectory does not match the supported ops task.'
    }
    Assert-CurrentUserTaskPrincipal `
        -Principal $task.Principal `
        -Label 'The legacy task principal'

    $arguments = [string]$action.Arguments
    $argumentTemplates = @(
        [PSCustomObject]@{
            Prefix = (
                "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden " +
                "-ExecutionPolicy Bypass -File `"$LegacyLauncherPath`" -InstallPath `""
            )
            Suffix = "`" -DataPath `"$ExpectedDataRoot`" -Port $ExpectedPort"
        },
        [PSCustomObject]@{
            Prefix = "-NoProfile -ExecutionPolicy Bypass -File $LegacyLauncherPath -InstallPath "
            Suffix = " -DataPath $ExpectedDataRoot -Port $ExpectedPort"
        }
    )
    $matchingTemplates = @(
        foreach ($template in $argumentTemplates) {
            if (
                $arguments.StartsWith(
                    [string]$template.Prefix,
                    [System.StringComparison]::Ordinal
                ) -and
                $arguments.EndsWith(
                    [string]$template.Suffix,
                    [System.StringComparison]::Ordinal
                ) -and
                $arguments.Length -gt (
                    ([string]$template.Prefix).Length +
                    ([string]$template.Suffix).Length
                )
            ) {
                $template
            }
        }
    )
    if ($matchingTemplates.Count -ne 1) {
        throw 'The legacy task arguments do not match the exact supported parameterized ops action.'
    }
    $prefix = [string]$matchingTemplates[0].Prefix
    $suffix = [string]$matchingTemplates[0].Suffix
    $previousValue = $arguments.Substring(
        $prefix.Length,
        $arguments.Length - $prefix.Length - $suffix.Length
    )
    $previousAppDirectory = Get-SafeFixedPath -Value $previousValue -Label 'Legacy InstallPath'
    $previousName = Split-Path -Leaf $previousAppDirectory
    if ($previousName -notmatch '^App-(?<version>[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)$') {
        throw 'The legacy InstallPath is not an App-version directory.'
    }
    $previousVersion = [string]$Matches['version']
    if ($previousAppDirectory -ne (Join-Path $ExpectedHubRoot "App-$previousVersion")) {
        throw 'The legacy InstallPath is outside the exact HubRoot App-version directory.'
    }
    $expectedLegacyTaskArguments = $prefix + $previousAppDirectory + $suffix
    if ($arguments -cne $expectedLegacyTaskArguments) {
        throw 'The legacy task arguments contain unsupported quoting or extra parameters.'
    }
    [void](Assert-OrdinaryFileSystemItem -Path $previousAppDirectory -Label 'Legacy application directory' -PathType Container)
    $previousEntrypoint = Join-Path $previousAppDirectory 'StoryForge Studio.exe'
    $previousExecutableItem = Assert-OrdinaryFileSystemItem -Path $previousEntrypoint -Label 'Legacy StoryForge executable' -PathType Leaf
    if ([long]$previousExecutableItem.Length -le 0) {
        throw 'The legacy StoryForge executable is empty.'
    }

    return [PSCustomObject]@{
        Task = $task
        Action = $action
        PreviousAppDirectory = $previousAppDirectory
        PreviousEntrypoint = $previousEntrypoint
        PreviousVersion = $previousVersion
    }
}

function Assert-ModernTaskIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedHubRoot,
        [Parameter(Mandatory = $true)][string]$ModernLauncherPath,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskName,
        [Parameter(Mandatory = $true)][string]$CanonicalPowerShell
    )

    $tasks = @(Get-ScheduledTask -TaskName $ExpectedTaskName -TaskPath '\' -ErrorAction SilentlyContinue)
    if ($tasks.Count -ne 1) {
        throw 'The migrated task could not be read back as one exact root task.'
    }
    $task = $tasks[0]
    $actions = @($task.Actions)
    if ($actions.Count -ne 1) {
        throw 'The migrated task does not contain exactly one action.'
    }
    $action = $actions[0]
    $executeValue = [string]$action.Execute
    $workingValue = [string]$action.WorkingDirectory
    if (
        -not [System.IO.Path]::IsPathRooted($executeValue) -or
        -not [System.IO.Path]::IsPathRooted($workingValue)
    ) {
        throw 'The migrated task action does not use absolute paths.'
    }
    $expectedArguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ModernLauncherPath`""
    if (
        [System.IO.Path]::GetFullPath($executeValue) -ne [System.IO.Path]::GetFullPath($CanonicalPowerShell) -or
        [string]$action.Arguments -cne $expectedArguments -or
        [System.IO.Path]::GetFullPath($workingValue).TrimEnd('\') -ne $ExpectedHubRoot
    ) {
        throw 'The migrated scheduled task does not match the exact modern Hub identity.'
    }
    Assert-CurrentUserTaskPrincipal `
        -Principal $task.Principal `
        -Label 'The migrated scheduled task principal'
    return $task
}

function Invoke-LegacyOpsMigration {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedHubRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedDataRoot,
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][int]$ExpectedPort,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskName,
        [Parameter(Mandatory = $true)][string]$PointerPath,
        [Parameter(Mandatory = $true)][string]$ModernLauncherPath,
        [Parameter(Mandatory = $true)][string]$DesktopLauncherPath,
        [Parameter(Mandatory = $true)][string]$LegacyLauncherPath
    )

    $powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    [void](Assert-OrdinaryFileSystemItem -Path $powerShell -Label 'Windows PowerShell 5.1' -PathType Leaf)
    $targetRelease = Assert-VerifiedTargetRelease `
        -Target $Target `
        -ExpectedHubRoot $ExpectedHubRoot `
        -ExpectedDataRoot $ExpectedDataRoot
    $legacyIdentity = Assert-LegacyOpsTaskIdentity `
        -ExpectedHubRoot $ExpectedHubRoot `
        -ExpectedDataRoot $ExpectedDataRoot `
        -LegacyLauncherPath $LegacyLauncherPath `
        -ExpectedTaskName $ExpectedTaskName `
        -ExpectedPort $ExpectedPort `
        -CanonicalPowerShell $powerShell
    if ([string]$legacyIdentity.PreviousAppDirectory -eq [string]$targetRelease.AppDirectory) {
        throw 'TargetAppDirectory must be a different verified release from the legacy InstallPath.'
    }
    $previousCoreVersion = [version](([string]$legacyIdentity.PreviousVersion -split '[-+]', 2)[0])
    $targetCoreVersion = [version](([string]$targetRelease.Version -split '[-+]', 2)[0])
    if ($targetCoreVersion -le $previousCoreVersion) {
        throw 'TargetAppDirectory must contain a newer verified release than the legacy InstallPath.'
    }

    $launcherLines = @(
        '$ErrorActionPreference = ''Stop''',
        '$env:STORYFORGE_DEPLOYMENT_ROLE = ''Hub''',
        "`$env:STORYFORGE_DATA_DIR = '$ExpectedDataRoot'",
        'Remove-Item Env:STORYFORGE_FROZEN_HUB_DATA_ROOT -ErrorAction SilentlyContinue',
        'Remove-Item Env:STORYFORGE_PORTABLE_MODE -ErrorAction SilentlyContinue',
        "& '$($targetRelease.Entrypoint)' --web --web-host 0.0.0.0 --web-port $ExpectedPort",
        'exit $LASTEXITCODE'
    )
    $launcherContent = [string]::Join([Environment]::NewLine, $launcherLines) + [Environment]::NewLine
    $desktopContent = (
        "@echo off`r`n" +
        "set `"STORYFORGE_DEPLOYMENT_ROLE=Hub`"`r`n" +
        "set `"STORYFORGE_DATA_DIR=$ExpectedDataRoot`"`r`n" +
        "set `"STORYFORGE_FROZEN_HUB_DATA_ROOT=`"`r`n" +
        "set `"STORYFORGE_PORTABLE_MODE=`"`r`n" +
        "start `"`" `"$($targetRelease.Entrypoint)`"`r`n"
    )
    $pointer = [ordered]@{
        schema_version = 1
        version = [string]$targetRelease.Version
        app_directory = [string]$targetRelease.AppDirectory
        entrypoint = [string]$targetRelease.Entrypoint
        installed_at_utc = [DateTime]::UtcNow.ToString('o')
        source_release = "v$($targetRelease.Version)"
    }
    $pointerContent = ($pointer | ConvertTo-Json) + [Environment]::NewLine
    $taskArguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ModernLauncherPath`""
    $newTaskAction = New-ScheduledTaskAction `
        -Execute $powerShell `
        -Argument $taskArguments `
        -WorkingDirectory $ExpectedHubRoot

    $launcherTemporary = Join-Path $ExpectedHubRoot ('.legacy-hub-launcher-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $desktopTemporary = Join-Path $ExpectedHubRoot ('.legacy-desktop-launcher-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $pointerTemporary = Join-Path $ExpectedHubRoot ('.legacy-current-pointer-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $createdPaths = New-Object System.Collections.Generic.List[string]
    $taskUpdateAttempted = $false
    try {
        [System.IO.File]::WriteAllText($launcherTemporary, $launcherContent, [System.Text.Encoding]::ASCII)
        [System.IO.File]::WriteAllText($desktopTemporary, $desktopContent, [System.Text.Encoding]::ASCII)
        [System.IO.File]::WriteAllText(
            $pointerTemporary,
            $pointerContent,
            (New-Object System.Text.UTF8Encoding($false))
        )

        # Rebind the mutation to the identity observed immediately before the
        # atomic file moves. A partial or concurrently changed state fails closed.
        foreach ($destination in @($PointerPath, $ModernLauncherPath, $DesktopLauncherPath)) {
            if (Test-Path -LiteralPath $destination) {
                throw "A modern deployment artifact appeared during migration: $destination"
            }
        }
        [void](Assert-LegacyOpsTaskIdentity `
            -ExpectedHubRoot $ExpectedHubRoot `
            -ExpectedDataRoot $ExpectedDataRoot `
            -LegacyLauncherPath $LegacyLauncherPath `
            -ExpectedTaskName $ExpectedTaskName `
            -ExpectedPort $ExpectedPort `
            -CanonicalPowerShell $powerShell)
        Assert-ExistingHubDataIdentity -Root $ExpectedDataRoot
        $confirmedTargetRelease = Assert-VerifiedTargetRelease `
            -Target $Target `
            -ExpectedHubRoot $ExpectedHubRoot `
            -ExpectedDataRoot $ExpectedDataRoot
        if (
            [string]$confirmedTargetRelease.Version -ne [string]$targetRelease.Version -or
            [string]$confirmedTargetRelease.AppDirectory -ne [string]$targetRelease.AppDirectory -or
            [string]$confirmedTargetRelease.Entrypoint -ne [string]$targetRelease.Entrypoint
        ) {
            throw 'Target release identity changed during legacy migration validation.'
        }

        [System.IO.File]::Move($launcherTemporary, $ModernLauncherPath)
        $createdPaths.Add($ModernLauncherPath) | Out-Null
        [System.IO.File]::Move($desktopTemporary, $DesktopLauncherPath)
        $createdPaths.Add($DesktopLauncherPath) | Out-Null
        [System.IO.File]::Move($pointerTemporary, $PointerPath)
        $createdPaths.Add($PointerPath) | Out-Null

        # Updating the definition does not start or stop the currently running
        # task instance. The new action takes effect only at the next approved run.
        $taskUpdateAttempted = $true
        Set-ScheduledTask `
            -TaskName $ExpectedTaskName `
            -TaskPath '\' `
            -Action $newTaskAction `
            -ErrorAction Stop | Out-Null
        [void](Assert-ModernTaskIdentity `
            -ExpectedHubRoot $ExpectedHubRoot `
            -ModernLauncherPath $ModernLauncherPath `
            -ExpectedTaskName $ExpectedTaskName `
            -CanonicalPowerShell $powerShell)
    }
    catch {
        $migrationError = $_
        $taskRollbackError = $null
        if ($taskUpdateAttempted) {
            try {
                Set-ScheduledTask `
                    -TaskName $ExpectedTaskName `
                    -TaskPath '\' `
                    -Action $legacyIdentity.Action `
                    -ErrorAction Stop | Out-Null
                [void](Assert-LegacyOpsTaskIdentity `
                    -ExpectedHubRoot $ExpectedHubRoot `
                    -ExpectedDataRoot $ExpectedDataRoot `
                    -LegacyLauncherPath $LegacyLauncherPath `
                    -ExpectedTaskName $ExpectedTaskName `
                    -ExpectedPort $ExpectedPort `
                    -CanonicalPowerShell $powerShell)
            }
            catch {
                $taskRollbackError = $_
            }
        }
        if ($null -eq $taskRollbackError) {
            foreach ($createdPath in $createdPaths) {
                if (Test-Path -LiteralPath $createdPath -PathType Leaf) {
                    Remove-Item -LiteralPath $createdPath -Force -ErrorAction SilentlyContinue
                }
            }
        }
        else {
            throw (
                'Legacy Hub migration failed and the task action could not be rolled back. ' +
                'The generated modern launchers were retained so either task action remains resolvable. ' +
                "Migration error: $($migrationError.Exception.Message) " +
                "Rollback error: $($taskRollbackError.Exception.Message)"
            )
        }
        throw $migrationError
    }
    finally {
        foreach ($temporary in @($launcherTemporary, $desktopTemporary, $pointerTemporary)) {
            if (Test-Path -LiteralPath $temporary -PathType Leaf) {
                Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
            }
        }
    }

    Write-Host 'Legacy StoryForge Hub task migrated to the verified modern launchers.'
    Write-Host 'The task was not started or stopped, and DataRoot was not changed.'
    Write-Host 'Restart the existing Hub only during an approved maintenance window.'
}

$HubRoot = Get-SafeFixedPath -Value $HubRoot -Label 'HubRoot'
$DataRoot = Get-SafeFixedPath -Value $DataRoot -Label 'DataRoot'
if ($HubRoot -eq $DataRoot) {
    throw 'DataRoot must be a dedicated directory, not HubRoot itself.'
}
[void](Assert-OrdinaryFileSystemItem -Path $HubRoot -Label 'Managed Hub root' -PathType Container)
Assert-ExistingHubDataIdentity -Root $DataRoot

$pointerPath = Join-Path $HubRoot 'current.json'
$launcherPath = Join-Path $HubRoot 'Start-StoryForge-Hub.ps1'
$desktopLauncherPath = Join-Path $HubRoot 'Start-StoryForge.cmd'
$legacyOpsLauncherPath = Join-Path $HubRoot 'Start-StoryForgeHub.ps1'
$modernArtifacts = @($pointerPath, $launcherPath, $desktopLauncherPath)
$modernArtifactCount = @(
    $modernArtifacts | Where-Object { Test-Path -LiteralPath $_ }
).Count
if ($modernArtifactCount -gt 0 -and $modernArtifactCount -lt $modernArtifacts.Count) {
    throw 'Existing Hub has a partial modern launcher/pointer state; refusing to infer or overwrite it.'
}
if ($modernArtifactCount -eq 0) {
    if ([string]::IsNullOrWhiteSpace($TargetAppDirectory)) {
        throw 'Legacy ops-task migration requires explicit -TargetAppDirectory after verified package installation.'
    }
    if ($DataRoot -ne (Join-Path $HubRoot 'Data')) {
        throw 'The supported legacy ops-task layout requires DataRoot to be HubRoot\Data.'
    }
    Invoke-LegacyOpsMigration `
        -ExpectedHubRoot $HubRoot `
        -ExpectedDataRoot $DataRoot `
        -Target $TargetAppDirectory `
        -ExpectedPort $Port `
        -ExpectedTaskName $TaskName `
        -PointerPath $pointerPath `
        -ModernLauncherPath $launcherPath `
        -DesktopLauncherPath $desktopLauncherPath `
        -LegacyLauncherPath $legacyOpsLauncherPath
    exit 0
}
if (-not [string]::IsNullOrWhiteSpace($TargetAppDirectory)) {
    throw '-TargetAppDirectory is only valid for the exact legacy ops-task migration path.'
}

[void](Assert-OrdinaryFileSystemItem -Path $pointerPath -Label 'Managed deployment pointer' -PathType Leaf)
try {
    $pointer = Get-Content -LiteralPath $pointerPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    throw 'current.json is invalid; refusing launcher repair.'
}
if ([int]$pointer.schema_version -ne 1) {
    throw 'current.json uses an unsupported schema.'
}
$pointerVersion = [string]$pointer.version
if ($pointerVersion -notmatch '^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$') {
    throw 'current.json contains an invalid version.'
}
$appDirectory = Get-SafeFixedPath -Value ([string]$pointer.app_directory) -Label 'app_directory'
$expectedAppDirectory = Join-Path $HubRoot ("App-$pointerVersion")
if ($appDirectory -ne $expectedAppDirectory) {
    throw 'current.json does not identify the exact managed App-version directory.'
}
[void](Assert-OrdinaryFileSystemItem -Path $appDirectory -Label 'Managed application directory' -PathType Container)

$entrypoint = Get-SafeFixedPath -Value ([string]$pointer.entrypoint) -Label 'entrypoint'
$expectedEntrypoint = Join-Path $appDirectory 'StoryForge Studio.exe'
if ($entrypoint -ne $expectedEntrypoint) {
    throw 'current.json does not identify the exact managed StoryForge executable.'
}
[void](Assert-OrdinaryFileSystemItem -Path $entrypoint -Label 'Managed StoryForge executable' -PathType Leaf)

$internalManifestPath = Join-Path $appDirectory 'storyforge-update.json'
[void](Assert-OrdinaryFileSystemItem -Path $internalManifestPath -Label 'Internal release manifest' -PathType Leaf)
try {
    $internalManifest = Get-Content -LiteralPath $internalManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    throw 'The internal release manifest is invalid.'
}
if (
    [string]$internalManifest.version -ne $pointerVersion -or
    [string]$internalManifest.entrypoint -ne 'StoryForge Studio.exe'
) {
    throw 'current.json and the internal release manifest do not match.'
}

[void](Assert-OrdinaryFileSystemItem -Path $launcherPath -Label 'Existing Hub launcher' -PathType Leaf)
[void](Assert-OrdinaryFileSystemItem -Path $desktopLauncherPath -Label 'Existing Hub desktop launcher' -PathType Leaf)
$powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
[void](Assert-OrdinaryFileSystemItem -Path $powerShell -Label 'Windows PowerShell 5.1' -PathType Leaf)
$expectedTaskArguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcherPath`""

$tasks = @(Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue)
if ($tasks.Count -ne 1) {
    throw "Exactly one existing root task named '$TaskName' is required."
}
$task = $tasks[0]
$actions = @($task.Actions)
if ($actions.Count -ne 1) {
    throw 'The existing Hub task must contain exactly one action.'
}
$action = $actions[0]
$actionExecutableValue = [string]$action.Execute
$actionWorkingDirectoryValue = [string]$action.WorkingDirectory
if (
    [string]::IsNullOrWhiteSpace($actionExecutableValue) -or
    [string]::IsNullOrWhiteSpace($actionWorkingDirectoryValue) -or
    -not [System.IO.Path]::IsPathRooted($action.Execute) -or
    -not [System.IO.Path]::IsPathRooted($action.WorkingDirectory)
) {
    throw 'The existing Hub task action must use absolute executable and working-directory paths.'
}
$actualTaskExecutable = [System.IO.Path]::GetFullPath($actionExecutableValue)
$actualWorkingDirectory = [System.IO.Path]::GetFullPath($actionWorkingDirectoryValue).TrimEnd('\')
if (
    $actualTaskExecutable -ne [System.IO.Path]::GetFullPath($powerShell) -or
    [string]$action.Arguments -ne $expectedTaskArguments -or
    $actualWorkingDirectory -ne $HubRoot
) {
    throw 'The existing scheduled task is not the exact managed StoryForge Hub identity.'
}
Assert-CurrentUserTaskPrincipal `
    -Principal $task.Principal `
    -Label 'The existing scheduled task principal'

$newLauncherLines = @(
    '$ErrorActionPreference = ''Stop''',
    '$env:STORYFORGE_DEPLOYMENT_ROLE = ''Hub''',
    "`$env:STORYFORGE_DATA_DIR = '$DataRoot'",
    'Remove-Item Env:STORYFORGE_FROZEN_HUB_DATA_ROOT -ErrorAction SilentlyContinue',
    'Remove-Item Env:STORYFORGE_PORTABLE_MODE -ErrorAction SilentlyContinue',
    "& '$entrypoint' --web --web-host 0.0.0.0 --web-port $Port",
    'exit $LASTEXITCODE'
)
$newLauncherContent = [string]::Join([Environment]::NewLine, $newLauncherLines) + [Environment]::NewLine
$existingLauncherContent = [System.IO.File]::ReadAllText($launcherPath, [System.Text.Encoding]::ASCII)
$normalizedExisting = ConvertTo-NormalizedText -Value $existingLauncherContent
$normalizedNew = ConvertTo-NormalizedText -Value $newLauncherContent
$hubLauncherNeedsRepair = $normalizedExisting -ne $normalizedNew

$newDesktopLauncherContent = (
    "@echo off`r`n" +
    "set `"STORYFORGE_DEPLOYMENT_ROLE=Hub`"`r`n" +
    "set `"STORYFORGE_DATA_DIR=$DataRoot`"`r`n" +
    "set `"STORYFORGE_FROZEN_HUB_DATA_ROOT=`"`r`n" +
    "set `"STORYFORGE_PORTABLE_MODE=`"`r`n" +
    "start `"`" `"$entrypoint`"`r`n"
)
$existingDesktopLauncherContent = [System.IO.File]::ReadAllText(
    $desktopLauncherPath,
    [System.Text.Encoding]::ASCII
)
$normalizedExistingDesktop = ConvertTo-NormalizedText -Value $existingDesktopLauncherContent
$normalizedNewDesktop = ConvertTo-NormalizedText -Value $newDesktopLauncherContent
$desktopLauncherNeedsRepair = $normalizedExistingDesktop -ne $normalizedNewDesktop

if (-not $hubLauncherNeedsRepair -and -not $desktopLauncherNeedsRepair) {
    Write-Host 'StoryForge Hub launchers already use the current fixed deployment contract.'
    exit 0
}

$previousEntrypointValue = ''
if ($hubLauncherNeedsRepair) {
    $existingLauncherLines = @($normalizedExisting -split "`n")
    $oldLauncherPattern = "^& '(?<entrypoint>.+)' --web --web-host 0[.]0[.]0[.]0 --web-port (?<port>[0-9]+)`r?$"
    $oldLauncherMatches = @(
        $existingLauncherLines |
            Where-Object { $_.StartsWith("& '") -and $_ -like '* --web --web-host *' }
    )
    if ($oldLauncherMatches.Count -ne 1) {
        throw 'The existing Hub launcher is not an exact supported managed launcher.'
    }
    if (-not ($oldLauncherMatches[0] -match $oldLauncherPattern)) {
        throw 'The existing Hub launch command does not match the supported managed form.'
    }
    $previousEntrypointValue = [string]$Matches['entrypoint']
    $previousPort = [int]$Matches['port']
    if ($previousPort -ne $Port) {
        throw 'The previous launcher port does not match the requested managed Hub port.'
    }
}
else {
    $existingDesktopLauncherLines = @($normalizedExistingDesktop -split "`n")
    $desktopCommandPattern = '^start "" "(?<entrypoint>.+)"(?: %[*])?$'
    $desktopCommandMatches = @(
        $existingDesktopLauncherLines |
            Where-Object { $_.StartsWith('start "" "') }
    )
    if (
        $desktopCommandMatches.Count -ne 1 -or
        -not ($desktopCommandMatches[0] -match $desktopCommandPattern)
    ) {
        throw 'The existing Hub desktop launcher command is not a supported managed form.'
    }
    $previousEntrypointValue = [string]$Matches['entrypoint']
}
$previousEntrypoint = Get-SafeFixedPath `
    -Value $previousEntrypointValue `
    -Label 'Previous managed entrypoint'
$previousAppDirectory = Split-Path -Parent $previousEntrypoint
$previousAppName = Split-Path -Leaf $previousAppDirectory
if ($previousAppName -notmatch '^App-(?<version>[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)$') {
    throw 'The previous launcher entrypoint is not inside an App-version directory.'
}
$previousVersion = [string]$Matches['version']
$previousAppDirectory = Join-Path $HubRoot ("App-$previousVersion")
$expectedPreviousEntrypoint = Join-Path $previousAppDirectory 'StoryForge Studio.exe'
if ($previousEntrypoint -ne $expectedPreviousEntrypoint) {
    throw 'The previous launcher entrypoint is outside its managed App-version directory.'
}
[void](Assert-OrdinaryFileSystemItem -Path $previousAppDirectory -Label 'Previous managed application directory' -PathType Container)
[void](Assert-OrdinaryFileSystemItem -Path $previousEntrypoint -Label 'Previous managed StoryForge executable' -PathType Leaf)

$previousInternalManifestPath = Join-Path $previousAppDirectory 'storyforge-update.json'
[void](Assert-OrdinaryFileSystemItem -Path $previousInternalManifestPath -Label 'Previous internal release manifest' -PathType Leaf)
try {
    $previousInternalManifest = Get-Content -LiteralPath $previousInternalManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    throw 'The previous managed release manifest is invalid.'
}
if (
    [string]$previousInternalManifest.version -ne $previousVersion -or
    [string]$previousInternalManifest.entrypoint -ne 'StoryForge Studio.exe'
) {
    throw 'The previous managed release manifest does not match its App-version directory.'
}

$previousLegacyLauncherLines = @(
    '$ErrorActionPreference = ''Stop''',
    "`$env:STORYFORGE_DATA_DIR = '$DataRoot'",
    "& '$previousEntrypoint' --web --web-host 0.0.0.0 --web-port $Port",
    'exit $LASTEXITCODE'
)
$previousRoleLauncherLines = @(
    '$ErrorActionPreference = ''Stop''',
    '$env:STORYFORGE_DEPLOYMENT_ROLE = ''Hub''',
    "`$env:STORYFORGE_DATA_DIR = '$DataRoot'",
    'Remove-Item Env:STORYFORGE_FROZEN_HUB_DATA_ROOT -ErrorAction SilentlyContinue',
    'Remove-Item Env:STORYFORGE_PORTABLE_MODE -ErrorAction SilentlyContinue',
    "& '$previousEntrypoint' --web --web-host 0.0.0.0 --web-port $Port",
    'exit $LASTEXITCODE'
)
$previousLegacyLauncherContent = [string]::Join([Environment]::NewLine, $previousLegacyLauncherLines) + [Environment]::NewLine
$previousRoleLauncherContent = [string]::Join([Environment]::NewLine, $previousRoleLauncherLines) + [Environment]::NewLine
$normalizedPreviousLegacy = ConvertTo-NormalizedText -Value $previousLegacyLauncherContent
$normalizedPreviousRole = ConvertTo-NormalizedText -Value $previousRoleLauncherContent
if ($hubLauncherNeedsRepair -and (
    $normalizedExisting -ne $normalizedPreviousLegacy -and
    $normalizedExisting -ne $normalizedPreviousRole
)) {
    throw 'The existing Hub launcher is not an exact supported managed launcher.'
}

$previousLegacyDesktopLauncherContent = (
    "@echo off`r`n" +
    "start `"`" `"$previousEntrypoint`" %*`r`n"
)
$previousRoleDesktopLauncherContent = (
    "@echo off`r`n" +
    "set `"STORYFORGE_DEPLOYMENT_ROLE=Hub`"`r`n" +
    "set `"STORYFORGE_DATA_DIR=$DataRoot`"`r`n" +
    "set `"STORYFORGE_FROZEN_HUB_DATA_ROOT=`"`r`n" +
    "set `"STORYFORGE_PORTABLE_MODE=`"`r`n" +
    "start `"`" `"$previousEntrypoint`" %*`r`n"
)
$previousFixedDesktopLauncherContent = (
    "@echo off`r`n" +
    "set `"STORYFORGE_DEPLOYMENT_ROLE=Hub`"`r`n" +
    "set `"STORYFORGE_DATA_DIR=$DataRoot`"`r`n" +
    "set `"STORYFORGE_FROZEN_HUB_DATA_ROOT=`"`r`n" +
    "set `"STORYFORGE_PORTABLE_MODE=`"`r`n" +
    "start `"`" `"$previousEntrypoint`"`r`n"
)
$currentLegacyDesktopLauncherContent = (
    "@echo off`r`n" +
    "start `"`" `"$entrypoint`" %*`r`n"
)
$currentRoleDesktopLauncherContent = (
    "@echo off`r`n" +
    "set `"STORYFORGE_DEPLOYMENT_ROLE=Hub`"`r`n" +
    "set `"STORYFORGE_DATA_DIR=$DataRoot`"`r`n" +
    "set `"STORYFORGE_FROZEN_HUB_DATA_ROOT=`"`r`n" +
    "set `"STORYFORGE_PORTABLE_MODE=`"`r`n" +
    "start `"`" `"$entrypoint`" %*`r`n"
)
$allowedDesktopLaunchers = @(
    $previousLegacyDesktopLauncherContent,
    $previousRoleDesktopLauncherContent,
    $previousFixedDesktopLauncherContent,
    $currentLegacyDesktopLauncherContent,
    $currentRoleDesktopLauncherContent,
    $newDesktopLauncherContent
) | ForEach-Object { ConvertTo-NormalizedText -Value $_ }
if ($normalizedExistingDesktop -notin $allowedDesktopLaunchers) {
    throw 'The existing Hub desktop launcher is not an exact supported managed launcher.'
}

$temporary = Join-Path $HubRoot ('.hub-launcher-repair-' + [Guid]::NewGuid().ToString('N') + '.tmp')
$backup = Join-Path $HubRoot ('.hub-launcher-backup-' + [Guid]::NewGuid().ToString('N') + '.tmp')
$desktopTemporary = Join-Path $HubRoot ('.desktop-launcher-repair-' + [Guid]::NewGuid().ToString('N') + '.tmp')
$desktopBackup = Join-Path $HubRoot ('.desktop-launcher-backup-' + [Guid]::NewGuid().ToString('N') + '.tmp')
$hubRollbackDiscard = Join-Path $HubRoot ('.hub-launcher-rollback-' + [Guid]::NewGuid().ToString('N') + '.tmp')
$desktopRollbackDiscard = Join-Path $HubRoot ('.desktop-launcher-rollback-' + [Guid]::NewGuid().ToString('N') + '.tmp')
$hubReplaced = $false
$desktopReplaced = $false
try {
    if ($hubLauncherNeedsRepair) {
        [System.IO.File]::WriteAllText(
            $temporary,
            $newLauncherContent,
            [System.Text.Encoding]::ASCII
        )
        [System.IO.File]::Replace($temporary, $launcherPath, $backup, $true)
        $hubReplaced = $true
    }
    if ($desktopLauncherNeedsRepair) {
        [System.IO.File]::WriteAllText(
            $desktopTemporary,
            $newDesktopLauncherContent,
            [System.Text.Encoding]::ASCII
        )
        [System.IO.File]::Replace(
            $desktopTemporary,
            $desktopLauncherPath,
            $desktopBackup,
            $true
        )
        $desktopReplaced = $true
    }
}
catch {
    $replacementError = $_
    if ($desktopReplaced -and (Test-Path -LiteralPath $desktopBackup -PathType Leaf)) {
        [System.IO.File]::Replace(
            $desktopBackup,
            $desktopLauncherPath,
            $desktopRollbackDiscard,
            $true
        )
    }
    if ($hubReplaced -and (Test-Path -LiteralPath $backup -PathType Leaf)) {
        [System.IO.File]::Replace(
            $backup,
            $launcherPath,
            $hubRollbackDiscard,
            $true
        )
    }
    throw $replacementError
}
finally {
    foreach ($cleanupPath in @(
        $temporary,
        $backup,
        $desktopTemporary,
        $desktopBackup,
        $hubRollbackDiscard,
        $desktopRollbackDiscard
    )) {
        if (Test-Path -LiteralPath $cleanupPath -PathType Leaf) {
            Remove-Item -LiteralPath $cleanupPath -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host 'StoryForge Hub launchers repaired. The task and DataRoot were not changed.'
Write-Host 'Restart the existing Hub only during an approved maintenance window.'
